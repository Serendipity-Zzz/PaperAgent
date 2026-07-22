from __future__ import annotations

from collections import Counter

from paperagent.context.models import ContextItem, ContextItemKind


class ContextInvariantError(ValueError):
    pass


def validate_tool_pairs(items: list[ContextItem]) -> None:
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    for item in items:
        if item.kind not in {ContextItemKind.MESSAGE, ContextItemKind.TOOL_STATE}:
            continue
        raw_calls = item.metadata.get("tool_call_ids", [])
        if isinstance(raw_calls, list):
            calls.update(str(call_id) for call_id in raw_calls)
        result_id = item.metadata.get("tool_call_id")
        if result_id is not None:
            results[str(result_id)] += 1
    mismatched = {
        call_id: (calls[call_id], results[call_id])
        for call_id in calls.keys() | results.keys()
        if calls[call_id] != 1 or results[call_id] != 1
    }
    if mismatched:
        raise ContextInvariantError(f"orphan or duplicate tool pair: {mismatched}")


def protected_facts(items: list[ContextItem]) -> set[str]:
    facts: set[str] = set()
    for item in items:
        raw = item.metadata.get("protected_facts", [])
        if isinstance(raw, list):
            facts.update(str(value) for value in raw if str(value).strip())
        if item.protected:
            facts.add(f"source:{item.source_id}:{item.content_hash}")
    return facts
