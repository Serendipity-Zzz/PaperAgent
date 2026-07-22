from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    FigureSpec,
    Provenance,
)
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.migrations import upgrade_database
from paperagent.db.models import ArtifactRecord, DocumentDeliveryRecord
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.rendering.asset_binding import manifest_from_document
from paperagent.rendering.delivery import DeliveryStatus
from paperagent.rendering.delivery_store import (
    DeliveryTransitionError,
    DocumentDeliveryStore,
)
from paperagent.rendering.preflight import RenderPreflight
from paperagent.rendering.renderers import MarkdownRenderer
from paperagent.schemas.typography import TypographySpec


def _suite(tmp_path: Path) -> tuple[ExecutionToolSuite, ArtifactService, str]:
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
            run_id="p11-delivery",
            uv_path=None,
            artifact_service=artifacts,
            source_conversation_id="conversation-p11",
        ),
        artifacts,
        project_id,
    )


def _canonical(suite: ExecutionToolSuite) -> tuple[str, int]:
    payload = suite.document_pipeline.compose(
        {
            "title": "Canonical report",
            "content": "# Result\n\nVerified body content.",
            "language": "en",
        }
    )
    assert isinstance(payload, dict)
    return str(payload["document_id"]), int(str(payload["revision"]))


def test_requested_typography_creates_style_only_revision_before_render(
    tmp_path: Path,
) -> None:
    suite, _artifacts, _project_id = _suite(tmp_path)
    document_id, revision = _canonical(suite)
    before = suite._revision_store().load(UUID(document_id), revision)
    before_hashes = before.hashes()
    suite.requested_typography = TypographySpec(body_font="SimSun", body_size_pt=12)

    payload = suite.document_render(
        {"document_id": document_id, "revision": revision, "format": "md"}
    )

    assert isinstance(payload, dict)
    assert payload["document_revision"] == revision + 1
    after = suite._revision_store().load(UUID(document_id))
    after_hashes = after.hashes()
    assert after.typography.body_font == "SimSun"
    assert after.typography.body_size_pt == 12
    assert after_hashes.content_hash == before_hashes.content_hash
    assert after_hashes.asset_set_hash == before_hashes.asset_set_hash
    assert after_hashes.style_hash != before_hashes.style_hash


def test_render_preflight_rejects_path_only_figure_before_renderer(tmp_path: Path) -> None:
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Unsafe legacy document",
        language="en",
        sections=[
            DocumentSection(
                title="Result",
                goal="preflight",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.FIGURE,
                        figure=FigureSpec(path="figure.png"),
                        caption="Unbound figure",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )
    document.asset_manifest = manifest_from_document(document, image_required=True)
    result = RenderPreflight().validate(document, format_name="pdf")
    assert not result.passed
    assert {item.category.value for item in result.issues} == {"missing_asset"}
    assert {item.repair_node for item in result.issues} == {"document_asset_barrier"}


def test_delivery_state_machine_enforces_order_and_optimistic_lock(tmp_path: Path) -> None:
    suite, artifacts, project_id = _suite(tmp_path)
    try:
        document_id, revision = _canonical(suite)
        revision_id = suite.document_pipeline.store.revision_id(UUID(document_id), revision)
        deliveries = DocumentDeliveryStore(artifacts.databases, project_id)
        record = deliveries.create(
            revision_id=revision_id,
            format_name="md",
            renderer="markdown",
            renderer_version="2.0.0",
            options_hash="a" * 64,
            figure_artifact_ids=[],
            source_run_id="p11-delivery",
        )
        with pytest.raises(DeliveryTransitionError, match="illegal"):
            deliveries.transition(
                record.id,
                DeliveryStatus.DELIVERED,
                expected_version=record.version,
            )
        rendering = deliveries.transition(
            record.id,
            DeliveryStatus.RENDERING,
            expected_version=record.version,
        )
        with pytest.raises(DeliveryTransitionError, match="optimistic"):
            deliveries.transition(
                record.id,
                DeliveryStatus.VALIDATING,
                expected_version=record.version,
            )
        validating = deliveries.transition(
            record.id,
            DeliveryStatus.VALIDATING,
            expected_version=rendering.version,
        )
        delivered = deliveries.transition(
            record.id,
            DeliveryStatus.DELIVERED,
            expected_version=validating.version,
        )
        assert delivered.status == "delivered"
    finally:
        suite.close()


def test_render_is_idempotent_and_publishes_only_after_qa(tmp_path: Path) -> None:
    suite, artifacts, project_id = _suite(tmp_path)
    try:
        document_id, revision = _canonical(suite)
        assert (
            suite.document_pipeline.store.status(UUID(document_id), revision).value
            == "canonical_ready"
        )
        request = {
            "document_id": document_id,
            "revision": revision,
            "format": "md",
            "filename": "canonical.md",
        }
        first = suite.document_render(request)
        second = suite.document_render(request)
        assert first["artifact_id"] == second["artifact_id"]
        artifact = artifacts.get(str(first["artifact_id"]))
        assert artifact.delivery_status == "delivered"
        assert artifact.validation_status == "valid"
        assert first["presentation_schema_version"] == "1.0"
        assert first["presentation_hash"]
        assert first["presentation_expectation_hash"]
        lineage = json.loads(artifact.lineage_json)
        assert lineage["presentation_schema_version"] == "1.0"
        assert lineage["presentation_hash"] == first["presentation_hash"]
        assert (
            lineage["presentation_expectation_hash"]
            == first["presentation_expectation_hash"]
        )
        with artifacts.databases.project_session(project_id) as session:
            deliveries = list(session.scalars(select(DocumentDeliveryRecord)))
        assert len(deliveries) == 1
        assert deliveries[0].status == "delivered"
    finally:
        suite.close()


def test_legacy_canonical_is_migrated_as_new_revision_without_overwrite(
    tmp_path: Path,
) -> None:
    suite, artifacts, _project_id = _suite(tmp_path)
    try:
        legacy = DocumentIR(
            requirement_id=uuid4(),
            requirement_version=1,
            outline_id=uuid4(),
            title="Legacy canonical",
            language="en",
            sections=[
                DocumentSection(
                    title="Body",
                    goal="migration",
                    blocks=[
                        DocumentBlock(
                            kind=BlockKind.PARAGRAPH,
                            text="Original immutable body.",
                            provenance=Provenance(agent="legacy"),
                        )
                    ],
                )
            ],
        )
        suite.document_pipeline.store.save(legacy)
        result = suite.document_render(
            {
                "document_id": str(legacy.document_id),
                "revision": 1,
                "format": "md",
            }
        )
        assert result["document_revision"] == 2
        assert suite.document_pipeline.store.load(legacy.document_id, 1).asset_manifest is None
        migrated = suite.document_pipeline.store.load(legacy.document_id, 2)
        assert migrated.asset_manifest is not None
        assert migrated.metadata["lazy_migrated_from_revision"] == 1
        artifact = artifacts.get(str(result["artifact_id"]))
        assert artifact.revision_id == suite.document_pipeline.store.revision_id(
            legacy.document_id, 2
        )
    finally:
        suite.close()


def test_failed_qa_registers_rejected_diagnostic_not_final_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite, artifacts, project_id = _suite(tmp_path)
    try:
        document_id, revision = _canonical(suite)

        def leak_placeholder(
            _renderer: MarkdownRenderer, _document: DocumentIR, output: Path
        ) -> Path:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "# Report\n\nVerified source content is supplied by the renderer.",
                encoding="utf-8",
            )
            return output

        monkeypatch.setattr(MarkdownRenderer, "render", leak_placeholder)
        with pytest.raises(RuntimeError, match="rejected by delivery QA"):
            suite.document_render(
                {
                    "document_id": document_id,
                    "revision": revision,
                    "format": "md",
                    "filename": "must-not-publish.md",
                }
            )
        assert not (suite.artifact_root / "must-not-publish.md").exists()
        with artifacts.databases.project_session(project_id) as session:
            rejected = list(
                session.scalars(
                    select(ArtifactRecord).where(
                        ArtifactRecord.delivery_status == "rejected"
                    )
                )
            )
            deliveries = list(session.scalars(select(DocumentDeliveryRecord)))
        assert len(rejected) == 1
        assert rejected[0].validation_status == "rejected"
        assert deliveries[0].status == "rejected"
    finally:
        suite.close()


def test_document_delivery_migration_upgrades_p11_c_database_replay_safely(
    tmp_path: Path,
) -> None:
    database = tmp_path / "p11-c.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(64) PRIMARY KEY);
            INSERT INTO alembic_version(version_num) VALUES ('0013_asset_manifests');
            CREATE TABLE artifacts (id VARCHAR(36) PRIMARY KEY);
            """
        )
    upgrade_database(database, kind="project")
    upgrade_database(database, kind="project")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        artifact_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
        }
    assert "document_deliveries" in tables
    assert {"delivery_status", "renderer_version", "lineage_json"} <= artifact_columns
