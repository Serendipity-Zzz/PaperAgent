from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from paperagent.agents.document_ir import BlockKind, DocumentIR
from paperagent.rendering.renderers import CompileResult, Runner, default_runner

PROTECTED = re.compile(
    r"(```[\s\S]*?```|`[^`]+`|\$\$[\s\S]*?\$\$|\$[^$]+\$|\[[0-9, -]+\]|"
    r"10\.\d{4,9}/[-._;()/:A-Z0-9]+|\b\d+(?:\.\d+)?\s*(?:%|kg|g|ms|s|GB|MB|°C)?)",
    re.IGNORECASE,
)


class GlossaryTerm(BaseModel):
    source: str
    target: str
    confirmed: bool = False


class TranslationReport(BaseModel):
    direction: str
    protected_tokens: int
    glossary_hits: int
    warnings: list[str] = Field(default_factory=list)


class TranslationAgent:
    def translate(
        self,
        document: DocumentIR,
        translator: Callable[[str], str],
        *,
        direction: str,
        glossary: list[GlossaryTerm] | None = None,
        bilingual: bool = False,
    ) -> tuple[DocumentIR, TranslationReport]:
        if direction not in {"zh_to_en", "en_to_zh"}:
            raise ValueError("only zh_to_en and en_to_zh are supported")
        payload = document.model_dump(mode="json")
        protected_count = 0
        glossary_hits = 0

        def convert(text: str) -> str:
            nonlocal protected_count, glossary_hits
            replacements: dict[str, str] = {}

            def hold(match: re.Match[str]) -> str:
                placeholder = f"__PA_TOKEN_{len(replacements)}__"
                replacements[placeholder] = match.group(0)
                return placeholder

            guarded = PROTECTED.sub(hold, text)
            protected_count += len(replacements)
            for term in glossary or []:
                if term.confirmed and term.source in guarded:
                    placeholder = f"__PA_TERM_{glossary_hits}__"
                    guarded = guarded.replace(term.source, placeholder)
                    replacements[placeholder] = term.target
                    glossary_hits += 1
            translated = translator(guarded)
            for placeholder, value in replacements.items():
                translated = translated.replace(placeholder, value)
            if "__PA_" in translated:
                raise ValueError("translator changed or dropped protected placeholders")
            return f"{text}\n\n{translated}" if bilingual else translated

        payload["title"] = convert(str(payload["title"]))
        for section in payload["sections"]:
            section["title"] = convert(str(section["title"]))
            for block in section["blocks"]:
                if block["kind"] not in {BlockKind.CODE, BlockKind.EQUATION}:
                    block["text"] = convert(str(block["text"]))
                    if block.get("caption"):
                        block["caption"] = convert(str(block["caption"]))
        payload["language"] = "mixed" if bilingual else ("en" if direction == "zh_to_en" else "zh")
        payload["revision"] = document.revision + 1
        return DocumentIR.model_validate(payload), TranslationReport(
            direction=direction,
            protected_tokens=protected_count,
            glossary_hits=glossary_hits,
        )


class PdfMathTranslateAdapter:
    def __init__(
        self,
        executable: str | None = None,
        runner: Runner = default_runner,
        environment: Path | None = None,
    ) -> None:
        self.executable = executable or shutil.which("pdf2zh")
        self.runner = runner
        self.environment = environment

    def version(self) -> str | None:
        if not self.executable:
            return None
        result = self.runner([self.executable, "--version"], Path.cwd(), 15)
        return (result.stdout or result.stderr).strip() if result.returncode == 0 else None

    def translate(
        self,
        source: Path,
        output_dir: Path,
        *,
        language: str,
        api_configured: bool,
        cancelled: Callable[[], bool] | None = None,
    ) -> CompileResult:
        if not self.executable:
            return CompileResult(
                False, None, (), "PDFMathTranslate-next is not installed", "PDF2ZH_MISSING"
            )
        if not api_configured:
            return CompileResult(
                False, None, (), "Translation provider key is missing", "PDF2ZH_NO_KEY"
            )
        if cancelled and cancelled():
            return CompileResult(False, None, (), "Cancelled before launch", "CANCELLED")
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.executable,
            str(source),
            "--lang-out",
            language,
            "--output",
            str(output_dir),
        ]
        result = self.runner(command, self.environment or output_dir, 1800)
        candidates = sorted(output_dir.glob("*.pdf"), key=lambda item: item.stat().st_mtime)
        success = result.returncode == 0 and bool(candidates)
        return CompileResult(
            success,
            candidates[-1] if success else None,
            tuple(command),
            result.stdout + result.stderr,
            None if success else "PDF2ZH_FAILED",
        )
