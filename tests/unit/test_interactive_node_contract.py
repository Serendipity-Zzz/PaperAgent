from uuid import uuid4

from paperagent.engine import AgentLoopResult
from paperagent.orchestration.interactive import (
    CapabilityPlanFactory,
    _artifact_relation,
    _collected_figure_catalog,
    _compact_prior_results,
    _formats_from_layout,
    _ground_markdown_figures,
    _node_boundary,
    _node_contract_issue,
    _requested_document_formats,
    _with_upstream_artifact_evidence,
)
from paperagent.orchestration.plan_models import CandidateNode
from paperagent.providers import ChatMessage, Usage
from paperagent.tools import ToolResult, ToolResultStatus


def result_with_tools(*tools: tuple[str, list[str]]) -> AgentLoopResult:
    messages = [ChatMessage(role="assistant", content="done")]
    for index, (name, refs) in enumerate(tools):
        payload = ToolResult(
            call_id=f"call-{index}",
            status=ToolResultStatus.SUCCESS,
            content={"ok": True},
            artifact_refs=refs,
        )
        messages.append(
            ChatMessage(
                role="tool",
                content=payload.model_dump_json(),
                tool_call_id=payload.call_id,
                tool_name=name,
            )
        )
    return AgentLoopResult(
        content="done",
        messages=messages,
        rounds=1,
        tool_call_count=len(tools),
        usage=Usage(input_tokens=0, output_tokens=0),
        routes=["test"],
        finish_reason="stop",
    )


def test_experiment_node_rejects_prose_only_completion() -> None:
    node = CandidateNode(
        node_id="experiment",
        agent_type="experiment_agent",
        objective="run it",
        required_tools=["process.execute", "result.collect"],
        output_keys=["experiment_result"],
    )
    assert "process.execute" in (_node_contract_issue(node, result_with_tools()) or "")
    assert (
        _node_contract_issue(
            node,
            result_with_tools(
                ("process.execute", ["source-id"]),
                ("result.collect", ["figure-id"]),
            ),
        )
        is None
    )


def test_experiment_node_requires_real_figure_for_visual_request() -> None:
    node = CandidateNode(
        node_id="experiment",
        agent_type="experiment_agent",
        objective="run it",
        required_tools=["process.execute", "result.collect"],
        output_keys=["experiment_result"],
    )
    result = result_with_tools(
        ("process.execute", ["source-id"]),
        ("result.collect", ["pdf-id"]),
    )
    issue = _node_contract_issue(
        node,
        result,
        request_text="生成实验报告, 要有 Python 运行的效果图, 输出为pdf",
    )
    assert issue is not None
    assert "no verified independent image artifact" in issue

    collected = ToolResult.model_validate_json(result.messages[-1].content).model_copy(
        update={
            "content": {
                "artifacts": [
                    {
                        "artifact_id": "figure-id",
                        "name": "standing-wave.png",
                        "relative_path": "runs/test/standing-wave.png",
                        "relation": "figure",
                    }
                ]
            }
        }
    )
    result.messages[-1].content = collected.model_dump_json()
    assert (
        _node_contract_issue(
            node,
            result,
            request_text="生成实验报告, 要有 Python 运行的效果图, 输出为pdf",
        )
        is None
    )


def test_render_node_requires_verified_artifact_reference() -> None:
    node = CandidateNode(
        node_id="render",
        agent_type="render_agent",
        objective="render it",
        required_tools=["document.render"],
        output_keys=["render_result"],
    )
    assert "artifact reference" in (
        _node_contract_issue(node, result_with_tools(("document.render", []))) or ""
    )


def test_render_node_requires_every_requested_format() -> None:
    node = CandidateNode(
        node_id="document_render",
        agent_type="render_agent",
        objective="render all requested formats",
        required_tools=["document.render"],
        output_keys=["rendered_artifacts"],
    )
    result = result_with_tools(("document.render", ["docx-id"]))
    tool_message = result.messages[-1]
    payload = ToolResult.model_validate_json(tool_message.content).model_copy(
        update={"content": {"name": "paperagent-result.docx"}}
    )
    tool_message.content = payload.model_dump_json()

    issue = _node_contract_issue(
        node,
        result,
        expected_formats=["pdf", "docx"],
    )
    assert issue is not None
    assert "pdf" in issue

    pdf_result = result_with_tools(("document.render", ["pdf-id"]))
    pdf_payload = ToolResult.model_validate_json(pdf_result.messages[-1].content).model_copy(
        update={"content": {"name": "paperagent-result.pdf"}}
    )
    pdf_result.messages[-1].content = pdf_payload.model_dump_json()
    result.messages.append(pdf_result.messages[-1])
    assert (
        _node_contract_issue(
            node,
            result,
            expected_formats=["pdf", "docx"],
        )
        is None
    )


def test_document_layout_blocks_degraded_or_unsupported_renderer() -> None:
    node = CandidateNode(
        node_id="document_layout",
        agent_type="render_agent",
        objective="resolve layout",
        required_tools=["document.layout.resolve"],
        output_keys=["render_plan"],
    )
    result = result_with_tools(("document.layout.resolve", []))
    tool_message = result.messages[-1]
    payload = ToolResult.model_validate_json(tool_message.content).model_copy(
        update={
            "content": {
                "render_plan": {
                    "formats": [
                        {
                            "format": "pdf",
                            "fidelity": "unsupported",
                            "confirmation_required": True,
                        }
                    ]
                }
            }
        }
    )
    tool_message.content = payload.model_dump_json()
    assert "user confirmation" in (_node_contract_issue(node, result) or "")

    exact_payload = payload.model_copy(
        update={
            "content": {
                "render_plan": {
                    "formats": [
                        {
                            "format": "pdf",
                            "fidelity": "exact",
                            "confirmation_required": False,
                        }
                    ]
                }
            }
        }
    )
    tool_message.content = exact_payload.model_dump_json()
    assert _node_contract_issue(node, result) is None


def test_document_qa_failure_cannot_be_claimed_as_complete() -> None:
    node = CandidateNode(
        node_id="document_qa",
        agent_type="review_agent",
        objective="validate artifacts",
        required_tools=["document.qa"],
        output_keys=["document_qa"],
    )
    result = result_with_tools(("document.qa", []))
    tool_message = result.messages[-1]
    payload = ToolResult.model_validate_json(tool_message.content).model_copy(
        update={"content": {"passed": False, "issues": ["EMBEDDED_IMAGE_MISSING"]}}
    )
    tool_message.content = payload.model_dump_json()
    assert "EMBEDDED_IMAGE_MISSING" in (_node_contract_issue(node, result) or "")


def test_artifact_lookup_requires_tool_evidence_and_reference() -> None:
    node = CandidateNode(
        node_id="artifact_lookup",
        agent_type="artifact_agent",
        objective="return the original source",
        required_tools=["artifact.lookup"],
        output_keys=["artifact_result"],
    )
    assert "artifact.lookup" in (_node_contract_issue(node, result_with_tools()) or "")
    assert "artifact reference" in (
        _node_contract_issue(node, result_with_tools(("artifact.lookup", []))) or ""
    )
    assert (
        _node_contract_issue(
            node,
            result_with_tools(("artifact.lookup", ["original-source-id"])),
        )
        is None
    )


def test_artifact_relation_prefers_exact_source_intent() -> None:
    assert _artifact_relation("把刚才驻波实验的 Python 源码给我") == "source"
    assert _artifact_relation("下载刚才的 CSV 数据") == "data"
    assert _artifact_relation("打开上一版 PDF 报告") == "output"


def test_requested_document_formats_are_deterministic() -> None:
    assert _requested_document_formats("只生成新的 PDF 排版版本") == ["pdf"]
    assert _requested_document_formats("导出 Word 和 Markdown") == ["docx", "md"]
    assert _requested_document_formats("导出 Markdown bundle") == ["md_bundle"]
    assert _requested_document_formats("导出 Markdown 和 Markdown bundle") == [
        "md",
        "md_bundle",
    ]
    assert _requested_document_formats("结果输出为pdf") == ["pdf"]
    assert _requested_document_formats("另存为docx并保留md") == ["docx", "md"]
    assert _requested_document_formats("使用pdfium检查") == []


def test_writer_grounding_removes_invented_paths_and_attaches_verified_figures() -> None:
    catalog = [
        {
            "artifact_id": "figure-id",
            "name": "standing-wave.png",
            "relative_path": "runs/test/standing-wave.png",
        }
    ]
    content = "# 驻波实验\n\n![错误图片](made-up-id.png)\n\n## 结论\n\n完成。"
    grounded = _ground_markdown_figures(content, catalog, image_required=True)
    assert "made-up-id.png" not in grounded
    assert "![standing-wave.png](runs/test/standing-wave.png)" in grounded


def test_verified_figure_catalog_and_layout_formats_use_tool_evidence() -> None:
    collected = result_with_tools(("result.collect", ["figure-id"]))
    collected_payload = ToolResult.model_validate_json(collected.messages[-1].content).model_copy(
        update={
            "content": {
                "artifacts": [
                    {
                        "artifact_id": "figure-id",
                        "name": "standing-wave.png",
                        "relative_path": "runs/test/standing-wave.png",
                        "relation": "figure",
                    }
                ]
            }
        }
    )
    collected.messages[-1].content = collected_payload.model_dump_json()
    layout = result_with_tools(("document.layout.resolve", []))
    layout_payload = ToolResult.model_validate_json(layout.messages[-1].content).model_copy(
        update={"content": {"render_plan": {"formats": [{"format": "pdf"}]}}}
    )
    layout.messages[-1].content = layout_payload.model_dump_json()
    prior = {
        "experiment": collected.model_dump(mode="json"),
        "document_layout": layout.model_dump(mode="json"),
    }
    assert _collected_figure_catalog(prior, lambda item: item) == [
        {
            "artifact_id": "figure-id",
            "name": "standing-wave.png",
            "relative_path": "runs/test/standing-wave.png",
        }
    ]
    assert _formats_from_layout(prior, lambda item: item) == ["pdf"]


def test_typography_request_uses_style_only_plan_without_experiment() -> None:
    plan = CapabilityPlanFactory().build(
        "将报告字体改为宋体并导出 PDF, 不要重跑实验",
        requirement_id=uuid4(),
        available_tools=["document.render", "process.execute"],
    )
    assert [node.node_id for node in plan.nodes] == ["document_restyle"]
    assert plan.nodes[0].agent_type == "render_agent"
    assert all(node.agent_type != "experiment_agent" for node in plan.nodes)
    assert "rerun_experiment:false" in plan.assumptions


def test_experiment_boundary_keeps_document_rendering_downstream() -> None:
    node = CandidateNode(
        node_id="experiment",
        agent_type="experiment_agent",
        objective="run it",
        required_tools=["process.execute", "result.collect"],
        output_keys=["experiment_result"],
    )
    boundary = _node_boundary(node)
    assert "Do not generate MD, DOCX, PDF" in boundary
    assert "exit with code 0" in boundary


def test_prior_context_pack_excludes_tool_call_transcript_and_arguments() -> None:
    result = result_with_tools(("code.materialize", ["source-id"]))
    result.messages.insert(
        0,
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "secret-call",
                    "name": "code.materialize",
                    "arguments": {"content": "print('large secret source')"},
                }
            ],
        ),
    )
    compact = _compact_prior_results({"experiment": result.model_dump(mode="json")})
    serialized = str(compact)
    assert compact["experiment"]["artifact_refs"] == ["source-id"]
    assert "large secret source" not in serialized
    assert "code.materialize" not in serialized


def test_upstream_artifact_evidence_reaches_delivery_result() -> None:
    lookup = result_with_tools(("artifact.lookup", ["original-source-id"]))
    terminal = result_with_tools()
    merged = _with_upstream_artifact_evidence(
        terminal,
        {
            "artifact_lookup": lookup.model_dump(mode="json"),
            "respond": terminal.model_dump(mode="json"),
        },
        {"respond"},
    )
    serialized = "\n".join(message.content for message in merged.messages)
    assert "original-source-id" in serialized


def test_document_delivery_returns_human_summary_instead_of_raw_qa_json() -> None:
    render = result_with_tools(("document.render", ["pdf-id"]))
    render_payload = ToolResult.model_validate_json(render.messages[-1].content).model_copy(
        update={"content": {"name": "paperagent-result.pdf"}}
    )
    render.messages[-1].content = render_payload.model_dump_json()
    terminal = result_with_tools(("document.qa", []))
    terminal.content = '{"passed":true,"issues":[]}'
    merged = _with_upstream_artifact_evidence(
        terminal,
        {
            "document_render": render.model_dump(mode="json"),
            "document_qa": terminal.model_dump(mode="json"),
        },
        {"document_qa"},
    )
    assert "已完成文档生成和交付验收" in merged.content
    assert "实验执行" not in merged.content
    assert "paperagent-result.pdf" in merged.content
    assert not merged.content.startswith("{")
