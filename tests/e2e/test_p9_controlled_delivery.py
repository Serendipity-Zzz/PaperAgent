from __future__ import annotations

import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from docx import Document
from pypdf import PdfReader
from sqlalchemy import select

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
from paperagent.db.models import ExecutionRecordRow
from paperagent.execution.tool_suite import ExecutionToolSuite


@pytest.mark.skipif(sys.platform != "win32", reason="P9 local execution target is Windows")
def test_real_uv_experiment_and_three_format_delivery(tmp_path: Path) -> None:
    uv = shutil.which("uv") or r"E:\App\uv\current\uv.exe"
    if not Path(uv).is_file():
        pytest.skip("uv is an external first-run dependency")
    if shutil.which("xelatex") is None:
        pytest.skip("TeX Live is an external first-run dependency")

    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    project_root = databases.project_root(project_id)
    project_root.mkdir(parents=True)
    databases.project_engine(project_id).dispose()
    artifacts = ArtifactService(databases, project_id)
    suite = ExecutionToolSuite(
        data_root=settings.resolved_data_dir,
        project_root=project_root,
        run_id="standing-wave-e2e",
        uv_path=Path(uv),
        artifact_service=artifacts,
    )
    try:
        environment_request = {
            "dependencies": ["six==1.17.0"],
            "python_version": "3.12",
        }
        first = suite.environment_prepare(environment_request)
        second = suite.environment_prepare(environment_request)
        assert isinstance(first, dict) and isinstance(second, dict)
        assert first["fingerprint"] == second["fingerprint"]

        source = "\n".join(
            [
                "from pathlib import Path",
                "import six",
                "csv = 'x,amplitude\\n0,0\\n0.25,1\\n0.5,0\\n'",
                "Path('standing-wave.csv').write_text(csv, encoding='utf-8')",
                "svg = (",
                '    \'<svg xmlns="http://www.w3.org/2000/svg" width="640" \'',
                '    \'height="320"><rect width="100%" height="100%" \'',
                '    \'fill="#181818"/><path d="M0 160 Q80 20 160 160 \'',
                '    \'T320 160 T480 160 T640 160" fill="none" \'',
                '    \'stroke="#339cff" stroke-width="5"/></svg>\'',
                ")",
                "Path('standing-wave.svg').write_text(svg, encoding='utf-8')",
                "print('generated with six ' + six.__version__)",
                "",
            ]
        )
        suite.code_materialize({"filename": "experiment.py", "content": source})
        execution = suite.process_execute({"argv": ["python", "experiment.py"]})
        assert isinstance(execution, dict) and execution["status"] == "completed"
        collected = suite.result_collect({})
        assert isinstance(collected, dict)
        names = {item["name"] for item in collected["artifacts"]}
        assert {"experiment.py", "standing-wave.csv", "standing-wave.svg"} <= names

        document = DocumentIR(
            requirement_id=uuid4(),
            requirement_version=1,
            outline_id=uuid4(),
            title="驻波实验报告",
            language="zh",
            sections=[
                DocumentSection(
                    title="正文",
                    goal="实验交付",
                    blocks=[
                        DocumentBlock(
                            kind=BlockKind.PARAGRAPH,
                            text="本报告包含真实执行产生的数据与驻波示意图。",
                            provenance=Provenance(agent="test"),
                        )
                    ],
                )
            ],
        )

        suite.document_pipeline.store.save(document, source_run_id="standing-wave-e2e")
        for format_name in ("md", "docx", "pdf"):
            artifact = suite.document_render(
                {
                    "document_id": str(document.document_id),
                    "revision": document.revision,
                    "format": format_name,
                    "filename": f"standing-wave-report.{format_name}",
                }
            )
            assert isinstance(artifact, dict)
            output = suite.project_root / str(artifact["relative_path"])
            assert output.is_file() and output.stat().st_size > 0
            if format_name == "docx":
                assert Document(output).paragraphs
            if format_name == "pdf":
                assert len(PdfReader(output).pages) >= 1
        kinds = {item.kind for item in artifacts.for_run("standing-wave-e2e")}
        assert {"source", "data", "figure", "log", "output"} <= kinds
        with databases.project_session(project_id) as session:
            records = list(session.scalars(select(ExecutionRecordRow)))
            assert len(records) == 1
            assert records[0].status == "succeeded"
            assert records[0].source_artifact_id is not None
    finally:
        suite.close()
