from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperagent.orchestration.delivery_recovery import (
    DeliveryCheckpoint,
    DeliveryCheckpointStore,
    DeliveryRecoveryRouter,
    compile_delivery_recovery_graph,
)
from paperagent.orchestration.failure import (
    FailureAnalyzer,
    FailureCategory,
    RecoveryAction,
)


@pytest.mark.parametrize(
    ("message", "category", "resume_node"),
    [
        ("revision not found", FailureCategory.MISSING_REVISION, "document_resolve_revision"),
        (
            "ambiguous revision candidates",
            FailureCategory.AMBIGUOUS_REVISION,
            "document_resolve_revision",
        ),
        ("DocumentIR block tree is invalid", FailureCategory.STRUCTURE_ERROR, "document_compose"),
        (
            "required image is not embedded in PDF",
            FailureCategory.MISSING_ASSET,
            "document_asset_barrier",
        ),
        ("asset barrier pending", FailureCategory.PENDING_ASSET, "document_asset_barrier"),
        ("ambiguous figure asset", FailureCategory.AMBIGUOUS_ASSET, "document_asset_barrier"),
        ("image hash mismatch", FailureCategory.INVALID_ASSET, "document_asset_barrier"),
        ("asset derivative failed", FailureCategory.DERIVATIVE_FAILED, "document_asset_derive"),
        ("XeLaTeX compile error", FailureCategory.COMPILE_ERROR, "document_render"),
        ("layout overflow at figure", FailureCategory.LAYOUT_ERROR, "document_layout_resolve"),
        (
            "delivery validation QA failed",
            FailureCategory.VALIDATION_ERROR,
            "document_validate_delivery",
        ),
    ],
)
def test_delivery_failure_routes_to_minimum_responsible_node(
    message: str, category: FailureCategory, resume_node: str
) -> None:
    failure = FailureAnalyzer.analyze(
        "document_validate_delivery",
        RuntimeError(message),
        document_id="doc-1",
        revision=2,
        input_hash="a" * 64,
    )
    decision = DeliveryRecoveryRouter().planner.decide(failure)
    assert failure.category is category
    assert decision.resume_node == resume_node
    assert not (category is FailureCategory.MISSING_ASSET and decision.retry_node)


def test_failure_fingerprint_is_stable_redacted_and_revision_scoped() -> None:
    first = FailureAnalyzer.analyze(
        "render",
        RuntimeError("required image 123 missing api_key=" + "sk-" + "fixture-token-123"),
        tool_name="document.render",
        document_id="doc",
        revision=4,
        input_hash="b" * 64,
    )
    second = FailureAnalyzer.analyze(
        "render",
        RuntimeError("required image 999 missing api_key=" + "sk-" + "fixture-token-999"),
        tool_name="document.render",
        document_id="doc",
        revision=4,
        input_hash="b" * 64,
    )
    changed_revision = second.model_copy(update={"revision": 5})
    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() != changed_revision.fingerprint()
    assert "fixture-token" not in first.message


def test_checkpoint_is_atomic_migrates_and_deduplicates_strategy(tmp_path: Path) -> None:
    store = DeliveryCheckpointStore(tmp_path / "checkpoints")
    checkpoint = DeliveryCheckpoint(
        project_id="p",
        task_id="t",
        document_id="d",
        revision=1,
        canonical_artifact_id="canonical",
        requested_formats=("pdf", "docx"),
        delivered_formats={"docx": "artifact-docx"},
    )
    store.save(checkpoint)
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))
    loaded = store.load("t")
    assert loaded is not None and loaded.delivered_formats == {"docx": "artifact-docx"}

    legacy = loaded.model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("strategy_history")
    (tmp_path / "checkpoints" / "t.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert store.load("t") is not None

    failure = FailureAnalyzer.analyze("render", RuntimeError("XeLaTeX compile error"))
    first, updated = DeliveryRecoveryRouter().route(failure, loaded)
    second, _ = DeliveryRecoveryRouter().route(failure, updated)
    assert first.action is RecoveryAction.REPAIR_OUTPUT
    assert second.action is RecoveryAction.HUMAN_TAKEOVER


def test_executable_langgraph_routes_human_and_automatic_branches() -> None:
    checkpoint = DeliveryCheckpoint(
        project_id="p",
        task_id="t",
        document_id="d",
        revision=1,
        canonical_artifact_id="canonical",
    )
    graph = compile_delivery_recovery_graph()
    automatic = graph.invoke(
        {
            "failure": FailureAnalyzer.analyze("render", RuntimeError("XeLaTeX compile error")),
            "checkpoint": checkpoint,
        }
    )
    human = graph.invoke(
        {
            "failure": FailureAnalyzer.analyze("resolve", RuntimeError("ambiguous revision")),
            "checkpoint": checkpoint,
        }
    )
    assert automatic["resume_node"] == "document_render"
    assert human["resume_node"] == "human_takeover"
