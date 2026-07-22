import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.db.migrations import upgrade_database
from paperagent.security import LocalSessionTokens


def _headers(tokens: LocalSessionTokens) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.issue()}"}


def _save(
    client: TestClient,
    headers: dict[str, str],
    provider_id: str,
    *,
    modality: str = "text",
    model: str | None = None,
    api_key: str | None = None,
    version: int | None = None,
    delay_ms: int = 0,
) -> dict[str, object]:
    response = client.post(
        "/api/settings/providers",
        headers=headers,
        json={
            "id": provider_id,
            "display_name": provider_id,
            "modality": modality,
            "protocol": "mock" if modality == "text" else "seedream_image",
            "provider_type": "mock" if modality == "text" else "seedream_image",
            "base_url": "http://test.invalid/v1",
            "model": model or provider_id,
            "api_key": api_key,
            "capabilities": ["chat"] if modality == "text" else ["image_generation"],
            "extra": {"delay_ms": delay_ms},
            "version": version,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_text_image_bindings_rotation_redaction_and_safe_disable(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"p" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = _headers(tokens)
        first = _save(client, headers, "text-a", api_key="p6-plaintext-secret-a")
        second = _save(client, headers, "text-b", api_key="p6-plaintext-secret-b")
        image = _save(client, headers, "image-a", modality="image")

        assert first["active"] is True
        assert second["active"] is False
        assert image["active"] is True
        assert first["credential_status"] == "available"
        assert "credential_ref" not in first and "api_key" not in first

        bindings = client.get("/api/settings/provider-bindings", headers=headers).json()
        text_binding = next(item for item in bindings if item["modality"] == "text")
        switched = client.post(
            "/api/settings/providers/text-b/activate",
            headers=headers,
            json={"scope": "global", "expected_version": text_binding["version"]},
        )
        assert switched.status_code == 200

        cannot_disable_active = client.delete(
            "/api/settings/providers/text-b",
            headers=headers,
            params={"confirmation": "DISABLE PROVIDER", "expected_version": second["version"]},
        )
        assert cannot_disable_active.status_code == 409

        binding_version = switched.json()["version"]
        assert client.post(
            "/api/settings/providers/text-a/activate",
            headers=headers,
            json={"scope": "global", "expected_version": binding_version},
        ).status_code == 200
        assert client.delete(
            "/api/settings/providers/text-b",
            headers=headers,
            params={"confirmation": "DISABLE PROVIDER", "expected_version": second["version"]},
        ).status_code == 200

        rotated = _save(
            client,
            headers,
            "text-a",
            api_key="p6-rotated-plaintext-secret",
            version=int(first["version"]),
        )
        assert rotated["secret_version"] == 2
        conflict = client.post(
            "/api/settings/providers",
            headers=headers,
            json={
                "id": "text-a",
                "provider_type": "mock",
                "base_url": "http://test.invalid/v1",
                "model": "stale-write",
                "capabilities": ["chat"],
                "version": int(first["version"]),
            },
        )
        assert conflict.status_code == 409

    persisted = b"".join(path.read_bytes() for path in settings.resolved_data_dir.rglob("*.*"))
    assert b"p6-plaintext-secret-a" not in persisted
    assert b"p6-plaintext-secret-b" not in persisted
    assert b"p6-rotated-plaintext-secret" not in persisted


def test_inflight_run_keeps_snapshot_while_new_run_uses_switched_binding(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"q" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = _headers(tokens)
        _save(client, headers, "snapshot-a", model="model-a", delay_ms=250)
        _save(client, headers, "snapshot-b", model="model-b")
        project = client.post("/api/projects", headers=headers, json={"name": "snapshot"}).json()
        session = client.post(
            f"/api/projects/{project['id']}/sessions",
            headers=headers,
            json={"title": "snapshot"},
        ).json()
        base = f"/api/projects/{project['id']}"
        first = client.post(
            f"{base}/sessions/{session['id']}/agent/jobs",
            headers=headers,
            json={"content": "first", "idempotency_key": "snapshot-first"},
        )
        assert first.status_code == 202
        assert first.json()["provider_snapshot"]["id"] == "snapshot-a"
        bindings = client.get("/api/settings/provider-bindings", headers=headers).json()
        binding = next(item for item in bindings if item["modality"] == "text")
        assert client.post(
            "/api/settings/providers/snapshot-b/activate",
            headers=headers,
            json={"scope": "global", "expected_version": binding["version"]},
        ).status_code == 200
        second = client.post(
            f"{base}/sessions/{session['id']}/agent/jobs",
            headers=headers,
            json={"content": "second", "idempotency_key": "snapshot-second"},
        )
        assert second.status_code == 202
        assert second.json()["provider_snapshot"]["id"] == "snapshot-b"

        for task_id, expected in (
            (first.json()["id"], "snapshot-a"),
            (second.json()["id"], "snapshot-b"),
        ):
            for _ in range(100):
                task = client.get(f"{base}/agent/tasks/{task_id}", headers=headers).json()
                if task["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert task["status"] == "completed"
            assert task["provider_snapshot"]["id"] == expected
            assert "credential_ref" not in task["provider_snapshot"]


def test_image_generation_without_credential_is_blocked_not_mocked(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"r" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = _headers(tokens)
        _save(client, headers, "image-blocked", modality="image")
        project = client.post("/api/projects", headers=headers, json={"name": "images"}).json()
        result = client.post(
            f"/api/projects/{project['id']}/images/generate",
            headers=headers,
            json={"prompt": "a workflow", "approved": True},
        )
        assert result.status_code == 409
        assert result.json()["detail"] == "image provider credential is missing"


def test_legacy_provider_is_backfilled_as_active_binding(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE alembic_version (version_num VARCHAR(64) PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('0008_provider_domains');
        CREATE TABLE providers (
            id VARCHAR(64) PRIMARY KEY,
            modality VARCHAR(32) NOT NULL,
            enabled BOOLEAN NOT NULL
        );
        INSERT INTO providers VALUES ('legacy-text', 'text', 1);
        CREATE TABLE active_provider_bindings (
            id VARCHAR(160) PRIMARY KEY,
            scope VARCHAR(32) NOT NULL,
            scope_id VARCHAR(64),
            modality VARCHAR(32) NOT NULL,
            provider_id VARCHAR(64) NOT NULL,
            version INTEGER NOT NULL,
            updated_at DATETIME
        );
        """
    )
    connection.commit()
    connection.close()
    upgrade_database(database, kind="global")
    connection = sqlite3.connect(database)
    binding = connection.execute(
        "SELECT modality, provider_id FROM active_provider_bindings"
    ).fetchone()
    revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    connection.close()
    assert binding == ("text", "legacy-text")
    assert revision == ("0015_document_presentation",)
