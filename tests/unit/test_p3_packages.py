import shutil
from pathlib import Path

import pytest

from paperagent.knowledge.index import FtsKnowledgeIndex
from paperagent.knowledge.packages import KnowledgePackageManager


def builtin_root() -> Path:
    return Path(__file__).resolve().parents[2] / "knowledge" / "builtin"


def test_all_eleven_builtin_packages_validate_offline(tmp_path: Path) -> None:
    manager = KnowledgePackageManager()
    manifest = manager.load(builtin_root() / "manifest.yaml")
    assert len(manifest.packages) == 11
    assert all(entry.license == "CC0-1.0" for entry in manifest.packages)
    items = manager.items(builtin_root() / "manifest.yaml")
    assert len(items) == 11
    assert all(
        item.trust_level == "normative" and item.citation_policy == "never" for item in items
    )
    index = FtsKnowledgeIndex(tmp_path / "builtin.db")
    assert index.upsert(items) == 11
    assert index.search("引用 学术规范")


def test_bad_hash_is_rejected_and_atomic_install_preserves_previous(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(builtin_root(), source)
    target = tmp_path / "installed"
    target.mkdir()
    (target / "sentinel.txt").write_text("old", encoding="utf-8")
    manager = KnowledgePackageManager()
    manager.atomic_install(source, target)
    assert (target / "manifest.yaml").is_file()
    (source / "academic-writing" / "content.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        manager.atomic_install(source, target)
    assert (target / "manifest.yaml").is_file()
