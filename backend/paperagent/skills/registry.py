from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from paperagent.skills.security import SecurityReport


class SkillSource(StrEnum):
    BUILTIN = "builtin"
    GITHUB = "github"
    LOCAL = "local"


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    name: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    source: SkillSource
    source_uri: str | None = None
    source_commit: str | None = None
    license: str
    entry: str = "SKILL.md"
    capabilities: list[str]
    permissions: list[str] = Field(default_factory=list)
    runtime_profile: str | None = None
    providers: list[str] = Field(default_factory=list)
    checksum: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    enabled: bool = False
    min_paperagent_version: str = "0.1.0"
    prompt_layer: str = "skill"

    @model_validator(mode="after")
    def safe_hierarchy(self) -> SkillManifest:
        if self.prompt_layer != "skill":
            raise ValueError(
                "Skill cannot claim system, security, developer, or user prompt authority"
            )
        if self.source is SkillSource.GITHUB and not (self.source_uri and self.source_commit):
            raise ValueError("GitHub Skill requires source URI and pinned commit")
        return self


class InstalledSkill(BaseModel):
    manifest: SkillManifest
    installed_path: str
    installed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    previous_version: str | None = None


class SkillCatalogEntry(BaseModel):
    id: str
    name: str
    version: str
    description: str
    capabilities: list[str]
    checksum: str
    path: str
    enabled: bool


class LoadedSkill(BaseModel):
    catalog: SkillCatalogEntry
    instructions: str
    available_references: list[str] = Field(default_factory=list)
    loaded_references: dict[str, str] = Field(default_factory=dict)


class ProgressiveSkillLoader:
    """Keep metadata resident and load instructions/references only after a match."""

    DOCUMENT_ALLOWLIST: ClassVar[set[str]] = {
        "document.numbering.inspect",
        "document.numbering.normalize",
        "document.template.inspect",
        "document.layout.resolve",
        "document.layout.patch",
        "document.render",
        "document.qa",
        "document.repair",
    }

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._catalog = self._scan()

    def catalog(self) -> list[SkillCatalogEntry]:
        return list(self._catalog.values())

    def match(self, request: str) -> list[SkillCatalogEntry]:
        if not re.search(
            r"论文|报告|文档|排版|格式|字体|字号|标题|编号|模板|页眉|页脚|"
            r"docx|pdf|markdown|layout|typography|numbering|template",
            request,
            re.I,
        ):
            return []
        return [
            item
            for item in self._catalog.values()
            if item.enabled and item.id == "professional-document-layout"
        ]

    def load(self, skill_id: str) -> LoadedSkill:
        entry = self._catalog.get(skill_id)
        if entry is None or not entry.enabled:
            raise KeyError(f"Skill is unavailable: {skill_id}")
        root = Path(entry.path)
        manifest = SkillRegistry(root.parent.parent).load_manifest(root / "manifest.yaml")
        if manifest.permissions:
            raise PermissionError("built-in layout Skill cannot request runtime permissions")
        if not set(manifest.capabilities) <= self.DOCUMENT_ALLOWLIST:
            raise PermissionError("built-in layout Skill declares a tool outside its allowlist")
        if tree_checksum(root) != manifest.checksum:
            raise ValueError("Skill checksum mismatch")
        instructions = (root / manifest.entry).read_text(encoding="utf-8")
        references = sorted(
            item.relative_to(root).as_posix()
            for item in (root / "references").glob("*.md")
            if item.is_file()
        )
        return LoadedSkill(
            catalog=entry,
            instructions=instructions,
            available_references=references,
        )

    def load_reference(self, loaded: LoadedSkill, reference: str) -> LoadedSkill:
        if reference not in loaded.available_references:
            raise ValueError("Skill reference is not declared")
        root = Path(loaded.catalog.path).resolve()
        path = (root / reference).resolve()
        if root not in path.parents or path.suffix.casefold() != ".md":
            raise ValueError("Skill reference escapes the Skill root")
        result = loaded.model_copy(deep=True)
        result.loaded_references[reference] = path.read_text(encoding="utf-8")
        return result

    def _scan(self) -> dict[str, SkillCatalogEntry]:
        entries: dict[str, SkillCatalogEntry] = {}
        if not self.root.is_dir():
            return entries
        registry = SkillRegistry(self.root.parent)
        for manifest_path in sorted(self.root.glob("*/manifest.yaml")):
            manifest = registry.load_manifest(manifest_path)
            skill_root = manifest_path.parent
            frontmatter = (skill_root / manifest.entry).read_text(encoding="utf-8")[:2048]
            description_match = re.search(r"(?m)^description:\s*(.+)$", frontmatter)
            entries[manifest.id] = SkillCatalogEntry(
                id=manifest.id,
                name=manifest.name,
                version=manifest.version,
                description=(description_match.group(1).strip() if description_match else ""),
                capabilities=manifest.capabilities,
                checksum=manifest.checksum,
                path=str(skill_root),
                enabled=manifest.enabled,
            )
        return entries


def tree_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        if ".git" in file.parts or file.name == "manifest.yaml":
            continue
        digest.update(file.relative_to(path).as_posix().encode())
        digest.update(file.read_bytes())
    return f"sha256:{digest.hexdigest()}"


class SkillRegistry:
    def __init__(self, root: Path, app_version: str = "0.1.0") -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index = self.root / "registry.json"
        self.app_version = app_version

    def load_manifest(self, path: Path) -> SkillManifest:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Skill manifest must be a mapping")
        return SkillManifest.model_validate(raw)

    def list(self) -> list[InstalledSkill]:
        if not self.index.exists():
            return []
        return [
            InstalledSkill.model_validate(item)
            for item in json.loads(self.index.read_text("utf-8"))
        ]

    def install(self, source: Path, manifest: SkillManifest, *, approved: bool) -> InstalledSkill:
        if not approved:
            raise PermissionError("Skill review approval is required")
        if tree_checksum(source) != manifest.checksum:
            raise ValueError("Skill checksum mismatch")
        if not (source / manifest.entry).is_file():
            raise ValueError("Skill entry is missing")
        installed = self.list()
        same = [item for item in installed if item.manifest.id == manifest.id]
        if same and same[-1].manifest.version == manifest.version:
            raise ValueError("Skill version is already installed")
        if same and not set(manifest.permissions) <= set(same[-1].manifest.permissions):
            raise PermissionError("Skill update requests additional permissions")
        destination = self.root / manifest.id / manifest.version
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        record = InstalledSkill(
            manifest=manifest,
            installed_path=destination.relative_to(self.root).as_posix(),
            previous_version=same[-1].manifest.version if same else None,
        )
        installed.append(record)
        self.index.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in installed], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        return record

    def install_reviewed(
        self,
        source: Path,
        manifest: SkillManifest,
        report: SecurityReport,
        *,
        approved: bool,
    ) -> InstalledSkill:
        if report.blocked:
            raise PermissionError("Skill security report contains unwaived high-risk findings")
        if not report.licenses:
            raise PermissionError("Skill has no detected license or notice")
        return self.install(source, manifest, approved=approved)

    def enable(self, skill_id: str, version: str) -> InstalledSkill:
        installed = self.list()
        selected = None
        for item in installed:
            if item.manifest.id == skill_id:
                item.manifest.enabled = item.manifest.version == version
                if item.manifest.enabled:
                    selected = item
        if selected is None:
            raise KeyError(f"Skill version not installed: {skill_id}@{version}")
        self.index.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in installed], ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        return selected

    def rollback(self, skill_id: str) -> InstalledSkill:
        versions = [item for item in self.list() if item.manifest.id == skill_id]
        active = next((item for item in versions if item.manifest.enabled), None)
        if active is None or active.previous_version is None:
            raise ValueError("No previous Skill version is available")
        return self.enable(skill_id, active.previous_version)
