from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from paperagent.engine import AgentLoop, AgentLoopLimitError, AgentLoopRequest
from paperagent.providers import (
    Capability,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderConfig,
    ProviderToolCall,
    Usage,
)
from paperagent.providers.routing import ProviderRouter
from paperagent.tools import (
    ConcurrencyPolicy,
    ToolExecutor,
    ToolRegistry,
    ToolResultStore,
    ToolSpec,
)
from paperagent.tools.adapters import CallableToolAdapter


class ScriptedProvider:
    def __init__(self, responses: list[ChatResponse]) -> None:
        self.config = ProviderConfig(
            id="scripted",
            provider_type="test",
            base_url="https://provider.example/v1",
            model="test-model",
            capabilities={Capability.CHAT, Capability.TOOLS},
        )
        self.responses = responses
        self.requests: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        del request
        if False:
            yield ""


def response(content: str = "", tool_calls: list[ProviderToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        model="test-model",
        finish_reason="tool_calls" if tool_calls else "stop",
        usage=Usage(input_tokens=10, output_tokens=5),
        tool_calls=tool_calls or [],
    )


def loop(tmp_path: Path, provider: ScriptedProvider) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="math.double",
            version="1.0.0",
            description="Double an integer.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            allowed_agents={"writer_agent"},
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        CallableToolAdapter(lambda arguments: {"value": int(arguments["value"]) * 2}),
    )
    return AgentLoop(
        ProviderRouter([provider]),
        registry,
        ToolExecutor(registry, ToolResultStore(tmp_path / "results")),
        tmp_path,
    )


@pytest.mark.anyio
async def test_agent_loop_observes_schema_error_corrects_tool_call_and_finishes(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            response(tool_calls=[ProviderToolCall(id="bad", name="math.double", arguments={})]),
            response(
                tool_calls=[
                    ProviderToolCall(id="fixed", name="math.double", arguments={"value": 4})
                ]
            ),
            response("结果是 8。"),
        ]
    )
    result = await loop(tmp_path, provider).run(
        AgentLoopRequest(
            project_id="project-1",
            agent_type="writer_agent",
            messages=[ChatMessage(role="user", content="把 4 加倍")],
            tool_names=["math.double"],
        )
    )
    assert result.content == "结果是 8。"
    assert result.rounds == 3 and result.tool_call_count == 2
    assert result.usage.input_tokens == 30
    assert "TOOL_INPUT_SCHEMA" in provider.requests[1].messages[-1].content
    assert '"value":8' in provider.requests[2].messages[-1].content
    assert provider.requests[1].messages[-1].tool_call_id == "bad"
    assert provider.requests[2].messages[-1].tool_call_id == "fixed"


@pytest.mark.anyio
async def test_agent_loop_enforces_round_and_tool_budgets(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [response(tool_calls=[ProviderToolCall(id="one", name="math.double", arguments={})])]
    )
    with pytest.raises(AgentLoopLimitError, match="tool call budget") as captured:
        await loop(tmp_path, provider).run(
            AgentLoopRequest(
                project_id="project-1",
                agent_type="writer_agent",
                messages=[ChatMessage(role="user", content="double")],
                tool_names=["math.double"],
                max_tool_calls=0,
            )
        )
    assert captured.value.messages[-1].role == "tool"
    assert "TOOL_BUDGET_EXCEEDED" in captured.value.messages[-1].content


@pytest.mark.anyio
async def test_agent_loop_rejects_duplicate_call_ids(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            response(
                tool_calls=[
                    ProviderToolCall(id="same", name="math.double", arguments={"value": 1}),
                    ProviderToolCall(id="same", name="math.double", arguments={"value": 2}),
                ]
            )
        ]
    )
    with pytest.raises(ValueError, match="duplicate tool call ids"):
        await loop(tmp_path, provider).run(
            AgentLoopRequest(
                project_id="project-1",
                agent_type="writer_agent",
                messages=[ChatMessage(role="user", content="double")],
                tool_names=["math.double"],
            )
        )


@pytest.mark.anyio
async def test_agent_loop_routes_around_provider_without_tool_capability(
    tmp_path: Path,
) -> None:
    basic = ScriptedProvider([])
    basic.config.capabilities = {Capability.CHAT}
    capable = ScriptedProvider([response("done")])
    configured = loop(tmp_path, capable)
    configured.router.providers.insert(0, basic)
    result = await configured.run(
        AgentLoopRequest(
            project_id="project-1",
            agent_type="writer_agent",
            messages=[ChatMessage(role="user", content="double")],
            tool_names=["math.double"],
        )
    )
    assert result.content == "done"
    assert basic.requests == []


@pytest.mark.anyio
async def test_agent_loop_enforces_usage_budget_before_executing_tools(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            response(
                tool_calls=[ProviderToolCall(id="one", name="math.double", arguments={"value": 2})]
            )
        ]
    )
    with pytest.raises(AgentLoopLimitError, match="INPUT_TOKEN") as captured:
        await loop(tmp_path, provider).run(
            AgentLoopRequest(
                project_id="project-1",
                agent_type="writer_agent",
                messages=[ChatMessage(role="user", content="double")],
                tool_names=["math.double"],
                max_total_input_tokens=5,
            )
        )
    assert "INPUT_TOKEN_BUDGET_EXCEEDED" in captured.value.messages[-1].content


@pytest.mark.anyio
async def test_agent_loop_injects_guidance_at_a_safe_model_boundary(tmp_path: Path) -> None:
    provider = ScriptedProvider([response("initial draft"), response("revised draft")])
    configured = loop(tmp_path, provider)
    calls = 0

    async def guidance() -> list[ChatMessage]:
        nonlocal calls
        calls += 1
        if calls == 2:
            return [ChatMessage(role="developer", content="Add the approved limitation section.")]
        return []

    configured.guidance_hook = guidance
    result = await configured.run(
        AgentLoopRequest(
            project_id="project-1",
            agent_type="writer_agent",
            messages=[ChatMessage(role="user", content="write")],
        )
    )
    assert result.content == "revised draft"
    assert result.rounds == 2
    assert provider.requests[1].messages[-1].content == "Add the approved limitation section."


@pytest.mark.anyio
async def test_agent_loop_stops_duplicate_side_effects_after_tool_contract(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            response(
                tool_calls=[
                    ProviderToolCall(id="completed", name="math.double", arguments={"value": 4})
                ]
            ),
            response(
                "I will inspect it again.",
                tool_calls=[
                    ProviderToolCall(id="duplicate", name="math.double", arguments={"value": 4})
                ],
            ),
        ]
    )
    result = await loop(tmp_path, provider).run(
        AgentLoopRequest(
            project_id="project-1",
            agent_type="writer_agent",
            messages=[ChatMessage(role="user", content="double once")],
            tool_names=["math.double"],
            required_successful_tools=["math.double"],
        )
    )

    assert result.finish_reason == "tool_contract_completed"
    assert result.tool_call_count == 1
    assert result.rounds == 2
    assert "NODE_TOOL_CONTRACT_ALREADY_SATISFIED" in result.messages[-1].content
    assert len(provider.requests) == 2


@pytest.mark.anyio
async def test_agent_loop_does_not_accept_prose_before_required_tool_contract(
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(
        [
            response("The result should be eight."),
            response(
                tool_calls=[
                    ProviderToolCall(id="required", name="math.double", arguments={"value": 4})
                ]
            ),
            response("Verified result: 8."),
        ]
    )

    result = await loop(tmp_path, provider).run(
        AgentLoopRequest(
            project_id="project-1",
            agent_type="writer_agent",
            messages=[ChatMessage(role="user", content="double with evidence")],
            tool_names=["math.double"],
            required_successful_tools=["math.double"],
        )
    )

    assert result.content == "Verified result: 8."
    assert result.rounds == 3
    assert result.tool_call_count == 1
    assert "math.double" in provider.requests[1].messages[-1].content
