from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class OutputFormat(StrEnum):
    MARKDOWN = "markdown"
    DOCX = "docx"
    XELATEX_PDF = "xelatex_pdf"
    WORD_PDF = "word_pdf"


class SemanticElement(StrEnum):
    COVER = "cover"
    TABLE_OF_CONTENTS = "table_of_contents"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    FIGURE = "figure"
    EQUATION = "equation"
    CODE = "code"
    CITATION = "citation"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    PAGE_BREAK = "page_break"
    SECTION_BREAK = "section_break"


class Fidelity(StrEnum):
    EXACT = "exact"
    EQUIVALENT = "equivalent"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True)

    fidelity: Fidelity
    representation: str
    limitation: str | None = None


def _cap(
    fidelity: Fidelity,
    representation: str,
    limitation: str | None = None,
) -> Capability:
    return Capability(
        fidelity=fidelity,
        representation=representation,
        limitation=limitation,
    )


FORMAT_CAPABILITIES: dict[SemanticElement, dict[OutputFormat, Capability]] = {
    SemanticElement.COVER: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EQUIVALENT, "title metadata and lead section"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "cover section"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "title page"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "DOCX cover section"),
    },
    SemanticElement.TABLE_OF_CONTENTS: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EQUIVALENT, "linked heading list"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "TOC field"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "tableofcontents"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "updated Word TOC field"),
    },
    SemanticElement.HEADING: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EXACT, "ATX heading"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "named Heading style"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "section hierarchy"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "named Heading style"),
    },
    SemanticElement.PARAGRAPH: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.EQUIVALENT, "plain paragraph", "page typography is not portable Markdown"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "BodyText style"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "paragraph layout"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "BodyText style"),
    },
    SemanticElement.LIST: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EXACT, "nested GFM list"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "numbering definitions"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "itemize/enumerate"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "numbering definitions"),
    },
    SemanticElement.TABLE: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.DEGRADED, "GFM table", "merged cells and page layout require HTML fallback"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "native table"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "tabular/longtable"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "native table"),
    },
    SemanticElement.FIGURE: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EQUIVALENT, "portable relative image and caption"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "embedded media relationship and caption"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "figure environment"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "embedded media relationship and caption"),
    },
    SemanticElement.EQUATION: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EQUIVALENT, "CommonMark math extension"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "OMML"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "native TeX math"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "OMML"),
    },
    SemanticElement.CODE: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EXACT, "fenced code block"),
        OutputFormat.DOCX: _cap(Fidelity.EQUIVALENT, "Code named style"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EQUIVALENT, "verbatim/listings"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EQUIVALENT, "Code named style"),
    },
    SemanticElement.CITATION: {
        OutputFormat.MARKDOWN: _cap(Fidelity.EQUIVALENT, "stable citation key and references"),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "citation and bibliography fields"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "biblatex/biber"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "citation and bibliography fields"),
    },
    SemanticElement.HEADER: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.UNSUPPORTED, "none", "portable Markdown has no page header"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "section header"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "page style header"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "section header"),
    },
    SemanticElement.FOOTER: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.UNSUPPORTED, "none", "portable Markdown has no page footer"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "section footer"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "page style footer"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "section footer"),
    },
    SemanticElement.PAGE_NUMBER: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.UNSUPPORTED, "none", "pagination belongs to the viewer"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "PAGE field"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "page counter"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "PAGE field"),
    },
    SemanticElement.PAGE_BREAK: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.DEGRADED, "portable HTML page-break marker", "viewer support varies"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "page break element"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "newpage/clearpage"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "page break element"),
    },
    SemanticElement.SECTION_BREAK: {
        OutputFormat.MARKDOWN: _cap(
            Fidelity.DEGRADED, "semantic divider", "page section properties are not portable"
        ),
        OutputFormat.DOCX: _cap(Fidelity.EXACT, "section properties"),
        OutputFormat.XELATEX_PDF: _cap(Fidelity.EXACT, "layout boundary"),
        OutputFormat.WORD_PDF: _cap(Fidelity.EXACT, "section properties"),
    },
}


def capability_for(element: SemanticElement, output: OutputFormat) -> Capability:
    return FORMAT_CAPABILITIES[element][output]
