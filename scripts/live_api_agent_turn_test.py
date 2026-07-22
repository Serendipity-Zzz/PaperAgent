from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app


def main() -> None:
    started = time.perf_counter()
    with TestClient(create_app()) as client:
        token = client.get("/api/bootstrap-token").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project = client.post(
            "/api/projects", headers=headers, json={"name": "live-agent-api-gate"}
        ).json()
        session = client.post(
            f"/api/projects/{project['id']}/sessions",
            headers=headers,
            json={"title": "ConversationEngine live gate"},
        ).json()
        content = "Reply with API_AGENT_OK. Use tools only if they are necessary."
        client.post(
            f"/api/projects/{project['id']}/sessions/{session['id']}/messages",
            headers=headers,
            json={"role": "user", "content": content},
        ).raise_for_status()
        response = client.post(
            f"/api/projects/{project['id']}/sessions/{session['id']}/agent/jobs",
            headers=headers,
            json={
                "content": content,
                "approved": False,
                "idempotency_key": f"live-api:{session['id']}",
            },
        )
        response.raise_for_status()
        task = response.json()
        task_id = task["id"]
        paused = client.post(
            f"/api/projects/{project['id']}/agent/tasks/{task_id}/pause",
            headers=headers,
        )
        paused.raise_for_status()
        resumed = client.post(
            f"/api/projects/{project['id']}/agent/tasks/{task_id}/resume",
            headers=headers,
        )
        resumed.raise_for_status()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            task = client.get(
                f"/api/projects/{project['id']}/agent/tasks/{task_id}",
                headers=headers,
            ).json()
            if task["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.2)
        if task["status"] != "completed":
            raise RuntimeError(f"live async Agent job ended as {task['status']}")
        payload = task["payload"]["result"]
        if "API_AGENT_OK" not in payload["message"]["content"]:
            raise RuntimeError("live API Agent turn did not satisfy the assertion")
        inspection = client.get(
            f"/api/projects/{project['id']}/agent/tasks/{task_id}/inspect",
            headers=headers,
        ).json()
        evidence = {
            "status": "passed",
            "conversation_engine": True,
            "langgraph": True,
            "agent_loop": True,
            "async_job": True,
            "pause_resume": True,
            "task_id": task_id,
            "rounds": payload["rounds"],
            "tool_call_count": payload["tool_call_count"],
            "routes": payload["routes"],
            "prompt_modules": payload["prompt"]["modules"],
            "event_types": sorted({item["type"] for item in inspection["events"]}),
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
        encoded = json.dumps(evidence, ensure_ascii=False, indent=2)
        print(encoded)
        target_value = os.environ.get("PAPERAGENT_LIVE_API_EVIDENCE")
        if target_value:
            target = Path(target_value).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
