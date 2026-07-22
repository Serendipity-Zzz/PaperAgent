from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from markdown_it import MarkdownIt

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    FigureSpec,
    InlineKind,
    InlineNode,
    ListItem,
    ListKind,
    ListSpec,
    Provenance,
)
from paperagent.rendering.markdown_parser import parse_markdown_blocks, parse_markdown_sections
from paperagent.rendering.renderers import DocxRenderer, LatexRenderer, MarkdownRenderer


def _document(image: Path) -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Portable 报告",
        language="mixed",
        sections=[
            DocumentSection(
                title="结果",
                goal="verify portable output",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="ignored fallback",
                        inlines=[
                            InlineNode(kind=InlineKind.TEXT, text="A "),
                            InlineNode(kind=InlineKind.STRONG, text="structured"),
                            InlineNode(kind=InlineKind.TEXT, text=" paragraph."),
                        ],
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.LIST,
                        list_spec=ListSpec(
                            kind=ListKind.ORDERED,
                            items=[
                                ListItem(text="first", children=[ListItem(text="nested")]),
                                ListItem(text="second"),
                            ],
                        ),
                        provenance=Provenance(agent="test"),
                    ),
                    DocumentBlock(
                        kind=BlockKind.FIGURE,
                        caption="Figure 1",
                        figure=FigureSpec(path=str(image), alt_text="result"),
                        provenance=Provenance(agent="test"),
                    ),
                ],
            )
        ],
    )


def test_markdown_is_portable_semantic_utf8_and_uses_relative_assets(tmp_path: Path) -> None:
    image = tmp_path / "source image.png"
    image.write_bytes(b"valid-fixture-payload")
    output = MarkdownRenderer().render(_document(image), tmp_path / "delivery" / "report.md")
    source = output.read_text("utf-8")

    assert source.startswith("# Portable 报告\n")
    assert not source.startswith("---\n")
    assert "**structured**" in source
    assert "1. first\n    - nested\n2. second" in source
    assert str(tmp_path) not in source
    assert "](assets/source-image-" in source
    assert list((output.parent / "assets").glob("source-image-*.png"))
    assert MarkdownIt("commonmark").enable("table").parse(source)


def test_markdown_bundle_contains_report_and_assets(tmp_path: Path) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"valid-fixture-payload")
    bundle = MarkdownRenderer().render_bundle(_document(image), tmp_path / "paper.zip")
    with ZipFile(bundle) as archive:
        names = archive.namelist()
        assert "report.md" in names
        assert any(name.startswith("assets/") and name.endswith(".png") for name in names)


def test_standard_front_matter_is_explicit_and_does_not_contain_private_state(
    tmp_path: Path,
) -> None:
    image = tmp_path / "figure.png"
    image.write_bytes(b"valid-fixture-payload")
    output = MarkdownRenderer().render(
        _document(image),
        tmp_path / "report.md",
        include_front_matter=True,
    )
    source = output.read_text("utf-8")
    assert source.startswith('---\ntitle: "Portable 报告"\nlanguage: "mixed"\n---\n')
    assert "typography_overrides" not in source


def test_markdown_parser_extracts_image_after_leading_paragraph_text() -> None:
    blocks = parse_markdown_blocks(
        "实验结果如下。\n![驻波实验图](standing_wave.png)",
        agent="test",
    )

    assert [block.kind for block in blocks] == [BlockKind.PARAGRAPH, BlockKind.FIGURE]
    assert blocks[0].text == "实验结果如下。"
    assert blocks[1].caption == "驻波实验图"
    assert blocks[1].figure is not None
    assert blocks[1].figure.path == "standing_wave.png"


def test_markdown_sections_remove_duplicate_title_and_author_numbering() -> None:
    sections = parse_markdown_sections(
        "# 驻波实验报告\n\n## 1. 实验目的\n\n正文\n\n### 1.1 测量目标\n\n细节",
        title="驻波实验报告",
        agent="test",
    )

    assert [section.title for section in sections] == ["实验目的"]
    assert [child.title for child in sections[0].children] == ["测量目标"]
    assert sections[0].blocks[0].text == "正文"


def test_tex_parenthesis_delimiters_survive_all_document_renderers(tmp_path: Path) -> None:
    source = r"""# 公式兼容

波长为 \(\lambda_n = 2L/n\), 频率为 \(f_n = n f_1\)。

推导结果如下:
\[
f_n = \frac{n}{2L}\sqrt{\frac{T}{\mu}}
\]
该结果用于后续分析。

1. 根据 \(f \propto \sqrt{T}/L\) 验证关系。
2. 代码示例 `\(not_math\)` 不应转换。
"""
    sections = parse_markdown_sections(source, title="公式测试", agent="test")
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="公式测试",
        language="zh",
        sections=sections,
    )

    markdown_path = MarkdownRenderer().render(document, tmp_path / "formula.md")
    markdown = markdown_path.read_text("utf-8")
    assert r"$\lambda_n = 2L/n$" in markdown
    assert "$$\nf_n = \\frac{n}{2L}\\sqrt{\\frac{T}{\\mu}}\n$$" in markdown
    assert r"`\(not_math\)`" in markdown

    latex = LatexRenderer().source(document)
    assert r"\(\lambda_n = 2L/n\)" in latex
    assert r"\[f_n = \frac{n}{2L}\sqrt{\frac{T}{\mu}}\]" in latex.replace("\n", "")
    assert r"f_n = \frac{n}{2L}\sqrt{\frac{T}{\mu}}" in latex
    assert r"\textbackslash{}lambda" not in latex
    assert r"\textbackslash{}frac" not in latex
    assert r"\texttt{\textbackslash{}(not\_math\textbackslash{})}" in latex

    docx_path = DocxRenderer().render(document, tmp_path / "formula.docx")
    with ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert document_xml.count("<m:oMath") >= 4
    assert "lambda_n" not in document_xml


def test_tex_math_canonicalization_does_not_touch_fenced_code() -> None:
    blocks = parse_markdown_blocks(
        "正文 \\(x^2\\)。\n\n```tex\n\\(not_math\\)\n```",
        agent="test",
    )

    assert blocks[0].text == "正文 $x^2$。"
    assert blocks[1].kind is BlockKind.CODE
    assert blocks[1].text == r"\(not_math\)"
