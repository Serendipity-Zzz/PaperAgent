from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import fitz
from docx import Document

from paperagent.agents.change_intent import ChangeIntent, ChangeScope
from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.rendering import TargetedTypographyService


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    project = root / "docs" / "test-reports" / "artifacts" / f"typography-{run_id}"
    project.mkdir(parents=True, exist_ok=False)
    paragraph = DocumentBlock(
        kind=BlockKind.PARAGRAPH,
        text="PaperAgent 字体定向修改真实渲染证据。The content must stay unchanged.",
        provenance=Provenance(agent="live-render-test"),
    )
    untouched = DocumentBlock(
        kind=BlockKind.PARAGRAPH,
        text="这个段落不应进入局部变更集合。",
        provenance=Provenance(agent="live-render-test"),
    )
    original = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="字体渲染验收",
        language="mixed",
        sections=[
            DocumentSection(
                title="真实输出",
                goal="验证 PDF/DOCX 字体与视觉差异",
                blocks=[paragraph, untouched],
            )
        ],
    )
    service = TargetedTypographyService(project)
    first = service.apply(
        original,
        ChangeIntent(
            scope=ChangeScope.GLOBAL,
            typography_patch={"body_font": "SimSun", "body_size_pt": 11},
        ),
        formats=["md", "docx", "latex", "pdf"],
    )
    if "pdf" in first.render_errors:
        raise RuntimeError(f"first PDF render failed: {first.render_errors['pdf']}")
    second = service.apply(
        first.document,
        ChangeIntent(
            scope=ChangeScope.BLOCK,
            block_ids=[paragraph.block_id],
            typography_patch={"body_font": "SimSun", "body_size_pt": 16},
        ),
        formats=["md", "docx", "latex", "pdf"],
    )
    if "pdf" in second.render_errors:
        raise RuntimeError(f"second PDF render failed: {second.render_errors['pdf']}")
    if second.document.sections != original.sections:
        raise AssertionError("typography revision changed document content")
    if second.invalidation.affected_block_ids != [paragraph.block_id]:
        raise AssertionError("local invalidation escaped the selected block")
    if second.visual_diff is None or not second.visual_diff.changed_pages:
        raise AssertionError("visual diff did not detect the font-size revision")

    pdf_artifact = next(item for item in second.artifacts if item.format == "pdf")
    docx_artifact = next(item for item in second.artifacts if item.format == "docx")
    pdf_path = project / pdf_artifact.path
    docx_path = project / docx_artifact.path
    pdf = fitz.open(pdf_path)
    try:
        fonts = sorted(
            {
                str(font[3])
                for page in pdf
                for font in page.get_fonts(full=True)
                if len(font) > 3
            }
        )
    finally:
        pdf.close()
    word = Document(str(docx_path))
    target_run = next(
        paragraph.runs[0]
        for paragraph in word.paragraphs
        if "PaperAgent" in paragraph.text
    )
    report = {
        "status": "passed",
        "executed_at": datetime.now(UTC).isoformat(),
        "renderer": "TeX Live xelatex + python-docx",
        "document_id": str(second.document.document_id),
        "from_revision": first.document.revision,
        "to_revision": second.document.revision,
        "affected_block_ids": [str(item) for item in second.invalidation.affected_block_ids],
        "content_regenerated": second.invalidation.regenerate_text,
        "rerun_retrieval": second.invalidation.rerun_retrieval,
        "rerun_experiments": second.invalidation.rerun_experiments,
        "pdf_fonts": fonts,
        "docx_target_font": target_run.font.name,
        "docx_target_size_pt": target_run.font.size.pt if target_run.font.size else None,
        "changed_pages": second.visual_diff.changed_pages,
        "artifacts": [item.model_dump(mode="json") for item in second.artifacts],
        "visual_diff": second.visual_diff.model_dump(mode="json"),
    }
    report_path = root / "docs" / "test-reports" / "P5-R-live-render.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
