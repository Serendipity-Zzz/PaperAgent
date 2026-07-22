from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    InlineKind,
    InlineNode,
    ListItem,
    ListKind,
    ListSpec,
    Provenance,
    TableCell,
    TableRow,
    TableSpec,
)
from paperagent.rendering.latex_native import (
    classify_error,
    classify_warnings,
    template_for,
    validate_template_source,
)
from paperagent.rendering.pdf_modes import (
    DocumentPdfRenderer,
    PdfModeSelector,
    PdfRenderMode,
    WordParityAdapter,
)
from paperagent.rendering.renderers import CompileResult, LatexRenderer


def _document() -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="双模式 PDF 报告",
        language="mixed",
        metadata={"toc": True},
        sections=[
            DocumentSection(
                title="方法与结果",
                goal="verify semantic TeX",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        inlines=[
                            InlineNode(kind=InlineKind.TEXT, text="结构化 "),
                            InlineNode(kind=InlineKind.STRONG, text="正文"),
                            InlineNode(
                                kind=InlineKind.TEXT,
                                text=r", where $f_n=n\cdot f_1$.",
                            ),
                        ],
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.LIST,
                        list_spec=ListSpec(
                            kind=ListKind.ORDERED,
                            items=[ListItem(text="prepare"), ListItem(text="execute")],
                        ),
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.TABLE,
                        table=TableSpec(
                            rows=[
                                TableRow(
                                    cells=[
                                        TableCell(text="mode", header=True),
                                        TableCell(text=r"frequency $f_n$", header=True),
                                    ]
                                ),
                                TableRow(cells=[TableCell(text="1"), TableCell(text="60")]),
                            ]
                        ),
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.EQUATION,
                        text="f_n=nv/(2L)",
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.CODE,
                        text="print('result')",
                        data={"language": "Python"},
                        provenance=Provenance(agent="test"),
                    ),
                ],
            )
        ],
    )


def test_auto_mode_is_transparent_and_template_selects_word_parity(tmp_path: Path) -> None:
    selector = PdfModeSelector()
    default = selector.select(_document())
    parity = selector.select(_document(), template=tmp_path / "template.docx")
    explicit = selector.select(_document(), requested=PdfRenderMode.XELATEX)
    assert default.selected is PdfRenderMode.XELATEX
    assert default.reason == "default-independent-pdf"
    assert parity.selected is PdfRenderMode.WORD_PARITY
    assert parity.reason == "template-layout-parity"
    assert explicit.reason == "user-explicit-xelatex"


def test_xelatex_source_uses_semantic_environments_and_complete_page_contract() -> None:
    source = LatexRenderer(executable="xelatex").source(_document())
    assert "a4paper" in source and "\\usepackage{geometry}" in source
    assert "\\usepackage{fancyhdr}" in source and "\\thepage" in source
    assert "\\tableofcontents" in source
    assert "\\textbf{正文}" in source
    assert r"\(f_n=n\cdot f_1\)" in source
    assert r"frequency \(f_n\)" in source
    assert r"\$f\_n" not in source
    assert "\\begin{enumerate}" in source
    assert "\\begin{longtable}" in source
    assert "\\begin{equation}" in source
    assert "\\begin{lstlisting}" in source
    assert template_for(_document()).template_id == "paperagent-research-report"


def test_uploaded_latex_template_contract_rejects_execution_primitives() -> None:
    validate_template_source("\\documentclass{article}\n{{DOCUMENT_BODY}}")
    for unsafe in (
        "\\write18{calc.exe}\n{{DOCUMENT_BODY}}",
        "\\input|powershell\n{{DOCUMENT_BODY}}",
        "no placeholder",
    ):
        try:
            validate_template_source(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe template was accepted")


def test_latex_compile_repeats_and_classifies_diagnostics(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        calls.append(command)
        (cwd / "report.pdf").write_bytes(b"%PDF-real-contract")
        return subprocess.CompletedProcess(command, 0, "Overfull \\hbox", "")

    result = LatexRenderer(executable="xelatex", runner=runner).render(
        _document(),
        tmp_path / "report.pdf",
    )
    assert result.success and len(calls) == 3
    assert "LATEX_OVERFULL_BOX" in result.log
    assert classify_error("font Fira Math cannot be found") == "LATEX_FONT_MISSING"
    assert classify_error("File chart.png not found") == "LATEX_ASSET_MISSING"
    assert classify_error("File booktabs.sty not found") == "LATEX_PACKAGE_MISSING"
    assert classify_warnings("Overfull \\hbox and undefined references") == (
        "LATEX_UNDEFINED_REFERENCE",
        "LATEX_OVERFULL_BOX",
    )


def test_word_parity_mode_records_engine_and_source_docx(tmp_path: Path) -> None:
    class SuccessfulParity(WordParityAdapter):
        def export(
            self,
            docx: Path,
            pdf: Path,
            *,
            timeout: int = 180,
        ) -> tuple[CompileResult, str]:
            del timeout
            pdf.write_bytes(b"%PDF-word-parity")
            return CompileResult(True, pdf, (), "ok"), "word-com-test"

    result = DocumentPdfRenderer(word_parity=SuccessfulParity()).render(
        _document(),
        tmp_path / "parity.pdf",
        mode=PdfRenderMode.WORD_PARITY,
    )
    assert result.success
    assert result.engine == "word-com-test"
    assert result.source_document and result.source_document.is_file()
    assert result.decision.selected is PdfRenderMode.WORD_PARITY
