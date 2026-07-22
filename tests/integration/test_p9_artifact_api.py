from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from paperagent.api import create_app
from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.db.migrations import upgrade_database
from paperagent.db.models import FileRecord
from paperagent.security import LocalSessionTokens


def test_artifact_preview_download_lookup_and_message_links(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    tokens = LocalSessionTokens(secret=b"a" * 32)
    app = create_app(settings, tokens)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        project_id = client.post(
            "/api/projects", json={"name": "artifacts"}, headers=headers
        ).json()["id"]
        conversation_id = client.post(
            f"/api/projects/{project_id}/sessions",
            json={"title": "delivery"},
            headers=headers,
        ).json()["id"]
        message_id = client.post(
            f"/api/projects/{project_id}/sessions/{conversation_id}/messages",
            json={"role": "assistant", "content": "真实文件见附件", "run_id": "run-api"},
            headers=headers,
        ).json()["id"]

        root = app.state.databases.project_root(project_id)
        report = root / "artifacts" / "report.pdf"
        report.parent.mkdir(parents=True)
        report.write_bytes(b"%PDF-1.7\nartifact-api")
        service = ArtifactService(app.state.databases, project_id)
        artifact = service.register(
            report,
            kind="output",
            producer_tool="document.render",
            run_id="run-api",
        )
        service.link_run_to_message("run-api", conversation_id, message_id)

        messages = client.get(
            f"/api/projects/{project_id}/sessions/{conversation_id}/messages",
            headers=headers,
        ).json()
        assert messages[0]["artifact_links"][0]["id"] == artifact.id

        preview = client.get(
            f"/api/projects/{project_id}/artifacts/{artifact.id}/preview",
            headers=headers,
        )
        assert preview.status_code == 200 and preview.content.startswith(b"%PDF-")
        assert "inline" in preview.headers["content-disposition"]
        assert preview.headers["accept-ranges"] == "bytes"
        structured = client.post(
            f"/api/projects/{project_id}/artifacts/{artifact.id}/structured-preview",
            headers=headers,
        )
        assert structured.status_code == 200
        assert structured.json()["payload"]["options"]["raw_url"].endswith(
            f"/artifacts/{artifact.id}/preview"
        )

        download = client.get(
            f"/api/projects/{project_id}/artifacts/{artifact.id}/download",
            headers=headers,
        )
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]

        lookup = client.get(
            f"/api/projects/{project_id}/artifacts/lookup",
            params={"conversation_id": conversation_id, "relation": "output"},
            headers=headers,
        )
        assert lookup.json()[0]["sha256"] == artifact.sha256

        other_project = client.post(
            "/api/projects", json={"name": "other"}, headers=headers
        ).json()["id"]
        cross_project = client.get(
            f"/api/projects/{other_project}/artifacts/{artifact.id}/download",
            headers=headers,
        )
        assert cross_project.status_code == 404
        assert (
            client.get(
                f"/api/projects/{project_id}/artifacts/{artifact.id}/download"
            ).status_code
            == 401
        )


def test_legacy_files_are_backfilled_idempotently(tmp_path: Path) -> None:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    root = databases.project_root(project_id)
    root.mkdir(parents=True)
    engine = databases.project_engine(project_id)
    legacy = root / "artifacts" / "legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("# Legacy\n", encoding="utf-8")
    file_id = str(uuid4())
    with databases.project_session(project_id) as session:
        session.add(
            FileRecord(
                id=file_id,
                category="output",
                original_name="legacy.md",
                relative_path="artifacts/legacy.md",
                sha256="f" * 64,
                size_bytes=legacy.stat().st_size,
                provenance_json="{}",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num='0010_execution_artifacts'")
        )
    engine.dispose()

    database = root / "project.db"
    upgrade_database(database, kind="project")
    upgrade_database(database, kind="project")
    artifact = ArtifactService(databases, project_id).get(file_id, verify=False)
    assert artifact.id == file_id
    assert artifact.original_name == "legacy.md"
    assert artifact.producer_tool == "legacy.file"
