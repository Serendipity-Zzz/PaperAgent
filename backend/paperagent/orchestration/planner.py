from __future__ import annotations

import json
import re
from itertools import pairwise
from typing import Any

from pydantic import Field, ValidationError

from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.engine import AgentLoop, AgentLoopRequest, BudgetLimits
from paperagent.orchestration.plan_models import (
    CandidateEdge,
    CandidateEdgeCondition,
    CandidateNode,
    CandidatePlan,
)
from paperagent.prompts import PromptSelectionContext, default_prompt_compiler
from paperagent.providers import ChatMessage
from paperagent.schemas.common import StrictModel


class CandidatePlanSet(StrictModel):
    candidates: list[CandidatePlan] = Field(min_length=1, max_length=5)
    recommended_index: int = Field(default=0, ge=0)


class CandidatePlanGenerator:
    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self.compiler = default_prompt_compiler()
        self.last_schema_repair_count = 0
        self.last_schema_errors: list[str] = []
        self.last_schema_projection_used = False

    async def generate(
        self,
        requirement: RequirementSpec,
        *,
        project_id: str,
        available_tools: list[str],
    ) -> CandidatePlanSet:
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("dynamic planning requires confirmed requirements")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        compiled = self.compiler.compile(
            PromptSelectionContext(
                agent_type="supervisor",
                task="generate_candidate_plan",
                document_type=confirmed.document_type.value,
                language=confirmed.primary_language.value,
                features={
                    name
                    for name, enabled in {
                        "experiment": confirmed.requires_experiment,
                        "generated_image": confirmed.requires_generated_image,
                        "data_chart": confirmed.requires_data_chart,
                        "literature": confirmed.requires_literature_search,
                    }.items()
                    if enabled
                },
            ),
            [
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "confirmed_requirement": confirmed.model_dump(mode="json"),
                            "available_tools": available_tools,
                            "instruction": (
                                "Propose 1-3 materially different executable candidate plans. "
                                "Use conditional repair edges and approvals for side effects."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )
        messages = list(compiled.messages)
        plan_set: CandidatePlanSet | None = None
        self.last_schema_repair_count = 0
        self.last_schema_errors = []
        self.last_schema_projection_used = False
        for attempt in range(3):
            result = await self.loop.run(
                AgentLoopRequest(
                    project_id=project_id,
                    agent_type="supervisor",
                    messages=messages,
                    tool_names=[],
                    response_schema=CandidatePlanSet.model_json_schema(),
                    temperature=0.1 if attempt == 0 else 0,
                    max_rounds=3,
                )
            )
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.content.strip())
            try:
                plan_set = CandidatePlanSet.model_validate_json(content)
                break
            except ValidationError as error:
                self.last_schema_repair_count += 1
                error_text = str(error)[:6_000]
                self.last_schema_errors.append(error_text)
                plan_set = self._project_plan_set(content)
                if plan_set is None:
                    plan_set = self._normalize_common_plan_dialect(content)
                if plan_set is None:
                    plan_set = self._adapt_semantic_plan_set(
                        content,
                        requirement=requirement,
                        available_tools=available_tools,
                    )
                if plan_set is not None:
                    self.last_schema_projection_used = True
                    break
                if attempt == 2:
                    raise
                messages.extend(
                    [
                        ChatMessage(role="assistant", content=result.content),
                        ChatMessage(
                            role="user",
                            content=(
                                "The previous candidate-plan JSON failed strict schema "
                                "validation. Correct the same plans without adding new work, "
                                "facts, tools, or approvals. Return only one JSON object that "
                                "matches the supplied schema exactly; remove unknown fields. "
                                f"Validation errors:\n{error_text}"
                            ),
                        ),
                    ]
                )
        assert plan_set is not None
        if plan_set.recommended_index >= len(plan_set.candidates):
            raise ValueError("recommended candidate index is out of range")
        return plan_set

    @staticmethod
    def _project_plan_set(content: str) -> CandidatePlanSet | None:
        """Conservative alias cleanup after two model-led schema repairs fail.

        Projection never fabricates plan nodes or drops invalid nested plan data. It only
        accepts a known top-level alias and removes explanatory wrapper fields; the complete
        strict CandidatePlan schema is still validated afterwards.
        """

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        candidates = raw.get("candidates", raw.get("plan_candidates"))
        if not isinstance(candidates, list):
            return None
        projected = {
            "candidates": candidates,
            "recommended_index": raw.get("recommended_index", 0),
        }
        try:
            return CandidatePlanSet.model_validate(projected)
        except ValidationError:
            return None

    @classmethod
    def _normalize_common_plan_dialect(cls, content: str) -> CandidatePlanSet | None:
        """Normalize lossless container variations before semantic adaptation.

        OpenAI-compatible providers sometimes serialize a list of schema-valid nodes as an
        id-keyed object and emit a legacy budget object. This conversion preserves every node
        field, replaces only the two containers, and still requires full strict validation.
        """

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        candidates = raw.get("candidates", raw.get("plan_candidates"))
        if not isinstance(candidates, list):
            return None
        normalized: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return None
            projected = dict(candidate)
            nodes = projected.get("nodes")
            if isinstance(nodes, dict):
                if not all(
                    isinstance(node_id, str) and isinstance(node, dict)
                    for node_id, node in nodes.items()
                ):
                    return None
                projected["nodes"] = [
                    ({"node_id": node_id} | node) for node_id, node in nodes.items()
                ]
            projected["limits"] = cls._adapt_limits(projected.get("limits")).model_dump(
                mode="json"
            )
            normalized.append(projected)
        try:
            return CandidatePlanSet.model_validate(
                {
                    "candidates": normalized,
                    "recommended_index": raw.get("recommended_index", 0),
                }
            )
        except ValidationError:
            return None

    @classmethod
    def _adapt_semantic_plan_set(
        cls,
        content: str,
        *,
        requirement: RequirementSpec,
        available_tools: list[str],
    ) -> CandidatePlanSet | None:
        """Translate a common semantic plan dialect, then revalidate the full executable plan.

        Some OpenAI-compatible providers honor the JSON response mode but use descriptive
        ``action/dependencies/outputs`` node fields. This adapter preserves those semantics and
        binds requirement identity and budgets locally. It refuses malformed identifiers,
        missing actions, unknown dependencies, duplicate nodes, and invalid graph references.
        """

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        raw_candidates = raw.get("candidates", raw.get("plan_candidates"))
        if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= 5:
            return None
        confirmed = requirement.confirmed_requirement
        if requirement.status is not RequirementStatus.CONFIRMED or confirmed is None:
            return None
        converted: list[CandidatePlan] = []
        try:
            for raw_plan in raw_candidates:
                if not isinstance(raw_plan, dict):
                    return None
                raw_nodes_value = raw_plan.get("nodes")
                if isinstance(raw_nodes_value, dict):
                    raw_nodes = [
                        ({"node_id": node_id} | node)
                        for node_id, node in raw_nodes_value.items()
                        if isinstance(node_id, str) and isinstance(node, dict)
                    ]
                    if len(raw_nodes) != len(raw_nodes_value):
                        return None
                elif isinstance(raw_nodes_value, list):
                    raw_nodes = raw_nodes_value
                else:
                    return None
                if not raw_nodes:
                    return None
                id_map: dict[str, str] = {}
                normalized_ids: set[str] = set()
                for index, raw_node in enumerate(raw_nodes, start=1):
                    if not isinstance(raw_node, dict):
                        return None
                    raw_node_id = raw_node.get("node_id")
                    if not isinstance(raw_node_id, str) or raw_node_id in id_map:
                        return None
                    normalized = cls._normalize_node_id(raw_node_id, index)
                    if normalized in normalized_ids:
                        return None
                    id_map[raw_node_id] = normalized
                    normalized_ids.add(normalized)
                nodes: list[CandidateNode] = []
                dependencies: dict[str, list[str]] = {}
                transition_edges: list[CandidateEdge] = []
                for raw_node in raw_nodes:
                    if not isinstance(raw_node, dict):
                        return None
                    raw_node_id = raw_node.get("node_id")
                    if not isinstance(raw_node_id, str):
                        return None
                    node_id = id_map[raw_node_id]
                    objective = cls._first_text(
                        raw_node, "objective", "action", "description", "task", "instruction"
                    )
                    raw_tool = raw_node.get("tool", raw_node.get("tool_name"))
                    tool_name = cls._tool_name(raw_tool, available_tools)
                    if objective is None and tool_name is not None:
                        objective = f"Execute {tool_name} for plan node {node_id}."
                    if objective is None:
                        tool_label = cls._raw_tool_label(raw_tool)
                        if tool_label is not None:
                            objective = f"Execute {tool_label} for plan node {node_id}."
                    if objective is None:
                        return None
                    raw_dependencies = cls._first_value(
                        raw_node, "dependencies", "depends_on", "after"
                    )
                    raw_dependency_list = cls._coerce_string_list(raw_dependencies)
                    dependency_list = (
                        [id_map.get(item, item) for item in raw_dependency_list]
                        if raw_dependency_list is not None
                        else []
                    )
                    raw_outputs = cls._first_value(
                        raw_node, "output_keys", "outputs", "output", "output_formats"
                    )
                    parameters = raw_node.get("parameters")
                    if raw_outputs is None and isinstance(parameters, dict):
                        raw_outputs = cls._first_value(
                            parameters, "outputs", "output", "output_formats"
                        )
                    output_list = cls._coerce_string_list(raw_outputs)
                    if output_list is None:
                        output_list = []
                    dependencies[node_id] = dependency_list
                    raw_node_text = json.dumps(raw_node, ensure_ascii=False)
                    action_text = f"{node_id} {objective} {raw_node_text}"
                    required_tools = [
                        name
                        for name in available_tools
                        if name.casefold() in action_text.casefold()
                    ]
                    if tool_name is not None and tool_name not in required_tools:
                        required_tools.append(tool_name)
                    node_type = str(raw_node.get("type", raw_node.get("kind", "")))
                    if tool_name is not None and not node_type:
                        node_type = "tool"
                    adapted_transitions = cls._adapt_transitions(
                        node_id, raw_node.get("transitions")
                    )
                    if adapted_transitions is None:
                        return None
                    transition_edges.extend(adapted_transitions)
                    next_nodes = cls._coerce_string_list(raw_node.get("next_nodes"))
                    if next_nodes:
                        transition_edges.extend(
                            CandidateEdge(
                                source=node_id,
                                target=id_map.get(target, target),
                            )
                            for target in next_nodes
                        )
                    nodes.append(
                        CandidateNode(
                            node_id=node_id,
                            agent_type=cls._agent_type(node_id, node_type),
                            objective=objective,
                            input_refs=dependency_list,
                            output_keys=output_list,
                            required_tools=required_tools,
                            allow_parallel=node_type.casefold() == "parallel",
                            success_criteria=[f"{node_id} completes without an unresolved error"],
                        )
                    )
                known = {node.node_id for node in nodes}
                if len(known) != len(nodes):
                    return None
                explicit_edges = cls._adapt_edges(raw_plan.get("edges"))
                if explicit_edges is None:
                    return None
                if (
                    not explicit_edges
                    and not transition_edges
                    and not any(dependencies.values())
                    and len(nodes) > 1
                ):
                    for previous, current in pairwise(nodes):
                        dependencies[current.node_id] = [previous.node_id]
                if any(source not in known for refs in dependencies.values() for source in refs):
                    return None
                edges = [
                    CandidateEdge(source=source, target=target)
                    for target, refs in dependencies.items()
                    for source in refs
                ]
                edges.extend(explicit_edges)
                edges.extend(transition_edges)
                if not edges and len(nodes) > 1:
                    for previous, current in pairwise(nodes):
                        edges.append(CandidateEdge(source=previous.node_id, target=current.node_id))
                unique_edges = {
                    (
                        id_map.get(edge.source, edge.source),
                        id_map.get(edge.target, edge.target),
                        edge.condition,
                    ): edge.model_copy(
                        update={
                            "source": id_map.get(edge.source, edge.source),
                            "target": id_map.get(edge.target, edge.target),
                        }
                    )
                    for edge in edges
                }
                edges = list(unique_edges.values())
                if any(edge.source not in known or edge.target not in known for edge in edges):
                    return None
                incoming = {edge.target for edge in edges}
                outgoing = {edge.source for edge in edges}
                entry_candidates = [node.node_id for node in nodes if node.node_id not in incoming]
                terminal_nodes = {node.node_id for node in nodes if node.node_id not in outgoing}
                if len(entry_candidates) != 1 or not terminal_nodes:
                    return None
                limits = cls._adapt_limits(raw_plan.get("limits"))
                converted.append(
                    CandidatePlan(
                        requirement_id=confirmed.requirement_id,
                        requirement_version=confirmed.requirement_version,
                        entry_node=entry_candidates[0],
                        terminal_nodes=terminal_nodes,
                        nodes=nodes,
                        edges=edges,
                        limits=limits,
                        max_repair_rounds=int(raw_plan.get("max_repair_rounds", 3)),
                        rationale=str(
                            raw_plan.get(
                                "rationale",
                                "Provider semantic plan normalized to the executable schema.",
                            )
                        ),
                        assumptions=cls._safe_string_list(raw_plan.get("assumptions", [])),
                    )
                )
            recommended = raw.get("recommended_index", 0)
            if not isinstance(recommended, int):
                recommended = 0
            return CandidatePlanSet(candidates=converted, recommended_index=recommended)
        except (TypeError, ValueError, ValidationError):
            return None

    @staticmethod
    def _string_list(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    @classmethod
    def _safe_string_list(cls, value: Any) -> list[str]:
        return list(value) if cls._string_list(value) else []

    @staticmethod
    def _first_value(payload: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in payload:
                return payload[name]
        return None

    @classmethod
    def _first_text(cls, payload: dict[str, Any], *names: str) -> str | None:
        value = cls._first_value(payload, *names)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _coerce_string_list(value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        if isinstance(value, dict) and all(isinstance(item, str) for item in value):
            return list(value)
        return None

    @staticmethod
    def _tool_name(value: Any, available_tools: list[str]) -> str | None:
        candidate: str | None = None
        if isinstance(value, str):
            candidate = value
        elif isinstance(value, dict):
            for name in ("name", "tool_name", "id"):
                if isinstance(value.get(name), str):
                    candidate = value[name]
                    break
        if candidate is None:
            return None
        return next(
            (name for name in available_tools if name.casefold() == candidate.casefold()),
            None,
        )

    @staticmethod
    def _raw_tool_label(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()[:255]
        if isinstance(value, dict):
            for name in ("name", "tool_name", "id"):
                candidate = value.get(name)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()[:255]
        return None

    @staticmethod
    def _adapt_edges(value: Any) -> list[CandidateEdge] | None:
        if value is None:
            return []
        if not isinstance(value, list):
            return None
        edges: list[CandidateEdge] = []
        for item in value:
            if not isinstance(item, dict):
                return None
            source = item.get("source", item.get("from"))
            target = item.get("target", item.get("to"))
            if not isinstance(source, str) or not isinstance(target, str):
                return None
            edges.append(CandidateEdge(source=source, target=target))
        return edges

    @staticmethod
    def _adapt_transitions(source: str, value: Any) -> list[CandidateEdge] | None:
        if value is None:
            return []
        if not isinstance(value, list):
            return None
        conditions = {
            "true": CandidateEdgeCondition.ON_SUCCESS,
            "success": CandidateEdgeCondition.ON_SUCCESS,
            "on_success": CandidateEdgeCondition.ON_SUCCESS,
            "false": CandidateEdgeCondition.REPAIR_REQUIRED,
            "failure": CandidateEdgeCondition.REPAIR_REQUIRED,
            "repair_required": CandidateEdgeCondition.REPAIR_REQUIRED,
            "approved": CandidateEdgeCondition.APPROVED,
            "rejected": CandidateEdgeCondition.REJECTED,
            "needs_input": CandidateEdgeCondition.NEEDS_INPUT,
            "always": CandidateEdgeCondition.ALWAYS,
        }
        edges: list[CandidateEdge] = []
        for item in value:
            if not isinstance(item, dict):
                return None
            target = item.get("target", item.get("next_node"))
            if not isinstance(target, str):
                return None
            raw_condition = str(item.get("condition", "on_success")).casefold()
            condition = conditions.get(raw_condition)
            if condition is None:
                return None
            edges.append(CandidateEdge(source=source, target=target, condition=condition))
        return edges

    @staticmethod
    def _agent_type(node_id: str, node_type: str) -> str:
        value = f"{node_id} {node_type}".casefold()
        routes = (
            (("outline", "framework"), "outline_agent"),
            (("literature", "evidence", "retrieval", "search"), "evidence_agent"),
            (("experiment",), "experiment_agent"),
            (("chart", "visual"), "chart_agent"),
            (("write", "draft", "report"), "writer_agent"),
            (("review", "audit"), "review_agent"),
            (("render", "export"), "render_agent"),
        )
        for markers, agent_type in routes:
            if any(marker in value for marker in markers):
                return agent_type
        return "supervisor_agent"

    @staticmethod
    def _normalize_node_id(value: str, index: int) -> str:
        normalized = re.sub(r"[^a-z0-9_-]+", "_", value.casefold()).strip("_-")
        if not normalized or not normalized[0].isalpha():
            normalized = f"node_{index}_{normalized}".rstrip("_")
        if len(normalized) < 2:
            normalized = f"{normalized}_node"
        return normalized[:128]

    @staticmethod
    def _adapt_limits(value: Any) -> BudgetLimits:
        raw = value if isinstance(value, dict) else {}
        timeout_minutes = raw.get("timeout_minutes", raw.get("max_duration_minutes"))
        elapsed = raw.get("max_elapsed_ms", 300_000)
        timeout_seconds = raw.get("max_duration_sec")
        raw_tool_calls = raw.get("max_tool_calls", raw.get("max_api_calls"))
        max_tool_calls = (
            raw_tool_calls
            if isinstance(raw_tool_calls, int) and 0 <= raw_tool_calls <= 1_000
            else 20
        )
        if isinstance(timeout_minutes, (int, float)) and timeout_minutes > 0:
            elapsed = int(timeout_minutes * 60_000)
        elif isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            elapsed = int(timeout_seconds * 1_000)
        return BudgetLimits(
            max_input_tokens=(
                int(raw["max_input_tokens"])
                if isinstance(raw.get("max_input_tokens"), int)
                and raw["max_input_tokens"] > 0
                else 64_000
            ),
            max_output_tokens=(
                int(raw["max_output_tokens"])
                if isinstance(raw.get("max_output_tokens"), int)
                and raw["max_output_tokens"] > 0
                else 16_000
            ),
            max_tool_calls=max_tool_calls,
            max_elapsed_ms=(
                int(elapsed)
                if isinstance(elapsed, (int, float)) and elapsed > 0
                else 300_000
            ),
        )
