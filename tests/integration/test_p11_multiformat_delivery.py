from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from docx import Document
from PIL import Image
from pypdf import PdfReader

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.rendering.docx_native import NativeDocxRenderer
from paperagent.rendering.latex_native import NativeLatexRenderer
from paperagent.rendering.renderers import default_runner


def _suite(tmp_path: Path) -> tuple[ExecutionToolSuite, ArtifactService, Path]:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    root = databases.project_root(project_id)
    root.mkdir(parents=True)
    artifacts = ArtifactService(databases, project_id)
    return (
        ExecutionToolSuite(
            data_root=settings.resolved_data_dir,
            project_root=root,
            run_id="p11-five-figure",
            uv_path=None,
            artifact_service=artifacts,
            source_conversation_id="conversation-five-figure",
            source_message_id="message-five-figure",
        ),
        artifacts,
        root,
    )


@pytest.mark.skipif(
    not Path(r"E:\App\TexLive\texlive\2026\bin\windows\xelatex.exe").is_file(),
    reason="real XeLaTeX is an external first-run capability",
)
def test_same_revision_delivers_five_images_in_md_bundle_docx_and_pdf(
    tmp_path: Path,
) -> None:
    suite, artifacts, root = _suite(tmp_path)
    try:
        filenames: list[str] = []
        source_ids: list[str] = []
        for index in range(1, 6):
            filename = f"驻波 图 {index}.png"
            path = root / "runs" / suite.run_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (480, 270), color=(25 * index, 80, 180, 180)).save(path)
            artifact = artifacts.register(
                path,
                kind="figure",
                producer_tool="result.collect",
                run_id=suite.run_id,
            )
            filenames.append(filename)
            source_ids.append(artifact.id)
        markdown = (
            "# 实验结果\n\n```python\n# prepare coordinates\nx = [0, 1]\n```\n\n"
            + "\n\n".join(
                f"![驻波图 {index}]({filename})"
                for index, filename in enumerate(filenames, start=1)
            )
        )
        canonical = suite.document_pipeline.compose(
            {
                "title": "驻波五图实验报告",
                "content": markdown,
                "language": "zh",
                "image_required": True,
            }
        )
        assert isinstance(canonical, dict)
        document_id = str(canonical["document_id"])
        revision = int(str(canonical["revision"]))

        outputs = {
            format_name: suite.document_render(
                {
                    "document_id": document_id,
                    "revision": revision,
                    "format": format_name,
                    "filename": f"standing-wave.{extension}",
                    **({"pdf_mode": "xelatex"} if format_name == "pdf" else {}),
                }
            )
            for format_name, extension in (
                ("md_bundle", "zip"),
                ("docx", "docx"),
                ("pdf", "pdf"),
            )
        }
        paths = {
            key: root / str(value["relative_path"]) for key, value in outputs.items()
        }
        with ZipFile(paths["md_bundle"]) as archive:
            names = set(archive.namelist())
            report = archive.read("report.md").decode("utf-8")
            assert len([name for name in names if name.startswith("assets/")]) == 5
            assert len(re.findall(r"!\[[^\]]*\]\(assets/[^)]+\)", report)) == 5
        word = Document(paths["docx"])
        with ZipFile(paths["docx"]) as archive:
            media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert len(word.inline_shapes) == len(media) == 5
        pdf = PdfReader(paths["pdf"])
        assert sum(len(page.images) for page in pdf.pages) >= 5

        records = [artifacts.get(str(value["artifact_id"])) for value in outputs.values()]
        assert len({item.revision_id for item in records}) == 1
        for record in records:
            lineage = json.loads(record.lineage_json)
            assert set(lineage["figure_artifact_ids"]) == set(source_ids)
            assert record.delivery_status == "delivered"
        validation = suite.document_pipeline.validate_delivery(
            {
                "document_id": document_id,
                "revision": revision,
                "artifact_ids": [record.id for record in records],
            }
        )
        assert validation["passed"] is True
        assert validation["required_image_count"] == 5
    finally:
        suite.close()


def test_single_markdown_publishes_resolvable_relative_assets(tmp_path: Path) -> None:
    suite, artifacts, root = _suite(tmp_path)
    try:
        image = root / "runs" / suite.run_id / "figure with space.png"
        image.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 90), color="navy").save(image)
        artifacts.register(
            image,
            kind="figure",
            producer_tool="result.collect",
            run_id=suite.run_id,
        )
        canonical = suite.document_pipeline.compose(
            {
                "title": "Portable Markdown",
                "content": "# Result\n\n![Figure](figure with space.png)",
                "language": "en",
                "image_required": True,
            }
        )
        assert isinstance(canonical, dict)
        rendered = suite.document_render(
            {
                "document_id": str(canonical["document_id"]),
                "revision": int(str(canonical["revision"])),
                "format": "md",
                "filename": "portable.md",
            }
        )
        markdown_path = root / str(rendered["relative_path"])
        text = markdown_path.read_text(encoding="utf-8")
        links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
        assert len(links) == 1
        assert not Path(links[0]).is_absolute()
        assert (markdown_path.parent / links[0]).is_file()
        validated = suite.document_pipeline.validate_delivery(
            {
                "document_id": str(rendered["document_id"]),
                "revision": int(str(rendered["document_revision"])),
                "artifact_ids": [str(rendered["artifact_id"])],
            }
        )
        assert validated["passed"] is True
    finally:
        suite.close()


def test_ordinary_sections_do_not_force_page_breaks(tmp_path: Path) -> None:
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Continuous sections",
        language="en",
        metadata={"archetype": "meeting-minutes"},
        sections=[
            DocumentSection(
                title=f"Section {index}",
                goal="continuous layout",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text=f"Short body {index}.",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
            for index in range(1, 4)
        ],
    )
    latex = NativeLatexRenderer(None, default_runner).source(document)
    body = latex.split(r"\clearpage", 1)[-1]
    assert r"\section{Section 1}" in body
    assert r"\clearpage" not in body

    output = NativeDocxRenderer().render(document, tmp_path / "continuous.docx")
    with ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert xml.count('w:type="page"') == 1  # cover only
    assert "w:pageBreakBefore" not in xml
