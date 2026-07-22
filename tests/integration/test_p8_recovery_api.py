from pathlib import Path

from fastapi.testclient import TestClient

from paperagent.api import create_app
from paperagent.core.config import Settings
from paperagent.recovery import SideEffectAction, SideEffectState, SideEffectStore
from paperagent.security import LocalSessionTokens


def test_recovery_and_first_run_api_requires_explicit_resume(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    tokens = LocalSessionTokens(secret=b"z" * 32)
    store = SideEffectStore(settings.resolved_data_dir / "global" / "recovery.db")
    record = store.intent(
        "project-a", SideEffectAction.API, "ambiguous", "paid call", paid=True, estimated_cost=1.5
    )
    store.transition(record.id, SideEffectState.UNKNOWN)
    with TestClient(create_app(settings, tokens)) as client:
        headers = {"Authorization": f"Bearer {tokens.issue()}"}
        center = client.get("/api/recovery", headers=headers)
        assert center.status_code == 200
        assert center.json()["pending"][0]["automatic_retry_safe"] is False
        assert store.get(record.id).state is SideEffectState.UNKNOWN
        resumed = client.post(
            f"/api/recovery/{record.id}/decision", json={"decision": "retry"}, headers=headers
        )
        assert resumed.json() == {"id": record.id, "state": "running", "explicit_user_action": True}
        first_run = client.get("/api/first-run", headers=headers).json()
        assert first_run["completed"] is False
        assert first_run["disk"]["writable"] is True
        install_plan = client.post(
            "/api/first-run/dependencies/plan",
            headers=headers,
            json={"tool": "typst", "confirmed": False},
        )
        assert install_plan.status_code == 200
        assert install_plan.json()["source"] == "Typst.Typst"
        refused = client.post(
            "/api/first-run/dependencies/install",
            headers=headers,
            json={"tool": "typst", "confirmed": False},
        )
        assert refused.status_code == 409
        completed = client.post(
            "/api/first-run/complete",
            json={"privacy_mode": "offline", "providers_configured": False, "skipped": ["all"]},
            headers=headers,
        )
        assert completed.json()["privacy_mode"] == "offline"
