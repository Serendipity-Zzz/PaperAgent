import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.engine import (
    CancelRequest,
    ConversationEngine,
    EngineEvent,
    EngineEventKind,
    EngineSignal,
    LangGraphLifecycleAdapter,
    ResumeRequest,
    SqliteConversationPersistence,
    TurnRequest,
)
from paperagent.services.repositories import ConversationRepository, ProjectRepository


def setup_project(tmp_path: Path) -> tuple[DatabaseManager, str]:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = ProjectRepository(databases).create("conversation-engine").id
    return databases, project_id


async def collect(stream: AsyncIterator[EngineEvent]) -> list[EngineEvent]:
    return [event async for event in stream]


class RecordingLifecycle:
    def __init__(self, databases: DatabaseManager, project_id: str) -> None:
        self.databases = databases
        self.project_id = project_id
        self.starts = 0
        self.resumes = 0
        self.cancels = 0
        self.message_was_persisted = False
        self.fail = False

    async def start(self, request: TurnRequest) -> AsyncIterator[EngineSignal]:
        self.starts += 1
        messages = ConversationRepository(self.databases).list_messages(
            self.project_id, request.thread_id
        )
        self.message_was_persisted = any(row.id == request.message_id for row in messages)
        yield EngineSignal(
            kind=EngineEventKind.NODE_STARTED,
            payload={"node_id": "requirements"},
        )
        if self.fail:
            raise RuntimeError("provider rejected sk-abcdefghijk")
        yield EngineSignal(
            kind=EngineEventKind.NODE_COMPLETED,
            payload={"node_id": "requirements"},
        )

    async def resume(self, request: ResumeRequest) -> AsyncIterator[EngineSignal]:
        self.resumes += 1
        yield EngineSignal(
            kind=EngineEventKind.NODE_COMPLETED,
            payload={"checkpoint_id": request.checkpoint_id or "latest"},
        )

    async def cancel(self, request: CancelRequest) -> None:
        del request
        self.cancels += 1


def test_user_message_precedes_graph_and_completed_turn_replays_idempotently(
    tmp_path: Path,
) -> None:
    databases, project_id = setup_project(tmp_path)
    lifecycle = RecordingLifecycle(databases, project_id)
    engine = ConversationEngine(SqliteConversationPersistence(databases), lifecycle)
    request = TurnRequest(
        project_id=project_id,
        thread_id="thread-1",
        task_id="task-1",
        message_id="message-1",
        user_message="请写实验报告",
        idempotency_key="turn-1",
    )
    first = asyncio.run(collect(engine.run_turn(request)))
    assert lifecycle.message_was_persisted
    assert [event.kind for event in first] == [
        EngineEventKind.TURN_ACCEPTED,
        EngineEventKind.GRAPH_STARTED,
        EngineEventKind.NODE_STARTED,
        EngineEventKind.NODE_COMPLETED,
        EngineEventKind.COMPLETED,
    ]
    assert [event.sequence for event in first] == [1, 2, 3, 4, 5]

    replay = asyncio.run(collect(engine.run_turn(request)))
    assert [event.event_id for event in replay] == [event.event_id for event in first]
    assert lifecycle.starts == 1
    messages = ConversationRepository(databases).list_messages(project_id, "thread-1")
    assert len(messages) == 1


def test_failure_is_durable_redacted_and_resume_is_idempotent(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    lifecycle = RecordingLifecycle(databases, project_id)
    lifecycle.fail = True
    engine = ConversationEngine(SqliteConversationPersistence(databases), lifecycle)
    turn = TurnRequest(
        project_id=project_id,
        thread_id="thread-1",
        task_id="task-1",
        message_id="message-1",
        user_message="继续",
        idempotency_key="turn-1",
    )
    failed = asyncio.run(collect(engine.run_turn(turn)))
    assert failed[-1].kind is EngineEventKind.FAILED
    assert "sk-abcdefghijk" not in failed[-1].model_dump_json()
    assert "[REDACTED]" in failed[-1].model_dump_json()

    resumed_engine = ConversationEngine(SqliteConversationPersistence(databases), lifecycle)
    resume = ResumeRequest(
        project_id=project_id,
        thread_id="thread-1",
        task_id="task-1",
        checkpoint_id="checkpoint-7",
        decision={"approved": True},
        idempotency_key="resume-1",
    )
    first_resume = asyncio.run(collect(resumed_engine.resume(resume)))
    assert first_resume[0].kind is EngineEventKind.GRAPH_RESUMED
    assert first_resume[-1].kind is EngineEventKind.COMPLETED
    replay = asyncio.run(collect(resumed_engine.resume(resume)))
    assert [item.event_id for item in replay] == [item.event_id for item in first_resume]
    assert lifecycle.resumes == 1


def test_cancel_is_durable_and_idempotent(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    lifecycle = RecordingLifecycle(databases, project_id)
    engine = ConversationEngine(SqliteConversationPersistence(databases), lifecycle)
    request = CancelRequest(
        project_id=project_id,
        thread_id="thread-1",
        task_id="task-1",
        reason="用户取消",
        idempotency_key="cancel-1",
    )
    first = asyncio.run(engine.cancel(request))
    second = asyncio.run(engine.cancel(request))
    assert first.kind is EngineEventKind.CANCELLED
    assert second.event_id == first.event_id
    assert lifecycle.cancels == 1


class AdapterState(TypedDict, total=False):
    data: dict[str, object]
    graph_status: str


def test_real_langgraph_adapter_streams_through_conversation_engine(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    builder = StateGraph(AdapterState)

    def requirement_node(state: AdapterState) -> AdapterState:
        data = dict(state["data"])
        data["normalized"] = True
        return {"data": data, "graph_status": "completed"}

    builder.add_node("requirements", requirement_node)
    builder.add_edge(START, "requirements")
    builder.add_edge("requirements", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    engine = ConversationEngine(
        SqliteConversationPersistence(databases), LangGraphLifecycleAdapter(graph)
    )
    request = TurnRequest(
        project_id=project_id,
        thread_id="thread-graph",
        task_id="task-graph",
        message_id="message-graph",
        user_message="规范化这个需求",
        idempotency_key="turn-graph",
    )
    events = asyncio.run(collect(engine.run_turn(request)))
    assert events[-1].kind is EngineEventKind.COMPLETED
    node_event = next(event for event in events if event.kind is EngineEventKind.NODE_COMPLETED)
    assert node_event.payload["requirements"] == {
        "data": {
            "project_id": project_id,
            "thread_id": "thread-graph",
            "task_id": "task-graph",
            "message_id": "message-graph",
            "user_message": "规范化这个需求",
            "attachment_ids": [],
            "normalized": True,
        },
        "graph_status": "completed",
    }


def test_real_langgraph_adapter_resumes_failed_node_without_interrupt_command(
    tmp_path: Path,
) -> None:
    databases, project_id = setup_project(tmp_path)
    attempts = 0
    builder = StateGraph(AdapterState)

    def unstable_node(state: AdapterState) -> AdapterState:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary node failure")
        return {"data": dict(state["data"]), "graph_status": "completed"}

    builder.add_node("unstable", unstable_node)
    builder.add_edge(START, "unstable")
    builder.add_edge("unstable", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    engine = ConversationEngine(
        SqliteConversationPersistence(databases), LangGraphLifecycleAdapter(graph)
    )
    turn = TurnRequest(
        project_id=project_id,
        thread_id="thread-failed-graph",
        task_id="task-failed-graph",
        message_id="message-failed-graph",
        user_message="resume the failed node",
        idempotency_key="turn-failed-graph",
    )
    failed = asyncio.run(collect(engine.run_turn(turn)))
    assert failed[-1].kind is EngineEventKind.FAILED

    resumed = asyncio.run(
        collect(
            engine.resume(
                ResumeRequest(
                    project_id=project_id,
                    thread_id="thread-failed-graph",
                    task_id="task-failed-graph",
                    decision=None,
                    idempotency_key="resume-failed-graph",
                )
            )
        )
    )

    assert attempts == 2
    assert resumed[0].kind is EngineEventKind.GRAPH_RESUMED
    assert resumed[-1].kind is EngineEventKind.COMPLETED
