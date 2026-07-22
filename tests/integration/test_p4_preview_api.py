from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_preview_file_parts_selection_annotation_and_cache_api(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"p" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project = client.post("/api/projects", json={"name": "preview"}, headers=headers).json()
        project_id = project["id"]
        uploaded = client.post(
            f"/api/projects/{project_id}/knowledge/import",
            headers=headers,
            files={"file": ("evidence.md", "# Result\nMeasured improvement", "text/markdown")},
            data={"collection_id": "evidence", "confidentiality": "personal"},
        )
        assert uploaded.status_code == 201
        file_id = uploaded.json()["file_id"]

        listed = client.get(f"/api/projects/{project_id}/files", headers=headers)
        assert listed.json()[0]["original_name"] == "evidence.md"
        preview = client.post(f"/api/projects/{project_id}/preview/{file_id}", headers=headers)
        assert preview.status_code == 200, preview.text
        artifact = preview.json()
        assert artifact["status"] == "ready"
        assert artifact["part_count"] == 2
        repeated = client.post(
            f"/api/projects/{project_id}/preview/{file_id}", headers=headers
        ).json()
        assert repeated["id"] == artifact["id"]

        parts_response = client.get(
            f"/api/projects/{project_id}/preview/artifacts/{artifact['id']}/parts",
            params={"offset": 0, "limit": 1},
            headers=headers,
        )
        parts = parts_response.json()["parts"]
        assert len(parts) == 1
        anchor = parts[0]["anchor"]
        selected = client.post(
            f"/api/projects/{project_id}/preview/artifacts/{artifact['id']}/selection",
            json={"action": "evidence", "anchor": anchor, "text": "Measured improvement"},
            headers=headers,
        )
        assert selected.json()["anchor"]["source_hash"] == artifact["source_hash"]
        annotated = client.post(
            f"/api/projects/{project_id}/preview/artifacts/{artifact['id']}/annotations",
            json={"anchor": anchor, "body": "Use in discussion"},
            headers=headers,
        )
        assert annotated.status_code == 201, annotated.text

        raw = client.get(f"/api/projects/{project_id}/files/{file_id}/raw", headers=headers)
        assert raw.content.startswith(b"# Result")
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in raw.headers["content-security-policy"]

        cleared = client.delete(f"/api/projects/{project_id}/preview/cache", headers=headers)
        assert cleared.json() == {"cleared": 1}
        annotations = client.get(
            f"/api/projects/{project_id}/files/{file_id}/annotations", headers=headers
        ).json()
        assert annotations[0]["body"] == "Use in discussion"


def test_unknown_and_broken_preview_degrade_without_breaking_import(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"q" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_id = client.post(
            "/api/projects", json={"name": "fallback"}, headers=headers
        ).json()["id"]
        upload = client.post(
            f"/api/projects/{project_id}/knowledge/import",
            headers=headers,
            files={"file": ("unknown.xyz", b"opaque content", "application/octet-stream")},
            data={"collection_id": "misc", "confidentiality": "personal"},
        )
        # Ingestion may not parse an unknown format, but the stored source remains previewable.
        assert upload.status_code == 422
        files = client.get(f"/api/projects/{project_id}/files", headers=headers).json()
        artifact = client.post(
            f"/api/projects/{project_id}/preview/{files[0]['id']}", headers=headers
        ).json()
        assert artifact["fidelity"] == "metadata"
        assert artifact["capabilities"] == ["system_open"]
