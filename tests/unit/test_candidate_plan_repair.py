from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from paperagent.agents.state import (
    DocumentType,
    PrimaryLanguage,
    RawRequest,
    RequirementSpec,
    RequirementStatus,
)
from paperagent.engine import AgentLoopResult
from paperagent.orchestration.fallback_plans import safe_fallback_plan
from paperagent.orchestration.planner import CandidatePlanGenerator
from paperagent.providers import ChatMessage, Usage


def confirmed_requirement() -> RequirementSpec:
    return RequirementSpec(
        raw_request=RawRequest(text="写一篇测试报告"),
        normalized_request="写一篇测试报告",
        document_type=DocumentType.EXPERIMENT_REPORT,
        primary_language=PrimaryLanguage.ZH,
        target_length={"value": 2_000, "unit": "chinese_char"},
        audience="测试人员",
        citation_style="GB/T 7714",
        requires_literature_search=True,
        requires_experiment=False,
        requires_data_chart=False,
        requires_generated_image=False,
        output_formats=["md"],
        status=RequirementStatus.AWAITING_CONFIRMATION,
    ).confirm()


def result(content: str) -> AgentLoopResult:
    return AgentLoopResult(
        content=content,
        messages=[ChatMessage(role="assistant", content=content)],
        rounds=1,
        tool_call_count=0,
        usage=Usage(input_tokens=0, output_tokens=0),
        routes=["test:schema"],
        finish_reason="stop",
    )


def test_generator_feeds_validation_error_back_to_model() -> None:
    requirement = confirmed_requirement()
    plan = safe_fallback_plan(requirement)
    loop = AsyncMock()
    loop.run.side_effect = [
        result('{"plan_candidates": [{"rationale": "missing nodes"}]}'),
        result(
            '{"candidates": [' + plan.model_dump_json() + '], "recommended_index": 0}'
        ),
    ]

    generated = asyncio.run(
        CandidatePlanGenerator(loop).generate(
            requirement, project_id="project", available_tools=[]
        )
    )

    assert generated.candidates == [plan]
    assert loop.run.await_count == 2
    correction = loop.run.await_args_list[1].args[0].messages[-1].content
    assert "Validation errors" in correction
    assert "plan_candidates" in correction


def test_projection_only_normalizes_known_top_level_alias() -> None:
    requirement = confirmed_requirement()
    plan = safe_fallback_plan(requirement)
    payload = (
        '{"plan_candidates": ['
        + plan.model_dump_json()
        + '], "decision_guidance": {}, "next_step": "confirm"}'
    )

    projected = CandidatePlanGenerator._project_plan_set(payload)

    assert projected is not None
    assert projected.candidates == [plan]
    assert projected.recommended_index == 0


def test_projection_does_not_silently_repair_invalid_nested_plan() -> None:
    assert (
        CandidatePlanGenerator._project_plan_set(
            '{"plan_candidates": [{"rationale": "missing nodes"}]}'
        )
        is None
    )


def test_common_dialect_normalizes_node_mapping_and_legacy_limits_losslessly() -> None:
    requirement = confirmed_requirement()
    plan = safe_fallback_plan(requirement)
    payload = plan.model_dump(mode="json")
    payload["nodes"] = {
        node["node_id"]: {key: value for key, value in node.items() if key != "node_id"}
        for node in payload["nodes"]
    }
    payload["limits"] = {
        "max_duration_sec": 300,
        "max_retries": 3,
        "resource_bounds": {"memory_mb": 1024},
    }

    normalized = CandidatePlanGenerator._normalize_common_plan_dialect(
        json.dumps({"candidates": [payload], "recommended_index": 0})
    )

    assert normalized is not None
    assert normalized.candidates[0].nodes == plan.nodes
    assert normalized.candidates[0].limits.max_elapsed_ms == 300_000


def test_semantic_plan_dialect_is_mapped_and_strictly_revalidated() -> None:
    requirement = confirmed_requirement()
    payload = json.dumps(
        {
            "plan_candidates": [
                {
                    "nodes": [
                        {
                            "node_id": "start",
                            "type": "start",
                            "action": "初始化",
                            "outputs": [],
                            "dependencies": [],
                        },
                        {
                            "node_id": "experiment_run",
                            "type": "tool",
                            "action": "调用 experiment.run",
                            "outputs": ["data"],
                            "dependencies": ["start"],
                        },
                        {
                            "node_id": "report_render",
                            "type": "tool",
                            "action": "调用 document.render",
                            "outputs": ["pdf"],
                            "dependencies": ["experiment_run"],
                        },
                    ],
                    "limits": {"timeout_minutes": 10, "max_tool_calls": 8},
                    "rationale": "先实验后渲染",
                }
            ]
        },
        ensure_ascii=False,
    )

    adapted = CandidatePlanGenerator._adapt_semantic_plan_set(
        payload,
        requirement=requirement,
        available_tools=["experiment.run", "document.render"],
    )

    assert adapted is not None
    plan = adapted.candidates[0]
    assert plan.entry_node == "start"
    assert plan.terminal_nodes == {"report_render"}
    assert plan.nodes[1].agent_type == "experiment_agent"
    assert plan.nodes[1].required_tools == ["experiment.run"]
    assert plan.limits.max_elapsed_ms == 600_000


def test_semantic_adapter_rejects_unknown_dependency() -> None:
    requirement = confirmed_requirement()
    payload = json.dumps(
        {
            "candidates": [
                {
                    "nodes": [
                        {
                            "node_id": "write",
                            "type": "decision",
                            "action": "撰写",
                            "outputs": [],
                            "dependencies": ["missing"],
                        }
                    ]
                }
            ]
        },
        ensure_ascii=False,
    )

    assert (
        CandidatePlanGenerator._adapt_semantic_plan_set(
            payload, requirement=requirement, available_tools=[]
        )
        is None
    )


def test_semantic_adapter_accepts_mapping_nodes_and_ordered_fallback() -> None:
    requirement = confirmed_requirement()
    payload = json.dumps(
        {
            "candidates": [
                {
                    "nodes": {
                        "search": {
                            "tool": "knowledge.search",
                            "description": "检索证据",
                            "output": "evidence",
                        },
                        "render": {
                            "tool": "document.render",
                            "description": "渲染文档",
                            "output_formats": ["pdf", "docx"],
                        },
                    },
                    "limits": {"max_steps": 3},
                }
            ]
        },
        ensure_ascii=False,
    )

    adapted = CandidatePlanGenerator._adapt_semantic_plan_set(
        payload,
        requirement=requirement,
        available_tools=["knowledge.search", "document.render"],
    )

    assert adapted is not None
    plan = adapted.candidates[0]
    assert [(edge.source, edge.target) for edge in plan.edges] == [("search", "render")]
    assert plan.nodes[0].required_tools == ["knowledge.search"]


def test_semantic_adapter_maps_tool_transitions() -> None:
    requirement = confirmed_requirement()
    payload = json.dumps(
        {
            "candidates": [
                {
                    "nodes": [
                        {
                            "node_id": "search",
                            "tool_name": "knowledge.search",
                            "parameters": {"query": "本地检索"},
                            "transitions": [
                                {"next_node": "render", "condition": "true"}
                            ],
                        },
                        {
                            "node_id": "render",
                            "tool_name": "document.render",
                            "parameters": {"output_formats": ["pdf", "docx"]},
                            "transitions": [],
                        },
                    ],
                    "limits": {"max_duration_minutes": 20},
                }
            ]
        },
        ensure_ascii=False,
    )

    adapted = CandidatePlanGenerator._adapt_semantic_plan_set(
        payload,
        requirement=requirement,
        available_tools=["knowledge.search", "document.render"],
    )

    assert adapted is not None
    plan = adapted.candidates[0]
    assert [(edge.source, edge.target) for edge in plan.edges] == [("search", "render")]
    assert plan.nodes[1].output_keys == ["pdf", "docx"]
    assert plan.limits.max_elapsed_ms == 1_200_000


def test_semantic_adapter_normalizes_dotted_ids_and_next_nodes() -> None:
    requirement = confirmed_requirement()
    payload = json.dumps(
        {
            "candidates": [
                {
                    "nodes": [
                        {
                            "node_id": "s1.1",
                            "action": "检索",
                            "tool": "knowledge.search",
                            "output": "evidence",
                            "next_nodes": ["s1.2"],
                        },
                        {
                            "node_id": "s1.2",
                            "action": "渲染",
                            "tool": "document.render",
                            "output": "pdf",
                            "next_nodes": [],
                        },
                    ],
                    "limits": {"max_duration_minutes": 30, "max_api_calls": 7},
                }
            ]
        },
        ensure_ascii=False,
    )

    adapted = CandidatePlanGenerator._adapt_semantic_plan_set(
        payload,
        requirement=requirement,
        available_tools=["knowledge.search", "document.render"],
    )

    assert adapted is not None
    plan = adapted.candidates[0]
    assert [node.node_id for node in plan.nodes] == ["s1_1", "s1_2"]
    assert [(edge.source, edge.target) for edge in plan.edges] == [("s1_1", "s1_2")]
    assert plan.limits.max_tool_calls == 7
