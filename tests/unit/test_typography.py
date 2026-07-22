from pathlib import Path
from uuid import uuid4

from docx import Document

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.rendering import DocxRenderer, LatexRenderer, TypstRenderer
from paperagent.schemas.typography import TypographySpec, extract_typography


def styled_document() -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="字体测试",
        language="mixed",
        typography=TypographySpec(
            body_font="Times New Roman",
            heading_font="黑体",
            code_font="Consolas",
            body_size_pt=12,
            heading_size_pt=16,
            line_spacing=1.5,
            first_line_indent_chars=2,
        ),
        sections=[
            DocumentSection(
                title="标题",
                goal="验证样式",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="正文 content",
                        provenance=Provenance(agent="test", author_viewpoint=True),
                    )
                ],
            )
        ],
    )


def test_flexible_typography_extraction_supports_named_and_chinese_sizes() -> None:
    typography, matched = extract_typography(
        "正文使用 Times New Roman 字体, 字号12pt; 标题字体为黑体, "
        "标题三号, 行距1.5倍, 首行缩进2字符"
    )

    assert typography.body_font == "Times New Roman"
    assert typography.heading_font == "黑体"
    assert typography.body_size_pt == 12
    assert typography.heading_size_pt == 16
    assert typography.line_spacing == 1.5
    assert typography.first_line_indent_chars == 2
    assert "body_font" in matched


def test_typography_extraction_understands_revision_verbs() -> None:
    typography, matched = extract_typography("将报告正文改为宋体,保留其他内容")
    assert matched == {"body_font"}
    assert typography.body_font == "宋体"
    assert "body_font" in matched


def test_layout_instruction_is_not_misclassified_as_a_font() -> None:
    typography, matched = extract_typography(
        "文档默认 A4, 正文不要每节另起一页, 两个章节之间正常空一行。"
    )

    assert typography.body_font is None
    assert typography.heading_font is None
    assert "body_font" not in matched

    cover, cover_matched = extract_typography('封面标题为"驻波实验报告"')
    assert cover.heading_font is None
    assert "heading_font" not in cover_matched


def test_restyle_changes_only_style_revision_and_renderers_consume_it(tmp_path: Path) -> None:
    original = styled_document()
    restyled = original.restyle(
        original.typography.model_copy(update={"body_font": "宋体", "body_size_pt": 10.5})
    )
    output = DocxRenderer().render(restyled, tmp_path / "styled.docx")
    rendered = Document(output)

    assert restyled.revision == original.revision + 1
    assert restyled.sections == original.sections
    assert rendered.styles["Normal"].font.name == "宋体"
    assert rendered.styles["Normal"].font.size.pt == 10.5
    assert 'font: ("宋体"' in TypstRenderer().source(restyled)
    assert "\\setmainfont{宋体}" in LatexRenderer().source(restyled)
