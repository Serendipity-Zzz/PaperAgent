from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import fitz
from PIL import Image, ImageChops
from pydantic import BaseModel, Field

from paperagent.agents.document_ir import DocumentIR, diff_documents


class RenderInvalidation(BaseModel):
    document_id: UUID
    from_revision: int
    to_revision: int
    formats: list[str]
    affected_section_ids: list[UUID] = Field(default_factory=list)
    affected_block_ids: list[UUID] = Field(default_factory=list)
    content_changed: bool
    rerun_retrieval: bool = False
    rerun_experiments: bool = False
    regenerate_text: bool = False


class RenderDependencyTracker:
    """Compute the smallest logical render scope for a Document IR revision."""

    SUPPORTED_FORMATS = ("md", "docx", "typst", "latex", "pdf")

    def plan(
        self,
        before: DocumentIR,
        after: DocumentIR,
        *,
        available_formats: list[str] | None = None,
    ) -> RenderInvalidation:
        if before.document_id != after.document_id:
            raise ValueError("render dependency comparison requires the same document")
        diff = diff_documents(before, after)
        content_changed = self._content_signature(before) != self._content_signature(after)
        changed_sections = [
            section.section_id
            for section in after.sections
            if self._section_changed(before, after, section.section_id)
            or any(block.block_id in set(diff.changed_blocks) for block in section.blocks)
        ]
        requested = available_formats or list(self.SUPPORTED_FORMATS)
        formats = [item for item in requested if item in self.SUPPORTED_FORMATS]
        return RenderInvalidation(
            document_id=after.document_id,
            from_revision=before.revision,
            to_revision=after.revision,
            formats=list(dict.fromkeys(formats)),
            affected_section_ids=changed_sections,
            affected_block_ids=diff.changed_blocks,
            content_changed=content_changed,
            rerun_retrieval=content_changed,
            regenerate_text=content_changed,
        )

    @staticmethod
    def _content_signature(document: DocumentIR) -> str:
        return json.dumps(
            {
                "title": document.title,
                "language": document.language,
                "sections": [section.model_dump(mode="json") for section in document.sections],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _section_changed(before: DocumentIR, after: DocumentIR, section_id: UUID) -> bool:
        old = next((item for item in before.sections if item.section_id == section_id), None)
        new = next((item for item in after.sections if item.section_id == section_id), None)
        if old is None or new is None:
            return True
        return before.resolve_typography(section_id=section_id) != after.resolve_typography(
            section_id=section_id
        )


class VisualPageDiff(BaseModel):
    page: int = Field(ge=1)
    changed_pixel_ratio: float = Field(ge=0, le=1)
    bbox: tuple[int, int, int, int] | None = None
    before_image: str | None = None
    after_image: str | None = None
    diff_image: str | None = None


class VisualDiffReport(BaseModel):
    before_path: str
    after_path: str
    page_count_before: int
    page_count_after: int
    changed_pages: list[int]
    pages: list[VisualPageDiff]


class PdfVisualDiff:
    """Render both PDF revisions and persist an auditable per-page pixel diff."""

    def compare(self, before: Path, after: Path, output_dir: Path) -> VisualDiffReport:
        if not before.is_file() or not after.is_file():
            raise FileNotFoundError("both PDF revisions are required for visual diff")
        output_dir.mkdir(parents=True, exist_ok=True)
        old = fitz.open(before)
        new = fitz.open(after)
        old_count = old.page_count
        new_count = new.page_count
        pages: list[VisualPageDiff] = []
        try:
            total = max(old.page_count, new.page_count)
            for index in range(total):
                old_image = self._page(old, index)
                new_image = self._page(new, index)
                width = max(old_image.width, new_image.width)
                height = max(old_image.height, new_image.height)
                old_canvas = Image.new("RGB", (width, height), "white")
                new_canvas = Image.new("RGB", (width, height), "white")
                old_canvas.paste(old_image, (0, 0))
                new_canvas.paste(new_image, (0, 0))
                difference = ImageChops.difference(old_canvas, new_canvas)
                bbox = difference.getbbox()
                changed = 0
                if bbox is not None:
                    histogram = difference.convert("L").histogram()
                    changed = sum(histogram[9:])
                ratio = changed / (width * height) if width and height else 0
                before_image = output_dir / f"page-{index + 1:04d}-before.png"
                after_image = output_dir / f"page-{index + 1:04d}-after.png"
                diff_image = output_dir / f"page-{index + 1:04d}-diff.png"
                old_canvas.save(before_image)
                new_canvas.save(after_image)
                difference.save(diff_image)
                pages.append(
                    VisualPageDiff(
                        page=index + 1,
                        changed_pixel_ratio=ratio,
                        bbox=bbox,
                        before_image=str(before_image),
                        after_image=str(after_image),
                        diff_image=str(diff_image),
                    )
                )
        finally:
            old.close()
            new.close()
        report = VisualDiffReport(
            before_path=str(before),
            after_path=str(after),
            page_count_before=old_count,
            page_count_after=new_count,
            changed_pages=[item.page for item in pages if item.bbox is not None],
            pages=pages,
        )
        report_path = output_dir / "visual-diff.json"
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, report_path)
        return report

    @staticmethod
    def _page(document: fitz.Document, index: int) -> Image.Image:
        if index >= document.page_count:
            return Image.new("RGB", (1, 1), "white")
        pixmap = document[index].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
