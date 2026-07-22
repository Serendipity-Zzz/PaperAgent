from uuid import uuid4

import pytest

from paperagent.context import (
    CompactionCircuitOpen,
    ContextAssembler,
    ContextCompactor,
    ContextInvariantError,
    ContextItem,
    ContextItemKind,
    Sensitivity,
    SessionSummary,
    estimate_tokens,
    micro_compact,
)
from paperagent.engine import BudgetLimits


def item(
    kind: ContextItemKind,
    source: str,
    content: str,
    *,
    tokens: int = 10,
    protected: bool = False,
    metadata: dict[str, object] | None = None,
) -> ContextItem:
    return ContextItem.model_validate(
        {
            "kind": kind,
            "source_id": source,
            "content": content,
            "estimated_tokens": tokens,
            "protected": protected,
            "compressible": not protected,
            "sensitivity": Sensitivity.PERSONAL,
            "metadata": metadata or {},
        }
    )


def test_multilingual_token_estimate_and_fixed_budget_selection() -> None:
    assert estimate_tokens("这是中文") > 0
    assert estimate_tokens("English words") > 0
    assert estimate_tokens("中英 mixed 123") > 0
    requirement = item(
        ContextItemKind.REQUIREMENT,
        "requirement:v2",
        "confirmed",
        tokens=20,
        protected=True,
    )
    recent = item(ContextItemKind.MESSAGE, "message:2", "latest", tokens=20)
    low = item(ContextItemKind.MEMORY, "memory:old", "old", tokens=30)
    pack = ContextAssembler().assemble(
        trace_id=uuid4(),
        items=[low, requirement, recent],
        limits=BudgetLimits(max_input_tokens=45, max_output_tokens=100),
        transcript_ref="sessions/thread/events.jsonl#1-2",
        recent_message_count=1,
    )
    assert pack.requirement == requirement
    assert pack.recent_messages == [recent]
    assert pack.memories == []
    assert pack.budget.frozen


def test_orphan_tool_pair_is_blocked() -> None:
    assistant = item(
        ContextItemKind.MESSAGE,
        "message:assistant",
        "calling",
        metadata={"tool_call_ids": ["call-1"]},
    )
    with pytest.raises(ContextInvariantError, match="tool pair"):
        ContextAssembler().assemble(
            trace_id=uuid4(),
            items=[assistant],
            limits=BudgetLimits(max_input_tokens=100, max_output_tokens=100),
        )


@pytest.mark.anyio
async def test_compaction_is_atomic_and_opens_circuit_after_three_omissions() -> None:
    source = item(
        ContextItemKind.REQUIREMENT,
        "requirement:v3",
        "must retain",
        protected=True,
        metadata={"protected_facts": ["requirement:v3", "font:SimSun"]},
    )

    async def incomplete(
        _items: list[ContextItem], _facts: set[str]
    ) -> SessionSummary:
        return SessionSummary(
            original_goals=["write paper"],
            current_position="writing",
            protected_facts=[],
            transcript_start=1,
            transcript_end=20,
        )

    compactor = ContextCompactor()
    for _ in range(2):
        with pytest.raises(ContextInvariantError, match="omitted"):
            await compactor.compact([source], incomplete, source_id="summary:1")
    with pytest.raises(CompactionCircuitOpen, match="three times"):
        await compactor.compact([source], incomplete, source_id="summary:1")
    with pytest.raises(CompactionCircuitOpen, match="circuit is open"):
        await compactor.compact([source], incomplete, source_id="summary:1")


@pytest.mark.anyio
async def test_valid_compaction_preserves_facts_and_transcript_pointer() -> None:
    source = item(
        ContextItemKind.TASK_STATE,
        "task:current",
        "continue",
        protected=True,
        metadata={"protected_facts": ["todo:render"]},
    )

    async def complete(_items: list[ContextItem], facts: set[str]) -> SessionSummary:
        return SessionSummary(
            original_goals=["write paper"],
            todos=["render"],
            current_position="review",
            next_steps=["render"],
            protected_facts=sorted(facts),
            transcript_start=10,
            transcript_end=30,
        )

    summary = await ContextCompactor().compact(
        [source], complete, source_id="sessions/thread/summary.json"
    )
    assert summary.kind is ContextItemKind.SUMMARY
    assert "todo:render" in summary.content
    assert summary.metadata["transcript_end"] == 30


def test_micro_compaction_keeps_latest_duplicate_and_protected_content() -> None:
    older = item(ContextItemKind.TASK_STATE, "progress:1", "50%")
    newer = item(ContextItemKind.TASK_STATE, "progress:2", "50%")
    protected = item(
        ContextItemKind.TASK_STATE,
        "approval:1",
        "approved",
        protected=True,
    )
    assert micro_compact([older, protected, newer]) == [protected, newer]
