from __future__ import annotations

from typing import ClassVar

from paperagent.agents.state import GraphCondition, NodeDefinition, TaskEdge, TaskGraph
from paperagent.orchestration.plan_models import CandidateEdgeCondition, CandidatePlan


class TaskGraphCompiler:
    CONDITION_MAP: ClassVar[dict[CandidateEdgeCondition, GraphCondition]] = {
        CandidateEdgeCondition.ON_SUCCESS: GraphCondition.ON_SUCCESS,
        CandidateEdgeCondition.ALWAYS: GraphCondition.ALWAYS,
        CandidateEdgeCondition.APPROVED: GraphCondition.APPROVED,
        CandidateEdgeCondition.REJECTED: GraphCondition.REJECTED,
        CandidateEdgeCondition.NEEDS_INPUT: GraphCondition.NEEDS_INPUT,
        CandidateEdgeCondition.REPAIR_REQUIRED: GraphCondition.REPAIR_REQUIRED,
    }

    def compile(self, plan: CandidatePlan) -> TaskGraph:
        return TaskGraph(
            entry_node=plan.entry_node,
            terminal_nodes=plan.terminal_nodes,
            nodes=[
                NodeDefinition(
                    node_id=node.node_id,
                    agent_type=node.agent_type,
                    input_keys=tuple(node.input_refs),
                    output_keys=tuple(node.output_keys),
                    requires_confirmed_requirement=True,
                    max_attempts=node.max_attempts,
                )
                for node in plan.nodes
            ],
            edges=[
                TaskEdge(
                    source=edge.source,
                    target=edge.target,
                    condition=self.CONDITION_MAP[edge.condition],
                )
                for edge in plan.edges
            ],
        )
