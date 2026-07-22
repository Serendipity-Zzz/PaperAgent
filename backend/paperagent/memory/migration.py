from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from paperagent.memory.repository import FileMemoryRepository
from paperagent.memory.schemas import (
    MemoryEntry,
    MemoryMigrationReport,
    MemoryScope,
    MemoryStatus,
)


class LegacyMemoryRecord(Protocol):
    id: str
    scope: str
    project_id: str | None
    kind: str
    content: str
    source: str


class LegacyMemoryReadAdapter:
    """Read-only bridge retained solely for migration and rollback inspection."""

    def __init__(self, records: Iterable[LegacyMemoryRecord]) -> None:
        self._records = records

    def list(self) -> list[LegacyMemoryRecord]:
        return list(self._records)

    def write(self, *_args: object, **_kwargs: object) -> None:
        raise PermissionError("legacy SQLite memory is read-only after file-first migration")


def migrate_legacy_memories(
    repository: FileMemoryRepository,
    records: Iterable[LegacyMemoryRecord],
) -> MemoryMigrationReport:
    scanned = migrated = skipped = 0
    errors: list[str] = []
    for record in records:
        scanned += 1
        try:
            scope = MemoryScope.PROJECT if record.scope == "project" else MemoryScope.GLOBAL
            result = repository.write(
                MemoryEntry(
                    topic=_topic(record.kind),
                    subject=f"Migrated {record.kind}",
                    scope=scope,
                    project_id=record.project_id if scope is MemoryScope.PROJECT else None,
                    kind=record.kind,
                    content=record.content,
                    source_type="legacy_sqlite",
                    source_id=f"memory:{record.id}",
                    status=MemoryStatus.CONFIRMED,
                    idempotency_key=f"legacy-memory:{record.id}",
                ),
                explicit=scope is MemoryScope.GLOBAL,
            )
            if result.created:
                migrated += 1
            else:
                skipped += 1
        except Exception as error:
            errors.append(f"{record.id}: {error}")
    return MemoryMigrationReport(
        scanned=scanned,
        migrated=migrated,
        skipped=skipped,
        errors=errors,
    )


def _topic(kind: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in kind)
    normalized = normalized.casefold().strip("_")
    return (normalized or "general")[:64]
