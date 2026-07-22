from pathlib import Path

from paperagent.core.config import Settings, default_data_dir


def test_default_data_directory_is_sibling_of_project() -> None:
    root = Path("C:/工作区/含 空格/paperagent")
    assert default_data_dir(root) == root.parent / "paperagent-data"


def test_custom_data_directory_is_resolved(tmp_path: Path) -> None:
    target = tmp_path / "数据 目录"
    settings = Settings(project_root=tmp_path, data_dir=target, environment="test")
    settings.ensure_data_layout()
    assert settings.resolved_data_dir == target.resolve()
    assert (target / "runtimes" / "envs").is_dir()
    assert (target / "global_library" / "vectors").is_dir()


def test_no_drive_letter_is_required(tmp_path: Path) -> None:
    settings = Settings(project_root=tmp_path / "repo", environment="test")
    assert settings.resolved_data_dir == (tmp_path / "paperagent-data").resolve()
