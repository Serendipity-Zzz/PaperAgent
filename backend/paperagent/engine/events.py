from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "access_token",
    "refresh_token",
}
_SECRET_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def _redact(value: JsonValue, key: str | None = None) -> JsonValue:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    return value


class EngineEventKind(StrEnum):
    TURN_ACCEPTED = "turn.accepted"
    GRAPH_STARTED = "graph.started"
    GRAPH_RESUMED = "graph.resumed"
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    MODEL_STARTED = "model.started"
    MODEL_TOKEN = "model.token"
    MODEL_COMPLETED = "model.completed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUIRED = "approval.required"
    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    AUTHORIZATION_REQUIRED = "authorization.required"
    ENVIRONMENT_RESOLVED = "environment.resolved"
    ENVIRONMENT_PREPARED = "environment.prepared"
    EXECUTION_OUTPUT = "execution.output"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_VALIDATED = "artifact.validated"
    ARTIFACT_LINKED = "artifact.linked"
    RENDER_STARTED = "render.started"
    RENDER_COMPLETED = "render.completed"
    CLAIM_VALIDATION_FAILED = "claim.validation_failed"
    CONTEXT_COMPACTED = "context.compacted"
    INTERRUPTED = "engine.interrupted"
    CANCELLED = "engine.cancelled"
    FAILED = "engine.failed"
    COMPLETED = "engine.completed"


class TurnRequest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    project_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    message_id: str = Field(min_length=1, max_length=255)
    user_message: str = Field(min_length=1, max_length=2_000_000)
    attachment_ids: list[str] = Field(default_factory=list)
    provider_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)


class EngineEvent(StrictModel):
    schema_version: str = SCHEMA_VERSION
    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    project_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    sequence: int = Field(ge=0)
    kind: EngineEventKind
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def redact_payload(self) -> Self:
        self.payload = {name: _redact(value, name) for name, value in self.payload.items()}
        return self

    def stable_hash(self) -> str:
        return stable_json_hash(
            {
                "schema_version": self.schema_version,
                "trace_id": str(self.trace_id),
                "project_id": self.project_id,
                "thread_id": self.thread_id,
                "task_id": self.task_id,
                "sequence": self.sequence,
                "kind": self.kind.value,
                "payload": self.payload,
            }
        )


def ensure_event_sequence(events: list[EngineEvent]) -> None:
    if not events:
        return
    identity = (events[0].trace_id, events[0].project_id, events[0].thread_id, events[0].task_id)
    previous = -1
    event_ids: set[UUID] = set()
    for event in events:
        current = (event.trace_id, event.project_id, event.thread_id, event.task_id)
        if current != identity:
            raise ValueError("engine event stream identity changed")
        if event.event_id in event_ids:
            raise ValueError("engine event stream contains duplicate event ids")
        if event.sequence <= previous:
            raise ValueError("engine event sequence must be strictly increasing")
        previous = event.sequence
        event_ids.add(event.event_id)
