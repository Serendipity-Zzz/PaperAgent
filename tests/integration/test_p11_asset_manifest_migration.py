from __future__ import annotations

import sqlite3
from pathlib import Path

from paperagent.db.migrations import upgrade_database


def _legacy_p10_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(64) PRIMARY KEY);
            INSERT INTO alembic_version(version_num) VALUES ('0012_document_revisions');
            CREATE TABLE artifacts (id VARCHAR(36) PRIMARY KEY);
            CREATE TABLE document_revisions (id VARCHAR(36) PRIMARY KEY);
            CREATE TABLE document_revision_assets (id VARCHAR(36) PRIMARY KEY);
            """
        )


def test_asset_manifest_migration_is_additive_and_replay_safe(tmp_path: Path) -> None:
    database = tmp_path / "legacy-project.db"
    _legacy_p10_database(database)
    upgrade_database(database, kind="project")
    upgrade_database(database, kind="project")
    with sqlite3.connect(database) as connection:
        revision_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(document_revisions)")
        }
        binding_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(document_revision_assets)")
        }
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {
        "asset_manifest_json",
        "asset_manifest_hash",
        "image_required",
        "expected_asset_count",
        "presentation_hash",
        "numbering_hash",
    } <= revision_columns
    assert {"logical_id", "binding_evidence", "status"} <= binding_columns
    assert version == ("0016_document_numbering",)
