from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock, RLock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from paperagent.agents.document_ir import DocumentIR, diff_documents, migrate_document_ir
from paperagent.artifacts.service import ArtifactService
from paperagent.db.manager import DatabaseManager
from paperagent.db.models import DocumentRecord, DocumentRevisionAssetRecord, DocumentRevisionRecord
from paperagent.rendering.delivery import RevisionStatus


class DocumentRevisionConflict(RuntimeError):
    def __init__(
        self, expected_revision: int, current_revision: int, changed_block_ids: list[UUID]
    ) -> None:
        super().__init__(
            f"document revision conflict: expected {expected_revision}, current {current_revision}"
        )
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        self.changed_block_ids = changed_block_ids


class DocumentSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: DocumentIR
    revision: int
    content_hash: str
    structure_hash: str = ""
    style_hash: str = ""
    asset_set_hash: str = ""
    citation_set_hash: str = ""
    presentation_hash: str = ""
    numbering_hash: str = ""
    read_only: bool = True


class DocumentRevisionNotFound(FileNotFoundError):
    pass


_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = Lock()
_PERSISTENCE_ORDER: dict[str, int] = {}
_DOCUMENT_PERSISTENCE_ORDER: dict[tuple[str, str], int] = {}


def _persistence_order(root: Path, document: DocumentIR, current: Path) -> int:
    memory_order = _DOCUMENT_PERSISTENCE_ORDER.get((str(root), str(document.document_id)))
    if memory_order is not None:
        return memory_order
    try:
        return current.stat().st_mtime_ns
    except OSError:
        return 0


class DocumentRevisionStore:
    """File-first revision storage; current.json remains restart-safe and user inspectable."""

    def __init__(
        self,
        project_root: Path,
        *,
        databases: DatabaseManager | None = None,
        project_id: str | None = None,
        artifact_service: ArtifactService | None = None,
    ) -> None:
        self.root = project_root.resolve() / ".paperagent" / "documents"
        self.project_root = project_root.resolve()
        self.databases = databases
        self.project_id = project_id
        self.artifact_service = artifact_service
        self.last_canonical_artifact_id: str | None = None
        if (databases is None) != (project_id is None):
            raise ValueError("databases and project_id must be configured together")
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root), RLock())

    def save(
        self,
        document: DocumentIR,
        *,
        parent_revision_id: str | None = None,
        source_message_id: str | None = None,
        source_run_id: str | None = None,
        source_conversation_id: str | None = None,
        status: str | RevisionStatus | None = None,
    ) -> Path:
        status_was_explicit = status is not None
        directory = self.root / str(document.document_id)
        revisions = directory / "revisions"
        version = revisions / f"{document.revision:06d}.json"
        with self.lock:
            previous_order = _PERSISTENCE_ORDER.get(str(self.root), 0)
            persistence_order = max(time.time_ns(), previous_order + 1)
            _PERSISTENCE_ORDER[str(self.root)] = persistence_order
            _DOCUMENT_PERSISTENCE_ORDER[
                (str(self.root), str(document.document_id))
            ] = persistence_order
        provenance = {
            key: value
            for key, value in {
                "source_message_id": source_message_id,
                "source_run_id": source_run_id,
                "source_conversation_id": source_conversation_id,
            }.items()
            if value
        }
        if provenance:
            document = document.model_copy(
                deep=True,
                update={"metadata": dict(document.metadata) | provenance},
            )
        revisions.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(document.canonical_payload(), ensure_ascii=False, indent=2)
        self._atomic_write(version, canonical)
        self._atomic_write(directory / "current.json", canonical)
        revision_id = self.revision_id(document.document_id, document.revision)
        canonical_artifact_id = self._save_canonical_artifact(document, canonical, revision_id)
        self.last_canonical_artifact_id = canonical_artifact_id
        resolved_status = (
            status.value
            if isinstance(status, RevisionStatus)
            else status or self._inferred_status(document).value
        )
        self._catalog(
            document,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            source_message_id=source_message_id,
            source_run_id=source_run_id,
            source_conversation_id=source_conversation_id,
            canonical_artifact_id=canonical_artifact_id,
            status=resolved_status,
            update_status=status_was_explicit,
        )
        return version

    def load(self, document_id: UUID, revision: int | None = None) -> DocumentIR:
        directory = self.root / str(document_id)
        path = (
            directory / "current.json"
            if revision is None
            else directory / "revisions" / f"{revision:06d}.json"
        )
        if not path.is_file():
            revision_label = revision if revision is not None else "latest"
            raise DocumentRevisionNotFound(
                f"document revision not found: document={document_id}, revision={revision_label}"
            )
        return migrate_document_ir(json.loads(path.read_text(encoding="utf-8")))

    def snapshot(self, document_id: UUID) -> DocumentSnapshot:
        document = self.load(document_id)
        hashes = document.hashes()
        return DocumentSnapshot(
            document=document.model_copy(deep=True),
            revision=document.revision,
            content_hash=hashes.content_hash,
            structure_hash=hashes.structure_hash,
            style_hash=hashes.style_hash,
            asset_set_hash=hashes.asset_set_hash,
            citation_set_hash=hashes.citation_set_hash,
            presentation_hash=hashes.presentation_hash,
            numbering_hash=hashes.numbering_hash,
        )

    @staticmethod
    def revision_id(document_id: UUID, revision: int) -> str:
        return str(uuid5(NAMESPACE_URL, f"paperagent:{document_id}:{revision}"))

    def create(
        self,
        document: DocumentIR,
        *,
        source_message_id: str | None = None,
        source_run_id: str | None = None,
        source_conversation_id: str | None = None,
    ) -> Path:
        if document.revision != 1:
            raise ValueError("a new document must start at revision 1")
        return self.save(
            document,
            source_message_id=source_message_id,
            source_run_id=source_run_id,
            source_conversation_id=source_conversation_id,
        )

    def get(self, document_id: UUID, revision: int | None = None) -> DocumentIR:
        return self.load(document_id, revision)

    def latest(
        self,
        *,
        document_id: UUID | None = None,
        source_conversation_id: str | None = None,
    ) -> DocumentIR:
        if document_id is not None:
            return self.load(document_id)
        candidates: list[tuple[int, DocumentIR]] = []
        if self.root.is_dir():
            for current in self.root.glob("*/current.json"):
                try:
                    document = migrate_document_ir(json.loads(current.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    continue
                source = document.metadata.get("source_conversation_id")
                if source_conversation_id is None or source == source_conversation_id:
                    candidates.append((self._candidate_order(document, current), document))
        if not candidates:
            scope = (
                f"conversation={source_conversation_id}" if source_conversation_id else "project"
            )
            raise DocumentRevisionNotFound(f"no valid document revision found for {scope}")
        return max(candidates, key=lambda item: (item[0], item[1].revision))[1]

    def candidates(self, *, source_conversation_id: str | None = None) -> list[DocumentIR]:
        """Return newest-first canonical documents within the requested scope."""

        candidates: list[tuple[int, DocumentIR]] = []
        if self.root.is_dir():
            for current in self.root.glob("*/current.json"):
                try:
                    document = migrate_document_ir(json.loads(current.read_text(encoding="utf-8")))
                except (OSError, ValueError, TypeError):
                    continue
                source = str(document.metadata.get("source_conversation_id") or "")
                if source_conversation_id is None or source == source_conversation_id:
                    candidates.append((self._candidate_order(document, current), document))
        return [
            item[1]
            for item in sorted(
            candidates,
            key=lambda item: (
                    item[0],
                    item[1].revision,
            ),
            reverse=True,
            )
        ]

    def _candidate_order(self, document: DocumentIR, current: Path) -> int:
        return _persistence_order(self.root, document, current)

    def branch(self, document_id: UUID, revision: int) -> DocumentIR:
        source = self.load(document_id, revision)
        metadata = dict(source.metadata)
        metadata["branched_from"] = {
            "document_id": str(document_id),
            "revision": revision,
            "revision_id": self.revision_id(document_id, revision),
        }
        return source.model_copy(
            deep=True,
            update={"document_id": uuid4(), "revision": 1, "metadata": metadata},
        )

    def restyle(self, document_id: UUID, typography: object) -> DocumentIR:
        from paperagent.schemas.typography import TypographySpec

        source = self.load(document_id)
        spec = (
            typography
            if isinstance(typography, TypographySpec)
            else TypographySpec.model_validate(typography)
        )
        changed = source.restyle(spec)
        self.save(
            changed,
            parent_revision_id=self.revision_id(source.document_id, source.revision),
            source_conversation_id=str(source.metadata.get("source_conversation_id") or "") or None,
        )
        return changed

    def patch_target(
        self,
        document_id: UUID,
        block_id: UUID,
        patch: dict[str, object],
        *,
        source_run_id: str | None = None,
    ) -> DocumentIR:
        source = self.load(document_id)
        changed = source.patch_block(block_id, patch)
        self.save(
            changed,
            parent_revision_id=self.revision_id(source.document_id, source.revision),
            source_run_id=source_run_id,
            source_conversation_id=str(source.metadata.get("source_conversation_id") or "") or None,
        )
        return changed

    def list_lineage(self, document_id: UUID) -> list[DocumentSnapshot]:
        directory = self.root / str(document_id) / "revisions"
        if not directory.is_dir():
            raise DocumentRevisionNotFound(f"document not found: {document_id}")
        snapshots = []
        for path in sorted(directory.glob("*.json")):
            document = migrate_document_ir(json.loads(path.read_text(encoding="utf-8")))
            hashes = document.hashes()
            snapshots.append(
                DocumentSnapshot(
                    document=document,
                    revision=document.revision,
                    content_hash=hashes.content_hash,
                    structure_hash=hashes.structure_hash,
                    style_hash=hashes.style_hash,
                    asset_set_hash=hashes.asset_set_hash,
                    citation_set_hash=hashes.citation_set_hash,
                    presentation_hash=hashes.presentation_hash,
                    numbering_hash=hashes.numbering_hash,
                )
            )
        return snapshots

    def rollback(self, document_id: UUID, revision: int) -> DocumentIR:
        """Restore an old snapshot as a new immutable lineage entry."""

        current = self.load(document_id)
        source = self.load(document_id, revision)
        metadata = dict(source.metadata)
        metadata["rollback_from"] = {
            "revision": current.revision,
            "restored_revision": revision,
            "revision_id": self.revision_id(document_id, revision),
        }
        restored = source.model_copy(
            deep=True,
            update={
                "revision": current.revision + 1,
                "metadata": metadata,
            },
        )
        self.save(
            restored,
            parent_revision_id=self.revision_id(document_id, current.revision),
            source_conversation_id=str(metadata.get("source_conversation_id") or "") or None,
        )
        return restored

    def commit(
        self,
        document: DocumentIR,
        *,
        expected_revision: int,
        input_hash: str,
        run_id: str,
    ) -> Path:
        """Optimistic commit; stale writers receive merge/replan evidence."""
        with self.lock:
            directory = self.root / str(document.document_id)
            current_path = directory / "current.json"
            current = (
                migrate_document_ir(json.loads(current_path.read_text(encoding="utf-8")))
                if current_path.exists()
                else None
            )
            current_revision = current.revision if current is not None else 0
            if current_revision != expected_revision:
                base = None
                base_path = directory / "revisions" / f"{expected_revision:06d}.json"
                if base_path.exists():
                    base = migrate_document_ir(json.loads(base_path.read_text(encoding="utf-8")))
                changed = (
                    diff_documents(base, current).changed_blocks
                    if base is not None and current is not None
                    else []
                )
                raise DocumentRevisionConflict(expected_revision, current_revision, changed)
            if document.revision != expected_revision + 1:
                raise ValueError(
                    "committed document revision must increment expected revision by one"
                )
            metadata = dict(document.metadata)
            metadata["commit_provenance"] = {
                "run_id": run_id,
                "input_hash": input_hash,
                "base_revision": expected_revision,
            }
            committed = document.model_copy(update={"metadata": metadata})
            return self.save(
                committed,
                parent_revision_id=(
                    self.revision_id(document.document_id, expected_revision)
                    if expected_revision
                    else None
                ),
                source_run_id=run_id,
                source_conversation_id=str(committed.metadata.get("source_conversation_id") or "")
                or None,
            )

    def _save_canonical_artifact(
        self,
        document: DocumentIR,
        canonical: str,
        revision_id: str,
    ) -> str | None:
        if self.artifact_service is None:
            return None
        target = (
            self.project_root
            / "artifacts"
            / "document-ir"
            / str(document.document_id)
            / f"{document.revision:06d}.document-ir.json"
        )
        self._atomic_write(target, canonical)
        artifact = self.artifact_service.register(
            target,
            kind="document_ir",
            producer_tool="document.revision.save",
            document_id=str(document.document_id),
            revision_id=revision_id,
        )
        return artifact.id

    def _catalog(
        self,
        document: DocumentIR,
        *,
        revision_id: str,
        parent_revision_id: str | None,
        source_message_id: str | None,
        source_run_id: str | None,
        source_conversation_id: str | None,
        canonical_artifact_id: str | None,
        status: str,
        update_status: bool,
    ) -> None:
        if self.databases is None or self.project_id is None:
            return
        hashes = document.hashes()
        with self.databases.project_session(self.project_id) as session:
            record = session.get(DocumentRecord, str(document.document_id))
            if record is None:
                record = DocumentRecord(
                    id=str(document.document_id),
                    title=document.title,
                    source_conversation_id=source_conversation_id,
                )
                session.add(record)
            revision = session.get(DocumentRevisionRecord, revision_id)
            if revision is None:
                manifest = document.asset_manifest
                revision = DocumentRevisionRecord(
                    id=revision_id,
                    document_id=str(document.document_id),
                    revision_number=document.revision,
                    parent_revision_id=parent_revision_id,
                    source_message_id=source_message_id,
                    source_run_id=source_run_id,
                    source_conversation_id=source_conversation_id,
                    canonical_artifact_id=canonical_artifact_id,
                    schema_version=document.schema_version,
                    asset_manifest_json=(
                        manifest.model_dump_json() if manifest is not None else None
                    ),
                    asset_manifest_hash=(
                        manifest.manifest_hash if manifest is not None else None
                    ),
                    image_required=manifest.image_required if manifest is not None else False,
                    expected_asset_count=(
                        manifest.required_count if manifest is not None else 0
                    ),
                    status=status,
                    **hashes.model_dump(),
                )
                session.add(revision)
                session.flush()
            else:
                if update_status or revision.status in {"valid", "draft"}:
                    revision.status = status
            existing_bindings = {
                (item.artifact_id, item.block_id)
                for item in session.scalars(
                    select(DocumentRevisionAssetRecord).where(
                        DocumentRevisionAssetRecord.revision_id == revision_id
                    )
                )
            }
            for block in document.iter_blocks():
                figure = block.figure
                if figure is None or figure.artifact_id is None:
                    continue
                binding_key = (str(figure.artifact_id), str(block.block_id))
                if binding_key in existing_bindings:
                    continue
                session.add(
                    DocumentRevisionAssetRecord(
                        id=str(uuid4()),
                        revision_id=revision_id,
                        artifact_id=str(figure.artifact_id),
                        role="figure",
                        block_id=str(block.block_id),
                        logical_id=str(block.block_id),
                        binding_evidence="canonical FigureSpec artifact_id",
                        status="ready",
                    )
                )
            record.title = document.title
            record.latest_revision_id = revision_id
            record.updated_at = document.updated_at
            session.commit()

    def status(self, document_id: UUID, revision: int) -> RevisionStatus:
        if self.databases is None or self.project_id is None:
            return self._inferred_status(self.load(document_id, revision))
        revision_id = self.revision_id(document_id, revision)
        with self.databases.project_session(self.project_id) as session:
            record = session.get(DocumentRevisionRecord, revision_id)
            if record is None:
                raise DocumentRevisionNotFound(revision_id)
            if record.status == "valid":
                return self._inferred_status(self.load(document_id, revision))
            return RevisionStatus(record.status)

    def canonical_artifact_id(self, document_id: UUID, revision: int) -> str | None:
        if self.databases is None or self.project_id is None:
            return None
        revision_id = self.revision_id(document_id, revision)
        with self.databases.project_session(self.project_id) as session:
            record = session.get(DocumentRevisionRecord, revision_id)
            if record is None:
                raise DocumentRevisionNotFound(revision_id)
            return record.canonical_artifact_id

    def transition_status(
        self,
        document_id: UUID,
        revision: int,
        target: RevisionStatus,
        *,
        expected: RevisionStatus | None = None,
    ) -> RevisionStatus:
        if self.databases is None or self.project_id is None:
            raise RuntimeError("revision status transitions require the project catalog")
        allowed = {
            RevisionStatus.DRAFT: {
                RevisionStatus.ASSETS_PENDING,
                RevisionStatus.CANONICAL_READY,
                RevisionStatus.REPAIR_REQUIRED,
            },
            RevisionStatus.ASSETS_PENDING: {
                RevisionStatus.CANONICAL_READY,
                RevisionStatus.REPAIR_REQUIRED,
            },
            RevisionStatus.CANONICAL_READY: {
                RevisionStatus.RENDERING,
                RevisionStatus.REPAIR_REQUIRED,
            },
            RevisionStatus.RENDERING: {
                RevisionStatus.CANONICAL_READY,
                RevisionStatus.DELIVERED,
                RevisionStatus.REPAIR_REQUIRED,
            },
            RevisionStatus.REPAIR_REQUIRED: {
                RevisionStatus.CANONICAL_READY,
                RevisionStatus.RENDERING,
                RevisionStatus.REJECTED,
            },
            RevisionStatus.DELIVERED: {RevisionStatus.RENDERING},
            RevisionStatus.REJECTED: set(),
        }
        revision_id = self.revision_id(document_id, revision)
        with self.databases.project_session(self.project_id) as session:
            record = session.get(DocumentRevisionRecord, revision_id)
            if record is None:
                raise DocumentRevisionNotFound(revision_id)
            current = (
                self._inferred_status(self.load(document_id, revision))
                if record.status == "valid"
                else RevisionStatus(record.status)
            )
            if expected is not None and current is not expected:
                raise DocumentRevisionConflict(revision, revision, [])
            if target not in allowed[current]:
                raise ValueError(
                    f"illegal revision transition: {current.value} -> {target.value}"
                )
            record.status = target.value
            session.commit()
        return target

    @staticmethod
    def _inferred_status(document: DocumentIR) -> RevisionStatus:
        manifest = document.asset_manifest
        figures = [block.figure for block in document.iter_blocks() if block.figure is not None]
        if manifest is not None and manifest.image_required and (
            manifest.required_figure_count == 0
            or len(figures) < manifest.required_figure_count
            or any(figure.artifact_id is None for figure in figures)
        ):
            return RevisionStatus.ASSETS_PENDING
        return RevisionStatus.CANONICAL_READY

    def attach_asset(
        self,
        *,
        document_id: UUID,
        revision: int,
        artifact_id: str,
        role: str,
        block_id: UUID | None = None,
        derivative_for: str | None = None,
    ) -> None:
        if self.databases is None or self.project_id is None:
            raise RuntimeError("asset catalog requires a configured project database")
        revision_id = self.revision_id(document_id, revision)
        with self.databases.project_session(self.project_id) as session:
            if session.get(DocumentRevisionRecord, revision_id) is None:
                raise DocumentRevisionNotFound(revision_id)
            session.add(
                DocumentRevisionAssetRecord(
                    id=str(uuid4()),
                    revision_id=revision_id,
                    artifact_id=artifact_id,
                    role=role,
                    block_id=str(block_id) if block_id else None,
                    derivative_for=derivative_for,
                )
            )
            session.commit()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
