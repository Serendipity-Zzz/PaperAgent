from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import Field

from paperagent.orchestration.failure import FailureRecord, RecoveryDecision, RecoveryPlanner
from paperagent.schemas.common import StrictModel


class StrategyHistory(StrictModel):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    strategies: list[str] = Field(default_factory=list)


class RecoveryStrategyLedger:
    def __init__(self, path: Path, max_strategies: int = 3) -> None:
        self.path = path.resolve()
        self.max_strategies = max_strategies

    def decide(self, failure: FailureRecord) -> RecoveryDecision:
        histories = self._load()
        fingerprint = failure.fingerprint()
        history = histories.setdefault(
            fingerprint, StrategyHistory(fingerprint=fingerprint)
        )
        decision = RecoveryPlanner().decide(
            failure,
            prior_strategies=history.strategies,
            max_strategy_attempts=self.max_strategies,
        )
        if decision.strategy not in history.strategies:
            history.strategies.append(decision.strategy)
            self._save(histories)
        return decision

    def _load(self) -> dict[str, StrategyHistory]:
        if not self.path.is_file():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("recovery strategy ledger must be an object")
        return {
            str(key): StrategyHistory.model_validate(value)
            for key, value in payload.items()
        }

    def _save(self, histories: dict[str, StrategyHistory]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".paperagent-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {
                        key: value.model_dump(mode="json")
                        for key, value in histories.items()
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
