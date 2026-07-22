from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import ClassVar
from uuid import UUID

from paperagent.context.invariants import ContextInvariantError, validate_tool_pairs
from paperagent.context.models import ContextItem, ContextItemKind, ContextPack
from paperagent.engine.budgets import BudgetDecision, BudgetLimits, BudgetUsage

_CJK = re.compile(r"[\u3400-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9_]+")


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    words = len(_WORD.findall(text))
    other = max(0, len(text) - cjk - sum(len(value) for value in _WORD.findall(text)))
    return max(1, math.ceil(cjk / 1.5 + words * 1.3 + other / 4)) if text else 0


class ContextAssembler:
    ORDER: ClassVar[dict[ContextItemKind, int]] = {
        ContextItemKind.SAFETY: 0,
        ContextItemKind.REQUIREMENT: 1,
        ContextItemKind.TASK_STATE: 2,
        ContextItemKind.MESSAGE: 3,
        ContextItemKind.MEMORY: 4,
        ContextItemKind.EVIDENCE: 5,
        ContextItemKind.SUMMARY: 6,
        ContextItemKind.TOOL_STATE: 7,
    }

    def assemble(
        self,
        *,
        trace_id: UUID,
        items: Iterable[ContextItem],
        limits: BudgetLimits,
        transcript_ref: str | None = None,
        recent_message_count: int = 8,
    ) -> ContextPack:
        candidates = list(items)
        validate_tool_pairs(candidates)
        self._validate_singletons(candidates)
        required = self._required_items(candidates, recent_message_count)
        required_tokens = sum(item.estimated_tokens for item in required)
        if required_tokens > limits.max_input_tokens:
            raise ContextInvariantError(
                "protected and recent context exceeds budget; split the task before compaction"
            )
        selected = list(required)
        required_ids = {item.item_id for item in required}
        remaining = [item for item in candidates if item.item_id not in required_ids]
        remaining.sort(
            key=lambda item: (-item.priority, self.ORDER[item.kind], item.source_id)
        )
        used = required_tokens
        for item in remaining:
            if used + item.estimated_tokens <= limits.max_input_tokens:
                selected.append(item)
                used += item.estimated_tokens
        selected.sort(key=lambda item: (self.ORDER[item.kind], item.source_id))
        selected_ids = {item.item_id for item in selected}
        decision = BudgetDecision(
            limits=limits,
            usage=BudgetUsage(input_tokens=used),
            selected_context_ids=[str(item.item_id) for item in selected],
            omitted_context_ids=[
                str(item.item_id) for item in candidates if item.item_id not in selected_ids
            ],
            frozen=True,
            reason="fixed-order priority selection within token budget",
        )
        return self._pack(trace_id, selected, decision, transcript_ref)

    @staticmethod
    def _required_items(items: list[ContextItem], recent_count: int) -> list[ContextItem]:
        required = [item for item in items if item.protected]
        messages = [item for item in items if item.kind is ContextItemKind.MESSAGE]
        required.extend(messages[-recent_count:])
        required.extend(
            item
            for item in items
            if "tool_call_ids" in item.metadata or "tool_call_id" in item.metadata
        )
        by_id = {item.item_id: item for item in required}
        return list(by_id.values())

    @staticmethod
    def _validate_singletons(items: list[ContextItem]) -> None:
        for kind in (ContextItemKind.REQUIREMENT, ContextItemKind.SUMMARY):
            if sum(item.kind is kind for item in items) > 1:
                raise ContextInvariantError(f"multiple {kind.value} items")

    @staticmethod
    def _pack(
        trace_id: UUID,
        items: list[ContextItem],
        decision: BudgetDecision,
        transcript_ref: str | None,
    ) -> ContextPack:
        def group(kind: ContextItemKind) -> list[ContextItem]:
            return [item for item in items if item.kind is kind]

        requirement = group(ContextItemKind.REQUIREMENT)
        summary = group(ContextItemKind.SUMMARY)
        return ContextPack(
            trace_id=trace_id,
            safety=group(ContextItemKind.SAFETY),
            requirement=requirement[0] if requirement else None,
            task_state=group(ContextItemKind.TASK_STATE),
            recent_messages=group(ContextItemKind.MESSAGE),
            memories=group(ContextItemKind.MEMORY),
            evidence=group(ContextItemKind.EVIDENCE),
            summary=summary[0] if summary else None,
            tool_state=group(ContextItemKind.TOOL_STATE),
            budget=decision,
            transcript_ref=transcript_ref,
        )
