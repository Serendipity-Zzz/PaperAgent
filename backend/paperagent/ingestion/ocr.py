from __future__ import annotations

import shutil
from dataclasses import dataclass
from uuid import UUID

from paperagent.ingestion.schemas import Chunk, CitationPolicy, Locator, TrustLevel


@dataclass(frozen=True)
class OcrCapability:
    available: bool
    executable: str | None
    reason: str


@dataclass(frozen=True)
class TextQuality:
    character_count: int
    printable_ratio: float
    replacement_ratio: float
    requires_ocr: bool


def detect_ocr() -> OcrCapability:
    executable = shutil.which("tesseract")
    return OcrCapability(
        available=bool(executable),
        executable=executable,
        reason="tesseract detected" if executable else "No supported local OCR executable found",
    )


def assess_text_quality(text: str, *, minimum_characters: int = 40) -> TextQuality:
    total = len(text)
    printable = sum(character.isprintable() or character.isspace() for character in text)
    replacements = text.count("�")
    printable_ratio = printable / total if total else 0.0
    replacement_ratio = replacements / total if total else 0.0
    return TextQuality(
        character_count=total,
        printable_ratio=printable_ratio,
        replacement_ratio=replacement_ratio,
        requires_ocr=total < minimum_characters
        or printable_ratio < 0.85
        or replacement_ratio > 0.02,
    )


def generated_ocr_chunk(
    *,
    source_id: UUID,
    page: int,
    text: str,
    confidence: float,
    bbox: tuple[float, float, float, float],
) -> Chunk:
    return Chunk(
        source_id=source_id,
        text=text,
        kind="ocr_text",
        locator=Locator(page=page, bbox=bbox),
        trust=TrustLevel.GENERATED,
        citation_policy=CitationPolicy.VERIFY_FIRST,
        instruction_trust=False,
        metadata={"generated_extraction": True, "ocr_confidence": confidence},
    )
