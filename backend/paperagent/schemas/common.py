from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

SCHEMA_VERSION = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


def stable_json_hash(value: BaseModel | JsonValue) -> str:
    """Return a byte-stable hash for contracts, checkpoints and trace references."""

    def canonicalize(item: object) -> JsonValue:
        if isinstance(item, BaseModel):
            return canonicalize(item.model_dump(mode="python"))
        if isinstance(item, dict):
            return {str(key): canonicalize(child) for key, child in item.items()}
        if isinstance(item, (set, frozenset)):
            children = [canonicalize(child) for child in item]
            return sorted(
                children,
                key=lambda child: json.dumps(child, ensure_ascii=False, sort_keys=True),
            )
        if isinstance(item, (list, tuple)):
            return [canonicalize(child) for child in item]
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, StrEnum):
            return item.value
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError(f"value is not JSON serializable: {type(item).__name__}")

    payload = canonicalize(value)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class ArtifactKind(StrEnum):
    SOURCE = "source"
    PREVIEW = "preview"
    DOCUMENT = "document"
    IMAGE = "image"
    DATASET = "dataset"
    LOG = "log"


class AuditFields(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = SCHEMA_VERSION


class Artifact(AuditFields):
    kind: ArtifactKind
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    provenance: dict[str, str] = Field(default_factory=dict)


class ErrorDetail(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    message: str
    retryable: bool = False
    context: dict[str, str] = Field(default_factory=dict)


class ErrorResponse(StrictModel):
    error: ErrorDetail
    trace_id: UUID = Field(default_factory=uuid4)
    schema_version: str = SCHEMA_VERSION


class Page[T](StrictModel):
    items: list[T]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(gt=0, le=500)
    schema_version: str = SCHEMA_VERSION
