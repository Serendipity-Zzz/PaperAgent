import os
from pathlib import Path

import pytest

from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.memory import ContextBudget, MemoryService, RollingContextBuilder
from paperagent.security.credentials import CredentialStore


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_dpapi_credential_create_update_delete_without_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    value = "credential-value-for-dpapi-test"
    reference = store.put("mock", value)
    assert value not in path.read_text(encoding="utf-8")
    assert store.get(reference) == value
    store.put("mock", "updated-value", reference)
    assert store.get(reference) == "updated-value"
    assert store.delete(reference)
    with pytest.raises(KeyError):
        store.get(reference)


def test_memory_scope_explicit_intent_and_clear(tmp_path: Path) -> None:
    manager = DatabaseManager(
        Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    )
    manager.initialize_global()
    memory = MemoryService(manager)
    with pytest.raises(PermissionError):
        memory.remember(scope="long_term", kind="preference", content="x", source="model")
    row = memory.remember(
        scope="long_term",
        kind="preference",
        content="prefer concise writing",
        source="user",
        explicit=True,
    )
    assert memory.list(scope="long_term")[0].id == row.id
    with pytest.raises(ValueError):
        memory.remember(
            scope="long_term",
            kind="api_key",
            content="forbidden",
            source="user",
            explicit=True,
        )
    with pytest.raises(PermissionError):
        memory.clear(scope="long_term", confirmation="yes")
    assert memory.clear(scope="long_term", confirmation="CLEAR MEMORY") == 1
    assert memory.list(scope="long_term") == []


def test_context_compression_preserves_non_lossy_state() -> None:
    envelope = RollingContextBuilder().build(
        ["old " * 100, "recent decision", "latest request"],
        ContextBudget(max_chars=500, recent_chars=80),
        protected_ids=["artifact:abc", "approval:def", "artifact:abc"],
        decisions=["Use Typst by default"],
        pending_tasks=["Render chapter 2"],
        previous_version=4,
    )
    assert envelope.summary_version == 5
    assert envelope.recent_messages[-1] == "latest request"
    assert envelope.protected_ids == ["artifact:abc", "approval:def"]
    assert envelope.decisions == ["Use Typst by default"]
    assert envelope.pending_tasks == ["Render chapter 2"]
