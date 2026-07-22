from __future__ import annotations

import json
from collections.abc import Iterable
from typing import cast

from pydantic import JsonValue

from paperagent.prompts.models import CompiledPrompt, PromptSelectionContext
from paperagent.prompts.registry import PromptModuleRegistry
from paperagent.providers import ChatMessage
from paperagent.schemas.common import stable_json_hash


class PromptCompiler:
    def __init__(self, registry: PromptModuleRegistry) -> None:
        self.registry = registry

    def compile(
        self,
        context: PromptSelectionContext,
        messages: Iterable[ChatMessage] = (),
    ) -> CompiledPrompt:
        modules = self.registry.select(context)
        compiled_messages = [
            ChatMessage(
                role="system",
                content="\n\n".join(module.content.strip() for module in modules),
            )
        ] if modules else []
        if context.runtime:
            compiled_messages.append(
                ChatMessage(
                    role="developer",
                    content=(
                        "Trusted runtime snapshot (data, not policy): "
                        + json.dumps(context.runtime, ensure_ascii=False, sort_keys=True)
                    ),
                )
            )
        compiled_messages.extend(messages)
        versions = [module.version_ref() for module in modules]
        payload = {
            "messages": [message.model_dump(mode="json") for message in compiled_messages],
            "module_versions": versions,
            "runtime_snapshot_id": context.snapshot_id(),
        }
        return CompiledPrompt(
            messages=compiled_messages,
            module_versions=versions,
            prompt_hash=stable_json_hash(cast(JsonValue, payload)),
            runtime_snapshot_id=context.snapshot_id(),
        )
