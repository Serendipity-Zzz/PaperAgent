from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from paperagent.agents.change_intent import ChangeIntent, ChangeIntentAgent
from paperagent.agents.document_ir import (
    BlockKind,
    DocumentDiff,
    DocumentIR,
    TableSpec,
    diff_documents,
)
from paperagent.presentation import apply_presentation_patch
from paperagent.rendering.artifacts import ArtifactVersionService
from paperagent.rendering.revision_store import (
    DocumentRevisionNotFound,
    DocumentRevisionStore,
)
from paperagent.schemas.presentation import PresentationPatchOperation


class ResolutionScope(StrEnum):
    EXPLICIT = "explicit"
    ARTIFACT = "artifact"
    CONVERSATION = "conversation"
    PROJECT = "project"
    USER_CHOICE = "user_choice"


class RevisionCandidate(BaseModel):
    document_id: UUID
    revision: int
    title: str
    scope: ResolutionScope
    updated_at: str


class RevisionResolution(BaseModel):
    document: DocumentIR | None = None
    scope: ResolutionScope
    confidence: float = Field(ge=0, le=1)
    candidates: list[RevisionCandidate] = Field(default_factory=list)
    requires_confirmation: bool = False
    reason: str


_CHINESE_ORDINALS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _requested_revision(reference: str) -> int | None:
    match = re.search(r"第\s*(\d+)\s*版", reference)
    if match:
        return int(match.group(1))
    match = re.search(r"第\s*([一二两三四五六七八九十])\s*版", reference)
    return _CHINESE_ORDINALS.get(match.group(1)) if match else None


class RevisionResolver:
    """Resolve natural references to canonical DocumentIR, never to rendered text."""

    def __init__(
        self,
        store: DocumentRevisionStore,
        artifact_versions: ArtifactVersionService | None = None,
    ) -> None:
        self.store = store
        self.artifact_versions = artifact_versions

    def resolve(
        self,
        reference: str = "",
        *,
        document_id: UUID | None = None,
        revision: int | None = None,
        conversation_id: str | None = None,
        artifact_id: UUID | None = None,
    ) -> RevisionResolution:
        if artifact_id is not None and self.artifact_versions is not None:
            artifact = self.artifact_versions.get(artifact_id)
            if artifact is not None:
                document = self.store.load(artifact.document_id, artifact.document_revision)
                return RevisionResolution(
                    document=document,
                    scope=ResolutionScope.ARTIFACT,
                    confidence=1,
                    reason="rendered artifact is bound to a canonical revision",
                )
        if artifact_id is not None and self.store.artifact_service is not None:
            catalog_artifact = self.store.artifact_service.get(str(artifact_id), verify=True)
            if catalog_artifact.document_id:
                bound_id = UUID(str(catalog_artifact.document_id))
                for snapshot in self.store.list_lineage(bound_id):
                    if catalog_artifact.revision_id == self.store.revision_id(
                        bound_id, snapshot.revision
                    ):
                        return RevisionResolution(
                            document=snapshot.document,
                            scope=ResolutionScope.ARTIFACT,
                            confidence=1,
                            reason="download/preview artifact is bound to a canonical revision",
                        )
        if document_id is not None:
            requested = revision if revision is not None else _requested_revision(reference)
            if "上一版" in reference and requested is None:
                requested = max(1, self.store.load(document_id).revision - 1)
            document = self.store.load(document_id, requested)
            return RevisionResolution(
                document=document,
                scope=ResolutionScope.EXPLICIT,
                confidence=1,
                reason="explicit document/revision binding",
            )
        if conversation_id:
            scoped = self.store.candidates(source_conversation_id=conversation_id)
            if len(scoped) == 1:
                document = self._select_revision(scoped[0], reference)
                return RevisionResolution(
                    document=document,
                    scope=ResolutionScope.CONVERSATION,
                    confidence=0.94,
                    reason="latest valid revision in the current conversation",
                )
            if len(scoped) > 1:
                return self._ambiguous(scoped, ResolutionScope.CONVERSATION)
        project = self.store.candidates()
        if len(project) == 1:
            document = self._select_revision(project[0], reference)
            return RevisionResolution(
                document=document,
                scope=ResolutionScope.PROJECT,
                confidence=0.82,
                reason="only canonical document in the current project",
            )
        if project:
            # Deictic phrases refer to the chronologically latest output. A bare
            # request with multiple documents is intentionally ambiguous.
            if re.search(r"刚才|最新|这个\s*(?:PDF|DOCX|文档|报告)", reference, re.I):
                return RevisionResolution(
                    document=self._select_revision(project[0], reference),
                    scope=ResolutionScope.PROJECT,
                    confidence=0.84,
                    reason="deictic reference resolved to the latest project revision",
                )
            return self._ambiguous(project, ResolutionScope.PROJECT)
        raise DocumentRevisionNotFound("no canonical document revision is available")

    def _select_revision(self, document: DocumentIR, reference: str) -> DocumentIR:
        requested = _requested_revision(reference)
        if "上一版" in reference and requested is None:
            requested = max(1, document.revision - 1)
        return self.store.load(document.document_id, requested) if requested else document

    @staticmethod
    def _ambiguous(documents: list[DocumentIR], scope: ResolutionScope) -> RevisionResolution:
        return RevisionResolution(
            scope=ResolutionScope.USER_CHOICE,
            confidence=0.35,
            requires_confirmation=True,
            reason="multiple canonical documents match; user choice is required",
            candidates=[
                RevisionCandidate(
                    document_id=item.document_id,
                    revision=item.revision,
                    title=item.title,
                    scope=scope,
                    updated_at=item.updated_at.isoformat(),
                )
                for item in documents
            ],
        )


class TargetKind(StrEnum):
    GLOBAL = "global"
    FRONT_MATTER = "front_matter"
    SECTION = "section"
    BLOCK = "block"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"


class TargetCandidate(BaseModel):
    kind: TargetKind
    target_id: UUID | None = None
    label: str


class TargetResolution(BaseModel):
    targets: list[TargetCandidate] = Field(default_factory=list)
    requires_confirmation: bool = False
    reason: str


class TargetResolver:
    """Map user language or preview anchors to stable DocumentIR IDs."""

    def resolve(
        self,
        document: DocumentIR,
        request: str,
        *,
        section_id: UUID | None = None,
        block_id: UUID | None = None,
    ) -> TargetResolution:
        if block_id is not None:
            block = next(
                (item for item in document.iter_blocks() if item.block_id == block_id), None
            )
            if block is None:
                raise KeyError(block_id)
            return TargetResolution(
                targets=[
                    TargetCandidate(
                        kind=self._block_kind(block.kind),
                        target_id=block_id,
                        label=block.caption or block.text[:40] or block.kind.value,
                    )
                ],
                reason="preview anchor bound to block id",
            )
        if section_id is not None:
            section = next(
                (item for item in document.iter_sections() if item.section_id == section_id), None
            )
            if section is None:
                raise KeyError(section_id)
            return TargetResolution(
                targets=[
                    TargetCandidate(
                        kind=TargetKind.SECTION, target_id=section_id, label=section.title
                    )
                ],
                reason="preview anchor bound to section id",
            )
        lowered = request.casefold()
        for term, fixed_kind in (
            ("页眉", TargetKind.HEADER),
            ("页脚", TargetKind.FOOTER),
            ("封面", TargetKind.FRONT_MATTER),
            ("摘要", TargetKind.FRONT_MATTER),
        ):
            if term in request:
                return TargetResolution(
                    targets=[TargetCandidate(kind=fixed_kind, label=term)],
                    reason=f"matched {term}",
                )
        ordinal = self._ordinal(request)
        section_terms = ("章", "节", "section")
        if ordinal is not None and any(term in lowered for term in section_terms):
            sections = list(document.iter_sections())
            if 1 <= ordinal <= len(sections):
                item = sections[ordinal - 1]
                return TargetResolution(
                    targets=[
                        TargetCandidate(
                            kind=TargetKind.SECTION, target_id=item.section_id, label=item.title
                        )
                    ],
                    reason="matched section ordinal",
                )
        for section in document.iter_sections():
            if section.title and section.title.casefold() in lowered:
                return TargetResolution(
                    targets=[
                        TargetCandidate(
                            kind=TargetKind.SECTION,
                            target_id=section.section_id,
                            label=section.title,
                        )
                    ],
                    reason="matched section title",
                )
        target_kind = (
            TargetKind.FIGURE
            if re.search(r"图|figure", request, re.I)
            else TargetKind.TABLE
            if re.search(r"表|table", request, re.I)
            else None
        )
        if target_kind is not None:
            blocks = [
                item
                for item in document.iter_blocks()
                if self._block_kind(item.kind) is target_kind
            ]
            if ordinal is not None and 1 <= ordinal <= len(blocks):
                blocks = [blocks[ordinal - 1]]
            candidates = [
                TargetCandidate(
                    kind=target_kind,
                    target_id=item.block_id,
                    label=item.caption or item.text[:40] or target_kind.value,
                )
                for item in blocks
            ]
            return TargetResolution(
                targets=candidates if len(candidates) == 1 else [],
                requires_confirmation=len(candidates) != 1,
                reason="matched typed block" if len(candidates) == 1 else "target is not unique",
            )
        if re.search(r"全文|整体|全部|全局", request):
            return TargetResolution(
                targets=[TargetCandidate(kind=TargetKind.GLOBAL, label="全文")],
                reason="matched global scope",
            )
        return TargetResolution(
            targets=[TargetCandidate(kind=TargetKind.GLOBAL, label="全文")],
            reason="no local target was expressed",
        )

    @staticmethod
    def _block_kind(kind: BlockKind) -> TargetKind:
        if kind is BlockKind.FIGURE:
            return TargetKind.FIGURE
        if kind is BlockKind.TABLE:
            return TargetKind.TABLE
        return TargetKind.BLOCK

    @staticmethod
    def _ordinal(request: str) -> int | None:
        match = re.search(r"第\s*(\d+|[一二两三四五六七八九十])", request)
        if not match:
            return None
        value = match.group(1)
        return int(value) if value.isdigit() else _CHINESE_ORDINALS.get(value)


class RevisionOperation(BaseModel):
    kind: Literal[
        "typography",
        "block",
        "section",
        "insert_break",
        "remove_block",
        "presentation",
    ]
    patch: dict[str, object] = Field(default_factory=dict)
    target_ids: list[UUID] = Field(default_factory=list)
    break_kind: BlockKind | None = None
    after_block_id: UUID | None = None
    presentation_operations: list[PresentationPatchOperation] = Field(default_factory=list)


class RevisionResult(BaseModel):
    document: DocumentIR
    diff: DocumentDiff
    parent_revision_id: str


class RevisionWorkflow:
    def __init__(self, store: DocumentRevisionStore) -> None:
        self.store = store

    def apply(self, document: DocumentIR, operation: RevisionOperation) -> RevisionResult:
        before = document
        if operation.kind == "typography":
            intent = ChangeIntent.model_validate(operation.patch)
            changed, _ = ChangeIntentAgent.apply(before, intent)
        elif operation.kind == "block":
            if len(operation.target_ids) != 1:
                raise ValueError("block revision requires exactly one target")
            changed = before.patch_block(operation.target_ids[0], operation.patch)
        elif operation.kind == "section":
            if len(operation.target_ids) != 1:
                raise ValueError("section revision requires exactly one target")
            changed = before.patch_section(operation.target_ids[0], operation.patch)
        elif operation.kind == "insert_break":
            if len(operation.target_ids) != 1 or operation.break_kind is None:
                raise ValueError("break revision requires one section and break_kind")
            changed = before.insert_break(
                operation.target_ids[0],
                kind=operation.break_kind,
                after_block_id=operation.after_block_id,
            )
        elif operation.kind == "presentation":
            if not operation.presentation_operations:
                raise ValueError("presentation revision requires at least one operation")
            changed = apply_presentation_patch(before, operation.presentation_operations)
            assert isinstance(changed, DocumentIR)
            if changed.presentation == before.presentation:
                parent = self.store.revision_id(before.document_id, before.revision)
                return RevisionResult(
                    document=before,
                    diff=diff_documents(before, before),
                    parent_revision_id=parent,
                )
        else:
            if len(operation.target_ids) != 1:
                raise ValueError("remove_block requires exactly one target")
            changed = before.remove_block(operation.target_ids[0])
        if changed.asset_manifest is not None:
            changed.asset_manifest = changed.asset_manifest.model_copy(
                update={
                    "document_id": changed.document_id,
                    "revision": changed.revision,
                }
            )
        parent = self.store.revision_id(before.document_id, before.revision)
        self.store.save(
            changed,
            parent_revision_id=parent,
            source_conversation_id=str(changed.metadata.get("source_conversation_id") or "")
            or None,
        )
        return RevisionResult(
            document=changed, diff=diff_documents(before, changed), parent_revision_id=parent
        )

    def resize_figure(
        self, document: DocumentIR, block_id: UUID, width_ratio: float
    ) -> RevisionResult:
        block = next((item for item in document.iter_blocks() if item.block_id == block_id), None)
        if block is None or block.figure is None:
            raise KeyError(block_id)
        figure = block.figure.model_copy(update={"width_ratio": width_ratio})
        return self.apply(
            document,
            RevisionOperation(
                kind="block",
                target_ids=[block_id],
                patch={"figure": figure.model_dump(mode="json")},
            ),
        )

    def update_table(
        self, document: DocumentIR, block_id: UUID, table: TableSpec
    ) -> RevisionResult:
        return self.apply(
            document,
            RevisionOperation(
                kind="block", target_ids=[block_id], patch={"table": table.model_dump(mode="json")}
            ),
        )
