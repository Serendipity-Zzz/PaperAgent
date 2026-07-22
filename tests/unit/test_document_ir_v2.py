from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, select

from paperagent.agents.document_ir import (
    CURRENT_DOCUMENT_IR_SCHEMA,
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    FigureSpec,
    InlineKind,
    InlineNode,
    Provenance,
    TableCell,
    TableRow,
    TableSpec,
    diff_documents,
    migrate_document_ir,
)
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.models import DocumentRecord, DocumentRevisionRecord
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.rendering.revision_store import DocumentRevisionStore
from paperagent.schemas.typography import TypographySpec


def _document(tmp_path: Path, *, conversation_id: str = "conversation-a") -> DocumentIR:
    figure = tmp_path / "figure.png"
    figure.write_bytes(b"synthetic-image-bytes")
    child = DocumentSection(
        title="Child section",
        goal="Nested structure",
        level=2,
        blocks=[
            DocumentBlock(
                kind=BlockKind.TABLE,
                table=TableSpec(
                    rows=[
                        TableRow(
                            cells=[
                                TableCell(text="name", header=True),
                                TableCell(text="value", header=True),
                            ]
                        ),
                        TableRow(cells=[TableCell(text="mode"), TableCell(text="1")]),
                    ]
                ),
                provenance=Provenance(agent="test"),
            ),
            DocumentBlock(
                kind=BlockKind.FIGURE,
                caption="Synthetic figure",
                figure=FigureSpec(path=str(figure), alt_text="Synthetic figure"),
                provenance=Provenance(agent="test"),
            ),
        ],
    )
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="DocumentIR 2.0",
        language="mixed",
        metadata={"source_conversation_id": conversation_id},
        sections=[
            DocumentSection(
                title="Root section",
                goal="Semantic content",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="Semantic body",
                        inlines=[InlineNode(kind=InlineKind.STRONG, text="Semantic")],
                        provenance=Provenance(agent="test"),
                    )
                ],
                children=[child],
            )
        ],
    )


def test_document_ir_v2_is_recursive_typed_and_hashes_styles_independently(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    assert document.schema_version == CURRENT_DOCUMENT_IR_SCHEMA == "2.2"
    assert len(list(document.iter_sections())) == 2
    assert len(list(document.iter_blocks())) == 3
    assert document.sections[0].children[0].blocks[0].table is not None
    assert document.sections[0].children[0].blocks[1].figure is not None

    before = document.hashes()
    changed = document.restyle(TypographySpec(body_font="宋体", body_size_pt=11))
    after = changed.hashes()
    assert before.content_hash == after.content_hash
    assert before.structure_hash == after.structure_hash
    assert before.asset_set_hash == after.asset_set_hash
    assert before.citation_set_hash == after.citation_set_hash
    assert before.style_hash != after.style_hash


def test_canonical_json_roundtrip_uses_body_and_preserves_hashes(tmp_path: Path) -> None:
    document = _document(tmp_path)
    payload = document.canonical_payload()
    assert "body" in payload and "sections" not in payload
    assert payload["hashes"] == document.hashes().model_dump(mode="json")
    restored = migrate_document_ir(json.loads(json.dumps(payload)))
    assert restored == document
    assert restored.hashes() == document.hashes()


def test_v11_markdown_blob_migrates_to_semantic_blocks_without_mutating_input() -> None:
    payload = {
        "schema_version": "1.1",
        "document_id": str(uuid4()),
        "requirement_id": str(uuid4()),
        "requirement_version": 1,
        "outline_id": str(uuid4()),
        "title": "Legacy",
        "language": "mixed",
        "sections": [
            {
                "title": "正文",
                "goal": "migrate",
                "blocks": [
                    {
                        "kind": "paragraph",
                        "text": (
                            "## Method\n\n**Goal**\n\n- first\n- second\n\n"
                            "| A | B |\n|---|---|\n|1|2|"
                        ),
                        "provenance": {"agent": "legacy"},
                    }
                ],
            }
        ],
    }
    original = json.loads(json.dumps(payload))
    migrated = migrate_document_ir(payload)
    kinds = [block.kind for block in migrated.sections[0].blocks]
    assert BlockKind.HEADING in kinds
    assert BlockKind.LIST in kinds
    assert BlockKind.TABLE in kinds
    assert len(migrated.sections[0].blocks) > 1
    assert payload == original


def test_revision_store_catalogs_canonical_artifact_and_lineage(tmp_path: Path) -> None:
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
    artifacts = ArtifactService(databases, project_id)
    store = DocumentRevisionStore(
        project_root,
        databases=databases,
        project_id=project_id,
        artifact_service=artifacts,
    )
    first = _document(tmp_path)
    store.save(first, source_conversation_id="conversation-a")
    second = first.restyle(TypographySpec(body_font="宋体"))
    store.save(
        second,
        parent_revision_id=store.revision_id(first.document_id, first.revision),
        source_conversation_id="conversation-a",
    )

    assert store.latest(document_id=first.document_id) == second
    assert store.latest(source_conversation_id="conversation-a") == second
    lineage = store.list_lineage(first.document_id)
    assert [item.revision for item in lineage] == [1, 2]
    assert lineage[0].content_hash == lineage[1].content_hash
    assert lineage[0].style_hash != lineage[1].style_hash
    with databases.project_session(project_id) as session:
        document_row = session.get(DocumentRecord, str(first.document_id))
        revisions = list(
            session.scalars(
                select(DocumentRevisionRecord)
                .where(DocumentRevisionRecord.document_id == str(first.document_id))
                .order_by(DocumentRevisionRecord.revision_number)
            )
        )
        assert document_row is not None
        assert document_row.latest_revision_id == store.revision_id(first.document_id, 2)
        assert len(revisions) == 2
        assert all(item.canonical_artifact_id for item in revisions)
    canonical = artifacts.for_run("")
    assert canonical == []
    all_artifact_names = [
        path.name for path in (project_root / "artifacts" / "document-ir").rglob("*.json")
    ]
    assert all_artifact_names == ["000001.document-ir.json", "000002.document-ir.json"]


def test_project_migration_is_additive_for_document_lineage(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    engine = databases.project_engine(project_id)
    inspector = inspect(engine)
    assert {
        "documents",
        "document_revisions",
        "document_revision_assets",
    } <= set(inspector.get_table_names())
    assert {"document_id", "revision_id", "derived_from_artifact_id"} <= {
        item["name"] for item in inspector.get_columns("artifacts")
    }
    assert {
        "asset_manifest_json",
        "asset_manifest_hash",
        "image_required",
        "expected_asset_count",
        "numbering_hash",
    } <= {item["name"] for item in inspector.get_columns("document_revisions")}
    assert {"logical_id", "binding_evidence", "status"} <= {
        item["name"] for item in inspector.get_columns("document_revision_assets")
    }
    engine.dispose()


def test_document_render_persists_one_canonical_revision_before_all_formats(
    tmp_path: Path,
) -> None:
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
    artifacts = ArtifactService(databases, project_id)
    suite = ExecutionToolSuite(
        data_root=settings.resolved_data_dir,
        project_root=project_root,
        run_id="run-document-v2",
        uv_path=None,
        artifact_service=artifacts,
    )
    try:
        document = _document(tmp_path)
        document = document.model_copy(
            deep=True,
            update={"sections": [document.sections[0].model_copy(update={"children": []})]},
        )
        suite.document_pipeline.store.save(document)
        results = [
            suite.document_render(
                {
                    "document_id": str(document.document_id),
                    "revision": document.revision,
                    "format": format_name,
                    "filename": f"shared.{format_name}",
                }
            )
            for format_name in ("md", "docx")
        ]
    finally:
        suite.close()

    records = [artifacts.get(str(result["artifact_id"])) for result in results]
    assert len({item.document_id for item in records}) == 1
    assert len({item.revision_id for item in records}) == 1
    assert len({item.derived_from_artifact_id for item in records}) == 1
    canonical_id = records[0].derived_from_artifact_id
    assert canonical_id is not None
    canonical = artifacts.get(canonical_id)
    assert canonical.kind == "document_ir"
    assert canonical.revision_id == records[0].revision_id


def test_global_style_diff_includes_back_matter_without_losing_anchor(tmp_path: Path) -> None:
    document = _document(tmp_path).model_copy(
        update={
            "back_matter": [
                DocumentBlock(
                    kind=BlockKind.CITATION,
                    text="Reference entry",
                    provenance=Provenance(agent="test"),
                )
            ]
        }
    )
    changed = document.restyle(TypographySpec(body_font="SimSun"))
    diff = diff_documents(document, changed)
    assert diff.style_changed is True
    assert diff.content_changed is False
    assert document.back_matter[0].block_id in diff.changed_blocks
