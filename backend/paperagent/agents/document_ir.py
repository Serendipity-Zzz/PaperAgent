from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, Field, model_validator

from paperagent.schemas.numbering import (
    LabelMap,
    NumberingContract,
    NumberingNormalizer,
    NumberingOwner,
    default_numbering_contract,
)
from paperagent.schemas.presentation import (
    CoverField,
    CoverSpec,
    DocumentPresentationSpec,
    PresentationFieldProvenance,
    PresentationSource,
    normalize_cover_key,
)
from paperagent.schemas.typography import TypographySpec

CURRENT_DOCUMENT_IR_SCHEMA = "2.2"


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    LIST = "list"
    QUOTE = "quote"
    CODE = "code"
    EQUATION = "equation"
    TABLE = "table"
    FIGURE = "figure"
    CITATION = "citation"
    CALLOUT = "callout"
    PAGE_BREAK = "page_break"
    SECTION_BREAK = "section_break"


class InlineKind(StrEnum):
    TEXT = "text"
    STRONG = "strong"
    EMPHASIS = "emphasis"
    CODE = "code"
    LINK = "link"
    CITATION = "citation"
    FOOTNOTE = "footnote"
    CROSS_REFERENCE = "cross_reference"


class ListKind(StrEnum):
    BULLET = "bullet"
    ORDERED = "ordered"


class InlineNode(BaseModel):
    inline_id: UUID = Field(default_factory=uuid4)
    kind: InlineKind
    text: str = ""
    href: str | None = None
    target_id: UUID | None = None
    citation_id: UUID | None = None
    children: list[InlineNode] = Field(default_factory=list)


class TableCell(BaseModel):
    text: str = ""
    inlines: list[InlineNode] = Field(default_factory=list)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    header: bool = False


class TableRow(BaseModel):
    cells: list[TableCell] = Field(min_length=1)


class TableSpec(BaseModel):
    rows: list[TableRow] = Field(min_length=1)
    repeat_header: bool = True
    style_name: str = "Table"
    column_widths: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_column_widths(self) -> TableSpec:
        if any(value <= 0 for value in self.column_widths):
            raise ValueError("table column widths must be positive")
        columns = max(len(row.cells) for row in self.rows)
        if self.column_widths and len(self.column_widths) != columns:
            raise ValueError("table column widths must match the table column count")
        return self


class FigureSpec(BaseModel):
    artifact_id: UUID | None = None
    path: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    media_type: str | None = None
    alt_text: str = ""
    width_ratio: float | None = Field(default=None, gt=0, le=1)
    derivative_artifact_ids: dict[str, UUID] = Field(default_factory=dict)


class RequiredAssetKind(StrEnum):
    FIGURE = "figure"
    TABLE = "table"
    EQUATION = "equation"
    ATTACHMENT = "attachment"


class RequiredAsset(BaseModel):
    logical_id: str = Field(min_length=1)
    kind: RequiredAssetKind
    required: bool = True
    source_node_id: str | None = None
    source_run_id: str | None = None
    expected_filename: str | None = None
    expected_mime_types: list[str] = Field(default_factory=list)
    purpose: str = ""
    caption: str | None = None
    order: int = Field(default=0, ge=0)


class AssetRequirementManifest(BaseModel):
    document_id: UUID
    revision: int = Field(ge=1)
    image_required: bool = False
    required_assets: list[RequiredAsset] = Field(default_factory=list)

    @property
    def required_count(self) -> int:
        return sum(1 for item in self.required_assets if item.required)

    @property
    def required_figure_count(self) -> int:
        return sum(
            1
            for item in self.required_assets
            if item.required and item.kind is RequiredAssetKind.FIGURE
        )

    @property
    def manifest_hash(self) -> str:
        return _stable_hash(self.model_dump(mode="json"))


class EquationSpec(BaseModel):
    latex: str
    mathml: str | None = None
    display: bool = True
    number: bool = False
    label: str | None = None


class ListItem(BaseModel):
    item_id: UUID = Field(default_factory=uuid4)
    inlines: list[InlineNode] = Field(default_factory=list)
    text: str = ""
    children: list[ListItem] = Field(default_factory=list)


class ListSpec(BaseModel):
    kind: ListKind = ListKind.BULLET
    start: int = Field(default=1, ge=1)
    items: list[ListItem] = Field(min_length=1)


class FrontMatter(BaseModel):
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    organization: str | None = None
    date: str | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    custom: dict[str, object] = Field(default_factory=dict)


class BlockReviewStatus(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class TypographyOverrideScope(StrEnum):
    SECTION = "section"
    BLOCK = "block"


class TypographyOverride(BaseModel):
    """A sparse, durable style override anchored to a stable Document IR id."""

    scope: TypographyOverrideScope
    target_id: UUID
    typography: TypographySpec


class Provenance(BaseModel):
    agent: str
    evidence_ids: list[UUID] = Field(default_factory=list)
    source_anchors: list[dict[str, object]] = Field(default_factory=list)
    author_viewpoint: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CitationRef(BaseModel):
    citation_id: UUID = Field(default_factory=uuid4)
    evidence_id: UUID
    locator: str | None = None
    verified: bool = False


class DocumentBlock(BaseModel):
    block_id: UUID = Field(default_factory=uuid4)
    kind: BlockKind
    text: str = ""
    inlines: list[InlineNode] = Field(default_factory=list)
    caption: str | None = None
    style_name: str | None = None
    list_spec: ListSpec | None = None
    table: TableSpec | None = None
    figure: FigureSpec | None = None
    equation: EquationSpec | None = None
    data: dict[str, object] = Field(default_factory=dict)
    citations: list[CitationRef] = Field(default_factory=list)
    provenance: Provenance
    review_status: BlockReviewStatus = BlockReviewStatus.DRAFT

    @model_validator(mode="after")
    def normalize_typed_payloads(self) -> DocumentBlock:
        """Read legacy data while making typed fields the canonical v2 interface."""

        if self.kind is BlockKind.TABLE and self.table is None:
            rows = self.data.get("rows")
            if isinstance(rows, list) and rows and all(isinstance(row, list) for row in rows):
                self.table = TableSpec(
                    rows=[
                        TableRow(
                            cells=[
                                TableCell(text=str(value), header=row_index == 0) for value in row
                            ]
                        )
                        for row_index, row in enumerate(rows)
                    ]
                )
        if self.kind is BlockKind.FIGURE and self.figure is None:
            path = self.data.get("path")
            artifact_id = self.data.get("artifact_id")
            self.figure = FigureSpec(
                path=str(path) if path else None,
                artifact_id=UUID(str(artifact_id)) if artifact_id else None,
                alt_text=self.caption or "",
            )
        if self.kind is BlockKind.EQUATION and self.equation is None and self.text:
            self.equation = EquationSpec(latex=self.text)
        if self.kind is BlockKind.LIST and self.list_spec is None and self.text.strip():
            self.list_spec = ListSpec(
                items=[ListItem(text=line) for line in self.text.splitlines() if line.strip()]
            )
        return self


class DocumentSection(BaseModel):
    section_id: UUID = Field(default_factory=uuid4)
    outline_section_id: UUID | None = None
    title: str
    goal: str
    blocks: list[DocumentBlock] = Field(default_factory=list)
    children: list[DocumentSection] = Field(default_factory=list)
    level: int = Field(default=1, ge=1, le=6)
    style_name: str | None = None
    review_status: BlockReviewStatus = BlockReviewStatus.DRAFT


class DocumentHashes(BaseModel):
    content_hash: str
    structure_hash: str
    style_hash: str
    asset_set_hash: str
    citation_set_hash: str
    presentation_hash: str
    numbering_hash: str


class DocumentIR(BaseModel):
    schema_version: str = CURRENT_DOCUMENT_IR_SCHEMA
    document_id: UUID = Field(default_factory=uuid4)
    requirement_id: UUID
    requirement_version: int = Field(ge=1)
    outline_id: UUID
    title: str
    language: str = Field(pattern=r"^(zh|en|mixed)$")
    front_matter: FrontMatter = Field(default_factory=FrontMatter)
    presentation: DocumentPresentationSpec = Field(default_factory=DocumentPresentationSpec)
    numbering: NumberingContract = Field(default_factory=default_numbering_contract)
    typography: TypographySpec = Field(default_factory=TypographySpec)
    typography_overrides: list[TypographyOverride] = Field(default_factory=list)
    asset_manifest: AssetRequirementManifest | None = None
    sections: list[DocumentSection] = Field(min_length=1)
    back_matter: list[DocumentBlock] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def unique_ids_and_valid_citations(self) -> DocumentIR:
        all_sections = list(self.iter_sections())
        all_blocks = list(self.iter_blocks())
        section_ids = [section.section_id for section in all_sections]
        block_ids = [block.block_id for block in all_blocks]
        citation_ids = [
            citation.citation_id for block in all_blocks for citation in block.citations
        ]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("duplicate section id")
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("duplicate block id")
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("duplicate citation id")
        targets = [(item.scope, item.target_id) for item in self.typography_overrides]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate typography override target")
        valid_sections = set(section_ids)
        valid_blocks = set(block_ids)
        for item in self.typography_overrides:
            valid = (
                valid_sections if item.scope is TypographyOverrideScope.SECTION else valid_blocks
            )
            if item.target_id not in valid:
                raise ValueError(f"typography override references unknown {item.scope.value}")
        cover_keys = {item.semantic_key for item in self.presentation.cover.fields}
        if len(cover_keys) != len(self.presentation.cover.fields):
            raise ValueError("presentation cover fields must have unique semantic keys")
        self._normalize_structural_labels()
        return self

    def _normalize_structural_labels(self) -> None:
        """Keep structural labels semantic while retaining reversible provenance."""

        normalizer = NumberingNormalizer()
        preserve_headings = self.numbering.headings.owner is NumberingOwner.AUTHOR
        preserve_lists = self.numbering.lists.owner is NumberingOwner.AUTHOR
        existing = {
            (item.node_id, item.node_kind, item.original, item.semantic): item
            for item in self.numbering.label_map
        }

        def remember(result: LabelMap) -> None:
            if not result.prefixes and not result.protected_reason:
                return
            existing[(result.node_id, result.node_kind, result.original, result.semantic)] = result

        def normalize_list(items: list[ListItem]) -> None:
            if preserve_lists:
                return
            for item in items:
                result = normalizer.normalize(
                    item.text,
                    node_kind="list_label",
                    node_id=str(item.item_id),
                )
                item.text = result.semantic
                remember(LabelMap.model_validate(result.model_dump(exclude={"changed"})))
                normalize_list(item.children)

        for section in self.iter_sections():
            original_title = section.title
            if not preserve_headings:
                result = normalizer.normalize(
                    original_title,
                    node_kind="heading",
                    node_id=str(section.section_id),
                )
                section.title = result.semantic
                if section.goal.strip() == original_title.strip():
                    section.goal = result.semantic
                remember(LabelMap.model_validate(result.model_dump(exclude={"changed"})))
            for block in section.blocks:
                if block.kind is BlockKind.HEADING and not preserve_headings:
                    block_result = normalizer.normalize(
                        block.text,
                        node_kind="heading",
                        node_id=str(block.block_id),
                    )
                    block.text = block_result.semantic
                    remember(LabelMap.model_validate(block_result.model_dump(exclude={"changed"})))
                if block.list_spec is not None:
                    normalize_list(block.list_spec.items)
                caption_owner = {
                    BlockKind.FIGURE: self.numbering.figures.owner,
                    BlockKind.TABLE: self.numbering.tables.owner,
                    BlockKind.EQUATION: self.numbering.equations.owner,
                }.get(block.kind)
                if (
                    block.caption
                    and caption_owner is not None
                    and caption_owner is not NumberingOwner.AUTHOR
                ):
                    caption_result = normalizer.normalize(
                        block.caption,
                        node_kind="caption_label",
                        node_id=str(block.block_id),
                    )
                    block.caption = caption_result.semantic
                    remember(
                        LabelMap.model_validate(caption_result.model_dump(exclude={"changed"}))
                    )
        for block in self.back_matter:
            if block.kind is BlockKind.HEADING and not preserve_headings:
                block_result = normalizer.normalize(
                    block.text,
                    node_kind="heading",
                    node_id=str(block.block_id),
                )
                block.text = block_result.semantic
                remember(LabelMap.model_validate(block_result.model_dump(exclude={"changed"})))
            if block.list_spec is not None:
                normalize_list(block.list_spec.items)
        label_map = sorted(
            existing.values(),
            key=lambda item: (item.node_kind, item.node_id or "", item.original),
        )
        label_payload = [item.model_dump(mode="json") for item in label_map]
        self.numbering = self.numbering.model_copy(
            update={
                "label_map": label_map,
                "normalized_label_hash": _stable_hash(label_payload),
            }
        )

    def iter_sections(self) -> Iterator[DocumentSection]:
        def walk(items: list[DocumentSection]) -> Iterator[DocumentSection]:
            for item in items:
                yield item
                yield from walk(item.children)

        return walk(self.sections)

    def iter_blocks(self) -> Iterator[DocumentBlock]:
        def walk() -> Iterator[DocumentBlock]:
            for section in self.iter_sections():
                yield from section.blocks
            yield from self.back_matter

        return walk()

    def hashes(self) -> DocumentHashes:
        """Return independent hashes for semantic, structural, style and resource changes."""

        sections = list(self.iter_sections())
        blocks = list(self.iter_blocks())
        content = {
            "title": self.title,
            "front_matter": {
                "abstract": self.front_matter.abstract,
                "keywords": self.front_matter.keywords,
            },
            "sections": [
                {
                    "title": section.title,
                    "goal": section.goal,
                    "blocks": [
                        {
                            "kind": block.kind.value,
                            "text": block.text,
                            "inlines": block.inlines,
                            "caption": block.caption,
                            "list": block.list_spec,
                            "table": (
                                {
                                    "rows": block.table.rows,
                                    "repeat_header": block.table.repeat_header,
                                }
                                if block.table
                                else None
                            ),
                            "equation": block.equation,
                        }
                        for block in section.blocks
                    ],
                }
                for section in sections
            ],
            "back_matter": [
                {
                    "kind": block.kind.value,
                    "text": block.text,
                    "inlines": block.inlines,
                    "caption": block.caption,
                }
                for block in self.back_matter
            ],
        }
        structure = {
            "sections": [
                {
                    "id": str(section.section_id),
                    "level": section.level,
                    "block_ids": [str(block.block_id) for block in section.blocks],
                    "block_kinds": [block.kind.value for block in section.blocks],
                    "children": [str(child.section_id) for child in section.children],
                }
                for section in sections
            ],
            "back_matter": [str(block.block_id) for block in self.back_matter],
        }
        style = {
            "typography": self.typography,
            "overrides": self.typography_overrides,
            "theme_id": self.metadata.get("theme_id"),
            "template_contract_hash": self.metadata.get("template_contract_hash"),
            "section_styles": {str(section.section_id): section.style_name for section in sections},
            "block_styles": {str(block.block_id): block.style_name for block in blocks},
            "figure_layout": {
                str(block.block_id): block.figure.width_ratio
                for block in blocks
                if block.kind is BlockKind.FIGURE and block.figure is not None
            },
            "table_layout": {
                str(block.block_id): {
                    "style_name": block.table.style_name,
                    "column_widths": block.table.column_widths,
                }
                for block in blocks
                if block.kind is BlockKind.TABLE and block.table is not None
            },
        }
        assets = sorted(
            [
                {
                    "artifact_id": str(block.figure.artifact_id)
                    if block.figure and block.figure.artifact_id
                    else None,
                    "sha256": block.figure.sha256 if block.figure else None,
                    "derivatives": {
                        key: str(value)
                        for key, value in sorted(
                            (block.figure.derivative_artifact_ids if block.figure else {}).items()
                        )
                    },
                }
                for block in blocks
                if block.kind is BlockKind.FIGURE
            ],
            key=lambda item: json.dumps(item, sort_keys=True),
        )
        citations = sorted(
            [
                {
                    "citation_id": str(citation.citation_id),
                    "evidence_id": str(citation.evidence_id),
                    "locator": citation.locator,
                    "verified": citation.verified,
                }
                for block in blocks
                for citation in block.citations
            ],
            key=lambda item: str(item["citation_id"]),
        )
        return DocumentHashes(
            content_hash=_stable_hash(content),
            structure_hash=_stable_hash(structure),
            style_hash=_stable_hash(style),
            asset_set_hash=_stable_hash(assets),
            citation_set_hash=_stable_hash(citations),
            presentation_hash=_stable_hash(self.presentation.model_dump(mode="json")),
            numbering_hash=self.numbering.contract_hash,
        )

    def canonical_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload["body"] = payload.pop("sections")
        payload["hashes"] = self.hashes().model_dump(mode="json")
        return payload

    def patch_block(self, block_id: UUID, patch: dict[str, object]) -> DocumentIR:
        allowed = {
            "text",
            "inlines",
            "caption",
            "list_spec",
            "table",
            "figure",
            "equation",
            "data",
            "citations",
            "provenance",
            "review_status",
            "style_name",
        }
        if not set(patch) <= allowed:
            raise ValueError("block patch contains immutable or unknown fields")
        payload = self.model_dump(mode="json")
        found = False
        for section in _payload_sections(payload["sections"]):
            blocks = section.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block["block_id"] == str(block_id):
                    block.update(patch)
                    found = True
        if not found:
            raise KeyError(block_id)
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)

    def patch_section(self, section_id: UUID, patch: dict[str, object]) -> DocumentIR:
        """Patch a stable section anchor without regenerating sibling content."""

        allowed = {"title", "goal", "level", "style_name", "review_status"}
        if not set(patch) <= allowed:
            raise ValueError("section patch contains immutable or unknown fields")
        payload = self.model_dump(mode="json")
        found = False
        for section in _payload_sections(payload["sections"]):
            if section.get("section_id") == str(section_id):
                section.update(patch)
                found = True
                break
        if not found:
            raise KeyError(section_id)
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)

    def insert_break(
        self,
        section_id: UUID,
        *,
        kind: BlockKind,
        after_block_id: UUID | None = None,
    ) -> DocumentIR:
        """Insert a page or section break at a stable location."""

        if kind not in {BlockKind.PAGE_BREAK, BlockKind.SECTION_BREAK}:
            raise ValueError("insert_break accepts page_break or section_break only")
        payload = self.model_dump(mode="json")
        found = False
        new_block = DocumentBlock(
            kind=kind,
            provenance=Provenance(agent="revision_workflow"),
        ).model_dump(mode="json")
        for section in _payload_sections(payload["sections"]):
            if section.get("section_id") != str(section_id):
                continue
            raw_blocks = section.get("blocks", [])
            if not isinstance(raw_blocks, list) or not all(
                isinstance(item, dict) for item in raw_blocks
            ):
                raise ValueError("section blocks must be a list of objects")
            blocks: list[dict[str, object]] = list(raw_blocks)
            if after_block_id is None:
                blocks.append(new_block)
            else:
                for index, block in enumerate(blocks):
                    if block.get("block_id") == str(after_block_id):
                        blocks.insert(index + 1, new_block)
                        break
                else:
                    raise KeyError(after_block_id)
            section["blocks"] = blocks
            found = True
            break
        if not found:
            raise KeyError(section_id)
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)

    def remove_block(self, block_id: UUID) -> DocumentIR:
        payload = self.model_dump(mode="json")
        found = False
        for section in _payload_sections(payload["sections"]):
            raw_blocks = section.get("blocks", [])
            if not isinstance(raw_blocks, list) or not all(
                isinstance(item, dict) for item in raw_blocks
            ):
                raise ValueError("section blocks must be a list of objects")
            blocks: list[dict[str, object]] = list(raw_blocks)
            retained = [item for item in blocks if item.get("block_id") != str(block_id)]
            if len(retained) != len(blocks):
                section["blocks"] = retained
                found = True
                break
        if not found:
            raise KeyError(block_id)
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)

    def restyle(self, typography: TypographySpec) -> DocumentIR:
        """Create a new IR revision without regenerating or mutating body content."""

        return self.model_copy(
            deep=True,
            update={
                "typography": typography,
                "revision": self.revision + 1,
                "updated_at": datetime.now(UTC),
            },
        )

    def renumber(self, numbering: NumberingContract) -> DocumentIR:
        """Create a numbering-only revision from the same canonical content."""

        payload = self.model_dump(mode="json")
        payload["numbering"] = numbering.model_dump(mode="json")
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)

    def resolve_typography(
        self, *, section_id: UUID | None = None, block_id: UUID | None = None
    ) -> TypographySpec:
        """Resolve global -> section -> block typography using stable IR anchors."""

        values = self.typography.model_dump(exclude_none=True)
        for scope, target in (
            (TypographyOverrideScope.SECTION, section_id),
            (TypographyOverrideScope.BLOCK, block_id),
        ):
            if target is None:
                continue
            override = next(
                (
                    item
                    for item in self.typography_overrides
                    if item.scope is scope and item.target_id == target
                ),
                None,
            )
            if override is not None:
                values.update(override.typography.model_dump(exclude_none=True))
        return TypographySpec.model_validate(values)

    def restyle_targets(
        self,
        *,
        scope: TypographyOverrideScope,
        target_ids: list[UUID],
        patch: dict[str, object],
    ) -> DocumentIR:
        """Patch local styles without mutating any section or block content."""

        if not target_ids:
            raise ValueError("local typography patch requires at least one target")
        TypographySpec.model_validate(patch)
        payload = self.model_dump(mode="json")
        overrides = {(item.scope, item.target_id): item for item in self.typography_overrides}
        for target_id in target_ids:
            current = overrides.get((scope, target_id))
            values = current.typography.model_dump(exclude_none=True) if current else {}
            values.update(patch)
            overrides[(scope, target_id)] = TypographyOverride(
                scope=scope,
                target_id=target_id,
                typography=TypographySpec.model_validate(values),
            )
        payload["typography_overrides"] = [
            item.model_dump(mode="json")
            for item in sorted(
                overrides.values(), key=lambda item: (item.scope.value, str(item.target_id))
            )
        ]
        payload["revision"] = self.revision + 1
        payload["updated_at"] = datetime.now(UTC)
        return DocumentIR.model_validate(payload)


class DocumentDiff(BaseModel):
    added_blocks: list[UUID]
    removed_blocks: list[UUID]
    changed_blocks: list[UUID]
    content_changed: bool = False
    structure_changed: bool = False
    style_changed: bool = False
    assets_changed: bool = False
    citations_changed: bool = False
    presentation_changed: bool = False
    numbering_changed: bool = False


def diff_documents(before: DocumentIR, after: DocumentIR) -> DocumentDiff:
    def blocks(document: DocumentIR) -> dict[UUID, DocumentBlock]:
        return {block.block_id: block for block in document.iter_blocks()}

    old, new = blocks(before), blocks(after)
    before_hashes = before.hashes()
    after_hashes = after.hashes()
    return DocumentDiff(
        added_blocks=sorted(set(new) - set(old), key=str),
        removed_blocks=sorted(set(old) - set(new), key=str),
        changed_blocks=sorted(
            [
                block_id
                for block_id in set(old) & set(new)
                if old[block_id] != new[block_id]
                or _resolved_block_typography(before, block_id)
                != _resolved_block_typography(after, block_id)
            ],
            key=str,
        ),
        content_changed=before_hashes.content_hash != after_hashes.content_hash,
        structure_changed=before_hashes.structure_hash != after_hashes.structure_hash,
        style_changed=before_hashes.style_hash != after_hashes.style_hash,
        assets_changed=before_hashes.asset_set_hash != after_hashes.asset_set_hash,
        citations_changed=before_hashes.citation_set_hash != after_hashes.citation_set_hash,
        presentation_changed=(before_hashes.presentation_hash != after_hashes.presentation_hash),
        numbering_changed=(before_hashes.numbering_hash != after_hashes.numbering_hash),
    )


def _resolved_block_typography(document: DocumentIR, block_id: UUID) -> TypographySpec:
    for section in document.iter_sections():
        if any(block.block_id == block_id for block in section.blocks):
            return document.resolve_typography(section_id=section.section_id, block_id=block_id)
    if any(block.block_id == block_id for block in document.back_matter):
        return document.resolve_typography(block_id=block_id)
    raise KeyError(block_id)


def migrate_document_ir(payload: dict[str, object]) -> DocumentIR:
    normalized = deepcopy(payload)
    if "sections" not in normalized and "body" in normalized:
        normalized["sections"] = normalized.pop("body")
    normalized.pop("hashes", None)
    version = str(normalized.get("schema_version", "0.1"))
    if version == CURRENT_DOCUMENT_IR_SCHEMA:
        return DocumentIR.model_validate(normalized)
    if version in {"1.0", "1.1", "2.0", "2.1"}:
        migrated = normalized
        migrated["schema_version"] = CURRENT_DOCUMENT_IR_SCHEMA
        migrated.setdefault("typography_overrides", [])
        migrated.setdefault("front_matter", {})
        migrated.setdefault("back_matter", [])
        migrated.setdefault("numbering", default_numbering_contract().model_dump(mode="json"))
        _upgrade_presentation(migrated)
        _upgrade_markdown_blobs(migrated)
        return DocumentIR.model_validate(migrated)
    if version != "0.1":
        raise ValueError(f"unsupported Document IR schema: {version}")
    migrated = normalized
    migrated["schema_version"] = CURRENT_DOCUMENT_IR_SCHEMA
    sections = []
    legacy_sections = migrated.get("sections", [])
    if not isinstance(legacy_sections, list):
        raise ValueError("legacy sections must be a list")
    for legacy in legacy_sections:
        if not isinstance(legacy, dict):
            raise ValueError("legacy section must be structured")
        content = str(legacy.pop("content", ""))
        legacy.setdefault("goal", legacy.get("title", ""))
        legacy["blocks"] = [
            DocumentBlock(
                kind=BlockKind.PARAGRAPH,
                text=content,
                provenance=Provenance(agent="migration"),
            ).model_dump(mode="json")
        ]
        sections.append(legacy)
    migrated["sections"] = sections
    migrated.setdefault("front_matter", {})
    migrated.setdefault("back_matter", [])
    migrated.setdefault("typography_overrides", [])
    migrated.setdefault("numbering", default_numbering_contract().model_dump(mode="json"))
    _upgrade_presentation(migrated)
    _upgrade_markdown_blobs(migrated)
    return DocumentIR.model_validate(migrated)


def _upgrade_presentation(payload: dict[str, object]) -> None:
    if isinstance(payload.get("presentation"), dict):
        return
    document_id = UUID(str(payload.setdefault("document_id", str(uuid4()))))
    front = payload.get("front_matter")
    front_data = front if isinstance(front, dict) else {}
    fields: list[CoverField] = []

    def add(key: str, label: str, value: object, order: int) -> None:
        text = str(value).strip() if value is not None else ""
        if not text or any(item.semantic_key == key for item in fields):
            return
        fields.append(
            CoverField(
                field_id=uuid5(NAMESPACE_URL, f"paperagent:{document_id}:cover:{key}"),
                semantic_key=key,
                label=label,
                value=text,
                order=order,
                provenance=PresentationFieldProvenance(
                    source=PresentationSource.DEFAULT,
                    source_ref="document-ir-migration",
                ),
            )
        )

    authors = front_data.get("authors")
    if isinstance(authors, list):
        add("author", "作者", "、".join(str(item) for item in authors if str(item).strip()), 10)
    add("institution", "单位", front_data.get("organization"), 20)
    add("date", "日期", front_data.get("date"), 30)
    custom = front_data.get("custom")
    if isinstance(custom, dict):
        for index, (label, value) in enumerate(custom.items(), start=4):
            add(normalize_cover_key(str(label)), str(label), value, index * 10)
    metadata = payload.get("metadata")
    metadata_data = metadata if isinstance(metadata, dict) else {}
    from paperagent.presentation.canonical import default_page_chrome

    page_chrome = default_page_chrome(
        header_text=str(metadata_data.get("header_text") or "") or None,
        footer_text=str(metadata_data.get("footer_text") or "") or None,
    )
    payload["presentation"] = DocumentPresentationSpec(
        cover=CoverSpec(
            enabled=True,
            subtitle=str(front_data.get("subtitle") or "") or None,
            fields=fields,
        ),
        page_chrome=page_chrome,
    ).model_dump(mode="json")


def _upgrade_markdown_blobs(payload: dict[str, object]) -> None:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        return
    from paperagent.rendering.markdown_parser import parse_markdown_blocks

    def walk(items: list[object]) -> None:
        for section in items:
            if not isinstance(section, dict):
                continue
            blocks = section.get("blocks")
            if isinstance(blocks, list) and len(blocks) == 1 and isinstance(blocks[0], dict):
                block = blocks[0]
                text = str(block.get("text", ""))
                if block.get("kind") == BlockKind.PARAGRAPH.value and _looks_like_markdown(text):
                    parsed = parse_markdown_blocks(text, agent="document-ir-migration")
                    if parsed:
                        section["blocks"] = [item.model_dump(mode="json") for item in parsed]
                    else:
                        block["review_status"] = BlockReviewStatus.NEEDS_REVIEW.value
            children = section.get("children")
            if isinstance(children, list):
                walk(children)

    walk(sections)


def _looks_like_markdown(value: str) -> bool:
    return any(
        marker in value for marker in ("\n# ", "\n## ", "\n- ", "\n1. ", "```", "\n| ", "![")
    ) or value.startswith(("# ", "## ", "- ", "1. "))


def _payload_sections(value: object) -> Iterator[dict[str, object]]:
    if not isinstance(value, list):
        return
    for item in value:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _payload_sections(item.get("children"))


def _stable_hash(value: object) -> str:
    def fallback(item: object) -> object:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        if isinstance(item, UUID):
            return str(item)
        raise TypeError(f"unsupported canonical value: {type(item)!r}")

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=fallback,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
