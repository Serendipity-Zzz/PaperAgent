# ruff: noqa: RUF001 - tests intentionally exercise natural Chinese punctuation.

from __future__ import annotations

import asyncio
from uuid import uuid4

from paperagent.agents.presentation_intent import (
    PresentationChangeIntentClassifier,
    PresentationImpactDomain,
)
from paperagent.orchestration.compiler import TaskGraphCompiler
from paperagent.orchestration.document_delivery import DocumentIntentClassifier
from paperagent.orchestration.document_production import DocumentProductionSubgraph
from paperagent.orchestration.failure import (
    FailureAnalyzer,
    FailureCategory,
    RecoveryPlanner,
)
from paperagent.orchestration.interactive import CapabilityPlanFactory
from paperagent.orchestration.plan_validation import PlanValidator
from paperagent.rendering.delivery import DocumentAction
from paperagent.steering import SteeringContext, SteeringImpactAgent, SteeringPlanValidator
from paperagent.tools import ToolRegistry
from paperagent.tools.adapters import CallableToolAdapter
from paperagent.tools.contracts import ToolSpec

PRESENTATION_TOOLS = {
    "document.resolve_revision",
    "document.presentation.patch",
    "document.layout.resolve",
    "document.render",
    "document.validate_delivery",
}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in PRESENTATION_TOOLS:
        registry.register(
            ToolSpec(
                name=name,
                version="1.0.0",
                description=name,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                capabilities={"document"},
                allowed_agents={"render_agent", "review_agent"},
            ),
            CallableToolAdapter(lambda _arguments: {}),
        )
    return registry


def test_presentation_change_intent_separates_domains_and_mixed_content() -> None:
    classifier = PresentationChangeIntentClassifier()
    presentation = classifier.classify(
        "学校改为新大学，页眉改为大学物理实验报告，输出 PDF"
    )
    assert not presentation.changes_content
    assert presentation.requested_formats == ["pdf"]
    assert set(presentation.affected_domains) == {
        PresentationImpactDomain.COVER_DATA,
        PresentationImpactDomain.HEADER_FOOTER,
    }
    assert len(presentation.operations) == 2

    mixed = classifier.classify("页眉改为课程名，同时正文新增一节误差分析")
    assert mixed.changes_content
    assert set(mixed.affected_domains) == {
        PresentationImpactDomain.HEADER_FOOTER,
        PresentationImpactDomain.CONTENT,
    }
    assert DocumentIntentClassifier().classify(
        "页眉改为课程名，同时正文新增一节误差分析"
    ).action is DocumentAction.REVISE_CONTENT

    page_chrome_only = classifier.classify(
        "学校改为新大学, 删除班级字段, 正文页眉改为高级物理实验报告, "
        "输出 PDF 和 DOCX, 不要重写正文"
    )
    assert not page_chrome_only.changes_content
    assert set(page_chrome_only.requested_formats) == {"pdf", "docx"}
    assert DocumentIntentClassifier().classify(
        "学校改为新大学, 删除班级字段, 正文页眉改为高级物理实验报告, "
        "输出 PDF 和 DOCX, 不要重写正文"
    ).action is DocumentAction.REVISE_PRESENTATION

    existing_report = DocumentIntentClassifier().classify(
        "基于刚才生成的报告，只修改文档呈现：学校改为“合成科技大学”，"
        "删除班级字段，正文页眉改为“高级物理实验报告”；重新导出 PDF 和 "
        "DOCX。不要重写正文。"
    )
    assert existing_report.action is DocumentAction.REVISE_PRESENTATION
    assert existing_report.preserve_content

    quoted = classifier.classify(
        "学校改为“合成科技大学”，课程改为“高级大学物理实验”，"
        "正文页眉改为“高级物理实验报告”"
    )
    values = [item.value for item in quoted.operations if item.value]
    token_values = [token.value for item in quoted.operations for token in item.tokens]
    assert values == ["合成科技大学", "高级大学物理实验"]
    assert token_values == ["高级物理实验报告"]


def test_presentation_only_plan_contains_no_research_or_generation_agents() -> None:
    plan = CapabilityPlanFactory().build(
        "学校改为新大学，页眉改为大学物理实验报告，输出 PDF",
        requirement_id=uuid4(),
        available_tools=sorted(PRESENTATION_TOOLS),
    )
    assert [node.node_id for node in plan.nodes] == [
        "document_resolve_revision",
        "document_presentation_patch",
        "document_presentation_layout",
        "document_render",
        "document_validate_delivery",
    ]
    assert not {
        "writer_agent",
        "experiment_agent",
        "evidence_agent",
        "visual_agent",
    }.intersection(node.agent_type for node in plan.nodes)
    report = PlanValidator(
        _registry(), allowed_agents={"render_agent", "review_agent"}
    ).validate(plan, document_action=DocumentAction.REVISE_PRESENTATION)
    assert report.valid, report.issues


def test_first_generation_inserts_presentation_resolver_only_when_needed() -> None:
    tools = {
        "document.classify",
        "document.structure.plan",
        "document.presentation.resolve",
        "document.compose",
    }
    rich = DocumentProductionSubgraph().build_plan(
        "写实验报告，姓名：张三，学校：某某大学，页眉：大学物理实验报告",
        requirement_id=uuid4(),
        available_tools=tools,
        render_requested=False,
    )
    plain = DocumentProductionSubgraph().build_plan(
        "写实验报告",
        requirement_id=uuid4(),
        available_tools=tools,
        render_requested=False,
    )
    assert "document_presentation_resolve" in {node.node_id for node in rich.nodes}
    assert "document_presentation_resolve" not in {node.node_id for node in plain.nodes}


def test_presentation_steering_invalidates_from_compose_or_patch_boundary() -> None:
    creation = DocumentProductionSubgraph().build_plan(
        "写实验报告，姓名：张三",
        requirement_id=uuid4(),
        available_tools={
            "document.classify",
            "document.structure.plan",
            "document.presentation.resolve",
            "document.compose",
        },
        render_requested=False,
    )
    creation_graph = TaskGraphCompiler().compile(creation)
    before_compose = SteeringContext(
        target_run_id="run-a",
        public_status="running",
        public_phase="structure",
        completed_nodes=("document_classify", "document_presentation_resolve"),
        task_graph=creation_graph,
    )
    decision = asyncio.run(
        SteeringImpactAgent().decide("补充班级：物理一班", before_compose)
    )
    assert decision.affected_nodes == ("document_compose",)

    revision = CapabilityPlanFactory().build(
        "页眉改为课程名，输出 PDF",
        requirement_id=uuid4(),
        available_tools=sorted(PRESENTATION_TOOLS),
    )
    revision_graph = TaskGraphCompiler().compile(revision)
    after_compose = SteeringContext(
        target_run_id="run-b",
        public_status="running",
        public_phase="render",
        completed_nodes=("document_resolve_revision",),
        task_graph=revision_graph,
    )
    revised = asyncio.run(
        SteeringImpactAgent().decide("学校改为新大学", after_compose)
    )
    assert revised.affected_nodes == ("document_presentation_patch",)
    impact = SteeringPlanValidator().validate(revised, after_compose)
    assert impact.invalidated_nodes == (
        "document_presentation_patch",
        "document_presentation_layout",
        "document_render",
        "document_validate_delivery",
    )


def test_presentation_failure_recovery_never_routes_to_experiment() -> None:
    schema = FailureAnalyzer.analyze(
        "document_presentation_patch",
        ValueError("presentation schema invalid"),
    )
    assert schema.category is FailureCategory.PRESENTATION_SCHEMA
    schema_decision = RecoveryPlanner().decide(schema)
    assert schema_decision.resume_node == "document_presentation_resolve"

    overflow = FailureAnalyzer.analyze(
        "document_presentation_layout",
        RuntimeError("cover layout overflow"),
    )
    overflow_decision = RecoveryPlanner().decide(overflow)
    assert overflow_decision.resume_node == "document_presentation_layout"
    assert "experiment" not in (overflow_decision.resume_node or "")
