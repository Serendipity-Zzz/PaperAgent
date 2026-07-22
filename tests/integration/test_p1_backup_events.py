from pathlib import Path

from paperagent.core.config import Settings
from paperagent.db import DatabaseManager
from paperagent.services.backup import BackupService
from paperagent.services.repositories import EventRepository, ProjectRepository


def test_online_backup_verify_and_restore(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    ProjectRepository(databases).create("before backup")
    backups = BackupService(settings.resolved_data_dir / "backups")
    manifest = backups.create(databases.global_path)
    assert backups.verify(manifest.backup_id) == manifest
    restored = tmp_path / "restored.db"
    backups.restore(manifest.backup_id, restored)
    assert restored.exists()


def test_event_sequence_resume_is_strictly_after_cursor(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path, data_dir=tmp_path / "data", environment="test")
    databases = DatabaseManager(settings)
    databases.initialize_global()
    project = ProjectRepository(databases).create("events")
    repository = EventRepository(databases)
    first = repository.append(project.id, "first", {"n": 1})
    second = repository.append(project.id, "second", {"n": 2})
    resumed = repository.after(project.id, first.sequence)
    assert [event.event_id for event in resumed] == [second.event_id]
