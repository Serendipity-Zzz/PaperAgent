from paperagent.orchestration.failure import (
    FailureAnalyzer,
    FailureCategory,
    RecoveryAction,
    RecoveryPlanner,
)
from paperagent.providers import ProviderError


def test_failure_analyzer_redacts_keys_and_separates_transient_from_semantic() -> None:
    transient = FailureAnalyzer.analyze(
        "writer", ProviderError("TIMEOUT", "network timed out", retryable=True)
    )
    invalid = FailureAnalyzer.analyze("writer", ValueError("schema path blocks.0.text invalid"))
    secret = FailureAnalyzer.analyze("writer", RuntimeError("api_key=sk-not-for-logs"))

    assert transient.category is FailureCategory.TRANSIENT
    assert invalid.category is FailureCategory.INVALID_OUTPUT
    assert "sk-not-for-logs" not in secret.message


def test_recovery_planner_changes_strategy_instead_of_blind_retry() -> None:
    failure = FailureAnalyzer.analyze("writer", ValueError("invalid JSON schema"))
    first = RecoveryPlanner().decide(failure)
    second = RecoveryPlanner().decide(failure, prior_strategies=[first.strategy])

    assert first.action is RecoveryAction.REPAIR_OUTPUT
    assert first.strategy == "schema_repair_with_error_feedback"
    assert second.action is RecoveryAction.SPLIT_TASK
    assert second.strategy != first.strategy
    assert second.replan


def test_unknown_side_effect_is_reconciled_before_any_retry() -> None:
    failure = FailureAnalyzer.analyze(
        "image",
        ProviderError("TIMEOUT", "response state unknown", retryable=True, state_unknown=True),
    )
    decision = RecoveryPlanner().decide(failure)

    assert decision.action is RecoveryAction.RECONCILE
    assert decision.requires_human
    assert not decision.retry_node


def test_execution_failures_select_materially_different_recovery_strategies() -> None:
    cases = {
        RuntimeError("CUDA driver is incompatible"): (
            FailureCategory.CUDA,
            RecoveryAction.REDUCE_RESOURCE,
        ),
        RuntimeError("Traceback: NameError in experiment.py"): (
            FailureCategory.CODE,
            RecoveryAction.REPAIR_OUTPUT,
        ),
        RuntimeError("missing dataset column wavelength"): (
            FailureCategory.DATA,
            RecoveryAction.REQUEST_INPUT,
        ),
        RuntimeError("xelatex render failed: missing fontspec"): (
            FailureCategory.COMPILE_ERROR,
            RecoveryAction.REPAIR_OUTPUT,
        ),
        PermissionError("deletion is blocked by managed policy"): (
            FailureCategory.POLICY,
            RecoveryAction.REQUEST_INPUT,
        ),
    }
    for error, (category, action) in cases.items():
        failure = FailureAnalyzer.analyze("experiment", error)
        decision = RecoveryPlanner().decide(failure)
        assert failure.category is category
        assert decision.action is action
