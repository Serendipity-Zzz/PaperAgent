from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from paperagent.providers import Capability, ChatMessage, ChatRequest, ProviderConfig
from paperagent.providers.adapters import OpenAICompatibleProvider


async def main() -> None:
    key = os.environ.get("PAPERAGENT_LIVE_API_KEY")
    if not key:
        raise SystemExit("Set PAPERAGENT_LIVE_API_KEY for this process only")
    base_url = os.environ.get("PAPERAGENT_LIVE_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("PAPERAGENT_LIVE_MODEL", "deepseek-v4-pro")
    provider = OpenAICompatibleProvider(
        ProviderConfig.model_validate(
            {
                "id": "live-deepseek",
                "provider_type": "deepseek",
                "base_url": base_url,
                "model": model,
                "capabilities": [
                    Capability.CHAT,
                    Capability.STREAM,
                    Capability.TOOLS,
                    Capability.STRUCTURED_OUTPUT,
                    Capability.REASONING,
                ],
                "extra": {"thinking": {"type": "disabled"}},
            }
        ),
        lambda: key,
    )
    started = time.perf_counter()
    try:
        basic = await provider.chat(
            ChatRequest(
                messages=[ChatMessage(role="user", content="Reply with LIVE_OK only.")],
                max_tokens=64,
            )
        )
        structured = await provider.chat(
            ChatRequest(
                messages=[
                    ChatMessage(
                        role="system",
                        content="Return JSON only with keys status and value.",
                    ),
                    ChatMessage(role="user", content="Set status to ok and value to 7."),
                ],
                max_tokens=256,
                response_schema={
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "value": {"type": "integer"},
                    },
                    "required": ["status", "value"],
                    "additionalProperties": False,
                },
            )
        )
        structured_value = json.loads(structured.content)
        tool = await provider.chat(
            ChatRequest(
                messages=[
                    ChatMessage(
                        role="user",
                        content="You must call echo_value with value 7. Do not answer directly.",
                    )
                ],
                max_tokens=256,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "echo_value",
                            "description": "Return the supplied integer.",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "integer"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
            )
        )
        if basic.content.strip() != "LIVE_OK":
            raise RuntimeError(
                "basic response did not satisfy the live assertion: "
                + repr(basic.content[:160])
            )
        if structured_value != {"status": "ok", "value": 7}:
            raise RuntimeError("structured response did not satisfy the live assertion")
        if not tool.tool_calls:
            raise RuntimeError("model returned no tool call")
        evidence = {
            "status": "passed",
            "model": basic.model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "basic": True,
            "structured_output": True,
            "tool_selection": True,
        }
        encoded = json.dumps(evidence, ensure_ascii=False)
        print(encoded)
        evidence_path = os.environ.get("PAPERAGENT_LIVE_EVIDENCE")
        if evidence_path:
            target = Path(evidence_path).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encoded + "\n", encoding="utf-8")
    finally:
        await provider.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
