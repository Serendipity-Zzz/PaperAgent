from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from paperagent.artifacts import ArtifactService
from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.engine import AgentLoop, AgentLoopRequest
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.orchestration import CapabilityPlanFactory, compile_dynamic_interactive_graph
from paperagent.providers import (
    Capability,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderConfig,
    Usage,
)
from paperagent.providers.routing import ProviderRouter
from paperagent.tools import ToolExecutor, ToolRegistry, ToolResultStore
from paperagent.tools.adapters import CallableToolAdapter
from paperagent.tools.builtins import builtin_tool_specs


class ScriptedProvider:
    def __init__(self, responses: list[str]) -> None:
        self.config = ProviderConfig(
            id="scripted",
            provider_type="test",
            base_url="https://provider.example/v1",
            model="test-model",
            capabilities={Capability.CHAT, Capability.TOOLS},
        )
        self.responses = list(responses)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        del request
        return ChatResponse(
            content=self.responses.pop(0),
            model="test-model",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        del request
        if False:
            yield ""


def build_loop(tmp_path: Path, responses: list[str]) -> tuple[AgentLoop, ToolRegistry]:
    registry = ToolRegistry()
    knowledge = next(item for item in builtin_tool_specs() if item.name == "knowledge.search")
    registry.register(
        knowledge,
        CallableToolAdapter(lambda _arguments: {"items": []}),
    )
    loop = AgentLoop(
        ProviderRouter([ScriptedProvider(responses)]),
        registry,
        ToolExecutor(registry, ToolResultStore(tmp_path / "results")),
        tmp_path,
    )
    return loop, registry


def test_capability_plan_topology_changes_with_request_and_tools() -> None:
    factory = CapabilityPlanFactory()
    plain = factory.build(
        "解释驻波原理", requirement_id=uuid4(), available_tools=[]
    )
    evidence = factory.build(
        "检索文献并解释驻波原理",
        requirement_id=uuid4(),
        available_tools=["knowledge.search"],
    )
    assert [node.node_id for node in plain.nodes] == ["respond"]
    assert [node.node_id for node in evidence.nodes] == ["evidence", "respond"]


def test_artifact_lookup_does_not_rerun_experiment_or_render() -> None:
    factory = CapabilityPlanFactory()
    plan = factory.build(
        (
            "请把刚才真实运行驻波实验所使用的原始 Python 源码文件直接提供给我下载,"
            "不要重新生成近似代码。"
        ),
        requirement_id=uuid4(),
        available_tools=[
            "artifact.lookup",
            "machine.inspect",
            "environment.prepare",
            "code.materialize",
            "process.execute",
            "result.collect",
            "document.render",
        ],
    )
    assert [node.node_id for node in plan.nodes] == ["artifact_lookup", "respond"]


def test_artifact_lookup_can_explicitly_request_recomputation() -> None:
    factory = CapabilityPlanFactory()
    plan = factory.build(
        "重新运行一次刚才的驻波实验,并给我新的数据",
        requirement_id=uuid4(),
        available_tools=["artifact.lookup", "process.execute", "result.collect"],
    )
    assert [node.node_id for node in plan.nodes] == [
        "artifact_lookup",
        "experiment",
        "respond",
    ]


def test_typography_revision_keeps_lookup_and_render_nodes() -> None:
    factory = CapabilityPlanFactory()
    plan = factory.build(
        "把刚才的 PDF 报告正文改为宋体,其他内容不变",
        requirement_id=uuid4(),
        available_tools=["artifact.lookup", "document.render"],
    )
    assert [node.node_id for node in plan.nodes] == [
        "artifact_lookup",
        "respond",
        "render",
    ]


def test_python_result_figure_request_requires_document_asset_barrier() -> None:
    plan = CapabilityPlanFactory().build(
        "生成驻波实验报告,要有 Python 运行的效果图,结果输出为 PDF",
        requirement_id=uuid4(),
        available_tools=[
            "process.execute",
            "result.collect",
            "document.classify",
            "document.structure.plan",
            "document.compose",
            "asset.resolve",
            "asset.derive",
            "document.layout.resolve",
            "document.render",
            "document.qa",
        ],
    )

    assets = next(node for node in plan.nodes if node.node_id == "document_assets")
    assert assets.required_tools == ["asset.resolve", "asset.derive"]


def test_format_conversion_uses_canonical_delivery_subgraph() -> None:
    plan = CapabilityPlanFactory().build(
        "将结果转成 PDF,不要重新运行实验",
        requirement_id=uuid4(),
        available_tools=[
            "document.resolve_revision",
            "document.bind_assets",
            "document.render",
            "document.qa",
            "document.validate_delivery",
        ],
    )
    assert [node.node_id for node in plan.nodes] == [
        "document_resolve_revision",
        "document_asset_barrier",
        "document_render",
        "document_validate_delivery",
    ]
    assert all(node.agent_type != "writer_agent" for node in plan.nodes)


@pytest.mark.anyio
async def test_dynamic_graph_executes_validated_capability_plan(tmp_path: Path) -> None:
    loop, _registry = build_loop(tmp_path, ["证据摘要", "最终回答"])
    captured = []
    request = AgentLoopRequest(
        project_id="project-1",
        agent_type="requirement_agent",
        messages=[ChatMessage(role="user", content="请检索文献后解释驻波")],
    )
    graph = compile_dynamic_interactive_graph(
        loop,
        request,
        available_tools=["knowledge.search"],
        result_sink=captured.append,
    )
    result = await graph.ainvoke({})
    assert result["plan_source"] == "capability_fallback"
    assert [node["node_id"] for node in result["plan"]["nodes"]] == [
        "evidence",
        "respond",
    ]
    assert captured[0].content == "最终回答"


@pytest.mark.anyio
async def test_presentation_revision_graph_executes_without_model_or_writer(
    tmp_path: Path,
) -> None:
    settings = Settings(
        project_root=tmp_path / "repo",
        data_dir=tmp_path / "data",
        environment="test",
    )
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project_id = str(uuid4())
    project_root = databases.project_root(project_id)
    project_root.mkdir(parents=True)
    artifacts = ArtifactService(databases, project_id)
    suite = ExecutionToolSuite(
        data_root=settings.resolved_data_dir,
        project_root=project_root,
        run_id="presentation-graph",
        uv_path=None,
        artifact_service=artifacts,
    )
    registry = ToolRegistry()
    suite.register(registry)
    provider = ScriptedProvider([])
    loop = AgentLoop(
        ProviderRouter([provider]),
        registry,
        ToolExecutor(registry, ToolResultStore(tmp_path / "tool-results")),
        project_root,
    )
    try:
        canonical = suite.document_pipeline.compose(
            {
                "title": "驻波实验报告",
                "content": "# 驻波实验报告\n\n## 结果\n\n已验证正文。",
                "language": "zh",
            }
        )
        assert isinstance(canonical, dict)
        before = suite.document_pipeline.store.load(UUID(str(canonical["document_id"])))
        request = AgentLoopRequest(
            project_id=project_id,
            agent_type="requirement_agent",
            messages=[ChatMessage(role="user", content="页眉改为课程名,输出 Markdown")],
            approved=True,
        )
        graph = compile_dynamic_interactive_graph(
            loop,
            request,
            available_tools=list(ExecutionToolSuite.TOOL_NAMES),
        )
        result = await graph.ainvoke({})
        assert result["plan_source"] == "document_invariant"
        assert [item["node_id"] for item in result["plan"]["nodes"]] == [
            "document_resolve_revision",
            "document_presentation_patch",
            "document_presentation_layout",
            "document_render",
            "document_validate_delivery",
        ]
        after = suite.document_pipeline.store.load(before.document_id)
        assert after.revision == before.revision + 1
        assert after.hashes().content_hash == before.hashes().content_hash
        assert after.hashes().asset_set_hash == before.hashes().asset_set_hash
        assert after.hashes().presentation_hash != before.hashes().presentation_hash
        assert provider.responses == []

        replay = await graph.ainvoke({})
        assert replay["plan_source"] == "document_invariant"
        replayed = suite.document_pipeline.store.load(before.document_id)
        assert replayed.revision == after.revision
        assert replayed.hashes().presentation_hash == after.hashes().presentation_hash
    finally:
        suite.close()
