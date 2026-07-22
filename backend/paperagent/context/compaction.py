from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Self, cast

from pydantic import Field, JsonValue, model_validator

from paperagent.context.invariants import (
    ContextInvariantError,
    protected_facts,
    validate_tool_pairs,
)
from paperagent.context.models import ContextItem, ContextItemKind, Sensitivity
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel


class SessionSummary(StrictModel):
    schema_version: str = SCHEMA_VERSION
    original_goals: list[str] = Field(min_length=1)
    explicit_changes: list[str] = Field(default_factory=list)
    requirement_refs: list[str] = Field(default_factory=list)
    confirmed_decisions: list[str] = Field(default_factory=list)
    concepts_and_methods: list[str] = Field(default_factory=list)
    file_locators: list[str] = Field(default_factory=list)
    experiments: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    review_issues: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    unknown_side_effects: list[str] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    current_position: str
    next_steps: list[str] = Field(default_factory=list)
    protected_facts: list[str] = Field(default_factory=list)
    transcript_start: int = Field(ge=0)
    transcript_end: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_pointer(self) -> Self:
        if self.transcript_end < self.transcript_start:
            raise ValueError("transcript_end precedes transcript_start")
        return self


class CompactionCircuitOpen(RuntimeError):
    pass


SummaryBuilder = Callable[[list[ContextItem], set[str]], Awaitable[SessionSummary]]


class ContextCompactor:
    def __init__(self, max_failures: int = 3) -> None:
        self.max_failures = max_failures
        self.consecutive_failures = 0

    async def compact(
        self,
        items: list[ContextItem],
        builder: SummaryBuilder,
        *,
        source_id: str,
    ) -> ContextItem:
        if self.consecutive_failures >= self.max_failures:
            raise CompactionCircuitOpen("compaction circuit is open; split task or intervene")
        validate_tool_pairs(items)
        expected = protected_facts(items)
        try:
            summary = await builder(items, expected)
            missing = expected - set(summary.protected_facts)
            if missing:
                raise ContextInvariantError(
                    f"summary omitted protected facts: {sorted(missing)}"
                )
            metadata: dict[str, JsonValue] = {
                "transcript_start": summary.transcript_start,
                "transcript_end": summary.transcript_end,
                "protected_facts": cast(list[JsonValue], summary.protected_facts),
            }
            result = ContextItem(
                kind=ContextItemKind.SUMMARY,
                source_id=source_id,
                content=summary.model_dump_json(),
                estimated_tokens=max(1, len(summary.model_dump_json()) // 4),
                priority=90,
                sensitivity=Sensitivity.SENSITIVE,
                compressible=False,
                protected=True,
                metadata=metadata,
            )
        except Exception as error:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.max_failures:
                raise CompactionCircuitOpen(
                    "compaction failed three times; split task or require intervention"
                ) from error
            raise
        self.consecutive_failures = 0
        return result


def micro_compact(items: list[ContextItem]) -> list[ContextItem]:
    """Remove byte-identical compressible progress items without touching protected state."""
    seen: set[tuple[ContextItemKind, str]] = set()
    result: list[ContextItem] = []
    for item in reversed(items):
        key = (item.kind, item.content_hash or "")
        if item.compressible and not item.protected and key in seen:
            continue
        seen.add(key)
        result.append(item)
    return list(reversed(result))
