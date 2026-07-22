from __future__ import annotations

import sqlite3
from pathlib import Path

from paperagent.db.migrations import upgrade_database


def test_legacy_project_database_gains_ordered_message_metadata(tmp_path: Path) -> None:
    path = tmp_path / "legacy-project.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0003_provider_settings');
        CREATE TABLE sessions (
            id VARCHAR(36) PRIMARY KEY, title VARCHAR(255) NOT NULL,
            archived BOOLEAN NOT NULL DEFAULT 0, draft TEXT NOT NULL DEFAULT '',
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE messages (
            id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36) NOT NULL,
            role VARCHAR(32) NOT NULL, content TEXT NOT NULL, created_at DATETIME
        );
        INSERT INTO sessions VALUES ('s1', 'legacy', 0, '', '2026-01-01', '2026-01-01');
        INSERT INTO messages VALUES ('m2', 's1', 'assistant', 'two', '2026-01-02');
        INSERT INTO messages VALUES ('m1', 's1', 'user', 'one', '2026-01-01');
        """
    )
    connection.commit()
    connection.close()

    upgrade_database(path, kind="project")

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
    rows = connection.execute(
        "SELECT content, sequence, status FROM messages ORDER BY sequence"
    ).fetchall()
    session_columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
    steering_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(steering_decisions)")
    }
    connection.close()
    assert {"sequence", "run_id", "branch_id", "status", "updated_at"} <= columns
    assert {"status", "last_read_sequence", "deleted_at"} <= session_columns
    assert {
        "target_task_id",
        "trigger_message_id",
        "envelope_json",
        "status",
        "replacement_task_id",
    } <= steering_columns
    assert rows == [("one", 1, "final"), ("two", 2, "final")]
