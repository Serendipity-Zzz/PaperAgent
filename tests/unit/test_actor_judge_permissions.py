from uuid import uuid4

import pytest

from paperagent.security import (
    ActorJudgePermissionEvaluator,
    JudgeDecision,
    JudgeInput,
    PermissionJudge,
)
from paperagent.tools import PermissionPolicy, SideEffect, ToolCall, ToolSpec
from paperagent.tools.permissions import PermissionOutcome


def call(arguments: dict[str, object] | None = None) -> ToolCall:
    return ToolCall.model_validate(
        {
            "call_id": "call-1",
            "trace_id": uuid4(),
            "sequence": 1,
            "tool_name": "files.write",
            "arguments": arguments or {},
            "requested_by": "writer_agent",
            "idempotency_key": "idem-1",
        }
    )


def spec(
    *, side_effect: SideEffect = SideEffect.NONE,
    policy: PermissionPolicy = PermissionPolicy.DETERMINISTIC,
) -> ToolSpec:
    return ToolSpec(
        name="files.write",
        version="1.0.0",
        description="write file",
        input_schema={"type": "object"},
        side_effect=side_effect,
        permission_policy=policy,
    )


@pytest.mark.anyio
async def test_hard_secret_rule_denies_before_judge() -> None:
    evaluator = ActorJudgePermissionEvaluator(user_intent="write")
    decision = await evaluator.evaluate(
        call({"api_key": "never"}), spec(), approved=False
    )
    assert decision.outcome is PermissionOutcome.DENY
    assert evaluator.traces[0].stages == ["hard_forbid"]


@pytest.mark.anyio
async def test_paid_side_effect_is_never_auto_approved_by_judge() -> None:
    async def permissive(_request: JudgeInput) -> JudgeDecision:
        return JudgeDecision(
            outcome=PermissionOutcome.ALLOW,
            reason="allow",
            risk="low",
        )

    evaluator = ActorJudgePermissionEvaluator(
        user_intent="generate image",
        judge=PermissionJudge(permissive),
        shadow_mode=False,
    )
    decision = await evaluator.evaluate(
        call(), spec(side_effect=SideEffect.PAID), approved=False
    )
    assert decision.outcome is PermissionOutcome.REQUIRE_APPROVAL
    assert "judge" not in evaluator.traces[0].stages


@pytest.mark.anyio
async def test_judge_shadow_decision_cannot_change_real_policy() -> None:
    async def deny(_request: JudgeInput) -> JudgeDecision:
        return JudgeDecision(
            outcome=PermissionOutcome.DENY,
            reason="shadow deny",
            risk="medium",
        )

    evaluator = ActorJudgePermissionEvaluator(
        user_intent="ambiguous local action",
        judge=PermissionJudge(deny),
        shadow_mode=True,
    )
    decision = await evaluator.evaluate(
        call(), spec(policy=PermissionPolicy.REQUIRE_APPROVAL), approved=False
    )
    assert decision.outcome is PermissionOutcome.REQUIRE_APPROVAL
    assert evaluator.traces[0].shadow_outcome == "deny"
