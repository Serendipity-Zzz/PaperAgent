from uuid import uuid4

from paperagent.engine import BudgetLimits
from paperagent.orchestration import (
    CandidateEdge,
    CandidateNode,
    CandidatePlan,
    PlanValidator,
    TaskGraphCompiler,
)
from paperagent.rendering.delivery import DocumentAction
from paperagent.tools import ToolRegistry


def candidate(*, cyclic: bool = False) -> CandidatePlan:
    edges = [CandidateEdge(source="understand", target="write")]
    if cyclic:
        edges.append(CandidateEdge(source="write", target="understand"))
    return CandidatePlan(
        requirement_id=uuid4(),
        requirement_version=1,
        entry_node="understand",
        terminal_nodes={"write"},
        nodes=[
            CandidateNode(
                node_id="understand",
                agent_type="requirement_agent",
                objective="understand",
                output_keys=["requirement"],
            ),
            CandidateNode(
                node_id="write",
                agent_type="writer_agent",
                objective="write",
                input_refs=["requirement"],
                output_keys=["document"],
            ),
        ],
        edges=edges,
        limits=BudgetLimits(max_input_tokens=1000, max_output_tokens=500),
        rationale="test plan",
    )


def test_valid_candidate_compiles_to_executable_task_graph_contract() -> None:
    plan = candidate()
    report = PlanValidator(
        ToolRegistry(),
        allowed_agents={"requirement_agent", "writer_agent"},
    ).validate(plan)
    assert report.valid and report.topological_order == ["understand", "write"]
    graph = TaskGraphCompiler().compile(plan)
    assert graph.entry_node == "understand"
    assert graph.nodes[1].input_keys == ("requirement",)


def test_cycle_and_unknown_agent_fail_closed() -> None:
    report = PlanValidator(
        ToolRegistry(), allowed_agents={"requirement_agent"}
    ).validate(candidate(cyclic=True))
    assert not report.valid
    assert {issue.code for issue in report.issues} >= {
        "AGENT_NOT_ALLOWED",
        "GRAPH_CYCLE",
    }


def test_format_conversion_rejects_writer_plan_without_delivery_invariants() -> None:
    report = PlanValidator(
        ToolRegistry(),
        allowed_agents={"requirement_agent", "writer_agent"},
    ).validate(candidate(), document_action=DocumentAction.CONVERT_FORMAT)
    assert not report.valid
    assert {issue.code for issue in report.issues} >= {
        "DELIVERY_INVARIANT_MISSING",
        "DELIVERY_SIDE_EFFECT_FORBIDDEN",
    }
