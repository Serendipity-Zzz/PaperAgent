from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import cast

import httpx
from pydantic import JsonValue

from paperagent.providers.base import (
    Capability,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderConfig,
    ProviderError,
    ProviderToolCall,
    Usage,
)


def _arguments(value: object) -> dict[str, JsonValue]:
    if isinstance(value, str):
        value = json.loads(value or "{}")
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be an object")
    return cast(dict[str, JsonValue], value)


def _openai_tool(tool: dict[str, object]) -> dict[str, object]:
    if tool.get("type") == "function":
        return tool
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {"type": "object"}),
        },
    }


def _openai_message(message: ChatMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _response_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _response_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _response_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _provider_http_error(error: httpx.HTTPError, prefix: str = "") -> ProviderError:
    if isinstance(error, httpx.TimeoutException):
        return ProviderError("TIMEOUT", str(error), retryable=True, state_unknown=True)
    if isinstance(error, httpx.ConnectError):
        return ProviderError("CONNECT", str(error), retryable=True)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        code = {
            401: "AUTH",
            403: "AUTH",
            404: "ENDPOINT_OR_MODEL_NOT_FOUND",
            429: "RATE_LIMIT",
        }.get(status, f"{prefix}HTTP_ERROR" if prefix else "HTTP_ERROR")
        return ProviderError(code, str(error), retryable=status == 429 or status >= 500)
    return ProviderError(f"{prefix}NETWORK_ERROR", str(error), retryable=True)


def _anthropic_blocks(value: object) -> tuple[str, list[ProviderToolCall]]:
    text: list[str] = []
    calls: list[ProviderToolCall] = []
    for raw in _response_list(value, "Anthropic content"):
        block = _response_mapping(raw, "Anthropic content block")
        kind = block.get("type")
        if kind == "text":
            content = block.get("text")
            if not isinstance(content, str):
                raise ValueError("Anthropic text block is missing text")
            text.append(content)
        elif kind == "tool_use":
            call_id = block.get("id")
            name = block.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ValueError("Anthropic tool_use requires string id and name")
            calls.append(
                ProviderToolCall(
                    id=call_id,
                    name=name,
                    arguments=_arguments(block.get("input", {})),
                )
            )
    return "".join(text), calls


def _gemini_parts(value: object) -> tuple[str, list[ProviderToolCall]]:
    text: list[str] = []
    calls: list[ProviderToolCall] = []
    for index, raw in enumerate(_response_list(value, "Gemini parts")):
        part = _response_mapping(raw, "Gemini part")
        content = part.get("text")
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("Gemini text part must be a string")
            text.append(content)
        if "functionCall" not in part:
            continue
        function_call = _response_mapping(part["functionCall"], "Gemini functionCall")
        name = function_call.get("name")
        if not isinstance(name, str):
            raise ValueError("Gemini functionCall requires a string name")
        calls.append(
            ProviderToolCall(
                id=f"gemini-{index}-{name}",
                name=name,
                arguments=_arguments(function_call.get("args", {})),
            )
        )
    return "".join(text), calls


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: ProviderConfig,
        credential: Callable[[], str | None],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.credential = credential
        default_timeout = 300 if config.provider_type == "xiaomi_mimo" else 180
        configured_timeout = config.extra.get("request_timeout_seconds", default_timeout)
        timeout = (
            float(configured_timeout)
            if isinstance(configured_timeout, (int, float)) and configured_timeout > 0
            else float(default_timeout)
        )
        self.client = client or httpx.AsyncClient(timeout=timeout)

    def headers(self) -> dict[str, str]:
        key = self.credential()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def payload(self, request: ChatRequest, *, stream: bool = False) -> dict[str, object]:
        missing = request.required_capabilities() - self.config.capabilities
        if missing:
            raise ProviderError("CAPABILITY_UNSUPPORTED", str(sorted(missing)), retryable=False)
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": [_openai_message(message) for message in request.messages],
            "stream": stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = [_openai_tool(tool) for tool in request.tools]
        if request.response_schema:
            structured_mode = str(
                self.config.extra.get(
                    "structured_output_mode",
                    "json_object"
                    if self.config.provider_type in {"deepseek", "xiaomi_mimo"}
                    else "json_schema",
                )
            )
            if structured_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            else:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "paperagent_response",
                        "schema": request.response_schema,
                    },
                }
        payload.update(
            {
                key: value
                for key, value in self.config.extra.items()
                if key not in {"structured_output_mode", "request_timeout_seconds"}
            }
        )
        return payload

    async def chat(self, request: ChatRequest) -> ChatResponse:
        missing = request.required_capabilities() - self.config.capabilities
        if missing:
            raise ProviderError("CAPABILITY_UNSUPPORTED", str(sorted(missing)), retryable=False)
        try:
            headers = self.headers()
            if request.idempotency_key:
                headers["Idempotency-Key"] = request.idempotency_key
            response = await self.client.post(
                str(self.config.base_url).rstrip("/") + "/chat/completions",
                headers=headers,
                json=self.payload(request),
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            return ChatResponse(
                content=data["choices"][0]["message"].get("content") or "",
                model=data.get("model", self.config.model),
                finish_reason=data["choices"][0].get("finish_reason", "stop"),
                usage=Usage(
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                ),
                tool_calls=[
                    ProviderToolCall(
                        id=item["id"],
                        name=item["function"]["name"],
                        arguments=_arguments(item["function"].get("arguments", {})),
                    )
                    for item in data["choices"][0]["message"].get("tool_calls") or []
                ],
            )
        except httpx.HTTPError as error:
            raise _provider_http_error(error) from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError("INVALID_RESPONSE", str(error), retryable=False) from error

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        if Capability.STREAM not in self.config.capabilities:
            raise ProviderError("CAPABILITY_UNSUPPORTED", "stream", retryable=False)
        try:
            async with self.client.stream(
                "POST",
                str(self.config.base_url).rstrip("/") + "/chat/completions",
                headers=self.headers(),
                json=self.payload(request, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    data = json.loads(line.removeprefix("data: "))
                    choices = data.get("choices")
                    if not isinstance(choices, list) or not choices:
                        # Some OpenAI-compatible APIs end with a usage-only event.
                        continue
                    choice = _response_mapping(choices[0], "stream choice")
                    delta = _response_mapping(choice.get("delta", {}), "stream delta")
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield content
        except httpx.HTTPError as error:
            mapped = _provider_http_error(error, prefix="STREAM_")
            raise mapped from error
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ProviderError("INVALID_STREAM_RESPONSE", str(error), retryable=False) from error


def mimo_profile(*, base_url: str, model: str, credential_ref: str | None) -> ProviderConfig:
    return ProviderConfig.model_validate(
        {
            "id": "mimo",
            "provider_type": "xiaomi_mimo",
            "base_url": base_url,
            "model": model,
            "credential_ref": credential_ref,
            "capabilities": {
                Capability.CHAT,
                Capability.STREAM,
                Capability.TOOLS,
                Capability.STRUCTURED_OUTPUT,
                Capability.REASONING,
            },
        }
    )


class AnthropicProvider(OpenAICompatibleProvider):
    def headers(self) -> dict[str, str]:
        key = self.credential()
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        system = "\n".join(
            message.content
            for message in request.messages
            if message.role in {"system", "developer"}
        )
        messages: list[dict[str, object]] = []
        for message in request.messages:
            if message.role == "assistant":
                blocks: list[dict[str, object]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                messages.append({"role": "assistant", "content": blocks})
            elif message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content,
                            }
                        ],
                    }
                )
            elif message.role == "user":
                messages.append({"role": "user", "content": message.content})
        payload: dict[str, object] = {
            "model": self.config.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 4096,
        }
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object"}),
                }
                for tool in request.tools
            ]
        try:
            response = await self.client.post(
                str(self.config.base_url).rstrip("/") + "/messages",
                headers=self.headers(),
                json=payload,
            )
            response.raise_for_status()
            data = _response_mapping(response.json(), "Anthropic response")
            text, tool_calls = _anthropic_blocks(data.get("content", []))
            usage = _response_mapping(data.get("usage", {}), "Anthropic usage")
            model = data.get("model", self.config.model)
            stop_reason = data.get("stop_reason") or "stop"
            if not isinstance(model, str) or not isinstance(stop_reason, str):
                raise ValueError("Anthropic model and stop_reason must be strings")
            return ChatResponse(
                content=text,
                model=model,
                finish_reason=stop_reason,
                usage=Usage(
                    input_tokens=_response_int(
                        usage.get("input_tokens", 0), "Anthropic input_tokens"
                    ),
                    output_tokens=_response_int(
                        usage.get("output_tokens", 0), "Anthropic output_tokens"
                    ),
                ),
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as error:
            raise _provider_http_error(error, "ANTHROPIC_") from error
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError("INVALID_RESPONSE", str(error), retryable=False) from error

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": item.role, "content": item.content}
                for item in request.messages
                if item.role in {"user", "assistant"}
            ],
            "max_tokens": request.max_tokens or 4096,
            "stream": True,
        }
        async with self.client.stream(
            "POST",
            str(self.config.base_url).rstrip("/") + "/messages",
            headers=self.headers(),
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    text = data.get("delta", {}).get("text")
                    if text:
                        yield text


class GeminiProvider(OpenAICompatibleProvider):
    def headers(self) -> dict[str, str]:
        key = self.credential()
        return {"x-goog-api-key": key or ""}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        missing = request.required_capabilities() - self.config.capabilities
        if missing:
            raise ProviderError("CAPABILITY_UNSUPPORTED", str(sorted(missing)), retryable=False)
        system = "\n".join(
            message.content
            for message in request.messages
            if message.role in {"system", "developer"}
        )
        contents: list[dict[str, object]] = []
        for message in request.messages:
            if message.role == "assistant":
                parts: list[dict[str, object]] = []
                if message.content:
                    parts.append({"text": message.content})
                parts.extend(
                    {"functionCall": {"name": call.name, "args": call.arguments}}
                    for call in message.tool_calls
                )
                contents.append({"role": "model", "parts": parts})
            elif message.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.tool_name or message.tool_call_id,
                                    "response": {"content": message.content},
                                }
                            }
                        ],
                    }
                )
            elif message.role == "user":
                contents.append({"role": "user", "parts": [{"text": message.content}]})
        payload: dict[str, object] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("input_schema", {"type": "object"}),
                        }
                        for tool in request.tools
                    ]
                }
            ]
        try:
            response = await self.client.post(
                f"{str(self.config.base_url).rstrip('/')}/models/{self.config.model}:generateContent",
                headers=self.headers(),
                json=payload,
            )
            response.raise_for_status()
            data = _response_mapping(response.json(), "Gemini response")
            candidates = _response_list(data.get("candidates"), "Gemini candidates")
            candidate = _response_mapping(candidates[0], "Gemini candidate")
            content = _response_mapping(candidate.get("content"), "Gemini content")
            text, tool_calls = _gemini_parts(content.get("parts"))
            usage = _response_mapping(data.get("usageMetadata", {}), "Gemini usage")
            finish_reason = candidate.get("finishReason", "STOP")
            if not isinstance(finish_reason, str):
                raise ValueError("Gemini finishReason must be a string")
            return ChatResponse(
                content=text,
                model=self.config.model,
                finish_reason=finish_reason.lower(),
                usage=Usage(
                    input_tokens=_response_int(
                        usage.get("promptTokenCount", 0), "Gemini promptTokenCount"
                    ),
                    output_tokens=_response_int(
                        usage.get("candidatesTokenCount", 0),
                        "Gemini candidatesTokenCount",
                    ),
                ),
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as error:
            raise _provider_http_error(error, "GEMINI_") from error
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderError("INVALID_RESPONSE", str(error), retryable=False) from error

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        payload = {
            "contents": [
                {
                    "role": "model" if item.role == "assistant" else "user",
                    "parts": [{"text": item.content}],
                }
                for item in request.messages
                if item.role in {"user", "assistant"}
            ]
        }
        url = (
            f"{str(self.config.base_url).rstrip('/')}/models/"
            f"{self.config.model}:streamGenerateContent?alt=sse"
        )
        async with self.client.stream(
            "POST", url, headers=self.headers(), json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                        if part.get("text"):
                            yield part["text"]
