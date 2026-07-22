from __future__ import annotations

import re

from pydantic import JsonValue

from paperagent.execution.contracts import AuthorizationGrant
from paperagent.schemas.common import stable_json_hash
from paperagent.security.judge import JudgeInput, PermissionJudge
from paperagent.security.policy_trace import PolicyTrace
from paperagent.tools.contracts import PermissionPolicy, SideEffect, ToolCall, ToolSpec
from paperagent.tools.permissions import PermissionDecision, PermissionOutcome

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|password|credential|authorization)")


class ActorJudgePermissionEvaluator:
    def __init__(
        self,
        *,
        user_intent: str,
        authorized_tools: set[str] | None = None,
        judge: PermissionJudge | None = None,
        shadow_mode: bool = True,
    ) -> None:
        self.user_intent = user_intent
        self.authorized_tools = authorized_tools or set()
        self.judge = judge
        self.shadow_mode = shadow_mode
        self.traces: list[PolicyTrace] = []

    async def evaluate(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        approved: bool,
        grant: AuthorizationGrant | None = None,
    ) -> PermissionDecision:
        stages = ["hard_forbid"]
        hard = self._hard_policy(call, spec)
        if hard is not None:
            return self._record(call, stages, hard)
        stages.append("authorization_scope")
        grant_matches = bool(
            grant
            and spec.side_effect is not SideEffect.DESTRUCTIVE
            and grant.authorizes(
                spec.name,
                stable_json_hash(
                    {
                        "tool": spec.name,
                        "version": spec.version,
                        "arguments": call.arguments,
                    }
                ),
            )
        )
        if approved or grant_matches or spec.name in self.authorized_tools:
            return self._record(
                call,
                stages,
                PermissionDecision(
                    outcome=PermissionOutcome.ALLOW,
                    reason="action is inside explicit user authorization scope",
                    policy="authorization",
                ),
            )
        deterministic = self._deterministic(spec)
        stages.append("deterministic")
        if deterministic.outcome is not PermissionOutcome.REQUIRE_APPROVAL:
            return self._record(call, stages, deterministic)
        never_auto_approve = spec.side_effect is not SideEffect.NONE or bool(
            spec.capabilities
            & {"code_execution", "system_install", "publish", "unscanned_extension"}
        )
        if never_auto_approve:
            return self._record(call, stages, deterministic)
        if self.judge is None:
            return self._record(call, stages, deterministic)
        stages.append("judge")
        judged = await self.judge.classify(
            JudgeInput(
                user_intent=self.user_intent,
                tool_name=spec.name,
                redacted_arguments=self._redact(call.arguments),
            )
        )
        if self.shadow_mode:
            return self._record(call, stages, deterministic, shadow=judged.outcome.value)
        decision = PermissionDecision(
            outcome=judged.outcome,
            reason=judged.reason,
            policy="judge",
        )
        return self._record(call, stages, decision)

    @staticmethod
    def _hard_policy(call: ToolCall, spec: ToolSpec) -> PermissionDecision | None:
        if spec.permission_policy is PermissionPolicy.DENY:
            return PermissionDecision(
                outcome=PermissionOutcome.DENY,
                reason="tool policy explicitly denies execution",
                policy="hard_forbid",
            )
        if any(_SECRET_KEY.search(key) for key in call.arguments):
            return PermissionDecision(
                outcome=PermissionOutcome.DENY,
                reason="credential-like arguments cannot be exposed to tool execution",
                policy="hard_forbid",
            )
        return None

    @staticmethod
    def _deterministic(spec: ToolSpec) -> PermissionDecision:
        requires_approval = spec.permission_policy is PermissionPolicy.REQUIRE_APPROVAL
        requires_approval = requires_approval or spec.side_effect in {
            SideEffect.EXTERNAL,
            SideEffect.PAID,
            SideEffect.DESTRUCTIVE,
        }
        return PermissionDecision(
            outcome=(
                PermissionOutcome.REQUIRE_APPROVAL
                if requires_approval
                else PermissionOutcome.ALLOW
            ),
            reason=(
                "side effect requires explicit approval"
                if requires_approval
                else "deterministic low-risk policy permits execution"
            ),
            policy="deterministic",
        )

    @staticmethod
    def _redact(arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(key) else value
            for key, value in arguments.items()
        }

    def _record(
        self,
        call: ToolCall,
        stages: list[str],
        decision: PermissionDecision,
        *,
        shadow: str | None = None,
    ) -> PermissionDecision:
        self.traces.append(
            PolicyTrace(
                tool_name=call.tool_name,
                call_id=call.call_id,
                stages=stages,
                outcome=decision.outcome.value,
                reason=decision.reason,
                shadow_outcome=shadow,
            )
        )
        return decision
