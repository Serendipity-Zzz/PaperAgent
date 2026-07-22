from pathlib import Path

import pytest

from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.schemas import TaskStatus
from paperagent.security import LocalSessionTokens
from paperagent.services.tasks import TaskService


def test_local_token_expiry_and_tampering() -> None:
    service = LocalSessionTokens(secret=b"z" * 32, ttl_seconds=10)
    token = service.issue(now=100)
    assert service.verify(token, now=105)
    assert not service.verify(token, now=111)
    assert not service.verify(token + "changed", now=105)


def test_task_transitions_approval_and_idempotency(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    databases.project_engine(project_id).dispose()
    service = TaskService(databases)
    task = service.create(project_id, "write", "same-key", {"chapter": 1})
    duplicate = service.create(project_id, "write", "same-key", {"chapter": 2})
    assert duplicate.id == task.id
    service.transition(project_id, task.id, TaskStatus.RUNNING)
    approval = service.request_approval(project_id, task.id, "network", {"host": "example.test"})
    first = service.decide_approval(project_id, approval.id, approved=True)
    second = service.decide_approval(project_id, approval.id, approved=True)
    assert first.status == second.status == "approved"
    with pytest.raises(ValueError):
        service.decide_approval(project_id, approval.id, approved=False)
    with pytest.raises(ValueError):
        service.transition(project_id, task.id, TaskStatus.COMPLETED)
