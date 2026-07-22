from pathlib import Path

from paperagent.orchestration.failure import FailureCategory, FailureRecord
from paperagent.recovery.strategy import RecoveryStrategyLedger


def test_strategy_history_is_scoped_by_stable_failure_fingerprint(tmp_path: Path) -> None:
    ledger = RecoveryStrategyLedger(tmp_path / "strategy.json", max_strategies=2)
    failure = FailureRecord(
        node="writer",
        tool_name="knowledge.search",
        category=FailureCategory.INVALID_OUTPUT,
        code="SCHEMA_ERROR",
        message="field missing",
        input_hash="a" * 64,
    )
    first = ledger.decide(failure)
    second = ledger.decide(failure)
    third = ledger.decide(failure)
    assert first.strategy == "schema_repair_with_error_feedback"
    assert second.strategy == "reduce_output_scope_and_split"
    assert third.requires_human
    changed = failure.model_copy(update={"input_hash": "b" * 64})
    assert ledger.decide(changed).strategy == "schema_repair_with_error_feedback"
