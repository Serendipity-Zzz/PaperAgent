from pathlib import Path

import pytest

from paperagent.privacy import PrivacyMode, PrivacyPolicy
from paperagent.recovery import RecoveryService, SideEffectAction, SideEffectState, SideEffectStore


@pytest.mark.security
@pytest.mark.parametrize("mode", list(PrivacyMode))
def test_privacy_modes_never_leak_secret_in_blocked_or_controlled_output(mode: PrivacyMode) -> None:
    policy = PrivacyPolicy(mode)
    preview = policy.preview(
        "provider", "chat", "email=a@example.com api_key=" + "sec" + "ret-value"
    )
    if mode is PrivacyMode.STANDARD:
        assert preview.allowed
    elif mode is PrivacyMode.CONTROLLED:
        assert "a@example.com" not in preview.content
        assert "value" not in preview.content
    else:
        assert not preview.allowed and preview.content == ""


@pytest.mark.security
def test_unknown_delete_cannot_be_silently_replayed(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    record = store.intent("p", SideEffectAction.DELETE, "delete", "delete project")
    store.transition(record.id, SideEffectState.UNKNOWN)
    center = RecoveryService(store).center("p")
    assert center["pending"][0]["automatic_retry_safe"] is False  # type: ignore[index]
    assert store.get(record.id).state is SideEffectState.UNKNOWN
