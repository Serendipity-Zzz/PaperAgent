from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from paperagent.schemas.common import SCHEMA_VERSION, StrictModel, stable_json_hash


class CapabilityKind(StrEnum):
    AGENT = "agent"
    TOOL = "tool"
    RENDERER = "renderer"
    PROVIDER = "provider"


class CapabilityDescriptor(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    kind: CapabilityKind
    input_types: set[str] = Field(default_factory=set)
    output_types: set[str] = Field(default_factory=set)
    tags: set[str] = Field(default_factory=set)
    side_effect: str = Field(
        default="none", pattern=r"^(none|local_write|external|paid|destructive)$"
    )
    permission_policy: str = Field(
        default="deterministic", pattern=r"^(deterministic|require_approval|deny)$"
    )
    allowed_agents: set[str] = Field(default_factory=set)
    resource_requirements: dict[str, JsonValue] = Field(default_factory=dict)
    available: bool = True
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> CapabilityDescriptor:
        if not self.available and not self.unavailable_reason:
            raise ValueError("unavailable capability requires unavailable_reason")
        if self.available and self.unavailable_reason:
            raise ValueError("available capability cannot have unavailable_reason")
        return self


class CapabilitySnapshot(StrictModel):
    schema_version: str = SCHEMA_VERSION
    descriptors: list[CapabilityDescriptor]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def freeze_hash(self) -> CapabilitySnapshot:
        identities = [(item.kind, item.name, item.version) for item in self.descriptors]
        if len(identities) != len(set(identities)):
            raise ValueError("capability snapshot contains duplicate identities")
        calculated = stable_json_hash(
            {
                "schema_version": self.schema_version,
                "descriptors": [
                    item.model_dump(mode="json")
                    for item in sorted(
                        self.descriptors,
                        key=lambda value: (value.kind.value, value.name, value.version),
                    )
                ],
            }
        )
        if self.snapshot_hash is not None and self.snapshot_hash != calculated:
            raise ValueError("capability snapshot hash does not match contents")
        self.snapshot_hash = calculated
        return self


class AuthorizationExpiry(StrEnum):
    RUN = "run"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"


class NetworkPolicy(StrEnum):
    DENY = "deny"
    DEPENDENCY_REGISTRY_ONLY = "dependency_registry_only"
    ALLOWLIST = "allowlist"
    USER_AUTHORIZED = "user_authorized"


class AuthorizationGrant(StrictModel):
    grant_id: UUID = Field(default_factory=uuid4)
    subject: str = Field(min_length=1, max_length=255)
    capabilities: set[str] = Field(min_length=1)
    write_roots: list[str] = Field(default_factory=list)
    delete_allowed: bool = False
    outside_write_allowed: bool = False
    network_policy: NetworkPolicy = NetworkPolicy.DENY
    network_allowlist: set[str] = Field(default_factory=set)
    expires: AuthorizationExpiry = AuthorizationExpiry.RUN
    action_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    issued_by: str = Field(default="user", min_length=1, max_length=64)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def enforce_non_bypassable_limits(self) -> AuthorizationGrant:
        if self.delete_allowed:
            raise ValueError("delete permission cannot be granted as a reusable authorization")
        if self.outside_write_allowed:
            raise ValueError("outside-write permission requires a one-shot approval")
        if self.network_policy is NetworkPolicy.ALLOWLIST and not self.network_allowlist:
            raise ValueError("allowlist network policy requires at least one host")
        return self

    def authorizes(self, capability: str, action_hash: str) -> bool:
        return capability in self.capabilities and self.action_hash == action_hash


class ExecutionRequest(StrictModel):
    request_id: UUID = Field(default_factory=uuid4)
    run_id: str = Field(min_length=1, max_length=64)
    capability: str = Field(default="process.execute", min_length=1, max_length=128)
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    expected_read_paths: list[str] = Field(default_factory=list)
    expected_write_paths: list[str] = Field(default_factory=list)
    expected_delete_paths: list[str] = Field(default_factory=list)
    network_hosts: set[str] = Field(default_factory=set)
    timeout_ms: int = Field(default=300_000, ge=100, le=86_400_000)
    max_output_chars: int = Field(default=2_000_000, ge=1_000, le=50_000_000)
    resource_limits: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_ambiguous_shell_requests(self) -> ExecutionRequest:
        if any("\x00" in item for item in self.argv):
            raise ValueError("execution arguments cannot contain NUL")
        executable = self.argv[0].casefold()
        if executable in {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            raise ValueError("shell interpreters require a reviewed script artifact")
        if self.expected_delete_paths:
            raise ValueError("deletion requires a one-shot approval and separate request")
        return self

    def action_hash(self) -> str:
        return stable_json_hash(
            self.model_dump(mode="json", exclude={"request_id"})
        )


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    POLICY_VIOLATION = "policy_violation"
    UNKNOWN = "unknown"


class ExecutionRecord(StrictModel):
    record_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    run_id: str
    status: ExecutionStatus
    source_artifact_id: str | None = None
    environment_ref: str | None = None
    command_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    exit_code: int | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    observed_writes: list[str] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_timing_and_exit(self) -> ExecutionRecord:
        if self.finished_at < self.started_at:
            raise ValueError("execution finished before it started")
        if self.status is ExecutionStatus.SUCCEEDED and self.exit_code != 0:
            raise ValueError("successful execution requires exit_code=0")
        return self


class ArtifactRelation(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    SOURCE = "source"
    FIGURE = "figure"
    DATA = "data"
    LOG = "log"
    PREVIEW = "preview"
    ATTACHMENT = "attachment"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class ArtifactContract(StrictModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    project_id: str
    kind: str
    mime_type: str
    original_name: str
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    producer_tool: str | None = None
    producer_version: str | None = None
    run_id: str | None = None
    source_artifact_ids: list[str] = Field(default_factory=list)
    environment_ref: str | None = None
    preview_status: ValidationStatus = ValidationStatus.PENDING
    validation_status: ValidationStatus = ValidationStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ArtifactLink(StrictModel):
    link_id: UUID = Field(default_factory=uuid4)
    artifact_id: UUID
    conversation_id: str | None = None
    message_id: str | None = None
    run_id: str | None = None
    relation: ArtifactRelation
    label: str = ""
    display_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_owner_reference(self) -> ArtifactLink:
        if not any((self.conversation_id, self.message_id, self.run_id)):
            raise ValueError("artifact link requires conversation, message or run")
        return self


class CompletionClaim(StrictModel):
    claim_id: UUID = Field(default_factory=uuid4)
    run_id: str
    statement: str = Field(min_length=1, max_length=2_000)
    artifact_ids: list[UUID] = Field(default_factory=list)
    tool_result_ids: list[UUID] = Field(default_factory=list)
    claim_type: str = Field(pattern=r"^(file|experiment|image|document)$")
