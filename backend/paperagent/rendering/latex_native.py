from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    InlineKind,
    InlineNode,
    ListItem,
)
from paperagent.rendering.fonts import FontResolver
from paperagent.rendering.layout import ArchetypeId
from paperagent.rendering.markdown_parser import math_aware_inline_nodes
from paperagent.rendering.math_markup import math_fragments
from paperagent.rendering.presentation_view import (
    RenderLine,
    RenderPresentationViewModel,
    RenderToken,
    RenderTokenKind,
)
from paperagent.schemas.typography import TypographySpec

Runner = Callable[[list[str], Path, int], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LatexTemplateSpec:
    template_id: str
    default_toc: bool
    heading_numbering: bool


LATEX_TEMPLATES: dict[ArchetypeId, LatexTemplateSpec] = {
    archetype: LatexTemplateSpec(
        template_id=f"paperagent-{archetype.value}",
        default_toc=archetype not in {ArchetypeId.MEETING_MINUTES, ArchetypeId.FORMAL_DOCUMENT},
        heading_numbering=archetype is not ArchetypeId.MEETING_MINUTES,
    )
    for archetype in ArchetypeId
}


@dataclass(frozen=True)
class NativeLatexResult:
    success: bool
    output: Path | None
    command: tuple[str, ...]
    log: str
    error_code: str | None
    rounds: int
    warnings: tuple[str, ...] = ()


class NativeLatexRenderer:
    def __init__(self, executable: str | None, runner: Runner) -> None:
        self.executable = executable
        self.runner = runner

    def source(self, document: DocumentIR) -> str:
        template = template_for(document)
        typography = document.typography
        if typography.body_font:
            body_font = cjk_font = typography.body_font
        else:
            resolver = FontResolver()
            body_font = (
                resolver.resolve("Times New Roman", allow_fallback=True).resolved
                or "Times New Roman"
            )
            cjk_font = resolver.resolve("宋体", allow_fallback=True).resolved or "Noto Serif CJK SC"
        size = typography.body_size_pt or 11
        baseline = size * (typography.line_spacing or 1.2)
        presentation = RenderPresentationViewModel.from_document(document)
        lines = [
            rf"\documentclass[{size:g}pt,a4paper]{{article}}",
            r"\usepackage{fontspec}",
            r"\usepackage{xeCJK}",
            r"\usepackage{amsmath}",
            r"\usepackage{unicode-math}",
            r"\usepackage{graphicx}",
            r"\usepackage{geometry}",
            r"\geometry{a4paper,top=25.4mm,bottom=25.4mm,left=31.8mm,right=31.8mm}",
            r"\usepackage{fancyhdr}",
            r"\usepackage{lastpage}",
            r"\usepackage{longtable}",
            r"\usepackage{tabularx}",
            r"\usepackage{booktabs}",
            r"\usepackage{array}",
            r"\usepackage{enumitem}",
            r"\usepackage{listings}",
            r"\usepackage{xcolor}",
            r"\usepackage{hyperref}",
            r"\usepackage{bookmark}",
            r"\usepackage{caption}",
            r"\usepackage{float}",
            rf"\setmainfont{{{escape(body_font)}}}",
            rf"\setCJKmainfont{{{escape(cjk_font)}}}",
            r"\hypersetup{unicode=true,hidelinks,pdfcreator={PaperAgent}}",
            rf"\def\PaperAgentTemplateId{{{escape(template.template_id)}}}",
            r"\setlength{\headheight}{14pt}",
            r"\setcounter{tocdepth}{3}",
            r"\lstset{basicstyle=\ttfamily\small,breaklines=true,frame=single,backgroundcolor=\color{gray!5}}",
        ]
        if document.language in {"zh", "mixed"}:
            lines.extend(
                [
                    r"\renewcommand{\contentsname}{目录}",
                    r"\renewcommand{\figurename}{图}",
                    r"\renewcommand{\tablename}{表}",
                ]
            )
        equation_fonts = {
            local.equation_font
            for section in document.iter_sections()
            for block in section.blocks
            if block.kind is BlockKind.EQUATION
            for local in [
                document.resolve_typography(
                    section_id=section.section_id,
                    block_id=block.block_id,
                )
            ]
            if local.equation_font
        }
        if len(equation_fonts) > 1:
            raise ValueError(
                "XeLaTeX renderer requires one resolved equation font per document"
            )
        if equation_fonts:
            lines.append(rf"\setmathfont{{{escape(next(iter(equation_fonts)))}}}")
        lines.extend(self._page_styles(presentation))
        lines.extend(
            [
                r"\begin{document}",
                rf"\fontsize{{{size:g}}}{{{baseline:g}}}\selectfont",
                r"\date{}",
                *self._cover_source(presentation),
                r"\pagestyle{paperagentdefault}",
            ]
        )
        if self.toc_enabled(document, template=template):
            lines.extend([r"\tableofcontents", r"\clearpage"])
        for section in document.sections:
            lines.extend(self._section(document, section, depth=1))
        references = reference_entries(document)
        if references:
            heading = "参考文献" if document.language in {"zh", "mixed"} else "References"
            lines.append(rf"\section*{{{heading}}}")
            for item in references:
                source = f" --- {escape(str(item['source_uri']))}" if item["source_uri"] else ""
                lines.append(
                    rf"\noindent [{item['number']}] {escape(str(item['title']))}{source}"
                    r"\par\smallskip"
                )
        lines.append(r"\end{document}")
        return "\n".join(lines) + "\n"

    def _page_styles(self, presentation: RenderPresentationViewModel) -> list[str]:
        lines = [r"\fancypagestyle{paperagentdefault}{", r"\fancyhf{}"]
        if presentation.different_odd_even:
            lines.extend(
                self._fancy_line(presentation.odd_page.header, "head", page="O")
            )
            lines.extend(
                self._fancy_line(presentation.odd_page.footer, "foot", page="O")
            )
            lines.extend(
                self._fancy_line(presentation.even_page.header, "head", page="E")
            )
            lines.extend(
                self._fancy_line(presentation.even_page.footer, "foot", page="E")
            )
        else:
            lines.extend(self._fancy_line(presentation.default.header, "head"))
            lines.extend(self._fancy_line(presentation.default.footer, "foot"))
        lines.append(r"}")
        lines.extend([r"\fancypagestyle{paperagentfirst}{", r"\fancyhf{}"])
        if presentation.different_first_page:
            lines.extend(self._fancy_line(presentation.first_page.header, "head"))
            lines.extend(self._fancy_line(presentation.first_page.footer, "foot"))
            if not any(
                (
                    presentation.first_page.header.left,
                    presentation.first_page.header.center,
                    presentation.first_page.header.right,
                )
            ):
                lines.append(r"\renewcommand{\headrulewidth}{0pt}")
        else:
            lines.extend(self._fancy_line(presentation.default.header, "head"))
            lines.extend(self._fancy_line(presentation.default.footer, "foot"))
        lines.append(r"}")
        return lines

    def _fancy_line(self, line: RenderLine, kind: str, *, page: str = "") -> list[str]:
        commands: list[str] = []
        for region, tokens in (("L", line.left), ("C", line.center), ("R", line.right)):
            if not tokens:
                continue
            selector = f"{region}{page}" if page else region
            body = "".join(self._latex_token(item) for item in tokens)
            commands.append(rf"\fancy{kind}[{selector}]{{{body}}}")
        return commands

    @staticmethod
    def _latex_token(token: RenderToken) -> str:
        if token.kind is RenderTokenKind.TEXT:
            return escape(token.text)
        if token.kind is RenderTokenKind.PAGE_NUMBER:
            return r"\thepage"
        if token.kind is RenderTokenKind.TOTAL_PAGES:
            return r"\pageref*{LastPage}"
        return r"\nouppercase{\leftmark}"

    def _cover_source(self, presentation: RenderPresentationViewModel) -> list[str]:
        cover = presentation.cover
        if not cover.enabled:
            return []
        if len(cover.fields) > 24 or sum(len(item.value) for item in cover.fields) > 8_000:
            raise ValueError("cover content exceeds the safe single-page layout budget")
        alignment = {"left": "flushleft", "center": "center", "right": "flushright"}[
            cover.alignment
        ]
        lines = [r"\begin{titlepage}" if cover.start_new_page_after else r"\begingroup"]
        lines.extend(
            [
                rf"\begin{{{alignment}}}",
                r"\vspace*{0.12\textheight}",
                rf"{{\LARGE\bfseries {escape(cover.title)}\par}}",
            ]
        )
        if cover.subtitle:
            lines.extend(
                [r"\vspace{1.2em}", rf"{{\large {escape(cover.subtitle)}\par}}"]
            )
        lines.extend(
            [
                rf"\vspace{{{max(12, cover.title_spacing_after_pt):g}pt}}",
                r"\end{" + alignment + "}",
            ]
        )
        if cover.fields:
            lines.extend(
                [
                    r"\begin{center}",
                    rf"\begin{{tabularx}}{{{cover.max_content_width_mm:g}mm}}"
                    r"{@{}>{\raggedleft\arraybackslash}p{0.28\linewidth} "
                    r"@{\quad} X@{}}",
                ]
            )
            for item in cover.fields:
                lines.append(
                    rf"{escape(item.label)} & {escape(item.value)} \\["
                    rf"{max(2, cover.field_row_spacing_pt):g}pt]"
                )
            lines.extend([r"\end{tabularx}", r"\end{center}"])
        lines.append(r"\thispagestyle{paperagentfirst}")
        lines.append(r"\end{titlepage}" if cover.start_new_page_after else r"\endgroup")
        return lines

    def _section(
        self,
        document: DocumentIR,
        section: DocumentSection,
        *,
        depth: int,
    ) -> list[str]:
        commands = ("section", "subsection", "subsubsection", "paragraph", "subparagraph")
        command = commands[min(depth - 1, len(commands) - 1)]
        lines = [rf"\{command}{{{escape(section.title)}}}"]
        for block in section.blocks:
            lines.extend(self._block(document, section, block))
        for child in section.children:
            lines.extend(self._section(document, child, depth=depth + 1))
        return lines

    def _block(
        self,
        document: DocumentIR,
        section: DocumentSection,
        block: DocumentBlock,
    ) -> list[str]:
        typography = document.resolve_typography(
            section_id=section.section_id,
            block_id=block.block_id,
        )
        style_open, style_close = style_scope(typography, block.kind)
        citations = "".join(f" [{number}]" for number in block_citation_numbers(document, block))
        if block.kind is BlockKind.FIGURE:
            path = (block.figure.path if block.figure else str(block.data.get("path", ""))) or ""
            path = path.replace("\\", "/")
            return [
                style_open,
                r"\begin{figure}[H]",
                r"\centering",
                rf"\includegraphics[width=0.85\linewidth,height=0.72\textheight,"
                rf"keepaspectratio]{{\detokenize{{{path}}}}}",
                rf"\caption{{{escape(caption_body(block.caption or 'Figure', 'Figure'))}}}",
                r"\end{figure}",
                style_close,
            ]
        if block.kind is BlockKind.TABLE and block.table:
            return [style_open, *self._table(block), style_close]
        if block.kind is BlockKind.LIST and block.list_spec:
            return [
                style_open,
                *self._list(
                    block.list_spec.items,
                    ordered=block.list_spec.kind.value == "ordered",
                ),
                style_close,
            ]
        if block.kind is BlockKind.CODE:
            language = safe_language(str(block.data.get("language", "")))
            return [
                style_open,
                rf"\begin{{lstlisting}}[language={language}]",
                block.text,
                r"\end{lstlisting}",
                style_close,
            ]
        if block.kind is BlockKind.QUOTE:
            return [style_open, r"\begin{quote}", escape(block.text), r"\end{quote}", style_close]
        if block.kind is BlockKind.EQUATION:
            equation = block.equation.latex if block.equation else block.text
            return [style_open, r"\begin{equation}", equation, r"\end{equation}", style_close]
        if block.kind in {BlockKind.PAGE_BREAK, BlockKind.SECTION_BREAK}:
            return [r"\clearpage"]
        if block.kind is BlockKind.HEADING:
            level = max(1, min(5, int(str(block.data.get("level", 2)))))
            command = ("section", "subsection", "subsubsection", "paragraph", "subparagraph")[
                level - 1
            ]
            return [rf"\{command}{{{escape(block.text)}}}"]
        nodes = math_aware_inline_nodes(block.text, block.inlines)
        body = inline_source(nodes) if nodes else _inline_text_with_math(block.text)
        return [style_open + body + citations + r"\par" + style_close]

    def _table(self, block: DocumentBlock) -> list[str]:
        assert block.table is not None
        columns = max(len(row.cells) for row in block.table.rows)
        configured = block.table.column_widths
        weights = configured or [1.0] * columns
        total = sum(weights)
        specification = (
            "@{}"
            + "".join(
                rf">{{\raggedright\arraybackslash}}p{{{0.92 * weight / total:.3f}\linewidth}}"
                for weight in weights
            )
            + "@{}"
        )
        lines = [rf"\begin{{longtable}}{{{specification}}}"]
        if block.caption:
            lines.append(rf"\caption{{{escape(caption_body(block.caption, 'Table'))}}}\\")
        lines.append(r"\toprule")
        header: str | None = None
        for index, row in enumerate(block.table.rows):
            cells = []
            for cell in row.cells:
                nodes = math_aware_inline_nodes(cell.text, cell.inlines)
                cells.append(
                    inline_source(nodes) if nodes else _inline_text_with_math(cell.text)
                )
            cells += [""] * (columns - len(cells))
            rendered = " & ".join(cells) + r" \\"
            lines.append(rendered)
            if index == 0:
                header = rendered
                lines.extend(
                    [r"\midrule", r"\endfirsthead", r"\toprule", header, r"\midrule", r"\endhead"]
                )
        lines.extend([r"\bottomrule", r"\end{longtable}"])
        return lines

    def _list(self, items: list[ListItem], *, ordered: bool) -> list[str]:
        environment = "enumerate" if ordered else "itemize"
        lines = [rf"\begin{{{environment}}}"]
        for item in items:
            nodes = math_aware_inline_nodes(item.text, item.inlines)
            body = inline_source(nodes) if nodes else _inline_text_with_math(item.text)
            lines.append(rf"\item {body}")
            if item.children:
                lines.extend(self._list(item.children, ordered=False))
        lines.append(rf"\end{{{environment}}}")
        return lines

    @staticmethod
    def toc_enabled(
        document: DocumentIR,
        *,
        template: LatexTemplateSpec | None = None,
    ) -> bool:
        requested = document.metadata.get("toc")
        if isinstance(requested, bool):
            return requested
        if template and not template.default_toc:
            return False
        return len(list(document.iter_sections())) >= 4 or len(list(document.iter_blocks())) >= 24

    def render(
        self, document: DocumentIR, output: Path, *, timeout: int = 180
    ) -> NativeLatexResult:
        output = output.resolve()
        source = output.with_suffix(".tex")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(self.source(document), encoding="utf-8", newline="\n")
        if not self.executable:
            return NativeLatexResult(
                False,
                None,
                (),
                "xelatex not found; configure TeX Live path",
                "TEXLIVE_MISSING",
                0,
            )
        command = [
            self.executable,
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={source.parent}",
            str(source),
        ]
        logs: list[str] = []
        rounds = (
            3
            if self.toc_enabled(document, template=template_for(document))
            or reference_entries(document)
            else 2
        )
        for completed_round in range(1, rounds + 1):
            try:
                completed = self.runner(command, source.parent, timeout)
            except subprocess.TimeoutExpired as error:
                return NativeLatexResult(
                    False,
                    None,
                    tuple(command),
                    str(error),
                    "LATEX_TIMEOUT",
                    completed_round - 1,
                )
            logs.append(completed.stdout + completed.stderr)
            if completed.returncode != 0:
                log = "\n".join(logs)
                return NativeLatexResult(
                    False,
                    None,
                    tuple(command),
                    log,
                    classify_error(log),
                    completed_round,
                    classify_warnings(log),
                )
        produced = source.with_suffix(".pdf")
        if produced != output and produced.exists():
            shutil.copy2(produced, output)
        log = "\n".join(logs)
        return NativeLatexResult(
            output.is_file(),
            output if output.is_file() else None,
            tuple(command),
            log,
            None if output.is_file() else "LATEX_OUTPUT_MISSING",
            rounds,
            classify_warnings(log),
        )


def template_for(document: DocumentIR) -> LatexTemplateSpec:
    raw = document.metadata.get("archetype", ArchetypeId.RESEARCH_REPORT.value)
    try:
        archetype = ArchetypeId(str(raw))
    except ValueError:
        archetype = ArchetypeId.RESEARCH_REPORT
    requested = document.metadata.get("latex_template_id")
    template = LATEX_TEMPLATES[archetype]
    if requested is not None and str(requested) != template.template_id:
        raise ValueError("unregistered LaTeX template id")
    return template


def validate_template_source(source: str) -> None:
    """Reject execution and filesystem primitives in a future uploaded template."""

    forbidden = (
        r"\write18",
        r"\immediate\write",
        r"\input|",
        r"\include|",
        r"\openout",
        r"\read",
    )
    normalized = source.casefold()
    if any(token.casefold() in normalized for token in forbidden):
        raise ValueError("LaTeX template contains a forbidden primitive")
    if source.count("{{DOCUMENT_BODY}}") != 1:
        raise ValueError("LaTeX template must contain exactly one DOCUMENT_BODY placeholder")


def escape(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "$": r"\$",
        "&": r"\&",
        "#": r"\#",
        "%": r"\%",
        "_": r"\_",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def caption_body(value: str, sequence: str) -> str:
    """Remove an author-supplied caption label before the renderer adds SEQ numbering."""

    labels = ("Table", "表") if sequence == "Table" else ("Figure", "图")
    punctuation = ":\uFF1A.\u3001-"
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = rf"^\s*(?:{label_pattern})\s*\d*\s*[{punctuation}]?\s*"
    body = re.sub(pattern, "", value, count=1, flags=re.IGNORECASE).strip()
    return body or ("数据表" if sequence == "Table" else "插图")


def inline_source(nodes: list[InlineNode]) -> str:
    parts: list[str] = []
    for node in nodes:
        content = (
            inline_source(node.children)
            if node.children
            else _inline_text_with_math(node.text)
        )
        if node.kind is InlineKind.STRONG:
            content = rf"\textbf{{{content}}}"
        elif node.kind is InlineKind.EMPHASIS:
            content = rf"\emph{{{content}}}"
        elif node.kind is InlineKind.CODE:
            content = rf"\texttt{{{escape(node.text)}}}"
        elif node.kind is InlineKind.LINK and node.href:
            content = rf"\href{{{escape(node.href)}}}{{{content}}}"
        elif node.kind is InlineKind.CROSS_REFERENCE:
            content = rf"\ref{{{safe_label(node.text)}}}"
        elif node.kind is InlineKind.FOOTNOTE:
            content = rf"\footnote{{{content}}}"
        parts.append(content)
    return "".join(parts)


def _inline_text_with_math(value: str) -> str:
    """Escape prose while preserving trusted TeX inside Markdown math delimiters."""
    parts: list[str] = []
    for kind, content in math_fragments(value):
        if kind == "display":
            parts.append(r"\[" + content + r"\]")
        elif kind == "inline":
            parts.append(r"\(" + content + r"\)")
        else:
            parts.append(escape(content))
    return "".join(parts)


def style_scope(typography: TypographySpec, kind: BlockKind) -> tuple[str, str]:
    if kind is BlockKind.HEADING:
        font = typography.heading_font or typography.body_font
        size = typography.heading_size_pt or typography.body_size_pt
    elif kind is BlockKind.TABLE:
        font = typography.table_font or typography.body_font
        size = typography.table_size_pt or typography.body_size_pt
    elif kind is BlockKind.CODE:
        font = typography.code_font or typography.body_font
        size = typography.code_size_pt or typography.body_size_pt
    elif kind is BlockKind.EQUATION:
        font = typography.equation_font or typography.body_font
        size = typography.equation_size_pt or typography.body_size_pt
    else:
        font = typography.body_font
        size = typography.body_size_pt
    commands: list[str] = []
    if kind is not BlockKind.EQUATION and font:
        commands.extend((rf"\fontspec{{{escape(font)}}}", rf"\CJKfontspec{{{escape(font)}}}"))
    if size:
        baseline = size * (typography.line_spacing or 1.2)
        # Terminate the control word explicitly before CJK text. XeTeX treats CJK
        # letters as part of an adjacent command name (for example, \selectfont驻波).
        commands.append(rf"\fontsize{{{size:g}}}{{{baseline:g}}}\selectfont{{}}")
    return ("{" + "".join(commands), "}") if commands else ("", "")


def reference_entries(document: DocumentIR) -> list[dict[str, str | int]]:
    raw = document.metadata.get("evidence_manifest", [])
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "Untitled source"))
        uri = str(item.get("source_uri", ""))
        key = uri or title.casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append({"number": len(entries) + 1, "title": title, "source_uri": uri})
    return entries


def block_citation_numbers(document: DocumentIR, block: DocumentBlock) -> list[int]:
    raw = document.metadata.get("evidence_manifest", [])
    if not isinstance(raw, list):
        return []
    mapping: dict[str, int] = {}
    source_numbers: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("source_uri", "")) or str(item.get("title", "")).casefold()
        source_numbers.setdefault(key, len(source_numbers) + 1)
        mapping[str(item.get("evidence_id", ""))] = source_numbers[key]
    return sorted(
        {
            mapping[str(citation.evidence_id)]
            for citation in block.citations
            if str(citation.evidence_id) in mapping
        }
    )


def safe_language(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_+-]", "", value)


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9:._-]", "-", value)


def classify_error(log: str) -> str:
    lowered = log.casefold()
    if "font" in lowered and ("not found" in lowered or "cannot be found" in lowered):
        return "LATEX_FONT_MISSING"
    if "file" in lowered and "not found" in lowered:
        if re.search(r"\.(png|jpe?g|pdf|svg)", lowered):
            return "LATEX_ASSET_MISSING"
        if ".sty" in lowered:
            return "LATEX_PACKAGE_MISSING"
    if "undefined control sequence" in lowered:
        return "LATEX_UNDEFINED_COMMAND"
    if "emergency stop" in lowered:
        return "LATEX_SYNTAX_ERROR"
    return "LATEX_COMPILE_FAILED"


def classify_warnings(log: str) -> tuple[str, ...]:
    warnings: list[str] = []
    lowered = log.casefold()
    if "undefined references" in lowered or ("reference" in lowered and "undefined" in lowered):
        warnings.append("LATEX_UNDEFINED_REFERENCE")
    if "overfull \\hbox" in lowered:
        warnings.append("LATEX_OVERFULL_BOX")
    if "underfull \\hbox" in lowered:
        warnings.append("LATEX_UNDERFULL_BOX")
    return tuple(warnings)
