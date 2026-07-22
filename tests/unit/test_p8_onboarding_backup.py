from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from paperagent.onboarding import FirstRunService
from paperagent.services.backup import BackupService


def database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('中文路径')")
        connection.commit()


def test_first_run_can_skip_every_optional_tool(tmp_path: Path) -> None:
    service = FirstRunService(tmp_path / "数据 目录")
    status = service.complete(
        privacy_mode="offline", providers_configured=False, skipped=["provider", "texlive"]
    )
    assert status["privacy_mode"] == "offline"
    assert service.status()["completed"] is True
    assert service.disk()["writable"] is True
    assert {tool.name for tool in service.detect()} >= {"uv", "typst", "xelatex"}


def test_dependency_install_plan_requires_confirmation_and_safe_destination(
    tmp_path: Path,
) -> None:
    service = FirstRunService(tmp_path / "data")
    typst = service.install_plan("typst")
    assert typst.method == "winget" and typst.source == "Typst.Typst"
    texlive = service.install_plan("xelatex", tmp_path / "runtimes" / "texlive")
    assert texlive.method == "texlive-official"
    assert texlive.estimated_bytes >= 7 * 1024**3
    assert "selected_scheme scheme-full" in service._texlive_profile(tmp_path / "texlive")
    with pytest.raises(PermissionError):
        service.start_install("typst", confirmed=False)
    with pytest.raises(ValueError, match="drive root"):
        service.install_plan("xelatex", Path(Path.cwd().anchor))


def test_daily_backup_retention_export_and_drill(tmp_path: Path) -> None:
    source = tmp_path / "project.db"
    database(source)
    service = BackupService(tmp_path / "backups")
    daily = service.create_daily(source, keep=7)
    assert service.create_daily(source).backup_id == daily.backup_id
    manual = [service.create(source) for _ in range(3)]
    assert len(service.prune(keep=1, reason="manual")) == 2
    assert service.recovery_drill(daily.backup_id, tmp_path / "drill")["status"] == "passed"
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.md").write_text("ok", encoding="utf-8")
    archive = service.export_project(project, tmp_path / "exports" / "project.paperagent.zip")
    assert archive.exists()
    assert manual[-1].backup_id in {item.backup_id for item in service.list_backups()}
