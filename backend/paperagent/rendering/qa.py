from __future__ import annotations

from pathlib import Path

import fitz
from pydantic import BaseModel, Field
from pypdf import PdfReader


class PdfPageIssue(BaseModel):
    page: int
    code: str
    severity: str
    message: str
    bbox: tuple[float, float, float, float] | None = None


class PdfQaReport(BaseModel):
    page_count: int
    rendered_pages: list[str]
    text_blocks: int
    font_count: int = 0
    image_count: int = 0
    link_count: int = 0
    outline_count: int = 0
    metadata: dict[str, str] = Field(default_factory=dict)
    issues: list[PdfPageIssue]
    passed: bool


class PdfQualityAssurance:
    def inspect(self, pdf: Path, output_dir: Path) -> PdfQaReport:
        reader = PdfReader(pdf)
        if reader.is_encrypted:
            return PdfQaReport(
                page_count=0,
                rendered_pages=[],
                text_blocks=0,
                issues=[
                    PdfPageIssue(
                        page=1,
                        code="encrypted",
                        severity="blocking",
                        message="PDF requires a password",
                    )
                ],
                passed=False,
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        document = fitz.open(pdf)
        issues: list[PdfPageIssue] = []
        rendered: list[str] = []
        block_count = 0
        fonts: set[int] = set()
        images: set[int] = set()
        link_count = 0
        for index, page in enumerate(document, start=1):
            image = output_dir / f"page-{index:04d}.png"
            page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).save(image)
            rendered.append(str(image))
            blocks = page.get_text("blocks")
            block_count += len(blocks)
            fonts.update(int(item[0]) for item in page.get_fonts(full=True))
            images.update(int(item[0]) for item in page.get_images(full=True))
            link_count += len(page.get_links())
            if not blocks:
                issues.append(
                    PdfPageIssue(
                        page=index,
                        code="blank_or_scan",
                        severity="warning",
                        message="页面没有可提取文本",
                    )
                )
            for block in blocks:
                x0, y0, x1, y1 = (float(value) for value in block[:4])
                if x0 < -1 or y0 < -1 or x1 > page.rect.width + 1 or y1 > page.rect.height + 1:
                    issues.append(
                        PdfPageIssue(
                            page=index,
                            code="overflow",
                            severity="error",
                            message="文本块超出页面边界",
                            bbox=(x0, y0, x1, y1),
                        )
                    )
                if y1 >= page.rect.height - 2:
                    issues.append(
                        PdfPageIssue(
                            page=index,
                            code="possible_cutoff",
                            severity="warning",
                            message="文本接近页面底边, 需检查截断",
                            bbox=(x0, y0, x1, y1),
                        )
                    )
        document.close()
        return PdfQaReport(
            page_count=len(reader.pages),
            rendered_pages=rendered,
            text_blocks=block_count,
            font_count=len(fonts),
            image_count=len(images),
            link_count=link_count,
            outline_count=len(document.get_toc())
            if not document.is_closed
            else len(reader.outline),
            metadata={key: str(value) for key, value in (reader.metadata or {}).items()},
            issues=issues,
            passed=not any(issue.severity in {"error", "blocking"} for issue in issues),
        )
