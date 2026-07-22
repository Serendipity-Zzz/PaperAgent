from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.security import LocalSessionTokens
from paperagent.services.tasks import TaskService


def test_three_runs_l0_l2_l4_l5_survive_refresh_disconnect_and_restart(
    tmp_path: Path,
) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"p7-certification-secret-32-bytes")
    bearer = tokens.issue()
    headers = {"Authorization": f"Bearer {bearer}"}
    run_ids: list[str] = []
    conversation_ids: list[str] = []

    with TestClient(create_app(settings, tokens)) as client:
        project_id = client.post(
            "/api/projects", headers=headers, json={"name": "P7 three-run certification"}
        ).json()["id"]
        for index in range(3):
            conversation_id = client.post(
                f"/api/projects/{project_id}/conversations",
                headers=headers,
                json={"title": f"background-{index + 1}"},
            ).json()["id"]
            task = client.post(
                f"/api/projects/{project_id}/tasks",
                headers=headers,
                json={
                    "kind": "agent.turn",
                    "idempotency_key": f"p7-run-{index + 1}",
                    "payload": {
                        "session_id": conversation_id,
                        "content": f"background request {index + 1}",
                        "provider_id": None,
                    },
                },
            ).json()
            client.patch(
                f"/api/projects/{project_id}/tasks/{task['id']}",
                headers=headers,
                json={"status": "running"},
            )
            conversation_ids.append(conversation_id)
            run_ids.append(task["id"])

        path_a = f"/api/projects/{project_id}/runs/{run_ids[0]}/steer"
        l0 = client.post(
            path_a,
            headers=headers,
            json={"content": "另外问一个独立问题, 不影响当前任务"},
        ).json()
        assert l0["envelope"]["impact_level"] == "L0"
        l2 = client.post(
            path_a, headers=headers, json={"content": "请补充一段局限性"}
        ).json()
        assert l2["envelope"]["impact_level"] == "L2"
        assert client.post(
            path_a,
            headers=headers,
            json={
                "content": "请补充一段局限性",
                "decision_id": l2["envelope"]["decision_id"],
                "confirmed": True,
            },
        ).json()["target_run"]["status"] == "running"

        tasks = TaskService(DatabaseManager(settings))
        tasks.set_checkpoint(project_id, run_ids[1], "checkpoint-draft")
        current = tasks.get(project_id, run_ids[1])
        tasks.update_payload(
            project_id,
            run_ids[1],
            {
                **json.loads(current.payload_json),
                "completed_nodes": ["research"],
                "artifact_hashes": {"research": "sha256-stable"},
                "paid_side_effect": True,
            },
        )
        path_b = f"/api/projects/{project_id}/runs/{run_ids[1]}/steer"
        l4 = client.post(
            path_b,
            headers=headers,
            json={"content": "前面错了, 研究对象改为中学生"},
        ).json()
        assert l4["envelope"]["impact_level"] == "L4"
        l4_applied = client.post(
            path_b,
            headers=headers,
            json={
                "content": "前面错了, 研究对象改为中学生",
                "decision_id": l4["envelope"]["decision_id"],
                "confirmed": True,
            },
        ).json()
        replacement_run_id = l4_applied["replacement_run_id"]

        path_c = f"/api/projects/{project_id}/runs/{run_ids[2]}/steer"
        l5 = client.post(
            path_c, headers=headers, json={"content": "停止当前任务"}
        ).json()
        assert l5["envelope"]["impact_level"] == "L5"
        assert client.post(
            path_c,
            headers=headers,
            json={
                "content": "停止当前任务",
                "decision_id": l5["envelope"]["decision_id"],
                "confirmed": True,
            },
        ).json()["target_run"]["status"] == "cancelled"

        refreshed = client.get(
            f"/api/projects/{project_id}/agent/tasks", headers=headers
        ).json()
        assert len({row["conversation_id"] for row in refreshed if row["conversation_id"]}) == 3

    with TestClient(create_app(settings, tokens)) as restarted:
        rows = restarted.get(
            f"/api/projects/{project_id}/agent/tasks", headers=headers
        ).json()
        by_id = {row["id"]: row for row in rows}
        assert by_id[run_ids[0]]["status"] == "paused"
        assert by_id[run_ids[0]]["current_phase"] == "recovery_required"
        assert by_id[run_ids[0]]["recovery_strategy"] == "confirm_before_replay"
        assert by_id[run_ids[0]]["payload"]["pending_guidance"]
        assert by_id[run_ids[1]]["status"] == "superseded"
        assert by_id[run_ids[2]]["status"] == "cancelled"
        assert by_id[replacement_run_id]["parent_task_id"] == run_ids[1]
        assert by_id[replacement_run_id]["checkpoint_ref"] == "checkpoint-draft"
        assert by_id[replacement_run_id]["payload"]["preserved_nodes"] == ["research"]

        for run_id, minimum in zip(run_ids, (2, 1, 1), strict=True):
            audit = restarted.get(
                f"/api/projects/{project_id}/runs/{run_id}/steering", headers=headers
            ).json()
            assert len(audit) >= minimum
            assert all(row["status"] == "applied" for row in audit)
        messages = restarted.get(
            f"/api/projects/{project_id}/conversations/{conversation_ids[2]}/messages",
            headers=headers,
        ).json()
        assert any("原任务已保留并折叠" in row["content"] for row in messages)
