from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

from pydantic import BaseModel, Field

from paperagent.agents.document_ir import (
    AssetRequirementManifest,
    BlockKind,
    DocumentBlock,
    DocumentIR,
    RequiredAsset,
    RequiredAssetKind,
)
from paperagent.artifacts import ArtifactIntegrityError, ArtifactService
from paperagent.db.models import ArtifactRecord
from paperagent.rendering.delivery import (
    AmbiguousAssetGroup,
    AssetBinding,
    AssetCandidate,
)


class AssetBindingResult(BaseModel):
    document: DocumentIR
    bindings: list[AssetBinding] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    ambiguous: list[AmbiguousAssetGroup] = Field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not (self.missing or self.pending or self.invalid or self.ambiguous)


class AssetBarrierCheckpoint(BaseModel):
    document_id: UUID
    revision: int = Field(ge=1)
    status: str
    pending_logical_ids: list[str] = Field(default_factory=list)
    source_run_id: str | None = None
    source_message_id: str | None = None
    resume_from: str = "document_asset_barrier"
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    timeout_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.timeout_at


class AssetBarrierCheckpointStore:
    """File-first barrier state; safe to inspect and resume after a process restart."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve() / ".paperagent" / "delivery-checkpoints"

    def save_pending(
        self,
        *,
        document_id: UUID,
        revision: int,
        pending_logical_ids: list[str],
        source_run_id: str | None,
        source_message_id: str | None,
        timeout_seconds: int = 900,
    ) -> AssetBarrierCheckpoint:
        checkpoint = AssetBarrierCheckpoint(
            document_id=document_id,
            revision=revision,
            status="pending",
            pending_logical_ids=list(dict.fromkeys(pending_logical_ids)),
            source_run_id=source_run_id,
            source_message_id=source_message_id,
            timeout_at=datetime.now(UTC) + timedelta(seconds=timeout_seconds),
        )
        self._write(checkpoint)
        return checkpoint

    def mark_ready(self, document_id: UUID, revision: int) -> AssetBarrierCheckpoint:
        checkpoint = AssetBarrierCheckpoint(
            document_id=document_id,
            revision=revision,
            status="ready",
            timeout_at=datetime.now(UTC),
        )
        self._write(checkpoint)
        return checkpoint

    def load(self, document_id: UUID, revision: int) -> AssetBarrierCheckpoint | None:
        path = self._path(document_id, revision)
        if not path.is_file():
            return None
        return AssetBarrierCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))

    def _path(self, document_id: UUID, revision: int) -> Path:
        return self.root / f"{document_id}-{revision:06d}.json"

    def _write(self, checkpoint: AssetBarrierCheckpoint) -> None:
        path = self._path(checkpoint.document_id, checkpoint.revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(checkpoint.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)


def _filename(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    candidate = unquote(parsed.path or value).replace("\\", "/")
    name = Path(candidate).name.strip().casefold()
    return name or None


def manifest_from_document(
    document: DocumentIR,
    *,
    image_required: bool | None = None,
    source_run_id: str | None = None,
) -> AssetRequirementManifest:
    required: list[RequiredAsset] = []
    for order, block in enumerate(
        (item for item in document.iter_blocks() if item.kind is BlockKind.FIGURE),
        start=1,
    ):
        figure = block.figure
        required.append(
            RequiredAsset(
                logical_id=str(block.block_id),
                kind=RequiredAssetKind.FIGURE,
                source_run_id=source_run_id,
                expected_filename=figure.path if figure is not None else None,
                expected_mime_types=["image/*"],
                purpose=block.caption or "document figure",
                caption=block.caption,
                order=order,
            )
        )
    return AssetRequirementManifest(
        document_id=document.document_id,
        revision=document.revision,
        image_required=bool(required) if image_required is None else image_required,
        required_assets=required,
    )


class ArtifactBinder:
    """Bind figure references only inside explicit message/run artifact scopes."""

    def __init__(self, artifacts: ArtifactService) -> None:
        self.artifacts = artifacts

    def bind(
        self,
        document: DocumentIR,
        *,
        source_run_id: str | None = None,
        source_message_id: str | None = None,
        explicit_bindings: dict[str, str] | None = None,
        image_required: bool | None = None,
    ) -> AssetBindingResult:
        resolved = document.model_copy(deep=True)
        manifest = resolved.asset_manifest or manifest_from_document(
            resolved,
            image_required=image_required,
            source_run_id=source_run_id,
        )
        resolved.asset_manifest = manifest
        candidates = self._candidates(source_run_id, source_message_id)
        by_name: dict[str, list[ArtifactRecord]] = {}
        for artifact in candidates:
            name = _filename(artifact.original_name)
            if name:
                by_name.setdefault(name, []).append(artifact)

        manifest_by_id = {item.logical_id: item for item in manifest.required_assets}
        bindings: list[AssetBinding] = []
        missing: list[str] = []
        pending: list[str] = []
        invalid: list[str] = []
        ambiguous: list[AmbiguousAssetGroup] = []
        figures = [
            block
            for block in resolved.iter_blocks()
            if block.kind is BlockKind.FIGURE and block.figure is not None
        ]
        for block in figures:
            assert block.figure is not None
            logical_id = str(block.block_id)
            requirement = manifest_by_id.get(logical_id)
            explicit_id = (explicit_bindings or {}).get(logical_id)
            artifact_id = explicit_id or (
                str(block.figure.artifact_id) if block.figure.artifact_id is not None else None
            )
            if artifact_id is not None:
                state = self._bind_verified(block, artifact_id)
                if state == "ready":
                    bindings.append(
                        AssetBinding(
                            logical_id=logical_id,
                            artifact_id=artifact_id,
                            evidence="explicit artifact id",
                        )
                    )
                elif state == "pending":
                    pending.append(logical_id)
                else:
                    invalid.append(logical_id)
                continue

            expected = _filename(
                block.figure.path
                or (requirement.expected_filename if requirement is not None else None)
            )
            matches = list(dict.fromkeys(item.id for item in by_name.get(expected or "", [])))
            if len(matches) == 1:
                artifact_id = matches[0]
                state = self._bind_verified(block, artifact_id)
                if state == "ready":
                    bindings.append(
                        AssetBinding(
                            logical_id=logical_id,
                            artifact_id=artifact_id,
                            evidence=f"unique source-scope filename: {expected}",
                        )
                    )
                elif state == "pending":
                    pending.append(logical_id)
                else:
                    invalid.append(logical_id)
            elif len(matches) > 1:
                ambiguous.append(
                    AmbiguousAssetGroup(
                        logical_id=logical_id,
                        candidates=[
                            AssetCandidate(
                                artifact_id=item.id,
                                filename=item.original_name,
                                source_run_id=item.run_id,
                                sha256=item.sha256,
                            )
                            for item in candidates
                            if item.id in matches
                        ],
                    )
                )
            else:
                missing.append(logical_id)

        if manifest.image_required and not figures:
            missing.append("required-figure")
        return AssetBindingResult(
            document=resolved,
            bindings=bindings,
            missing=list(dict.fromkeys(missing)),
            pending=list(dict.fromkeys(pending)),
            invalid=list(dict.fromkeys(invalid)),
            ambiguous=ambiguous,
        )

    def _candidates(
        self,
        source_run_id: str | None,
        source_message_id: str | None,
    ) -> list[ArtifactRecord]:
        records: dict[str, ArtifactRecord] = {}
        if source_run_id:
            for artifact in self.artifacts.for_run(source_run_id):
                records[artifact.id] = artifact
        if source_message_id:
            for payload in self.artifacts.links_for_message(source_message_id):
                artifact_id = payload.get("id")
                if isinstance(artifact_id, str):
                    records[artifact_id] = self.artifacts.get(artifact_id, verify=False)
        return [
            item
            for item in records.values()
            if item.kind == "figure" or item.mime_type.startswith("image/")
        ]

    def _bind_verified(self, block: DocumentBlock, artifact_id: str) -> str:
        if block.figure is None:
            return "invalid"
        try:
            artifact = self.artifacts.get(artifact_id, verify=False)
            if artifact.validation_status == "pending":
                return "pending"
            path = self.artifacts.verify(artifact)
        except (KeyError, ArtifactIntegrityError, FileNotFoundError, PermissionError):
            return "invalid"
        if artifact.kind != "figure" and not artifact.mime_type.startswith("image/"):
            return "invalid"
        block.figure.artifact_id = UUID(artifact.id)
        block.figure.path = str(path)
        block.figure.sha256 = artifact.sha256
        block.figure.media_type = artifact.mime_type
        return "ready"
