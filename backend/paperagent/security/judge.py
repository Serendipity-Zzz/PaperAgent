from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import JsonValue

from paperagent.schemas.common import StrictModel
from paperagent.tools.permissions import PermissionOutcome


class JudgeInput(StrictModel):
    user_intent: str
    tool_name: str
    redacted_arguments: dict[str, JsonValue]


class JudgeDecision(StrictModel):
    outcome: PermissionOutcome
    reason: str
    risk: str


JudgeClassifier = Callable[[JudgeInput], Awaitable[JudgeDecision]]


class PermissionJudge:
    def __init__(self, classifier: JudgeClassifier) -> None:
        self.classifier = classifier

    async def classify(self, request: JudgeInput) -> JudgeDecision:
        try:
            decision = await self.classifier(request)
        except Exception:
            return JudgeDecision(
                outcome=PermissionOutcome.DENY,
                reason="judge failed to return a valid structured decision",
                risk="unknown",
            )
        return decision
