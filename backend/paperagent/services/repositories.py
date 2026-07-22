from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import EventRecord, MessageRecord, ProjectIndex, SessionRecord


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w-]+", "-", name.strip().lower(), flags=re.UNICODE).strip("-")
    return slug[:80] or "project"


class ProjectRepository:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases

    def create(self, name: str) -> ProjectIndex:
        project_id = str(uuid4())
        base_slug = slugify(name)
        with self.databases.global_session() as session:
            slug = base_slug
            suffix = 1
            while session.scalar(select(ProjectIndex).where(ProjectIndex.slug == slug)):
                suffix += 1
                slug = f"{base_slug}-{suffix}"
            row = ProjectIndex(
                id=project_id,
                name=name.strip(),
                slug=slug,
                relative_path=f"projects/{project_id}",
                status="active",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
        self.databases.project_engine(project_id).dispose()
        return row

    def list(self, *, include_archived: bool = False) -> list[ProjectIndex]:
        with self.databases.global_session() as session:
            query = (
                select(ProjectIndex)
                .where(ProjectIndex.status != "deleted")
                .order_by(ProjectIndex.updated_at.desc())
            )
            if not include_archived:
                query = query.where(ProjectIndex.archived.is_(False))
            return list(session.scalars(query))

    def get(self, project_id: str) -> ProjectIndex:
        with self.databases.global_session() as session:
            row = session.get(ProjectIndex, project_id)
            if row is None or row.status == "deleted":
                raise KeyError(project_id)
            session.expunge(row)
            return row

    def update(
        self, project_id: str, *, name: str | None = None, description: str | None = None
    ) -> ProjectIndex:
        with self.databases.global_session() as session:
            row = session.get(ProjectIndex, project_id)
            if row is None or row.status == "deleted":
                raise KeyError(project_id)
            if name is not None:
                row.name = name.strip()
            if description is not None:
                row.description = description
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return row

    def soft_delete(self, project_id: str) -> ProjectIndex:
        with self.databases.global_session() as session:
            row = session.get(ProjectIndex, project_id)
            if row is None:
                raise KeyError(project_id)
            row.status = "deleted"
            row.archived = True
            row.deleted_at = datetime.now(UTC)
            row.updated_at = row.deleted_at
            session.commit()
            session.refresh(row)
            return row

    def archive(self, project_id: str, *, archived: bool) -> ProjectIndex:
        with self.databases.global_session() as session:
            row = session.get(ProjectIndex, project_id)
            if row is None:
                raise KeyError(project_id)
            row.archived = archived
            row.status = "archived" if archived else "active"
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return row


class ConversationRepository:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases

    def create_session(self, project_id: str, title: str) -> SessionRecord:
        with self.databases.project_session(project_id) as session:
            row = SessionRecord(id=str(uuid4()), title=title.strip(), status="active")
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_sessions(self, project_id: str) -> list[SessionRecord]:
        with self.databases.project_session(project_id) as session:
            query = (
                select(SessionRecord)
                .where(SessionRecord.status == "active", SessionRecord.archived.is_(False))
                .order_by(SessionRecord.updated_at.desc())
            )
            return list(session.scalars(query))

    def get_session(self, project_id: str, session_id: str) -> SessionRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(SessionRecord, session_id)
            if row is None or row.status == "deleted":
                raise KeyError(session_id)
            session.expunge(row)
            return row

    def update_session(
        self,
        project_id: str,
        session_id: str,
        *,
        title: str | None = None,
        draft: str | None = None,
        archived: bool | None = None,
        last_read_sequence: int | None = None,
    ) -> SessionRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(SessionRecord, session_id)
            if row is None or row.status == "deleted":
                raise KeyError(session_id)
            if title is not None:
                row.title = title.strip()
            if draft is not None:
                row.draft = draft
            if archived is not None:
                row.archived = archived
                row.status = "archived" if archived else "active"
            if last_read_sequence is not None:
                row.last_read_sequence = max(row.last_read_sequence, last_read_sequence)
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return row

    def soft_delete_session(self, project_id: str, session_id: str) -> SessionRecord:
        with self.databases.project_session(project_id) as session:
            row = session.get(SessionRecord, session_id)
            if row is None:
                raise KeyError(session_id)
            row.status = "deleted"
            row.archived = True
            row.deleted_at = datetime.now(UTC)
            row.updated_at = row.deleted_at
            session.commit()
            session.refresh(row)
            return row

    def add_message(
        self,
        project_id: str,
        session_id: str,
        role: str,
        content: str,
        *,
        run_id: str | None = None,
        parent_message_id: str | None = None,
        branch_id: str | None = None,
        status: str = "final",
    ) -> MessageRecord:
        with self.databases.project_session(project_id) as session:
            parent = session.get(SessionRecord, session_id)
            if parent is None:
                raise KeyError(session_id)
            sequence = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(MessageRecord.sequence), 0)).where(
                            MessageRecord.session_id == session_id
                        )
                    )
                    or 0
                )
                + 1
            )
            row = MessageRecord(
                id=str(uuid4()),
                session_id=session_id,
                role=role,
                content=content,
                sequence=sequence,
                run_id=run_id,
                parent_message_id=parent_message_id,
                branch_id=branch_id,
                status=status,
            )
            parent.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list_messages(
        self,
        project_id: str,
        session_id: str,
        *,
        after: int = 0,
        before: int | None = None,
        limit: int = 200,
    ) -> list[MessageRecord]:
        with self.databases.project_session(project_id) as session:
            page_size = min(max(limit, 1), 500)
            if before is not None:
                query = (
                    select(MessageRecord)
                    .where(
                        MessageRecord.session_id == session_id,
                        MessageRecord.sequence < before,
                    )
                    .order_by(MessageRecord.sequence.desc())
                    .limit(page_size)
                )
                return list(reversed(list(session.scalars(query))))
            query = (
                select(MessageRecord)
                .where(MessageRecord.session_id == session_id, MessageRecord.sequence > after)
                .order_by(MessageRecord.sequence)
                .limit(page_size)
            )
            return list(session.scalars(query))


class EventRepository:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases

    def append(self, project_id: str, event_type: str, payload: dict[str, object]) -> EventRecord:
        with self.databases.project_session(project_id) as session:
            row = EventRecord(
                event_id=str(uuid4()),
                type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def after(self, project_id: str, sequence: int) -> list[EventRecord]:
        with self.databases.project_session(project_id) as session:
            query = (
                select(EventRecord)
                .where(EventRecord.sequence > sequence)
                .order_by(EventRecord.sequence)
                .limit(500)
            )
            return list(session.scalars(query))
