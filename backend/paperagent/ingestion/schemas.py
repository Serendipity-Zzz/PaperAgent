from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TrustLevel(StrEnum):
    VERIFIED = "verified"
    AUTHORITATIVE = "authoritative"
    USER_PROVIDED = "user_provided"
    GENERATED = "generated"
    UNTRUSTED = "untrusted"


class CitationPolicy(StrEnum):
    CITABLE = "citable"
    VERIFY_FIRST = "verify_first"
    INTERNAL_ONLY = "internal_only"
    PROCESS_ONLY = "process_only"
    NEVER = "never"


class Locator(BaseModel):
    page: int | None = Field(default=None, ge=1)
    paragraph: int | None = Field(default=None, ge=0)
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    message_id: str | None = None
    json_path: str | None = None
    bbox: tuple[float, float, float, float] | None = None


class Chunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_id: UUID
    text: str
    kind: str = "text"
    locator: Locator
    trust: TrustLevel = TrustLevel.USER_PROVIDED
    citation_policy: CitationPolicy = CitationPolicy.VERIFY_FIRST
    instruction_trust: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class SourceDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    media_type: str
    sha256: str
    parser: str
    chunks: list[Chunk] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class ImportReport(BaseModel):
    source: SourceDocument
    warnings: list[str] = Field(default_factory=list)
    duplicate_of: UUID | None = None
    cancelled: bool = False
