from __future__ import annotations

import sqlite3
from pathlib import Path

from paperagent.onboarding import FirstRunService
from paperagent.recovery import ProviderCallGuard, RecoveryService, SideEffectState, SideEffectStore
from paperagent.services.backup import BackupService


def test_first_run_crash_paid_unknown_decision_and_backup_drill(tmp_path: Path) -> None:
    data = tmp_path / "本地 数据"
    onboarding = FirstRunService(data)
    onboarding.complete(
        privacy_mode="privacy-controlled", providers_configured=True, skipped=["texlive"]
    )
    store = SideEffectStore(data / "global" / "recovery.db")
    record, result = ProviderCallGuard(store).call(
        "project-a",
        lambda _request_id: (_ for _ in ()).throw(ConnectionError("response lost")),
        idempotency_key="llm-draft-1",
        description="生成论文草稿",
        estimated_cost=0.05,
    )
    assert result is None and record.state is SideEffectState.UNKNOWN
    restarted = RecoveryService(SideEffectStore(data / "global" / "recovery.db"))
    assert restarted.center("project-a")["requires_attention"] is True
    restarted.decide(record.id, "skip")
    assert store.get(record.id).state is SideEffectState.SKIPPED
    database = data / "global" / "app.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state(value TEXT)")
        connection.execute("INSERT INTO state VALUES ('safe')")
    backups = BackupService(data / "backups")
    manifest = backups.create_daily(database)
    assert backups.recovery_drill(manifest.backup_id, data / "drill")["status"] == "passed"
