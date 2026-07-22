"""Safely clear PaperAgent conversation/run state while preserving user assets.

The command is intentionally dry-run by default. Applying a cleanup requires an
explicit confirmation phrase and creates SQLite backups before the transaction.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CONFIRMATION_PHRASE = "CLEAR-CONVERSATION-CACHE"
DELETE_ORDER = (
    "approvals",
    "steering_decisions",
    "events",
    "messages",
    "tasks",
    "sessions",
)
PROTECTED_TABLES = ("files", "schema_versions", "alembic_version")


@dataclass(frozen=True)
class DatabaseInventory:
    project_id: str
    database: str
    delete_counts: dict[str, int]
    protected_counts: dict[str, int]

    @property
    def rows_to_delete(self) -> int:
        return sum(self.delete_counts.values())


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def inventory_database(database: Path) -> DatabaseInventory:
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        tables = _table_names(connection)
        delete_counts = {
            table: _count_rows(connection, table)
            for table in DELETE_ORDER
            if table in tables
        }
        protected_counts = {
            table: _count_rows(connection, table)
            for table in PROTECTED_TABLES
            if table in tables
        }
    return DatabaseInventory(
        project_id=database.parent.name,
        database=str(database),
        delete_counts=delete_counts,
        protected_counts=protected_counts,
    )


def discover_project_databases(
    data_root: Path, project_ids: Iterable[str] | None = None
) -> list[Path]:
    projects_root = (data_root / "projects").resolve()
    if not projects_root.is_dir():
        raise FileNotFoundError(f"Projects directory does not exist: {projects_root}")

    requested = set(project_ids or ())
    databases: list[Path] = []
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        if requested and project_dir.name not in requested:
            continue
        database = project_dir / "project.db"
        if database.is_file():
            databases.append(database.resolve())

    found = {database.parent.name for database in databases}
    missing = requested - found
    if missing:
        raise FileNotFoundError(
            "Unknown project id(s): " + ", ".join(sorted(missing))
        )
    return databases


def backup_database(database: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    destination = backup_root / database.parent.name / "project.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    return destination


def clear_database(database: Path) -> tuple[DatabaseInventory, DatabaseInventory]:
    before = inventory_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = _table_names(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in DELETE_ORDER:
                if table in tables:
                    connection.execute(f"DELETE FROM {_quote_identifier(table)}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    after = inventory_database(database)
    for table, count in before.protected_counts.items():
        if after.protected_counts.get(table) != count:
            raise RuntimeError(
                f"Protected table changed in {database}: {table} "
                f"{count} -> {after.protected_counts.get(table)}"
            )
    return before, after


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _print_inventory(inventories: list[DatabaseInventory]) -> None:
    for item in inventories:
        counts = ", ".join(
            f"{name}={count}" for name, count in item.delete_counts.items()
        )
        protected = ", ".join(
            f"{name}={count}" for name, count in item.protected_counts.items()
        )
        print(
            f"{item.project_id}: delete[{counts or 'none'}] "
            f"preserve[{protected or 'none'}]"
        )
    print(f"Total rows selected: {sum(item.rows_to_delete for item in inventories)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Clear sessions, messages, runs and events from PaperAgent project "
            "databases. Projects, files, knowledge, memory and providers are preserved."
        )
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="Limit cleanup to a project id; repeat for multiple projects.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply cleanup; otherwise only show a dry-run."
    )
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required with --apply: {CONFIRMATION_PHRASE}",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        help="Optional backup destination; defaults below <data-root>/backups.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = args.data_root.expanduser().resolve()
    databases = discover_project_databases(data_root, args.project_id)
    inventories = [inventory_database(database) for database in databases]
    _print_inventory(inventories)

    if not args.apply:
        print("Dry-run only. No database was modified.")
        return 0
    if args.confirm != CONFIRMATION_PHRASE:
        raise SystemExit(
            f"Refusing cleanup: pass --confirm {CONFIRMATION_PHRASE}"
        )

    timestamp = _timestamp()
    backup_root = (
        args.backup_root.expanduser().resolve()
        if args.backup_root
        else data_root / "backups" / "conversation-cleanup" / timestamp
    )
    changed = [
        (database, inventory)
        for database, inventory in zip(databases, inventories, strict=True)
        if inventory.rows_to_delete > 0
    ]
    for database, _inventory in changed:
        backup_database(database, backup_root)

    results: list[dict[str, object]] = []
    for database, _inventory in changed:
        before, after = clear_database(database)
        results.append({"before": asdict(before), "after": asdict(after)})

    backup_root.mkdir(parents=True, exist_ok=True)
    report_path = backup_root / "cleanup-report.json"
    report_path.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "data_root": str(data_root),
                "deleted_tables": list(DELETE_ORDER),
                "protected_tables": list(PROTECTED_TABLES),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Cleanup complete. Backup and report: {backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
