from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from paperagent.onboarding import FirstRunService
from paperagent.recovery import RecoveryService, SideEffectAction, SideEffectStore


def test_warm_recovery_center_under_500ms(tmp_path: Path) -> None:
    store = SideEffectStore(tmp_path / "recovery.db")
    for index in range(1000):
        store.intent("p", SideEffectAction.FILE, str(index), "write")
    started = time.perf_counter()
    center = RecoveryService(store).center("p")
    elapsed = time.perf_counter() - started
    assert len(center["pending"]) == 1000  # type: ignore[arg-type]
    assert elapsed < 0.5


def test_first_run_warm_status_under_200ms(tmp_path: Path) -> None:
    service = FirstRunService(tmp_path)
    service.complete(privacy_mode="standard", providers_configured=False, skipped=[])
    service.status()
    started = time.perf_counter()
    assert service.status()["completed"] is True
    assert time.perf_counter() - started < 0.2


def test_sqlite_keyword_lookup_100k_under_one_second(tmp_path: Path) -> None:
    database = tmp_path / "fts.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIRTUAL TABLE chunks USING fts5(content)")
        connection.executemany(
            "INSERT INTO chunks(content) VALUES (?)",
            ((f"研究资料 {index} recovery",) for index in range(100_000)),
        )
        connection.commit()
        started = time.perf_counter()
        count = connection.execute(
            "SELECT count(*) FROM chunks WHERE chunks MATCH 'recovery'"
        ).fetchone()[0]
    assert count == 100_000
    assert time.perf_counter() - started < 1.0
