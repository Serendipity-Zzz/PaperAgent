from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL = "external"
    PAID = "paid"
    DESTRUCTIVE = "destructive"


class ConcurrencyPolicy(StrEnum):
    SAFE = "safe"
    EXCLUSIVE = "exclusive"
    INPUT_DEPENDENT = "input_dependent"


class PermissionPolicy(StrEnum):
    DETERMINISTIC = "deterministic"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ToolSpec(StrictModel):
    schema_version: str = SCHEMA_VERSION
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    description: str = Field(min_length=1, max_length=4_000)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None = None
    capabilities: set[str] = Field(default_factory=set)
    required_provider_capabilities: set[str] = Field(default_factory=lambda: {"tools"})
    search_hints: list[str] = Field(default_factory=list)
    allowed_agents: set[str] = Field(default_factory=set)
    side_effect: SideEffect = SideEffect.NONE
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.EXCLUSIVE
    permission_policy: PermissionPolicy = PermissionPolicy.DETERMINISTIC
    deferred: bool = False
    max_inline_chars: int = Field(default=12_000, ge=0, le=2_000_000)
    source: str = "builtin"

    def schema_hash(self) -> str:
        return stable_json_hash(self)


class ToolCall(StrictModel):
    schema_version: str = SCHEMA_VERSION
    call_id: str = Field(min_length=1, max_length=255)
    trace_id: UUID
    sequence: int = Field(ge=0)
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    tool_version: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    requested_by: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ToolError(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    message: str = Field(min_length=1, max_length=2_000)
    category: str
    retryable: bool = False
    state_unknown: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(StrictModel):
    schema_version: str = SCHEMA_VERSION
    result_id: UUID = Field(default_factory=uuid4)
    call_id: str = Field(min_length=1, max_length=255)
    status: ToolResultStatus
    content: JsonValue = None
    error: ToolError | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    truncated: bool = False
    full_result_ref: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is ToolResultStatus.SUCCESS and self.error is not None:
            raise ValueError("successful tool result cannot contain an error")
        if self.status is not ToolResultStatus.SUCCESS and self.error is None:
            raise ValueError("non-success tool result requires an error")
        if self.truncated and not self.full_result_ref:
            raise ValueError("truncated tool result requires full_result_ref")
        if self.content_hash is None:
            self.content_hash = stable_json_hash(self.content)
        return self
