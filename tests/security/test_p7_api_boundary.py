from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.security import LocalSessionTokens


def test_api_404_is_not_rewritten_to_spa_and_auth_cannot_be_bypassed(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"s" * 32)
    with TestClient(create_app(settings, tokens)) as client:
        missing = client.get("/api/route-that-does-not-exist")
        assert missing.status_code == 404
        assert "text/html" not in missing.headers.get("content-type", "")
        assert client.get("/api/projects").status_code == 401
        invalid = client.get(
            "/api/projects", headers={"Authorization": "Bearer invalid"}
        )
        assert invalid.status_code == 401
