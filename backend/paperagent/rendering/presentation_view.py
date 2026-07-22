from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from paperagent.agents.document_ir import DocumentIR
from paperagent.schemas.presentation import (
    HeaderFooterSectionSpec,
    PageChromeToken,
    PageChromeTokenKind,
    ThreeRegionLine,
)


class RenderTokenKind(StrEnum):
    TEXT = "text"
    CURRENT_HEADING = "current_heading"
    PAGE_NUMBER = "page_number"
    TOTAL_PAGES = "total_pages"


@dataclass(frozen=True)
class RenderToken:
    kind: RenderTokenKind
    text: str = ""


@dataclass(frozen=True)
class RenderLine:
    left: tuple[RenderToken, ...] = ()
    center: tuple[RenderToken, ...] = ()
    right: tuple[RenderToken, ...] = ()


@dataclass(frozen=True)
class RenderPageChrome:
    header: RenderLine = RenderLine()
    footer: RenderLine = RenderLine()


@dataclass(frozen=True)
class RenderCoverField:
    semantic_key: str
    label: str
    value: str
    order: int


@dataclass(frozen=True)
class RenderCover:
    enabled: bool
    title: str
    subtitle: str | None
    fields: tuple[RenderCoverField, ...]
    alignment: str
    title_spacing_after_pt: float
    field_row_spacing_pt: float
    max_content_width_mm: float
    start_new_page_after: bool


@dataclass(frozen=True)
class RenderPresentationViewModel:
    schema_version: str
    cover: RenderCover
    default: RenderPageChrome
    first_page: RenderPageChrome
    odd_page: RenderPageChrome
    even_page: RenderPageChrome
    different_first_page: bool
    different_odd_even: bool

    @classmethod
    def from_document(cls, document: DocumentIR) -> RenderPresentationViewModel:
        presentation = document.presentation
        visible = sorted(
            (item for item in presentation.cover.fields if item.visible and item.value.strip()),
            key=lambda item: (item.order, item.semantic_key),
        )
        fields = tuple(
            RenderCoverField(
                semantic_key=item.semantic_key,
                label=item.label,
                value=item.value,
                order=item.order,
            )
            for item in visible
        )
        if not fields:
            chinese = document.language in {"zh", "mixed"}
            legacy_fields = [
                (
                    "author",
                    "作者" if chinese else "Author",
                    ", ".join(document.front_matter.authors),
                ),
                (
                    "institution",
                    "单位" if chinese else "Organization",
                    document.front_matter.organization or "",
                ),
                (
                    "date",
                    "日期" if chinese else "Date",
                    document.front_matter.date or "",
                ),
            ]
            fields = tuple(
                RenderCoverField(
                    semantic_key=key,
                    label=label,
                    value=value,
                    order=index * 10,
                )
                for index, (key, label, value) in enumerate(legacy_fields, start=1)
                if value
            )
        field_values = {item.semantic_key: item.value for item in fields}
        title = (presentation.cover.title or document.title).strip()
        date_value = document.front_matter.date or ""

        def tokens(items: list[PageChromeToken]) -> tuple[RenderToken, ...]:
            rendered: list[RenderToken] = []
            for item in items:
                if item.kind is PageChromeTokenKind.PAGE_NUMBER:
                    rendered.append(RenderToken(RenderTokenKind.PAGE_NUMBER))
                elif item.kind is PageChromeTokenKind.TOTAL_PAGES:
                    rendered.append(RenderToken(RenderTokenKind.TOTAL_PAGES))
                elif item.kind is PageChromeTokenKind.CURRENT_HEADING:
                    rendered.append(RenderToken(RenderTokenKind.CURRENT_HEADING))
                else:
                    value = {
                        PageChromeTokenKind.TEXT: item.value or "",
                        PageChromeTokenKind.DOCUMENT_TITLE: title,
                        PageChromeTokenKind.COVER_FIELD: field_values.get(
                            item.field_key or "", ""
                        ),
                        PageChromeTokenKind.DATE: date_value,
                    }.get(item.kind, "")
                    if value:
                        rendered.append(RenderToken(RenderTokenKind.TEXT, value))
            return tuple(rendered)

        def line(raw: ThreeRegionLine) -> RenderLine:
            return RenderLine(
                left=tokens(raw.left),
                center=tokens(raw.center),
                right=tokens(raw.right),
            )

        def chrome(raw: HeaderFooterSectionSpec | None) -> RenderPageChrome:
            if raw is None:
                return RenderPageChrome()
            return RenderPageChrome(header=line(raw.header), footer=line(raw.footer))

        page_chrome = presentation.page_chrome
        subtitle = presentation.cover.subtitle or document.front_matter.subtitle
        default = chrome(page_chrome.default)
        if not any(
            (
                default.header.left,
                default.header.center,
                default.header.right,
                default.footer.left,
                default.footer.center,
                default.footer.right,
            )
        ):
            header = str(document.metadata.get("header_text") or title)
            footer = str(document.metadata.get("footer_text") or "")
            default = RenderPageChrome(
                header=RenderLine(center=(RenderToken(RenderTokenKind.TEXT, header),)),
                footer=RenderLine(
                    center=tuple(
                        [
                            *(
                                [RenderToken(RenderTokenKind.TEXT, footer + " · ")]
                                if footer
                                else []
                            ),
                            RenderToken(RenderTokenKind.PAGE_NUMBER),
                            RenderToken(RenderTokenKind.TEXT, " / "),
                            RenderToken(RenderTokenKind.TOTAL_PAGES),
                        ]
                    )
                ),
            )
        return cls(
            schema_version=presentation.schema_version,
            cover=RenderCover(
                enabled=presentation.cover.enabled,
                title=title,
                subtitle=subtitle.strip() if subtitle else None,
                fields=fields,
                alignment=presentation.cover.layout.alignment,
                title_spacing_after_pt=presentation.cover.layout.title_spacing_after_pt,
                field_row_spacing_pt=presentation.cover.layout.field_row_spacing_pt,
                max_content_width_mm=presentation.cover.layout.max_content_width_mm,
                start_new_page_after=presentation.cover.layout.start_new_page_after,
            ),
            default=default,
            first_page=chrome(page_chrome.first_page),
            odd_page=chrome(page_chrome.odd_page) if page_chrome.odd_page else default,
            even_page=chrome(page_chrome.even_page) if page_chrome.even_page else default,
            different_first_page=page_chrome.different_first_page,
            different_odd_even=page_chrome.different_odd_even,
        )

    def semantic_snapshot(self) -> dict[str, object]:
        def token(item: RenderToken) -> dict[str, str]:
            return {"kind": item.kind.value, "text": item.text}

        def line(item: RenderLine) -> dict[str, list[dict[str, str]]]:
            return {
                "left": [token(value) for value in item.left],
                "center": [token(value) for value in item.center],
                "right": [token(value) for value in item.right],
            }

        def chrome(item: RenderPageChrome) -> dict[str, object]:
            return {"header": line(item.header), "footer": line(item.footer)}

        return {
            "schema_version": self.schema_version,
            "cover": {
                "enabled": self.cover.enabled,
                "title": self.cover.title,
                "subtitle": self.cover.subtitle,
                "fields": [
                    {
                        "semantic_key": item.semantic_key,
                        "label": item.label,
                        "value": item.value,
                        "order": item.order,
                    }
                    for item in self.cover.fields
                ],
            },
            "page_chrome": {
                "default": chrome(self.default),
                "first_page": chrome(self.first_page),
                "odd_page": chrome(self.odd_page),
                "even_page": chrome(self.even_page),
                "different_first_page": self.different_first_page,
                "different_odd_even": self.different_odd_even,
            },
        }
