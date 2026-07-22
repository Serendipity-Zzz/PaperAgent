from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import insert

from paperagent.core.config import Settings
from paperagent.db.manager import DatabaseManager
from paperagent.db.models import MessageRecord
from paperagent.services.repositories import (
    ConversationRepository,
    ProjectRepository,
)


def test_latest_window_from_10k_messages_under_500ms(tmp_path: Path) -> None:
    databases = DatabaseManager(
        Settings(project_root=tmp_path / "repo", data_dir=tmp_path / "data", environment="test")
    )
    databases.initialize_global()
    project = ProjectRepository(databases).create("ten-thousand-messages")
    conversations = ConversationRepository(databases)
    conversation = conversations.create_session(project.id, "long history")
    now = datetime.now(UTC)
    with databases.project_session(project.id) as session:
        session.execute(
            insert(MessageRecord),
            [
                {
                    "id": str(uuid4()),
                    "session_id": conversation.id,
                    "role": "user" if sequence % 2 else "assistant",
                    "content": f"message-{sequence}",
                    "sequence": sequence,
                    "status": "final",
                    "created_at": now,
                    "updated_at": now,
                }
                for sequence in range(1, 10_001)
            ],
        )
        session.commit()
    started = time.perf_counter()
    latest = conversations.list_messages(
        project.id, conversation.id, before=2_147_483_647, limit=200
    )
    elapsed = time.perf_counter() - started
    assert len(latest) == 200
    assert (latest[0].sequence, latest[-1].sequence) == (9_801, 10_000)
    assert elapsed < 0.5
