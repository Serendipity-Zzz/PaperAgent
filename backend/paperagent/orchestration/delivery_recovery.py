from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from paperagent.orchestration.failure import FailureRecord, RecoveryDecision, RecoveryPlanner


class DeliveryCheckpoint(BaseModel):
    """Durable, versioned boundary for resuming document delivery only."""

    schema_version: int = 2
    project_id: str
    task_id: str
    document_id: str
    revision: int = Field(ge=1)
    canonical_artifact_id: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)
    pending_ids: tuple[str, ...] = ()
    requested_formats: tuple[str, ...] = ()
    delivered_formats: dict[str, str] = Field(default_factory=dict)
    qa_results: dict[str, Any] = Field(default_factory=dict)
    idempotency_keys: dict[str, str] = Field(default_factory=dict)
    failure_fingerprint: str | None = None
    strategy_history: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    safe_resume_node: str = "document_resolve_revision"
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class DeliveryCheckpointStore:
    """Atomic JSON checkpoint storage kept beside project-local agent state."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        safe = "".join(char for char in task_id if char.isalnum() or char in "-_")
        if not safe:
            raise ValueError("task_id has no safe filename characters")
        return self.root / f"{safe}.json"

    def save(self, checkpoint: DeliveryCheckpoint) -> Path:
        target = self._path(checkpoint.task_id)
        temporary = target.with_suffix(f".{os.getpid()}.tmp")
        payload = checkpoint.model_copy(
            update={"schema_version": 2, "updated_at": datetime.now(UTC).isoformat()}
        ).model_dump_json(indent=2)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, target)
        return target

    def load(self, task_id: str) -> DeliveryCheckpoint | None:
        path = self._path(task_id)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        version = int(raw.get("schema_version", 1))
        if version == 1:
            raw = self._migrate_v1(raw)
        if int(raw.get("schema_version", 0)) != 2:
            raise ValueError("unsupported delivery checkpoint schema")
        return DeliveryCheckpoint.model_validate(raw)

    @staticmethod
    def _migrate_v1(raw: dict[str, Any]) -> dict[str, Any]:
        migrated = dict(raw)
        migrated["schema_version"] = 2
        migrated.setdefault("delivered_formats", {})
        migrated.setdefault("qa_results", {})
        migrated.setdefault("idempotency_keys", {})
        migrated.setdefault("strategy_history", {})
        migrated.setdefault("safe_resume_node", "document_resolve_revision")
        return migrated


class DeliveryRecoveryRouter:
    """Maps a normalized failure to one minimal resume node and records strategy use."""

    def __init__(self, planner: RecoveryPlanner | None = None) -> None:
        self.planner = planner or RecoveryPlanner()

    def route(
        self, failure: FailureRecord, checkpoint: DeliveryCheckpoint
    ) -> tuple[RecoveryDecision, DeliveryCheckpoint]:
        fingerprint = failure.fingerprint()
        prior = list(checkpoint.strategy_history.get(fingerprint, ()))
        decision = self.planner.decide(failure, prior_strategies=prior)
        updated_history = dict(checkpoint.strategy_history)
        updated_history[fingerprint] = tuple([*prior, decision.strategy])
        updated = checkpoint.model_copy(
            update={
                "failure_fingerprint": fingerprint,
                "strategy_history": updated_history,
                "safe_resume_node": decision.resume_node or checkpoint.safe_resume_node,
            }
        )
        return decision, updated


class DeliveryRecoveryState(TypedDict, total=False):
    failure: FailureRecord
    checkpoint: DeliveryCheckpoint
    decision: RecoveryDecision
    resume_node: str


_RECOVERY_NODES = {
    "document_resolve_revision",
    "document_compose",
    "document_asset_barrier",
    "document_asset_derive",
    "document_render",
    "document_layout_resolve",
    "document_validate_delivery",
    "human_takeover",
}


def compile_delivery_recovery_graph() -> Any:
    """Executable conditional graph that selects, but never fakes, the repair boundary."""

    router = DeliveryRecoveryRouter()
    builder = StateGraph(DeliveryRecoveryState)

    def classify(state: DeliveryRecoveryState) -> DeliveryRecoveryState:
        decision, checkpoint = router.route(state["failure"], state["checkpoint"])
        resume_node = decision.resume_node or "human_takeover"
        if decision.requires_human:
            resume_node = "human_takeover"
        if resume_node not in _RECOVERY_NODES:
            resume_node = "human_takeover"
        return {"decision": decision, "checkpoint": checkpoint, "resume_node": resume_node}

    builder.add_node("classify_delivery_failure", classify)
    for node_name in sorted(_RECOVERY_NODES):
        builder.add_node(node_name, lambda state: state)
        builder.add_edge(node_name, END)
    builder.add_edge(START, "classify_delivery_failure")
    builder.add_conditional_edges(
        "classify_delivery_failure",
        lambda state: cast(str, state["resume_node"]),
        {name: name for name in _RECOVERY_NODES},
    )
    return builder.compile()
