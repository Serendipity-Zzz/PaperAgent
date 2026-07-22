from pathlib import Path

import pytest

from paperagent.services.backup import PortableDataBackup


def test_file_first_backup_moves_to_another_root_without_indexes(tmp_path: Path) -> None:
    source = tmp_path / "source-data"
    (source / "memory/topics/字体").mkdir(parents=True)
    (source / "memory/topics/字体/宋体.md").write_text("宋体", encoding="utf-8")
    (source / "projects/paper-1/conversations").mkdir(parents=True)
    (source / "projects/paper-1/conversations/thread.jsonl").write_text(
        '{"sequence":1}\n', encoding="utf-8"
    )
    (source / "projects/paper-1/indexes").mkdir(parents=True)
    (source / "projects/paper-1/indexes/knowledge.db").write_bytes(b"derived")
    archive = tmp_path / "portable.paperagent.zip"
    manifest = PortableDataBackup().export(source, archive, include_indexes=False)
    assert not any("indexes" in path for path in manifest.files)
    restored = tmp_path / "different-drive-root"
    PortableDataBackup().restore(archive, restored)
    assert (restored / "memory/topics/字体/宋体.md").read_text(encoding="utf-8") == "宋体"
    assert not (restored / "projects/paper-1/indexes").exists()


def test_restore_refuses_to_overwrite_existing_data(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "MEMORY.md").write_text("memory", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    PortableDataBackup().export(source, archive)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="empty"):
        PortableDataBackup().restore(archive, destination)
    assert (destination / "existing.txt").read_text(encoding="utf-8") == "keep"
