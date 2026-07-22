from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml
from pydantic import BaseModel, Field

from paperagent.ingestion.schemas import Locator
from paperagent.knowledge.models import (
    CitationPolicy,
    Confidentiality,
    KnowledgeItem,
    KnowledgeScope,
    ReviewStatus,
    TrustLevel,
)


class PackageEntry(BaseModel):
    id: str
    version: str
    language: list[str]
    license: str
    minimum_paperagent_version: str
    content_file: str
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    changelog_file: str


class PackageManifest(BaseModel):
    schema_version: str = "1.0"
    packages: list[PackageEntry]


class KnowledgePackageManager:
    def __init__(self, current_version: str = "0.1.0") -> None:
        self.current_version = current_version

    def load(self, manifest_path: Path) -> PackageManifest:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest = PackageManifest.model_validate(data)
        root = manifest_path.parent
        ids: set[str] = set()
        for entry in manifest.packages:
            if entry.id in ids:
                raise ValueError(f"Duplicate package id: {entry.id}")
            ids.add(entry.id)
            for relative in (entry.content_file, entry.changelog_file):
                path = (root / relative).resolve()
                if root.resolve() not in path.parents:
                    raise ValueError("Package path escapes root")
                if not path.is_file():
                    raise ValueError(f"Package file missing: {relative}")
            content = (root / entry.content_file).read_bytes()
            actual = "sha256:" + hashlib.sha256(content).hexdigest()
            if actual != entry.content_hash:
                raise ValueError(f"Package hash mismatch: {entry.id}")
            if not entry.license.strip():
                raise ValueError(f"Package license missing: {entry.id}")
            if self._version_tuple(entry.minimum_paperagent_version) > self._version_tuple(
                self.current_version
            ):
                raise ValueError(f"Package requires newer PaperAgent: {entry.id}")
        return manifest

    def items(self, manifest_path: Path) -> list[KnowledgeItem]:
        manifest = self.load(manifest_path)
        root = manifest_path.parent
        return [
            KnowledgeItem(
                id=uuid5(NAMESPACE_URL, f"paperagent://builtin/{entry.id}/{entry.version}"),
                collection_id=entry.id,
                scope=KnowledgeScope.BUILTIN,
                content_type="rule",
                title=entry.id,
                content=(root / entry.content_file).read_text(encoding="utf-8"),
                language="mixed" if len(entry.language) > 1 else entry.language[0],
                source_kind="official",
                source_uri=f"paperagent://builtin/{entry.id}/{entry.version}",
                author_or_owner="PaperAgent",
                version=entry.version,
                license=entry.license,
                confidentiality=Confidentiality.PUBLIC,
                trust_level=TrustLevel.NORMATIVE,
                citation_policy=CitationPolicy.NEVER,
                instruction_trust=False,
                locator=Locator(json_path="$"),
                content_hash=entry.content_hash.removeprefix("sha256:"),
                tags=["builtin", entry.id],
                review_status=ReviewStatus.ACCEPTED,
            )
            for entry in manifest.packages
        ]

    def atomic_install(self, source: Path, destination: Path) -> None:
        self.load(source / "manifest.yaml")
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="knowledge-stage-", dir=destination.parent))
        backup = destination.with_name(destination.name + ".previous")
        try:
            shutil.copytree(source, staging / "package", dirs_exist_ok=True)
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                os.replace(destination, backup)
            os.replace(staging / "package", destination)
        except BaseException:
            if not destination.exists() and backup.exists():
                os.replace(backup, destination)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in value.split("."))
        except ValueError as error:
            raise ValueError(f"Invalid semantic version: {value}") from error
