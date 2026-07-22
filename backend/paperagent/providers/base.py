from __future__ import annotations

import json
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, Self, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator


class Capability(StrEnum):
    CHAT = "chat"
    STREAM = "stream"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    REASONING = "reasoning"
    IMAGE_GENERATION = "image_generation"
    EMBEDDINGS = "embeddings"


class ProviderModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    EMBEDDING = "embedding"


class ProviderHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ERROR = "error"
    BLOCKED = "blocked"


class ProviderToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolCallFragmentAssembler:
    """Reassembles provider streaming argument fragments without executing partial JSON."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def add(
        self,
        index: int,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments_fragment: str = "",
    ) -> None:
        state = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id:
            state["id"] = call_id
        if name:
            state["name"] = name
        state["arguments"] += arguments_fragment

    def finish(self) -> list[ProviderToolCall]:
        calls: list[ProviderToolCall] = []
        for index, state in sorted(self._calls.items()):
            if not state["id"] or not state["name"]:
                raise ValueError(f"incomplete streamed tool call at index {index}")
            try:
                arguments = json.loads(state["arguments"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid streamed tool arguments at index {index}") from error
            if not isinstance(arguments, dict):
                raise ValueError(f"streamed tool arguments at index {index} are not an object")
            calls.append(
                ProviderToolCall(
                    id=state["id"],
                    name=state["name"],
                    arguments=arguments,
                )
            )
        return calls


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern=r"^(system|developer|user|assistant|tool)$")
    content: str = ""
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None

    @model_validator(mode="after")
    def validate_tool_fields(self) -> Self:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may contain tool calls")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("only tool messages may contain tool_call_id")
        if self.role != "tool" and self.tool_name is not None:
            raise ValueError("only tool messages may contain tool_name")
        return self


class ProviderConfig(BaseModel):
    id: str
    display_name: str = ""
    modality: ProviderModality = ProviderModality.TEXT
    protocol: str = "openai_compatible"
    provider_type: str
    base_url: HttpUrl
    model: str
    credential_ref: str | None = None
    capabilities: set[Capability] = Field(default_factory=lambda: {Capability.CHAT})
    extra: dict[str, object] = Field(default_factory=dict)
    enabled: bool = True
    health_status: ProviderHealth = ProviderHealth.UNKNOWN
    health_detail: str = ""
    version: int = Field(default=1, ge=1)
    secret_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_modality_capabilities(self) -> Self:
        required = (
            Capability.IMAGE_GENERATION
            if self.modality is ProviderModality.IMAGE
            else Capability.EMBEDDINGS
            if self.modality is ProviderModality.EMBEDDING
            else Capability.CHAT
        )
        if required not in self.capabilities:
            raise ValueError(
                f"{self.modality.value} provider requires {required.value} capability"
            )
        return self


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    tools: list[dict[str, object]] = Field(default_factory=list)
    response_schema: dict[str, object] | None = None
    required_capabilities_extra: set[Capability] = Field(default_factory=set)
    idempotency_key: str | None = None

    def required_capabilities(self) -> set[Capability]:
        required = {Capability.CHAT, *self.required_capabilities_extra}
        if self.tools:
            required.add(Capability.TOOLS)
        if self.response_schema:
            required.add(Capability.STRUCTURED_OUTPUT)
        return required


class Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(default=0, ge=0)


class ChatResponse(BaseModel):
    content: str
    model: str
    finish_reason: str = "stop"
    usage: Usage
    tool_calls: list[ProviderToolCall] = Field(default_factory=list)


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool, state_unknown: bool = False):
        # Some transport exceptions (notably httpx timeouts on Windows) have an
        # empty string representation.  Persist the stable provider code so API,
        # checkpoints and the Recovery Center never receive a blank diagnosis.
        super().__init__(message.strip() or code)
        self.code = code
        self.retryable = retryable
        self.state_unknown = state_unknown


@runtime_checkable
class ModelProvider(Protocol):
    config: ProviderConfig

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[str]: ...
