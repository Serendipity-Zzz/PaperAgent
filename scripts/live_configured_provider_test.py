from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from paperagent.core.config import Settings
from paperagent.db.manager import DatabaseManager
from paperagent.engine import AgentLoop, AgentLoopRequest
from paperagent.providers import ChatMessage
from paperagent.providers.adapters import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
)
from paperagent.providers.registry import ProviderRegistry
from paperagent.providers.routing import ProviderRouter
from paperagent.security.credentials import CredentialStore
from paperagent.tools import (
    ConcurrencyPolicy,
    ToolExecutor,
    ToolRegistry,
    ToolResultStore,
    ToolSpec,
)
from paperagent.tools.adapters import CallableToolAdapter


async def main() -> None:
    settings = Settings()
    databases = DatabaseManager(settings)
    databases.initialize_global()
    provider_id = os.environ.get("PAPERAGENT_LIVE_PROVIDER_ID")
    configs = [
        config
        for config in ProviderRegistry(databases).list()
        if provider_id is None or config.id == provider_id
    ]
    if not configs:
        raise SystemExit("No configured Provider with a credential was found")
    config = next((item for item in configs if item.credential_ref), None)
    if config is None:
        raise SystemExit("Configured Provider has no credential")
    credentials = CredentialStore(settings.resolved_data_dir / "global" / "credentials.json")

    def credential() -> str | None:
        return credentials.get(config.credential_ref) if config.credential_ref else None

    provider = (
        AnthropicProvider(config, credential)
        if config.provider_type == "anthropic"
        else GeminiProvider(config, credential)
        if config.provider_type == "gemini"
        else OpenAICompatibleProvider(config, credential)
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="workflow.step",
            version="1.0.0",
            description="Execute one named verification phase.",
            input_schema={
                "type": "object",
                "properties": {
                    "phase": {"type": "string", "enum": ["retrieve", "verify"]}
                },
                "required": ["phase"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            allowed_agents={"live_test_agent"},
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        CallableToolAdapter(
            lambda arguments: {
                "phase": arguments["phase"],
                "status": "completed",
                "next": "verify" if arguments["phase"] == "retrieve" else "answer",
            }
        ),
    )
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="paperagent-live-") as temporary:
            root = Path(temporary)
            loop = AgentLoop(
                ProviderRouter([provider]),
                registry,
                ToolExecutor(registry, ToolResultStore(root / "results")),
                root,
            )
            result = await loop.run(
                AgentLoopRequest(
                    project_id="live-provider-gate",
                    agent_type="live_test_agent",
                    messages=[
                        ChatMessage(
                            role="user",
                            content=(
                                "Use workflow.step with phase retrieve. After observing it, use "
                                "workflow.step again with phase verify. Only then answer LIVE_OK."
                            ),
                        )
                    ],
                    tool_names=["workflow.step"],
                    max_rounds=6,
                )
            )
        if result.tool_call_count < 2 or "LIVE_OK" not in result.content:
            raise RuntimeError("configured Provider did not complete the two-step ToolLoop")
        evidence = {
            "status": "passed",
            "provider_id": config.id,
            "provider_type": config.provider_type,
            "model": config.model,
            "rounds": result.rounds,
            "tool_call_count": result.tool_call_count,
            "routes": result.routes,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
        encoded = json.dumps(evidence, ensure_ascii=False, indent=2)
        print(encoded)
        target_value = os.environ.get("PAPERAGENT_LIVE_EVIDENCE")
        if target_value:
            target = Path(target_value).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encoded + "\n", encoding="utf-8")
    finally:
        await provider.client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
