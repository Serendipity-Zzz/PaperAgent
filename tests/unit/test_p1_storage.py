from pathlib import Path

import pytest

from paperagent.storage import ProjectFileStore


def test_store_writes_unicode_file_atomically(tmp_path: Path) -> None:
    store = ProjectFileStore(tmp_path / "项目")
    result = store.write(
        category="sources",
        name="论文 数据.txt",
        content="内容".encode(),
        provenance={"origin": "user-upload"},
    )
    assert result.sha256
    assert result.size_bytes == len("内容".encode())
    assert store.resolve(result.relative_path).read_text(encoding="utf-8") == "内容"
    assert result.provenance == {"origin": "user-upload"}


@pytest.mark.parametrize("name", ["../escape.txt", "sub/file.txt", "", ".."])
def test_store_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    store = ProjectFileStore(tmp_path)
    with pytest.raises(ValueError):
        store.write(category="sources", name=name, content=b"x", provenance={})


def test_resolve_rejects_project_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ProjectFileStore(tmp_path / "project").resolve("../../outside")
