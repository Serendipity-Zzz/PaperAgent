from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from paperagent.ingestion.schemas import Locator


class KnowledgeScope(StrEnum):
    BUILTIN = "builtin"
    GLOBAL = "global"
    PROJECT = "project"
    SESSION = "session"
    DYNAMIC = "dynamic"


class TrustLevel(StrEnum):
    NORMATIVE = "normative"
    VERIFIED = "verified"
    USER_CONFIRMED = "user_confirmed"
    UNVERIFIED = "unverified"
    GENERATED = "generated"


class Confidentiality(StrEnum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class CitationPolicy(StrEnum):
    SCHOLARLY = "scholarly"
    INTERNAL_ONLY = "internal_only"
    PROCESS_ONLY = "process_only"
    NEVER = "never"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class KnowledgeItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    collection_id: str
    scope: KnowledgeScope
    project_id: str | None = None
    content_type: str
    title: str
    content: str
    language: str = Field(pattern=r"^(zh|en|mixed)$")
    source_kind: str
    source_uri: str | None = None
    source_file_id: str | None = None
    author_or_owner: str | None = None
    version: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    imported_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    license: str | None = None
    confidentiality: Confidentiality = Confidentiality.PERSONAL
    trust_level: TrustLevel = TrustLevel.UNVERIFIED
    citation_policy: CitationPolicy = CitationPolicy.INTERNAL_ONLY
    instruction_trust: bool = False
    parent_id: UUID | None = None
    locator: Locator
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tags: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.PENDING

    @field_validator("instruction_trust")
    @classmethod
    def imported_content_never_becomes_instruction(cls, value: bool) -> bool:
        if value:
            raise ValueError("Imported knowledge cannot be trusted as system instruction")
        return False

    @model_validator(mode="after")
    def validate_scope_and_trust(self) -> KnowledgeItem:
        if self.scope is KnowledgeScope.PROJECT and not self.project_id:
            raise ValueError("Project knowledge requires project_id")
        if self.source_kind in {"system_generated", "model_generated"} and self.trust_level in {
            TrustLevel.NORMATIVE,
            TrustLevel.VERIFIED,
        }:
            raise ValueError("Generated content cannot become normative or verified")
        if self.citation_policy is CitationPolicy.SCHOLARLY and not self.source_uri:
            raise ValueError("Scholarly evidence requires a source URI")
        return self
