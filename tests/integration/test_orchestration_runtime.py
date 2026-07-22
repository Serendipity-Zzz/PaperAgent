from langgraph.checkpoint.memory import InMemorySaver

from paperagent.agents.state import (
    GraphCondition,
    NodeDefinition,
    TaskEdge,
    TaskGraph,
)
from paperagent.orchestration import ExecutableTaskGraph


def graph_definition() -> TaskGraph:
    return TaskGraph(
        entry_node="work",
        terminal_nodes={"done"},
        nodes=[
            NodeDefinition(
                node_id="work",
                agent_type="worker",
                input_keys=(),
                output_keys=("result",),
                max_attempts=2,
            ),
            NodeDefinition(
                node_id="done",
                agent_type="finisher",
                input_keys=("result",),
                output_keys=("finished",),
            ),
        ],
        edges=[TaskEdge(source="work", target="done", condition=GraphCondition.ON_SUCCESS)],
    )


def test_semantic_failure_changes_strategy_and_resumes_from_checkpoint() -> None:
    calls = 0

    def worker(data: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if data.get("recovery_strategy") != "schema_repair_with_error_feedback":
            raise ValueError("draft.blocks.0.text failed schema validation")
        return {"result": "repaired"}

    runtime = ExecutableTaskGraph(
        graph_definition(),
        {"worker": worker, "finisher": lambda data: {"finished": data["result"]}},
        checkpointer=InMemorySaver(),
    ).compile()
    result = runtime.invoke(
        {"data": {}, "node_status": {}, "strategy_history": []},
        config={"configurable": {"thread_id": "semantic-repair"}},
    )

    assert calls == 2
    assert result["data"]["finished"] == "repaired"
    assert result["strategy_history"] == ["schema_repair_with_error_feedback"]
    assert result["node_status"] == {"work": "completed", "done": "completed"}


def test_transient_failure_uses_langgraph_retry_policy_without_semantic_replan() -> None:
    calls = 0

    def worker(_data: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary connection reset")
        return {"result": "ok"}

    runtime = ExecutableTaskGraph(
        graph_definition(),
        {"worker": worker, "finisher": lambda data: {"finished": data["result"]}},
    ).compile()
    result = runtime.invoke({"data": {}, "node_status": {}, "strategy_history": []})

    assert calls == 2
    assert result["data"]["finished"] == "ok"
    assert result.get("strategy_history") == []


def test_repair_branch_takes_priority_over_normal_success_edge() -> None:
    definition = TaskGraph(
        entry_node="review",
        terminal_nodes={"done", "repair"},
        nodes=[
            NodeDefinition(
                node_id="review",
                agent_type="reviewer",
                input_keys=(),
                output_keys=("repair_required",),
            ),
            NodeDefinition(
                node_id="done",
                agent_type="done",
                input_keys=(),
                output_keys=("path",),
            ),
            NodeDefinition(
                node_id="repair",
                agent_type="repair",
                input_keys=(),
                output_keys=("path",),
            ),
        ],
        edges=[
            TaskEdge(
                source="review", target="done", condition=GraphCondition.ON_SUCCESS
            ),
            TaskEdge(
                source="review",
                target="repair",
                condition=GraphCondition.REPAIR_REQUIRED,
            ),
        ],
    )
    runtime = ExecutableTaskGraph(
        definition,
        {
            "reviewer": lambda _data: {"repair_required": True},
            "done": lambda _data: {"path": "done"},
            "repair": lambda _data: {"path": "repair"},
        },
    ).compile()

    result = runtime.invoke({"data": {}, "node_status": {}, "strategy_history": []})

    assert result["data"]["path"] == "repair"
    assert result["node_status"] == {"review": "completed", "repair": "completed"}
