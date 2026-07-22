from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    InlineKind,
    InlineNode,
    ListItem,
)
from paperagent.rendering.docx_native import NativeDocxRenderer
from paperagent.rendering.fonts import FontResolver
from paperagent.rendering.latex_native import NativeLatexRenderer
from paperagent.rendering.markdown_parser import (
    math_aware_inline_nodes,
    parse_markdown_blocks,
)
from paperagent.rendering.math_markup import canonicalize_math_delimiters, math_fragments
from paperagent.rendering.presentation_view import RenderPresentationViewModel
from paperagent.schemas.typography import TypographySpec


@dataclass(frozen=True)
class CompileResult:
    success: bool
    output: Path | None
    command: tuple[str, ...]
    log: str
    error_code: str | None = None


Runner = Callable[[list[str], Path, int], subprocess.CompletedProcess[str]]


def default_runner(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class MarkdownRenderer:
    """Render portable CommonMark/GFM without leaking PaperAgent metadata."""

    def render(
        self,
        document: DocumentIR,
        output: Path,
        *,
        include_front_matter: bool = False,
    ) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        lines = self._front_matter(document) if include_front_matter else []
        presentation = RenderPresentationViewModel.from_document(document)
        lines.extend(self._cover(presentation, fallback_title=document.title))
        front = document.front_matter
        if front.abstract:
            lines.extend(["## Abstract", "", front.abstract.strip(), ""])
        if front.keywords:
            lines.extend(
                [f"**Keywords:** {', '.join(self._plain(item) for item in front.keywords)}", ""]
            )
        citation_numbers = _citation_numbers(document)
        for section in document.sections:
            lines.extend(self._section(section, citation_numbers, depth=1, output=output))
        if document.back_matter:
            lines.extend(["## Appendix", ""])
            for block in document.back_matter:
                lines.extend(self._block(block, citation_numbers, output))
        references = _reference_entries(document)
        if references:
            lines.extend(["## References", ""])
            for item in references:
                source = f" <{item['source_uri']}>" if item.get("source_uri") else ""
                locators = item.get("locators", [])
                locator_text = f" (locators: {locators})" if locators else ""
                lines.append(f"- [{item['number']}] {item['title']}{source}{locator_text}")
        output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
        return output

    def render_bundle(self, document: DocumentIR, output: Path) -> Path:
        """Create report.md + assets/ as a self-contained ZIP bundle."""

        output.parent.mkdir(parents=True, exist_ok=True)
        bundle_root = output.parent / f"{output.stem}-bundle"
        bundle_root.mkdir(parents=True, exist_ok=True)
        report = self.render(document, bundle_root / "report.md")
        presentation = RenderPresentationViewModel.from_document(document)
        manifest = bundle_root / "presentation.json"
        manifest.write_text(
            json.dumps(
                {
                    "presentation": presentation.semantic_snapshot(),
                    "capabilities": {
                        "cover": "semantic",
                        "repeating_page_chrome": "preview_only",
                        "dynamic_page_fields": "preview_only",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(report, "report.md")
            archive.write(manifest, "presentation.json")
            assets = bundle_root / "assets"
            if assets.is_dir():
                for asset in sorted(path for path in assets.rglob("*") if path.is_file()):
                    archive.write(asset, asset.relative_to(bundle_root).as_posix())
        return output

    def render_html_preview(self, document: DocumentIR, output: Path) -> Path:
        """Render a safe paged cover surface from the same presentation view model."""

        presentation = RenderPresentationViewModel.from_document(document)
        cover = presentation.cover
        fields = "".join(
            "<div class=\"cover-row\"><dt>"
            + html.escape(item.label)
            + "</dt><dd>"
            + html.escape(item.value)
            + "</dd></div>"
            for item in cover.fields
        )
        subtitle = (
            f'<p class="cover-subtitle">{html.escape(cover.subtitle)}</p>'
            if cover.subtitle
            else ""
        )
        cover_html = (
            '<section class="paper-page paper-cover" data-page="cover">'
            f'<h1>{html.escape(cover.title)}</h1>{subtitle}<dl>{fields}</dl></section>'
            if cover.enabled
            else ""
        )
        snapshot = html.escape(
            json.dumps(presentation.semantic_snapshot(), ensure_ascii=False)
        )
        markup = f"""<!doctype html>
<html lang="{html.escape(document.language)}"><head><meta charset="utf-8">
<meta name="paperagent-presentation" content="{snapshot}">
<style>
body{{margin:0;background:#111;color:#181818;font-family:system-ui,sans-serif}}
.paper-page{{box-sizing:border-box;width:210mm;min-height:297mm;margin:18px auto;
background:#fff;padding:25.4mm 31.8mm;box-shadow:0 8px 28px #0008}}
.paper-cover{{display:flex;flex-direction:column;align-items:center}}
.paper-cover h1{{margin:38mm 0 {cover.title_spacing_after_pt:g}pt;
text-align:{cover.alignment};font-size:24pt}}
.paper-cover dl{{width:min(100%,{cover.max_content_width_mm:g}mm);margin:0}}
.cover-row{{display:grid;grid-template-columns:32% 68%;gap:8mm;
margin-bottom:{cover.field_row_spacing_pt:g}pt;break-inside:avoid}}
.cover-row dt{{text-align:right;font-weight:600}} .cover-row dd{{margin:0}}
@media(max-width:900px){{.paper-page{{width:calc(100vw - 24px);min-height:auto;
margin:12px;padding:32px}}}}
</style></head><body>{cover_html}</body></html>"""
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markup, encoding="utf-8", newline="\n")
        return output

    def _cover(
        self,
        presentation: RenderPresentationViewModel,
        *,
        fallback_title: str,
    ) -> list[str]:
        cover = presentation.cover
        title = cover.title if cover.enabled else fallback_title
        lines = [f"# {self._plain(title)}", ""]
        if not cover.enabled:
            return lines
        if cover.subtitle:
            lines.extend([f"*{self._plain(cover.subtitle)}*", ""])
        if cover.fields:
            lines.extend(["| 项目 | 内容 |", "| --- | --- |"])
            for item in cover.fields:
                label = self._plain(item.label).replace("|", r"\|")
                value = self._plain(item.value).replace("|", r"\|")
                lines.append(f"| {label} | {value} |")
            lines.append("")
        if any(
            (
                presentation.default.header.left,
                presentation.default.header.center,
                presentation.default.header.right,
                presentation.default.footer.left,
                presentation.default.footer.center,
                presentation.default.footer.right,
            )
        ):
            lines.extend(
                [
                    "<!-- PaperAgent: repeating headers, footers and dynamic page fields "
                    "are retained in presentation metadata and rendered in paged preview, "
                    "DOCX and PDF; CommonMark does not emulate physical pages. -->",
                    "",
                ]
            )
        return lines

    def _section(
        self,
        section: DocumentSection,
        citation_numbers: dict[str, int],
        *,
        depth: int,
        output: Path,
    ) -> list[str]:
        level = max(2, min(6, section.level + 1 if section.level else depth + 1))
        lines = [f"{'#' * level} {self._plain(section.title)}", ""]
        for block in self._normalized_blocks(section.blocks):
            lines.extend(self._block(block, citation_numbers, output))
        for child in section.children:
            lines.extend(self._section(child, citation_numbers, depth=depth + 1, output=output))
        return lines

    @staticmethod
    def _normalized_blocks(blocks: list[DocumentBlock]) -> list[DocumentBlock]:
        if len(blocks) != 1 or blocks[0].kind is not BlockKind.PARAGRAPH:
            return blocks
        source = blocks[0].text
        if "\n" not in source or not re.search(r"(?m)^(#{1,6}\s|[-*+]\s|\d+[.)]\s|\|.+\|)", source):
            return blocks
        return parse_markdown_blocks(source, agent="markdown-compatibility-migration")

    def _block(
        self,
        block: DocumentBlock,
        citation_numbers: dict[str, int],
        output: Path,
    ) -> list[str]:
        if block.kind is BlockKind.CODE:
            language = str(block.data.get("language", ""))
            fence = "```" if "```" not in block.text else "````"
            return [f"{fence}{language}", block.text.rstrip("\n"), fence, ""]
        if block.kind is BlockKind.EQUATION:
            equation = block.equation.latex if block.equation else block.text
            return ["$$", equation.strip(), "$$", ""]
        if block.kind is BlockKind.QUOTE:
            return [*(f"> {line}" for line in block.text.splitlines()), ""]
        if block.kind is BlockKind.LIST:
            if block.list_spec:
                return [
                    *self._list(
                        block.list_spec.items,
                        ordered=block.list_spec.kind.value == "ordered",
                        start=block.list_spec.start,
                    ),
                    "",
                ]
            return [*(f"- {line}" for line in block.text.splitlines()), ""]
        if block.kind is BlockKind.TABLE:
            rows: object = (
                [
                    [self._inline_value(cell.text, cell.inlines) for cell in row.cells]
                    for row in block.table.rows
                ]
                if block.table
                else block.data.get("rows", [])
            )
            if isinstance(rows, list) and rows:
                matrix = [
                    [self._table_cell(str(cell)) for cell in row]
                    for row in rows
                    if isinstance(row, list)
                ]
                if not matrix:
                    return []
                width = max(len(row) for row in matrix)
                matrix = [row + [""] * (width - len(row)) for row in matrix]
                result = [
                    "| " + " | ".join(matrix[0]) + " |",
                    "| " + " | ".join(["---"] * width) + " |",
                    *("| " + " | ".join(row) + " |" for row in matrix[1:]),
                    "",
                ]
                if block.caption:
                    result.extend([f"*{self._plain(block.caption)}*", ""])
                return result
        if block.kind is BlockKind.FIGURE:
            source = block.figure.path if block.figure else str(block.data.get("path", ""))
            path = self._portable_asset(source or "", output)
            alt = self._plain(
                (block.figure.alt_text if block.figure else "") or block.caption or "Figure"
            )
            result = [f"![{alt}]({path})", ""]
            if block.caption:
                result.extend([f"*{self._plain(block.caption)}*", ""])
            return result
        if block.kind is BlockKind.HEADING:
            level = max(2, min(6, int(str(block.data.get("level", 2)))))
            return [f"{'#' * level} {self._inline_value(block.text, block.inlines)}", ""]
        if block.kind is BlockKind.PAGE_BREAK:
            return ['<div style="page-break-after: always;"></div>', ""]
        if block.kind is BlockKind.SECTION_BREAK:
            return ["---", ""]
        citations = "".join(
            f" [{number}]" for number in _block_citation_numbers(block, citation_numbers)
        )
        return [self._inline_value(block.text.strip(), block.inlines) + citations, ""]

    def _list(
        self, items: list[ListItem], *, ordered: bool, start: int = 1, depth: int = 0
    ) -> list[str]:
        lines: list[str] = []
        for index, item in enumerate(items):
            marker = f"{start + index}." if ordered else "-"
            lines.append(
                f"{'    ' * depth}{marker} {self._inline_value(item.text, item.inlines)}"
            )
            if item.children:
                lines.extend(self._list(item.children, ordered=False, depth=depth + 1))
        return lines

    @classmethod
    def _inline(cls, nodes: list[InlineNode]) -> str:
        rendered: list[str] = []
        for node in nodes:
            content = cls._inline(node.children) if node.children else cls._inline_text(node.text)
            if node.kind is InlineKind.STRONG:
                content = f"**{content}**"
            elif node.kind is InlineKind.EMPHASIS:
                content = f"*{content}*"
            elif node.kind is InlineKind.CODE:
                ticks = "``" if "`" in node.text else "`"
                content = f"{ticks}{node.text}{ticks}"
            elif node.kind is InlineKind.LINK and node.href:
                content = f"[{content}]({cls._safe_href(node.href)})"
            elif node.kind is InlineKind.CITATION:
                content = f"[@{cls._plain(node.text)}]"
            elif node.kind is InlineKind.CROSS_REFERENCE:
                content = f"[{content}](#{cls._anchor(node.text)})"
            elif node.kind is InlineKind.FOOTNOTE:
                content = f"[^{cls._anchor(node.text)}]"
            rendered.append(content)
        return "".join(rendered)

    @classmethod
    def _inline_value(cls, source: str, nodes: list[InlineNode]) -> str:
        repaired = math_aware_inline_nodes(source, nodes)
        return cls._inline(repaired) if repaired else canonicalize_math_delimiters(source)

    @staticmethod
    def _inline_text(value: str) -> str:
        parts: list[str] = []
        for kind, content in math_fragments(value):
            if kind == "display":
                parts.append(f"$$\n{content}\n$$")
            elif kind == "inline":
                parts.append(f"${content}$")
            else:
                parts.append(re.sub(r"([\\`*\[\]<>])", r"\\\1", content))
        return "".join(parts)

    @staticmethod
    def _plain(value: str) -> str:
        return value.replace("\r", " ").replace("\n", " ").strip()

    @staticmethod
    def _table_cell(value: str) -> str:
        return value.replace("\r", " ").replace("\n", "<br>").replace("|", "\\|")

    @staticmethod
    def _anchor(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip().lower()).strip("-")

    @staticmethod
    def _safe_href(value: str) -> str:
        return value.replace(" ", "%20").replace(")", "%29")

    @staticmethod
    def _front_matter(document: DocumentIR) -> list[str]:
        return [
            "---",
            f"title: {json.dumps(document.title, ensure_ascii=False)}",
            f"language: {json.dumps(document.language, ensure_ascii=False)}",
            "---",
            "",
        ]

    @staticmethod
    def _portable_asset(source: str, output: Path) -> str:
        if not source:
            return "assets/missing-figure"
        path = Path(source)
        if not path.is_file():
            return (
                source.replace("\\", "/").replace(" ", "%20")
                if not path.is_absolute()
                else "assets/missing-figure"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-") or "figure"
        target = output.parent / "assets" / f"{safe_stem}-{digest}{path.suffix.lower()}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(path, target)
        return target.relative_to(output.parent).as_posix().replace(" ", "%20")


class DocxRenderer:
    def render(self, document: DocumentIR, output: Path, *, template: Path | None = None) -> Path:
        return NativeDocxRenderer().render(document, output, template=template)


class TypstRenderer:
    def __init__(self, executable: str | None = None, runner: Runner = default_runner) -> None:
        self.executable = executable or shutil.which("typst")
        self.runner = runner

    def source(self, document: DocumentIR) -> str:
        typography = document.typography
        fonts = [
            item for item in (typography.body_font, "Noto Serif CJK SC", "Times New Roman") if item
        ]
        unique_fonts = list(dict.fromkeys(fonts))
        font_source = ", ".join(f'"{item}"' for item in unique_fonts)
        size = typography.body_size_pt or 11
        lines = [f"#set text(font: ({font_source}), size: {size}pt)"]
        if typography.line_spacing:
            leading = max(typography.line_spacing - 1, 0)
            lines.append(f"#set par(leading: {leading:.2f}em)")
        lines.append(f"= {_typst_escape(document.title)}")
        for section in document.sections:
            section_typography = document.resolve_typography(section_id=section.section_id)
            section_text = _typst_text(
                _typst_escape(section.title), section_typography, BlockKind.HEADING
            )
            lines.append(f"#heading(level: 2)[{section_text}]")
            for block in section.blocks:
                local = document.resolve_typography(
                    section_id=section.section_id, block_id=block.block_id
                )
                if block.kind is BlockKind.FIGURE:
                    path = str(block.data.get("path", "")).replace("\\", "/")
                    escaped_path = path.replace('"', '\\"')
                    caption = _typst_escape(block.caption or "Figure")
                    lines.append(
                        f'#figure(image("{escaped_path}", width: 85%), caption: [{caption}])'
                    )
                    continue
                if block.kind is BlockKind.EQUATION:
                    body = f"$ {block.text} $"
                elif block.kind is BlockKind.CODE:
                    body = "`" + block.text.replace("`", "\\`") + "`"
                else:
                    body = _typst_escape(block.text)
                citation_numbers = _citation_numbers(document)
                citations = "".join(
                    f" [{number}]" for number in _block_citation_numbers(block, citation_numbers)
                )
                lines.append(_typst_text(body + citations, local, block.kind))
        references = _reference_entries(document)
        if references:
            lines.append("#heading(level: 2)[References]")
            lines.extend(
                f"- [{item['number']}] {_typst_escape(str(item['title']))} "
                + (_typst_escape(str(item.get("source_uri", ""))))
                for item in references
            )
        return "\n\n".join(lines) + "\n"

    def render(self, document: DocumentIR, output: Path, *, timeout: int = 120) -> CompileResult:
        source = output.with_suffix(".typ")
        source.parent.mkdir(parents=True, exist_ok=True)
        staged = _stage_figure_assets(document, source.parent)
        source.write_text(self.source(staged), encoding="utf-8")
        if not self.executable:
            return CompileResult(False, None, (), "Typst executable not found", "TYPST_MISSING")
        command = [self.executable, "compile", str(source), str(output)]
        completed = self.runner(command, source.parent, timeout)
        success = completed.returncode == 0 and output.is_file()
        return CompileResult(
            success,
            output if success else None,
            tuple(command),
            completed.stdout + completed.stderr,
            None if success else "TYPST_COMPILE_FAILED",
        )


class LegacyLatexRenderer:
    def __init__(self, executable: str | None = None, runner: Runner = default_runner) -> None:
        self.executable = executable or shutil.which("xelatex")
        self.runner = runner

    def source(self, document: DocumentIR) -> str:
        def escape(value: str) -> str:
            return re.sub(r"([#$%&_{}])", r"\\\1", value)

        typography = document.typography
        if typography.body_font:
            body_font = typography.body_font
            cjk_font = typography.body_font
        else:
            resolver = FontResolver()
            latin = resolver.resolve("Times New Roman", allow_fallback=True)
            cjk = resolver.resolve("宋体", allow_fallback=True)
            body_font = latin.resolved or "Times New Roman"
            cjk_font = cjk.resolved or "Noto Serif CJK SC"
        size = typography.body_size_pt or 11
        baseline = size * (typography.line_spacing or 1.2)
        lines = [
            r"\documentclass{article}",
            r"\usepackage{fontspec}",
            r"\usepackage{xeCJK}",
            r"\usepackage{amsmath}",
            r"\usepackage{unicode-math}",
            r"\usepackage{graphicx}",
            f"\\setmainfont{{{escape(body_font)}}}",
            f"\\setCJKmainfont{{{escape(cjk_font)}}}",
        ]
        equation_fonts = {
            local.equation_font
            for section in document.sections
            for block in section.blocks
            if block.kind is BlockKind.EQUATION
            for local in [
                document.resolve_typography(section_id=section.section_id, block_id=block.block_id)
            ]
            if local.equation_font
        }
        lines.extend(
            f"\\setmathfont[version={_math_version(font)}]{{{escape(font)}}}"
            for font in sorted(equation_fonts)
        )
        lines.extend(
            [
                r"\begin{document}",
                f"\\fontsize{{{size:g}}}{{{baseline:g}}}\\selectfont",
                f"\\title{{{escape(document.title)}}}\\maketitle",
            ]
        )
        for section in document.sections:
            section_typography = document.resolve_typography(section_id=section.section_id)
            lines.append(
                "\\section{"
                + _latex_styled(escape(section.title), section_typography, BlockKind.HEADING)
                + "}"
            )
            for block in section.blocks:
                local = document.resolve_typography(
                    section_id=section.section_id, block_id=block.block_id
                )
                if block.kind is BlockKind.FIGURE:
                    path = str(block.data.get("path", "")).replace("\\", "/")
                    caption = escape(block.caption or "Figure")
                    lines.append(
                        "\\begin{figure}[htbp]\\centering"
                        f"\\includegraphics[width=0.85\\linewidth]{{\\detokenize{{{path}}}}}"
                        f"\\caption{{{caption}}}\\end{{figure}}"
                    )
                    continue
                body = (
                    f"\\[{block.text}\\]"
                    if block.kind is BlockKind.EQUATION
                    else escape(block.text)
                )
                citation_numbers = _citation_numbers(document)
                body += "".join(
                    f" [{number}]" for number in _block_citation_numbers(block, citation_numbers)
                )
                lines.append(_latex_styled(body, local, block.kind))
        references = _reference_entries(document)
        if references:
            lines.append(r"\section*{References}")
            for item in references:
                source = f" --- {escape(str(item['source_uri']))}" if item.get("source_uri") else ""
                lines.append(
                    f"\\noindent [{item['number']}] "
                    f"{escape(str(item['title']))}{source}\\par\\smallskip"
                )
        lines.append(r"\end{document}")
        return "\n".join(lines)

    def render(self, document: DocumentIR, output: Path, *, timeout: int = 180) -> CompileResult:
        source = output.with_suffix(".tex")
        source.parent.mkdir(parents=True, exist_ok=True)
        staged = _stage_figure_assets(document, source.parent)
        source.write_text(self.source(staged), encoding="utf-8")
        if not self.executable:
            return CompileResult(
                False, None, (), "xelatex not found; configure TeX Live path", "TEXLIVE_MISSING"
            )
        command = [
            self.executable,
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={source.parent}",
            str(source),
        ]
        logs = []
        for _ in range(2):
            completed = self.runner(command, source.parent, timeout)
            logs.append(completed.stdout + completed.stderr)
            if completed.returncode != 0:
                return CompileResult(
                    False, None, tuple(command), "\n".join(logs), "LATEX_COMPILE_FAILED"
                )
        produced = source.with_suffix(".pdf")
        if produced != output and produced.exists():
            shutil.copy2(produced, output)
        return CompileResult(
            output.exists(),
            output if output.exists() else None,
            tuple(command),
            "\n".join(logs),
            None if output.exists() else "LATEX_OUTPUT_MISSING",
        )


class LatexRenderer:
    def __init__(self, executable: str | None = None, runner: Runner = default_runner) -> None:
        self.executable = executable or shutil.which("xelatex")
        self.runner = runner

    def source(self, document: DocumentIR) -> str:
        return NativeLatexRenderer(self.executable, self.runner).source(document)

    def render(self, document: DocumentIR, output: Path, *, timeout: int = 180) -> CompileResult:
        source_directory = output.with_suffix(".tex").parent
        source_directory.mkdir(parents=True, exist_ok=True)
        staged = _stage_figure_assets(document, source_directory)
        result = NativeLatexRenderer(self.executable, self.runner).render(
            staged,
            output,
            timeout=timeout,
        )
        warning_log = "\nDiagnostics: " + ", ".join(result.warnings) if result.warnings else ""
        return CompileResult(
            result.success,
            result.output,
            result.command,
            result.log + warning_log,
            result.error_code,
        )


class WordPdfQueue:
    _lock = Lock()

    def __init__(self, runner: Runner = default_runner) -> None:
        self.runner = runner

    def export(self, docx: Path, pdf: Path, *, timeout: int = 120) -> CompileResult:
        docx = docx.resolve()
        pdf = pdf.resolve()
        if not docx.is_file():
            raise FileNotFoundError(docx)
        if pdf.exists():
            return CompileResult(False, None, (), "Target PDF already exists", "OUTPUT_EXISTS")
        script = (
            "& { param([string]$inputPath,[string]$outputPath) "
            "$ErrorActionPreference='Stop';$w=New-Object -ComObject Word.Application;"
            "$w.Visible=$false;$w.DisplayAlerts=0;try{"
            "$d=$w.Documents.Open($inputPath);"
            "if($null -eq $d){throw 'Word returned null document'};"
            "if($null -ne $d.Fields){$d.Fields.Update()|Out-Null};"
            "$d.ExportAsFixedFormat($outputPath,17);$d.Close(0)}finally{$w.Quit()} }"
        )
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
            str(docx),
            str(pdf),
        ]
        with self._lock:
            completed = self.runner(command, docx.parent, timeout)
        success = completed.returncode == 0 and pdf.is_file()
        if not success:
            pdf.unlink(missing_ok=True)
        return CompileResult(
            success,
            pdf if success else None,
            tuple(command),
            completed.stdout + completed.stderr,
            None if success else "WORD_EXPORT_FAILED",
        )


def _font_and_size(typography: TypographySpec, kind: BlockKind) -> tuple[str | None, float | None]:
    if kind is BlockKind.HEADING:
        return (
            typography.heading_font or typography.body_font,
            typography.heading_size_pt or typography.body_size_pt,
        )
    if kind is BlockKind.TABLE:
        return (
            typography.table_font or typography.body_font,
            typography.table_size_pt or typography.body_size_pt,
        )
    if kind is BlockKind.CODE:
        return (
            typography.code_font or typography.body_font,
            typography.code_size_pt or typography.body_size_pt,
        )
    if kind is BlockKind.EQUATION:
        return (
            typography.equation_font or typography.body_font,
            typography.equation_size_pt or typography.body_size_pt,
        )
    return typography.body_font, typography.body_size_pt


def _typst_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("#", "\\#").replace("[", "\\[").replace("]", "\\]")


def _typst_text(value: str, typography: TypographySpec, kind: BlockKind) -> str:
    font, size = _font_and_size(typography, kind)
    arguments: list[str] = []
    if font:
        escaped_font = font.replace('"', '\\"')
        arguments.append(f'font: "{escaped_font}"')
    if size:
        arguments.append(f"size: {size:g}pt")
    return f"#text({', '.join(arguments)})[{value}]" if arguments else value


def _latex_styled(value: str, typography: TypographySpec, kind: BlockKind) -> str:
    font, size = _font_and_size(typography, kind)
    commands: list[str] = []
    if font:
        escaped = re.sub(r"([#$%&_{}])", r"\\\1", font)
        if kind is BlockKind.EQUATION and typography.equation_font:
            commands.append(f"\\mathversion{{{_math_version(typography.equation_font)}}}")
        else:
            commands.extend([f"\\fontspec{{{escaped}}}", f"\\CJKfontspec{{{escaped}}}"])
    if size:
        baseline = size * (typography.line_spacing or 1.2)
        commands.append(f"\\fontsize{{{size:g}}}{{{baseline:g}}}\\selectfont{{}}")
    if not commands:
        return value
    return "{" + "".join(commands) + value + "}"


def _math_version(font: str) -> str:
    return "pa" + hashlib.sha256(font.encode("utf-8")).hexdigest()[:12]


def _evidence_manifest(document: DocumentIR) -> list[dict[str, object]]:
    raw = document.metadata.get("evidence_manifest", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _reference_entries(document: DocumentIR) -> list[dict[str, object]]:
    """Group retrieval chunks by human source while retaining every evidence id."""

    grouped: dict[str, dict[str, object]] = {}
    for item in _evidence_manifest(document):
        raw_title = item.get("title")
        title = (
            raw_title.strip()
            if isinstance(raw_title, str) and raw_title.strip()
            else "Untitled source"
        )
        raw_source_uri = item.get("source_uri")
        source_uri = raw_source_uri.strip() if isinstance(raw_source_uri, str) else ""
        key = f"uri:{source_uri.casefold()}" if source_uri else f"title:{title.casefold()}"
        evidence_id = str(item.get("evidence_id", "")).strip()
        locator = item.get("locator")
        if key not in grouped:
            grouped[key] = {
                "number": len(grouped) + 1,
                "title": title,
                "source_uri": source_uri,
                "evidence_ids": [],
                "locators": [],
            }
        entry = grouped[key]
        evidence_ids = entry["evidence_ids"]
        locators = entry["locators"]
        if isinstance(evidence_ids, list) and evidence_id and evidence_id not in evidence_ids:
            evidence_ids.append(evidence_id)
        if isinstance(locators, list) and isinstance(locator, dict) and locator not in locators:
            locators.append(locator)
    return list(grouped.values())


def _citation_numbers(document: DocumentIR) -> dict[str, int]:
    numbers: dict[str, int] = {}
    for entry in _reference_entries(document):
        number = entry.get("number")
        evidence_ids = entry.get("evidence_ids")
        if not isinstance(number, int) or not isinstance(evidence_ids, list):
            continue
        numbers.update({str(evidence_id): number for evidence_id in evidence_ids})
    return numbers


def _block_citation_numbers(block: DocumentBlock, citation_numbers: dict[str, int]) -> list[int]:
    ordered: list[int] = []
    for citation in block.citations:
        number = citation_numbers.get(str(citation.evidence_id))
        if number is not None and number not in ordered:
            ordered.append(number)
    return ordered


def _stage_figure_assets(document: DocumentIR, output_directory: Path) -> DocumentIR:
    """Copy figures beside compiler sources so Typst/LaTeX remain portable and sandbox-safe."""

    staged = document.model_copy(deep=True)
    asset_directory = output_directory / "assets"
    for block in staged.iter_blocks():
        if block.kind is not BlockKind.FIGURE:
            continue
        raw_path = block.figure.path if block.figure else block.data.get("path", "")
        source = Path(str(raw_path or ""))
        if not source.is_file():
            continue
        asset_directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        destination = asset_directory / f"{digest}-{source.name}"
        if not destination.exists():
            shutil.copy2(source, destination)
        relative = destination.relative_to(output_directory).as_posix()
        block.data["path"] = relative
        if block.figure:
            block.figure.path = relative
    return staged
