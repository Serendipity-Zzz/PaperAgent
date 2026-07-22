from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def discover_project_root() -> Path:
    """Resolve the source root without depending on a drive letter."""
    override = os.getenv("PAPERAGENT_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def default_data_dir(project_root: Path | None = None) -> Path:
    """Keep development data beside, never inside, the source repository."""
    root = project_root or discover_project_root()
    return root.parent / "paperagent-data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAPERAGENT_",
        extra="ignore",
        validate_default=True,
    )

    app_name: str = "PaperAgent"
    environment: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "human"] = "human"
    project_root: Path = Field(default_factory=discover_project_root)
    data_dir: Path | None = None
    uv_path: Path | None = None

    @field_validator("project_root", "data_dir", "uv_path", mode="before")
    @classmethod
    def normalize_path(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
        return value

    @property
    def resolved_data_dir(self) -> Path:
        return (self.data_dir or default_data_dir(self.project_root)).resolve()

    def ensure_data_layout(self) -> None:
        directories = (
            "memory/preferences",
            "memory/domains",
            "memory/daily",
            "memory/archive",
            "global",
            "projects",
            "global_library/vectors",
            "runtimes/envs",
            "runtimes/runs",
            "runtimes/cache",
            "models",
            "backups",
            "logs",
        )
        for relative in directories:
            (self.resolved_data_dir / relative).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
