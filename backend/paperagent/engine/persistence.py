from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import EventRecord, MessageRecord, SessionRecord
from paperagent.engine.events import EngineEvent, EngineEventKind, TurnRequest
from paperagent.services.progress import DurableProgressSink
from paperagent.services.tasks import TaskService


class ConversationPersistence(Protocol):
    def save_user_message(self, request: TurnRequest) -> str: ...

    def append_event(self, event: EngineEvent) -> None: ...

    def latest_sequence(self, project_id: str, task_id: str) -> int: ...

    def events_for_request(
        self, project_id: str, task_id: str, request_id: str
    ) -> list[EngineEvent]: ...


class SqliteConversationPersistence:
    """Current transactional adapter; P5-R10 adds the file-first journal adapter."""

    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases
        self.progress = DurableProgressSink(TaskService(databases))

    def save_user_message(self, request: TurnRequest) -> str:
        with self.databases.project_session(request.project_id) as session:
            existing = session.get(MessageRecord, request.message_id)
            if existing is not None:
                if (
                    existing.session_id != request.thread_id
                    or existing.role != "user"
                    or existing.content != request.user_message
                ):
                    raise ValueError("message id already exists with different content")
                return existing.id
            parent = session.get(SessionRecord, request.thread_id)
            if parent is None:
                parent = SessionRecord(
                    id=request.thread_id,
                    title=request.user_message.strip().replace("\n", " ")[:80] or "New task",
                )
                session.add(parent)
                session.flush()
            row = MessageRecord(
                id=request.message_id,
                session_id=request.thread_id,
                role="user",
                content=request.user_message,
                sequence=int(
                    session.scalar(
                        select(func.coalesce(func.max(MessageRecord.sequence), 0)).where(
                            MessageRecord.session_id == request.thread_id
                        )
                    )
                    or 0
                )
                + 1,
            )
            session.add(row)
            session.commit()
            return row.id

    def append_event(self, event: EngineEvent) -> None:
        self.progress.emit(
            project_id=event.project_id,
            run_id=event.task_id,
            event_type=event.kind.value,
            payload={
                "trace_id": str(event.trace_id),
                "thread_id": event.thread_id,
                "engine_sequence": event.sequence,
                **event.payload,
            },
            event_id=str(event.event_id),
        )

    def latest_sequence(self, project_id: str, task_id: str) -> int:
        events = self._events(project_id, task_id)
        return max((event.sequence for event in events), default=0)

    def events_for_request(
        self, project_id: str, task_id: str, request_id: str
    ) -> list[EngineEvent]:
        return [
            event
            for event in self._events(project_id, task_id)
            if event.payload.get("request_id") == request_id
        ]

    def _events(self, project_id: str, task_id: str) -> list[EngineEvent]:
        event_types = [kind.value for kind in EngineEventKind]
        with self.databases.project_session(project_id) as session:
            rows = list(
                session.scalars(
                    select(EventRecord)
                    .where(EventRecord.task_id == task_id, EventRecord.type.in_(event_types))
                    .order_by(EventRecord.sequence)
                )
            )
        events: list[EngineEvent] = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
                events.append(
                    EngineEvent(
                        event_id=UUID(row.event_id),
                        trace_id=payload.get("trace_id", "00000000-0000-0000-0000-000000000000"),
                        project_id=project_id,
                        thread_id=str(payload.get("thread_id", "unknown")),
                        task_id=task_id,
                        sequence=int(payload.get("engine_sequence", row.run_sequence or 0)),
                        kind=EngineEventKind(row.type),
                        payload={
                            key: value
                            for key, value in payload.items()
                            if key not in {"thread_id", "engine_sequence", "trace_id"}
                        },
                        created_at=row.created_at,
                    )
                )
            except (ValidationError, json.JSONDecodeError):
                continue
        return events
