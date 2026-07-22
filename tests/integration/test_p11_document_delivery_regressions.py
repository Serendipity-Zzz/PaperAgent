from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PIL import Image
from sqlalchemy import select

from paperagent.agents.document_ir import BlockKind, migrate_document_ir
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.models import DocumentRevisionAssetRecord, DocumentRevisionRecord
from paperagent.execution.document_pipeline import DocumentPipelineTools


def _artifact_service(tmp_path: Path) -> tuple[ArtifactService, Path]:
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
    databases.project_engine(project_id).dispose()
    return ArtifactService(databases, project_id), root


def test_browser_ready_five_figure_report_keeps_artifacts_in_canonical_revision(
    tmp_path: Path,
) -> None:
    service, root = _artifact_service(tmp_path)
    run_id = "run-standing-wave"
    filenames: list[str] = []
    registered: list[str] = []
    for index in range(1, 6):
        filename = f"fig{index}_standing_wave.png"
        path = root / "runs" / run_id / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), color=(20 * index, 60, 140)).save(path)
        artifact = service.register(
            path,
            kind="figure",
            producer_tool="result.collect",
            run_id=run_id,
        )
        filenames.append(filename)
        registered.append(artifact.id)

    content = "# 实验结果\n\n" + "\n\n".join(
        f"![驻波实验图 {index}]({filename})"
        for index, filename in enumerate(filenames, start=1)
    )
    tools = DocumentPipelineTools(
        root,
        artifact_service=service,
        run_id=run_id,
        conversation_id="conversation-standing-wave",
    )
    payload = tools.compose(
        {
            "title": "驻波实验报告",
            "content": content,
            "language": "zh",
        }
    )
    assert isinstance(payload, dict)
    document = migrate_document_ir(payload)
    figures = [
        block.figure
        for block in document.iter_blocks()
        if block.kind is BlockKind.FIGURE and block.figure is not None
    ]
    assert len(figures) == 5
    assert {str(item.artifact_id) for item in figures} == set(registered)
    resolved = tools.asset_resolve({"document_ir": document.canonical_payload()})
    assert resolved["resolved"] == 5
    assert resolved["asset_barrier"] == "ready"
    with service.databases.project_session(service.project_id) as session:
        revision_id = tools.store.revision_id(document.document_id, document.revision)
        revision = session.get(DocumentRevisionRecord, revision_id)
        bindings = list(
            session.scalars(
                select(DocumentRevisionAssetRecord).where(
                    DocumentRevisionAssetRecord.revision_id == revision_id
                )
            )
        )
        assert revision is not None
        assert revision.image_required
        assert revision.expected_asset_count == 5
        assert revision.asset_manifest_hash == document.asset_manifest.manifest_hash
        assert len(bindings) == 5
        assert all(item.logical_id and item.binding_evidence for item in bindings)
