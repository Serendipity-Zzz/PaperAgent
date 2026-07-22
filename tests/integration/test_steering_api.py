from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.models import SteeringRecord
from paperagent.security import LocalSessionTokens
from paperagent.services.tasks import TaskService
from paperagent.workspace import SteeringEnvelope


def setup_running_task(client: TestClient, headers: dict[str, str]) -> tuple[str, str, str]:
    project = client.post("/api/projects", headers=headers, json={"name": "steering"}).json()
    project_id = project["id"]
    conversation = client.post(
        f"/api/projects/{project_id}/conversations",
        headers=headers,
        json={"title": "active A"},
    ).json()
    task = client.post(
        f"/api/projects/{project_id}/tasks",
        headers=headers,
        json={
            "kind": "agent.turn",
            "idempotency_key": "steering-a",
            "payload": {
                "session_id": conversation["id"],
                "content": "write A",
                "provider_id": None,
            },
        },
    ).json()
    client.patch(
        f"/api/projects/{project_id}/tasks/{task['id']}",
        headers=headers,
        json={"status": "running"},
    )
    return project_id, conversation["id"], task["id"]


def test_sidecar_guidance_confirmation_and_stop_are_auditable(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"t" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_id, session_id, run_id = setup_running_task(client, headers)
        path = f"/api/projects/{project_id}/runs/{run_id}/steer"

        status = client.post(path, headers=headers, json={"content": "当前进度?"})
        assert status.status_code == 200
        assert status.json()["envelope"]["impact_level"] == "L1"
        assert status.json()["target_run"]["status"] == "running"

        sidecar = client.post(
            path,
            headers=headers,
            json={"content": "另外问一个独立问题, 不影响当前任务"},
        )
        assert sidecar.status_code == 200
        assert sidecar.json()["envelope"]["impact_level"] == "L0"
        assert sidecar.json()["target_run"]["status"] == "running"
        task_rows = client.get(f"/api/projects/{project_id}/agent/tasks", headers=headers).json()
        assert any(row["kind"] == "steering.sidecar" for row in task_rows)

        guidance = client.post(
            path,
            headers=headers,
            json={"content": "请补充一段局限性"},
        )
        assert guidance.status_code == 200
        pending = guidance.json()
        assert pending["status"] == "pending_confirmation"
        assert pending["envelope"]["impact_level"] == "L2"
        assert pending["target_run"]["status"] == "running"

        applied = client.post(
            path,
            headers=headers,
            json={
                "content": "请补充一段局限性",
                "decision_id": pending["envelope"]["decision_id"],
                "confirmed": True,
            },
        )
        assert applied.status_code == 200
        assert applied.json()["target_run"]["payload"]["pending_guidance"]

        expired_stop = client.post(path, headers=headers, json={"content": "停止当前任务"}).json()
        databases = DatabaseManager(settings)
        with databases.project_session(project_id) as session:
            row = session.get(SteeringRecord, expired_stop["envelope"]["decision_id"])
            assert row is not None
            expired_envelope = SteeringEnvelope.model_validate_json(row.envelope_json)
            row.envelope_json = expired_envelope.model_copy(
                update={"expires_at": datetime.now(UTC) - timedelta(minutes=1)}
            ).model_dump_json()
            session.commit()
        expired = client.post(
            path,
            headers=headers,
            json={
                "content": "停止当前任务",
                "decision_id": expired_stop["envelope"]["decision_id"],
                "confirmed": True,
            },
        )
        assert expired.status_code == 409
        assert "expired" in expired.json()["detail"]

        stop = client.post(path, headers=headers, json={"content": "停止当前任务"}).json()
        assert stop["status"] == "pending_confirmation"
        rejected = client.post(
            path,
            headers=headers,
            json={
                "content": "停止当前任务",
                "decision_id": stop["envelope"]["decision_id"],
                "rejected": True,
            },
        )
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["target_run"]["status"] == "running"
        duplicate = client.post(
            path,
            headers=headers,
            json={
                "content": "停止当前任务",
                "decision_id": stop["envelope"]["decision_id"],
                "confirmed": True,
            },
        )
        assert duplicate.status_code == 409

        stop_again = client.post(path, headers=headers, json={"content": "停止当前任务"}).json()
        cancelled = client.post(
            path,
            headers=headers,
            json={
                "content": "停止当前任务",
                "decision_id": stop_again["envelope"]["decision_id"],
                "confirmed": True,
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["target_run"]["status"] == "cancelled"

        decisions = client.get(f"{path}ing", headers=headers).json()
        assert {row["status"] for row in decisions} >= {"applied", "rejected"}
        messages = client.get(
            f"/api/projects/{project_id}/conversations/{session_id}/messages",
            headers=headers,
        ).json()
        assert any("原任务已保留并折叠" in row["content"] for row in messages)


def test_l4_forks_from_checkpoint_and_preserves_stable_provenance(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"u" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_id, _session_id, run_id = setup_running_task(client, headers)
        tasks = TaskService(DatabaseManager(settings))
        tasks.set_checkpoint(project_id, run_id, "checkpoint-draft")
        current = tasks.get(project_id, run_id)
        tasks.update_payload(
            project_id,
            run_id,
            {
                **json.loads(current.payload_json),
                "completed_nodes": ["research"],
                "artifact_hashes": {"research": "sha256-stable"},
                "paid_side_effect": True,
            },
        )
        path = f"/api/projects/{project_id}/runs/{run_id}/steer"
        pending = client.post(
            path,
            headers=headers,
            json={"content": "前面错了, 研究对象改为中学生"},
        ).json()
        assert pending["status"] == "pending_confirmation"
        assert pending["envelope"]["impact_level"] == "L4"
        assert pending["envelope"]["permission_scopes"] == ["paid_or_external_side_effect"]
        assert pending["envelope"]["earliest_affected_checkpoint"] == "checkpoint-draft"

        applied = client.post(
            path,
            headers=headers,
            json={
                "content": "前面错了, 研究对象改为中学生",
                "decision_id": pending["envelope"]["decision_id"],
                "confirmed": True,
            },
        )
        assert applied.status_code == 200
        result = applied.json()
        assert result["target_run"]["status"] == "superseded"
        assert result["replacement_run_id"]
        rows = client.get(f"/api/projects/{project_id}/agent/tasks", headers=headers).json()
        replacement = next(row for row in rows if row["id"] == result["replacement_run_id"])
        assert replacement["parent_task_id"] == run_id
        assert replacement["checkpoint_ref"] == "checkpoint-draft"
        assert replacement["payload"]["artifact_hashes"]["research"] == "sha256-stable"
        assert replacement["payload"]["preserved_nodes"] == ["research"]
