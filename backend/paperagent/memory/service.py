from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select

from paperagent.db.manager import DatabaseManager
from paperagent.db.models import MemoryRecord

FORBIDDEN_KINDS = {"api_key", "credential", "raw_project_source"}


class MemoryService:
    def __init__(self, databases: DatabaseManager) -> None:
        self.databases = databases

    def remember(
        self,
        *,
        scope: str,
        kind: str,
        content: str,
        source: str,
        project_id: str | None = None,
        explicit: bool = False,
    ) -> MemoryRecord:
        if kind in FORBIDDEN_KINDS:
            raise ValueError("Sensitive or raw content cannot become memory")
        if scope == "long_term" and not explicit:
            raise PermissionError("Long-term memory requires explicit user intent")
        if scope == "project" and not project_id:
            raise ValueError("Project memory requires project id")
        with self.databases.global_session() as session:
            row = MemoryRecord(
                id=str(uuid4()),
                scope=scope,
                project_id=project_id,
                kind=kind,
                content=content,
                source=source,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def list(self, *, scope: str, project_id: str | None = None) -> list[MemoryRecord]:
        with self.databases.global_session() as session:
            query = select(MemoryRecord).where(
                MemoryRecord.scope == scope, MemoryRecord.deleted.is_(False)
            )
            if scope == "project":
                query = query.where(MemoryRecord.project_id == project_id)
            return list(session.scalars(query.order_by(MemoryRecord.updated_at.desc())))

    def update(self, memory_id: str, content: str) -> MemoryRecord:
        with self.databases.global_session() as session:
            row = session.get(MemoryRecord, memory_id)
            if row is None or row.deleted:
                raise KeyError(memory_id)
            row.content = content
            session.commit()
            session.refresh(row)
            return row

    def clear(self, *, scope: str, confirmation: str, project_id: str | None = None) -> int:
        if confirmation != "CLEAR MEMORY":
            raise PermissionError("Memory clear requires explicit confirmation")
        rows = self.list(scope=scope, project_id=project_id)
        with self.databases.global_session() as session:
            for row in rows:
                stored = session.get(MemoryRecord, row.id)
                if stored:
                    stored.deleted = True
            session.commit()
        return len(rows)
