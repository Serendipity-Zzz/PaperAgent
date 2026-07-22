from __future__ import annotations

from uuid import UUID

from paperagent.engine.budgets import BudgetLimits
from paperagent.orchestration.plan_models import (
    ApprovalRequirement,
    CandidateEdge,
    CandidateNode,
    CandidatePlan,
)
from paperagent.rendering.delivery import DocumentAction, DocumentActionIntent


class PresentationRevisionSubgraph:
    """Minimal immutable revision graph for cover and page-chrome changes."""

    REQUIRED_TOOLS = (
        "document.resolve_revision",
        "document.presentation.patch",
        "document.layout.resolve",
        "document.render",
        "document.validate_delivery",
    )

    def build_plan(
        self,
        intent: DocumentActionIntent,
        *,
        requirement_id: UUID,
        available_tools: set[str],
    ) -> CandidatePlan:
        if intent.action is not DocumentAction.REVISE_PRESENTATION:
            raise ValueError("presentation revision graph requires revise_presentation intent")
        missing = [name for name in self.REQUIRED_TOOLS if name not in available_tools]
        if missing:
            raise ValueError(
                "document presentation capability is unavailable: " + ", ".join(missing)
            )
        formats = [item.value for item in intent.target_formats]
        nodes = [
            CandidateNode(
                node_id="document_resolve_revision",
                agent_type="render_agent",
                objective="Resolve exactly one canonical revision without rewriting its content.",
                output_keys=["document_revision"],
                required_tools=["document.resolve_revision"],
                success_criteria=["one canonical revision is resolved"],
            ),
            CandidateNode(
                node_id="document_presentation_patch",
                agent_type="render_agent",
                objective=(
                    "Apply only the requested cover or page-chrome operations as one immutable "
                    "revision. Preserve content, structure, citations, experiments and assets."
                ),
                input_refs=["document_revision"],
                output_keys=["presentation_revision"],
                required_tools=["document.presentation.patch"],
                approval=ApprovalRequirement(
                    action="patch_document_presentation",
                    risk="creates one managed immutable document revision",
                    consequence="prior revisions and all non-presentation hashes remain unchanged",
                ),
                success_criteria=[
                    "only presentation_hash changes unless the request also changes content"
                ],
            ),
            CandidateNode(
                node_id="document_presentation_layout",
                agent_type="render_agent",
                objective=(
                    "Negotiate renderer capability and layout only for the affected presentation "
                    "and requested or previously delivered formats."
                ),
                input_refs=["presentation_revision"],
                output_keys=["presentation_layout"],
                required_tools=["document.layout.resolve"],
                success_criteria=["unsupported hard requirements fail before rendering"],
            ),
            CandidateNode(
                node_id="document_render",
                agent_type="render_agent",
                objective=(
                    "Render the patched canonical revision without Writer, Experiment, Literature "
                    "or Image Agent execution."
                ),
                input_refs=["presentation_revision", "presentation_layout"],
                output_keys=["rendered_artifacts"],
                required_tools=["document.render"],
                approval=ApprovalRequirement(
                    action="render_document_artifacts",
                    risk="writes new managed output artifacts",
                    consequence="canonical source and previous outputs remain immutable",
                ),
                success_criteria=["every target format has one verified artifact"],
            ),
            CandidateNode(
                node_id="document_validate_delivery",
                agent_type="review_agent",
                objective=(
                    "Validate presentation expectations, native structure and canonical lineage "
                    "before publishing the revised files."
                ),
                input_refs=["rendered_artifacts"],
                output_keys=["delivery_validation"],
                required_tools=["document.validate_delivery"],
                success_criteria=["all presentation expectations pass machine-verifiable QA"],
            ),
        ]
        return CandidatePlan(
            requirement_id=requirement_id,
            requirement_version=1,
            entry_node=nodes[0].node_id,
            terminal_nodes={nodes[-1].node_id},
            nodes=nodes,
            edges=[
                CandidateEdge(source=nodes[index].node_id, target=nodes[index + 1].node_id)
                for index in range(len(nodes) - 1)
            ],
            limits=BudgetLimits(max_input_tokens=24_000, max_output_tokens=4_000),
            rationale=(
                "Presentation-only requests use a conditional immutable revision graph and never "
                "invalidate research, writing or experiment nodes."
            ),
            assumptions=[
                "document_action:revise_presentation",
                "affected_domains:presentation",
                "preserve_content:true",
                "preserve_assets:true",
                "rerun_experiment:false",
                "target_formats:" + (",".join(formats) if formats else "current_delivered"),
            ],
        )
