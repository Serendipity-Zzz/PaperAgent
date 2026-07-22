from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(UTC)


class GlobalBase(DeclarativeBase):
    pass


class ProjectBase(DeclarativeBase):
    pass


class SchemaVersion(GlobalBase):
    __tablename__ = "schema_versions"
    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ProjectIndex(GlobalBase):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class MemoryRecord(GlobalBase):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ProviderRecord(GlobalBase):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    modality: Mapped[str] = mapped_column(String(32), default="text", index=True)
    protocol: Mapped[str] = mapped_column(String(64), default="openai_compatible")
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capabilities_json: Mapped[str] = mapped_column(Text, default="[]")
    extra_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    health_detail: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    secret_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ActiveProviderBindingRecord(GlobalBase):
    __tablename__ = "active_provider_bindings"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    modality: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AppSetting(GlobalBase):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)


class ProjectSchemaVersion(ProjectBase):
    __tablename__ = "schema_versions"
    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SessionRecord(ProjectBase):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    draft: Mapped[str] = mapped_column(Text, default="")
    last_read_sequence: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, index=True
    )
    messages: Mapped[list[MessageRecord]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class MessageRecord(ProjectBase):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    parent_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    branch_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="final", index=True)
    superseded_by_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )
    session: Mapped[SessionRecord] = relationship(back_populates="messages")


class TaskRecord(ProjectBase):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    current_phase: Mapped[str] = mapped_column(String(64), default="queued")
    checkpoint_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    context_snapshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_policy_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    resource_request_json: Mapped[str] = mapped_column(Text, default="{}")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_output_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recovery_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ApprovalRecord(ProjectBase):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SteeringRecord(ProjectBase):
    __tablename__ = "steering_decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    trigger_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    replacement_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventRecord(ProjectBase):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    internal_payload_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class FileRecord(ProjectBase):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ArtifactRecord(ProjectBase):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    producer_tool: Mapped[str | None] = mapped_column(String(128), nullable=True)
    producer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    derived_from_artifact_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    source_artifact_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    environment_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    delivery_status: Mapped[str] = mapped_column(
        String(32), default="not_applicable", server_default="not_applicable", index=True
    )
    renderer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lineage_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class DocumentRecord(ProjectBase):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    latest_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class DocumentRevisionRecord(ProjectBase):
    __tablename__ = "document_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    canonical_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    structure_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    style_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_set_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    citation_set_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    presentation_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    numbering_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_manifest_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    image_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expected_asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (
        UniqueConstraint("document_id", "revision_number", name="uq_document_revision_number"),
    )


class DocumentRevisionAssetRecord(ProjectBase):
    __tablename__ = "document_revision_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    block_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    derivative_for: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logical_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    binding_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ready", nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "artifact_id",
            "role",
            "block_id",
            name="uq_document_revision_asset",
        ),
    )


class DocumentDeliveryRecord(ProjectBase):
    __tablename__ = "document_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("document_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    renderer: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    options_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    validation_report_json: Mapped[str] = mapped_column(Text, default="{}")
    figure_artifact_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    source_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    __table_args__ = (
        UniqueConstraint(
            "revision_id",
            "format",
            "options_hash",
            name="uq_document_delivery_request",
        ),
    )


class ArtifactLinkRecord(ProjectBase):
    __tablename__ = "artifact_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    relation: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), default="")
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ExecutionRecordRow(ProjectBase):
    __tablename__ = "execution_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    environment_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command_json: Mapped[str] = mapped_column(Text, nullable=False)
    cwd_relative: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    side_effects_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_messages_session_created", MessageRecord.session_id, MessageRecord.created_at)
UniqueConstraint(MessageRecord.session_id, MessageRecord.sequence, name="uq_message_sequence")
Index("ix_events_task_sequence", EventRecord.task_id, EventRecord.sequence)
UniqueConstraint(EventRecord.task_id, EventRecord.run_sequence, name="uq_event_run_sequence")
