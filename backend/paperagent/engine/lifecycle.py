from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import Field, JsonValue, TypeAdapter

from paperagent.engine.events import EngineEventKind, TurnRequest
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel

_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class EngineSignal(StrictModel):
    kind: EngineEventKind
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class ResumeRequest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    project_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    checkpoint_id: str | None = None
    decision: JsonValue = None
    idempotency_key: str = Field(min_length=1, max_length=255)


class CancelRequest(StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID = Field(default_factory=uuid4)
    project_id: str = Field(min_length=1, max_length=255)
    thread_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    reason: str = Field(default="user_cancelled", min_length=1, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=255)


class GraphLifecycle(Protocol):
    def start(self, request: TurnRequest) -> AsyncIterator[EngineSignal]: ...

    def resume(self, request: ResumeRequest) -> AsyncIterator[EngineSignal]: ...

    async def cancel(self, request: CancelRequest) -> None: ...


GraphInputBuilder = Callable[[TurnRequest], dict[str, object]]


class LangGraphLifecycleAdapter:
    """Translate a compiled LangGraph stream into engine lifecycle signals."""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        *,
        input_builder: GraphInputBuilder | None = None,
    ) -> None:
        self.graph = graph
        self.input_builder = input_builder or self._default_input

    async def start(self, request: TurnRequest) -> AsyncIterator[EngineSignal]:
        config = self._config(request.project_id, request.thread_id)
        payload = self.input_builder(request)
        async for update in self.graph.astream(payload, config=config, stream_mode="updates"):
            yield self._signal(update)

    async def resume(self, request: ResumeRequest) -> AsyncIterator[EngineSignal]:
        config = self._config(request.project_id, request.thread_id, request.checkpoint_id)
        resume_input: Command[object] | None = (
            Command(resume=request.decision) if request.decision is not None else None
        )
        async for update in self.graph.astream(
            resume_input, config=config, stream_mode="updates"
        ):
            yield self._signal(update)

    async def cancel(self, request: CancelRequest) -> None:
        del request

    @staticmethod
    def _default_input(request: TurnRequest) -> dict[str, object]:
        return {
            "data": {
                "project_id": request.project_id,
                "thread_id": request.thread_id,
                "task_id": request.task_id,
                "message_id": request.message_id,
                "user_message": request.user_message,
                "attachment_ids": request.attachment_ids,
            },
            "graph_status": "running",
        }

    @staticmethod
    def _config(
        project_id: str, thread_id: str, checkpoint_id: str | None = None
    ) -> RunnableConfig:
        configurable: dict[str, object] = {"thread_id": f"{project_id}:{thread_id}"}
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    @staticmethod
    def _signal(update: object) -> EngineSignal:
        validated = _JSON_ADAPTER.validate_python(update)
        payload: dict[str, JsonValue]
        if isinstance(validated, dict):
            payload = validated
        else:
            payload = {"value": cast(JsonValue, validated)}
        return EngineSignal(kind=EngineEventKind.NODE_COMPLETED, payload=payload)
