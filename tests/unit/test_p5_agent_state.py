from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from paperagent.agents.state import (
    AgentState,
    AgentStateCheckpoint,
    DocumentType,
    GraphInterrupt,
    InterruptKind,
    NodeDefinition,
    OutputFormat,
    PrimaryLanguage,
    RawRequest,
    RequirementSpec,
    RequirementStatus,
    RequirementVersionHistory,
    TargetLength,
    TaskEdge,
    TaskGraph,
    migrate_checkpoint,
)


def complete_requirement() -> RequirementSpec:
    return RequirementSpec(
        raw_request=RawRequest(text="写一篇本地智能体实验报告", message_ids=("m1",)),
        normalized_request="撰写一篇关于本地智能体的实验报告。",
        document_type=DocumentType.EXPERIMENT_REPORT,
        primary_language=PrimaryLanguage.ZH,
        target_length=TargetLength(value=5_000, unit="chinese_char"),
        audience="计算机专业本科生",
        citation_style="GB/T 7714",
        requires_literature_search=True,
        requires_experiment=True,
        requires_data_chart=True,
        requires_generated_image=False,
        output_formats=[OutputFormat.DOCX, OutputFormat.PDF],
        acceptance_criteria=["实验可复现"],
    )


def graph() -> TaskGraph:
    return TaskGraph(
        entry_node="understand",
        terminal_nodes={"write"},
        nodes=[
            NodeDefinition(
                node_id="understand",
                agent_type="requirement",
                input_keys=("raw_request",),
                output_keys=("requirement_spec",),
            ),
            NodeDefinition(
                node_id="write",
                agent_type="writer",
                input_keys=("requirement_spec",),
                output_keys=("document_ir",),
                requires_confirmed_requirement=True,
            ),
        ],
        edges=[TaskEdge(source="understand", target="write")],
    )


def state(requirement: RequirementSpec) -> AgentState:
    return AgentState(
        project_id="project-1",
        thread_id="thread-1",
        task_id="task-1",
        graph=graph(),
        requirement_history=RequirementVersionHistory(
            requirement_id=requirement.requirement_id,
            versions=[requirement],
        ),
    )


def test_four_layer_requirement_confirmation_round_trip_and_raw_immutability() -> None:
    draft = complete_requirement()
    confirmed = draft.confirm(at=datetime(2026, 7, 16, tzinfo=UTC))
    assert confirmed.status is RequirementStatus.CONFIRMED
    assert confirmed.confirmed_requirement is not None
    assert confirmed.confirmed_requirement.content_hash
    assert confirmed.raw_request.text == "写一篇本地智能体实验报告"
    with pytest.raises(ValidationError):
        confirmed.raw_request.text = "overwrite"  # type: ignore[misc]
    restored = RequirementSpec.model_validate_json(confirmed.model_dump_json())
    assert restored == confirmed


def test_invalid_requirement_history_confirmation_and_graph_are_rejected() -> None:
    draft = complete_requirement()
    with pytest.raises(ValidationError, match="frozen confirmed"):
        RequirementSpec.model_validate(draft.model_dump() | {"status": "confirmed"})
    with pytest.raises(ValidationError, match="open question or conflict"):
        RequirementSpec.model_validate(draft.model_dump() | {"status": "needs_input"})
    second = draft.model_copy(update={"requirement_version": 2})
    with pytest.raises(ValidationError, match="exactly one active"):
        RequirementVersionHistory(
            requirement_id=draft.requirement_id,
            versions=[draft, second],
        )
    with pytest.raises(ValidationError, match="unknown node"):
        TaskGraph(
            entry_node="understand",
            terminal_nodes={"understand"},
            nodes=[graph().nodes[0]],
            edges=[TaskEdge(source="understand", target="missing")],
        )


def test_node_execution_is_idempotent_and_confirmed_only() -> None:
    draft_state = state(complete_requirement())
    with pytest.raises(PermissionError, match="confirmed"):
        draft_state.begin_node("write", "write-v1", {"requirement_spec": {}})

    confirmed_state = state(complete_requirement().confirm())
    assert confirmed_state.begin_node("write", "write-v1", {"requirement_spec": {"version": 1}})
    assert not confirmed_state.begin_node("write", "write-v1", {"requirement_spec": {"version": 1}})
    with pytest.raises(ValueError, match="undeclared"):
        confirmed_state.complete_node("write", {"unexpected": True})
    confirmed_state.complete_node("write", {"document_ir": {"id": "doc-1"}})
    assert not confirmed_state.begin_node("write", "write-v1", {})


def test_approval_interrupt_resume_and_checkpoint_hash() -> None:
    agent_state = state(complete_requirement().confirm())
    interrupt = GraphInterrupt(
        kind=InterruptKind.APPROVAL,
        node_id="write",
        action="paid_model_call",
        scope={"provider": "mock"},
        prompt="是否允许调用模型?",
    )
    agent_state.pause(interrupt)
    assert agent_state.status == "paused"
    checkpoint = AgentStateCheckpoint(state=agent_state)
    restored = AgentStateCheckpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored.state.pending_interrupt is not None
    assert restored.state.requirement_history.versions[-1].status == "confirmed"
    restored.state.resume(interrupt.interrupt_id, approved=True)
    assert restored.state.status == "running"
    with pytest.raises(KeyError, match="not found"):
        restored.state.resume(interrupt.interrupt_id, approved=True)

    invalid = checkpoint.model_dump(mode="json")
    invalid["state_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="hash mismatch"):
        AgentStateCheckpoint.model_validate(invalid)


def test_legacy_checkpoint_migration_preserves_requirement_version() -> None:
    requirement = complete_requirement()
    legacy = {
        "schema_version": "0.1",
        "checkpoint_id": str(uuid4()),
        "state": {
            "project_id": "project-1",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "graph": graph().model_dump(mode="json", exclude={"graph_version"}),
            "requirement": requirement.model_dump(mode="json"),
        },
    }
    migrated = migrate_checkpoint(legacy)
    current = migrated.state.requirement_history.versions[-1]
    assert migrated.schema_version == "1.0"
    assert migrated.state.graph.graph_version == "1.0"
    assert current.requirement_id == requirement.requirement_id
    assert current.requirement_version == 1
