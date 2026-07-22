from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast
from uuid import UUID, uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import JsonValue

from paperagent.agents.presentation_intent import PresentationChangeIntentClassifier
from paperagent.engine.agent_loop import (
    AgentLoop,
    AgentLoopLimitError,
    AgentLoopRequest,
    AgentLoopResult,
)
from paperagent.engine.budgets import BudgetLimits
from paperagent.orchestration.compiler import TaskGraphCompiler
from paperagent.orchestration.document_delivery import (
    DocumentDeliverySubgraph,
    DocumentIntentClassifier,
)
from paperagent.orchestration.document_production import (
    DocumentProductionSubgraph,
    DocumentRepairPlanner,
    requires_document_assets,
)
from paperagent.orchestration.plan_models import (
    ApprovalRequirement,
    CandidateEdge,
    CandidateNode,
    CandidatePlan,
)
from paperagent.orchestration.plan_validation import PlanValidationReport, PlanValidator
from paperagent.orchestration.planner import CandidatePlanSet
from paperagent.orchestration.presentation_revision import PresentationRevisionSubgraph
from paperagent.presentation import extract_explicit_presentation
from paperagent.providers import Capability, ChatMessage, Usage
from paperagent.rendering.delivery import DocumentAction
from paperagent.tools import (
    ToolCall,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
)

ALLOWED_INTERACTIVE_AGENTS = {
    "supervisor",
    "requirement_agent",
    "evidence_agent",
    "experiment_agent",
    "writer_agent",
    "review_agent",
    "render_agent",
    "repair_planner",
    "visual_agent",
    "artifact_agent",
    "translation_agent",
}


class InteractivePlanResult(TypedDict):
    plan: CandidatePlan
    validation: PlanValidationReport
    source: str
    planning_error: str | None


def _needs(text: str, patterns: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(re.search(pattern, folded, re.I) for pattern in patterns)


def _artifact_relation(text: str) -> str | None:
    relations = (
        ("source", (r"源码|代码|\.py\b|source|script",)),
        ("data", (r"数据|\.csv\b|dataset",)),
        ("figure", (r"图片|图表|实验图|\.svg\b|figure|image",)),
        ("log", (r"日志|stdout|stderr|\blog\b",)),
        ("output", (r"报告|pdf|docx|word|markdown|\bmd\b|document",)),
    )
    for relation, patterns in relations:
        if _needs(text, patterns):
            return relation
    return None


def _requested_document_formats(text: str) -> list[str]:
    formats: list[str] = []
    bundle_pattern = (
        r"\b(?:markdown|md)[\s_-]+bundle\b|"
        r"\b(?:markdown|md)[\s_-]*(?:portable[\s_-]*)?package\b|"
        r"(?:markdown|md)\s*(?:打包|便携包|资源包)"
    )
    bundle_requested = bool(re.search(bundle_pattern, text, re.I))
    text_without_bundle = re.sub(bundle_pattern, "", text, flags=re.I)
    # Python's Unicode ``\b`` does not separate a CJK character from an ASCII
    # letter.  Consequently a very natural request such as ``结果输出为pdf`` was
    # planned as a render job but produced an empty format list at execution
    # time.  Delimit only on ASCII identifiers so CJK-adjacent format names are
    # recognized without accepting substrings such as ``pdfium``.
    ascii_left = r"(?<![A-Za-z0-9])"
    ascii_right = r"(?![A-Za-z0-9])"
    for format_name, patterns in (
        ("pdf", (rf"{ascii_left}pdf{ascii_right}",)),
        (
            "docx",
            (
                rf"{ascii_left}docx{ascii_right}|"
                rf"{ascii_left}word{ascii_right}",
            ),
        ),
        (
            "md",
            (
                rf"{ascii_left}markdown{ascii_right}|"
                rf"\.md{ascii_right}|{ascii_left}md{ascii_right}",
            ),
        ),
    ):
        candidate = text_without_bundle if format_name == "md" else text
        if _needs(candidate, patterns):
            formats.append(format_name)
    if bundle_requested:
        formats.append("md_bundle")
    return formats


def _document_progress_phase(node_id: str) -> str | None:
    return {
        "document_resolve_revision": "revision_resolution",
        "document_asset_barrier": "asset_barrier",
        "document_validate_delivery": "quality_assurance",
        "document_classify": "classification",
        "document_structure": "structure",
        "document_presentation_resolve": "presentation_resolution",
        "document_compose": "composition",
        "document_presentation_patch": "presentation_revision",
        "document_presentation_layout": "layout",
        "document_assets": "asset_barrier",
        "document_citations": "citations",
        "document_layout": "layout",
        "document_render": "compilation",
        "document_qa": "quality_assurance",
    }.get(node_id)


def _document_progress_summary(phase: str | None, *, completed: bool = False) -> str | None:
    if phase is None:
        return None
    labels = {
        "revision_resolution": "正在定位已有文档版本",
        "classification": "正在判断文档类型与约束",
        "structure": "正在设计章节结构与标题层级",
        "presentation_resolution": "正在解析封面与页眉页脚要求",
        "composition": "正在生成并校验统一文档结构",
        "presentation_revision": "正在创建封面与页眉页脚修订",
        "asset_barrier": "正在校验图片并准备多格式资源",
        "citations": "正在绑定和格式化可追溯引用",
        "layout": "正在解析页面、字体与排版能力",
        "compilation": "正在编译所需文档格式",
        "quality_assurance": "正在检查结构、图片、版式与交付证据",
    }
    summary = labels.get(phase)
    if summary is None:
        return None
    return f"{summary.removeprefix('正在')}已完成" if completed else summary


class CapabilityPlanFactory:
    """Fail-closed capability planner; topology changes with request and registry."""

    def build(
        self,
        user_message: str,
        *,
        requirement_id: UUID,
        available_tools: list[str],
    ) -> CandidatePlan:
        available = set(available_tools)
        document_intent = DocumentIntentClassifier().classify(user_message)
        if document_intent.action is DocumentAction.CONVERT_FORMAT:
            return DocumentDeliverySubgraph().build_plan(
                document_intent,
                requirement_id=requirement_id,
                available_tools=available,
            )
        if document_intent.action is DocumentAction.REVISE_PRESENTATION:
            return PresentationRevisionSubgraph().build_plan(
                document_intent,
                requirement_id=requirement_id,
                available_tools=available,
            )
        if document_intent.action is DocumentAction.RESTYLE:
            if "document.render" not in available:
                raise ValueError("document restyle capability is unavailable: document.render")
            return CandidatePlan(
                requirement_id=requirement_id,
                requirement_version=1,
                entry_node="document_restyle",
                terminal_nodes={"document_restyle"},
                nodes=[
                    CandidateNode(
                        node_id="document_restyle",
                        agent_type="render_agent",
                        objective=(
                            "Resolve the canonical document, create a style-only revision, "
                            "and render the requested formats without rewriting content or "
                            "rerunning experiments."
                        ),
                        output_keys=["render_result"],
                        required_tools=["document.render"],
                        approval=ApprovalRequirement(
                            action="write_document_artifact",
                            risk="creates a new style-only revision and local artifact",
                            consequence="body content and source assets remain immutable",
                        ),
                        success_criteria=[
                            "style hash changes while content and asset hashes remain stable"
                        ],
                    )
                ],
                edges=[],
                limits=BudgetLimits(max_input_tokens=16_000, max_output_tokens=4_000),
                assumptions=[
                    "document_action:restyle",
                    "preserve_content:true",
                    "preserve_assets:true",
                    "rerun_experiment:false",
                ],
                rationale=("Typography changes use a deterministic style-only revision path."),
            )
        nodes: list[CandidateNode] = []
        edges: list[CandidateEdge] = []
        prior: str | None = None
        produced: list[str] = []

        def append(node: CandidateNode) -> None:
            nonlocal prior, produced
            nodes.append(node)
            if prior is not None:
                edges.append(CandidateEdge(source=prior, target=node.node_id))
            prior = node.node_id
            produced.extend(node.output_keys)

        literature = _needs(
            user_message,
            (r"文献|引用|参考资料|检索|论文依据", r"literature|citation|reference|evidence"),
        )
        experiment = _needs(
            user_message,
            (
                r"实验|运行.*代码|跑.*代码|生成.*(?:数据|曲线|实验图)",
                r"experiment|run .*code|execute",
            ),
        )
        artifact_lookup = _needs(
            user_message,
            (
                r"刚才|此前|之前.*(?:源码|代码|文件|数据|图片)",
                r"previous|earlier.*(?:source|file|data)",
            ),
        )
        explicit_recompute = _needs(
            user_message,
            (
                r"重新(?:运行|执行|生成|计算|绘制)|再(?:跑|运行|执行)一次",
                r"re-?run|run again|recompute|regenerate",
            ),
        )
        recompute_denied = _needs(
            user_message,
            (
                r"(?:不要|无需|不必|禁止|别).{0,8}(?:重新|再)(?:运行|执行|生成|计算|绘制|跑)",
                r"(?:do not|don't|without).{0,20}(?:re-?run|run again|recompute|regenerate)",
            ),
        )
        explicit_recompute = explicit_recompute and not recompute_denied
        experiment_denied = recompute_denied or _needs(
            user_message,
            (
                r"(?:不要|无需|不必|禁止|别).{0,8}(?:重跑|实验|运行.*代码|跑.*代码)",
                r"(?:do not|don't|without).{0,20}(?:experiment|run .*code|execute)",
            ),
        )
        if experiment_denied and not explicit_recompute:
            experiment = False
        # A request to hand back an existing artifact is a retrieval intent, even when
        # the artifact name contains words such as "experiment" or "report".  Do not
        # silently turn it into a new side-effecting run unless the user explicitly
        # asks to recompute or regenerate the material.
        if artifact_lookup and not explicit_recompute:
            experiment = False
        typography = _needs(user_message, (r"字体|字号|宋体|黑体|排版", r"font|typography"))
        render = _needs(
            user_message,
            (r"pdf|docx|word|markdown|md|导出|下载", r"render|export|download"),
        )
        if artifact_lookup and not explicit_recompute and not typography:
            render = False
        translation = _needs(user_message, (r"翻译|中译英|英译中", r"translate|translation"))
        document_request = _needs(
            user_message,
            (
                r"论文|报告|文档|方案|纪要|教程|公文",
                r"paper|report|document|proposal|minutes|tutorial",
            ),
        )

        if artifact_lookup and "artifact.lookup" in available:
            append(
                CandidateNode(
                    node_id="artifact_lookup",
                    agent_type="artifact_agent",
                    objective=(
                        "Locate and return the exact existing artifact without regenerating it."
                    ),
                    output_keys=["artifact_result"],
                    required_tools=["artifact.lookup"],
                    success_criteria=["artifact identity and hash are preserved"],
                )
            )
        if literature and "knowledge.search" in available:
            append(
                CandidateNode(
                    node_id="evidence",
                    agent_type="evidence_agent",
                    objective="Retrieve eligible evidence with source locators for this request.",
                    output_keys=["evidence_result"],
                    required_tools=["knowledge.search"],
                    success_criteria=["evidence is traceable to project knowledge"],
                )
            )
        execution_tools = [
            name
            for name in (
                "machine.inspect",
                "environment.prepare",
                "code.materialize",
                "process.execute",
                "result.collect",
            )
            if name in available
        ]
        if experiment and "process.execute" in execution_tools:
            append(
                CandidateNode(
                    node_id="experiment",
                    agent_type="experiment_agent",
                    objective=(
                        "Assess feasibility, materialize exact source, execute it in the "
                        "managed run workspace, and collect real results."
                    ),
                    input_refs=list(produced),
                    output_keys=["experiment_result"],
                    required_tools=execution_tools,
                    approval=ApprovalRequirement(
                        action="execute_local_code",
                        risk="local code consumes machine resources",
                        consequence="writes are restricted to the managed run workspace",
                    ),
                    success_criteria=["execution record and hashed artifacts exist"],
                )
            )
        pipeline_available = {
            "document.classify",
            "document.structure.plan",
            "document.compose",
        } <= available
        if document_request and not typography and pipeline_available:
            subgraph = DocumentProductionSubgraph().build_plan(
                user_message,
                requirement_id=requirement_id,
                available_tools=available,
                input_refs=list(produced),
                render_requested=render,
            )
            if prior is not None:
                edges.append(CandidateEdge(source=prior, target=subgraph.entry_node))
            nodes.extend(subgraph.nodes)
            edges.extend(subgraph.edges)
            prior = next(iter(subgraph.terminal_nodes))
            return CandidatePlan(
                requirement_id=requirement_id,
                requirement_version=1,
                entry_node=nodes[0].node_id,
                terminal_nodes={prior},
                nodes=nodes,
                edges=edges,
                limits=BudgetLimits(max_input_tokens=64_000, max_output_tokens=16_000),
                rationale=(
                    "Capability-driven interactive plan composed a reusable "
                    "DocumentProductionSubgraph with optional evidence, experiment, "
                    "asset, citation and renderer nodes."
                ),
            )
        agent_type = "translation_agent" if translation else "writer_agent"
        objective = (
            "Translate the requested content between Chinese and English while preserving "
            "formulae, citations, structure and terminology."
            if translation
            else (
                "Respond to the user and compose the requested document content from prior "
                "node results."
            )
        )
        append(
            CandidateNode(
                node_id="respond",
                agent_type=agent_type,
                objective=objective,
                input_refs=list(produced),
                output_keys=["response_result"],
                success_criteria=["response directly satisfies the current user request"],
            )
        )
        if (render or typography) and "document.render" in available:
            append(
                CandidateNode(
                    node_id="render",
                    agent_type="render_agent",
                    objective=(
                        "Apply the requested typography as a new document revision and "
                        "render validated downloadable formats."
                        if typography
                        else "Render the composed content to the requested validated formats."
                    ),
                    input_refs=["response_result"],
                    output_keys=["render_result"],
                    required_tools=["document.render"],
                    approval=ApprovalRequirement(
                        action="write_document_artifact",
                        risk="creates a new local document artifact",
                        consequence="writes only under the managed artifact root",
                    ),
                    success_criteria=["rendered artifact exists and passes format validation"],
                )
            )
        assert nodes and prior is not None
        return CandidatePlan(
            requirement_id=requirement_id,
            requirement_version=1,
            entry_node=nodes[0].node_id,
            terminal_nodes={prior},
            nodes=nodes,
            edges=edges,
            limits=BudgetLimits(max_input_tokens=64_000, max_output_tokens=16_000),
            rationale="Capability-driven fallback assembled from the current request and registry.",
        )


class InteractivePlanGenerator:
    def __init__(self, loop: AgentLoop, registry: ToolRegistry) -> None:
        self.loop = loop
        self.registry = registry
        self.fallback = CapabilityPlanFactory()

    async def generate(
        self,
        request: AgentLoopRequest,
        *,
        available_tools: list[str],
    ) -> InteractivePlanResult:
        requirement_id = uuid4()
        validator = PlanValidator(self.registry, allowed_agents=ALLOWED_INTERACTIVE_AGENTS)
        document_intent = DocumentIntentClassifier().classify(request.messages[-1].content)
        planning_error: str | None = None
        if document_intent.action in {
            DocumentAction.CONVERT_FORMAT,
            DocumentAction.REVISE_PRESENTATION,
            DocumentAction.RESTYLE,
        }:
            deterministic = self.fallback.build(
                request.messages[-1].content,
                requirement_id=requirement_id,
                available_tools=available_tools,
            )
            deterministic_report = validator.validate(
                deterministic,
                document_action=document_intent.action,
            )
            if not deterministic_report.valid:
                raise ValueError(
                    f"deterministic document plan is invalid: {deterministic_report.issues}"
                )
            return {
                "plan": deterministic,
                "validation": deterministic_report,
                "source": "document_invariant",
                "planning_error": None,
            }
        capability_baseline = self.fallback.build(
            request.messages[-1].content,
            requirement_id=requirement_id,
            available_tools=available_tools,
        )
        baseline_tools = {
            tool_name for node in capability_baseline.nodes for tool_name in node.required_tools
        }
        supports_schema = any(
            Capability.STRUCTURED_OUTPUT in provider.config.capabilities
            for provider in self.loop.router.providers
        )
        if supports_schema:
            try:
                result = await self.loop.run(
                    AgentLoopRequest(
                        project_id=request.project_id,
                        agent_type="supervisor",
                        messages=[
                            ChatMessage(
                                role="developer",
                                content=(
                                    "Create a capability-driven execution plan. Do not invent "
                                    "tools or force a fixed paper workflow. Side-effect tools "
                                    "need approval metadata. Return only the strict JSON schema."
                                ),
                            ),
                            ChatMessage(
                                role="user",
                                content=json.dumps(
                                    {
                                        "requirement_id": str(requirement_id),
                                        "requirement_version": 1,
                                        "request": request.messages[-1].content,
                                        "available_tools": available_tools,
                                        "allowed_agents": sorted(ALLOWED_INTERACTIVE_AGENTS),
                                    },
                                    ensure_ascii=False,
                                ),
                            ),
                        ],
                        response_schema=CandidatePlanSet.model_json_schema(),
                        max_rounds=2,
                        temperature=0,
                    )
                )
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.content.strip())
                candidates = CandidatePlanSet.model_validate_json(content)
                ordered = [candidates.candidates[candidates.recommended_index]] + [
                    candidate
                    for index, candidate in enumerate(candidates.candidates)
                    if index != candidates.recommended_index
                ]
                for candidate in ordered:
                    if candidate.requirement_id != requirement_id:
                        continue
                    report = validator.validate(
                        candidate,
                        document_action=document_intent.action,
                    )
                    candidate_tools = {
                        tool_name for node in candidate.nodes for tool_name in node.required_tools
                    }
                    missing_capabilities = sorted(baseline_tools - candidate_tools)
                    if report.valid and not missing_capabilities:
                        return {
                            "plan": candidate,
                            "validation": report,
                            "source": "model",
                            "planning_error": None,
                        }
                    if missing_capabilities:
                        planning_error = (
                            "model candidate omitted request-mandated capabilities: "
                            + ", ".join(missing_capabilities)
                        )
                planning_error = planning_error or (
                    "model candidates failed deterministic validation"
                )
            except Exception as error:
                planning_error = f"{error.__class__.__name__}: {str(error)[:1000]}"
        fallback = capability_baseline
        report = validator.validate(
            fallback,
            document_action=document_intent.action,
        )
        if not report.valid:
            raise ValueError(f"capability fallback plan is invalid: {report.issues}")
        return {
            "plan": fallback,
            "validation": report,
            "source": "capability_fallback",
            "planning_error": planning_error,
        }


def _merge_results(
    left: dict[str, dict[str, object]], right: dict[str, dict[str, object]]
) -> dict[str, dict[str, object]]:
    return left | right


class InteractiveInnerState(TypedDict, total=False):
    node_results: Annotated[dict[str, dict[str, object]], _merge_results]


class InteractiveGraphState(TypedDict, total=False):
    plan: dict[str, object]
    validation: dict[str, object]
    plan_source: str
    planning_error: str | None
    agent_result: dict[str, object]


ResultSink = Callable[[AgentLoopResult], None]
ProgressSink = Callable[[str, dict[str, object]], None]


_MANDATORY_NODE_TOOLS: dict[str, set[str]] = {
    "artifact_agent": {"artifact.lookup"},
    "experiment_agent": {"process.execute", "result.collect"},
    "render_agent": {"document.render"},
}


def _mandatory_node_tools(node: CandidateNode) -> set[str]:
    return (
        set(node.required_tools)
        if node.node_id.startswith("document_")
        else _MANDATORY_NODE_TOOLS.get(node.agent_type, set()) & set(node.required_tools)
    )


def _node_contract_issue(
    node: CandidateNode,
    result: AgentLoopResult,
    *,
    request_text: str | None = None,
    hydrate: Callable[[ToolResult], ToolResult] | None = None,
    expected_formats: list[str] | None = None,
) -> str | None:
    """Reject prose-only completion when a node promises a real side effect."""
    mandatory = _mandatory_node_tools(node)
    if not mandatory:
        return None
    successful: dict[str, ToolResult] = {}
    for message in result.messages:
        if message.role != "tool" or not message.tool_name:
            continue
        try:
            tool_result = ToolResult.model_validate_json(message.content)
        except ValueError:
            continue
        if hydrate is not None:
            tool_result = hydrate(tool_result)
        if tool_result.status is ToolResultStatus.SUCCESS:
            successful[message.tool_name] = tool_result
    missing = sorted(mandatory - set(successful))
    if missing:
        failed_details: list[str] = []
        for message in result.messages:
            if message.role != "tool" or message.tool_name not in missing:
                continue
            try:
                failed = ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
            if hydrate is not None:
                failed = hydrate(failed)
            if failed.error is not None:
                failed_details.append(f"{message.tool_name}: {failed.error.message}")
        detail = f" ({'; '.join(failed_details)})" if failed_details else ""
        return f"required tools did not complete successfully: {', '.join(missing)}{detail}"
    if expected_formats and "document.render" in mandatory:
        delivered_formats: set[str] = set()
        suffix_formats = {
            ".pdf": "pdf",
            ".docx": "docx",
            ".md": "md",
            ".zip": "md_bundle",
            ".tex": "tex",
            ".typ": "typ",
            ".html": "html",
        }
        for message in result.messages:
            if message.role != "tool" or message.tool_name != "document.render":
                continue
            try:
                rendered = ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
            if hydrate is not None:
                rendered = hydrate(rendered)
            if rendered.status is not ToolResultStatus.SUCCESS:
                continue
            name = (
                str(rendered.content.get("name", ""))
                if isinstance(rendered.content, dict)
                else ""
            )
            detected = suffix_formats.get(Path(name).suffix.casefold())
            if detected:
                delivered_formats.add(detected)
        missing_formats = list(
            dict.fromkeys(item for item in expected_formats if item not in delivered_formats)
        )
        if missing_formats:
            return (
                "document render did not deliver every requested format: "
                + ", ".join(missing_formats)
            )
    if (
        node.agent_type == "experiment_agent"
        and request_text is not None
        and requires_document_assets(request_text)
    ):
        collected = successful.get("result.collect")
        raw_artifacts = (
            collected.content.get("artifacts", [])
            if collected is not None and isinstance(collected.content, dict)
            else []
        )
        figures = [
            item
            for item in (raw_artifacts if isinstance(raw_artifacts, list) else [])
            if isinstance(item, dict)
            and (
                item.get("relation") == "figure"
                or str(item.get("name", ""))
                .casefold()
                .endswith((".png", ".jpg", ".jpeg", ".svg", ".webp"))
            )
            and item.get("artifact_id")
            and item.get("relative_path")
        ]
        if not figures:
            return (
                "the request requires experiment figures, but result.collect returned no "
                "verified independent image artifact"
            )
    layout_result = successful.get("document.layout.resolve")
    if layout_result is not None and isinstance(layout_result.content, dict):
        raw_plan = layout_result.content.get("render_plan")
        if isinstance(raw_plan, dict):
            raw_formats = raw_plan.get("formats")
            if isinstance(raw_formats, list) and any(
                isinstance(item, dict) and item.get("confirmation_required") is True
                for item in raw_formats
            ):
                return "render capability requires user confirmation before execution"
    qa_result = successful.get("document.validate_delivery") or successful.get("document.qa")
    if (
        qa_result is not None
        and isinstance(qa_result.content, dict)
        and qa_result.content.get("passed") is not True
    ):
        return f"document QA failed: {qa_result.content.get('issues', [])}"
    binding_result = successful.get("document.bind_assets")
    if (
        binding_result is not None
        and isinstance(binding_result.content, dict)
        and binding_result.content.get("ready") is not True
    ):
        return (
            "document assets are not ready: "
            f"status={binding_result.content.get('asset_barrier')}, "
            f"pending={binding_result.content.get('pending', [])}, "
            f"missing={binding_result.content.get('missing', [])}, "
            f"invalid={binding_result.content.get('invalid', [])}"
        )
    artifact_producing = mandatory & {
        "artifact.lookup",
        "process.execute",
        "result.collect",
        "document.render",
    }
    if artifact_producing and not any(item.artifact_refs for item in successful.values()):
        return "node completed without a verified artifact reference"
    return None


def _node_boundary(node: CandidateNode) -> str:
    if node.agent_type == "experiment_agent":
        return (
            "Produce only the experiment source, machine-readable data, and requested "
            "figure(s). Do not generate MD, DOCX, PDF, HTML, or the final report in this "
            "node; the writer and renderer nodes own those outputs. Prefer the standard "
            "library for CSV/SVG when it satisfies the request. When figures are requested, "
            "write one or more independent PNG, JPEG, SVG, or WebP files into the run "
            "workspace; a figure embedded only inside a PDF does not satisfy the contract. "
            "The executed experiment must exit with code 0 before result.collect."
        )
    if node.agent_type == "render_agent":
        return (
            "Render only the requested final document formats from the prior writer result. "
            "Use document.render and return its verified artifact references."
        )
    return "Stay within this agent's objective and leave downstream outputs to their nodes."


def _compact_prior_results(
    prior: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    """Build the node-to-node Context Pack without replaying tool-call transcripts."""
    compact: dict[str, dict[str, object]] = {}
    for node_id, payload in prior.items():
        try:
            result = AgentLoopResult.model_validate(payload)
        except ValueError:
            compact[node_id] = {"content": str(payload)[:4_000], "artifact_refs": []}
            continue
        refs: list[str] = []
        for message in result.messages:
            if message.role != "tool":
                continue
            try:
                tool_result = ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
            if tool_result.status is ToolResultStatus.SUCCESS:
                refs.extend(tool_result.artifact_refs)
        compact[node_id] = {
            "content": result.content[:12_000],
            "artifact_refs": list(dict.fromkeys(refs)),
            "finish_reason": result.finish_reason,
        }
    return compact


def _with_upstream_artifact_evidence(
    terminal: AgentLoopResult,
    node_results: dict[str, dict[str, object]],
    terminal_nodes: set[str],
) -> AgentLoopResult:
    """Carry verified upstream artifact refs to the delivery boundary.

    The messages are not replayed into another model call.  They are retained only in
    the structured AgentLoopResult so the API can deterministically attach artifacts
    produced or located by non-terminal graph nodes to the final assistant message.
    """
    evidence: list[ChatMessage] = []
    delivered_names: list[str] = []
    figure_names: list[str] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for node_id, payload in node_results.items():
        if node_id in terminal_nodes:
            continue
        try:
            result = AgentLoopResult.model_validate(payload)
        except ValueError:
            continue
        for message in result.messages:
            if message.role != "tool" or not message.tool_name:
                continue
            try:
                tool_result = ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
            if tool_result.status is not ToolResultStatus.SUCCESS or not tool_result.artifact_refs:
                continue
            if message.tool_name == "document.render" and isinstance(tool_result.content, dict):
                raw_name = tool_result.content.get("name")
                if isinstance(raw_name, str) and raw_name:
                    delivered_names.append(raw_name)
            if message.tool_name == "result.collect" and isinstance(tool_result.content, dict):
                raw_artifacts = tool_result.content.get("artifacts", [])
                if isinstance(raw_artifacts, list):
                    figure_names.extend(
                        str(item["name"])
                        for item in raw_artifacts
                        if isinstance(item, dict)
                        and item.get("relation") == "figure"
                        and isinstance(item.get("name"), str)
                    )
            key = (message.tool_name, tuple(tool_result.artifact_refs))
            if key in seen:
                continue
            seen.add(key)
            evidence.append(message)
    if not evidence:
        return terminal
    update: dict[str, object] = {"messages": [*evidence, *terminal.messages]}
    if delivered_names:
        delivered = "\n".join(f"- {name}" for name in dict.fromkeys(delivered_names))
        figure_note = ", 并完成实验图嵌入校验" if figure_names else ""
        update["content"] = (
            f"已完成文档生成和交付验收{figure_note}。\n\n"
            f"已生成文件:\n{delivered}\n\n"
            "可在下方文件卡片中预览或下载。"
        )
    return terminal.model_copy(update=update)


def _prior_tool_result(prior: dict[str, dict[str, object]], tool_name: str) -> ToolResult | None:
    for payload in prior.values():
        try:
            result = AgentLoopResult.model_validate(payload)
        except ValueError:
            continue
        for message in reversed(result.messages):
            if message.role != "tool" or message.tool_name != tool_name:
                continue
            try:
                return ToolResult.model_validate_json(message.content)
            except ValueError:
                continue
    return None


def _collected_figure_catalog(
    prior: dict[str, dict[str, object]],
    hydrate: Callable[[ToolResult], ToolResult],
) -> list[dict[str, str]]:
    """Return only verified run-scoped figures that the writer may reference."""
    collected = _prior_tool_result(prior, "result.collect")
    if collected is None:
        return []
    collected = hydrate(collected)
    if collected.status is not ToolResultStatus.SUCCESS or not isinstance(collected.content, dict):
        return []
    raw_artifacts = collected.content.get("artifacts", [])
    if not isinstance(raw_artifacts, list):
        return []
    catalog: list[dict[str, str]] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or item.get("relation") != "figure":
            continue
        artifact_id = item.get("artifact_id")
        relative_path = item.get("relative_path")
        name = item.get("name")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        if not isinstance(relative_path, str) or not relative_path:
            continue
        if not isinstance(name, str) or not name:
            continue
        catalog.append(
            {
                "artifact_id": artifact_id,
                "relative_path": relative_path,
                "name": name,
            }
        )
    return catalog


_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")


def _ground_markdown_figures(
    content: str,
    catalog: list[dict[str, str]],
    *,
    image_required: bool,
) -> str:
    """Remove invented image paths and deterministically attach verified figures."""
    allowed = {item["relative_path"].replace("\\", "/"): item for item in catalog}
    allowed_names = {item["name"]: item for item in catalog}
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        raw_path = match.group(2).strip().strip("<>").replace("\\", "/")
        candidate = allowed.get(raw_path) or allowed_names.get(raw_path.rsplit("/", 1)[-1])
        if candidate is None:
            return ""
        used.add(candidate["artifact_id"])
        alt = match.group(1).strip() or candidate["name"]
        return f"![{alt}]({candidate['relative_path']})"

    grounded = _MARKDOWN_IMAGE.sub(replace, content).rstrip()
    if image_required and not catalog:
        raise RuntimeError(
            "document composition requires experiment figures, but no verified figure "
            "artifact reached the writer"
        )
    if image_required and not used:
        figures = "\n\n".join(f"![{item['name']}]({item['relative_path']})" for item in catalog)
        grounded = f"{grounded}\n\n## 实验结果图\n\n{figures}".strip()
    return grounded


def _formats_from_layout(
    prior: dict[str, dict[str, object]],
    hydrate: Callable[[ToolResult], ToolResult],
) -> list[str]:
    layout = _prior_tool_result(prior, "document.layout.resolve")
    if layout is None:
        return []
    layout = hydrate(layout)
    if not isinstance(layout.content, dict):
        return []
    render_plan = layout.content.get("render_plan")
    raw_formats = render_plan.get("formats", []) if isinstance(render_plan, dict) else []
    if not isinstance(raw_formats, list):
        return []
    return list(
        dict.fromkeys(
            str(item["format"])
            for item in raw_formats
            if isinstance(item, dict) and item.get("format")
        )
    )


def _draft_title(content: str, request: str) -> str:
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", content)
    if heading:
        return heading.group(1).strip()[:120]
    compact = re.sub(r"\s+", " ", request).strip()
    return compact[:80] or "PaperAgent 文档"


def _draft_language(text: str) -> str:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    english = len(re.findall(r"\b[A-Za-z]{2,}\b", text))
    if chinese and english > max(20, chinese // 4):
        return "mixed"
    return "zh" if chinese else "en"


def _asset_target_formats(formats: list[str]) -> list[str]:
    mapped = {
        "pdf": "xelatex",
        "docx": "docx",
        "md": "markdown",
        "md_bundle": "markdown",
    }
    return list(dict.fromkeys(mapped[item] for item in formats if item in mapped))


def _compile_plan_graph(
    plan: CandidatePlan,
    loop: AgentLoop,
    base_request: AgentLoopRequest,
    progress_sink: ProgressSink | None = None,
) -> CompiledStateGraph[InteractiveInnerState, None, InteractiveInnerState, InteractiveInnerState]:
    definition = TaskGraphCompiler().compile(plan)
    candidates = {node.node_id: node for node in plan.nodes}
    builder = StateGraph(InteractiveInnerState)

    for node in definition.nodes:
        candidate = candidates[node.node_id]

        async def run_node(
            state: InteractiveInnerState,
            *,
            current: CandidateNode = candidate,
        ) -> InteractiveInnerState:
            prior = state.get("node_results", {})
            context = json.dumps(_compact_prior_results(prior), ensure_ascii=False)[:24_000]
            messages = [
                *base_request.messages,
                ChatMessage(
                    role="developer",
                    content=json.dumps(
                        {
                            "node_id": current.node_id,
                            "objective": current.objective,
                            "success_criteria": current.success_criteria,
                            "prior_node_results": context,
                            "instruction": (
                                "Complete only this node. Use registered tools for real-world "
                                "actions; never claim a file or experiment exists without a "
                                "successful tool result."
                            ),
                            "agent_boundary": _node_boundary(current),
                        },
                        ensure_ascii=False,
                    ),
                ),
            ]
            attempt_messages = messages
            last_issue = "node contract was not satisfied"

            async def execute_deterministic_tool(
                tool_name: str,
                arguments: dict[str, JsonValue],
                *,
                sequence: int = 0,
            ) -> ToolResult:
                result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-{tool_name}-{sequence}",
                        trace_id=base_request.trace_id,
                        sequence=sequence,
                        tool_name=tool_name,
                        arguments=arguments,
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:{tool_name}:{sequence}"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                return loop.executor.result_store.hydrate(result)

            if current.node_id in {"document_classify", "document_structure"}:
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "summary": _document_progress_summary(
                                _document_progress_phase(current.node_id)
                            )
                            or current.objective,
                        },
                    )
                if current.node_id == "document_classify":
                    tool_name = "document.classify"
                    deterministic_args: dict[str, JsonValue] = {
                        "request": base_request.messages[-1].content
                    }
                else:
                    classified = _prior_tool_result(prior, "document.classify")
                    if classified is None:
                        raise RuntimeError("document structure requires classification evidence")
                    classified = loop.executor.result_store.hydrate(classified)
                    if not isinstance(classified.content, dict) or not classified.content.get(
                        "archetype"
                    ):
                        raise RuntimeError("document classification returned no archetype")
                    tool_name = "document.structure.plan"
                    deterministic_args = {"archetype": str(classified.content["archetype"])}
                tool_result = await execute_deterministic_tool(tool_name, deterministic_args)
                deterministic = AgentLoopResult(
                    content=json.dumps(tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=tool_result.model_dump_json(),
                            tool_call_id=tool_result.call_id,
                            tool_name=tool_name,
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=[f"deterministic:{tool_name}"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic {current.node_id} failed: {issue}")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "tool_call_count": 1,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}

            if current.node_id == "document_presentation_resolve":
                request_text = base_request.messages[-1].content
                requested = extract_explicit_presentation(request_text)
                has_cover_values = bool(
                    requested.cover
                    and any(item.value for item in requested.cover.fields)
                )
                has_page_chrome = bool(
                    requested.page_chrome
                    and any(
                        (
                            requested.page_chrome.header_left,
                            requested.page_chrome.header_center,
                            requested.page_chrome.header_right,
                            requested.page_chrome.footer_left,
                            requested.page_chrome.footer_center,
                            requested.page_chrome.footer_right,
                            requested.page_chrome.page_number,
                        )
                    )
                )
                if not has_cover_values and not has_page_chrome:
                    raise RuntimeError(
                        "presentation requirements need concrete personal fields or page-chrome "
                        "values before composition"
                    )
                tool_result = await execute_deterministic_tool(
                    "document.presentation.resolve",
                    {"latest": requested.model_dump(mode="json")},
                )
                deterministic = AgentLoopResult(
                    content=json.dumps(tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=tool_result.model_dump_json(),
                            tool_call_id=tool_result.call_id,
                            tool_name="document.presentation.resolve",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.presentation.resolve"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic presentation resolve failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}

            if current.node_id == "document_compose":
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": "composition",
                            "attempt": 1,
                            "summary": _document_progress_summary("composition"),
                        },
                    )
                request_text = base_request.messages[-1].content
                image_required = requires_document_assets(request_text)
                figure_catalog = _collected_figure_catalog(
                    prior, loop.executor.result_store.hydrate
                )
                if image_required and not figure_catalog:
                    raise RuntimeError(
                        "document composition requires a verified experiment figure catalog"
                    )
                draft = await loop.run(
                    base_request.model_copy(
                        update={
                            "agent_type": current.agent_type,
                            "messages": [
                                *messages,
                                ChatMessage(
                                    role="developer",
                                    content=(
                                        "Write the complete report as Markdown now. Start with "
                                        "one H1 title. The verified figure catalog below is the "
                                        "only source of valid image paths. Use relative_path "
                                        "exactly; never use artifact IDs as filenames and never "
                                        "invent files. Include meaningful figure captions. Return "
                                        "the document content only.\nVERIFIED_FIGURE_CATALOG="
                                        + json.dumps(figure_catalog, ensure_ascii=False)
                                    ),
                                ),
                            ],
                            "tool_names": [],
                            "required_successful_tools": [],
                            "max_rounds": 2,
                            "max_elapsed_ms": current.timeout_ms,
                        }
                    )
                )
                if not draft.content.strip():
                    raise RuntimeError("document writer returned empty content")
                grounded_content = _ground_markdown_figures(
                    draft.content,
                    figure_catalog,
                    image_required=image_required,
                )
                compose_arguments: dict[str, JsonValue] = {
                    "title": _draft_title(grounded_content, request_text),
                    "content": grounded_content,
                    "language": _draft_language(grounded_content),
                    "image_required": image_required,
                }
                resolved_presentation = _prior_tool_result(
                    prior, "document.presentation.resolve"
                )
                if resolved_presentation is not None:
                    resolved_presentation = loop.executor.result_store.hydrate(
                        resolved_presentation
                    )
                    if not isinstance(resolved_presentation.content, dict) or not isinstance(
                        resolved_presentation.content.get("presentation"), dict
                    ):
                        raise RuntimeError(
                            "presentation resolver returned no canonical presentation"
                        )
                    compose_arguments["document_id"] = str(
                        resolved_presentation.content["document_id"]
                    )
                    compose_arguments["presentation"] = cast(
                        dict[str, JsonValue],
                        resolved_presentation.content["presentation"],
                    )
                compose_result = await execute_deterministic_tool(
                    "document.compose", compose_arguments
                )
                deterministic = AgentLoopResult(
                    content=grounded_content,
                    messages=[
                        *draft.messages,
                        ChatMessage(
                            role="tool",
                            content=compose_result.model_dump_json(),
                            tool_call_id=compose_result.call_id,
                            tool_name="document.compose",
                        ),
                    ],
                    rounds=draft.rounds,
                    tool_call_count=draft.tool_call_count + 1,
                    usage=draft.usage,
                    routes=[
                        *draft.routes,
                        "deterministic:document.figure_grounding",
                        "deterministic:document.compose",
                    ],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic document composition failed: {issue}")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": "composition",
                            "attempt": 1,
                            "tool_call_count": deterministic.tool_call_count,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}

            if current.node_id == "document_assets":
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "summary": _document_progress_summary(
                                _document_progress_phase(current.node_id)
                            )
                            or current.objective,
                        },
                    )
                composed = _prior_tool_result(prior, "document.compose")
                if composed is None:
                    raise RuntimeError("document assets require canonical composition")
                composed = loop.executor.result_store.hydrate(composed)
                if not isinstance(composed.content, dict):
                    raise RuntimeError("document composition returned no DocumentIR")
                composed_canonical = composed.content
                metadata = composed_canonical.get("metadata")
                composed_source_run_id = (
                    str(metadata.get("source_run_id"))
                    if isinstance(metadata, dict) and metadata.get("source_run_id")
                    else None
                )
                resolve_arguments: dict[str, JsonValue] = {
                    "document_ir": composed_canonical,
                    "image_required": True,
                }
                if composed_source_run_id:
                    resolve_arguments["source_run_id"] = composed_source_run_id
                resolved = await execute_deterministic_tool(
                    "asset.resolve", resolve_arguments, sequence=0
                )
                asset_results = [resolved]
                if resolved.status is not ToolResultStatus.SUCCESS:
                    raise RuntimeError(
                        "deterministic asset resolution failed: "
                        f"{resolved.error.message if resolved.error else resolved.status.value}"
                    )
                if "asset.derive" in current.required_tools:
                    if not isinstance(resolved.content, dict) or not isinstance(
                        resolved.content.get("document_ir"), dict
                    ):
                        raise RuntimeError("asset resolution returned no assembled DocumentIR")
                    formats = _asset_target_formats(
                        _requested_document_formats(base_request.messages[-1].content)
                    )
                    derived = await execute_deterministic_tool(
                        "asset.derive",
                        {
                            "document_ir": cast(
                                dict[str, JsonValue], resolved.content["document_ir"]
                            ),
                            "formats": cast(list[JsonValue], formats),
                        },
                        sequence=1,
                    )
                    asset_results.append(derived)
                deterministic = AgentLoopResult(
                    content=json.dumps(
                        [item.content for item in asset_results], ensure_ascii=False
                    ),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=item.model_dump_json(),
                            tool_call_id=item.call_id,
                            tool_name=tool_name,
                        )
                        for item, tool_name in zip(
                            asset_results,
                            ["asset.resolve", "asset.derive"][: len(asset_results)],
                            strict=True,
                        )
                    ],
                    rounds=1,
                    tool_call_count=len(asset_results),
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.assets"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic document assets failed: {issue}")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "tool_call_count": len(asset_results),
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}

            if current.node_id == "document_layout":
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "summary": _document_progress_summary(
                                _document_progress_phase(current.node_id)
                            )
                            or current.objective,
                        },
                    )
                classified = _prior_tool_result(prior, "document.classify")
                if classified is None:
                    raise RuntimeError("document layout requires classification evidence")
                classified = loop.executor.result_store.hydrate(classified)
                if not isinstance(classified.content, dict) or not classified.content.get(
                    "archetype"
                ):
                    raise RuntimeError("document layout received no archetype")
                formats = _requested_document_formats(base_request.messages[-1].content)
                if not formats:
                    raise RuntimeError("document layout received no requested output format")
                tool_result = await execute_deterministic_tool(
                    "document.layout.resolve",
                    {
                        "archetype": str(classified.content["archetype"]),
                        "formats": cast(list[JsonValue], formats),
                    },
                )
                deterministic = AgentLoopResult(
                    content=json.dumps(tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=tool_result.model_dump_json(),
                            tool_call_id=tool_result.call_id,
                            tool_name="document.layout.resolve",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.layout.resolve"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic document layout failed: {issue}")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": 1,
                            "tool_call_count": 1,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}

            if (
                current.agent_type == "artifact_agent"
                and "artifact.lookup" in current.required_tools
            ):
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "summary": current.objective,
                        },
                    )
                relation = _artifact_relation(base_request.messages[-1].content)
                lookup_arguments: dict[str, Any] = {"relation": relation} if relation else {}
                tool_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-lookup",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="artifact.lookup",
                        arguments=lookup_arguments,
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:artifact.lookup"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                tool_result = loop.executor.result_store.hydrate(tool_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=tool_result.model_dump_json(),
                            tool_call_id=tool_result.call_id,
                            tool_name="artifact.lookup",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:artifact.lookup"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    if progress_sink is not None:
                        progress_sink(
                            "node.validation_failed",
                            {
                                "node_id": current.node_id,
                                "agent_type": current.agent_type,
                                "attempt": 1,
                                "summary": issue,
                                "strategy": "deterministic_artifact_lookup",
                            },
                        )
                    raise RuntimeError(f"deterministic artifact lookup failed: {issue}")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "tool_call_count": 1,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id == "document_resolve_revision":
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "summary": current.objective,
                        },
                    )
                resolution_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-resolve",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="document.resolve_revision",
                        arguments={"reference": base_request.messages[-1].content},
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:document.resolve_revision"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                resolution_result = loop.executor.result_store.hydrate(resolution_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(resolution_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=resolution_result.model_dump_json(),
                            tool_call_id=resolution_result.call_id,
                            tool_name="document.resolve_revision",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.resolve_revision"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"canonical revision resolution failed: {issue}")
                content = resolution_result.content
                if isinstance(content, dict) and bool(content.get("requires_confirmation")):
                    candidates = content.get("candidates", [])
                    raise RuntimeError(
                        "canonical revision requires user confirmation: "
                        + json.dumps(candidates, ensure_ascii=False)[:2_000]
                    )
                if not isinstance(content, dict) or not isinstance(content.get("document"), dict):
                    raise RuntimeError("canonical revision resolver returned no document")
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "tool_call_count": 1,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id == "document_presentation_patch":
                upstream = prior.get("document_resolve_revision")
                if upstream is None:
                    raise RuntimeError("presentation patch requires a resolved canonical revision")
                resolved_result = AgentLoopResult.model_validate(upstream)
                patch_canonical: dict[str, JsonValue] | None = None
                for message in resolved_result.messages:
                    if message.role != "tool" or message.tool_name != "document.resolve_revision":
                        continue
                    hydrated = loop.executor.result_store.hydrate(
                        ToolResult.model_validate_json(message.content)
                    )
                    if isinstance(hydrated.content, dict) and isinstance(
                        hydrated.content.get("document"), dict
                    ):
                        patch_canonical = cast(
                            dict[str, JsonValue], hydrated.content["document"]
                        )
                if patch_canonical is None:
                    raise RuntimeError("presentation patch received no canonical document")
                intent = PresentationChangeIntentClassifier().classify(
                    base_request.messages[-1].content
                )
                if intent.clarification:
                    raise RuntimeError(intent.clarification)
                if intent.changes_content:
                    raise RuntimeError(
                        "mixed content and presentation change requires replan from "
                        "document_compose"
                    )
                patch_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-patch",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="document.presentation.patch",
                        arguments={
                            "document_id": str(patch_canonical["document_id"]),
                            "revision": int(str(patch_canonical["revision"])),
                            "operations": cast(
                                list[JsonValue],
                                [item.model_dump(mode="json") for item in intent.operations],
                            ),
                            "requested_formats": cast(
                                list[JsonValue], intent.requested_formats
                            ),
                        },
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:"
                            f"{patch_canonical['document_id']}:{patch_canonical['revision']}"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                patch_result = loop.executor.result_store.hydrate(patch_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(patch_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=patch_result.model_dump_json(),
                            tool_call_id=patch_result.call_id,
                            tool_name="document.presentation.patch",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.presentation.patch"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic presentation patch failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id == "document_presentation_layout":
                upstream = prior.get("document_presentation_patch")
                if upstream is None:
                    raise RuntimeError("presentation layout requires a patched revision")
                patched_result = AgentLoopResult.model_validate(upstream)
                presentation_formats: list[str] = []
                archetype = "research-report"
                for message in patched_result.messages:
                    if message.role != "tool" or message.tool_name != "document.presentation.patch":
                        continue
                    hydrated = loop.executor.result_store.hydrate(
                        ToolResult.model_validate_json(message.content)
                    )
                    if not isinstance(hydrated.content, dict):
                        continue
                    raw_formats = hydrated.content.get("rerender_formats")
                    if isinstance(raw_formats, list):
                        presentation_formats = [str(item) for item in raw_formats]
                    raw_document = hydrated.content.get("document_ir")
                    raw_metadata = (
                        raw_document.get("metadata") if isinstance(raw_document, dict) else None
                    )
                    if isinstance(raw_metadata, dict):
                        archetype = str(raw_metadata.get("archetype") or archetype)
                if not presentation_formats:
                    raise RuntimeError("presentation layout received no target formats")
                layout_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-layout",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="document.layout.resolve",
                        arguments={
                            "archetype": archetype,
                            "formats": cast(list[JsonValue], presentation_formats),
                        },
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:"
                            + ",".join(presentation_formats)
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                layout_result = loop.executor.result_store.hydrate(layout_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(layout_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=layout_result.model_dump_json(),
                            tool_call_id=layout_result.call_id,
                            tool_name="document.layout.resolve",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.layout.resolve"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic presentation layout failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id == "document_asset_barrier":
                upstream = prior.get("document_resolve_revision")
                if upstream is None:
                    raise RuntimeError("asset binding requires a resolved canonical revision")
                upstream_result = AgentLoopResult.model_validate(upstream)
                revision_content: dict[str, JsonValue] | None = None
                for message in upstream_result.messages:
                    if message.role != "tool" or message.tool_name != "document.resolve_revision":
                        continue
                    revision_tool_result = ToolResult.model_validate_json(message.content)
                    if isinstance(revision_tool_result.content, dict):
                        revision_content = revision_tool_result.content
                canonical = (
                    revision_content.get("document") if isinstance(revision_content, dict) else None
                )
                if not isinstance(canonical, dict):
                    raise RuntimeError("asset binding received no canonical document")
                bind_arguments: dict[str, object] = {
                    "document_id": canonical.get("document_id"),
                    "revision": canonical.get("revision"),
                }
                canonical_metadata = canonical.get("metadata")
                source_run_id = (
                    canonical_metadata.get("source_run_id")
                    if isinstance(canonical_metadata, dict)
                    else None
                )
                if isinstance(source_run_id, str) and source_run_id:
                    bind_arguments["source_run_id"] = source_run_id
                tool_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-bind",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="document.bind_assets",
                        arguments=cast(dict[str, JsonValue], bind_arguments),
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:document.bind_assets"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                tool_result = loop.executor.result_store.hydrate(tool_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=tool_result.model_dump_json(),
                            tool_call_id=tool_result.call_id,
                            tool_name="document.bind_assets",
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.bind_assets"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"document asset binding failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id == "document_render":
                render_canonical: dict[str, JsonValue] | None = None
                for upstream_payload in prior.values():
                    upstream_result = AgentLoopResult.model_validate(upstream_payload)
                    for message in upstream_result.messages:
                        if message.role != "tool":
                            continue
                        # Checkpoints created before cancelled tool messages were emitted
                        # as full ToolResult records can contain a legacy control payload.
                        # It carries no canonical document data, so ignore it while keeping
                        # valid upstream tool evidence available for deterministic render.
                        try:
                            upstream_tool = ToolResult.model_validate_json(message.content)
                        except ValueError:
                            continue
                        if not isinstance(upstream_tool.content, dict):
                            continue
                        render_candidate = upstream_tool.content.get("document")
                        if not isinstance(render_candidate, dict):
                            render_candidate = upstream_tool.content.get("document_ir")
                        if not isinstance(render_candidate, dict) and {
                            "document_id",
                            "revision",
                        } <= set(upstream_tool.content):
                            render_candidate = upstream_tool.content
                        if isinstance(render_candidate, dict) and render_candidate.get(
                            "document_id"
                        ):
                            render_canonical = render_candidate
                if render_canonical is None:
                    raise RuntimeError("document render received no canonical revision identity")
                formats = _formats_from_layout(prior, loop.executor.result_store.hydrate)
                if not formats:
                    formats = _requested_document_formats(base_request.messages[-1].content)
                if not formats:
                    raise RuntimeError("document render received no requested output format")
                render_document_id = str(render_canonical["document_id"])
                render_revision = int(str(render_canonical["revision"]))
                render_results: list[ToolResult] = []
                for sequence, format_name in enumerate(formats):
                    tool_result = await loop.executor.execute(
                        ToolCall(
                            call_id=f"{current.node_id}-{format_name}",
                            trace_id=base_request.trace_id,
                            sequence=sequence,
                            tool_name="document.render",
                            arguments={
                                "document_id": render_document_id,
                                "revision": render_revision,
                                "format": format_name,
                            },
                            requested_by=current.agent_type,
                            idempotency_key=(
                                f"{base_request.trace_id}:{current.node_id}:"
                                f"document.render:{render_document_id}:"
                                f"{render_revision}:{format_name}"
                            ),
                        ),
                        ToolExecutionContext(
                            project_id=base_request.project_id,
                            workspace=loop.workspace,
                            agent_type=current.agent_type,
                            provider_capabilities={
                                capability.value
                                for provider in loop.router.providers
                                for capability in provider.config.capabilities
                            },
                            approved=base_request.approved,
                        ),
                    )
                    tool_result = loop.executor.result_store.hydrate(tool_result)
                    render_results.append(tool_result)
                    if isinstance(tool_result.content, dict):
                        render_document_id = str(
                            tool_result.content.get("document_id") or render_document_id
                        )
                        render_revision = int(
                            str(tool_result.content.get("document_revision") or render_revision)
                        )
                deterministic = AgentLoopResult(
                    content=json.dumps(
                        [item.content for item in render_results], ensure_ascii=False
                    ),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=item.model_dump_json(),
                            tool_call_id=item.call_id,
                            tool_name="document.render",
                        )
                        for item in render_results
                    ],
                    rounds=1,
                    tool_call_count=len(render_results),
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.render"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(
                    current,
                    deterministic,
                    expected_formats=formats,
                )
                if issue is not None:
                    raise RuntimeError(f"deterministic document render failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            if current.node_id in {"document_qa", "document_validate_delivery"}:
                render_payload = prior.get("document_render")
                if render_payload is None:
                    raise RuntimeError("document QA requires rendered artifact evidence")
                render_result = AgentLoopResult.model_validate(render_payload)
                artifact_ids: list[str] = []
                qa_document_id: str | None = None
                qa_revision: int | None = None
                for message in render_result.messages:
                    if message.role != "tool" or message.tool_name != "document.render":
                        continue
                    render_tool_result = ToolResult.model_validate_json(message.content)
                    if render_tool_result.status is ToolResultStatus.SUCCESS:
                        artifact_ids.extend(render_tool_result.artifact_refs)
                        if isinstance(render_tool_result.content, dict):
                            raw_document_id = render_tool_result.content.get("document_id")
                            raw_revision = render_tool_result.content.get("document_revision")
                            if raw_document_id is not None:
                                qa_document_id = str(raw_document_id)
                            if raw_revision is not None:
                                qa_revision = int(str(raw_revision))
                artifact_ids = list(dict.fromkeys(artifact_ids))
                if not artifact_ids:
                    raise RuntimeError("document QA received no delivered artifacts")
                qa_tool_name = current.required_tools[0]
                qa_arguments: dict[str, JsonValue] = {
                    "artifact_ids": cast(list[JsonValue], artifact_ids),
                }
                if qa_tool_name == "document.validate_delivery":
                    if qa_document_id is None or qa_revision is None:
                        raise RuntimeError("delivery validation requires document/revision lineage")
                    qa_arguments.update({"document_id": qa_document_id, "revision": qa_revision})
                else:
                    qa_arguments["require_images"] = _needs(
                        base_request.messages[-1].content,
                        (r"图片|图像|实验图", r"figure|image|plot|chart"),
                    )
                qa_tool_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-qa",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name=qa_tool_name,
                        arguments=qa_arguments,
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:{qa_tool_name}:"
                            + ":".join(artifact_ids)
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                qa_tool_result = loop.executor.result_store.hydrate(qa_tool_result)
                deterministic = AgentLoopResult(
                    content=json.dumps(qa_tool_result.content, ensure_ascii=False),
                    messages=[
                        ChatMessage(
                            role="tool",
                            content=qa_tool_result.model_dump_json(),
                            tool_call_id=qa_tool_result.call_id,
                            tool_name=qa_tool_name,
                        )
                    ],
                    rounds=1,
                    tool_call_count=1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=[f"deterministic:{qa_tool_name}"],
                    finish_reason="tool_completed",
                )
                issue = _node_contract_issue(current, deterministic)
                if issue is not None:
                    raise RuntimeError(f"deterministic document QA failed: {issue}")
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            typography_request = _needs(
                base_request.messages[-1].content,
                (r"字体|字号|宋体|黑体|排版", r"font|typography"),
            )
            if current.agent_type == "render_agent" and typography_request:
                formats = _requested_document_formats(base_request.messages[-1].content)
                if not formats:
                    formats = ["pdf"]
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "summary": current.objective,
                        },
                    )
                resolution_result = await loop.executor.execute(
                    ToolCall(
                        call_id=f"{current.node_id}-resolve-revision",
                        trace_id=base_request.trace_id,
                        sequence=0,
                        tool_name="document.resolve_revision",
                        arguments={"reference": base_request.messages[-1].content},
                        requested_by=current.agent_type,
                        idempotency_key=(
                            f"{base_request.trace_id}:{current.node_id}:document.resolve_revision"
                        ),
                    ),
                    ToolExecutionContext(
                        project_id=base_request.project_id,
                        workspace=loop.workspace,
                        agent_type=current.agent_type,
                        provider_capabilities={
                            capability.value
                            for provider in loop.router.providers
                            for capability in provider.config.capabilities
                        },
                        approved=base_request.approved,
                    ),
                )
                resolution_result = loop.executor.result_store.hydrate(resolution_result)
                if resolution_result.status is not ToolResultStatus.SUCCESS or not isinstance(
                    resolution_result.content, dict
                ):
                    raise RuntimeError("canonical document revision could not be resolved")
                resolution = resolution_result.content
                if bool(resolution.get("requires_confirmation")):
                    raw_candidates = resolution.get("candidates", [])
                    candidates = raw_candidates if isinstance(raw_candidates, list) else []
                    labels = [
                        f"{item.get('title')} (revision {item.get('revision')})"
                        for item in candidates
                        if isinstance(item, dict)
                    ]
                    deterministic = AgentLoopResult(
                        content="检测到多个可修改文档, 请先选择目标:\n- " + "\n- ".join(labels),
                        messages=[
                            ChatMessage(
                                role="tool",
                                content=resolution_result.model_dump_json(),
                                tool_call_id=resolution_result.call_id,
                                tool_name="document.resolve_revision",
                            )
                        ],
                        rounds=1,
                        tool_call_count=1,
                        usage=Usage(input_tokens=0, output_tokens=0),
                        routes=["deterministic:document.resolve_revision"],
                        finish_reason="clarification_required",
                    )
                    return {
                        "node_results": {current.node_id: deterministic.model_dump(mode="json")}
                    }
                canonical = resolution.get("document")
                if not isinstance(canonical, dict):
                    raise RuntimeError("revision resolver returned no canonical document")
                document_id = canonical.get("document_id")
                revision = canonical.get("revision")
                results: list[ToolResult] = []
                for sequence, format_name in enumerate(formats):
                    results.append(
                        await loop.executor.execute(
                            ToolCall(
                                call_id=f"{current.node_id}-{format_name}",
                                trace_id=base_request.trace_id,
                                sequence=sequence + 1,
                                tool_name="document.render",
                                arguments={
                                    "document_id": document_id,
                                    "revision": revision,
                                    "format": format_name,
                                    "filename": f"paperagent-typography-revision.{format_name}",
                                },
                                requested_by=current.agent_type,
                                idempotency_key=(
                                    f"{base_request.trace_id}:{current.node_id}:"
                                    f"document.render:{format_name}"
                                ),
                            ),
                            ToolExecutionContext(
                                project_id=base_request.project_id,
                                workspace=loop.workspace,
                                agent_type=current.agent_type,
                                provider_capabilities={
                                    capability.value
                                    for provider in loop.router.providers
                                    for capability in provider.config.capabilities
                                },
                                approved=base_request.approved,
                            ),
                        )
                    )
                failed = [
                    item
                    for item in results
                    if item.status is not ToolResultStatus.SUCCESS or not item.artifact_refs
                ]
                if failed:
                    issue = "one or more requested typography formats failed to render"
                    if progress_sink is not None:
                        progress_sink(
                            "node.validation_failed",
                            {
                                "node_id": current.node_id,
                                "agent_type": current.agent_type,
                                "attempt": 1,
                                "summary": issue,
                                "strategy": "deterministic_typography_render",
                            },
                        )
                    raise RuntimeError(issue)
                messages = [
                    ChatMessage(
                        role="tool",
                        content=item.model_dump_json(),
                        tool_call_id=item.call_id,
                        tool_name="document.render",
                    )
                    for item in results
                ]
                names = [
                    str(item.content.get("name", format_name))
                    if isinstance(item.content, dict)
                    else format_name
                    for item, format_name in zip(results, formats, strict=True)
                ]
                deterministic = AgentLoopResult(
                    content=("已保留原文内容并完成新的排版版本:\n- " + "\n- ".join(names)),
                    messages=messages,
                    rounds=1,
                    tool_call_count=len(results) + 1,
                    usage=Usage(input_tokens=0, output_tokens=0),
                    routes=["deterministic:document.render"],
                    finish_reason="tool_completed",
                )
                if progress_sink is not None:
                    progress_sink(
                        "node.completed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "attempt": 1,
                            "tool_call_count": len(results) + 1,
                        },
                    )
                return {"node_results": {current.node_id: deterministic.model_dump(mode="json")}}
            document_repair_strategies: list[str] = []
            for attempt in range(1, current.max_attempts + 1):
                if progress_sink is not None:
                    progress_sink(
                        "node.started",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": _document_progress_phase(current.node_id),
                            "attempt": attempt,
                            "summary": _document_progress_summary(
                                _document_progress_phase(current.node_id)
                            )
                            or current.objective,
                        },
                    )
                try:
                    result = await loop.run(
                        base_request.model_copy(
                            update={
                                "agent_type": current.agent_type,
                                "messages": attempt_messages,
                                "tool_names": current.required_tools,
                                "required_successful_tools": sorted(_mandatory_node_tools(current)),
                                "max_elapsed_ms": current.timeout_ms,
                            }
                        )
                    )
                except AgentLoopLimitError as error:
                    last_issue = str(error)
                    if progress_sink is not None:
                        progress_sink(
                            "node.validation_failed",
                            {
                                "node_id": current.node_id,
                                "agent_type": current.agent_type,
                                "phase": _document_progress_phase(current.node_id),
                                "attempt": attempt,
                                "summary": last_issue,
                                "strategy": "reduce_context_and_call_tools_first",
                            },
                        )
                    if attempt >= current.max_attempts:
                        break
                    # A budget failure must not replay the same long transcript. Preserve the
                    # compiled safety prompt and current goal, but discard the failed model
                    # continuation and explicitly change to a tool-first strategy.
                    attempt_messages = [
                        *messages,
                        ChatMessage(
                            role="developer",
                            content=(
                                f"The previous attempt failed with {last_issue}. Use a "
                                "materially different tool-first strategy: minimize prose, "
                                "call the required registered tools immediately, inspect "
                                "compact result references, and finish within the node budget."
                            ),
                        ),
                    ]
                    continue
                issue = _node_contract_issue(
                    current,
                    result,
                    request_text=base_request.messages[-1].content,
                    hydrate=loop.executor.result_store.hydrate,
                )
                if issue is None:
                    if progress_sink is not None:
                        completed_phase = _document_progress_phase(current.node_id)
                        progress_sink(
                            "node.completed",
                            {
                                "node_id": current.node_id,
                                "agent_type": current.agent_type,
                                "phase": completed_phase,
                                "attempt": attempt,
                                "tool_call_count": result.tool_call_count,
                                "summary": _document_progress_summary(
                                    completed_phase, completed=True
                                ),
                            },
                        )
                    return {"node_results": {current.node_id: result.model_dump(mode="json")}}
                last_issue = issue
                phase = _document_progress_phase(current.node_id)
                repair = (
                    DocumentRepairPlanner().decide(
                        issue,
                        attempt=attempt,
                        prior_strategies=document_repair_strategies,
                    )
                    if phase is not None
                    else None
                )
                if repair is not None:
                    document_repair_strategies.append(repair.strategy)
                strategy = (
                    repair.strategy if repair is not None else "require_verified_tool_evidence"
                )
                if progress_sink is not None:
                    progress_sink(
                        "node.validation_failed",
                        {
                            "node_id": current.node_id,
                            "agent_type": current.agent_type,
                            "phase": phase,
                            "attempt": attempt,
                            "summary": issue,
                            "strategy": strategy,
                        },
                    )
                attempt_messages = [
                    *result.messages,
                    ChatMessage(
                        role="developer",
                        content=(
                            "The node output failed deterministic validation: "
                            f"{issue}. Change strategy to {strategy}: call the registered "
                            "tools, inspect "
                            "their structured results, and only finish after the required "
                            "verified artifact references exist. Do not return a script for "
                            "the user to run manually."
                            + (
                                f" Resume from capability {repair.resume_capability}; "
                                "do not restart composition."
                                if repair is not None
                                else ""
                            )
                        ),
                    ),
                ]
            raise RuntimeError(
                f"node {current.node_id!r} exhausted {current.max_attempts} materially "
                f"different attempts: {last_issue}"
            )

        builder.add_node(node.node_id, cast(Any, run_node))

    builder.add_edge(START, definition.entry_node)
    outgoing: dict[str, list[str]] = {}
    for edge in definition.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
    for node in definition.nodes:
        targets = outgoing.get(node.node_id, [])
        if targets:
            for target in targets:
                builder.add_edge(node.node_id, target)
        elif node.node_id in definition.terminal_nodes:
            builder.add_edge(node.node_id, END)
    return builder.compile()


def compile_dynamic_interactive_graph(
    loop: AgentLoop,
    loop_request: AgentLoopRequest,
    *,
    available_tools: list[str],
    result_sink: ResultSink | None = None,
    progress_sink: ProgressSink | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> CompiledStateGraph[InteractiveGraphState, None, InteractiveGraphState, InteractiveGraphState]:
    planner = InteractivePlanGenerator(loop, loop.registry)

    async def create_plan(_state: InteractiveGraphState) -> InteractiveGraphState:
        planned = await planner.generate(loop_request, available_tools=available_tools)
        return {
            "plan": cast(dict[str, object], planned["plan"].model_dump(mode="json")),
            "validation": cast(dict[str, object], planned["validation"].model_dump(mode="json")),
            "plan_source": planned["source"],
            "planning_error": planned["planning_error"],
        }

    async def execute_plan(state: InteractiveGraphState) -> InteractiveGraphState:
        plan = CandidatePlan.model_validate(state["plan"])
        graph = _compile_plan_graph(plan, loop, loop_request, progress_sink)
        result = await graph.ainvoke({"node_results": {}})
        terminal_results = [
            result.get("node_results", {}).get(node_id) for node_id in sorted(plan.terminal_nodes)
        ]
        payload = next((item for item in terminal_results if item is not None), None)
        if payload is None:
            raise RuntimeError("dynamic task graph produced no terminal AgentLoopResult")
        agent_result = AgentLoopResult.model_validate(payload)
        terminal_agent_types = {
            node.agent_type for node in plan.nodes if node.node_id in plan.terminal_nodes
        }
        if "render_agent" not in terminal_agent_types:
            agent_result = _with_upstream_artifact_evidence(
                agent_result,
                result.get("node_results", {}),
                set(plan.terminal_nodes),
            )
        if result_sink is not None:
            result_sink(agent_result)
        return {"agent_result": agent_result.model_dump(mode="json")}

    builder = StateGraph(InteractiveGraphState)
    builder.add_node("plan", cast(Any, create_plan))
    builder.add_node("execute", cast(Any, execute_plan))
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "execute")
    builder.add_edge("execute", END)
    return builder.compile(checkpointer=checkpointer)
