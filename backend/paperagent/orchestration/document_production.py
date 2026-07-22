from __future__ import annotations

import re
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from paperagent.engine.budgets import BudgetLimits
from paperagent.orchestration.plan_models import (
    ApprovalRequirement,
    CandidateEdge,
    CandidateNode,
    CandidatePlan,
)


def requires_document_assets(request: str) -> bool:
    """Return whether the requested document must carry visual assets."""

    return bool(
        re.search(
            r"图片|图像|图表|实验图|效果图|运行图|结果图|曲线图|现场图|"
            r"流程图|示意图|figure|image|diagram|plot|chart|visual",
            request,
            re.I,
        )
    )


class RenderFidelity(StrEnum):
    EXACT = "exact"
    EQUIVALENT = "equivalent"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


class FormatCapability(BaseModel):
    format: str
    fidelity: RenderFidelity
    renderer: str
    layout_profile: str
    reasons: list[str] = Field(default_factory=list)
    confirmation_required: bool = False


class RenderPlan(BaseModel):
    document_revision_id: str | None = None
    formats: list[FormatCapability]
    shared_compose: bool = True
    shared_asset_assembly: bool = True


class RenderPlanService:
    MATRIX: ClassVar[dict[str, tuple[RenderFidelity, str]]] = {
        "md": (RenderFidelity.EQUIVALENT, "markdown-ast"),
        "md_bundle": (RenderFidelity.EXACT, "portable-markdown-bundle"),
        "docx": (RenderFidelity.EXACT, "native-docx"),
        "pdf": (RenderFidelity.EXACT, "xelatex-or-word-parity"),
        "tex": (RenderFidelity.EQUIVALENT, "xelatex-source"),
        "typ": (RenderFidelity.EQUIVALENT, "typst-source"),
        "html": (RenderFidelity.DEGRADED, "semantic-html"),
    }

    def negotiate(
        self,
        formats: list[str],
        *,
        layout_profile: str,
        word_available: bool = True,
        tex_available: bool = True,
    ) -> RenderPlan:
        resolved: list[FormatCapability] = []
        for format_name in dict.fromkeys(item.casefold().lstrip(".") for item in formats):
            fidelity, renderer = self.MATRIX.get(format_name, (RenderFidelity.UNSUPPORTED, "none"))
            reasons: list[str] = []
            if format_name == "docx" and not word_available:
                reasons.append(
                    "native DOCX remains available; Word parity preview/export is unavailable"
                )
            if format_name == "pdf" and not tex_available and not word_available:
                fidelity = RenderFidelity.UNSUPPORTED
                renderer = "none"
                reasons.append("neither XeLaTeX nor Word/LibreOffice PDF engine is available")
            resolved.append(
                FormatCapability(
                    format=format_name,
                    fidelity=fidelity,
                    renderer=renderer,
                    layout_profile=layout_profile,
                    reasons=reasons,
                    confirmation_required=fidelity
                    in {RenderFidelity.DEGRADED, RenderFidelity.UNSUPPORTED},
                )
            )
        return RenderPlan(formats=resolved)


class DocumentRepairCategory(StrEnum):
    STRUCTURE_ERROR = "structure_error"
    MISSING_ASSET = "missing_asset"
    FONT_MISSING = "font_missing"
    COMPILE_ERROR = "compile_error"
    LAYOUT_OVERFLOW = "layout_overflow"
    PREVIEW_ERROR = "preview_error"


class DocumentRepairDecision(BaseModel):
    category: DocumentRepairCategory
    strategy: str
    resume_capability: str
    attempt: int
    max_attempts: int
    changes_document_content: bool = False
    requires_confirmation: bool = False


class DocumentRepairPlanner:
    STRATEGIES: ClassVar[dict[DocumentRepairCategory, tuple[str, ...]]] = {
        DocumentRepairCategory.STRUCTURE_ERROR: (
            "reparse_document_ir_with_schema_diagnostics",
            "recompose_only_invalid_semantic_nodes",
        ),
        DocumentRepairCategory.MISSING_ASSET: (
            "resolve_missing_artifact_by_stable_id",
            "rebuild_target_derivative_from_verified_source",
        ),
        DocumentRepairCategory.FONT_MISSING: (
            "select_metric_compatible_installed_font",
            "request_font_install_authorization",
        ),
        DocumentRepairCategory.COMPILE_ERROR: (
            "repair_from_classified_compiler_diagnostics",
            "switch_explicit_render_mode_after_capability_check",
        ),
        DocumentRepairCategory.LAYOUT_OVERFLOW: (
            "adjust_affected_page_break_table_width_or_figure_ratio",
            "apply_local_layout_override_to_overflow_anchor",
        ),
        DocumentRepairCategory.PREVIEW_ERROR: (
            "invalidate_preview_cache_only",
            "rebuild_format_specific_preview_derivative",
        ),
    }
    RESUME: ClassVar[dict[DocumentRepairCategory, str]] = {
        DocumentRepairCategory.STRUCTURE_ERROR: "document.compose",
        DocumentRepairCategory.MISSING_ASSET: "asset.resolve",
        DocumentRepairCategory.FONT_MISSING: "document.layout.resolve",
        DocumentRepairCategory.COMPILE_ERROR: "document.render",
        DocumentRepairCategory.LAYOUT_OVERFLOW: "document.layout.resolve",
        DocumentRepairCategory.PREVIEW_ERROR: "preview.render",
    }

    def classify(self, evidence: str) -> DocumentRepairCategory:
        patterns = (
            (DocumentRepairCategory.MISSING_ASSET, r"asset|image|figure.*missing|not found"),
            (DocumentRepairCategory.FONT_MISSING, r"font|字体.*(?:missing|缺失|不存在)"),
            (DocumentRepairCategory.LAYOUT_OVERFLOW, r"overfull|overflow|裁切|溢出"),
            (DocumentRepairCategory.PREVIEW_ERROR, r"preview|预览"),
            (DocumentRepairCategory.COMPILE_ERROR, r"latex|tex|compile|office|pdf.*failed"),
            (
                DocumentRepairCategory.STRUCTURE_ERROR,
                r"schema|structure|document.?ir|markdown.*leak",
            ),
        )
        return next(
            (category for category, pattern in patterns if re.search(pattern, evidence, re.I)),
            DocumentRepairCategory.STRUCTURE_ERROR,
        )

    def decide(
        self,
        evidence: str,
        *,
        attempt: int,
        prior_strategies: list[str] | None = None,
    ) -> DocumentRepairDecision:
        category = self.classify(evidence)
        strategies = self.STRATEGIES[category]
        prior = set(prior_strategies or [])
        strategy = next((item for item in strategies if item not in prior), strategies[-1])
        max_attempts = len(strategies)
        return DocumentRepairDecision(
            category=category,
            strategy=strategy,
            resume_capability=self.RESUME[category],
            attempt=attempt,
            max_attempts=max_attempts,
            requires_confirmation=(
                category is DocumentRepairCategory.FONT_MISSING and attempt >= max_attempts
            ),
        )


class DocumentProductionSubgraph:
    """Build a document DAG from capabilities; optional work is never a fixed paper chain."""

    def build_plan(
        self,
        request: str,
        *,
        requirement_id: UUID,
        available_tools: set[str],
        input_refs: list[str] | None = None,
        render_requested: bool = True,
    ) -> CandidatePlan:
        nodes: list[CandidateNode] = []
        edges: list[CandidateEdge] = []
        prior: str | None = None
        outputs = list(input_refs or [])

        def add(node: CandidateNode) -> None:
            nonlocal prior
            nodes.append(node)
            if prior:
                edges.append(CandidateEdge(source=prior, target=node.node_id))
            prior = node.node_id
            outputs.extend(node.output_keys)

        def require(tool: str) -> None:
            if tool not in available_tools:
                raise ValueError(f"document production capability is unavailable: {tool}")

        required = [
            "document.classify",
            "document.structure.plan",
            "document.compose",
        ]
        needs_presentation = bool(
            re.search(
                r"封面|首页.{0,20}(?:姓名|作者|学号|班级|学校|院系|专业|课程|导师)|"
                r"页眉|页脚|页码|cover|header|footer|page number",
                request,
                re.I,
            )
        )
        if needs_presentation:
            required.append("document.presentation.resolve")
        if render_requested:
            required.extend(["document.layout.resolve", "document.render", "document.qa"])
        for tool in required:
            require(tool)
        add(
            CandidateNode(
                node_id="document_classify",
                agent_type="writer_agent",
                objective=(
                    "Classify the requested document archetype with evidence and ambiguity flags."
                ),
                input_refs=list(outputs),
                output_keys=["document_classification"],
                required_tools=["document.classify"],
                success_criteria=["classification contains confidence and evidence"],
            )
        )
        if needs_presentation:
            add(
                CandidateNode(
                    node_id="document_presentation_resolve",
                    agent_type="requirement_agent",
                    objective=(
                        "Resolve confirmed cover and page-chrome requirements into one canonical "
                        "presentation without inventing personal values."
                    ),
                    input_refs=list(outputs),
                    output_keys=["resolved_presentation"],
                    required_tools=["document.presentation.resolve"],
                    success_criteria=[
                        "all supplied personal values are preserved and unresolved values block"
                    ],
                )
            )
        add(
            CandidateNode(
                node_id="document_structure",
                agent_type="writer_agent",
                objective=(
                    "Design a semantic section structure for the classified document "
                    "without generating final prose."
                ),
                input_refs=list(outputs),
                output_keys=["document_structure"],
                required_tools=["document.structure.plan"],
                success_criteria=[
                    "structure is archetype-appropriate and user constraints are retained"
                ],
            )
        )
        add(
            CandidateNode(
                node_id="document_compose",
                agent_type="writer_agent",
                objective=(
                    "Compose one canonical DocumentIR from requirements, evidence and "
                    "prior verified results."
                ),
                input_refs=list(outputs),
                output_keys=["document_ir"],
                required_tools=["document.compose"],
                approval=ApprovalRequirement(
                    action="write_canonical_document_revision",
                    risk="writes a new immutable revision inside the managed project workspace",
                    consequence="does not modify user files outside PaperAgent storage",
                ),
                success_criteria=[
                    "canonical DocumentIR validates and contains no private placeholders"
                ],
            )
        )

        needs_assets = requires_document_assets(request)
        if needs_assets and "asset.resolve" in available_tools:
            asset_tools = ["asset.resolve"]
            if "asset.derive" in available_tools:
                asset_tools.append("asset.derive")
            add(
                CandidateNode(
                    node_id="document_assets",
                    agent_type="visual_agent",
                    objective=(
                        "Resolve verified source assets and build every renderer derivative "
                        "before rendering."
                    ),
                    input_refs=list(outputs),
                    output_keys=["assembled_document_ir", "asset_set"],
                    required_tools=asset_tools,
                    approval=(
                        ApprovalRequirement(
                            action="derive_document_assets",
                            risk="writes renderer derivatives inside the managed workspace",
                            consequence="verified source assets remain immutable",
                        )
                        if "asset.derive" in asset_tools
                        else None
                    ),
                    success_criteria=[
                        "asset barrier is complete and every required figure is verified"
                    ],
                )
            )
        needs_citations = bool(re.search(r"引用|参考文献|文献|citation|reference", request, re.I))
        if needs_citations and "citation.format" in available_tools:
            add(
                CandidateNode(
                    node_id="document_citations",
                    agent_type="evidence_agent",
                    objective=(
                        "Format verified citations in the requested style and bind them "
                        "to evidence IDs."
                    ),
                    input_refs=list(outputs),
                    output_keys=["citation_set"],
                    required_tools=["citation.format"],
                    success_criteria=["citation set is traceable and style-valid"],
                )
            )
        if render_requested:
            add(
                CandidateNode(
                    node_id="document_layout",
                    agent_type="render_agent",
                    objective=(
                        "Resolve layout profile, typography, page geometry and renderer "
                        "capabilities before writing files."
                    ),
                    input_refs=list(outputs),
                    output_keys=["layout_profile", "render_plan"],
                    required_tools=["document.layout.resolve"],
                    success_criteria=["unsupported or degraded capabilities require confirmation"],
                )
            )
            add(
                CandidateNode(
                    node_id="document_render",
                    agent_type="render_agent",
                    objective=(
                        "Render all requested formats from the same canonical revision "
                        "and assembled asset set."
                    ),
                    input_refs=list(outputs),
                    output_keys=["rendered_artifacts"],
                    required_tools=["document.render"],
                    approval=ApprovalRequirement(
                        action="render_document_artifacts",
                        risk="writes requested output formats inside managed artifacts",
                        consequence="canonical revision and existing outputs remain immutable",
                    ),
                    success_criteria=["every requested artifact has a revision link and hash"],
                )
            )
            add(
                CandidateNode(
                    node_id="document_qa",
                    agent_type="review_agent",
                    objective=(
                        "Run structural, visual, asset, typography and placeholder QA "
                        "on every rendered artifact."
                    ),
                    input_refs=list(outputs),
                    output_keys=["document_qa", "repair_required"],
                    required_tools=["document.qa"],
                    success_criteria=["all completion claims have machine-verifiable evidence"],
                )
            )
        assert prior is not None
        return CandidatePlan(
            requirement_id=requirement_id,
            requirement_version=1,
            entry_node=nodes[0].node_id,
            terminal_nodes={prior},
            nodes=nodes,
            edges=edges,
            limits=BudgetLimits(max_input_tokens=64_000, max_output_tokens=16_000),
            rationale=(
                "Capability-driven DocumentProductionSubgraph shares canonical compose "
                "and asset assembly across requested renderers."
            ),
        )
