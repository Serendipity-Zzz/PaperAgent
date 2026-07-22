from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class PreviewStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    PARTIAL = "partial"
    READY = "ready"
    FAILED = "failed"


class PreviewFidelity(StrEnum):
    NATIVE = "native"
    RENDERED = "rendered"
    STRUCTURED = "structured"
    METADATA = "metadata"


class PreviewAnchor(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_file_id: str
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    format: str
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    sheet: str | None = None
    cell_range: str | None = None
    slide: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    message_id: str | None = None
    json_path: str | None = None
    quote: str | None = None

    @model_validator(mode="after")
    def validate_format_locator(self) -> PreviewAnchor:
        located = any(
            value is not None
            for value in (
                self.page,
                self.sheet,
                self.slide,
                self.line_start,
                self.message_id,
                self.json_path,
            )
        )
        if not located:
            raise ValueError("Preview anchor requires a format-specific locator")
        if self.bbox and not self.page:
            raise ValueError("bbox requires page")
        if self.cell_range and not self.sheet:
            raise ValueError("cell range requires sheet")
        return self

    def valid_for_hash(self, current_hash: str) -> bool:
        return self.source_hash == current_hash


class PreviewPart(BaseModel):
    index: int = Field(ge=0)
    kind: str
    label: str
    payload: dict[str, object]
    anchor: PreviewAnchor | None = None


class PreviewArtifact(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_file_id: str
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_name: str
    media_type: str
    status: PreviewStatus
    fidelity: PreviewFidelity
    renderer: str
    renderer_version: str
    cache_key: str
    capabilities: list[str] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)
    part_count: int = Field(default=0, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Annotation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: str
    artifact_id: UUID
    anchor: PreviewAnchor
    body: str = Field(min_length=1)
    status: str = Field(default="open", pattern=r"^(open|resolved|orphaned)$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
