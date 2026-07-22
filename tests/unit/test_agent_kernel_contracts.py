from uuid import uuid4

import pytest
from pydantic import ValidationError

from paperagent.context import ContextItem, ContextItemKind, ContextPack
from paperagent.engine import (
    BudgetDecision,
    BudgetLimits,
    BudgetUsage,
    EngineEvent,
    EngineEventKind,
    TurnRequest,
    ensure_event_sequence,
)
from paperagent.orchestration import CandidateEdge, CandidateNode, CandidatePlan
from paperagent.tools import (
    ConcurrencyPolicy,
    SideEffect,
    ToolCall,
    ToolError,
    ToolResult,
    ToolResultStatus,
    ToolSpec,
)


def limits() -> BudgetLimits:
    return BudgetLimits(max_input_tokens=8_000, max_output_tokens=2_000, max_cost=1)


def test_turn_event_round_trip_redaction_and_sequence() -> None:
    trace_id = uuid4()
    request = TurnRequest(
        trace_id=trace_id,
        project_id="project-1",
        thread_id="thread-1",
        task_id="task-1",
        message_id="message-1",
        user_message="写一篇报告",
        idempotency_key="turn-1",
    )
    assert TurnRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError, match="Extra inputs"):
        TurnRequest.model_validate(request.model_dump() | {"unknown": True})

    first = EngineEvent(
        trace_id=trace_id,
        project_id="project-1",
        thread_id="thread-1",
        task_id="task-1",
        sequence=1,
        kind=EngineEventKind.TURN_ACCEPTED,
        payload={"api_key": "do-not-store", "message": "token sk-abcdefghijk"},
    )
    second = first.model_copy(
        update={
            "event_id": uuid4(),
            "sequence": 2,
            "kind": EngineEventKind.GRAPH_STARTED,
        }
    )
    assert first.payload == {"api_key": "[REDACTED]", "message": "token [REDACTED]"}
    assert "do-not-store" not in first.model_dump_json()
    ensure_event_sequence([first, second])
    assert EngineEvent.model_validate_json(first.model_dump_json()) == first
    assert first.stable_hash() == first.model_copy(update={"event_id": uuid4()}).stable_hash()

    with pytest.raises(ValueError, match="strictly increasing"):
        ensure_event_sequence([second, first])
    with pytest.raises(ValueError, match="identity changed"):
        ensure_event_sequence([first, second.model_copy(update={"thread_id": "other"})])


def test_budget_decision_requires_reason_after_limit_is_exceeded() -> None:
    with pytest.raises(ValidationError, match="without decision reason"):
        BudgetDecision(
            limits=limits(),
            usage=BudgetUsage(input_tokens=8_001),
        )
    decision = BudgetDecision(
        limits=limits(),
        usage=BudgetUsage(input_tokens=8_001),
        reason="request task split",
        frozen=True,
    )
    assert BudgetDecision.model_validate_json(decision.model_dump_json()) == decision


def test_tool_contract_hash_result_invariants_and_json_round_trip() -> None:
    trace_id = uuid4()
    spec = ToolSpec(
        name="knowledge.search",
        version="1.0.0",
        description="Search project knowledge with source locators.",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object"},
        capabilities={"retrieval", "read"},
        side_effect=SideEffect.NONE,
        concurrency_policy=ConcurrencyPolicy.SAFE,
    )
    reordered = spec.model_copy(update={"capabilities": {"read", "retrieval"}})
    assert spec.schema_hash() == reordered.schema_hash()
    assert ToolSpec.model_validate_json(spec.model_dump_json()) == spec

    call = ToolCall(
        call_id="call-1",
        trace_id=trace_id,
        sequence=1,
        tool_name=spec.name,
        tool_version=spec.version,
        arguments={"query": "上下文压缩"},
        requested_by="evidence_agent",
        idempotency_key="tool-call-1",
    )
    assert ToolCall.model_validate_json(call.model_dump_json()) == call
    success = ToolResult(
        call_id=call.call_id,
        status=ToolResultStatus.SUCCESS,
        content={"hits": []},
    )
    assert success.content_hash is not None
    with pytest.raises(ValidationError, match="requires an error"):
        ToolResult(call_id=call.call_id, status=ToolResultStatus.ERROR)
    with pytest.raises(ValidationError, match="full_result_ref"):
        ToolResult(call_id=call.call_id, status=ToolResultStatus.SUCCESS, truncated=True)
    failure = ToolResult(
        call_id=call.call_id,
        status=ToolResultStatus.ERROR,
        error=ToolError(code="SCHEMA_ERROR", message="query is required", category="invalid"),
    )
    assert ToolResult.model_validate_json(failure.model_dump_json()) == failure


def test_context_pack_validates_groups_protection_and_stable_hash() -> None:
    trace_id = uuid4()
    safety = ContextItem(
        kind=ContextItemKind.SAFETY,
        source_id="policy:core",
        content="不得伪造数据。",
        estimated_tokens=8,
        protected=True,
        compressible=False,
    )
    requirement = ContextItem(
        kind=ContextItemKind.REQUIREMENT,
        source_id="requirement:r1:v1",
        content="生成实验报告。",
        estimated_tokens=12,
        protected=True,
        compressible=False,
    )
    budget = BudgetDecision(limits=limits(), selected_context_ids=[str(safety.item_id)])
    pack = ContextPack(
        trace_id=trace_id,
        safety=[safety],
        requirement=requirement,
        budget=budget,
        transcript_ref="conversations/thread-1.jsonl#12",
    )
    restored = ContextPack.model_validate_json(pack.model_dump_json())
    assert restored == pack
    assert pack.stable_hash() == pack.model_copy(update={"pack_id": uuid4()}).stable_hash()
    with pytest.raises(ValidationError, match="cannot be compressible"):
        ContextItem(
            kind=ContextItemKind.SAFETY,
            source_id="policy:bad",
            content="protected",
            estimated_tokens=1,
            protected=True,
        )
    with pytest.raises(ValidationError, match="requires kind memory"):
        ContextPack(trace_id=trace_id, memories=[safety], budget=budget)


def test_candidate_plan_contract_references_and_stable_hash() -> None:
    requirement_id = uuid4()
    plan = CandidatePlan(
        requirement_id=requirement_id,
        requirement_version=1,
        entry_node="retrieve",
        terminal_nodes={"write"},
        nodes=[
            CandidateNode(
                node_id="retrieve",
                agent_type="evidence_agent",
                objective="Retrieve citable evidence.",
                output_keys=["evidence_pack"],
                required_tools=["knowledge.search"],
            ),
            CandidateNode(
                node_id="write",
                agent_type="writer_agent",
                objective="Write from the evidence pack.",
                input_refs=["retrieve.evidence_pack"],
                output_keys=["document_ir"],
            ),
        ],
        edges=[CandidateEdge(source="retrieve", target="write")],
        limits=limits(),
        rationale="Evidence must be ready before writing.",
    )
    restored = CandidatePlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert plan.stable_hash() == plan.model_copy(update={"plan_id": uuid4()}).stable_hash()
    with pytest.raises(ValidationError, match="unknown node"):
        CandidatePlan.model_validate(
            plan.model_dump(mode="json")
            | {"edges": [{"source": "retrieve", "target": "missing"}]}
        )
