from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar
from uuid import uuid4


class SideEffectAction(StrEnum):
    API = "api"
    FILE = "file"
    EXPERIMENT = "experiment"
    RENDER = "render"
    INSTALL = "install"
    DELETE = "delete"


class SideEffectState(StrEnum):
    INTENT = "intent"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SideEffectRecord:
    id: str
    project_id: str
    action: SideEffectAction
    state: SideEffectState
    idempotency_key: str
    request_id: str | None
    description: str
    scope: dict[str, object]
    result: dict[str, object]
    paid: bool
    requires_approval: bool
    estimated_cost: float | None
    graph_version: int
    checkpoint: str | None
    created_at: str
    updated_at: str

    @property
    def automatic_retry_safe(self) -> bool:
        return (
            self.state in {SideEffectState.INTENT, SideEffectState.FAILED}
            and not self.paid
            and self.action not in {SideEffectAction.EXPERIMENT, SideEffectAction.DELETE}
        )


class InjectedFault(RuntimeError):
    pass


class FaultInjector:
    """Deterministic opt-in crash hook used by recovery tests and diagnostics."""

    def __init__(self, points: set[str] | None = None, *, seed: int = 0, probability: float = 1.0):
        self.points = points or set()
        self.random = random.Random(seed)
        self.probability = probability
        self.triggered: list[str] = []

    def hit(self, point: str) -> None:
        if point in self.points and self.random.random() <= self.probability:
            self.triggered.append(point)
            raise InjectedFault(point)


class SideEffectStore:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS side_effects (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, action TEXT NOT NULL,
                state TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_id TEXT,
                description TEXT NOT NULL, scope_json TEXT NOT NULL, result_json TEXT NOT NULL,
                paid INTEGER NOT NULL, requires_approval INTEGER NOT NULL,
                estimated_cost REAL, graph_version INTEGER NOT NULL, checkpoint TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(project_id, idempotency_key))"""
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def intent(
        self,
        project_id: str,
        action: SideEffectAction,
        idempotency_key: str,
        description: str,
        *,
        scope: dict[str, object] | None = None,
        paid: bool = False,
        requires_approval: bool = False,
        estimated_cost: float | None = None,
        request_id: str | None = None,
        graph_version: int = 1,
        injector: FaultInjector | None = None,
    ) -> SideEffectRecord:
        now = datetime.now(UTC).isoformat()
        record_id = str(uuid4())
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM side_effects WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._row(existing)
            connection.execute(
                "INSERT INTO side_effects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    project_id,
                    action.value,
                    SideEffectState.INTENT.value,
                    idempotency_key,
                    request_id,
                    description,
                    json.dumps(scope or {}, ensure_ascii=False),
                    "{}",
                    int(paid),
                    int(requires_approval),
                    estimated_cost,
                    graph_version,
                    None,
                    now,
                    now,
                ),
            )
            connection.commit()
        if injector:
            injector.hit("after_intent")
        return self.get(record_id)

    def transition(
        self,
        record_id: str,
        state: SideEffectState,
        *,
        result: dict[str, object] | None = None,
        checkpoint: str | None = None,
        request_id: str | None = None,
        injector: FaultInjector | None = None,
    ) -> SideEffectRecord:
        if injector:
            injector.hit("before_result")
        current = self.get(record_id)
        allowed = {
            SideEffectState.INTENT: {
                SideEffectState.RUNNING,
                SideEffectState.SUCCEEDED,
                SideEffectState.FAILED,
                SideEffectState.UNKNOWN,
                SideEffectState.SKIPPED,
            },
            SideEffectState.RUNNING: {
                SideEffectState.SUCCEEDED,
                SideEffectState.FAILED,
                SideEffectState.UNKNOWN,
                SideEffectState.SKIPPED,
            },
            SideEffectState.FAILED: {SideEffectState.RUNNING, SideEffectState.SKIPPED},
            SideEffectState.UNKNOWN: {
                SideEffectState.RUNNING,
                SideEffectState.SKIPPED,
                SideEffectState.SUCCEEDED,
                SideEffectState.FAILED,
            },
        }
        if state not in allowed.get(current.state, set()):
            raise ValueError(f"Invalid side-effect transition: {current.state} -> {state}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE side_effects SET state=?, result_json=?, checkpoint=?, "
                "request_id=COALESCE(?,request_id), updated_at=? WHERE id=?",
                (
                    state.value,
                    json.dumps(result or {}, ensure_ascii=False),
                    checkpoint,
                    request_id,
                    datetime.now(UTC).isoformat(),
                    record_id,
                ),
            )
            connection.commit()
        return self.get(record_id)

    def get(self, record_id: str) -> SideEffectRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM side_effects WHERE id=?", (record_id,)
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._row(row)

    def get_by_idempotency_key(
        self, project_id: str, idempotency_key: str
    ) -> SideEffectRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM side_effects WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
        return self._row(row) if row is not None else None

    def reconcile(
        self,
        record_id: str,
        probe: Callable[[SideEffectRecord], tuple[SideEffectState, dict[str, object]]],
    ) -> SideEffectRecord:
        """Resolve an unknown/running side effect before any caller can replay it."""

        current = self.get(record_id)
        if current.state is SideEffectState.SUCCEEDED:
            return current
        if current.state not in {SideEffectState.UNKNOWN, SideEffectState.RUNNING}:
            raise ValueError("only unknown or running side effects require reconciliation")
        state, result = probe(current)
        if state not in {
            SideEffectState.SUCCEEDED,
            SideEffectState.FAILED,
            SideEffectState.UNKNOWN,
        }:
            raise ValueError("reconciliation must produce succeeded, failed or unknown")
        if state is SideEffectState.UNKNOWN:
            return current
        return self.transition(record_id, state, result=result, checkpoint="reconciled")

    def list(self, project_id: str | None = None) -> list[SideEffectRecord]:
        query = "SELECT * FROM side_effects"
        params: tuple[str, ...] = ()
        if project_id:
            query += " WHERE project_id=?"
            params = (project_id,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> SideEffectRecord:
        return SideEffectRecord(
            id=row["id"],
            project_id=row["project_id"],
            action=SideEffectAction(row["action"]),
            state=SideEffectState(row["state"]),
            idempotency_key=row["idempotency_key"],
            request_id=row["request_id"],
            description=row["description"],
            scope=json.loads(row["scope_json"]),
            result=json.loads(row["result_json"]),
            paid=bool(row["paid"]),
            requires_approval=bool(row["requires_approval"]),
            estimated_cost=row["estimated_cost"],
            graph_version=row["graph_version"],
            checkpoint=row["checkpoint"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class RecoveryService:
    def __init__(self, store: SideEffectStore, *, current_graph_version: int = 1) -> None:
        self.store = store
        self.current_graph_version = current_graph_version

    def center(
        self,
        project_id: str | None = None,
        *,
        trace_id: str | None = None,
        task_completed: bool = False,
    ) -> dict[str, object]:
        records = self.store.list(project_id)
        if trace_id is not None:
            trace_prefix = f"{trace_id}:"
            records = [
                record for record in records if record.idempotency_key.startswith(trace_prefix)
            ]
        resolved_failures = [
            r for r in records if task_completed and r.state is SideEffectState.FAILED
        ]
        pending = [
            r
            for r in records
            if r.state
            in {
                SideEffectState.INTENT,
                SideEffectState.RUNNING,
                SideEffectState.UNKNOWN,
                SideEffectState.FAILED,
            }
            and r not in resolved_failures
        ]
        return {
            "completed": sum(r.state is SideEffectState.SUCCEEDED for r in records),
            "running": [asdict(r) for r in pending if r.state is SideEffectState.RUNNING],
            "unknown": [asdict(r) for r in pending if r.state is SideEffectState.UNKNOWN],
            "pending": [self._view(r) for r in pending],
            "resolved_failures": [self._view(r) for r in resolved_failures],
            "stopped_at": pending[0].description if pending else None,
            "requires_attention": bool(pending),
            "scope": "task" if trace_id is not None else "project",
        }

    def decide(self, record_id: str, decision: str) -> SideEffectRecord:
        record = self.store.get(record_id)
        if decision == "skip":
            return self.store.transition(
                record_id, SideEffectState.SKIPPED, result={"decision": "user_skip"}
            )
        if decision != "retry":
            raise ValueError("Decision must be retry or skip")
        if record.action in {SideEffectAction.EXPERIMENT, SideEffectAction.DELETE} or record.paid:
            # This method is only callable from an explicit user action; never from startup.
            return self.store.transition(
                record_id, SideEffectState.RUNNING, result={"decision": "user_retry"}
            )
        return self.store.transition(
            record_id, SideEffectState.RUNNING, result={"decision": "retry"}
        )

    def _view(self, record: SideEffectRecord) -> dict[str, object]:
        item = asdict(record)
        item["automatic_retry_safe"] = record.automatic_retry_safe
        item["graph_compatible"] = record.graph_version <= self.current_graph_version
        item["next_action"] = (
            "人工决定重试或跳过"
            if record.paid
            or record.action in {SideEffectAction.EXPERIMENT, SideEffectAction.DELETE}
            else "可安全继续"
        )
        return item


T = TypeVar("T")


class ProviderCallGuard:
    """Records a provider call before dispatch and treats ambiguous transport loss as unknown."""

    def __init__(self, store: SideEffectStore) -> None:
        self.store = store

    def call(
        self,
        project_id: str,
        operation: Callable[[str], T],
        *,
        idempotency_key: str,
        description: str,
        estimated_cost: float | None,
        injector: FaultInjector | None = None,
    ) -> tuple[SideEffectRecord, T | None]:
        request_id = str(uuid4())
        record = self.store.intent(
            project_id,
            SideEffectAction.API,
            idempotency_key,
            description,
            paid=True,
            requires_approval=True,
            estimated_cost=estimated_cost,
            request_id=request_id,
            injector=injector,
        )
        if record.state is SideEffectState.SUCCEEDED:
            return record, None
        self.store.transition(record.id, SideEffectState.RUNNING, request_id=request_id)
        try:
            value = operation(request_id)
            return self.store.transition(
                record.id,
                SideEffectState.SUCCEEDED,
                result={"request_id": request_id},
                checkpoint="provider_result",
                injector=injector,
            ), value
        except (TimeoutError, ConnectionError) as exc:
            unknown = self.store.transition(
                record.id,
                SideEffectState.UNKNOWN,
                result={
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "may_have_completed": True,
                },
                request_id=request_id,
            )
            return unknown, None
        except Exception as exc:
            failed = self.store.transition(
                record.id,
                SideEffectState.FAILED,
                result={"error": type(exc).__name__, "message": str(exc)},
                request_id=request_id,
            )
            return failed, None
