from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config

from paperagent.core.config import discover_project_root


def upgrade_database(path: Path, *, kind: str) -> None:
    if kind not in {"global", "project"}:
        raise ValueError("Unknown database kind")
    root = discover_project_root()
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    config.cmd_opts = Namespace(x=[f"kind={kind}"])
    command.upgrade(config, "head")
