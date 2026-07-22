import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.schemas import TaskStatus
from paperagent.security import LocalSessionTokens
from paperagent.services.progress import DurableProgressSink
from paperagent.services.repositories import ProjectRepository
from paperagent.services.tasks import TaskService


def setup_runtime(tmp_path: Path) -> tuple[TaskService, str]:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = ProjectRepository(databases).create("durable-runs").id
    return TaskService(databases), project_id


def test_run_sequences_are_monotonic_and_event_ids_are_idempotent(tmp_path: Path) -> None:
    tasks, project_id = setup_runtime(tmp_path)
    run = tasks.create(project_id, "agent.turn", "run-sequence", {"session_id": "s1"})

    def append(index: int) -> int:
        row = tasks.append_event(project_id, run.id, "run.progress", {"index": index})
        return int(row.run_sequence or 0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(append, range(40)))

    assert len(set(sequences)) == 40
    rows = tasks.events_after(project_id, run.id, 0)
    assert [row.run_sequence for row in rows] == list(range(1, 42))

    event_id = str(uuid4())
    first = tasks.append_event(
        project_id, run.id, "run.progress", {"step": 1}, event_id=event_id
    )
    repeated = tasks.append_event(
        project_id, run.id, "run.progress", {"step": 2}, event_id=event_id
    )
    assert repeated.sequence == first.sequence
    assert repeated.run_sequence == first.run_sequence


def test_claim_lease_cas_recovery_and_public_redaction(tmp_path: Path) -> None:
    tasks, project_id = setup_runtime(tmp_path)
    run = tasks.create(project_id, "agent.turn", "run-lease", {"session_id": "s1"})
    claimed = tasks.claim(project_id, run.id, "worker-a", lease_seconds=30)
    assert claimed.status == "running" and claimed.attempt == 1
    with pytest.raises(ValueError, match="leased"):
        tasks.claim(project_id, run.id, "worker-b", lease_seconds=30)
    heartbeat = tasks.heartbeat(project_id, run.id, "worker-a")
    assert heartbeat.heartbeat_at is not None
    with pytest.raises(ValueError, match="version changed"):
        tasks.transition(
            project_id,
            run.id,
            TaskStatus.PAUSED,
            expected_version=claimed.version - 1,
        )

    sink = DurableProgressSink(tasks)
    sink.emit(
        project_id=project_id,
        run_id=run.id,
        event_type="model.completed",
        payload={
            "summary": "模型调用完成 sk-private123456",
            "hidden_reasoning": "never publish this",
            "api_key": "secret-value",
        },
    )
    public = tasks.events_after(project_id, run.id, 0)[-1].payload_json
    assert "private123456" not in public
    assert "never publish" not in public
    assert "secret-value" not in public

    recovered = tasks.reconcile_orphans(project_id, force=True)
    assert recovered[0].status == "paused"
    assert recovered[0].recovery_strategy == "confirm_before_replay"


def test_run_event_history_and_sse_replay_from_last_event_id(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"r" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", headers=headers, json={"name": "sse"}).json()
        base = f"/api/projects/{project['id']}"
        created = client.post(
            f"{base}/tasks",
            headers=headers,
            json={"kind": "test", "idempotency_key": "sse-run", "payload": {}},
        ).json()
        client.patch(
            f"{base}/tasks/{created['id']}",
            headers=headers,
            json={"status": "running"},
        )

        history = client.get(
            f"{base}/runs/{created['id']}/events?after_sequence=1", headers=headers
        )
        assert history.status_code == 200
        assert [event["sequence"] for event in history.json()] == [2]

        replay = client.get(
            f"{base}/runs/{created['id']}/events/stream?follow=false",
            headers=headers | {"Last-Event-ID": "1"},
        )
        assert replay.status_code == 200
        assert "id: 2" in replay.text
        assert "event: run.status_changed" in replay.text


def test_restart_resumes_only_explicitly_replay_safe_checkpoint(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"s" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        client.post(
            "/api/settings/providers",
            headers=headers,
            json={
                "id": "restart-model",
                "provider_type": "mock",
                "base_url": "http://test.invalid/v1",
                "model": "test-only",
                "capabilities": ["chat"],
            },
        )
        project = client.post(
            "/api/projects", headers=headers, json={"name": "restart-safe"}
        ).json()
        conversation = client.post(
            f"/api/projects/{project['id']}/conversations",
            headers=headers,
            json={"title": "resume"},
        ).json()

    databases = DatabaseManager(settings)
    databases.initialize_global()
    tasks = TaskService(databases)
    run = tasks.create(
        project["id"],
        "agent.turn",
        "restart-checkpoint",
        {
            "session_id": conversation["id"],
            "content": "resume this safe request",
            "provider_id": "restart-model",
            "approved": False,
            "replay_safe": True,
        },
    )
    tasks.update_phase(project["id"], run.id, "model", checkpoint_ref="checkpoint-1")
    tasks.claim(project["id"], run.id, "dead-worker")

    with TestClient(create_app(settings, tokens)) as restarted:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        path = f"/api/projects/{project['id']}/runs/{run.id}"
        for _ in range(100):
            restored = restarted.get(path, headers=headers).json()
            if restored["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert restored["status"] == "completed"
        assert restored["attempt"] == 2
        event_types = [
            event["type"]
            for event in restarted.get(f"{path}/events", headers=headers).json()
        ]
        assert "run.recovery_required" in event_types
        assert "run.claimed" in event_types
