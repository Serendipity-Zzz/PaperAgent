from __future__ import annotations

import pytest
from pydantic import ValidationError

from paperagent.workspace.contracts import (
    ActiveProviderBinding,
    ImpactLevel,
    MessageStatus,
    ProviderConfig,
    ProviderModality,
    ResponseMode,
    RunStatus,
    SteeringAction,
    SteeringEnvelope,
    SteeringRelationship,
    WorkspaceEvent,
    ensure_message_transition,
    ensure_run_transition,
    ensure_workspace_event_sequence,
)


def test_run_and_message_state_machines_reject_terminal_reentry() -> None:
    ensure_run_transition(RunStatus.QUEUED, RunStatus.RUNNING)
    ensure_message_transition(MessageStatus.STREAMING, MessageStatus.FINAL)
    with pytest.raises(ValueError, match="illegal run transition"):
        ensure_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
    with pytest.raises(ValueError, match="illegal message transition"):
        ensure_message_transition(MessageStatus.CANCELLED, MessageStatus.FINAL)


def test_provider_contract_never_accepts_inline_api_key() -> None:
    provider = ProviderConfig(
        id="deepseek-default",
        display_name="DeepSeek",
        modality=ProviderModality.TEXT,
        protocol="openai_compatible",
        base_url="https://api.example.invalid/v1",
        model_name="model",
        secret_ref="credential:deepseek-default",
    )
    assert "api_key" not in provider.model_dump()
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProviderConfig.model_validate(provider.model_dump() | {"api_key": "not-allowed"})


def test_provider_binding_scope_is_explicit() -> None:
    ActiveProviderBinding(
        scope="global",
        modality=ProviderModality.TEXT,
        provider_config_id="provider-1",
    )
    with pytest.raises(ValidationError, match="requires scope_id"):
        ActiveProviderBinding(
            scope="project",
            modality=ProviderModality.IMAGE,
            provider_config_id="image-1",
        )


@pytest.mark.parametrize(
    ("level", "action", "checkpoint"),
    [
        (ImpactLevel.L0, SteeringAction.NONE, None),
        (ImpactLevel.L1, SteeringAction.NONE, None),
        (ImpactLevel.L2, SteeringAction.INJECT_AT_BOUNDARY, None),
        (ImpactLevel.L3, SteeringAction.REPLAN_REMAINING, None),
        (ImpactLevel.L4, SteeringAction.FORK_FROM_CHECKPOINT, "checkpoint-4"),
        (ImpactLevel.L5, SteeringAction.CANCEL, None),
    ],
)
def test_steering_levels_compile_to_constrained_actions(
    level: ImpactLevel, action: SteeringAction, checkpoint: str | None
) -> None:
    envelope = SteeringEnvelope(
        target_run_id="run-a",
        response_mode=ResponseMode.SIDECAR,
        relationship=SteeringRelationship.CORRECTION,
        impact_level=level,
        action_on_a=action,
        earliest_affected_checkpoint=checkpoint,
        confidence=0.9,
        rationale_summary="Public decision summary",
    )
    assert envelope.action_on_a is action


def test_steering_rejects_incompatible_action_and_overlap() -> None:
    with pytest.raises(ValidationError, match="requires action_on_a"):
        SteeringEnvelope(
            target_run_id="run-a",
            response_mode=ResponseMode.ACKNOWLEDGE,
            relationship=SteeringRelationship.STOP,
            impact_level=ImpactLevel.L5,
            action_on_a=SteeringAction.NONE,
            confidence=1,
            rationale_summary="Stop requested",
        )
    with pytest.raises(ValidationError, match="must be disjoint"):
        SteeringEnvelope(
            target_run_id="run-a",
            response_mode=ResponseMode.NO_IMMEDIATE_REPLY,
            relationship=SteeringRelationship.CONSTRAINT_CHANGE,
            impact_level=ImpactLevel.L3,
            action_on_a=SteeringAction.REPLAN_REMAINING,
            affected_nodes=("writer",),
            preserved_nodes=("writer",),
            confidence=0.8,
            rationale_summary="Constraint changed",
        )


def test_workspace_events_are_redacted_and_strictly_ordered() -> None:
    events = [
        WorkspaceEvent(
            run_id="run-a",
            sequence=index,
            event_type="model.completed",
            public_payload={"api_key": "secret", "message": "used sk-abcdefgh1234"},
        )
        for index in (1, 2)
    ]
    ensure_workspace_event_sequence(events)
    assert events[0].public_payload == {
        "api_key": "[REDACTED]",
        "message": "used [REDACTED]",
    }
    with pytest.raises(ValueError, match="strictly increasing"):
        ensure_workspace_event_sequence([events[1], events[0]])
