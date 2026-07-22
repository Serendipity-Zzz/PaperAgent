from pathlib import Path

import pytest

from paperagent.memory import (
    FileMemoryRepository,
    MemoryCandidate,
    MemoryConsolidator,
    MemoryEntry,
    MemoryExtractionService,
    MemoryQuery,
    MemoryRetriever,
    MemoryScope,
    MemoryStatus,
)


def test_extraction_advances_cursor_only_after_successful_writes(tmp_path: Path) -> None:
    repository = FileMemoryRepository(tmp_path)
    service = MemoryExtractionService(repository)
    stored = service.persist_candidates(
        [
            MemoryCandidate(
                topic="typography",
                subject="正文中文字体",
                kind="preference",
                content="中文正文使用宋体",
                source_type="user_message",
                source_id="thread-1:8",
                confidence=1,
            )
        ],
        scope=MemoryScope.PROJECT,
        project_id="paper-1",
        thread_id="thread-1",
        sequence=8,
    )
    assert stored[0].status is MemoryStatus.CANDIDATE
    cursor = tmp_path / "projects/paper-1/context/thread-1-memory-cursor.json"
    assert '"sequence": 8' in cursor.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="secret"):
        service.persist_candidates(
            [
                MemoryCandidate(
                    topic="privacy",
                    subject="bad",
                    kind="preference",
                    content="api_key=" + "sk-" + "this-must-never-be-stored",
                    source_type="user_message",
                    source_id="thread-1:9",
                    confidence=1,
                )
            ],
            scope=MemoryScope.PROJECT,
            project_id="paper-1",
            thread_id="thread-1",
            sequence=9,
        )
    assert '"sequence": 8' in cursor.read_text(encoding="utf-8")


def test_manifest_first_retrieval_limits_topics_and_provider_scope(tmp_path: Path) -> None:
    repository = FileMemoryRepository(tmp_path)
    for index, (topic, subject, content) in enumerate(
        [
            ("typography", "中文字体", "宋体正文和黑体标题"),
            ("writing", "摘要风格", "摘要保持简洁"),
            ("privacy", "资料上传", "敏感资料禁止外发"),
        ]
    ):
        repository.write(
            MemoryEntry(
                topic=topic,
                subject=subject,
                scope=MemoryScope.PROJECT,
                project_id="paper-1",
                kind="preference",
                content=content,
                source_type="user_message",
                source_id=f"thread-1:{index}",
                status=MemoryStatus.CONFIRMED,
                allowed_providers=["online-a"],
                idempotency_key=f"memory-{index}",
            )
        )
    matches = MemoryRetriever(repository).retrieve(
        MemoryQuery(
            text="中文字体宋体",
            scope=MemoryScope.PROJECT,
            project_id="paper-1",
            provider_id="online-a",
        )
    )
    assert matches and matches[0].entry.topic == "typography"
    assert (
        MemoryRetriever(repository).retrieve(
            MemoryQuery(
                text="中文字体宋体",
                scope=MemoryScope.PROJECT,
                project_id="paper-1",
                provider_id="online-b",
            )
        )
        == []
    )


def test_consolidation_marks_duplicates_and_conflicts_without_deleting_history(
    tmp_path: Path,
) -> None:
    repository = FileMemoryRepository(tmp_path)
    for index, content in enumerate(("宋体", "宋体", "仿宋")):
        repository.write(
            MemoryEntry(
                topic="typography",
                subject="正文中文字体",
                scope=MemoryScope.PROJECT,
                project_id="paper-1",
                kind="preference",
                content=content,
                source_type="user_message",
                source_id=f"thread-1:{index}",
                status=MemoryStatus.CONFIRMED,
                idempotency_key=f"font-{index}",
            )
        )
    result = MemoryConsolidator(repository).consolidate(
        MemoryScope.PROJECT, "paper-1"
    )
    assert len(result.duplicate_ids) == 1
    assert len(result.contested_ids) == 2
    entries = [
        entry for entry, _path in repository.list(MemoryScope.PROJECT, "paper-1")
    ]
    assert len(entries) == 3
    assert {entry.status for entry in entries} == {
        MemoryStatus.SUPERSEDED,
        MemoryStatus.CONTESTED,
    }
