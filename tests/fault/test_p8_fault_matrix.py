from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from paperagent.recovery import FaultInjector, SideEffectAction, SideEffectState, SideEffectStore
from paperagent.recovery.service import InjectedFault


@pytest.mark.fault
@pytest.mark.parametrize("action", list(SideEffectAction))
@pytest.mark.parametrize("point", ["after_intent", "before_result"])
def test_crash_matrix_is_recoverable(tmp_path: Path, action: SideEffectAction, point: str) -> None:
    store = SideEffectStore(tmp_path / f"{action}-{point}.db")
    injector = FaultInjector({point}, seed=42)
    if point == "after_intent":
        with pytest.raises(InjectedFault):
            store.intent("p", action, "key", "operation", injector=injector)
        record = store.list("p")[0]
        assert record.state is SideEffectState.INTENT
    else:
        record = store.intent("p", action, "key", "operation")
        store.transition(record.id, SideEffectState.RUNNING)
        with pytest.raises(InjectedFault):
            store.transition(record.id, SideEffectState.SUCCEEDED, injector=injector)
        assert store.get(record.id).state is SideEffectState.RUNNING


@pytest.mark.fault
def test_seed_replays_same_random_fault_sequence() -> None:
    def sequence() -> list[str]:
        injector = FaultInjector(
            {"node", "api", "db", "file", "word", "typst", "preview"}, seed=11, probability=0.5
        )
        for point in ("node", "api", "db", "file", "word", "typst", "preview"):
            with suppress(InjectedFault):
                injector.hit(point)
        return injector.triggered

    assert sequence() == sequence()
