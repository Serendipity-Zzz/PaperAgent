from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_import_search_classify_and_delete_knowledge(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"k" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", json={"name": "knowledge"}, headers=headers).json()
        project_id = project["id"]
        upload = client.post(
            f"/api/projects/{project_id}/knowledge/import",
            headers=headers,
            files={
                "file": ("requirements.md", "目标: 建立离线知识检索\n必须保护隐私", "text/markdown")
            },
            data={"collection_id": "requirements", "confidentiality": "sensitive"},
        )
        assert upload.status_code == 201, upload.text
        body = upload.json()
        assert body["indexed"] == 2
        assert body["classification"] == "requirement"
        assert all(not item["instruction_trust"] for item in body["items"])
        duplicate = client.post(
            f"/api/projects/{project_id}/knowledge/import",
            headers=headers,
            files={"file": ("renamed.md", "目标: 建立离线知识检索\n必须保护隐私", "text/markdown")},
            data={"collection_id": "requirements", "confidentiality": "sensitive"},
        )
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["indexed"] == 0

        search = client.get(
            f"/api/projects/{project_id}/knowledge/search",
            params={"q": "离线 检索"},
            headers=headers,
        )
        hits = search.json()["hits"]
        assert hits and hits[0]["locator"]["line_start"] == 1
        item_id = hits[0]["item_id"]

        corrected = client.patch(
            f"/api/projects/{project_id}/knowledge/{item_id}/classification",
            json={"content_type": "manual"},
            headers=headers,
        )
        assert corrected.status_code == 200
        denied = client.delete(f"/api/projects/{project_id}/knowledge/{item_id}", headers=headers)
        assert denied.status_code == 409
        deleted = client.delete(
            f"/api/projects/{project_id}/knowledge/{item_id}",
            params={"confirmation": "DELETE KNOWLEDGE"},
            headers=headers,
        )
        assert deleted.json() == {"deleted": True}
