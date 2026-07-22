from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.engine.budgets import BudgetDecision
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class ContextItemKind(StrEnum):
    SAFETY = "safety"
    REQUIREMENT = "requirement"
    TASK_STATE = "task_state"
    MESSAGE = "message"
    MEMORY = "memory"
    EVIDENCE = "evidence"
    SUMMARY = "summary"
    TOOL_STATE = "tool_state"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class ContextItem(StrictModel):
    schema_version: str = SCHEMA_VERSION
    item_id: UUID = Field(default_factory=uuid4)
    kind: ContextItemKind
    source_id: str = Field(min_length=1, max_length=1_024)
    content: str = Field(max_length=2_000_000)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    estimated_tokens: int = Field(ge=0)
    priority: int = Field(default=50, ge=0, le=100)
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    compressible: bool = True
    protected: bool = False
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def set_content_hash(self) -> Self:
        digest = stable_json_hash(self.content)
        if self.content_hash is not None and self.content_hash != digest:
            raise ValueError("context item content hash mismatch")
        self.content_hash = digest
        if self.protected and self.compressible:
            raise ValueError("protected context item cannot be compressible")
        return self


class ContextPack(StrictModel):
    schema_version: str = SCHEMA_VERSION
    pack_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    safety: list[ContextItem] = Field(default_factory=list)
    requirement: ContextItem | None = None
    task_state: list[ContextItem] = Field(default_factory=list)
    recent_messages: list[ContextItem] = Field(default_factory=list)
    memories: list[ContextItem] = Field(default_factory=list)
    evidence: list[ContextItem] = Field(default_factory=list)
    summary: ContextItem | None = None
    tool_state: list[ContextItem] = Field(default_factory=list)
    budget: BudgetDecision
    transcript_ref: str | None = None

    @model_validator(mode="after")
    def validate_items(self) -> Self:
        groups = [
            self.safety,
            [self.requirement] if self.requirement else [],
            self.task_state,
            self.recent_messages,
            self.memories,
            self.evidence,
            [self.summary] if self.summary else [],
            self.tool_state,
        ]
        items = [item for group in groups for item in group]
        ids = [item.item_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("context pack contains duplicate item ids")
        expected = (
            (self.safety, ContextItemKind.SAFETY),
            (self.task_state, ContextItemKind.TASK_STATE),
            (self.recent_messages, ContextItemKind.MESSAGE),
            (self.memories, ContextItemKind.MEMORY),
            (self.evidence, ContextItemKind.EVIDENCE),
            (self.tool_state, ContextItemKind.TOOL_STATE),
        )
        for group, kind in expected:
            if any(item.kind is not kind for item in group):
                raise ValueError(f"context group requires kind {kind.value}")
        if self.requirement and self.requirement.kind is not ContextItemKind.REQUIREMENT:
            raise ValueError("requirement context has wrong kind")
        if self.summary and self.summary.kind is not ContextItemKind.SUMMARY:
            raise ValueError("summary context has wrong kind")
        return self

    def stable_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"pack_id"})
        return stable_json_hash(payload)
