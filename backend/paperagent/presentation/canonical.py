from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

from paperagent.schemas.presentation import (
    CoverField,
    CoverLayoutSpec,
    CoverSpec,
    DocumentPresentationSpec,
    HeaderFooterSectionSpec,
    PageChromeSpec,
    PageChromeToken,
    PageChromeTokenKind,
    PresentationExpectationManifest,
    PresentationFieldProvenance,
    PresentationPatchKind,
    PresentationPatchOperation,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
    ThreeRegionLine,
)

if TYPE_CHECKING:
    from paperagent.agents.document_ir import DocumentIR


def expectation_from_presentation(
    value: DocumentPresentationSpec,
    *,
    allow_format_degradation: bool = False,
) -> PresentationExpectationManifest:
    header_tokens = _all_tokens(value.page_chrome, area="header")
    footer_tokens = _all_tokens(value.page_chrome, area="footer")
    return PresentationExpectationManifest(
        required_cover_keys=[item.semantic_key for item in value.cover.fields if item.visible],
        expected_header_text=[
            item.value
            for item in header_tokens
            if item.kind is PageChromeTokenKind.TEXT and item.value
        ],
        expected_footer_text=[
            item.value
            for item in footer_tokens
            if item.kind is PageChromeTokenKind.TEXT and item.value
        ],
        hide_on_cover=value.page_chrome.different_first_page,
        require_page_number=any(
            item.kind is PageChromeTokenKind.PAGE_NUMBER for item in footer_tokens
        ),
        require_total_pages=any(
            item.kind is PageChromeTokenKind.TOTAL_PAGES for item in footer_tokens
        ),
        allow_format_degradation=allow_format_degradation,
    )


def _all_tokens(
    value: PageChromeSpec,
    *,
    area: str,
) -> list[PageChromeToken]:
    sections = [value.default, value.first_page, value.odd_page, value.even_page]
    tokens: list[PageChromeToken] = []
    for section in sections:
        if section is None:
            continue
        line = section.header if area == "header" else section.footer
        tokens.extend([*line.left, *line.center, *line.right])
    return tokens


def presentation_from_requirement(
    value: RequirementPresentationSpec,
    *,
    document_id: UUID,
) -> DocumentPresentationSpec:
    request_cover = value.cover
    fields = []
    for index, item in enumerate(request_cover.fields if request_cover else [], start=1):
        if not item.value:
            continue
        fields.append(
            CoverField(
                field_id=uuid5(
                    NAMESPACE_URL,
                    f"paperagent:{document_id}:cover:{item.semantic_key}",
                ),
                semantic_key=item.semantic_key,
                label=item.label,
                value=item.value,
                order=item.order if item.order is not None else index * 10,
                provenance=PresentationFieldProvenance(
                    source=item.source,
                    source_ref=item.source_ref,
                ),
            )
        )
    cover = CoverSpec(
        enabled=request_cover.enabled is not False if request_cover else True,
        title=request_cover.title if request_cover else None,
        subtitle=request_cover.subtitle if request_cover else None,
        fields=fields,
        layout=CoverLayoutSpec(
            preset=request_cover.layout_hint or "academic-centered"
            if request_cover
            else "academic-centered"
        ),
    )
    return DocumentPresentationSpec(
        cover=cover,
        page_chrome=_page_chrome_from_requirement(value.page_chrome),
    )


def _page_chrome_from_requirement(value: RequirementPageChromeSpec | None) -> PageChromeSpec:
    if value is None:
        return default_page_chrome()
    header = ThreeRegionLine(
        left=_text_tokens(value.header_left),
        center=_text_tokens(value.header_center),
        right=_text_tokens(value.header_right),
    )
    footer = ThreeRegionLine(
        left=_text_tokens(value.footer_left),
        center=_footer_tokens(value),
        right=_text_tokens(value.footer_right),
    )
    return PageChromeSpec(
        default=HeaderFooterSectionSpec(header=header, footer=footer),
        different_first_page=value.hide_on_cover is not False,
        different_odd_even=bool(value.different_odd_even),
    )


def default_page_chrome(
    *,
    header_text: str | None = None,
    footer_text: str | None = None,
) -> PageChromeSpec:
    header = (
        _text_tokens(header_text)
        if header_text
        else [PageChromeToken(kind=PageChromeTokenKind.DOCUMENT_TITLE)]
    )
    footer = _text_tokens(footer_text)
    footer.append(PageChromeToken(kind=PageChromeTokenKind.PAGE_NUMBER))
    return PageChromeSpec(
        default=HeaderFooterSectionSpec(
            header=ThreeRegionLine(center=header),
            footer=ThreeRegionLine(center=footer),
        ),
        different_first_page=True,
    )


def _text_tokens(value: str | None) -> list[PageChromeToken]:
    return [PageChromeToken(kind=PageChromeTokenKind.TEXT, value=value)] if value else []


def _footer_tokens(value: RequirementPageChromeSpec) -> list[PageChromeToken]:
    tokens = _text_tokens(value.footer_center)
    if value.page_number:
        if tokens:
            tokens.append(PageChromeToken(kind=PageChromeTokenKind.TEXT, value=" · "))
        if value.total_pages:
            tokens.extend(
                [
                    PageChromeToken(kind=PageChromeTokenKind.TEXT, value="第 "),
                    PageChromeToken(kind=PageChromeTokenKind.PAGE_NUMBER),
                    PageChromeToken(kind=PageChromeTokenKind.TEXT, value=" 页 / 共 "),
                    PageChromeToken(kind=PageChromeTokenKind.TOTAL_PAGES),
                    PageChromeToken(kind=PageChromeTokenKind.TEXT, value=" 页"),
                ]
            )
        else:
            tokens.append(PageChromeToken(kind=PageChromeTokenKind.PAGE_NUMBER))
    return tokens


def apply_presentation_patch(
    document: DocumentIR,
    operations: list[PresentationPatchOperation],
) -> DocumentIR:
    # Local import avoids making the schema module depend on the Agent domain.
    from paperagent.agents.document_ir import DocumentIR

    if not isinstance(document, DocumentIR):
        raise TypeError("presentation patch requires DocumentIR")
    payload = document.presentation.model_copy(deep=True)
    fields = {item.semantic_key: item for item in payload.cover.fields}
    for operation in operations:
        kind = operation.kind
        if kind is PresentationPatchKind.SET_COVER_ENABLED:
            payload.cover.enabled = bool(operation.bool_value)
        elif kind is PresentationPatchKind.SET_COVER_TITLE:
            payload.cover.title = operation.value
        elif kind is PresentationPatchKind.SET_COVER_SUBTITLE:
            payload.cover.subtitle = operation.value
        elif kind is PresentationPatchKind.UPSERT_COVER_FIELD:
            assert operation.semantic_key and operation.label and operation.value
            current = fields.get(operation.semantic_key)
            fields[operation.semantic_key] = CoverField(
                field_id=current.field_id
                if current
                else uuid5(
                    NAMESPACE_URL,
                    f"paperagent:{document.document_id}:cover:{operation.semantic_key}",
                ),
                semantic_key=operation.semantic_key,
                label=operation.label,
                value=operation.value,
                order=operation.order
                if operation.order is not None
                else (current.order if current else (len(fields) + 1) * 10),
                provenance=PresentationFieldProvenance(source_ref="presentation.patch"),
            )
        elif kind is PresentationPatchKind.REMOVE_COVER_FIELD:
            assert operation.semantic_key
            fields.pop(operation.semantic_key, None)
        elif kind is PresentationPatchKind.REORDER_COVER_FIELDS:
            positions = {key: (index + 1) * 10 for index, key in enumerate(operation.ordered_keys)}
            fields = {
                key: item.model_copy(update={"order": positions.get(key, item.order)})
                for key, item in fields.items()
            }
        elif kind is PresentationPatchKind.SET_COVER_LAYOUT:
            assert operation.layout is not None
            payload.cover.layout = operation.layout
        elif kind in {
            PresentationPatchKind.SET_HEADER_REGION,
            PresentationPatchKind.SET_FOOTER_REGION,
        }:
            assert operation.region
            target = (
                payload.page_chrome.default.header
                if kind is PresentationPatchKind.SET_HEADER_REGION
                else payload.page_chrome.default.footer
            )
            setattr(target, operation.region, operation.tokens)
        elif kind is PresentationPatchKind.SET_FIRST_PAGE_POLICY:
            payload.page_chrome.different_first_page = bool(operation.bool_value)
        elif kind is PresentationPatchKind.SET_ODD_EVEN_POLICY:
            payload.page_chrome.different_odd_even = bool(operation.bool_value)
            if not operation.bool_value:
                payload.page_chrome.odd_page = None
                payload.page_chrome.even_page = None
    payload.cover.fields = sorted(fields.values(), key=lambda item: (item.order, item.semantic_key))
    validated = DocumentPresentationSpec.model_validate(payload.model_dump(mode="json"))
    return document.model_copy(
        deep=True,
        update={
            "presentation": validated,
            "revision": document.revision + 1,
            "updated_at": datetime.now(UTC),
        },
    )
