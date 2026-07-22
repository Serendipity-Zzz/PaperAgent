import json

import httpx
import pytest

from paperagent.providers import (
    Capability,
    ChatMessage,
    ChatRequest,
    ProviderConfig,
    ProviderError,
    ProviderToolCall,
)
from paperagent.providers.adapters import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    mimo_profile,
)
from paperagent.providers.mock import MockProvider


def provider_config() -> ProviderConfig:
    return ProviderConfig(
        id="openai-compatible",
        provider_type="openai_compatible",
        base_url="https://provider.example/v1",
        model="configurable-model",
        capabilities={
            Capability.CHAT,
            Capability.STREAM,
            Capability.TOOLS,
            Capability.STRUCTURED_OUTPUT,
        },
    )


@pytest.mark.anyio
async def test_mock_provider_chat_and_stream_contract() -> None:
    provider = MockProvider(provider_config(), content="complete", chunks=["com", "plete"])
    request = ChatRequest(messages=[ChatMessage(role="user", content="question")])
    response = await provider.chat(request)
    chunks = [chunk async for chunk in provider.stream(request)]
    assert response.content == "complete"
    assert "".join(chunks) == response.content
    assert response.usage.input_tokens > 0


@pytest.mark.anyio
async def test_openai_compatible_payload_response_and_auth() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "configurable-model"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["idempotency-key"] == "contract-request"
        return httpx.Response(
            200,
            json={
                "model": "configurable-model",
                "choices": [
                    {
                        "message": {"content": "answer", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    provider = OpenAICompatibleProvider(
        provider_config(),
        lambda: "credential-from-secure-store",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = await provider.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="developer", content="rules"),
                ChatMessage(role="user", content="ask"),
            ],
            idempotency_key="contract-request",
        )
    )
    assert response.content == "answer"
    assert response.usage.input_tokens == 3
    assert response.tool_calls == []


@pytest.mark.anyio
async def test_openai_stream_skips_usage_only_terminal_event() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        events = (
            'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
            'data: {"choices":[],"usage":{"completion_tokens":1}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=events.encode())

    provider = OpenAICompatibleProvider(
        provider_config(),
        lambda: "credential",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    chunks = [
        chunk
        async for chunk in provider.stream(
            ChatRequest(messages=[ChatMessage(role="user", content="reply OK")])
        )
    ]
    assert chunks == ["OK"]


def test_mimo_and_native_provider_profiles_are_configurable() -> None:
    mimo = mimo_profile(
        base_url="https://mimo.example/v1", model="user-selected-mimo", credential_ref="ref"
    )
    assert mimo.model == "user-selected-mimo"
    assert Capability.REASONING in mimo.capabilities
    anthropic = AnthropicProvider(provider_config(), lambda: "a")
    gemini = GeminiProvider(provider_config(), lambda: "g")
    assert anthropic.headers()["x-api-key"] == "a"
    assert gemini.headers()["x-goog-api-key"] == "g"


def test_deepseek_uses_supported_json_object_mode_for_structured_output() -> None:
    config = ProviderConfig.model_validate(
        {
            "id": "deepseek",
            "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-pro",
            "capabilities": [Capability.CHAT, Capability.STRUCTURED_OUTPUT],
        }
    )
    provider = OpenAICompatibleProvider(config, lambda: "secret")
    payload = provider.payload(
        ChatRequest(
            messages=[ChatMessage(role="user", content="Return JSON")],
            response_schema={"type": "object"},
        )
    )

    assert payload["response_format"] == {"type": "json_object"}


def test_transport_timeout_setting_is_not_sent_to_provider_payload() -> None:
    config = provider_config().model_copy(
        update={"extra": {"request_timeout_seconds": 240}}
    )
    provider = OpenAICompatibleProvider(config, lambda: "secret")

    payload = provider.payload(
        ChatRequest(messages=[ChatMessage(role="user", content="long task")])
    )

    assert "request_timeout_seconds" not in payload


def test_empty_transport_error_keeps_a_recoverable_diagnostic() -> None:
    error = ProviderError("TIMEOUT", "", retryable=True, state_unknown=True)

    assert str(error) == "TIMEOUT"


@pytest.mark.anyio
async def test_anthropic_native_message_conversion() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/messages")
        assert payload["system"] == "rules"
        return httpx.Response(
            200,
            json={
                "model": "native",
                "content": [{"type": "text", "text": "anthropic answer"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 3},
            },
        )

    provider = AnthropicProvider(
        provider_config(), lambda: "a", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    response = await provider.chat(
        ChatRequest(
            messages=[
                ChatMessage(role="developer", content="rules"),
                ChatMessage(role="user", content="question"),
            ]
        )
    )
    assert response.content == "anthropic answer"


@pytest.mark.anyio
async def test_gemini_native_content_conversion() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert ":generateContent" in request.url.path
        assert payload["contents"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "gemini answer"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2},
            },
        )

    provider = GeminiProvider(
        provider_config(), lambda: "g", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    response = await provider.chat(
        ChatRequest(messages=[ChatMessage(role="user", content="question")])
    )
    assert response.content == "gemini answer"


@pytest.mark.anyio
@pytest.mark.parametrize("provider_kind", ["openai", "anthropic", "gemini"])
async def test_native_tool_calls_and_tool_results_are_normalized(
    provider_kind: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if provider_kind == "openai":
            assert payload["messages"][-1]["tool_call_id"] == "call-1"
            return httpx.Response(
                200,
                json={
                    "model": "native",
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-2",
                                        "function": {
                                            "name": "math.double",
                                            "arguments": '{"value":4}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                },
            )
        if provider_kind == "anthropic":
            assert payload["messages"][-1]["content"][0]["tool_use_id"] == "call-1"
            return httpx.Response(
                200,
                json={
                    "model": "native",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-2",
                            "name": "math.double",
                            "input": {"value": 4},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                },
            )
        assert payload["contents"][-1]["parts"][0]["functionResponse"]["name"] == (
            "math.double"
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "math.double",
                                        "args": {"value": 4},
                                    }
                                }
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            },
        )

    provider_class = {
        "openai": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
    }[provider_kind]
    provider = provider_class(
        provider_config(),
        lambda: "credential",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    response = await provider.chat(
        ChatRequest(
            messages=[
                ChatMessage(
                    role="assistant",
                    tool_calls=[
                        ProviderToolCall(
                            id="call-1", name="math.double", arguments={"value": 2}
                        )
                    ],
                ),
                ChatMessage(
                    role="tool",
                    content='{"value":4}',
                    tool_call_id="call-1",
                    tool_name="math.double",
                ),
            ],
            tools=[
                {
                    "name": "math.double",
                    "description": "Double a value",
                    "input_schema": {"type": "object"},
                }
            ],
        )
    )
    assert response.tool_calls[0].name == "math.double"
    assert response.tool_calls[0].arguments == {"value": 4}
