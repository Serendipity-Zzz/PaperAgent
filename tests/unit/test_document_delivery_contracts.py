from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.orchestration.interactive import CapabilityPlanFactory
from paperagent.rendering.asset_assembly import AssetBarrier
from paperagent.rendering.delivery import (
    AssetBarrierResult,
    AssetBarrierStatus,
    AssetRequirementManifest,
    DeliveryIssueCategory,
    DeliveryValidationIssue,
    DeliveryValidationResult,
    DocumentAction,
    DocumentActionIntent,
    DocumentFormat,
    RequiredAsset,
    RequiredAssetKind,
)


def test_document_delivery_contracts_round_trip_and_enforce_invariants() -> None:
    intent = DocumentActionIntent(
        action=DocumentAction.CONVERT_FORMAT,
        target_formats=[DocumentFormat.PDF],
        preserve_content=False,
        preserve_assets=False,
        rerun_experiment=True,
        confidence=0.94,
        evidence=["用户要求把已有结果转成 PDF"],
    )
    assert intent.preserve_content
    assert intent.preserve_assets
    assert not intent.rerun_experiment

    manifest = AssetRequirementManifest(
        document_id=uuid4(),
        revision=1,
        image_required=True,
        required_assets=[
            RequiredAsset(
                logical_id=f"figure-{index}",
                kind=RequiredAssetKind.FIGURE,
                expected_filename=f"figure-{index}.png",
                order=index,
            )
            for index in range(5)
        ],
    )
    assert manifest.required_count == 5
    assert manifest.required_figure_count == 5

    ready = AssetBarrierResult(
        status=AssetBarrierStatus.READY,
        expected_count=5,
        bound_count=5,
        ready_count=5,
    )
    assert ready.ready
    with pytest.raises(ValidationError, match="requires every expected asset"):
        AssetBarrierResult(
            status=AssetBarrierStatus.READY,
            expected_count=5,
            bound_count=0,
            ready_count=0,
        )

    issue = DeliveryValidationIssue(
        category=DeliveryIssueCategory.MISSING_ASSET,
        document_id=manifest.document_id,
        revision=1,
        message="PDF embedded 0 of 5 required figures",
        repair_node="asset.resolve",
    )
    failed = DeliveryValidationResult(passed=False, issues=[issue])
    assert not failed.passed


def test_format_conversion_plan_cannot_use_writer_or_experiment() -> None:
    plan = CapabilityPlanFactory().build(
        "将结果给我转换成 PDF,不要重新运行实验",
        requirement_id=uuid4(),
        available_tools=list(ExecutionToolSuite.TOOL_NAMES),
    )
    ids = [node.node_id for node in plan.nodes]
    assert "document_resolve_revision" in ids
    assert "document_asset_barrier" in ids
    assert "document_render" in ids
    assert "document_validate_delivery" in ids
    assert all(node.agent_type not in {"writer_agent", "experiment_agent"} for node in plan.nodes)


@pytest.mark.parametrize(
    ("user_text", "action", "formats"),
    [
        ("给我 Word 版", DocumentAction.CONVERT_FORMAT, [DocumentFormat.DOCX]),
        ("整理成一个可打印版本", DocumentAction.CONVERT_FORMAT, [DocumentFormat.PDF]),
        (
            "导出 Markdown 和 PDF",
            DocumentAction.CONVERT_FORMAT,
            [DocumentFormat.PDF, DocumentFormat.MARKDOWN],
        ),
        ("写一份驻波报告并导出 PDF", DocumentAction.CREATE, [DocumentFormat.PDF]),
        (
            "重新运行实验再导出 PDF",
            DocumentAction.RERUN_EXPERIMENT,
            [DocumentFormat.PDF],
        ),
        ("把刚才的报告重新排版为宋体", DocumentAction.RESTYLE, []),
    ],
)
def test_document_intent_classifier_handles_open_conversion_language(
    user_text: str,
    action: DocumentAction,
    formats: list[DocumentFormat],
) -> None:
    from paperagent.orchestration.document_delivery import DocumentIntentClassifier

    intent = DocumentIntentClassifier().classify(user_text)
    assert intent.action is action
    assert intent.target_formats == formats


def test_image_required_empty_asset_barrier_is_not_ready() -> None:
    barrier = object.__new__(AssetBarrier)
    state = barrier.evaluate([], image_required=True)
    assert not state.ready
    assert state.repair_code == "ASSET_MISSING"


def test_document_render_tool_requires_canonical_revision_identity() -> None:
    spec = next(item for item in ExecutionToolSuite.specs() if item.name == "document.render")
    properties = spec.input_schema["properties"]
    required = set(spec.input_schema["required"])
    assert "document_ir" not in properties
    assert {"document_id", "revision", "format"} <= required
