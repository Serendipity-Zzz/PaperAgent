from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_provider_url_model_key_are_persisted_and_connection_is_callable(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"u" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        saved = client.post(
            "/api/settings/providers",
            headers=headers,
            json={
                "id": "mock-ui-contract",
                "provider_type": "mock",
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "mock-configured-model",
                "api_key": "fixture-key-not-a-real-secret",
                "capabilities": ["chat", "stream", "tools", "structured_output"],
            },
        )
        assert saved.status_code == 200
        assert saved.json()["has_credential"] is True
        listed = client.get("/api/settings/providers", headers=headers).json()[0]
        assert listed["base_url"] == "http://127.0.0.1:9999/v1"
        assert listed["model"] == "mock-configured-model"
        assert "api_key" not in listed and listed["has_credential"] is True
        tested = client.post(
            "/api/providers/mock-ui-contract/test",
            headers=headers,
            json={"confirmation": "TEST PROVIDER"},
        )
        assert tested.status_code == 200
        assert tested.json()["model"] == "mock-configured-model"
