from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ContextBudget:
    max_chars: int
    recent_chars: int


@dataclass(frozen=True)
class ContextEnvelope:
    summary_version: int
    summary: str
    recent_messages: list[str]
    protected_ids: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    pending_tasks: list[str] = field(default_factory=list)


class RollingContextBuilder:
    def build(
        self,
        messages: list[str],
        budget: ContextBudget,
        *,
        protected_ids: list[str],
        decisions: list[str],
        pending_tasks: list[str],
        previous_version: int = 0,
    ) -> ContextEnvelope:
        recent: list[str] = []
        used = 0
        split_at = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if used + len(message) > budget.recent_chars and recent:
                split_at = index + 1
                break
            recent.insert(0, message)
            used += len(message)
            split_at = index
        older = messages[:split_at]
        summary = "\n".join(item[:240] for item in older)
        fixed = "\n".join([*protected_ids, *decisions, *pending_tasks])
        available = max(budget.max_chars - used - len(fixed), 0)
        summary = summary[-available:] if available else ""
        return ContextEnvelope(
            summary_version=previous_version + 1,
            summary=summary,
            recent_messages=recent,
            protected_ids=list(dict.fromkeys(protected_ids)),
            decisions=list(decisions),
            pending_tasks=list(pending_tasks),
        )
