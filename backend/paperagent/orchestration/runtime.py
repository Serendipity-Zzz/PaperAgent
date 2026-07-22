from __future__ import annotations

from collections.abc import Callable, Hashable, Mapping
from typing import Any, Literal, TypedDict, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy, interrupt

from paperagent.agents.state import GraphCondition, TaskGraph
from paperagent.orchestration.failure import (
    FailureAnalyzer,
    FailureRecord,
    RecoveryDecision,
    RecoveryPlanner,
    retryable_by_langgraph,
)


class WorkflowState(TypedDict, total=False):
    data: dict[str, object]
    node_status: dict[str, str]
    last_failure: dict[str, object]
    recovery_decision: dict[str, object]
    strategy_history: list[str]
    strategy_history_by_fingerprint: dict[str, list[str]]
    graph_status: str
    prompt_hash: str
    context_pack_id: str
    tool_records: list[dict[str, object]]
    budget_decision: dict[str, object]
    external_request_ids: list[str]
    artifact_hashes: list[str]
    memory_extraction_cursor: int


NodeHandler = Callable[[dict[str, object]], dict[str, object]]
RECOVERY_NODE = "__failure_recovery__"


class ExecutableTaskGraph:
    """Compile the serializable TaskGraph contract into a durable LangGraph runtime."""

    def __init__(
        self,
        definition: TaskGraph,
        handlers: Mapping[str, NodeHandler],
        *,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        recovery_planner: RecoveryPlanner | None = None,
    ) -> None:
        self.definition = definition
        self.handlers = dict(handlers)
        self.checkpointer = checkpointer
        self.recovery_planner = recovery_planner or RecoveryPlanner()
        if any(node.node_id == RECOVERY_NODE for node in definition.nodes):
            raise ValueError(f"{RECOVERY_NODE!r} is reserved by the workflow runtime")
        missing = {node.agent_type for node in definition.nodes} - set(self.handlers)
        if missing:
            raise ValueError(f"task graph has no handlers for agent types: {sorted(missing)}")

    def compile(self) -> CompiledStateGraph[WorkflowState, None, WorkflowState, WorkflowState]:
        builder = StateGraph(WorkflowState)
        node_ids = {item.node_id for item in self.definition.nodes}
        for node in self.definition.nodes:
            builder.add_node(
                node.node_id,
                cast(Any, self._node_action(node.node_id, node.agent_type)),
                retry_policy=RetryPolicy(
                    max_attempts=node.max_attempts,
                    initial_interval=0.25,
                    backoff_factor=2.0,
                    max_interval=8.0,
                    jitter=True,
                    retry_on=retryable_by_langgraph,
                ),
                error_handler=cast(Any, self._node_error),
                destinations=(RECOVERY_NODE,),
            )
        builder.add_node(
            RECOVERY_NODE,
            self._recover,
            destinations=(*tuple(node_ids), END),
        )
        builder.add_edge(START, self.definition.entry_node)
        outgoing: dict[str, list[tuple[str, GraphCondition]]] = {}
        for edge in self.definition.edges:
            outgoing.setdefault(edge.source, []).append((edge.target, edge.condition))
        for node_id in node_ids:
            routes = outgoing.get(node_id, [])
            if node_id in self.definition.terminal_nodes and not routes:
                builder.add_edge(node_id, END)
            else:
                destinations: dict[Hashable, str] = {
                    target: target for target, _condition in routes
                }
                destinations[END] = END
                builder.add_conditional_edges(
                    node_id,
                    self._route(routes),
                    destinations,
                )
        return builder.compile(checkpointer=self.checkpointer)

    def _node_action(
        self, node_id: str, agent_type: str
    ) -> Callable[[WorkflowState], WorkflowState]:
        handler = self.handlers[agent_type]

        def action(state: WorkflowState) -> WorkflowState:
            data = dict(state.get("data", {}))
            output = handler(data)
            data.update(output)
            statuses = dict(state.get("node_status", {}))
            statuses[node_id] = "completed"
            return {"data": data, "node_status": statuses, "graph_status": "running"}

        return action

    @staticmethod
    def _node_error(
        state: WorkflowState, error: NodeError
    ) -> Command[Literal["__failure_recovery__"]]:
        prior = state.get("last_failure", {})
        prior_attempt = prior.get("attempt", 0)
        attempt = (
            prior_attempt + 1
            if prior.get("node") == error.node and isinstance(prior_attempt, int)
            else 1
        )
        failure = FailureAnalyzer.analyze(error.node, error.error, attempt=attempt)
        statuses = dict(state.get("node_status", {}))
        statuses[error.node] = "failed"
        return Command(
            update={
                "last_failure": failure.model_dump(mode="json"),
                "node_status": statuses,
                "graph_status": "recovering",
            },
            goto="__failure_recovery__",
        )

    def _recover(self, state: WorkflowState) -> Command[str]:
        failure = FailureRecord.model_validate(state["last_failure"])
        fingerprint = failure.fingerprint()
        histories = {
            key: list(value)
            for key, value in state.get("strategy_history_by_fingerprint", {}).items()
        }
        history = histories.get(fingerprint, [])
        decision = self.recovery_planner.decide(failure, prior_strategies=history)
        history = [*history, decision.strategy]
        histories[fingerprint] = history
        update: WorkflowState = {
            "recovery_decision": {
                **decision.model_dump(mode="json"),
                "failure_fingerprint": fingerprint,
            },
            "strategy_history": history,
            "strategy_history_by_fingerprint": histories,
        }
        if decision.requires_human:
            answer = interrupt(
                {
                    "kind": "failure_recovery",
                    "failure": failure.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                    "question": "是否按建议恢复策略继续?",
                }
            )
            approved = bool(answer.get("approved")) if isinstance(answer, dict) else bool(answer)
            if not approved:
                update["graph_status"] = "cancelled"
                return Command(update=update, goto=END)
        data = dict(state.get("data", {}))
        data["recovery_strategy"] = decision.strategy
        update["data"] = data
        update["graph_status"] = "running"
        target = self._recovery_target(failure, decision)
        return Command(update=update, goto=target)

    def _recovery_target(self, failure: FailureRecord, decision: RecoveryDecision) -> str:
        known = {node.node_id for node in self.definition.nodes}
        if decision.resume_node in known:
            return decision.resume_node
        if decision.replan:
            supervisor = next(
                (node.node_id for node in self.definition.nodes if node.agent_type == "supervisor"),
                None,
            )
            if supervisor and supervisor != failure.node:
                return supervisor
        return failure.node

    @staticmethod
    def _route(
        routes: list[tuple[str, GraphCondition]],
    ) -> Callable[[WorkflowState], str | list[str]]:
        def choose(state: WorkflowState) -> str | list[str]:
            data = state.get("data", {})
            predicates = (
                (GraphCondition.REPAIR_REQUIRED, data.get("repair_required") is True),
                (GraphCondition.NEEDS_INPUT, data.get("needs_input") is True),
                (GraphCondition.REJECTED, data.get("approved") is False),
                (GraphCondition.APPROVED, data.get("approved") is True),
            )
            # State-specific branches take priority over the normal success path.
            # This prevents ON_SUCCESS from swallowing a repair or approval branch.
            for condition, matched in predicates:
                if matched:
                    targets = [
                        target
                        for target, edge_condition in routes
                        if edge_condition is condition
                    ]
                    if targets:
                        return targets[0] if len(targets) == 1 else targets
            fallback = [
                target
                for target, condition in routes
                if condition in {GraphCondition.ON_SUCCESS, GraphCondition.ALWAYS}
            ]
            if fallback:
                return fallback[0] if len(fallback) == 1 else fallback
            return END

        return choose
