from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.security import LocalSessionTokens


def make_settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path / "repo", data_dir=tmp_path / "data", environment="test")


def test_database_initialization_is_idempotent_and_enforces_pragmas(tmp_path: Path) -> None:
    manager = DatabaseManager(make_settings(tmp_path))
    manager.initialize_global()
    manager.initialize_global()
    assert manager.schema_version() == 1
    with manager.global_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"


def test_project_session_message_survives_app_restart(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tokens = LocalSessionTokens(secret=b"x" * 32)
    app = create_app(settings, tokens)
    with TestClient(app) as client:
        token = client.get("/api/bootstrap-token").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        project = client.post("/api/projects", json={"name": "中文 项目"}, headers=headers)
        assert project.status_code == 201
        project_id = project.json()["id"]
        session = client.post(
            f"/api/projects/{project_id}/sessions", json={"title": "第一次会话"}, headers=headers
        )
        assert session.status_code == 200
        session_id = session.json()["id"]
        message = client.post(
            f"/api/projects/{project_id}/sessions/{session_id}/messages",
            json={"role": "user", "content": "保留这条消息"},
            headers=headers,
        )
        assert message.status_code == 201

    restarted = create_app(settings, tokens)
    with TestClient(restarted) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        projects = client.get("/api/projects", headers=headers).json()
        messages = client.get(
            f"/api/projects/{project_id}/sessions/{session_id}/messages", headers=headers
        ).json()
        assert projects[0]["name"] == "中文 项目"
        assert messages[0]["content"] == "保留这条消息"


def test_write_routes_reject_missing_or_invalid_tokens(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path), LocalSessionTokens(secret=b"y" * 32))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/projects", json={"name": "forbidden"}).status_code == 401
        assert (
            client.post(
                "/api/projects",
                json={"name": "forbidden"},
                headers={"Authorization": "Bearer invalid"},
            ).status_code
            == 401
        )
        preflight = client.options(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in preflight.headers


def test_frontend_es_module_worker_uses_javascript_mime_type(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    asset = settings.project_root / "frontend" / "dist" / "assets" / "worker.mjs"
    asset.parent.mkdir(parents=True)
    asset.write_text("export const ready = true;", encoding="utf-8")
    (asset.parents[1] / "index.html").write_text("<main>PaperAgent</main>", encoding="utf-8")
    with TestClient(create_app(settings, LocalSessionTokens(secret=b"m" * 32))) as client:
        response = client.get("/assets/worker.mjs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.text == "export const ready = true;"


def test_task_approval_and_backup_api(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tokens = LocalSessionTokens(secret=b"b" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", json={"name": "task api"}, headers=headers).json()
        task = client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"kind": "write", "idempotency_key": "api-1", "payload": {}},
            headers=headers,
        )
        assert task.status_code == 200
        running = client.patch(
            f"/api/projects/{project['id']}/tasks/{task.json()['id']}",
            json={"status": "running"},
            headers=headers,
        )
        assert running.json()["status"] == "running"
        approval = client.post(
            f"/api/projects/{project['id']}/tasks/{task.json()['id']}/approvals",
            json={"action": "network", "scope": {"host": "example.test"}},
            headers=headers,
        )
        decision = client.patch(
            f"/api/projects/{project['id']}/approvals/{approval.json()['id']}",
            json={"approved": True},
            headers=headers,
        )
        assert decision.json()["status"] == "approved"
        backup = client.post("/api/backups/global", headers=headers)
        assert backup.status_code == 200
        verified = client.get(f"/api/backups/{backup.json()['backup_id']}/verify", headers=headers)
        assert verified.status_code == 200
