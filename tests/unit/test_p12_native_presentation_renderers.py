from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.presentation import expectation_from_presentation, presentation_from_requirement
from paperagent.rendering.latex_native import NativeLatexRenderer
from paperagent.rendering.preflight import RenderedArtifactValidator
from paperagent.rendering.presentation_view import RenderPresentationViewModel
from paperagent.rendering.renderers import DocxRenderer, MarkdownRenderer, default_runner
from paperagent.schemas.presentation import (
    RequirementCoverField,
    RequirementCoverSpec,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
)


def _document(*, field_count: int = 6) -> DocumentIR:
    document_id = uuid4()
    values = [
        ("author", "姓名", "张三"),
        ("student_id", "学号", "20260001"),
        ("class_name", "班级", "物理学拔尖人才培养实验一班"),
        ("institution", "学校", "某某大学物理与电子科学学院"),
        ("major", "专业", "物理学"),
        ("course", "课程", "大学物理实验"),
        ("advisor", "指导老师", "李老师"),
        ("custom.laboratory", "实验室", "合成测试实验室"),
        ("experiment_date", "实验日期", "2026-07-20"),
        ("custom.version", "版本", "最终验收版"),
        ("custom.group", "实验组", "第一组"),
        ("custom.location", "地点", "综合实验楼 301"),
    ][:field_count]
    presentation = presentation_from_requirement(
        RequirementPresentationSpec(
            cover=RequirementCoverSpec(
                enabled=True,
                title="驻波特性实验报告",
                subtitle="综合设计性实验",
                fields=[
                    RequirementCoverField(
                        semantic_key=key,
                        label=label,
                        value=value,
                        order=index * 10,
                    )
                    for index, (key, label, value) in enumerate(values, start=1)
                ],
            ),
            page_chrome=RequirementPageChromeSpec(
                header_center="大学物理实验报告",
                footer_left="某某大学",
                page_number=True,
                total_pages=True,
                hide_on_cover=True,
            ),
        ),
        document_id=document_id,
    )
    return DocumentIR(
        document_id=document_id,
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="驻波特性实验报告",
        language="zh",
        metadata={"toc": False, "archetype": "experiment-report"},
        presentation=presentation,
        sections=[
            DocumentSection(
                title="实验结果",
                goal="verify native presentation",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="驻波由两列频率相同、传播方向相反的波叠加形成。" * 24,
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )


def test_render_presentation_view_is_ordered_and_cross_format_stable() -> None:
    document = _document(field_count=12)
    view = RenderPresentationViewModel.from_document(document)

    assert view.cover.title == "驻波特性实验报告"
    assert len(view.cover.fields) == 12
    assert [item.order for item in view.cover.fields] == sorted(
        item.order for item in view.cover.fields
    )
    assert view.different_first_page
    assert view.semantic_snapshot()["cover"]["fields"][0]["semantic_key"] == "author"


@pytest.mark.parametrize("field_count", [1, 4, 12])
def test_docx_cover_and_page_chrome_are_native_and_valid(
    tmp_path: Path,
    field_count: int,
) -> None:
    document = _document(field_count=field_count)
    output = DocxRenderer().render(document, tmp_path / f"cover-{field_count}.docx")
    expectation = expectation_from_presentation(document.presentation)
    result = RenderedArtifactValidator().validate(
        output,
        format_name="docx",
        required_image_count=0,
        document_id=document.document_id,
        revision=document.revision,
        document=document,
        presentation_expectation=expectation,
    )

    assert result.passed, result.model_dump_json()
    with ZipFile(output) as archive:
        names = set(archive.namelist())
        body = archive.read("word/document.xml").decode("utf-8")
        header = "".join(
            archive.read(name).decode("utf-8")
            for name in names
            if name.startswith("word/header")
        )
        footer = "".join(
            archive.read(name).decode("utf-8")
            for name in names
            if name.startswith("word/footer")
        )
        assert "w:titlePg" in body
        assert body.count('w:type="page"') == 1
        assert body.count("w:cantSplit") >= field_count
        assert all(item.value in body for item in document.presentation.cover.fields)
        assert "大学物理实验报告" in header
        assert " PAGE " in footer and " NUMPAGES " in footer


def test_markdown_bundle_and_html_preview_preserve_presentation_semantics(
    tmp_path: Path,
) -> None:
    document = _document()
    renderer = MarkdownRenderer()
    markdown = renderer.render(document, tmp_path / "report.md").read_text("utf-8")
    bundle = renderer.render_bundle(document, tmp_path / "report.zip")
    preview = renderer.render_html_preview(document, tmp_path / "preview.html")

    assert markdown.startswith("# 驻波特性实验报告\n")
    assert "| 姓名 | 张三 |" in markdown
    assert "CommonMark does not emulate physical pages" in markdown
    with ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("presentation.json"))
        assert manifest["presentation"] == RenderPresentationViewModel.from_document(
            document
        ).semantic_snapshot()
        assert manifest["capabilities"]["repeating_page_chrome"] == "preview_only"
    html = preview.read_text("utf-8")
    assert "paper-cover" in html and "张三" in html and "210mm" in html


def test_xelatex_source_uses_titlepage_and_dynamic_page_chrome() -> None:
    source = NativeLatexRenderer(None, default_runner).source(_document(field_count=12))

    assert r"\begin{titlepage}" in source
    assert r"\maketitle" not in source
    assert "张三" in source and "综合设计性实验" in source
    assert r"\fancyhead[C]{大学物理实验报告}" in source
    assert r"\thepage" in source and r"\pageref*{LastPage}" in source
    assert r"\renewcommand{\headrulewidth}{0pt}" in source
    assert source.count(r"\end{titlepage}") == 1


def test_real_xelatex_pdf_has_cover_body_and_no_blank_page(tmp_path: Path) -> None:
    executable = shutil.which("xelatex")
    if not executable:
        pytest.skip("XeLaTeX is not installed")
    document = _document(field_count=12)
    result = NativeLatexRenderer(executable, default_runner).render(
        document,
        tmp_path / "presentation.pdf",
    )

    assert result.success, result.log[-2_000:]
    assert result.output is not None
    reader = PdfReader(result.output)
    assert len(reader.pages) >= 2
    pages = [page.extract_text() or "" for page in reader.pages]
    assert "张三" in pages[0]
    assert "大学物理实验报告" not in pages[0]
    assert "大学物理实验报告" in "\n".join(pages[1:])
    assert all(page.strip() for page in pages)
    validation = RenderedArtifactValidator().validate(
        result.output,
        format_name="pdf",
        required_image_count=0,
        document_id=document.document_id,
        revision=document.revision,
        document=document,
        presentation_expectation=expectation_from_presentation(document.presentation),
    )
    assert validation.passed, validation.model_dump_json()
