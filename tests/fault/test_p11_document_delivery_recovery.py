from __future__ import annotations

from pathlib import Path

import pytest

from paperagent.orchestration.delivery_recovery import DeliveryCheckpoint, DeliveryCheckpointStore
from paperagent.orchestration.failure import FailureAnalyzer, FailureCategory, RecoveryPlanner
from paperagent.recovery import SideEffectAction, SideEffectState, SideEffectStore


@pytest.mark.fault
def test_unknown_successful_experiment_is_reconciled_not_executed_twice(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    record = store.intent("p", SideEffectAction.EXPERIMENT, "experiment:v1", "run experiment")
    store.transition(record.id, SideEffectState.RUNNING)
    store.transition(record.id, SideEffectState.UNKNOWN, result={"process_id": 42})
    executions = 1

    reconciled = store.reconcile(
        record.id,
        lambda _: (SideEffectState.SUCCEEDED, {"artifact_hash": "abc", "executions": executions}),
    )
    replay = store.intent("p", SideEffectAction.EXPERIMENT, "experiment:v1", "run experiment")
    assert reconciled.state is SideEffectState.SUCCEEDED
    assert replay.id == record.id
    assert replay.result["executions"] == 1


@pytest.mark.fault
@pytest.mark.parametrize(
    "resume_node", ["document_asset_barrier", "document_render", "document_validate_delivery"]
)
def test_process_restart_loads_last_atomic_delivery_boundary(
    tmp_path: Path, resume_node: str
) -> None:
    root = tmp_path / "project" / "checkpoints" / "delivery"
    store = DeliveryCheckpointStore(root)
    store.save(
        DeliveryCheckpoint(
            project_id="p",
            task_id="t",
            document_id="d",
            revision=3,
            canonical_artifact_id="canonical",
            manifest={"required": 5},
            bindings={"figure-1": "artifact-1"},
            requested_formats=("pdf", "docx", "markdown"),
            delivered_formats={"docx": "delivered-docx"},
            qa_results={"docx": {"status": "passed"}},
            idempotency_keys={"experiment": "experiment:v1"},
            safe_resume_node=resume_node,
        )
    )
    restarted = DeliveryCheckpointStore(root).load("t")
    assert restarted is not None
    assert restarted.safe_resume_node == resume_node
    assert restarted.delivered_formats == {"docx": "delivered-docx"}
    assert restarted.idempotency_keys["experiment"] == "experiment:v1"


@pytest.mark.fault
@pytest.mark.parametrize(
    ("error", "category"),
    [
        (RuntimeError("asset barrier pending"), FailureCategory.PENDING_ASSET),
        (RuntimeError("image file corrupt"), FailureCategory.INVALID_ASSET),
        (RuntimeError("asset derivative failed"), FailureCategory.DERIVATIVE_FAILED),
        (RuntimeError("XeLaTeX compile error"), FailureCategory.COMPILE_ERROR),
        (RuntimeError("layout overflow at figure"), FailureCategory.LAYOUT_ERROR),
        (OSError("disk full"), FailureCategory.RESOURCE),
        (ConnectionError("API connection reset"), FailureCategory.TRANSIENT),
    ],
)
def test_fault_class_has_a_bounded_non_blind_strategy(
    error: Exception, category: FailureCategory
) -> None:
    failure = FailureAnalyzer.analyze("document_render", error)
    decision = RecoveryPlanner().decide(failure)
    assert failure.category is category
    assert (
        decision.strategy != "exponential_backoff_with_jitter"
        or category is FailureCategory.TRANSIENT
    )
