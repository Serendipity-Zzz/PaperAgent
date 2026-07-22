from __future__ import annotations

from pathlib import Path

import pytest

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.execution.tool_suite import (
    ExecutionToolSuite,
    PythonSourceGuard,
    UnsafeSourceError,
)
from paperagent.schemas.typography import TypographySpec


def make_suite(tmp_path: Path) -> ExecutionToolSuite:
    return ExecutionToolSuite(
        data_root=tmp_path / "data",
        project_root=tmp_path / "project",
        run_id="run-001",
        uv_path=None,
    )


def sample_document(content: str = "这是由 PaperAgent 渲染的可交付文档。") -> DocumentIR:
    from uuid import uuid4

    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="驻波实验报告",
        language="zh",
        sections=[
            DocumentSection(
                title="正文",
                goal="交付",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH, text=content, provenance=Provenance(agent="test")
                    )
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\nPath('result.txt').unlink()",
        "import os\nos.remove('result.txt')",
        "import subprocess\nsubprocess.run(['python', 'other.py'])",
    ],
)
def test_python_source_guard_rejects_deletion_and_nested_processes(source: str) -> None:
    with pytest.raises(UnsafeSourceError):
        PythonSourceGuard.validate(source)


def test_execution_suite_materializes_runs_and_collects_real_artifacts(tmp_path: Path) -> None:
    suite = make_suite(tmp_path)
    try:
        source = (
            "from pathlib import Path\n"
            "Path('curve.csv').write_text('x,y\\n0,0\\n1,1\\n', encoding='utf-8')\n"
            "print('experiment complete')\n"
        )
        materialized = suite.code_materialize({"filename": "experiment.py", "content": source})
        assert isinstance(materialized, dict)
        assert materialized["relation"] == "source"

        result = suite.process_execute({"argv": ["python", "experiment.py"], "timeout_seconds": 30})
        assert isinstance(result, dict)
        assert result["status"] == "completed"
        assert "experiment complete" in str(result["stdout"])

        collected = suite.result_collect({})
        assert isinstance(collected, dict)
        artifacts = collected["artifacts"]
        assert isinstance(artifacts, list)
        assert {item["name"] for item in artifacts} == {
            "curve.csv",
            "experiment.py",
            "stdout.log",
            "stderr.log",
        }
        assert all(len(str(item["sha256"])) == 64 for item in artifacts)
    finally:
        suite.close()


def test_process_execute_accepts_materialized_project_relative_path(tmp_path: Path) -> None:
    suite = make_suite(tmp_path)
    try:
        materialized = suite.code_materialize(
            {
                "filename": "managed.py",
                "content": "from pathlib import Path\nPath('done.txt').write_text('ok')\n",
            }
        )
        assert isinstance(materialized, dict)

        result = suite.process_execute(
            {"argv": ["python", str(materialized["relative_path"])]}
        )

        assert isinstance(result, dict)
        assert result["status"] == "completed"
        assert (suite.run_root / "done.txt").read_text() == "ok"
    finally:
        suite.close()


def test_execution_suite_renders_real_markdown_and_docx(tmp_path: Path) -> None:
    suite = make_suite(tmp_path)
    try:
        document = sample_document()
        suite.document_pipeline.store.save(document)
        for format_name in ("md", "docx"):
            rendered = suite.document_render(
                {
                    "document_id": str(document.document_id),
                    "revision": document.revision,
                    "format": format_name,
                    "filename": f"standing-wave.{format_name}",
                }
            )
            assert isinstance(rendered, dict)
            target = suite.project_root / str(rendered["relative_path"])
            assert target.is_file()
            assert target.stat().st_size > 0
    finally:
        suite.close()


def test_typography_only_render_preserves_verified_source_content(tmp_path: Path) -> None:
    source = "这是不得改写的原始正文。"
    suite = ExecutionToolSuite(
        data_root=tmp_path / "data",
        project_root=tmp_path / "project",
        run_id="run-font",
        uv_path=None,
        requested_typography=TypographySpec(body_font="宋体"),
    )
    try:
        document = sample_document(source).model_copy(update={"title": "原始报告"})
        document = document.restyle(TypographySpec(body_font="宋体"))
        suite.document_pipeline.store.save(document)
        rendered = suite.document_render(
            {
                "document_id": str(document.document_id),
                "revision": document.revision,
                "format": "md",
                "filename": "restyled.md",
            }
        )
        assert isinstance(rendered, dict)
        target = suite.project_root / str(rendered["relative_path"])
        output = target.read_text(encoding="utf-8")
        assert "不得改写的原始正文" in output
        assert "模型试图改写" not in output
        with pytest.raises(ValueError, match="raw content"):
            suite.document_render(
                {"title": "伪造", "content": "模型试图改写的内容", "format": "md"}
            )
    finally:
        suite.close()


def test_execution_suite_blocks_writes_outside_run_workspace(tmp_path: Path) -> None:
    suite = make_suite(tmp_path)
    outside = tmp_path / "outside.txt"
    try:
        suite.code_materialize(
            {
                "filename": "escape.py",
                "content": (
                    "from pathlib import Path\n"
                    f"Path({str(outside)!r}).write_text('blocked', encoding='utf-8')\n"
                ),
            }
        )
        result = suite.process_execute({"argv": ["python", "escape.py"]})
        assert isinstance(result, dict)
        assert result["status"] == "failed"
        assert "outside the managed run workspace" in str(result["stderr"])
        assert not outside.exists()
    finally:
        suite.close()


def test_execution_suite_declares_tempdir_without_delete_probes(tmp_path: Path) -> None:
    suite = make_suite(tmp_path)
    try:
        suite.code_materialize(
            {
                "filename": "tempdir.py",
                "content": (
                    "import tempfile\n"
                    "from pathlib import Path\n"
                    "Path('tempdir.txt').write_text(tempfile.gettempdir(), encoding='utf-8')\n"
                ),
            }
        )
        result = suite.process_execute({"argv": ["python", "tempdir.py"]})
        assert isinstance(result, dict)
        assert result["status"] == "completed"
        assert (suite.run_root / "tempdir.txt").read_text(encoding="utf-8") == str(
            suite.run_root / ".scratch"
        )
        unexpected = [
            item.name
            for item in suite.run_root.iterdir()
            if item.is_file()
            and item.name not in {"tempdir.py", "tempdir.txt", "stdout.log", "stderr.log"}
        ]
        assert unexpected == []
    finally:
        suite.close()
