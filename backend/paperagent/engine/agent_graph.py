from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from paperagent.engine.agent_loop import AgentLoop, AgentLoopRequest, AgentLoopResult


class AgentLoopGraphState(TypedDict, total=False):
    agent_result: dict[str, object]


AgentResultSink = Callable[[AgentLoopResult], None]


def compile_agent_loop_graph(
    loop: AgentLoop,
    loop_request: AgentLoopRequest,
    *,
    result_sink: AgentResultSink | None = None,
) -> CompiledStateGraph[AgentLoopGraphState, None, AgentLoopGraphState, AgentLoopGraphState]:
    """Place the model-tool loop inside a real LangGraph lifecycle node."""

    async def run_agent(_state: AgentLoopGraphState) -> AgentLoopGraphState:
        result = await loop.run(loop_request)
        if result_sink is not None:
            result_sink(result)
        return {"agent_result": result.model_dump(mode="json")}

    builder = StateGraph(AgentLoopGraphState)
    builder.add_node("agent_loop", cast(Any, run_agent))
    builder.add_edge(START, "agent_loop")
    builder.add_edge("agent_loop", END)
    return builder.compile()
