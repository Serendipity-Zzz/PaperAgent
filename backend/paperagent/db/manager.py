from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from sqlalchemy import Engine, event, select
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session

from paperagent.core.config import Settings
from paperagent.db.migrations import upgrade_database
from paperagent.db.models import (
    ProjectSchemaVersion,
    SchemaVersion,
)

CURRENT_SCHEMA_VERSION = 1


def create_sqlite_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa_create_engine(
        f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 5}
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    return engine


class DatabaseManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.global_path = settings.resolved_data_dir / "global" / "app.db"
        self.global_engine = create_sqlite_engine(self.global_path)
        self._project_init_lock = RLock()
        self._initialized_projects: set[str] = set()

    def initialize_global(self) -> None:
        self.settings.ensure_data_layout()
        upgrade_database(self.global_path, kind="global")
        with Session(self.global_engine) as session:
            row = session.get(SchemaVersion, "global")
            if row is None:
                session.add(SchemaVersion(component="global", version=CURRENT_SCHEMA_VERSION))
            elif row.version > CURRENT_SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this application")
            session.commit()

    def project_root(self, project_id: str) -> Path:
        if not project_id or any(char not in "0123456789abcdef-" for char in project_id.lower()):
            raise ValueError("Invalid project id")
        return self.settings.resolved_data_dir / "projects" / project_id

    def project_engine(self, project_id: str, *, initialize: bool = True) -> Engine:
        engine = create_sqlite_engine(self.project_root(project_id) / "project.db")
        if initialize and project_id not in self._initialized_projects:
            with self._project_init_lock:
                if project_id not in self._initialized_projects:
                    upgrade_database(self.project_root(project_id) / "project.db", kind="project")
                    with Session(engine) as session:
                        row = session.get(ProjectSchemaVersion, "project")
                        if row is None:
                            session.add(
                                ProjectSchemaVersion(
                                    component="project", version=CURRENT_SCHEMA_VERSION
                                )
                            )
                        session.commit()
                    self._initialized_projects.add(project_id)
        return engine

    @contextmanager
    def global_session(self) -> Iterator[Session]:
        with Session(self.global_engine) as session:
            yield session

    @contextmanager
    def project_session(self, project_id: str) -> Iterator[Session]:
        engine = self.project_engine(project_id)
        try:
            with Session(engine) as session:
                yield session
        finally:
            engine.dispose()

    def schema_version(self) -> int:
        with Session(self.global_engine) as session:
            return session.scalar(select(SchemaVersion.version)) or 0
