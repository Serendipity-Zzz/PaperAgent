import time
from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def _poll(client: TestClient, path: str, headers: dict[str, str]) -> dict[str, object]:
    for _ in range(100):
        result = client.get(path, headers=headers).json()
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("agent job did not finish")


def test_async_agent_job_pause_resume_cancel_and_inspect(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"j" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        client.post(
            "/api/settings/providers",
            headers=headers,
            json={
                "id": "test-job-model",
                "provider_type": "mock",
                "base_url": "http://test.invalid/v1",
                "model": "test-only",
                "capabilities": ["chat"],
                "extra": {"delay_ms": 150},
            },
        )
        project = client.post(
            "/api/projects", headers=headers, json={"name": "job-api"}
        ).json()
        session = client.post(
            f"/api/projects/{project['id']}/sessions",
            headers=headers,
            json={"title": "job"},
        ).json()
        base = f"/api/projects/{project['id']}"
        started = client.post(
            f"{base}/sessions/{session['id']}/agent/jobs",
            headers=headers,
            json={
                "content": "explain the task",
                "provider_id": "test-job-model",
                "idempotency_key": "job-one",
            },
        )
        assert started.status_code == 202
        task_id = started.json()["id"]
        paused = client.post(f"{base}/agent/tasks/{task_id}/pause", headers=headers)
        assert paused.status_code == 200 and paused.json()["status"] == "paused"
        resumed = client.post(f"{base}/agent/tasks/{task_id}/resume", headers=headers)
        assert resumed.status_code == 200 and resumed.json()["status"] == "running"
        completed = _poll(client, f"{base}/agent/tasks/{task_id}", headers)
        assert completed["status"] == "completed"
        assert completed["payload"]["result"]["message"]["content"] == "OK"
        inspection = client.get(
            f"{base}/agent/tasks/{task_id}/inspect", headers=headers
        ).json()
        event_types = [item["type"] for item in inspection["events"]]
        assert "turn.accepted" in event_types
        assert "node.completed" in event_types

        second = client.post(
            f"{base}/sessions/{session['id']}/agent/jobs",
            headers=headers,
            json={
                "content": "cancel this",
                "provider_id": "test-job-model",
                "idempotency_key": "job-two",
            },
        ).json()
        cancelled = client.post(
            f"{base}/agent/tasks/{second['id']}/cancel", headers=headers
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"


def test_orphaned_running_task_is_reconciled_to_paused(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"k" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post(
            "/api/projects", headers=headers, json={"name": "restart"}
        ).json()
        base = f"/api/projects/{project['id']}"
        task = client.post(
            f"{base}/tasks",
            headers=headers,
            json={"kind": "agent.turn", "idempotency_key": "orphan", "payload": {}},
        ).json()
        client.patch(
            f"{base}/tasks/{task['id']}", headers=headers, json={"status": "running"}
        )
        reconciled = client.get(
            f"{base}/agent/tasks/{task['id']}", headers=headers
        ).json()
        assert reconciled["status"] == "paused"
        assert reconciled["payload"]["recovery_required"] is True


def test_recovery_selection_resumes_the_original_task(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"r" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post(
            "/api/projects", headers=headers, json={"name": "revision-choice"}
        ).json()
        session = client.post(
            f"/api/projects/{project['id']}/sessions",
            headers=headers,
            json={"title": "choice"},
        ).json()
        base = f"/api/projects/{project['id']}"
        task = client.post(
            f"{base}/tasks",
            headers=headers,
            json={
                "kind": "agent.turn",
                "idempotency_key": "choice-task",
                "payload": {
                    "session_id": session["id"],
                    "content": "convert the selected revision",
                    "recovery_required": True,
                    "recovery_options": [
                        {"id": "revision-2", "kind": "revision", "title": "Report r2"}
                    ],
                },
            },
        ).json()
        client.patch(
            f"{base}/tasks/{task['id']}", headers=headers, json={"status": "paused"}
        )
        resumed = client.post(
            f"{base}/agent/tasks/{task['id']}/resume",
            headers=headers,
            json={"selection_id": "revision-2"},
        )
        assert resumed.status_code == 200
        payload = resumed.json()["payload"]
        assert payload["selected_recovery_option"]["id"] == "revision-2"
        assert payload["pending_guidance"][-1]["kind"] == "recovery_selection"
