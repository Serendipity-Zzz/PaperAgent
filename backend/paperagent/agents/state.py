from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paperagent.schemas.presentation import RequirementPresentationSpec
from paperagent.schemas.typography import TypographySpec

CURRENT_AGENT_STATE_SCHEMA = "1.0"
CURRENT_GRAPH_VERSION = "1.0"


def now_utc() -> datetime:
    return datetime.now(UTC)


class RequirementStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_INPUT = "needs_input"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


class DocumentType(StrEnum):
    ACADEMIC_PAPER = "academic_paper"
    EXPERIMENT_REPORT = "experiment_report"
    PROJECT_REPORT = "project_report"
    PRACTICE_REPORT = "practice_report"
    SURVEY_REPORT = "survey_report"
    OTHER = "other"


class PrimaryLanguage(StrEnum):
    ZH = "zh"
    EN = "en"
    MIXED = "mixed"


class TranslationRequirement(StrEnum):
    NONE = "none"
    ZH_TO_EN = "zh_to_en"
    EN_TO_ZH = "en_to_zh"
    BILINGUAL = "bilingual"


class LengthUnit(StrEnum):
    CHINESE_CHAR = "chinese_char"
    ENGLISH_WORD = "english_word"
    MIXED_SCORE = "mixed_score"


class OutputFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    MARKDOWN = "md"
    MARKDOWN_BUNDLE = "md_bundle"
    TYPST = "typst"
    LATEX = "latex"


class ReviewLevel(StrEnum):
    LIGHT = "light"
    STANDARD = "standard"
    STRICT = "strict"


class PrivacyMode(StrEnum):
    STANDARD = "standard"
    PRIVACY_CONTROLLED = "privacy_controlled"
    OFFLINE = "offline"


class EvidenceSource(StrEnum):
    EXPLICIT_USER = "explicit_user"
    ATTACHMENT = "attachment"
    TEMPLATE = "template"
    MEMORY = "memory"
    RULE = "rule"
    AGENT_INFERENCE = "agent_inference"


class AssumptionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AssumptionStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class QuestionPriority(StrEnum):
    BLOCKING = "blocking"
    IMPORTANT = "important"
    OPTIONAL = "optional"


class RawRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=2_000_000)
    message_ids: tuple[str, ...] = ()
    attachment_ids: tuple[str, ...] = ()


class TargetLength(BaseModel):
    value: int = Field(gt=0, le=10_000_000)
    unit: LengthUnit


class ResearchFormulation(BaseModel):
    research_topic: str | None = None
    research_objective: str | None = None
    research_questions: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    research_subject: str | None = None
    scope_and_boundaries: list[str] = Field(default_factory=list)
    variables_or_dimensions: list[str] = Field(default_factory=list)
    methodology_candidates: list[str] = Field(default_factory=list)
    data_requirements: list[str] = Field(default_factory=list)


class FieldEvidence(BaseModel):
    source_type: EvidenceSource
    source_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_confirmation: bool = False


class Assumption(BaseModel):
    assumption_id: UUID = Field(default_factory=uuid4)
    field_path: str = Field(min_length=1)
    proposed_value: object
    reason: str = Field(min_length=1)
    risk: AssumptionRisk
    status: AssumptionStatus = AssumptionStatus.PROPOSED


class OpenQuestion(BaseModel):
    question_id: UUID = Field(default_factory=uuid4)
    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    affected_fields: list[str] = Field(min_length=1)
    priority: QuestionPriority


class ConfirmedRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    requirement_id: UUID
    requirement_version: int = Field(ge=1)
    normalized_request: str
    research_formulation: ResearchFormulation
    document_type: DocumentType
    primary_language: PrimaryLanguage
    translation_required: TranslationRequirement
    target_length: TargetLength
    audience: str
    citation_style: str
    template_id: str | None = None
    presentation: RequirementPresentationSpec = Field(default_factory=RequirementPresentationSpec)
    typography: TypographySpec = Field(default_factory=TypographySpec)
    requires_literature_search: bool
    requires_experiment: bool
    requires_data_chart: bool
    requires_generated_image: bool
    output_formats: tuple[OutputFormat, ...]
    review_level: ReviewLevel
    privacy_mode: PrivacyMode
    constraints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    confirmed_at: datetime
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequirementSpec(BaseModel):
    requirement_id: UUID = Field(default_factory=uuid4)
    requirement_version: int = Field(default=1, ge=1)
    status: RequirementStatus = RequirementStatus.DRAFT
    raw_request: RawRequest
    normalized_request: str = ""
    research_formulation: ResearchFormulation = Field(default_factory=ResearchFormulation)
    confirmed_requirement: ConfirmedRequirement | None = None
    document_type: DocumentType | None = None
    primary_language: PrimaryLanguage | None = None
    translation_required: TranslationRequirement = TranslationRequirement.NONE
    target_length: TargetLength | None = None
    audience: str | None = None
    citation_style: str | None = None
    template_id: str | None = None
    presentation: RequirementPresentationSpec = Field(default_factory=RequirementPresentationSpec)
    typography: TypographySpec = Field(default_factory=TypographySpec)
    requires_literature_search: bool | None = None
    requires_experiment: bool | None = None
    requires_data_chart: bool | None = None
    requires_generated_image: bool | None = None
    output_formats: list[OutputFormat] = Field(default_factory=list)
    review_level: ReviewLevel = ReviewLevel.STANDARD
    privacy_mode: PrivacyMode = PrivacyMode.STANDARD
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        if self.status is RequirementStatus.CONFIRMED:
            if self.confirmed_requirement is None or self.confirmed_at is None:
                raise ValueError("confirmed status requires a frozen confirmed requirement")
            if self.confirmed_requirement.requirement_id != self.requirement_id:
                raise ValueError("confirmed requirement id mismatch")
            if self.confirmed_requirement.requirement_version != self.requirement_version:
                raise ValueError("confirmed requirement version mismatch")
        elif self.confirmed_requirement is not None:
            raise ValueError("only confirmed status may contain a confirmed requirement")
        if self.status is RequirementStatus.NEEDS_INPUT and not (
            self.open_questions or self.conflicts
        ):
            raise ValueError("needs_input requires an open question or conflict")
        return self

    def confirm(self, *, at: datetime | None = None) -> RequirementSpec:
        missing = [
            name
            for name, value in {
                "normalized_request": self.normalized_request,
                "document_type": self.document_type,
                "primary_language": self.primary_language,
                "target_length": self.target_length,
                "audience": self.audience,
                "citation_style": self.citation_style,
                "requires_literature_search": self.requires_literature_search,
                "requires_experiment": self.requires_experiment,
                "requires_data_chart": self.requires_data_chart,
                "requires_generated_image": self.requires_generated_image,
                "output_formats": self.output_formats,
            }.items()
            if value is None or value == "" or value == []
        ]
        if missing or self.conflicts or self.open_questions:
            raise ValueError(f"requirement cannot be confirmed; unresolved: {', '.join(missing)}")
        assert self.target_length is not None
        confirmed_at = at or now_utc()
        payload = {
            "requirement_id": str(self.requirement_id),
            "requirement_version": self.requirement_version,
            "normalized_request": self.normalized_request,
            "research_formulation": self.research_formulation.model_dump(mode="json"),
            "document_type": self.document_type,
            "primary_language": self.primary_language,
            "translation_required": self.translation_required,
            "target_length": self.target_length.model_dump(mode="json"),
            "audience": self.audience,
            "citation_style": self.citation_style,
            "template_id": self.template_id,
            "presentation": self.presentation.model_dump(mode="json"),
            "typography": self.typography.model_dump(mode="json"),
            "requires_literature_search": self.requires_literature_search,
            "requires_experiment": self.requires_experiment,
            "requires_data_chart": self.requires_data_chart,
            "requires_generated_image": self.requires_generated_image,
            "output_formats": self.output_formats,
            "review_level": self.review_level,
            "privacy_mode": self.privacy_mode,
            "constraints": self.constraints,
            "acceptance_criteria": self.acceptance_criteria,
            "confirmed_at": confirmed_at,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        snapshot = ConfirmedRequirement.model_validate(
            payload | {"content_hash": hashlib.sha256(encoded).hexdigest()}
        )
        return self.model_copy(
            update={
                "status": RequirementStatus.CONFIRMED,
                "confirmed_at": confirmed_at,
                "confirmed_requirement": snapshot,
            }
        )


class RequirementVersionHistory(BaseModel):
    requirement_id: UUID
    versions: list[RequirementSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_versions(self) -> Self:
        numbers = [version.requirement_version for version in self.versions]
        if any(version.requirement_id != self.requirement_id for version in self.versions):
            raise ValueError("requirement history contains a different requirement id")
        if numbers != sorted(set(numbers)):
            raise ValueError("requirement versions must be unique and ascending")
        active = [
            version
            for version in self.versions
            if version.status is not RequirementStatus.SUPERSEDED
        ]
        if len(active) != 1:
            raise ValueError("requirement history must have exactly one active version")
        return self


class GraphCondition(StrEnum):
    ALWAYS = "always"
    ON_SUCCESS = "on_success"
    ON_FAILURE = "on_failure"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_INPUT = "needs_input"
    REPAIR_REQUIRED = "repair_required"


class NodeDefinition(BaseModel):
    node_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_-]*$")
    agent_type: str = Field(min_length=1)
    input_keys: tuple[str, ...]
    output_keys: tuple[str, ...]
    requires_confirmed_requirement: bool = False
    max_attempts: int = Field(default=3, ge=1, le=20)


class TaskEdge(BaseModel):
    source: str
    target: str
    condition: GraphCondition = GraphCondition.ON_SUCCESS


class TaskGraph(BaseModel):
    graph_version: str = CURRENT_GRAPH_VERSION
    entry_node: str
    terminal_nodes: set[str] = Field(min_length=1)
    nodes: list[NodeDefinition] = Field(min_length=1)
    edges: list[TaskEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("task graph contains duplicate node ids")
        known = set(node_ids)
        if self.entry_node not in known or not self.terminal_nodes <= known:
            raise ValueError("entry and terminal nodes must exist")
        if any(edge.source not in known or edge.target not in known for edge in self.edges):
            raise ValueError("task graph edge references an unknown node")
        return self


class NodeRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeRun(BaseModel):
    node_id: str
    execution_key: str = Field(min_length=1)
    status: NodeRunStatus = NodeRunStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    input: dict[str, object] = Field(default_factory=dict)
    output: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InterruptKind(StrEnum):
    APPROVAL = "approval"
    USER_INPUT = "user_input"


class InterruptStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class GraphInterrupt(BaseModel):
    interrupt_id: UUID = Field(default_factory=uuid4)
    kind: InterruptKind
    node_id: str
    action: str
    scope: dict[str, object] = Field(default_factory=dict)
    prompt: str
    status: InterruptStatus = InterruptStatus.PENDING
    created_at: datetime = Field(default_factory=now_utc)
    decided_at: datetime | None = None


class GraphRunStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentState(BaseModel):
    schema_version: str = CURRENT_AGENT_STATE_SCHEMA
    project_id: str
    thread_id: str
    task_id: str
    graph: TaskGraph
    requirement_history: RequirementVersionHistory
    status: GraphRunStatus = GraphRunStatus.READY
    active_node: str | None = None
    node_runs: dict[str, NodeRun] = Field(default_factory=dict)
    pending_interrupt: GraphInterrupt | None = None
    sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def validate_runtime(self) -> Self:
        if self.active_node is not None and self.active_node not in {
            node.node_id for node in self.graph.nodes
        }:
            raise ValueError("active node is not in task graph")
        if self.status is GraphRunStatus.PAUSED and self.pending_interrupt is None:
            raise ValueError("paused graph requires a pending interrupt")
        if self.pending_interrupt and self.pending_interrupt.status is not InterruptStatus.PENDING:
            raise ValueError("pending interrupt must have pending status")
        return self

    def begin_node(self, node_id: str, execution_key: str, node_input: dict[str, object]) -> bool:
        definition = next((node for node in self.graph.nodes if node.node_id == node_id), None)
        if definition is None:
            raise KeyError(node_id)
        if definition.requires_confirmed_requirement:
            current = self.requirement_history.versions[-1]
            if current.status is not RequirementStatus.CONFIRMED:
                raise PermissionError("node requires a confirmed Requirement Spec")
        existing = self.node_runs.get(node_id)
        if (
            existing
            and existing.execution_key == execution_key
            and existing.status
            in {
                NodeRunStatus.RUNNING,
                NodeRunStatus.COMPLETED,
            }
        ):
            return False
        attempt = (existing.attempt if existing else 0) + 1
        if attempt > definition.max_attempts:
            raise RuntimeError("node retry limit exceeded")
        self.node_runs[node_id] = NodeRun(
            node_id=node_id,
            execution_key=execution_key,
            status=NodeRunStatus.RUNNING,
            attempt=attempt,
            input=node_input,
            started_at=now_utc(),
        )
        self.active_node = node_id
        self.status = GraphRunStatus.RUNNING
        self.sequence += 1
        self.updated_at = now_utc()
        return True

    def complete_node(self, node_id: str, output: dict[str, object]) -> None:
        run = self.node_runs.get(node_id)
        if run is None or run.status is not NodeRunStatus.RUNNING:
            raise ValueError("node is not running")
        definition = next(node for node in self.graph.nodes if node.node_id == node_id)
        unexpected = set(output) - set(definition.output_keys)
        if unexpected:
            raise ValueError(f"node emitted undeclared outputs: {sorted(unexpected)}")
        run.output = output
        run.status = NodeRunStatus.COMPLETED
        run.completed_at = now_utc()
        self.active_node = None
        self.sequence += 1
        self.updated_at = now_utc()

    def pause(self, interrupt: GraphInterrupt) -> None:
        if interrupt.node_id not in {node.node_id for node in self.graph.nodes}:
            raise ValueError("interrupt node is not in task graph")
        if self.pending_interrupt is not None:
            raise ValueError("another interrupt is already pending")
        self.pending_interrupt = interrupt
        self.status = GraphRunStatus.PAUSED
        self.sequence += 1
        self.updated_at = now_utc()

    def resume(self, interrupt_id: UUID, *, approved: bool) -> None:
        if self.pending_interrupt is None or self.pending_interrupt.interrupt_id != interrupt_id:
            raise KeyError("pending interrupt not found")
        decision = InterruptStatus.APPROVED if approved else InterruptStatus.REJECTED
        self.pending_interrupt.status = decision
        self.pending_interrupt.decided_at = now_utc()
        self.pending_interrupt = None
        self.status = GraphRunStatus.RUNNING if approved else GraphRunStatus.CANCELLED
        self.sequence += 1
        self.updated_at = now_utc()


class AgentStateCheckpoint(BaseModel):
    checkpoint_id: UUID = Field(default_factory=uuid4)
    schema_version: str = CURRENT_AGENT_STATE_SCHEMA
    state: AgentState
    state_hash: str = ""
    created_at: datetime = Field(default_factory=now_utc)

    @model_validator(mode="after")
    def set_or_validate_hash(self) -> Self:
        payload = self.state.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if self.state_hash and self.state_hash != digest:
            raise ValueError("checkpoint state hash mismatch")
        self.state_hash = digest
        return self


def migrate_checkpoint(payload: dict[str, object]) -> AgentStateCheckpoint:
    version = str(payload.get("schema_version", "0.1"))
    if version == CURRENT_AGENT_STATE_SCHEMA:
        return AgentStateCheckpoint.model_validate(payload)
    if version != "0.1":
        raise ValueError(f"unsupported checkpoint schema: {version}")
    state_payload = payload.get("state")
    if not isinstance(state_payload, dict):
        raise ValueError("legacy checkpoint has no structured state")
    legacy_state = dict(state_payload)
    requirement = legacy_state.pop("requirement", None)
    if not isinstance(requirement, dict):
        raise ValueError("legacy checkpoint has no structured requirement")
    requirement.setdefault("requirement_id", str(uuid4()))
    requirement.setdefault("requirement_version", 1)
    requirement_id = requirement["requirement_id"]
    legacy_state["schema_version"] = CURRENT_AGENT_STATE_SCHEMA
    legacy_state["requirement_history"] = {
        "requirement_id": requirement_id,
        "versions": [requirement],
    }
    graph = dict(legacy_state.get("graph", {}))
    graph.setdefault("graph_version", CURRENT_GRAPH_VERSION)
    legacy_state["graph"] = graph
    return AgentStateCheckpoint.model_validate(
        {
            "checkpoint_id": payload.get("checkpoint_id", str(uuid4())),
            "schema_version": CURRENT_AGENT_STATE_SCHEMA,
            "state": legacy_state,
            "created_at": payload.get("created_at", now_utc()),
        }
    )
