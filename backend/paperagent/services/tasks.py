from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from sqlalchemy import func, select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import ApprovalRecord, EventRecord, TaskRecord
from paperagent.schemas import TaskStatus

ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SUPERSEDED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.SUPERSEDED,
    },
    TaskStatus.PAUSED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED},
    TaskStatus.CANCELLED: set(),
    TaskStatus.SUPERSEDED: set(),
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TaskService:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases
        self._event_lock = RLock()

    def create(
        self, project_id: str, kind: str, idempotency_key: str, payload: dict[str, object]
    ) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            existing = (
                session.query(TaskRecord)
                .filter(TaskRecord.idempotency_key == idempotency_key)
                .one_or_none()
            )
            if existing:
                return existing
            public_payload = dict(payload)
            public_payload.pop("provider_snapshot", None)
            row = TaskRecord(
                id=str(uuid4()),
                kind=kind,
                status=TaskStatus.PENDING.value,
                idempotency_key=idempotency_key,
                payload_json=json.dumps(public_payload, ensure_ascii=False),
                conversation_id=(
                    str(payload.get("session_id")) if payload.get("session_id") else None
                ),
                parent_task_id=(
                    str(payload.get("parent_task_id")) if payload.get("parent_task_id") else None
                ),
                provider_snapshot_json=json.dumps(
                    payload.get("provider_snapshot", {}), ensure_ascii=False
                ),
                context_snapshot_ref=(
                    str(payload.get("context_snapshot_ref"))
                    if payload.get("context_snapshot_ref")
                    else None
                ),
                tool_policy_snapshot_json=json.dumps(
                    payload.get("tool_policy_snapshot", {}), ensure_ascii=False
                ),
            )
            session.add(row)
            session.flush()
            self._event(session, row.id, "task.created", {"status": row.status})
            session.commit()
            session.refresh(row)
            return row

    def transition(
        self,
        project_id: str,
        task_id: str,
        target: TaskStatus,
        *,
        expected_version: int | None = None,
    ) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            if expected_version is not None and row.version != expected_version:
                raise ValueError(
                    f"Run version changed: expected {expected_version}, found {row.version}"
                )
            current = TaskStatus(row.status)
            if target not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"Illegal task transition: {current.value} -> {target.value}")
            row.status = target.value
            row.updated_at = datetime.now(UTC)
            row.version += 1
            if target is TaskStatus.RUNNING and row.started_at is None:
                row.started_at = row.updated_at
            if target in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.SUPERSEDED,
            }:
                row.finished_at = row.updated_at
                row.lease_expires_at = None
                row.worker_id = None
            elif target in {TaskStatus.PAUSED, TaskStatus.WAITING_APPROVAL}:
                row.lease_expires_at = None
                row.worker_id = None
            self._event(
                session,
                row.id,
                "run.status_changed",
                {"from": current.value, "to": target.value, "version": row.version},
            )
            session.commit()
            session.refresh(row)
            return row

    def get(self, project_id: str, task_id: str) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            session.expunge(row)
            return row

    def consume_pending_guidance(self, project_id: str, task_id: str) -> list[dict[str, object]]:
        """Atomically remove steering guidance so a safe boundary applies it once."""
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            payload = json.loads(row.payload_json)
            raw = payload.pop("pending_guidance", [])
            guidance = [item for item in raw if isinstance(item, dict)]
            if not guidance:
                return []
            row.payload_json = json.dumps(payload, ensure_ascii=False)
            row.version += 1
            row.updated_at = datetime.now(UTC)
            self._event(
                session,
                row.id,
                "steering.guidance_consumed",
                {"count": len(guidance), "version": row.version},
            )
            session.commit()
            return guidance

    def list(self, project_id: str) -> list[TaskRecord]:
        with self.databases.project_session(project_id) as session:
            rows = list(session.query(TaskRecord).order_by(TaskRecord.created_at.desc()).all())
            for row in rows:
                session.expunge(row)
            return rows

    def update_payload(
        self, project_id: str, task_id: str, payload: dict[str, object]
    ) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            row.payload_json = json.dumps(payload, ensure_ascii=False, default=str)
            row.updated_at = datetime.now(UTC)
            row.version += 1
            self._event(session, task_id, "run.payload_updated", {"version": row.version})
            session.commit()
            session.refresh(row)
            return row

    def claim(
        self,
        project_id: str,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 30,
    ) -> TaskRecord:
        """Atomically claim a queued/paused run or an expired lease."""
        now = datetime.now(UTC)
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            lease_expires_at = _utc(row.lease_expires_at)
            lease_live = lease_expires_at is not None and lease_expires_at > now
            if row.worker_id and row.worker_id != worker_id and lease_live:
                raise ValueError("Run is leased by another worker")
            if TaskStatus(row.status) in {
                TaskStatus.COMPLETED,
                TaskStatus.CANCELLED,
                TaskStatus.SUPERSEDED,
            }:
                raise ValueError("Terminal run cannot be claimed")
            previous_worker = row.worker_id
            row.worker_id = worker_id
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
            row.status = TaskStatus.RUNNING.value
            row.current_phase = "starting" if row.current_phase == "queued" else row.current_phase
            row.started_at = row.started_at or now
            if previous_worker != worker_id:
                row.attempt += 1
            row.version += 1
            self._event(
                session,
                task_id,
                "run.claimed",
                {"worker_id": worker_id, "attempt": row.attempt, "version": row.version},
            )
            session.commit()
            session.refresh(row)
            return row

    def heartbeat(
        self, project_id: str, task_id: str, worker_id: str, *, lease_seconds: int = 30
    ) -> TaskRecord:
        now = datetime.now(UTC)
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.worker_id != worker_id or TaskStatus(row.status) is not TaskStatus.RUNNING:
                raise ValueError("Run is not owned by this worker")
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=max(1, lease_seconds))
            session.commit()
            session.refresh(row)
            return row

    def update_phase(
        self,
        project_id: str,
        task_id: str,
        phase: str,
        *,
        checkpoint_ref: str | None = None,
    ) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            previous = row.current_phase
            row.current_phase = phase[:64]
            row.checkpoint_ref = checkpoint_ref or row.checkpoint_ref
            row.updated_at = datetime.now(UTC)
            row.version += 1
            self._event(
                session,
                task_id,
                "run.phase_changed",
                {"from": previous, "to": row.current_phase, "version": row.version},
            )
            session.commit()
            session.refresh(row)
            return row

    def set_resource_request(
        self, project_id: str, task_id: str, request: dict[str, object]
    ) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            row.resource_request_json = json.dumps(request, ensure_ascii=False, default=str)
            row.updated_at = datetime.now(UTC)
            row.version += 1
            self._event(session, task_id, "run.resources_requested", request)
            session.commit()
            session.refresh(row)
            return row

    def set_checkpoint(self, project_id: str, task_id: str, checkpoint_ref: str) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            row.checkpoint_ref = checkpoint_ref
            row.version += 1
            row.updated_at = datetime.now(UTC)
            self._event(
                session,
                task_id,
                "run.checkpoint_selected",
                {"checkpoint_ref": checkpoint_ref},
            )
            session.commit()
            session.refresh(row)
            return row

    def mark_read(self, project_id: str, task_id: str) -> TaskRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError(task_id)
            row.read_at = datetime.now(UTC)
            row.version += 1
            session.commit()
            session.refresh(row)
            return row

    def append_event(
        self,
        project_id: str,
        task_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        event_id: str | None = None,
        internal_payload_ref: str | None = None,
    ) -> EventRecord:
        with self._event_lock, self.databases.project_session(project_id) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if event_id:
                existing = session.scalar(
                    select(EventRecord).where(EventRecord.event_id == event_id)
                )
                if existing is not None:
                    session.expunge(existing)
                    return existing
            row = self._event(
                session,
                task_id,
                event_type,
                payload,
                event_id=event_id,
                internal_payload_ref=internal_payload_ref,
            )
            task = session.get(TaskRecord, task_id)
            if task is not None:
                now = datetime.now(UTC)
                task.first_output_at = task.first_output_at or now
                task.updated_at = now
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

    def events_after(
        self, project_id: str, task_id: str, sequence: int, *, limit: int = 500
    ) -> builtins.list[EventRecord]:
        with self.databases.project_session(project_id) as session:
            rows = list(
                session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.task_id == task_id,
                        EventRecord.run_sequence > sequence,
                    )
                    .order_by(EventRecord.run_sequence)
                    .limit(min(max(limit, 1), 1000))
                )
            )
            for row in rows:
                session.expunge(row)
            return rows

    def reconcile_orphans(
        self, project_id: str, *, force: bool = False
    ) -> builtins.list[TaskRecord]:
        """Pause unleased running runs and record a durable recovery decision."""
        now = datetime.now(UTC)
        recovered: builtins.list[TaskRecord] = []
        with self.databases.project_session(project_id) as session:
            rows = list(
                session.scalars(
                    select(TaskRecord).where(TaskRecord.status == TaskStatus.RUNNING.value)
                )
            )
            for row in rows:
                lease_expires_at = _utc(row.lease_expires_at)
                if not force and lease_expires_at is not None and lease_expires_at > now:
                    continue
                row.status = TaskStatus.PAUSED.value
                row.worker_id = None
                row.lease_expires_at = None
                row.recovery_strategy = (
                    "resume_checkpoint" if row.checkpoint_ref else "confirm_before_replay"
                )
                row.current_phase = "recovery_required"
                row.version += 1
                self._event(
                    session,
                    row.id,
                    "run.recovery_required",
                    {
                        "strategy": row.recovery_strategy,
                        "checkpoint_ref": row.checkpoint_ref,
                    },
                )
                recovered.append(row)
            session.commit()
            for row in recovered:
                session.refresh(row)
                session.expunge(row)
        return recovered

    def request_approval(
        self, project_id: str, task_id: str, action: str, scope: dict[str, object]
    ) -> ApprovalRecord:
        with self.databases.project_session(project_id) as session:
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError(task_id)
            if TaskStatus(task.status) is TaskStatus.RUNNING:
                task.status = TaskStatus.WAITING_APPROVAL.value
            approval = ApprovalRecord(
                id=str(uuid4()),
                task_id=task_id,
                action=action,
                status="pending",
                scope_json=json.dumps(scope, ensure_ascii=False),
            )
            session.add(approval)
            self._event(session, task_id, "approval.requested", {"approval_id": approval.id})
            session.commit()
            session.refresh(approval)
            return approval

    def decide_approval(
        self, project_id: str, approval_id: str, *, approved: bool
    ) -> ApprovalRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(ApprovalRecord, approval_id)
            if row is None:
                raise KeyError(approval_id)
            decision = "approved" if approved else "rejected"
            if row.status != "pending":
                if row.status != decision:
                    raise ValueError("Approval already has a different decision")
                return row
            row.status = decision
            row.decided_at = datetime.now(UTC)
            self._event(
                session,
                row.task_id,
                "approval.decided",
                {"approval_id": row.id, "status": decision},
            )
            session.commit()
            session.refresh(row)
            return row

    @staticmethod
    def _event(
        session: object,
        task_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        event_id: str | None = None,
        internal_payload_ref: str | None = None,
    ) -> EventRecord:
        sequence = (
            int(
                session.scalar(  # type: ignore[attr-defined]
                    select(func.coalesce(func.max(EventRecord.run_sequence), 0)).where(
                        EventRecord.task_id == task_id
                    )
                )
                or 0
            )
            + 1
        )
        row = EventRecord(
            event_id=event_id or str(uuid4()),
            task_id=task_id,
            run_sequence=sequence,
            type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            internal_payload_ref=internal_payload_ref,
        )
        session.add(row)  # type: ignore[attr-defined]
        return row
