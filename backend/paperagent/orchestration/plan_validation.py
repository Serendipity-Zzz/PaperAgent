from __future__ import annotations

from collections import defaultdict, deque

from pydantic import Field

from paperagent.orchestration.plan_models import CandidatePlan
from paperagent.rendering.delivery import DocumentAction
from paperagent.schemas.common import StrictModel
from paperagent.tools import SideEffect, ToolRegistry


class PlanValidationIssue(StrictModel):
    code: str
    message: str
    node_id: str | None = None


class PlanValidationReport(StrictModel):
    valid: bool
    issues: list[PlanValidationIssue] = Field(default_factory=list)
    topological_order: list[str] = Field(default_factory=list)


class PlanValidator:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        allowed_agents: set[str],
    ) -> None:
        self.registry = registry
        self.allowed_agents = allowed_agents

    def validate(
        self,
        plan: CandidatePlan,
        *,
        document_action: DocumentAction | None = None,
    ) -> PlanValidationReport:
        issues: list[PlanValidationIssue] = []
        nodes = {node.node_id: node for node in plan.nodes}
        for node in plan.nodes:
            if node.agent_type not in self.allowed_agents:
                issues.append(
                    PlanValidationIssue(
                        code="AGENT_NOT_ALLOWED",
                        message=f"agent is not registered: {node.agent_type}",
                        node_id=node.node_id,
                    )
                )
            for tool_name in node.required_tools:
                try:
                    tool = self.registry.resolve(tool_name, agent_type=node.agent_type)
                except (KeyError, PermissionError) as error:
                    issues.append(
                        PlanValidationIssue(
                            code="TOOL_NOT_ALLOWED",
                            message=str(error),
                            node_id=node.node_id,
                        )
                    )
                    continue
                if tool.spec.side_effect is not SideEffect.NONE and node.approval is None:
                    issues.append(
                        PlanValidationIssue(
                            code="APPROVAL_MISSING",
                            message=f"side-effect tool requires approval: {tool_name}",
                            node_id=node.node_id,
                        )
                    )
        order = self._topological_order(plan)
        if not order:
            issues.append(
                PlanValidationIssue(code="GRAPH_CYCLE", message="candidate plan contains a cycle")
            )
        elif set(order) != set(nodes):
            issues.append(
                PlanValidationIssue(
                    code="UNREACHABLE_NODE",
                    message="candidate plan has unreachable nodes",
                )
            )
        produced: set[str] = set()
        for node_id in order:
            node = nodes[node_id]
            missing = set(node.input_refs) - produced
            if missing:
                issues.append(
                    PlanValidationIssue(
                        code="INPUT_NOT_PRODUCED",
                        message=f"inputs have no prior producer: {sorted(missing)}",
                        node_id=node_id,
                    )
                )
            produced.update(node.output_keys)
        if document_action is DocumentAction.CONVERT_FORMAT or (
            "document_action:convert_format" in plan.assumptions
        ):
            required_nodes = {
                "document_resolve_revision",
                "document_asset_barrier",
                "document_render",
                "document_validate_delivery",
            }
            missing_nodes = required_nodes - set(nodes)
            if missing_nodes:
                issues.append(
                    PlanValidationIssue(
                        code="DELIVERY_INVARIANT_MISSING",
                        message=(
                            "format conversion is missing invariant nodes: "
                            f"{sorted(missing_nodes)}"
                        ),
                    )
                )
            forbidden = [
                node.node_id
                for node in plan.nodes
                if node.agent_type in {"writer_agent", "experiment_agent"}
            ]
            if forbidden:
                issues.append(
                    PlanValidationIssue(
                        code="DELIVERY_SIDE_EFFECT_FORBIDDEN",
                        message=(
                            "format conversion cannot rewrite content or rerun experiments: "
                            f"{forbidden}"
                        ),
                    )
                )
            render = nodes.get("document_render")
            if render is not None and "document_revision" not in render.input_refs:
                issues.append(
                    PlanValidationIssue(
                        code="DELIVERY_REVISION_INPUT_MISSING",
                        message="format conversion render must consume a canonical revision",
                        node_id=render.node_id,
                    )
                )
        if document_action is DocumentAction.RESTYLE or (
            "document_action:restyle" in plan.assumptions
        ):
            forbidden = [
                node.node_id
                for node in plan.nodes
                if node.agent_type in {"writer_agent", "experiment_agent"}
            ]
            if forbidden:
                issues.append(
                    PlanValidationIssue(
                        code="RESTYLE_SIDE_EFFECT_FORBIDDEN",
                        message=(
                            "typography-only changes cannot rewrite content or rerun "
                            f"experiments: {forbidden}"
                        ),
                    )
                )
            if not any(
                node.agent_type == "render_agent"
                and "document.render" in node.required_tools
                for node in plan.nodes
            ):
                issues.append(
                    PlanValidationIssue(
                        code="RESTYLE_RENDER_MISSING",
                        message="typography-only changes require the canonical renderer",
                    )
                )
        if document_action is DocumentAction.REVISE_PRESENTATION or (
            "document_action:revise_presentation" in plan.assumptions
        ):
            required_nodes = {
                "document_resolve_revision",
                "document_presentation_patch",
                "document_presentation_layout",
                "document_render",
                "document_validate_delivery",
            }
            missing_nodes = required_nodes - set(nodes)
            if missing_nodes:
                issues.append(
                    PlanValidationIssue(
                        code="PRESENTATION_INVARIANT_MISSING",
                        message=(
                            "presentation-only revision is missing invariant nodes: "
                            f"{sorted(missing_nodes)}"
                        ),
                    )
                )
            forbidden = [
                node.node_id
                for node in plan.nodes
                if node.agent_type
                in {"writer_agent", "experiment_agent", "evidence_agent", "visual_agent"}
            ]
            if forbidden:
                issues.append(
                    PlanValidationIssue(
                        code="PRESENTATION_SIDE_EFFECT_FORBIDDEN",
                        message=(
                            "presentation-only changes cannot rewrite, research, run experiments "
                            f"or generate images: {forbidden}"
                        ),
                    )
                )
        return PlanValidationReport(
            valid=not issues,
            issues=issues,
            topological_order=order,
        )

    @staticmethod
    def _topological_order(plan: CandidatePlan) -> list[str]:
        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree = {node.node_id: 0 for node in plan.nodes}
        for edge in plan.edges:
            outgoing[edge.source].append(edge.target)
            indegree[edge.target] += 1
        queue = deque([plan.entry_node] if indegree[plan.entry_node] == 0 else [])
        order: list[str] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for target in outgoing[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        return order
