from __future__ import annotations

from collections.abc import Callable

from pydantic import Field, JsonValue

from paperagent.providers import ChatMessage
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class PromptSelectionContext(StrictModel):
    schema_version: str = SCHEMA_VERSION
    agent_type: str
    task: str
    document_type: str | None = None
    language: str | None = None
    features: set[str] = Field(default_factory=set)
    runtime: dict[str, JsonValue] = Field(default_factory=dict)

    def snapshot_id(self) -> str:
        return stable_json_hash(self)


class PromptModule(StrictModel):
    schema_version: str = SCHEMA_VERSION
    module_id: str = Field(pattern=r"^[a-z][a-z0-9._/-]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    priority: int = Field(ge=0, le=10_000)
    content: str = Field(min_length=1, max_length=100_000)
    agent_types: set[str] = Field(default_factory=set)
    tasks: set[str] = Field(default_factory=set)
    document_types: set[str] = Field(default_factory=set)
    languages: set[str] = Field(default_factory=set)
    required_features: set[str] = Field(default_factory=set)

    def applies(self, context: PromptSelectionContext) -> bool:
        return (
            (not self.agent_types or context.agent_type in self.agent_types)
            and (not self.tasks or context.task in self.tasks)
            and (
                not self.document_types
                or context.document_type in self.document_types
            )
            and (not self.languages or context.language in self.languages)
            and self.required_features <= context.features
        )

    def version_ref(self) -> str:
        return f"{self.module_id}@{self.version}"


class CompiledPrompt(StrictModel):
    schema_version: str = SCHEMA_VERSION
    messages: list[ChatMessage]
    module_versions: list[str]
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")


PromptRenderer = Callable[[PromptSelectionContext, PromptModule], str]
