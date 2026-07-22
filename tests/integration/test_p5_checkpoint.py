from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from paperagent.agents import AgentCheckpointService
from paperagent.agents.state import (
    AgentState,
    GraphInterrupt,
    InterruptKind,
    NodeDefinition,
    RawRequest,
    RequirementSpec,
    RequirementVersionHistory,
    TaskGraph,
)
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.services.repositories import EventRepository, ProjectRepository


def setup_project(tmp_path: Path, name: str = "agent") -> tuple[DatabaseManager, str]:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    return databases, ProjectRepository(databases).create(name).id


def agent_state(project_id: str, thread_id: str = "thread-1") -> AgentState:
    requirement = RequirementSpec(raw_request=RawRequest(text="Write a report"))
    task_graph = TaskGraph(
        entry_node="understand",
        terminal_nodes={"understand"},
        nodes=[
            NodeDefinition(
                node_id="understand",
                agent_type="requirement",
                input_keys=("raw_request",),
                output_keys=("requirement_spec",),
            )
        ],
    )
    return AgentState(
        project_id=project_id,
        thread_id=thread_id,
        task_id=f"task-{thread_id}",
        graph=task_graph,
        requirement_history=RequirementVersionHistory(
            requirement_id=requirement.requirement_id,
            versions=[requirement],
        ),
    )


def test_checkpoint_before_after_restart_and_event_bridge(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    service = AgentCheckpointService(databases, project_id)
    state = agent_state(project_id)
    service.checkpoint(state)
    before, started = service.before_node(
        state,
        node_id="understand",
        execution_key="requirement-v1",
        node_input={"raw_request": "message-1"},
    )
    assert started
    assert before.state.active_node == "understand"
    service.close()

    restarted = AgentCheckpointService(databases, project_id)
    restored = restarted.latest("thread-1").state
    assert restored.node_runs["understand"].status == "running"
    completed = restarted.after_node(
        restored,
        node_id="understand",
        output={"requirement_spec": {"id": "requirement-1"}},
    )
    assert completed.state.node_runs["understand"].status == "completed"
    assert [checkpoint.state.sequence for checkpoint in restarted.history("thread-1")] == [0, 1, 2]
    events = EventRepository(databases).after(project_id, 0)
    assert [event.type for event in events][-3:] == [
        "agent.checkpoint.saved",
        "agent.node.started",
        "agent.node.completed",
    ]
    restarted.close()


def test_pause_resume_is_idempotent_and_cancel_is_durable(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    service = AgentCheckpointService(databases, project_id)
    state = agent_state(project_id)
    service.checkpoint(state)
    interrupt = GraphInterrupt(
        kind=InterruptKind.APPROVAL,
        node_id="understand",
        action="clarification",
        prompt="Confirm plan",
    )
    service.pause(state, interrupt)
    resumed = service.resume("thread-1", interrupt.interrupt_id, approved=True)
    sequence = resumed.sequence
    repeated = service.resume("thread-1", interrupt.interrupt_id, approved=True)
    assert repeated.sequence == sequence
    with pytest.raises(ValueError, match="different decision"):
        service.resume("thread-1", interrupt.interrupt_id, approved=False)
    cancelled = service.cancel("thread-1")
    assert cancelled.status == "cancelled"
    service.close()

    restarted = AgentCheckpointService(databases, project_id)
    assert restarted.latest("thread-1").state.status == "cancelled"
    restarted.close()


class CounterState(TypedDict):
    value: int


def counter_graph(service: AgentCheckpointService):  # type: ignore[no-untyped-def]
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.compile(checkpointer=service.saver)


def test_official_langgraph_sqlite_saver_survives_process_restart(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    first = AgentCheckpointService(databases, project_id)
    graph = counter_graph(first)
    assert graph.invoke({"value": 1}, config=first.config("langgraph-thread"))["value"] == 2
    first.close()

    second = AgentCheckpointService(databases, project_id)
    restored_graph = counter_graph(second)
    snapshot = restored_graph.get_state(second.config("langgraph-thread"))
    assert snapshot.values["value"] == 2
    assert list(restored_graph.get_state_history(second.config("langgraph-thread")))
    second.close()


def test_project_thread_isolation_and_concurrent_sessions(tmp_path: Path) -> None:
    databases, project_id = setup_project(tmp_path)
    other_project = ProjectRepository(databases).create("other").id
    first = AgentCheckpointService(databases, project_id)
    first.checkpoint(agent_state(project_id, "same-thread"))
    with pytest.raises(KeyError):
        first.latest("missing-thread")
    first.close()

    isolated = AgentCheckpointService(databases, other_project)
    with pytest.raises(KeyError):
        isolated.latest("same-thread")
    isolated.close()

    def save(thread_id: str) -> str:
        service = AgentCheckpointService(databases, project_id)
        service.checkpoint(agent_state(project_id, thread_id))
        restored = service.latest(thread_id).state.thread_id
        service.close()
        return restored

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert set(executor.map(save, ("parallel-a", "parallel-b"))) == {
            "parallel-a",
            "parallel-b",
        }
