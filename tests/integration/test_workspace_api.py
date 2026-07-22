from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def settings(tmp_path: Path) -> Settings:
    return Settings(project_root=tmp_path / "repo", data_dir=tmp_path / "data", environment="test")


def test_projects_conversations_and_messages_are_independent_durable_entities(
    tmp_path: Path,
) -> None:
    tokens = LocalSessionTokens(secret=b"w" * 32)
    with TestClient(create_app(settings(tmp_path), tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_ids: list[str] = []
        conversation_ids: list[str] = []
        for name in ("项目 A", "项目 B"):
            project = client.post("/api/projects", json={"name": name}, headers=headers).json()
            conversation = client.post(
                f"/api/projects/{project['id']}/conversations",
                json={"title": f"{name} 会话"},
                headers=headers,
            ).json()
            project_ids.append(project["id"])
            conversation_ids.append(conversation["id"])

        for index in range(100):
            response = client.post(
                f"/api/projects/{project_ids[0]}/conversations/{conversation_ids[0]}/messages",
                json={"role": "user", "content": f"message-{index}"},
                headers=headers,
            )
            assert response.status_code == 201
            assert response.json()["sequence"] == index + 1

        assert len(client.get("/api/projects", headers=headers).json()) == 2
        assert (
            len(client.get(f"/api/projects/{project_ids[0]}/conversations", headers=headers).json())
            == 1
        )
        assert (
            client.get(
                f"/api/projects/{project_ids[1]}/conversations/{conversation_ids[1]}/messages",
                headers=headers,
            ).json()
            == []
        )
        page = client.get(
            f"/api/projects/{project_ids[0]}/conversations/{conversation_ids[0]}/messages?after=90&limit=5",
            headers=headers,
        ).json()
        assert [item["sequence"] for item in page] == [91, 92, 93, 94, 95]
        latest = client.get(
            f"/api/projects/{project_ids[0]}/conversations/{conversation_ids[0]}/messages?before=2147483647&limit=5",
            headers=headers,
        ).json()
        assert [item["sequence"] for item in latest] == [96, 97, 98, 99, 100]
        older = client.get(
            f"/api/projects/{project_ids[0]}/conversations/{conversation_ids[0]}/messages?before=96&limit=5",
            headers=headers,
        ).json()
        assert [item["sequence"] for item in older] == [91, 92, 93, 94, 95]


def test_workspace_update_archive_and_soft_delete_require_explicit_actions(
    tmp_path: Path,
) -> None:
    tokens = LocalSessionTokens(secret=b"z" * 32)
    with TestClient(create_app(settings(tmp_path), tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", json={"name": "Before"}, headers=headers).json()
        project_id = project["id"]
        conversation = client.post(
            f"/api/projects/{project_id}/conversations",
            json={"title": "Before chat"},
            headers=headers,
        ).json()
        updated = client.patch(
            f"/api/projects/{project_id}",
            json={"name": "After", "description": "durable"},
            headers=headers,
        )
        assert updated.json()["name"] == "After"
        chat = client.patch(
            f"/api/projects/{project_id}/conversations/{conversation['id']}",
            json={"title": "After chat", "draft": "local draft", "last_read_sequence": 3},
            headers=headers,
        )
        assert chat.json()["draft"] == "local draft"
        assert client.delete(f"/api/projects/{project_id}", headers=headers).status_code == 409
        deleted = client.delete(
            f"/api/projects/{project_id}?confirmation=DELETE%20PROJECT", headers=headers
        )
        assert deleted.json() == {"deleted": True}
        assert client.get(f"/api/projects/{project_id}", headers=headers).status_code == 404
