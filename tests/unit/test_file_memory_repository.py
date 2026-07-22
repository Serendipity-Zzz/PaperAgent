from dataclasses import dataclass
from pathlib import Path

import pytest

from paperagent.memory import (
    FileMemoryRepository,
    MemoryEntry,
    MemoryScope,
    MemoryStatus,
    migrate_legacy_memories,
)


def entry(scope: MemoryScope, *, project_id: str | None = None) -> MemoryEntry:
    return MemoryEntry(
        topic="typography",
        subject="Chinese body font",
        scope=scope,
        project_id=project_id,
        kind="preference",
        content="中文论文正文使用宋体。",
        source_type="user_message",
        source_id="thread-1:message-8",
        status=MemoryStatus.CONFIRMED,
        allowed_providers=["configured_online"],
        idempotency_key=f"font-{scope.value}-{project_id or 'global'}",
    )


def test_file_memory_write_is_atomic_readable_and_idempotent(tmp_path: Path) -> None:
    repository = FileMemoryRepository(tmp_path)
    first = repository.write(entry(MemoryScope.GLOBAL), explicit=True)
    second = repository.write(entry(MemoryScope.GLOBAL), explicit=True)
    assert first.created and not second.created
    assert first.entry.memory_id == second.entry.memory_id
    stored = repository.read(tmp_path / first.relative_path)
    assert stored.content == "中文论文正文使用宋体。"
    manifest = (tmp_path / first.manifest_path).read_text(encoding="utf-8")
    assert "typography" in manifest and str(stored.memory_id) in manifest
    assert len(repository.list(MemoryScope.GLOBAL)) == 1


def test_project_memory_and_conversation_state_are_portable_files(tmp_path: Path) -> None:
    repository = FileMemoryRepository(tmp_path)
    written = repository.write(entry(MemoryScope.PROJECT, project_id="paper-1"))
    assert written.relative_path.startswith("projects/paper-1/memory/")
    conversation = repository.append_conversation(
        "paper-1", "thread-1", {"sequence": 1, "role": "user", "content": "hello"}
    )
    state = repository.write_project_state("paper-1", "requirement", {"version": 2})
    assert '"sequence": 1' in conversation.read_text(encoding="utf-8")
    assert '"version": 2' in state.read_text(encoding="utf-8")


def test_global_memory_requires_explicit_intent_and_rejects_sensitive_kind(
    tmp_path: Path,
) -> None:
    repository = FileMemoryRepository(tmp_path)
    with pytest.raises(PermissionError, match="explicit"):
        repository.write(entry(MemoryScope.GLOBAL))
    sensitive = entry(MemoryScope.PROJECT, project_id="paper-1").model_copy(
        update={"kind": "api_key", "idempotency_key": "secret-1"}
    )
    with pytest.raises(ValueError, match="credentials"):
        repository.write(sensitive)


@dataclass
class Legacy:
    id: str
    scope: str
    project_id: str | None
    kind: str
    content: str
    source: str


def test_legacy_migration_is_replay_safe(tmp_path: Path) -> None:
    repository = FileMemoryRepository(tmp_path)
    records = [Legacy("1", "long_term", None, "writing_style", "concise", "user")]
    first = migrate_legacy_memories(repository, records)
    second = migrate_legacy_memories(repository, records)
    assert (first.migrated, first.skipped, first.errors) == (1, 0, [])
    assert (second.migrated, second.skipped, second.errors) == (0, 1, [])
