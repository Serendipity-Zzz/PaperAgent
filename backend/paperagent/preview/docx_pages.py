from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from paperagent.rendering.pdf_modes import WordParityAdapter


@dataclass(frozen=True)
class DocxPreviewResult:
    success: bool
    path: Path | None
    engine: str
    cache_key: str
    error_code: str | None = None
    message: str = ""


class DocxPagePreviewService:
    """Cache page-faithful DOCX previews by source hash and converter version."""

    VERSION = "word-parity-v1"

    def __init__(self, project_root: Path, adapter: WordParityAdapter | None = None) -> None:
        self.project_root = project_root.resolve()
        self.adapter = adapter or WordParityAdapter()
        self.cache_root = self.project_root / ".paperagent" / "preview" / "docx-pdf"

    def convert(self, source: Path, source_hash: str) -> DocxPreviewResult:
        source = source.resolve()
        if source.suffix.casefold() != ".docx" or not source.is_file():
            raise ValueError("DOCX page preview requires an existing .docx source")
        cache_key = hashlib.sha256(f"{source_hash}:{self.VERSION}".encode()).hexdigest()
        target = self.cache_root / f"{cache_key}.pdf"
        if target.is_file() and target.stat().st_size:
            return DocxPreviewResult(True, target, "cached", cache_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        result, engine = self.adapter.export(source, target)
        if not result.success or result.output is None:
            target.unlink(missing_ok=True)
            return DocxPreviewResult(
                False,
                None,
                engine,
                cache_key,
                result.error_code or "DOCX_PREVIEW_CONVERSION_FAILED",
                result.log[-1000:],
            )
        return DocxPreviewResult(True, target, engine, cache_key)
