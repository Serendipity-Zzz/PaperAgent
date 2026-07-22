from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from paperagent.execution.document_pipeline import DocumentPipelineTools
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.orchestration import CapabilityPlanFactory, PlanValidator
from paperagent.orchestration.document_production import (
    DocumentProductionSubgraph,
    DocumentRepairCategory,
    DocumentRepairPlanner,
    RenderFidelity,
    RenderPlanService,
)
from paperagent.orchestration.interactive import ALLOWED_INTERACTIVE_AGENTS
from paperagent.tools import ToolRegistry


def test_render_plan_requires_confirmation_before_unsupported_pdf() -> None:
    plan = RenderPlanService().negotiate(
        ["docx", "pdf", "md", "md_bundle"],
        layout_profile="academic-paper",
        word_available=False,
        tex_available=False,
    )
    capabilities = {item.format: item for item in plan.formats}
    assert capabilities["docx"].fidelity is RenderFidelity.EXACT
    assert capabilities["pdf"].fidelity is RenderFidelity.UNSUPPORTED
    assert capabilities["pdf"].confirmation_required is True
    assert capabilities["md"].confirmation_required is False
    assert capabilities["md_bundle"].fidelity is RenderFidelity.EXACT
    assert plan.shared_compose and plan.shared_asset_assembly


def test_document_repair_changes_strategy_and_resume_point() -> None:
    planner = DocumentRepairPlanner()
    first = planner.decide("figure asset not found", attempt=1)
    second = planner.decide(
        "figure asset not found",
        attempt=2,
        prior_strategies=[first.strategy],
    )
    assert first.category is DocumentRepairCategory.MISSING_ASSET
    assert first.resume_capability == "asset.resolve"
    assert first.strategy != second.strategy

    overflow = planner.decide("Overfull box caused layout overflow", attempt=1)
    assert overflow.category is DocumentRepairCategory.LAYOUT_OVERFLOW
    assert overflow.resume_capability == "document.layout.resolve"
    assert overflow.changes_document_content is False


def test_document_subgraph_is_capability_driven() -> None:
    available = set(ExecutionToolSuite.TOOL_NAMES)
    plain = DocumentProductionSubgraph().build_plan(
        "撰写一份项目报告",
        requirement_id=uuid4(),
        available_tools=available,
        render_requested=False,
    )
    assert [node.node_id for node in plain.nodes] == [
        "document_classify",
        "document_structure",
        "document_compose",
    ]

    rich = DocumentProductionSubgraph().build_plan(
        "撰写带实验图和参考文献的驻波报告,导出 PDF 和 DOCX",
        requirement_id=uuid4(),
        available_tools=available,
    )
    ids = [node.node_id for node in rich.nodes]
    assert ids == [
        "document_classify",
        "document_structure",
        "document_compose",
        "document_assets",
        "document_citations",
        "document_layout",
        "document_render",
        "document_qa",
    ]
    assets = next(node for node in rich.nodes if node.node_id == "document_assets")
    assert assets.required_tools == ["asset.resolve", "asset.derive"]
    assert len(rich.edges) == len(rich.nodes) - 1


def test_capability_factory_embeds_document_subgraph_after_optional_experiment() -> None:
    plan = CapabilityPlanFactory().build(
        "运行驻波实验并撰写带实验图的报告,导出 PDF",
        requirement_id=uuid4(),
        available_tools=list(ExecutionToolSuite.TOOL_NAMES),
    )
    ids = [node.node_id for node in plan.nodes]
    assert ids[0] == "experiment"
    assert ids.count("document_compose") == 1
    assert "document_assets" in ids
    assert ids[-1] == "document_qa"
    assert any(
        edge.source == "experiment" and edge.target == "document_classify" for edge in plan.edges
    )


def test_document_subgraph_passes_permissions_and_side_effect_gate(tmp_path: Path) -> None:
    suite = ExecutionToolSuite(
        data_root=tmp_path / "data",
        project_root=tmp_path / "project",
        run_id="run-plan",
        uv_path=None,
    )
    registry = ToolRegistry()
    try:
        suite.register(registry)
        plan = CapabilityPlanFactory().build(
            "撰写带图片和引用的实验报告,导出 PDF 和 DOCX",
            requirement_id=uuid4(),
            available_tools=list(ExecutionToolSuite.TOOL_NAMES),
        )
        report = PlanValidator(
            registry,
            allowed_agents=ALLOWED_INTERACTIVE_AGENTS,
        ).validate(plan)
        assert report.valid, report.issues
    finally:
        suite.close()


def test_document_pipeline_tools_produce_canonical_revision(tmp_path: Path) -> None:
    tools = DocumentPipelineTools(tmp_path)
    composed = tools.compose(
        {
            "title": "驻波实验报告",
            "content": "## 实验结果\n\n测得波腹与波节分布。",
            "language": "zh",
        }
    )
    assert isinstance(composed, dict)
    assert composed["schema_version"] == "2.2"
    assert composed["revision"] == 1
    assert tools.store.load(UUID(str(composed["document_id"]))).title == "驻波实验报告"

    layout = tools.layout_resolve(
        {
            "archetype": "experiment-report",
            "formats": ["pdf", "docx"],
            "word_available": True,
            "tex_available": True,
        }
    )
    assert isinstance(layout, dict)
    assert layout["render_plan"]["shared_compose"] is True
