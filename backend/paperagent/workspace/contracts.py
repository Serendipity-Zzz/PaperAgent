from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.schemas.common import SCHEMA_VERSION, StrictModel

WORKSPACE_SCHEMA_VERSION = "2.0"
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|password|secret|token)", re.I)
_SECRET_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class MessageStatus(StrEnum):
    STREAMING = "streaming"
    FINAL = "final"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    ERROR = "error"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_RESOURCE = "waiting_resource"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class RunKind(StrEnum):
    PRIMARY = "primary"
    SIDECAR = "sidecar"
    REPAIR = "repair"
    RENDER = "render"
    EXPORT = "export"


class ProviderModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    EMBEDDING = "embedding"


class ResponseMode(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    SIDECAR = "sidecar"
    NO_IMMEDIATE_REPLY = "no_immediate_reply"


class SteeringRelationship(StrEnum):
    INDEPENDENT = "independent"
    QUERY_ABOUT_RUN = "query_about_run"
    SUPPLEMENT = "supplement"
    CONSTRAINT_CHANGE = "constraint_change"
    CORRECTION = "correction"
    REPLACEMENT = "replacement"
    STOP = "stop"


class ImpactLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"
    L5 = "L5"


class SteeringAction(StrEnum):
    NONE = "none"
    INJECT_AT_BOUNDARY = "inject_at_boundary"
    REPLAN_REMAINING = "replan_remaining"
    FORK_FROM_CHECKPOINT = "fork_from_checkpoint"
    CANCEL = "cancel"


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCELLING}),
    RunStatus.PLANNING: frozenset(
        {RunStatus.RUNNING, RunStatus.WAITING_USER, RunStatus.FAILED, RunStatus.CANCELLING}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_USER,
            RunStatus.WAITING_RESOURCE,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLING,
            RunStatus.SUPERSEDED,
        }
    ),
    RunStatus.WAITING_USER: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLING, RunStatus.SUPERSEDED}
    ),
    RunStatus.WAITING_RESOURCE: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLING, RunStatus.SUPERSEDED}
    ),
    RunStatus.CANCELLING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED, RunStatus.CANCELLING}),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.SUPERSEDED: frozenset(),
}

MESSAGE_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.STREAMING: frozenset(
        {
            MessageStatus.FINAL,
            MessageStatus.CANCELLED,
            MessageStatus.SUPERSEDED,
            MessageStatus.ERROR,
        }
    ),
    MessageStatus.FINAL: frozenset({MessageStatus.SUPERSEDED}),
    MessageStatus.CANCELLED: frozenset(),
    MessageStatus.SUPERSEDED: frozenset(),
    MessageStatus.ERROR: frozenset(),
}


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise ValueError(f"illegal run transition: {current.value} -> {target.value}")


def ensure_message_transition(current: MessageStatus, target: MessageStatus) -> None:
    if target not in MESSAGE_TRANSITIONS[current]:
        raise ValueError(f"illegal message transition: {current.value} -> {target.value}")


class ProviderConfig(StrictModel):
    schema_version: str = WORKSPACE_SCHEMA_VERSION
    id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)
    modality: ProviderModality
    protocol: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=2048)
    model_name: str = Field(min_length=1, max_length=255)
    secret_ref: str = Field(min_length=1, max_length=255)
    capability_flags: frozenset[str] = frozenset()
    enabled: bool = True
    health_status: str = "unknown"
    version: int = Field(default=1, ge=1)


class ActiveProviderBinding(StrictModel):
    schema_version: str = WORKSPACE_SCHEMA_VERSION
    scope: str = Field(pattern=r"^(global|project)$")
    scope_id: str | None = None
    modality: ProviderModality
    provider_config_id: str = Field(min_length=1, max_length=64)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope == "project" and not self.scope_id:
            raise ValueError("project provider binding requires scope_id")
        if self.scope == "global" and self.scope_id is not None:
            raise ValueError("global provider binding cannot have scope_id")
        return self


class SteeringEnvelope(StrictModel):
    schema_version: str = WORKSPACE_SCHEMA_VERSION
    decision_id: UUID = Field(default_factory=uuid4)
    target_run_id: str = Field(min_length=1, max_length=64)
    response_mode: ResponseMode
    relationship: SteeringRelationship
    impact_level: ImpactLevel
    action_on_a: SteeringAction
    affected_nodes: tuple[str, ...] = ()
    preserved_nodes: tuple[str, ...] = ()
    earliest_affected_checkpoint: str | None = None
    confidence: float = Field(ge=0, le=1)
    confirmation_required: bool = False
    rationale_summary: str = Field(min_length=1, max_length=2000)
    trigger_message_id: str | None = Field(default=None, max_length=64)
    decision_source: str = Field(default="impact_agent", pattern=r"^(rule|impact_agent|fallback)$")
    estimated_cost: float | None = Field(default=None, ge=0)
    permission_scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_impact_action(self) -> Self:
        required = {
            ImpactLevel.L0: SteeringAction.NONE,
            ImpactLevel.L1: SteeringAction.NONE,
            ImpactLevel.L2: SteeringAction.INJECT_AT_BOUNDARY,
            ImpactLevel.L3: SteeringAction.REPLAN_REMAINING,
            ImpactLevel.L4: SteeringAction.FORK_FROM_CHECKPOINT,
            ImpactLevel.L5: SteeringAction.CANCEL,
        }[self.impact_level]
        if self.action_on_a is not required:
            raise ValueError(f"{self.impact_level.value} requires action_on_a={required.value}")
        if self.impact_level is ImpactLevel.L4 and not self.earliest_affected_checkpoint:
            raise ValueError("L4 steering requires earliest_affected_checkpoint")
        if set(self.affected_nodes).intersection(self.preserved_nodes):
            raise ValueError("affected_nodes and preserved_nodes must be disjoint")
        return self


def _redact_public_payload(value: JsonValue, key: str | None = None) -> JsonValue:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact_public_payload(item) for item in value]
    if isinstance(value, dict):
        return {name: _redact_public_payload(item, name) for name, item in value.items()}
    return value


class WorkspaceEvent(StrictModel):
    schema_version: str = WORKSPACE_SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    run_id: str = Field(min_length=1, max_length=64)
    sequence: int = Field(ge=0)
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    public_payload: dict[str, JsonValue] = Field(default_factory=dict)
    internal_payload_ref: str | None = None

    @model_validator(mode="after")
    def redact_payload(self) -> Self:
        self.public_payload = {
            name: _redact_public_payload(value, name) for name, value in self.public_payload.items()
        }
        return self


def ensure_workspace_event_sequence(events: list[WorkspaceEvent]) -> None:
    previous = -1
    event_ids: set[UUID] = set()
    run_id: str | None = None
    for event in events:
        run_id = run_id or event.run_id
        if event.run_id != run_id:
            raise ValueError("workspace event stream changed run identity")
        if event.event_id in event_ids:
            raise ValueError("workspace event stream contains duplicate event ids")
        if event.sequence <= previous:
            raise ValueError("workspace event sequence must be strictly increasing")
        previous = event.sequence
        event_ids.add(event.event_id)


class ContractManifest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    workspace_schema_version: str = WORKSPACE_SCHEMA_VERSION
    project_statuses: tuple[ProjectStatus, ...] = tuple(ProjectStatus)
    conversation_statuses: tuple[ConversationStatus, ...] = tuple(ConversationStatus)
    message_statuses: tuple[MessageStatus, ...] = tuple(MessageStatus)
    run_statuses: tuple[RunStatus, ...] = tuple(RunStatus)
    event_schema_version: str = WORKSPACE_SCHEMA_VERSION
