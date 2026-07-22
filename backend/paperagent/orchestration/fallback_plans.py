from __future__ import annotations

from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.engine import BudgetLimits
from paperagent.orchestration.plan_models import (
    ApprovalRequirement,
    CandidateEdge,
    CandidateEdgeCondition,
    CandidateNode,
    CandidatePlan,
)


def safe_fallback_plan(requirement: RequirementSpec) -> CandidatePlan:
    if requirement.status is not RequirementStatus.CONFIRMED:
        raise PermissionError("fallback plan requires confirmed requirements")
    confirmed = requirement.confirmed_requirement
    assert confirmed is not None
    nodes = [
        CandidateNode(
            node_id="outline",
            agent_type="outline_agent",
            objective="Design the document structure from the confirmed requirement.",
            output_keys=["outline"],
            success_criteria=["outline matches confirmed document type"],
        ),
        CandidateNode(
            node_id="evidence",
            agent_type="evidence_agent",
            objective="Build an eligible Evidence Pack with locators.",
            input_refs=["outline"],
            output_keys=["evidence_pack"],
        ),
    ]
    prior = "evidence"
    if confirmed.requires_experiment:
        nodes.append(
            CandidateNode(
                node_id="experiment",
                agent_type="experiment_agent",
                objective="Check feasibility and run the approved isolated experiment.",
                input_refs=["outline"],
                output_keys=["experiment_result"],
                required_tools=["experiment.assess"],
                approval=ApprovalRequirement(
                    action="execute_experiment",
                    risk="third-party code and dependencies may consume local resources",
                    consequence="an isolated task environment will be created and executed",
                ),
            )
        )
        prior = "experiment"
    nodes.extend(
        [
            CandidateNode(
                node_id="writing",
                agent_type="writer_agent",
                objective="Write sections using only eligible Evidence IDs.",
                input_refs=["outline", "evidence_pack"],
                output_keys=["document_ir"],
            ),
            CandidateNode(
                node_id="review",
                agent_type="review_agent",
                objective="Review claims, citations, requirements, length, and formatting.",
                input_refs=["document_ir", "evidence_pack"],
                output_keys=["review_report", "repair_required"],
            ),
            CandidateNode(
                node_id="render",
                agent_type="render_agent",
                objective="Render approved outputs and visually verify them.",
                input_refs=["document_ir", "review_report"],
                output_keys=["artifacts"],
            ),
        ]
    )
    edges = [
        CandidateEdge(source="outline", target="evidence"),
        CandidateEdge(source="evidence", target=prior) if prior != "evidence" else None,
        CandidateEdge(source=prior, target="writing"),
        CandidateEdge(source="writing", target="review"),
        CandidateEdge(
            source="review",
            target="render",
            condition=CandidateEdgeCondition.ON_SUCCESS,
        ),
    ]
    return CandidatePlan(
        requirement_id=confirmed.requirement_id,
        requirement_version=confirmed.requirement_version,
        entry_node="outline",
        terminal_nodes={"render"},
        nodes=nodes,
        edges=[edge for edge in edges if edge is not None],
        limits=BudgetLimits(max_input_tokens=64_000, max_output_tokens=16_000),
        rationale="Deterministic fail-closed fallback after candidate validation failure.",
    )
