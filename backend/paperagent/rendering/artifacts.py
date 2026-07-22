from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from paperagent.agents.document_ir import DocumentIR, diff_documents
from paperagent.preview.schemas import PreviewAnchor


class ArtifactVersion(BaseModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    version: int = Field(ge=1)
    format: str
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_revision: int
    parent_artifact_id: UUID | None = None
    changed_block_ids: list[UUID] = Field(default_factory=list)


class AnchorBinding(BaseModel):
    artifact_id: UUID
    anchor: PreviewAnchor
    section_id: UUID
    block_id: UUID


class ArtifactVersionService:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self.connection = sqlite3.connect(self.root / "artifact-versions.db")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS artifact_versions(
              artifact_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version INTEGER NOT NULL,
              format TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(document_id,format,version)
            );
            CREATE TABLE IF NOT EXISTS artifact_anchors(
              artifact_id TEXT NOT NULL, anchor_id TEXT NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(artifact_id,anchor_id)
            );
            """
        )

    def close(self) -> None:
        self.connection.close()

    def register(
        self,
        document: DocumentIR,
        path: Path,
        *,
        parent: ArtifactVersion | None = None,
        previous_document: DocumentIR | None = None,
    ) -> ArtifactVersion:
        resolved = path.resolve()
        if self.root not in resolved.parents or not resolved.is_file():
            raise ValueError("artifact must be an existing project file")
        row = self.connection.execute(
            "SELECT MAX(version) FROM artifact_versions WHERE document_id=? AND format=?",
            (str(document.document_id), resolved.suffix.lstrip(".")),
        ).fetchone()
        version = int(row[0] or 0) + 1
        changed = (
            diff_documents(previous_document, document).changed_blocks
            if previous_document
            else [block.block_id for section in document.sections for block in section.blocks]
        )
        artifact = ArtifactVersion(
            document_id=document.document_id,
            version=version,
            format=resolved.suffix.lstrip("."),
            path=resolved.relative_to(self.root).as_posix(),
            sha256=hashlib.sha256(resolved.read_bytes()).hexdigest(),
            document_revision=document.revision,
            parent_artifact_id=parent.artifact_id if parent else None,
            changed_block_ids=changed,
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO artifact_versions VALUES (?,?,?,?,?)",
                (
                    str(artifact.artifact_id),
                    str(artifact.document_id),
                    artifact.version,
                    artifact.format,
                    artifact.model_dump_json(),
                ),
            )
        return artifact

    def bind(self, binding: AnchorBinding) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO artifact_anchors VALUES (?,?,?)",
                (str(binding.artifact_id), str(binding.anchor.id), binding.model_dump_json()),
            )

    def locate(self, artifact_id: UUID, anchor: PreviewAnchor) -> AnchorBinding | None:
        row = self.connection.execute(
            "SELECT payload_json FROM artifact_anchors WHERE artifact_id=? AND anchor_id=?",
            (str(artifact_id), str(anchor.id)),
        ).fetchone()
        if row:
            return AnchorBinding.model_validate_json(row[0])
        candidates = self.connection.execute(
            "SELECT payload_json FROM artifact_anchors WHERE artifact_id=?",
            (str(artifact_id),),
        ).fetchall()
        bindings = [AnchorBinding.model_validate_json(item[0]) for item in candidates]
        return next(
            (
                item
                for item in bindings
                if item.anchor.quote
                and anchor.quote
                and item.anchor.quote.strip() == anchor.quote.strip()
            ),
            None,
        )

    def get(self, artifact_id: UUID) -> ArtifactVersion | None:
        row = self.connection.execute(
            "SELECT payload_json FROM artifact_versions WHERE artifact_id=?",
            (str(artifact_id),),
        ).fetchone()
        return ArtifactVersion.model_validate_json(row[0]) if row else None

    def local_rerender(
        self,
        document: DocumentIR,
        changed_blocks: list[UUID],
        renderer: Callable[[DocumentIR, list[UUID]], Path],
    ) -> Path:
        existing = {block.block_id for section in document.sections for block in section.blocks}
        if not set(changed_blocks) <= existing:
            raise ValueError("local rerender references unknown blocks")
        return renderer(document, changed_blocks)
