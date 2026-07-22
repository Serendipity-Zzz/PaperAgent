from pathlib import Path
from uuid import uuid4

import fitz
from docx import Document
from markdown_it import MarkdownIt

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.rendering.qa import PdfQualityAssurance
from paperagent.rendering.renderers import DocxRenderer, MarkdownRenderer


def corpus(document_type: str, language: str, text: str) -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title=f"CC0 PaperAgent {document_type}",
        language=language,
        metadata={"license": "CC0-1.0", "corpus": document_type},
        sections=[
            DocumentSection(
                title="正文" if language != "en" else "Body",
                goal="Golden corpus validation",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=text,
                        provenance=Provenance(agent="golden", author_viewpoint=True),
                    )
                ],
            )
        ],
    )


def test_academic_experiment_practice_zh_en_mixed_golden_outputs(tmp_path: Path) -> None:
    samples = [
        corpus("academic-paper", "zh", "这是可重复生成的中文学术样例。"),
        corpus("experiment-report", "en", "This reproducible experiment has no external claims."),
        corpus("practice-report", "mixed", "实践步骤 are explicitly recorded."),
    ]
    for sample in samples:
        stem = str(sample.metadata["corpus"])
        markdown = MarkdownRenderer().render(sample, tmp_path / f"{stem}.md")
        docx = DocxRenderer().render(sample, tmp_path / f"{stem}.docx")
        assert MarkdownIt().parse(markdown.read_text("utf-8"))
        reopened = Document(str(docx))
        assert sample.title in [paragraph.text for paragraph in reopened.paragraphs]
        pdf = tmp_path / f"{stem}.pdf"
        generated = fitz.open()
        page = generated.new_page()
        page.insert_text((72, 72), f"Golden {stem}")
        generated.save(pdf)
        generated.close()
        qa = PdfQualityAssurance().inspect(pdf, tmp_path / f"{stem}-pages")
        assert qa.passed and qa.page_count == 1
