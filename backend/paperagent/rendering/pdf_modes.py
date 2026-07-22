from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from paperagent.agents.document_ir import DocumentIR
from paperagent.rendering.renderers import (
    CompileResult,
    DocxRenderer,
    LatexRenderer,
    Runner,
    WordPdfQueue,
    default_runner,
)


class PdfRenderMode(StrEnum):
    AUTO = "auto"
    XELATEX = "xelatex"
    WORD_PARITY = "word_parity"


@dataclass(frozen=True)
class PdfModeDecision:
    requested: PdfRenderMode
    selected: PdfRenderMode
    reason: str
    fallback_allowed: bool = False


@dataclass(frozen=True)
class PdfProductionResult:
    decision: PdfModeDecision
    compile: CompileResult
    engine: str
    source_document: Path | None = None

    @property
    def success(self) -> bool:
        return self.compile.success

    @property
    def output(self) -> Path | None:
        return self.compile.output

    @property
    def error_code(self) -> str | None:
        return self.compile.error_code

    @property
    def log(self) -> str:
        return self.compile.log


class PdfModeSelector:
    def select(
        self,
        document: DocumentIR,
        *,
        requested: PdfRenderMode = PdfRenderMode.AUTO,
        template: Path | None = None,
    ) -> PdfModeDecision:
        if requested is PdfRenderMode.XELATEX:
            return PdfModeDecision(requested, requested, "user-explicit-xelatex")
        if requested is PdfRenderMode.WORD_PARITY:
            return PdfModeDecision(requested, requested, "user-explicit-word-parity")
        metadata_mode = document.metadata.get("pdf_render_mode")
        if metadata_mode in {PdfRenderMode.XELATEX.value, PdfRenderMode.WORD_PARITY.value}:
            selected = PdfRenderMode(str(metadata_mode))
            return PdfModeDecision(requested, selected, "document-metadata")
        if template is not None or bool(document.metadata.get("require_word_parity")):
            return PdfModeDecision(requested, PdfRenderMode.WORD_PARITY, "template-layout-parity")
        return PdfModeDecision(requested, PdfRenderMode.XELATEX, "default-independent-pdf")


class WordParityAdapter:
    def __init__(self, runner: Runner = default_runner) -> None:
        self.runner = runner

    def export(self, docx: Path, pdf: Path, *, timeout: int = 180) -> tuple[CompileResult, str]:
        if platform.system() == "Windows":
            word = WordPdfQueue(runner=self.runner).export(docx, pdf, timeout=timeout)
            if word.success:
                return word, "word-com"
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            return (
                CompileResult(
                    False,
                    None,
                    (),
                    "Word COM unavailable or failed and LibreOffice was not found",
                    "WORD_PARITY_ENGINE_MISSING",
                ),
                "unavailable",
            )
        command = [
            soffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(pdf.parent),
            str(docx),
        ]
        completed = self.runner(command, docx.parent, timeout)
        produced = pdf.parent / f"{docx.stem}.pdf"
        if completed.returncode == 0 and produced.is_file():
            if produced != pdf:
                shutil.copy2(produced, pdf)
            return (
                CompileResult(True, pdf, tuple(command), completed.stdout + completed.stderr),
                "libreoffice",
            )
        return (
            CompileResult(
                False,
                None,
                tuple(command),
                completed.stdout + completed.stderr,
                "LIBREOFFICE_EXPORT_FAILED",
            ),
            "libreoffice",
        )


class DocumentPdfRenderer:
    def __init__(
        self,
        *,
        latex: LatexRenderer | None = None,
        word_parity: WordParityAdapter | None = None,
        selector: PdfModeSelector | None = None,
    ) -> None:
        self.latex = latex or LatexRenderer()
        self.word_parity = word_parity or WordParityAdapter()
        self.selector = selector or PdfModeSelector()

    def render(
        self,
        document: DocumentIR,
        output: Path,
        *,
        mode: PdfRenderMode = PdfRenderMode.AUTO,
        template: Path | None = None,
        timeout: int = 180,
    ) -> PdfProductionResult:
        decision = self.selector.select(document, requested=mode, template=template)
        if decision.selected is PdfRenderMode.XELATEX:
            compile_result = self.latex.render(document, output, timeout=timeout)
            return PdfProductionResult(decision, compile_result, "xelatex")
        source = output.with_suffix(".docx")
        if source.exists():
            source = output.with_name(f"{output.stem}-word-source.docx")
        DocxRenderer().render(document, source, template=template)
        compile_result, engine = self.word_parity.export(source, output, timeout=timeout)
        return PdfProductionResult(decision, compile_result, engine, source)
