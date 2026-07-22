from __future__ import annotations

from pathlib import Path

import pytest

from paperagent.recovery import (
    FaultInjector,
    ProviderCallGuard,
    RecoveryService,
    SideEffectAction,
    SideEffectState,
    SideEffectStore,
)
from paperagent.recovery.service import InjectedFault


@pytest.mark.parametrize("action", list(SideEffectAction))
def test_every_side_effect_type_persists_intent_and_result(
    tmp_path: Path, action: SideEffectAction
) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    record = store.intent("project-a", action, f"key-{action}", f"run {action}")
    result = store.transition(
        record.id, SideEffectState.SUCCEEDED, result={"ok": True}, checkpoint="done"
    )
    assert result.state is SideEffectState.SUCCEEDED
    assert result.checkpoint == "done"


def test_intent_is_idempotent_and_invalid_transition_is_rejected(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    first = store.intent("project-a", SideEffectAction.FILE, "same", "atomic write")
    second = store.intent("project-a", SideEffectAction.FILE, "same", "atomic write")
    assert first.id == second.id
    store.transition(first.id, SideEffectState.SUCCEEDED)
    with pytest.raises(ValueError):
        store.transition(first.id, SideEffectState.RUNNING)


def test_recovery_center_never_marks_paid_or_code_as_auto_retry(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    paid = store.intent("p", SideEffectAction.API, "paid", "LLM", paid=True)
    code = store.intent("p", SideEffectAction.EXPERIMENT, "code", "experiment")
    normal = store.intent("p", SideEffectAction.RENDER, "render", "render")
    center = RecoveryService(store).center("p")
    by_id = {item["id"]: item for item in center["pending"]}  # type: ignore[index]
    assert by_id[paid.id]["automatic_retry_safe"] is False
    assert by_id[code.id]["automatic_retry_safe"] is False
    assert by_id[normal.id]["automatic_retry_safe"] is True


def test_paid_transport_loss_is_unknown_and_requires_explicit_decision(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    guard = ProviderCallGuard(store)
    record, value = guard.call(
        "p",
        lambda _request_id: (_ for _ in ()).throw(TimeoutError("after dispatch")),
        idempotency_key="provider-1",
        description="generate",
        estimated_cost=0.25,
    )
    assert value is None
    assert record.state is SideEffectState.UNKNOWN
    assert record.request_id
    assert record.automatic_retry_safe is False
    retried = RecoveryService(store).decide(record.id, "retry")
    assert retried.state is SideEffectState.RUNNING


def test_old_graph_version_is_visible_but_not_resumable_in_view(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    record = store.intent("p", SideEffectAction.FILE, "old", "old graph", graph_version=9)
    center = RecoveryService(store, current_graph_version=2).center("p")
    assert center["pending"][0]["id"] == record.id  # type: ignore[index]
    assert center["pending"][0]["graph_compatible"] is False  # type: ignore[index]


def test_recovery_center_can_be_scoped_to_one_task_trace(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    selected = store.intent("p", SideEffectAction.FILE, "trace-a:render:pdf", "selected task")
    unrelated = store.intent("p", SideEffectAction.FILE, "trace-b:render:pdf", "old task")
    store.transition(selected.id, SideEffectState.FAILED)
    store.transition(unrelated.id, SideEffectState.FAILED)
    center = RecoveryService(store).center("p", trace_id="trace-a")
    pending = center["pending"]
    assert isinstance(pending, list)
    assert [item["id"] for item in pending] == [selected.id]
    assert center["scope"] == "task"


def test_completed_task_keeps_repaired_failure_as_audit_not_pending(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    failed = store.intent("p", SideEffectAction.FILE, "trace-a:attempt-1", "first strategy")
    succeeded = store.intent("p", SideEffectAction.FILE, "trace-a:attempt-2", "repair strategy")
    store.transition(failed.id, SideEffectState.FAILED)
    store.transition(succeeded.id, SideEffectState.SUCCEEDED)

    center = RecoveryService(store).center(
        "p",
        trace_id="trace-a",
        task_completed=True,
    )

    assert center["pending"] == []
    assert center["requires_attention"] is False
    assert [item["id"] for item in center["resolved_failures"]] == [failed.id]  # type: ignore[index]


def test_fault_hook_is_deterministic(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    with pytest.raises(InjectedFault, match="after_intent"):
        store.intent(
            "p",
            SideEffectAction.FILE,
            "crash",
            "write",
            injector=FaultInjector({"after_intent"}, seed=7),
        )
    assert store.list("p")[0].state is SideEffectState.INTENT
