from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from paperagent.context.models import Sensitivity
from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class MemoryScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    CONTESTED = "contested"
    ARCHIVED = "archived"


class MemoryEntry(StrictModel):
    schema_version: str = SCHEMA_VERSION
    memory_id: UUID = Field(default_factory=uuid4)
    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    subject: str = Field(min_length=1, max_length=255)
    scope: MemoryScope
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    kind: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=200_000)
    source_type: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=1_024)
    source_locator: str | None = Field(default=None, max_length=2_048)
    confidence: float = Field(default=1, ge=0, le=1)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_verified_at: datetime | None = None
    valid_until: datetime | None = None
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    allowed_providers: list[str] = Field(default_factory=list)
    supersedes_id: UUID | None = None
    conflict_ids: list[UUID] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=1, max_length=255)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_entry(self) -> Self:
        if self.scope is MemoryScope.PROJECT and not self.project_id:
            raise ValueError("project memory requires project_id")
        if self.scope is MemoryScope.GLOBAL and self.project_id:
            raise ValueError("global memory cannot contain project_id")
        digest = stable_json_hash(self.content)
        if self.content_hash is not None and self.content_hash != digest:
            raise ValueError("memory content hash mismatch")
        self.content_hash = digest
        return self


class MemoryWriteResult(StrictModel):
    entry: MemoryEntry
    relative_path: str
    manifest_path: str
    created: bool


class MemoryMigrationReport(StrictModel):
    scanned: int = Field(ge=0)
    migrated: int = Field(ge=0)
    skipped: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
