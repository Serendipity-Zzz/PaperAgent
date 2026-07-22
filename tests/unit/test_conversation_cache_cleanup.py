from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "clean_conversation_cache.py"
SPEC = importlib.util.spec_from_file_location("clean_conversation_cache", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup
SPEC.loader.exec_module(cleanup)


def _make_database(path: Path, *, with_steering: bool = True) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY);
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE tasks (id TEXT PRIMARY KEY);
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY,
                task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE
            );
            CREATE TABLE events (sequence INTEGER PRIMARY KEY);
            CREATE TABLE files (id TEXT PRIMARY KEY, relative_path TEXT NOT NULL);
            CREATE TABLE schema_versions (component TEXT PRIMARY KEY);
            INSERT INTO sessions VALUES ('session-1');
            INSERT INTO messages VALUES ('message-1', 'session-1');
            INSERT INTO tasks VALUES ('task-1');
            INSERT INTO approvals VALUES ('approval-1', 'task-1');
            INSERT INTO events VALUES (1);
            INSERT INTO files VALUES ('file-1', 'artifacts/result.pdf');
            INSERT INTO schema_versions VALUES ('project');
            """
        )
        if with_steering:
            connection.executescript(
                """
                CREATE TABLE steering_decisions (id TEXT PRIMARY KEY);
                INSERT INTO steering_decisions VALUES ('steering-1');
                """
            )


def test_clear_database_preserves_files_and_schema(tmp_path: Path) -> None:
    database = tmp_path / "projects" / "project-1" / "project.db"
    _make_database(database)

    before, after = cleanup.clear_database(database)

    assert before.rows_to_delete == 6
    assert after.rows_to_delete == 0
    assert after.protected_counts == before.protected_counts == {
        "files": 1,
        "schema_versions": 1,
    }


def test_backup_and_legacy_schema_without_steering(tmp_path: Path) -> None:
    database = tmp_path / "projects" / "project-2" / "project.db"
    _make_database(database, with_steering=False)
    backup = cleanup.backup_database(database, tmp_path / "backups")

    cleanup.clear_database(database)

    assert cleanup.inventory_database(database).rows_to_delete == 0
    assert cleanup.inventory_database(backup).rows_to_delete == 5


def test_discovery_is_scoped_to_requested_projects(tmp_path: Path) -> None:
    first = tmp_path / "projects" / "project-1" / "project.db"
    second = tmp_path / "projects" / "project-2" / "project.db"
    _make_database(first)
    _make_database(second)

    assert cleanup.discover_project_databases(tmp_path, ["project-2"]) == [
        second.resolve()
    ]
