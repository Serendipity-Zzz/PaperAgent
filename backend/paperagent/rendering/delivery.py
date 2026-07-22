from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from paperagent.agents.document_ir import (
    AssetRequirementManifest as AssetRequirementManifest,
)
from paperagent.agents.document_ir import (
    RequiredAsset as RequiredAsset,
)
from paperagent.agents.document_ir import (
    RequiredAssetKind as RequiredAssetKind,
)


class DocumentAction(StrEnum):
    CREATE = "create"
    REVISE_CONTENT = "revise_content"
    REVISE_PRESENTATION = "revise_presentation"
    RESTYLE = "restyle"
    CONVERT_FORMAT = "convert_format"
    INSPECT = "inspect"
    DOWNLOAD = "download"
    RERUN_EXPERIMENT = "rerun_experiment"


class DocumentFormat(StrEnum):
    MARKDOWN = "md"
    MARKDOWN_BUNDLE = "md_bundle"
    DOCX = "docx"
    PDF = "pdf"


class DocumentActionIntent(BaseModel):
    action: DocumentAction
    target_formats: list[DocumentFormat] = Field(default_factory=list)
    target_reference: str | None = None
    preserve_content: bool = True
    preserve_assets: bool = True
    rerun_experiment: bool = False
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_conversion_invariants(self) -> DocumentActionIntent:
        if self.action is DocumentAction.CONVERT_FORMAT:
            self.preserve_content = True
            self.preserve_assets = True
            self.rerun_experiment = False
            if not self.target_formats:
                raise ValueError("format conversion requires at least one target format")
        return self


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    ASSETS_PENDING = "assets_pending"
    CANONICAL_READY = "canonical_ready"
    RENDERING = "rendering"
    REPAIR_REQUIRED = "repair_required"
    DELIVERED = "delivered"
    REJECTED = "rejected"


class DeliveryStatus(StrEnum):
    PLANNED = "planned"
    RENDERING = "rendering"
    VALIDATING = "validating"
    DELIVERED = "delivered"
    REPAIR_REQUIRED = "repair_required"
    REJECTED = "rejected"


class AssetBarrierStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    MISSING = "missing"
    INVALID = "invalid"
    AMBIGUOUS = "ambiguous"


class AssetCandidate(BaseModel):
    artifact_id: str
    filename: str | None = None
    source_run_id: str | None = None
    sha256: str | None = None


class AmbiguousAssetGroup(BaseModel):
    logical_id: str
    candidates: list[AssetCandidate] = Field(min_length=2)


class AssetBinding(BaseModel):
    logical_id: str
    artifact_id: str
    evidence: str


class AssetBarrierResult(BaseModel):
    status: AssetBarrierStatus
    expected_count: int = Field(ge=0)
    bound_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    pending: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    ambiguous: list[AmbiguousAssetGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_and_state_are_consistent(self) -> AssetBarrierResult:
        if self.ready_count > self.bound_count or self.bound_count > self.expected_count:
            raise ValueError("asset barrier counts are inconsistent")
        has_problem = bool(self.pending or self.missing or self.invalid or self.ambiguous)
        if self.status is AssetBarrierStatus.READY and (
            has_problem or self.ready_count != self.expected_count
        ):
            raise ValueError("ready asset barrier requires every expected asset")
        return self

    @property
    def ready(self) -> bool:
        return self.status is AssetBarrierStatus.READY


class DeliveryIssueCategory(StrEnum):
    MISSING_REVISION = "missing_revision"
    AMBIGUOUS_REVISION = "ambiguous_revision"
    STRUCTURE_ERROR = "structure_error"
    MISSING_ASSET = "missing_asset"
    PENDING_ASSET = "pending_asset"
    AMBIGUOUS_ASSET = "ambiguous_asset"
    INVALID_ASSET = "invalid_asset"
    DERIVATIVE_FAILED = "derivative_failed"
    COMPILE_ERROR = "compile_error"
    LAYOUT_ERROR = "layout_error"
    VALIDATION_ERROR = "validation_error"
    TRANSIENT_PROVIDER = "transient_provider"


class DeliveryIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DeliveryValidationIssue(BaseModel):
    category: DeliveryIssueCategory
    severity: DeliveryIssueSeverity = DeliveryIssueSeverity.ERROR
    document_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)
    artifact_id: str | None = None
    block_id: UUID | None = None
    message: str
    repair_node: str | None = None


class DeliveryValidationResult(BaseModel):
    passed: bool
    issues: list[DeliveryValidationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def passed_result_has_no_errors(self) -> DeliveryValidationResult:
        errors = [
            item for item in self.issues if item.severity is DeliveryIssueSeverity.ERROR
        ]
        if self.passed and errors:
            raise ValueError("a passed delivery validation cannot contain errors")
        if not self.passed and not errors:
            raise ValueError("a failed delivery validation requires an error issue")
        return self
