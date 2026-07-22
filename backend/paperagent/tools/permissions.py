from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import Field

from paperagent.execution.contracts import AuthorizationGrant
from paperagent.schemas.common import StrictModel, stable_json_hash
from paperagent.tools.contracts import PermissionPolicy, SideEffect, ToolCall, ToolSpec


class PermissionOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PermissionDecision(StrictModel):
    outcome: PermissionOutcome
    reason: str = Field(min_length=1)
    policy: str


class PermissionEvaluator(Protocol):
    async def evaluate(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        approved: bool,
        grant: AuthorizationGrant | None = None,
    ) -> PermissionDecision: ...


class DeterministicPermissionEvaluator:
    """Safe baseline. P5-R15 adds the policy engine and ambiguous-action Judge."""

    async def evaluate(
        self,
        call: ToolCall,
        spec: ToolSpec,
        *,
        approved: bool,
        grant: AuthorizationGrant | None = None,
    ) -> PermissionDecision:
        if spec.permission_policy is PermissionPolicy.DENY:
            return PermissionDecision(
                outcome=PermissionOutcome.DENY,
                reason="tool policy denies execution",
                policy="tool",
            )
        action_hash = stable_json_hash(
            {
                "tool": spec.name,
                "version": spec.version,
                "arguments": call.arguments,
            }
        )
        reusable_grant = bool(
            grant
            and spec.side_effect is not SideEffect.DESTRUCTIVE
            and grant.authorizes(spec.name, action_hash)
        )
        needs_approval = spec.permission_policy is PermissionPolicy.REQUIRE_APPROVAL or (
            spec.side_effect in {SideEffect.EXTERNAL, SideEffect.PAID, SideEffect.DESTRUCTIVE}
        )
        if needs_approval and not approved and not reusable_grant:
            return PermissionDecision(
                outcome=PermissionOutcome.REQUIRE_APPROVAL,
                reason="tool side effect requires explicit approval",
                policy="deterministic",
            )
        return PermissionDecision(
            outcome=PermissionOutcome.ALLOW,
            reason=(
                "action matches a scoped authorization grant"
                if reusable_grant
                else "deterministic policy permits execution"
            ),
            policy="authorization" if reusable_grant else "deterministic",
        )
