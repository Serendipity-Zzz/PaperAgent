from __future__ import annotations

import sqlite3
from pathlib import Path

from paperagent.db.migrations import upgrade_database


def test_project_migration_adds_execution_artifact_tables(tmp_path: Path) -> None:
    database = tmp_path / "project.db"
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
        link_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(artifact_links)")
        }
    assert {"artifacts", "artifact_links", "execution_records"} <= tables
    assert {"run_id", "sha256", "validation_status", "relative_path"} <= artifact_columns
    assert {"ix_artifact_links_message_id", "ix_artifact_links_run_id"} <= link_indexes
