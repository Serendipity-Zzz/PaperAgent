from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from paperagent import __version__
from paperagent.agents.change_intent import ChangeIntent, ChangeIntentAgent, ChangeScope
from paperagent.agents.document_ir import DocumentIR
from paperagent.agents.outline import OutlineDesignerAgent
from paperagent.agents.requirements import (
    RequirementCandidate,
    RequirementUnderstandingAgent,
    RequirementValidator,
    plan_preview,
)
from paperagent.agents.state import (
    EvidenceSource,
    FieldEvidence,
    RawRequest,
    RequirementSpec,
    TaskGraph,
)
from paperagent.artifacts import ArtifactIntegrityError, ArtifactService, CompletionClaimValidator
from paperagent.core.config import Settings, get_settings
from paperagent.db.manager import DatabaseManager
from paperagent.db.models import (
    ApprovalRecord,
    AppSetting,
    ArtifactRecord,
    DocumentRevisionRecord,
    EventRecord,
    FileRecord,
)
from paperagent.engine import (
    AgentLoop,
    AgentLoopRequest,
    AgentLoopResult,
    ConversationEngine,
    LangGraphLifecycleAdapter,
    ResumeRequest,
    SqliteConversationPersistence,
    TurnRequest,
)
from paperagent.execution.tool_suite import ExecutionToolSuite
from paperagent.ingestion.classification import DocumentClassifier
from paperagent.ingestion.parsers import default_registry
from paperagent.knowledge.index import MODEL_MANIFESTS, EmbeddingModelStore
from paperagent.knowledge.models import Confidentiality, KnowledgeScope
from paperagent.knowledge.service import ProjectKnowledgeService, report_to_items
from paperagent.memory import MemoryService
from paperagent.onboarding import FirstRunService
from paperagent.orchestration import compile_dynamic_interactive_graph
from paperagent.preview import Annotation, PreviewAnchor, PreviewService
from paperagent.preview.docx_pages import DocxPagePreviewService
from paperagent.prompts import PromptSelectionContext, default_prompt_compiler
from paperagent.providers import (
    Capability,
    ChatMessage,
    ChatRequest,
    ModelProvider,
    ProviderConfig,
    ProviderHealth,
    ProviderModality,
)
from paperagent.providers.adapters import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
)
from paperagent.providers.health import classify_provider_error
from paperagent.providers.registry import ProviderRegistry
from paperagent.providers.routing import ProviderRouter
from paperagent.recovery import RecoveryService, SideEffectStore
from paperagent.rendering import (
    DocumentRevisionStore,
    MissingFontError,
    RevisionOperation,
    RevisionResolver,
    RevisionWorkflow,
    TargetedTypographyService,
    TargetResolver,
)
from paperagent.rendering.artifacts import ArtifactVersion
from paperagent.rendering.changes import VisualDiffReport
from paperagent.rendering.presentation_view import RenderPresentationViewModel
from paperagent.rendering.renderers import MarkdownRenderer
from paperagent.schemas import TaskStatus, extract_typography
from paperagent.schemas.presentation import PresentationPatchOperation
from paperagent.security.credentials import CredentialStore
from paperagent.security.session_token import LocalSessionTokens
from paperagent.services.backup import BackupService
from paperagent.services.progress import DurableProgressSink
from paperagent.services.repositories import (
    ConversationRepository,
    EventRepository,
    ProjectRepository,
)
from paperagent.services.resources import ProcessLedger, ResourceLimits, ResourceRequest
from paperagent.services.run_scheduler import DurableRunScheduler
from paperagent.services.tasks import TaskService
from paperagent.steering import (
    DeterministicSteeringRules,
    SteeringContext,
    SteeringDecisionStore,
    SteeringImpactAgent,
    SteeringPlanValidator,
)
from paperagent.storage import ProjectFileStore
from paperagent.tools import (
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolResultStatus,
    ToolResultStore,
)
from paperagent.tools.adapters import CallableToolAdapter
from paperagent.tools.builtins import builtin_tool_specs
from paperagent.visuals import OpenAIImageProvider, SeedreamImageProvider
from paperagent.visuals.service import ImageRequest
from paperagent.workspace import ImpactLevel, SteeringRelationship

CURRENT_INTERACTIVE_TOOL_NAMES = (
    "knowledge.search",
    "file.read",
    "artifact.lookup",
    *ExecutionToolSuite.TOOL_NAMES,
)


def _verified_artifact_refs(result: AgentLoopResult) -> list[str]:
    refs: list[str] = []
    for message in result.messages:
        if message.role != "tool":
            continue
        try:
            tool_result = ToolResult.model_validate_json(message.content)
        except ValueError:
            continue
        if tool_result.status is ToolResultStatus.SUCCESS:
            refs.extend(tool_result.artifact_refs)
    return list(dict.fromkeys(refs))


def _restore_rendered_markdown_source(value: str) -> tuple[str | None, str]:
    """Recover the source block stored by MarkdownRenderer for style-only rerenders."""
    title: str | None = None
    body = value
    if value.startswith("---"):
        parts = value.split("---", 2)
        if len(parts) == 3:
            header, body = parts[1], parts[2]
            title_match = re.search(r'(?m)^title:\s*"([^"]+)"\s*$', header)
            if title_match:
                title = title_match.group(1)
    body = body.lstrip()
    body = re.sub(r"^##\s+正文\s*\r?\n+", "", body, count=1)
    body = re.sub(r"\\([#*_+>\-])", r"\1", body)
    return title, body.rstrip()


class SPAStaticFiles(StaticFiles):
    """Serve the frontend entry point for durable client-side workspace routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        eligible = scope.get("method") in {"GET", "HEAD"} and not path.lstrip("/").startswith(
            "api/"
        )
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code == 404 and eligible:
                return await super().get_response("index.html", scope)
            raise
        if response.status_code == 404 and eligible:
            return await super().get_response("index.html", scope)
        # Windows' MIME registry commonly classifies ES module workers as
        # text/plain. Browsers then reject PDF.js' dynamic worker import even
        # though the asset exists. Keep the correction local to frontend static
        # delivery instead of mutating process-wide MIME mappings.
        if path.casefold().endswith(".mjs"):
            response.headers["content-type"] = "text/javascript; charset=utf-8"
        return response


class NamedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=20_000)


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    draft: str | None = Field(default=None, max_length=2_000_000)
    archived: bool | None = None
    last_read_sequence: int | None = Field(default=None, ge=0)


class MessageCreate(BaseModel):
    role: str = Field(pattern=r"^(user|assistant|system|tool)$")
    content: str = Field(min_length=1, max_length=2_000_000)
    run_id: str | None = Field(default=None, max_length=64)
    parent_message_id: str | None = Field(default=None, max_length=64)
    branch_id: str | None = Field(default=None, max_length=64)


class TaskCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)
    payload: dict[str, object] = Field(default_factory=dict)


class TaskTransition(BaseModel):
    status: TaskStatus


class ApprovalCreate(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    scope: dict[str, object] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    approved: bool


class ProviderSave(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=255)
    modality: ProviderModality = ProviderModality.TEXT
    protocol: str = Field(default="openai_compatible", max_length=64)
    provider_type: str
    base_url: str
    model: str
    api_key: str | None = None
    capabilities: set[Capability] = Field(default_factory=lambda: {Capability.CHAT})
    extra: dict[str, object] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


class ProviderActivate(BaseModel):
    scope: str = Field(default="global", pattern=r"^(global|project)$")
    scope_id: str | None = Field(default=None, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)


class ProviderTestRequest(BaseModel):
    confirmation: str = Field(pattern=r"^TEST PROVIDER$")


class ImageGenerateCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    provider_id: str | None = Field(default=None, max_length=64)
    width: int = Field(default=1024, ge=256, le=4096)
    height: int = Field(default=1024, ge=256, le=4096)
    approved: bool = False


class AgentTurnCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    provider_id: str | None = None
    approved: bool = False
    task_id: str | None = Field(default=None, min_length=1, max_length=255)
    resume_checkpoint: bool = False


class AgentJobCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    provider_id: str | None = None
    approved: bool = False
    idempotency_key: str = Field(min_length=1, max_length=255)
    resource_request: ResourceRequest | None = None
    resume_checkpoint: bool = False


class SteeringCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    message_id: str | None = Field(default=None, max_length=64)
    provider_id: str | None = None
    confirmed: bool = False
    decision_id: str | None = Field(default=None, max_length=64)
    rejected: bool = False


class MemoryCreate(BaseModel):
    scope: str
    kind: str
    content: str
    source: str = "user"
    project_id: str | None = None
    explicit: bool = False


class MemoryClear(BaseModel):
    scope: str
    project_id: str | None = None
    confirmation: str


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=1)


class PrivacySave(BaseModel):
    mode: str = Field(pattern=r"^(standard|privacy-controlled|offline)$")


class ClassificationOverride(BaseModel):
    content_type: str = Field(min_length=1, max_length=64)


class PreviewAnnotationCreate(BaseModel):
    anchor: PreviewAnchor
    body: str = Field(min_length=1, max_length=20_000)


class PreviewSelectionCreate(BaseModel):
    action: str = Field(pattern=r"^(chat|evidence|citation)$")
    anchor: PreviewAnchor
    text: str = Field(min_length=1, max_length=100_000)


class TypographyChangeCreate(BaseModel):
    intent: ChangeIntent
    formats: list[str] = Field(default_factory=lambda: ["md", "docx", "typst", "latex", "pdf"])
    allow_fallback: bool = False


class RevisionResolveCreate(BaseModel):
    reference: str = ""
    document_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1)
    conversation_id: str | None = None
    artifact_id: UUID | None = None


class DocumentRevisionCreate(BaseModel):
    operation: RevisionOperation


class PresentationPatchCreate(BaseModel):
    revision: int = Field(ge=1)
    operations: list[PresentationPatchOperation] = Field(min_length=1, max_length=100)
    formats: list[str] = Field(default_factory=list, max_length=4)


class TargetResolveCreate(BaseModel):
    request: str
    revision: int | None = Field(default=None, ge=1)
    section_id: UUID | None = None
    block_id: UUID | None = None


class AnnotationTypographyChangeCreate(BaseModel):
    anchor: PreviewAnchor
    body: str = Field(min_length=1, max_length=20_000)
    block_id: UUID | None = None
    provider_id: str | None = None
    formats: list[str] = Field(default_factory=lambda: ["md", "docx", "typst", "latex", "pdf"])
    allow_fallback: bool = False


class RequirementAnalyzeCreate(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    message_ids: list[str] = Field(default_factory=list)
    attachment_ids: list[str] = Field(default_factory=list)


class RequirementConfirmCreate(BaseModel):
    requirement: RequirementSpec


class RecoveryDecision(BaseModel):
    decision: str = Field(pattern=r"^(retry|skip)$")


class AgentResumeCreate(BaseModel):
    selection_id: str | None = Field(default=None, min_length=1, max_length=255)


class FirstRunComplete(BaseModel):
    privacy_mode: str = Field(pattern=r"^(standard|privacy-controlled|offline)$")
    providers_configured: bool = False
    skipped: list[str] = Field(default_factory=list)


class DependencyInstallCreate(BaseModel):
    tool: str = Field(pattern=r"^(uv|typst|pandoc|xelatex)$")
    destination: str | None = None
    confirmed: bool = False


def row_dict(row: object, *fields: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for field in fields:
        value = getattr(row, field)
        if isinstance(value, datetime):
            utc_value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            result[field] = utc_value.isoformat().replace("+00:00", "Z")
        else:
            result[field] = value
    return result


def create_app(
    settings: Settings | None = None, tokens: LocalSessionTokens | None = None
) -> FastAPI:
    app_settings = settings or get_settings()
    token_service = tokens or LocalSessionTokens()
    databases = DatabaseManager(app_settings)
    projects = ProjectRepository(databases)
    conversations = ConversationRepository(databases)
    events = EventRepository(databases)
    tasks = TaskService(databases)
    progress = DurableProgressSink(tasks)
    scheduler = DurableRunScheduler(tasks)
    process_ledger = ProcessLedger(app_settings.resolved_data_dir / "global" / "process-ledger.db")
    orphan_process_audit: list[dict[str, object]] = []
    steering_rules = DeterministicSteeringRules()
    steering_validator = SteeringPlanValidator()
    steering_decisions = SteeringDecisionStore(databases)
    backups = BackupService(app_settings.resolved_data_dir / "backups")
    credentials = CredentialStore(app_settings.resolved_data_dir / "global" / "credentials.json")
    provider_registry = ProviderRegistry(databases)
    memories = MemoryService(databases)
    side_effects = SideEffectStore(app_settings.resolved_data_dir / "global" / "recovery.db")
    recovery = RecoveryService(side_effects)
    first_run = FirstRunService(app_settings.resolved_data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        databases.initialize_global()
        with databases.global_session() as session:
            resource_setting = session.get(AppSetting, "resource_limits")
            if resource_setting is not None:
                scheduler.resources.configure(
                    ResourceLimits.model_validate_json(resource_setting.value_json)
                )
        orphan_process_audit.extend(process_ledger.audit_orphans(terminate=True))
        for project in projects.list():
            recovered = tasks.reconcile_orphans(project.id, force=True)
            for run in recovered:
                payload = json.loads(run.payload_json)
                if not (run.checkpoint_ref and bool(payload.get("replay_safe"))):
                    continue
                body = AgentJobCreate(
                    content=str(payload.get("content", "")),
                    provider_id=cast(str | None, payload.get("provider_id")),
                    approved=bool(payload.get("approved", False)),
                    idempotency_key=run.idempotency_key,
                    resource_request=ResourceRequest.model_validate(
                        payload.get("resource_request", {})
                    ),
                )
                launch_agent_job(project.id, str(payload["session_id"]), run.id, body)
        yield
        await scheduler.shutdown()
        process_ledger.close()
        databases.global_engine.dispose()

    app = FastAPI(title="PaperAgent Local API", version=__version__, lifespan=lifespan)
    app.state.tokens = token_service
    app.state.databases = databases
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", f"http://127.0.0.1:{app_settings.port}"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )

    def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
        if not token_service.verify(authorization.removeprefix("Bearer ")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    def project_file(project_id: str, file_id: str) -> FileRecord:
        with databases.project_session(project_id) as session:
            record = session.get(FileRecord, file_id)
            if record is None:
                raise HTTPException(status_code=404, detail="project file not found")
            session.expunge(record)
            return record

    def register_rendered_files(
        project_id: str, revision: int, artifacts: list[ArtifactVersion]
    ) -> list[dict[str, object]]:
        registered: list[dict[str, object]] = []
        with databases.project_session(project_id) as session:
            for raw in artifacts:
                path = raw.path
                existing = session.scalar(
                    select(FileRecord).where(FileRecord.relative_path == path)
                )
                record = existing or FileRecord(
                    id=str(uuid4()),
                    category="output",
                    original_name=f"paper-r{revision}.{raw.format}",
                    relative_path=path,
                    sha256=raw.sha256,
                    size_bytes=(databases.project_root(project_id) / path).stat().st_size,
                    provenance_json=json.dumps(
                        {
                            "artifact_id": str(raw.artifact_id),
                            "document_id": str(raw.document_id),
                            "document_revision": revision,
                        }
                    ),
                )
                session.add(record)
                session.flush()
                registered.append(
                    row_dict(
                        record,
                        "id",
                        "category",
                        "original_name",
                        "sha256",
                        "size_bytes",
                        "created_at",
                    )
                )
            session.commit()
        return registered

    def register_visual_diff_files(
        project_id: str, document_id: UUID, revision: int, report: VisualDiffReport | None
    ) -> list[dict[str, object]]:
        if report is None:
            return []
        project_root = databases.project_root(project_id).resolve()
        registered: list[dict[str, object]] = []
        with databases.project_session(project_id) as session:
            for page in report.pages:
                for kind, raw_path in (
                    ("before", page.before_image),
                    ("after", page.after_image),
                    ("diff", page.diff_image),
                ):
                    if raw_path is None:
                        continue
                    path = Path(raw_path).resolve()
                    if project_root not in path.parents or not path.is_file():
                        raise ValueError("visual diff escaped the project root")
                    relative = path.relative_to(project_root).as_posix()
                    existing = session.scalar(
                        select(FileRecord).where(FileRecord.relative_path == relative)
                    )
                    record = existing or FileRecord(
                        id=str(uuid4()),
                        category="visual_diff",
                        original_name=f"paper-r{revision}-page-{page.page}-{kind}.png",
                        relative_path=relative,
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        size_bytes=path.stat().st_size,
                        provenance_json=json.dumps(
                            {
                                "document_id": str(document_id),
                                "document_revision": revision,
                                "page": page.page,
                                "diff_kind": kind,
                            }
                        ),
                    )
                    session.add(record)
                    session.flush()
                    registered.append(
                        row_dict(
                            record,
                            "id",
                            "category",
                            "original_name",
                            "sha256",
                            "size_bytes",
                            "created_at",
                        )
                        | {"page": page.page, "kind": kind}
                    )
            session.commit()
        return registered

    def model_provider(config: ProviderConfig) -> ModelProvider:
        if config.modality is not ProviderModality.TEXT:
            raise ValueError(f"provider {config.id} is not a text provider")

        def credential() -> str | None:
            return credentials.get(config.credential_ref) if config.credential_ref else None

        if config.provider_type == "mock" and app_settings.environment == "test":
            from paperagent.providers.mock import MockProvider

            return MockProvider(config, content="OK")
        if config.provider_type == "anthropic":
            return AnthropicProvider(config, credential)
        if config.provider_type == "gemini":
            return GeminiProvider(config, credential)
        return OpenAICompatibleProvider(config, credential)

    def image_provider(config: ProviderConfig) -> OpenAIImageProvider | SeedreamImageProvider:
        if config.modality is not ProviderModality.IMAGE:
            raise ValueError(f"provider {config.id} is not an image provider")
        key = credentials.get(config.credential_ref) if config.credential_ref else None
        if config.provider_type in {"seedream", "seedream_image"}:
            return SeedreamImageProvider(str(config.base_url), key, config.model)
        return OpenAIImageProvider(str(config.base_url), key, config.model)

    def public_provider(config: ProviderConfig) -> dict[str, object]:
        binding_ids = {row.provider_id: row.id for row in provider_registry.bindings()}
        return config.model_dump(mode="json", exclude={"credential_ref", "extra"}) | {
            "model_name": config.model,
            "has_credential": credentials.has(config.credential_ref),
            "credential_status": credentials.status(config.credential_ref),
            "active": config.id in binding_ids,
            "binding_id": binding_ids.get(config.id),
        }

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/bootstrap-token")
    def bootstrap_token(request: Request) -> dict[str, str]:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=403, detail="local access only")
        return {"token": token_service.issue()}

    @app.post("/api/projects", dependencies=[Depends(require_token)], status_code=201)
    def create_project(body: NamedCreate) -> dict[str, object]:
        row = projects.create(body.name)
        return row_dict(
            row,
            "id",
            "name",
            "slug",
            "description",
            "status",
            "archived",
            "created_at",
            "updated_at",
        )

    @app.get("/api/projects", dependencies=[Depends(require_token)])
    def list_projects() -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "name",
                "slug",
                "description",
                "status",
                "archived",
                "created_at",
                "updated_at",
            )
            for row in projects.list()
        ]

    @app.get("/api/projects/{project_id}", dependencies=[Depends(require_token)])
    def get_project(project_id: str) -> dict[str, object]:
        try:
            row = projects.get(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        return row_dict(
            row,
            "id",
            "name",
            "slug",
            "description",
            "status",
            "archived",
            "created_at",
            "updated_at",
        )

    @app.patch("/api/projects/{project_id}", dependencies=[Depends(require_token)])
    def update_project(project_id: str, body: ProjectUpdate) -> dict[str, object]:
        try:
            row = projects.update(project_id, name=body.name, description=body.description)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        return row_dict(
            row,
            "id",
            "name",
            "slug",
            "description",
            "status",
            "archived",
            "created_at",
            "updated_at",
        )

    @app.delete("/api/projects/{project_id}", dependencies=[Depends(require_token)])
    def delete_project(project_id: str, confirmation: str = "") -> dict[str, bool]:
        if confirmation != "DELETE PROJECT":
            raise HTTPException(
                status_code=409, detail="explicit project deletion confirmation required"
            )
        try:
            projects.soft_delete(project_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        return {"deleted": True}

    @app.patch("/api/projects/{project_id}/archive", dependencies=[Depends(require_token)])
    def archive_project(project_id: str, archived: bool = True) -> dict[str, object]:
        try:
            row = projects.archive(project_id, archived=archived)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="project not found") from error
        return row_dict(
            row,
            "id",
            "name",
            "slug",
            "description",
            "status",
            "archived",
            "created_at",
            "updated_at",
        )

    @app.post("/api/projects/{project_id}/sessions", dependencies=[Depends(require_token)])
    def create_session(project_id: str, body: SessionCreate) -> dict[str, object]:
        row = conversations.create_session(project_id, body.title)
        events.append(project_id, "session.created", {"session_id": row.id})
        return row_dict(
            row,
            "id",
            "title",
            "status",
            "archived",
            "draft",
            "last_read_sequence",
            "created_at",
            "updated_at",
        )

    @app.get("/api/projects/{project_id}/sessions", dependencies=[Depends(require_token)])
    def list_sessions(project_id: str) -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "title",
                "status",
                "archived",
                "draft",
                "last_read_sequence",
                "created_at",
                "updated_at",
            )
            for row in conversations.list_sessions(project_id)
        ]

    @app.post(
        "/api/projects/{project_id}/conversations",
        dependencies=[Depends(require_token)],
        status_code=201,
    )
    def create_conversation(project_id: str, body: SessionCreate) -> dict[str, object]:
        row = conversations.create_session(project_id, body.title)
        events.append(project_id, "conversation.created", {"conversation_id": row.id})
        return row_dict(
            row,
            "id",
            "title",
            "status",
            "archived",
            "draft",
            "last_read_sequence",
            "created_at",
            "updated_at",
        )

    @app.get("/api/projects/{project_id}/conversations", dependencies=[Depends(require_token)])
    def list_conversations(project_id: str) -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "title",
                "status",
                "archived",
                "draft",
                "last_read_sequence",
                "created_at",
                "updated_at",
            )
            for row in conversations.list_sessions(project_id)
        ]

    @app.patch(
        "/api/projects/{project_id}/conversations/{conversation_id}",
        dependencies=[Depends(require_token)],
    )
    def update_conversation(
        project_id: str, conversation_id: str, body: ConversationUpdate
    ) -> dict[str, object]:
        try:
            row = conversations.update_session(
                project_id,
                conversation_id,
                title=body.title,
                draft=body.draft,
                archived=body.archived,
                last_read_sequence=body.last_read_sequence,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="conversation not found") from error
        return row_dict(
            row,
            "id",
            "title",
            "status",
            "archived",
            "draft",
            "last_read_sequence",
            "created_at",
            "updated_at",
        )

    @app.delete(
        "/api/projects/{project_id}/conversations/{conversation_id}",
        dependencies=[Depends(require_token)],
    )
    def delete_conversation(
        project_id: str, conversation_id: str, confirmation: str = ""
    ) -> dict[str, bool]:
        if confirmation != "DELETE CONVERSATION":
            raise HTTPException(
                status_code=409, detail="explicit conversation deletion confirmation required"
            )
        try:
            conversations.soft_delete_session(project_id, conversation_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="conversation not found") from error
        return {"deleted": True}

    @app.post(
        "/api/projects/{project_id}/sessions/{session_id}/messages",
        dependencies=[Depends(require_token)],
        status_code=201,
    )
    def add_message(project_id: str, session_id: str, body: MessageCreate) -> dict[str, object]:
        try:
            row = conversations.add_message(
                project_id,
                session_id,
                body.role,
                body.content,
                run_id=body.run_id,
                parent_message_id=body.parent_message_id,
                branch_id=body.branch_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="session not found") from error
        events.append(
            project_id, "message.created", {"message_id": row.id, "session_id": session_id}
        )
        return row_dict(
            row,
            "id",
            "session_id",
            "role",
            "content",
            "sequence",
            "run_id",
            "parent_message_id",
            "branch_id",
            "status",
            "superseded_by_message_id",
            "created_at",
            "updated_at",
        )

    @app.get(
        "/api/projects/{project_id}/sessions/{session_id}/messages",
        dependencies=[Depends(require_token)],
    )
    def list_messages(
        project_id: str,
        session_id: str,
        after: int = 0,
        before: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "session_id",
                "role",
                "content",
                "sequence",
                "run_id",
                "parent_message_id",
                "branch_id",
                "status",
                "superseded_by_message_id",
                "created_at",
                "updated_at",
            )
            | {"artifact_links": ArtifactService(databases, project_id).links_for_message(row.id)}
            for row in conversations.list_messages(
                project_id, session_id, after=after, before=before, limit=limit
            )
        ]

    @app.post(
        "/api/projects/{project_id}/conversations/{conversation_id}/messages",
        dependencies=[Depends(require_token)],
        status_code=201,
    )
    def add_conversation_message(
        project_id: str, conversation_id: str, body: MessageCreate
    ) -> dict[str, object]:
        return add_message(project_id, conversation_id, body)

    @app.get(
        "/api/projects/{project_id}/conversations/{conversation_id}/messages",
        dependencies=[Depends(require_token)],
    )
    def list_conversation_messages(
        project_id: str,
        conversation_id: str,
        after: int = 0,
        before: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        return list_messages(project_id, conversation_id, after=after, before=before, limit=limit)

    @app.post(
        "/api/projects/{project_id}/sessions/{session_id}/agent/turn",
        dependencies=[Depends(require_token)],
    )
    async def run_agent_turn(
        project_id: str, session_id: str, body: AgentTurnCreate
    ) -> dict[str, object]:
        selected: ProviderConfig | None = None
        if body.task_id:
            try:
                task_row = tasks.get(project_id, body.task_id)
                snapshot = json.loads(task_row.provider_snapshot_json)
                if snapshot:
                    selected = ProviderConfig.model_validate(snapshot)
            except KeyError:
                selected = None
        if selected is None and body.provider_id:
            selected = provider_registry.get(body.provider_id)
        if selected is None:
            selected = provider_registry.active(ProviderModality.TEXT, project_id=project_id)
        if (
            selected is None
            or selected.modality is not ProviderModality.TEXT
            or (selected.provider_type == "mock" and app_settings.environment != "test")
        ):
            raise HTTPException(
                status_code=409,
                detail="no real text provider is configured for this request",
            )
        configured = [selected]
        providers = [model_provider(selected)]
        project_root = databases.project_root(project_id)
        task_id = body.task_id or str(uuid4())

        def artifact_event_sink(event_type: str, payload: dict[str, object]) -> None:
            events.append(project_id, event_type, payload)

        def interactive_progress_sink(event_type: str, payload: dict[str, object]) -> None:
            phase = payload.get("phase")
            if isinstance(phase, str) and phase:
                with suppress(KeyError):
                    tasks.update_phase(project_id, task_id, phase)
            progress.emit(
                project_id=project_id,
                run_id=task_id,
                event_type=event_type,
                payload=payload,
            )

        artifact_service = ArtifactService(
            databases,
            project_id,
            event_sink=artifact_event_sink,
        )
        tool_registry = ToolRegistry()
        knowledge = ProjectKnowledgeService(project_root)

        def search_knowledge(arguments: dict[str, JsonValue]) -> JsonValue:
            limit_value = arguments.get("limit", 10)
            if not isinstance(limit_value, int):
                raise ValueError("knowledge search limit must be an integer")
            results = knowledge.retrieve(
                str(arguments["query"]),
                project_id=project_id,
                limit=limit_value,
            )
            items = [
                cast(
                    JsonValue,
                    {
                        "item_id": result.hit.item_id,
                        "title": result.hit.title,
                        "content": result.hit.content,
                        "source_uri": result.hit.source_uri,
                        "locator": result.hit.locator,
                        "reason": result.reason,
                    },
                )
                for result in results
            ]
            return {"items": items}

        def read_file(arguments: dict[str, JsonValue]) -> JsonValue:
            target = (project_root / str(arguments["relative_path"])).resolve()
            start_value = arguments.get("start", 0)
            maximum_value = arguments.get("max_chars", 120_000)
            if not isinstance(start_value, int) or not isinstance(maximum_value, int):
                raise ValueError("file range values must be integers")
            start = start_value
            maximum = maximum_value
            content = target.read_text(encoding="utf-8", errors="replace")
            return {
                "relative_path": target.relative_to(project_root).as_posix(),
                "start": start,
                "content": content[start : start + maximum],
            }

        def lookup_artifact(arguments: dict[str, JsonValue]) -> JsonValue:
            relation_value = arguments.get("relation")
            run_value = arguments.get("run_id")
            matches = artifact_service.lookup(
                conversation_id=session_id,
                relation=str(relation_value) if relation_value is not None else None,
                run_id=str(run_value) if run_value is not None else None,
            )
            return cast(
                JsonValue,
                {
                    "matches": matches,
                    "artifact_refs": [
                        str(match["id"])
                        for match in matches
                        if match.get("validation_status") == "valid"
                    ],
                },
            )

        adapters = {
            "knowledge.search": CallableToolAdapter(search_knowledge),
            "file.read": CallableToolAdapter(read_file),
            "artifact.lookup": CallableToolAdapter(lookup_artifact),
        }
        for spec in builtin_tool_specs():
            adapter = adapters.get(spec.name)
            if adapter is not None:
                tool_registry.register(spec, adapter)
        requested_typography, requested_typography_fields = extract_typography(body.content)
        execution_tools = ExecutionToolSuite(
            data_root=app_settings.resolved_data_dir,
            project_root=project_root,
            run_id=task_id,
            uv_path=app_settings.uv_path,
            artifact_service=artifact_service,
            requested_typography=(requested_typography if requested_typography_fields else None),
            source_conversation_id=session_id,
        )
        execution_tools.register(tool_registry)
        history = conversations.list_messages(project_id, session_id)
        messages = [
            ChatMessage(role=row.role, content=row.content)
            for row in history
            if row.role in {"user", "assistant"}
        ]
        if not messages or messages[-1].role != "user" or messages[-1].content != body.content:
            row = conversations.add_message(project_id, session_id, "user", body.content)
            messages.append(ChatMessage(role="user", content=row.content))
            user_message_id = row.id
        else:
            user_message_id = history[-1].id
        execution_tools.set_source_message(user_message_id)
        compiled = default_prompt_compiler().compile(
            PromptSelectionContext(
                agent_type="requirement_agent",
                task="interactive_turn",
                runtime={
                    "project_id": project_id,
                    "provider_ids": [config.id for config in configured],
                    "workspace": str(project_root),
                },
            ),
            messages,
        )
        tool_names = (
            list(CURRENT_INTERACTIVE_TOOL_NAMES)
            if any(Capability.TOOLS in config.capabilities for config in configured)
            else []
        )

        async def read_steering_guidance() -> list[ChatMessage]:
            if not scheduler.is_active(project_id, task_id):
                return []
            guidance = tasks.consume_pending_guidance(project_id, task_id)
            if not guidance:
                return []
            return [
                ChatMessage(
                    role="developer",
                    content=json.dumps(
                        {
                            "steering_guidance": guidance,
                            "instruction": (
                                "Apply this user-approved guidance to the remaining work. "
                                "Preserve completed valid work and report any conflict."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
            ]

        loop = AgentLoop(
            ProviderRouter(providers),
            tool_registry,
            ToolExecutor(
                tool_registry,
                ToolResultStore(project_root / "artifacts" / "tool-results"),
                side_effects=side_effects,
            ),
            project_root,
            control_hook=(
                (lambda: scheduler.checkpoint(project_id, task_id))
                if scheduler.is_active(project_id, task_id)
                else None
            ),
            guidance_hook=read_steering_guidance,
        )
        loop_request = AgentLoopRequest(
            project_id=project_id,
            agent_type="requirement_agent",
            messages=compiled.messages,
            tool_names=tool_names,
            approved=body.approved,
        )
        # Side-effect idempotency keys are trace-scoped. Persist that trace on
        # the durable task so the Recovery Center can show only operations from
        # the selected run instead of accumulated failures from the project.
        with suppress(KeyError):
            task_row = tasks.get(project_id, task_id)
            task_payload = json.loads(task_row.payload_json)
            task_payload["trace_id"] = str(loop_request.trace_id)
            tasks.update_payload(project_id, task_id, task_payload)
        captured: dict[str, AgentLoopResult] = {}
        try:
            checkpoint_root = project_root / "checkpoints"
            checkpoint_root.mkdir(parents=True, exist_ok=True)
            async with AsyncSqliteSaver.from_conn_string(
                (checkpoint_root / "langgraph.db").as_posix()
            ) as checkpointer:
                graph = compile_dynamic_interactive_graph(
                    loop,
                    loop_request,
                    available_tools=tool_names,
                    result_sink=lambda value: captured.__setitem__("result", value),
                    progress_sink=interactive_progress_sink,
                    checkpointer=checkpointer,
                )
                conversation_engine = ConversationEngine(
                    SqliteConversationPersistence(databases),
                    LangGraphLifecycleAdapter(graph),
                )
                turn = TurnRequest(
                    project_id=project_id,
                    thread_id=session_id,
                    task_id=task_id,
                    message_id=user_message_id,
                    user_message=body.content,
                    provider_id=body.provider_id,
                    idempotency_key=f"interactive:{session_id}:{user_message_id}",
                )
                if body.resume_checkpoint:
                    checkpoint_ref = tasks.get(project_id, task_id).checkpoint_ref
                    resume = ResumeRequest(
                        project_id=project_id,
                        thread_id=session_id,
                        task_id=task_id,
                        decision=None,
                        idempotency_key=f"resume:{task_id}:{checkpoint_ref or 'latest'}",
                        checkpoint_id=checkpoint_ref,
                    )
                    engine_events = [event async for event in conversation_engine.resume(resume)]
                else:
                    engine_events = [event async for event in conversation_engine.run_turn(turn)]
            result = captured.get("result")
            if result is None:
                failure = next(
                    (
                        event
                        for event in reversed(engine_events)
                        if event.kind.value == "engine.failed"
                    ),
                    None,
                )
                message = (
                    str(failure.payload.get("message", "agent graph failed"))
                    if failure
                    else "agent graph produced no result"
                )
                raise RuntimeError(message)
        except Exception as error:
            events.append(
                project_id,
                "agent.failed",
                {"session_id": session_id, "task_id": task_id, "message": str(error)[:2_000]},
            )
            raise HTTPException(status_code=502, detail=str(error)) from error
        finally:
            execution_tools.close()
            for provider in providers:
                client = getattr(provider, "client", None)
                if client is not None:
                    await client.aclose()
        try:
            CompletionClaimValidator(artifact_service).validate(task_id, result.content)
        except ArtifactIntegrityError as error:
            events.append(
                project_id,
                "agent.delivery_blocked",
                {"session_id": session_id, "task_id": task_id, "message": str(error)},
            )
            raise HTTPException(status_code=502, detail=str(error)) from error
        assistant = conversations.add_message(
            project_id, session_id, "assistant", result.content, run_id=task_id
        )
        artifact_service.link_run_to_message(task_id, session_id, assistant.id)
        artifact_service.link_verified_to_message(
            _verified_artifact_refs(result),
            conversation_id=session_id,
            message_id=assistant.id,
        )
        assistant_artifacts = artifact_service.links_for_message(assistant.id)
        events.append(
            project_id,
            "agent.completed",
            {
                "session_id": session_id,
                "task_id": task_id,
                "message_id": assistant.id,
                "rounds": result.rounds,
                "tool_call_count": result.tool_call_count,
                "routes": result.routes,
                "prompt_hash": compiled.prompt_hash,
            },
        )
        return {
            "message": row_dict(assistant, "id", "session_id", "role", "content", "created_at")
            | {"artifact_links": assistant_artifacts},
            "task_id": task_id,
            "rounds": result.rounds,
            "tool_call_count": result.tool_call_count,
            "routes": result.routes,
            "prompt": {
                "hash": compiled.prompt_hash,
                "modules": compiled.module_versions,
                "runtime_snapshot_id": compiled.runtime_snapshot_id,
            },
        }

    def task_response(project_id: str, task_id: str) -> dict[str, object]:
        row = tasks.get(project_id, task_id)
        payload = json.loads(row.payload_json)
        resource_request = json.loads(row.resource_request_json)
        raw_snapshot = json.loads(row.provider_snapshot_json)
        provider_snapshot = (
            ProviderConfig.model_validate(raw_snapshot).model_dump(
                mode="json", exclude={"credential_ref", "extra"}
            )
            if raw_snapshot
            else {}
        )
        return row_dict(
            row,
            "id",
            "kind",
            "status",
            "idempotency_key",
            "conversation_id",
            "parent_task_id",
            "current_phase",
            "checkpoint_ref",
            "attempt",
            "version",
            "worker_id",
            "lease_expires_at",
            "heartbeat_at",
            "started_at",
            "first_output_at",
            "finished_at",
            "error_code",
            "recovery_strategy",
            "cancel_requested",
            "read_at",
            "notification_sent",
            "created_at",
            "updated_at",
        ) | {
            "payload": payload,
            "resource_request": resource_request,
            "provider_snapshot": provider_snapshot,
            "unread": row.read_at is None
            and TaskStatus(row.status)
            in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.SUPERSEDED,
            },
        }

    async def execute_agent_job(
        project_id: str,
        session_id: str,
        task_id: str,
        body: AgentJobCreate,
    ) -> None:
        key = (project_id, task_id)
        try:
            tasks.update_phase(project_id, task_id, "understanding")
            progress.emit(
                project_id=project_id,
                run_id=task_id,
                event_type="run.progress",
                payload={"phase": "understanding", "summary": "正在理解需求并准备执行计划"},
            )
            await scheduler.checkpoint(project_id, task_id)
            result = await run_agent_turn(
                project_id,
                session_id,
                AgentTurnCreate(
                    content=body.content,
                    provider_id=body.provider_id,
                    approved=body.approved,
                    task_id=task_id,
                    resume_checkpoint=body.resume_checkpoint,
                ),
            )
            await scheduler.checkpoint(project_id, task_id)
            current = tasks.get(project_id, task_id)
            payload = json.loads(current.payload_json)
            tasks.update_payload(project_id, task_id, payload | {"result": result})
            tasks.update_phase(project_id, task_id, "finalizing")
            if TaskStatus(current.status) is TaskStatus.RUNNING:
                tasks.transition(project_id, task_id, TaskStatus.COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            current = tasks.get(project_id, task_id)
            payload = json.loads(current.payload_json)
            detail = error.detail if isinstance(error, HTTPException) else str(error)
            tasks.update_payload(
                project_id,
                task_id,
                payload | {"error": str(detail)[:2_000], "recovery_required": True},
            )
            tasks.update_phase(project_id, task_id, "recovery_required")
            if TaskStatus(current.status) is TaskStatus.RUNNING:
                tasks.transition(project_id, task_id, TaskStatus.FAILED)
        finally:
            scheduler.discard(*key)

    def launch_agent_job(
        project_id: str,
        session_id: str,
        task_id: str,
        body: AgentJobCreate,
    ) -> None:
        key = (project_id, task_id)
        if scheduler.is_active(*key):
            return
        scheduler.launch(
            project_id,
            task_id,
            lambda: execute_agent_job(project_id, session_id, task_id, body),
            body.resource_request,
        )

    @app.post(
        "/api/projects/{project_id}/sessions/{session_id}/agent/jobs",
        dependencies=[Depends(require_token)],
        status_code=202,
    )
    async def start_agent_job(
        project_id: str, session_id: str, body: AgentJobCreate
    ) -> dict[str, object]:
        selected = (
            provider_registry.get(body.provider_id)
            if body.provider_id
            else provider_registry.active(ProviderModality.TEXT, project_id=project_id)
        )
        if selected is None or selected.modality is not ProviderModality.TEXT:
            raise HTTPException(status_code=409, detail="no active text provider is configured")
        row = tasks.create(
            project_id,
            "agent.turn",
            body.idempotency_key,
            {
                "session_id": session_id,
                "content": body.content,
                "provider_id": selected.id,
                "provider_snapshot": selected.model_dump(mode="json"),
                "approved": body.approved,
                "resource_request": (body.resource_request or ResourceRequest()).model_dump(
                    mode="json"
                ),
            },
        )
        if TaskStatus(row.status) is TaskStatus.PENDING:
            launch_agent_job(project_id, session_id, row.id, body)
        return task_response(project_id, row.id)

    @app.get(
        "/api/projects/{project_id}/agent/tasks",
        dependencies=[Depends(require_token)],
    )
    def list_agent_tasks(project_id: str) -> list[dict[str, object]]:
        return [task_response(project_id, row.id) for row in tasks.list(project_id)]

    @app.get(
        "/api/projects/{project_id}/agent/tasks/{task_id}",
        dependencies=[Depends(require_token)],
    )
    def get_agent_task(project_id: str, task_id: str) -> dict[str, object]:
        try:
            row = tasks.get(project_id, task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="task not found") from error
        if TaskStatus(row.status) is TaskStatus.RUNNING and not scheduler.is_active(
            project_id, task_id
        ):
            tasks.transition(project_id, task_id, TaskStatus.PAUSED)
            payload = json.loads(row.payload_json)
            tasks.update_payload(
                project_id,
                task_id,
                payload | {"recovery_required": True, "stopped_at": "process_restart"},
            )
        return task_response(project_id, task_id)

    @app.post(
        "/api/projects/{project_id}/agent/tasks/{task_id}/pause",
        dependencies=[Depends(require_token)],
    )
    def pause_agent_task(project_id: str, task_id: str) -> dict[str, object]:
        try:
            scheduler.pause(project_id, task_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return task_response(project_id, task_id)

    @app.post(
        "/api/projects/{project_id}/runs/{task_id}/resume",
        dependencies=[Depends(require_token)],
    )
    @app.post(
        "/api/projects/{project_id}/agent/tasks/{task_id}/resume",
        dependencies=[Depends(require_token)],
    )
    async def resume_agent_task(
        project_id: str, task_id: str, body: AgentResumeCreate | None = None
    ) -> dict[str, object]:
        try:
            row = tasks.get(project_id, task_id)
            payload = json.loads(row.payload_json)
            if body and body.selection_id:
                options = list(payload.get("recovery_options", []))
                selected = next(
                    (
                        option
                        for option in options
                        if isinstance(option, dict) and option.get("id") == body.selection_id
                    ),
                    None,
                )
                if selected is None:
                    raise ValueError("recovery selection is unavailable or expired")
                guidance = list(payload.get("pending_guidance", []))
                guidance.append(
                    {
                        "kind": "recovery_selection",
                        "selection_id": body.selection_id,
                        "selection": selected,
                    }
                )
                payload = payload | {
                    "pending_guidance": guidance,
                    "selected_recovery_option": selected,
                }
            payload = dict(payload)
            payload.pop("error", None)
            payload.pop("failure_category", None)
            payload.pop("failure_details", None)
            payload["recovery_required"] = False
            tasks.update_payload(project_id, task_id, payload)
            tasks.transition(project_id, task_id, TaskStatus.RUNNING)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not scheduler.resume(project_id, task_id):
            job_body = AgentJobCreate(
                content=str(payload["content"]),
                provider_id=cast(str | None, payload.get("provider_id")),
                approved=bool(payload.get("approved", False)),
                idempotency_key=row.idempotency_key,
                resource_request=ResourceRequest.model_validate(
                    payload.get("resource_request", {})
                ),
                resume_checkpoint=True,
            )
            launch_agent_job(project_id, str(payload["session_id"]), task_id, job_body)
        return task_response(project_id, task_id)

    @app.post(
        "/api/projects/{project_id}/runs/{task_id}/cancel",
        dependencies=[Depends(require_token)],
    )
    @app.post(
        "/api/projects/{project_id}/agent/tasks/{task_id}/cancel",
        dependencies=[Depends(require_token)],
    )
    async def cancel_agent_task(project_id: str, task_id: str) -> dict[str, object]:
        try:
            await scheduler.cancel(project_id, task_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return task_response(project_id, task_id)

    @app.post(
        "/api/projects/{project_id}/runs/{run_id}/steer",
        dependencies=[Depends(require_token)],
    )
    async def steer_run(project_id: str, run_id: str, body: SteeringCreate) -> dict[str, object]:
        try:
            target = tasks.get(project_id, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="target run not found") from error
        payload = json.loads(target.payload_json)
        provider_snapshot_payload = json.loads(target.provider_snapshot_json)
        graph_payload = payload.get("task_graph")
        graph = TaskGraph.model_validate(graph_payload) if isinstance(graph_payload, dict) else None
        completed_nodes = tuple(str(item) for item in payload.get("completed_nodes", []))
        checkpoints = (target.checkpoint_ref,) if target.checkpoint_ref else ()
        context = SteeringContext(
            target_run_id=run_id,
            public_status=target.status,
            public_phase=target.current_phase,
            completed_nodes=completed_nodes,
            available_checkpoints=checkpoints,
            task_graph=graph,
            stable_artifact_hashes={
                str(key): str(value)
                for key, value in dict(payload.get("artifact_hashes", {})).items()
            },
            has_paid_or_external_side_effects=bool(
                payload.get("paid_side_effect") or payload.get("external_side_effect")
            ),
        )
        envelope = None
        if body.decision_id:
            pending = steering_decisions.get(project_id, body.decision_id)
            if pending is None or pending.target_task_id != run_id:
                raise HTTPException(status_code=404, detail="steering decision not found")
            if pending.status != "pending_confirmation":
                raise HTTPException(status_code=409, detail="steering decision is already resolved")
            envelope = steering_decisions.envelope(pending)
            if envelope.expires_at and envelope.expires_at < datetime.now(UTC):
                steering_decisions.update_status(project_id, body.decision_id, "expired")
                raise HTTPException(status_code=409, detail="steering confirmation expired")
            if body.rejected:
                steering_decisions.update_status(project_id, body.decision_id, "rejected")
                return {
                    "status": "rejected",
                    "envelope": envelope.model_dump(mode="json"),
                    "target_run": task_response(project_id, run_id),
                }
            if not body.confirmed:
                raise HTTPException(status_code=409, detail="confirmation is required")
        else:
            envelope = steering_rules.decide(
                body.content, context, trigger_message_id=body.message_id
            )
        provider: ModelProvider | None = None
        if envelope is None:
            provider_id = body.provider_id or cast(str | None, payload.get("provider_id"))
            config = provider_registry.get(provider_id) if provider_id else None
            if config is not None and config.modality is not ProviderModality.TEXT:
                config = None
            provider = model_provider(config) if config is not None else None
            try:
                envelope = await SteeringImpactAgent(provider).decide(
                    body.content, context, trigger_message_id=body.message_id
                )
            finally:
                client = getattr(provider, "client", None)
                if client is not None:
                    await client.aclose()

        if (
            graph is not None
            and envelope.impact_level not in {ImpactLevel.L0, ImpactLevel.L1}
            and not envelope.affected_nodes
        ):
            candidates = [
                node.node_id for node in graph.nodes if node.node_id not in completed_nodes
            ]
            if target.current_phase in {node.node_id for node in graph.nodes}:
                candidates.insert(0, target.current_phase)
            envelope = envelope.model_copy(
                update={"affected_nodes": tuple(dict.fromkeys(candidates[:1]))}
            )
        impact = steering_validator.validate(envelope, context)
        envelope = envelope.model_copy(
            update={
                "affected_nodes": impact.affected_nodes,
                "preserved_nodes": impact.preserved_nodes,
                "confirmation_required": envelope.confirmation_required
                or (
                    envelope.confidence < 0.65
                    and envelope.impact_level not in {ImpactLevel.L0, ImpactLevel.L1}
                )
                or (
                    context.has_paid_or_external_side_effects
                    and envelope.impact_level not in {ImpactLevel.L0, ImpactLevel.L1}
                ),
                "permission_scopes": (
                    ("paid_or_external_side_effect",)
                    if context.has_paid_or_external_side_effects
                    else envelope.permission_scopes
                ),
            }
        )
        session_id = str(payload.get("session_id") or target.conversation_id or "")
        if envelope.confirmation_required and not body.confirmed:
            steering_decisions.create(project_id, envelope, status="pending_confirmation")
            message = conversations.add_message(
                project_id,
                session_id,
                "assistant",
                f"这条消息会触发 {envelope.impact_level.value} 变更: "
                f"{envelope.rationale_summary} 请确认后执行。",
                run_id=run_id,
            )
            return {
                "status": "pending_confirmation",
                "envelope": envelope.model_dump(mode="json"),
                "impact": impact.model_dump(mode="json"),
                "message": row_dict(message, "id", "session_id", "role", "content", "created_at"),
                "target_run": task_response(project_id, run_id),
            }

        replacement_id: str | None = None
        immediate_message: dict[str, object] | None = None
        if envelope.impact_level in {ImpactLevel.L0, ImpactLevel.L1}:
            if (
                envelope.relationship is SteeringRelationship.SUPPLEMENT
                and TaskStatus(target.status) is TaskStatus.PAUSED
            ):
                resumed = resume_agent_task(project_id, run_id)
                text = "当前任务已从安全边界继续。"
                message = conversations.add_message(
                    project_id, session_id, "assistant", text, run_id=run_id
                )
                immediate_message = row_dict(
                    message, "id", "session_id", "role", "content", "created_at"
                ) | {"run": resumed}
            elif envelope.relationship is SteeringRelationship.QUERY_ABOUT_RUN:
                text = (
                    f"当前任务状态为 {target.status}, 公开阶段为 {target.current_phase}, "
                    f"已尝试 {target.attempt} 次。主任务保持不变。"
                )
                message = conversations.add_message(
                    project_id, session_id, "assistant", text, run_id=run_id
                )
                immediate_message = row_dict(
                    message, "id", "session_id", "role", "content", "created_at"
                )
            else:
                sidecar = tasks.create(
                    project_id,
                    "steering.sidecar",
                    f"steering:{envelope.decision_id}",
                    {
                        "session_id": session_id,
                        "content": body.content,
                        "provider_id": body.provider_id or payload.get("provider_id"),
                        "parent_task_id": run_id,
                        "read_only": True,
                        "provider_snapshot": provider_snapshot_payload,
                    },
                )
                tasks.transition(project_id, sidecar.id, TaskStatus.RUNNING)
                try:
                    result = await run_agent_turn(
                        project_id,
                        session_id,
                        AgentTurnCreate(
                            content=body.content,
                            provider_id=body.provider_id
                            or cast(str | None, payload.get("provider_id")),
                            approved=False,
                            task_id=sidecar.id,
                        ),
                    )
                    tasks.update_payload(
                        project_id,
                        sidecar.id,
                        json.loads(tasks.get(project_id, sidecar.id).payload_json)
                        | {"result": result, "read_only": True},
                    )
                    tasks.transition(project_id, sidecar.id, TaskStatus.COMPLETED)
                    immediate_message = cast(dict[str, object], result["message"])
                except Exception:
                    text = (
                        "已将 B 作为只读 Sidecar 保存; A 继续运行。"
                        "Sidecar 模型暂不可用, 可稍后重试。"
                    )
                    message = conversations.add_message(
                        project_id, session_id, "assistant", text, run_id=sidecar.id
                    )
                    tasks.transition(project_id, sidecar.id, TaskStatus.FAILED)
                    immediate_message = row_dict(
                        message, "id", "session_id", "role", "content", "created_at"
                    )
        elif envelope.impact_level in {ImpactLevel.L2, ImpactLevel.L3}:
            guidance = list(payload.get("pending_guidance", []))
            guidance.append(
                {
                    "decision_id": str(envelope.decision_id),
                    "content": body.content,
                    "impact_level": envelope.impact_level.value,
                    "affected_nodes": list(impact.invalidated_nodes),
                    "preserved_nodes": list(impact.preserved_nodes),
                }
            )
            revised_graph = (
                steering_validator.compile_remaining_graph(impact, context)
                if envelope.impact_level is ImpactLevel.L3
                else None
            )
            tasks.update_payload(
                project_id,
                run_id,
                payload
                | {
                    "pending_guidance": guidance,
                    "replan_required": envelope.impact_level is ImpactLevel.L3,
                    "revised_task_graph": (
                        revised_graph.model_dump(mode="json") if revised_graph else None
                    ),
                },
            )
            progress.emit(
                project_id=project_id,
                run_id=run_id,
                event_type=(
                    "plan.revised"
                    if envelope.impact_level is ImpactLevel.L3
                    else "steering.guidance_queued"
                ),
                payload={
                    "summary": envelope.rationale_summary,
                    "preserved_nodes": list(impact.preserved_nodes),
                    "invalidated_nodes": list(impact.invalidated_nodes),
                    "revised_graph": (
                        revised_graph.model_dump(mode="json") if revised_graph else None
                    ),
                },
            )
            message = conversations.add_message(
                project_id,
                session_id,
                "assistant",
                "已记录变更, 将在安全边界注入。已完成且仍有效的节点会保留。",
                run_id=run_id,
            )
            immediate_message = row_dict(
                message, "id", "session_id", "role", "content", "created_at"
            )
        elif envelope.impact_level is ImpactLevel.L4:
            if scheduler.is_active(project_id, run_id):
                await scheduler.supersede(project_id, run_id)
            elif TaskStatus(target.status) is not TaskStatus.SUPERSEDED:
                tasks.transition(project_id, run_id, TaskStatus.SUPERSEDED)
            replacement = tasks.create(
                project_id,
                "agent.turn",
                f"steering-fork:{envelope.decision_id}",
                payload
                | {
                    "content": (
                        f"{payload.get('content', '')}\n\n[Steering correction]\n{body.content}"
                    ),
                    "parent_task_id": run_id,
                    "steering_decision_id": str(envelope.decision_id),
                    "preserved_nodes": list(impact.preserved_nodes),
                    "provider_snapshot": provider_snapshot_payload,
                },
            )
            if envelope.earliest_affected_checkpoint:
                tasks.set_checkpoint(
                    project_id, replacement.id, envelope.earliest_affected_checkpoint
                )
            replacement_id = replacement.id
            launch_agent_job(
                project_id,
                session_id,
                replacement.id,
                AgentJobCreate(
                    content=str(json.loads(replacement.payload_json)["content"]),
                    provider_id=cast(str | None, payload.get("provider_id")),
                    approved=bool(payload.get("approved", False)),
                    idempotency_key=replacement.idempotency_key,
                ),
            )
        else:
            if scheduler.is_active(project_id, run_id):
                await scheduler.cancel(project_id, run_id)
            elif TaskStatus(target.status) is not TaskStatus.CANCELLED:
                tasks.transition(project_id, run_id, TaskStatus.CANCELLED)
            if envelope.relationship is not SteeringRelationship.STOP:
                replacement = tasks.create(
                    project_id,
                    "agent.turn",
                    f"steering-replace:{envelope.decision_id}",
                    payload
                    | {
                        "content": body.content,
                        "parent_task_id": run_id,
                        "steering_decision_id": str(envelope.decision_id),
                        "provider_snapshot": provider_snapshot_payload,
                    },
                )
                replacement_id = replacement.id
                launch_agent_job(
                    project_id,
                    session_id,
                    replacement.id,
                    AgentJobCreate(
                        content=body.content,
                        provider_id=cast(str | None, payload.get("provider_id")),
                        approved=bool(payload.get("approved", False)),
                        idempotency_key=replacement.idempotency_key,
                    ),
                )
            message = conversations.add_message(
                project_id,
                session_id,
                "assistant",
                "原任务已保留并折叠。" + ("替代任务已启动。" if replacement_id else "任务已停止。"),
                run_id=replacement_id or run_id,
            )
            immediate_message = row_dict(
                message, "id", "session_id", "role", "content", "created_at"
            )

        if body.decision_id:
            steering_decisions.update_status(
                project_id,
                body.decision_id,
                "applied",
                replacement_task_id=replacement_id,
            )
        else:
            steering_decisions.create(
                project_id,
                envelope,
                status="applied",
                replacement_task_id=replacement_id,
            )
        return {
            "status": "applied",
            "envelope": envelope.model_dump(mode="json"),
            "impact": impact.model_dump(mode="json"),
            "message": immediate_message,
            "replacement_run_id": replacement_id,
            "target_run": task_response(project_id, run_id),
        }

    @app.get(
        "/api/projects/{project_id}/runs/{run_id}/steering",
        dependencies=[Depends(require_token)],
    )
    def list_run_steering(project_id: str, run_id: str) -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "target_task_id",
                "trigger_message_id",
                "status",
                "replacement_task_id",
                "created_at",
                "decided_at",
            )
            | {"envelope": json.loads(row.envelope_json)}
            for row in steering_decisions.list(project_id, run_id)
        ]

    @app.get(
        "/api/projects/{project_id}/agent/tasks/{task_id}/inspect",
        dependencies=[Depends(require_token)],
    )
    def inspect_agent_task(project_id: str, task_id: str) -> dict[str, object]:
        task = task_response(project_id, task_id)
        with databases.project_session(project_id) as session:
            event_rows = list(
                session.scalars(
                    select(EventRecord)
                    .where(EventRecord.task_id == task_id)
                    .order_by(EventRecord.sequence)
                )
            )
            approval_rows = list(
                session.scalars(select(ApprovalRecord).where(ApprovalRecord.task_id == task_id))
            )
        return {
            "task": task,
            "events": [
                {
                    "sequence": row.sequence,
                    "type": row.type,
                    "payload": json.loads(row.payload_json),
                    "created_at": row.created_at.isoformat(),
                }
                for row in event_rows
            ],
            "approvals": [
                row_dict(row, "id", "action", "status", "decided_at")
                | {"scope": json.loads(row.scope_json)}
                for row in approval_rows
            ],
        }

    @app.get(
        "/api/projects/{project_id}/runs/{run_id}",
        dependencies=[Depends(require_token)],
    )
    def get_run(project_id: str, run_id: str) -> dict[str, object]:
        try:
            return task_response(project_id, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error

    @app.get(
        "/api/projects/{project_id}/runs/{run_id}/events",
        dependencies=[Depends(require_token)],
    )
    def list_run_events(
        project_id: str, run_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[dict[str, object]]:
        try:
            tasks.get(project_id, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        return [
            {
                "id": row.event_id,
                "sequence": row.run_sequence,
                "type": row.type,
                "payload": json.loads(row.payload_json),
                "created_at": row.created_at.isoformat(),
            }
            for row in tasks.events_after(project_id, run_id, after_sequence, limit=limit)
        ]

    @app.get(
        "/api/projects/{project_id}/runs/{run_id}/events/stream",
        dependencies=[Depends(require_token)],
    )
    async def stream_run_events(
        project_id: str,
        run_id: str,
        request: Request,
        after_sequence: int = 0,
        follow: bool = True,
    ) -> StreamingResponse:
        try:
            tasks.get(project_id, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        header_cursor = int(request.headers.get("last-event-id", "0") or 0)

        async def generate() -> AsyncIterator[str]:
            cursor = max(after_sequence, header_cursor)
            idle_ticks = 0
            while not await request.is_disconnected():
                rows = tasks.events_after(project_id, run_id, cursor)
                for row in rows:
                    cursor = int(row.run_sequence or cursor)
                    yield (
                        f"id: {cursor}\nevent: {row.type}\n"
                        f"data: {json.dumps(json.loads(row.payload_json), ensure_ascii=False)}\n\n"
                    )
                if rows:
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks % 40 == 0:
                        yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                if not follow:
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/activity", dependencies=[Depends(require_token)])
    def list_activity() -> list[dict[str, object]]:
        activity: list[dict[str, object]] = []
        for project in projects.list():
            for row in tasks.list(project.id):
                if TaskStatus(row.status) in {
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                    TaskStatus.PAUSED,
                    TaskStatus.WAITING_APPROVAL,
                    TaskStatus.FAILED,
                } or (
                    TaskStatus(row.status)
                    in {
                        TaskStatus.COMPLETED,
                        TaskStatus.CANCELLED,
                        TaskStatus.SUPERSEDED,
                    }
                    and row.read_at is None
                ):
                    activity.append(
                        task_response(project.id, row.id)
                        | {"project_id": project.id, "project_name": project.name}
                    )
        return sorted(activity, key=lambda item: str(item["updated_at"]), reverse=True)

    @app.post(
        "/api/projects/{project_id}/runs/{run_id}/read",
        dependencies=[Depends(require_token)],
    )
    def mark_run_read(project_id: str, run_id: str) -> dict[str, object]:
        try:
            tasks.mark_read(project_id, run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        return task_response(project_id, run_id)

    @app.get("/api/runtime/resources", dependencies=[Depends(require_token)])
    def runtime_resources() -> dict[str, object]:
        return scheduler.resource_snapshot() | {"orphan_process_audit": orphan_process_audit}

    @app.put("/api/runtime/resources", dependencies=[Depends(require_token)])
    def configure_runtime_resources(body: ResourceLimits) -> dict[str, object]:
        try:
            scheduler.resources.configure(body)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        with databases.global_session() as session:
            row = session.get(AppSetting, "resource_limits")
            if row is None:
                row = AppSetting(key="resource_limits", value_json=body.model_dump_json())
                session.add(row)
            else:
                row.value_json = body.model_dump_json()
            session.commit()
        return scheduler.resource_snapshot()

    @app.get("/api/projects/{project_id}/events", dependencies=[Depends(require_token)])
    async def stream_events(project_id: str, request: Request) -> StreamingResponse:
        last_event_id = int(request.headers.get("last-event-id", "0") or 0)

        async def generate() -> AsyncIterator[str]:
            cursor = last_event_id
            while not await request.is_disconnected():
                rows = events.after(project_id, cursor)
                for row in rows:
                    cursor = row.sequence
                    yield (
                        f"id: {row.sequence}\nevent: {row.type}\n"
                        f"data: {json.dumps(json.loads(row.payload_json), ensure_ascii=False)}\n\n"
                    )
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/projects/{project_id}/tasks", dependencies=[Depends(require_token)])
    def create_task(project_id: str, body: TaskCreate) -> dict[str, object]:
        row = tasks.create(project_id, body.kind, body.idempotency_key, body.payload)
        return row_dict(row, "id", "kind", "status", "idempotency_key", "created_at", "updated_at")

    @app.patch("/api/projects/{project_id}/tasks/{task_id}", dependencies=[Depends(require_token)])
    def transition_task(project_id: str, task_id: str, body: TaskTransition) -> dict[str, object]:
        try:
            row = tasks.transition(project_id, task_id, body.status)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="task not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return row_dict(row, "id", "kind", "status", "idempotency_key", "created_at", "updated_at")

    @app.post(
        "/api/projects/{project_id}/tasks/{task_id}/approvals",
        dependencies=[Depends(require_token)],
    )
    def request_approval(project_id: str, task_id: str, body: ApprovalCreate) -> dict[str, object]:
        row = tasks.request_approval(project_id, task_id, body.action, body.scope)
        return row_dict(row, "id", "task_id", "action", "status", "decided_at")

    @app.patch(
        "/api/projects/{project_id}/approvals/{approval_id}",
        dependencies=[Depends(require_token)],
    )
    def decide_approval(
        project_id: str, approval_id: str, body: ApprovalDecision
    ) -> dict[str, object]:
        try:
            row = tasks.decide_approval(project_id, approval_id, approved=body.approved)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="approval not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return row_dict(row, "id", "task_id", "action", "status", "decided_at")

    @app.post("/api/backups/global", dependencies=[Depends(require_token)])
    def create_global_backup() -> dict[str, object]:
        return backups.create(databases.global_path).__dict__

    @app.get("/api/backups/{backup_id}/verify", dependencies=[Depends(require_token)])
    def verify_backup(backup_id: str) -> dict[str, object]:
        try:
            return backups.verify(backup_id).__dict__
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/api/settings/providers", dependencies=[Depends(require_token)])
    def list_provider_settings() -> list[dict[str, object]]:
        return [public_provider(config) for config in provider_registry.list(include_disabled=True)]

    @app.get("/api/settings/provider-bindings", dependencies=[Depends(require_token)])
    def list_provider_bindings() -> list[dict[str, object]]:
        return [
            row_dict(
                row,
                "id",
                "scope",
                "scope_id",
                "modality",
                "provider_id",
                "version",
                "updated_at",
            )
            for row in provider_registry.bindings()
        ]

    @app.post("/api/settings/providers", dependencies=[Depends(require_token)])
    def save_provider_settings(body: ProviderSave) -> dict[str, object]:
        if body.provider_type == "mock" and app_settings.environment != "test":
            raise HTTPException(status_code=422, detail="mock providers are test-only")
        existing = provider_registry.get(body.id, include_disabled=True)
        credential_ref = existing.credential_ref if existing else None
        secret_version = existing.secret_version if existing else 0
        if body.api_key:
            credential_ref = credentials.put(body.id, body.api_key)
            secret_version += 1
        config = ProviderConfig.model_validate(
            body.model_dump(exclude={"api_key", "version"})
            | {
                "display_name": body.display_name or body.id,
                "credential_ref": credential_ref,
                "secret_version": secret_version,
                "enabled": True,
                "health_status": existing.health_status if existing else ProviderHealth.UNKNOWN,
                "health_detail": existing.health_detail if existing else "",
                "version": existing.version if existing else 1,
            }
        )
        try:
            saved = provider_registry.save(config, expected_version=body.version)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not any(row.modality == saved.modality.value for row in provider_registry.bindings()):
            provider_registry.bind(saved.id)
        return public_provider(saved)

    @app.post(
        "/api/settings/providers/{provider_id}/activate",
        dependencies=[Depends(require_token)],
    )
    def activate_provider(provider_id: str, body: ProviderActivate) -> dict[str, object]:
        try:
            row = provider_registry.bind(
                provider_id,
                scope=body.scope,
                scope_id=body.scope_id,
                expected_version=body.expected_version,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="provider not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return row_dict(
            row, "id", "scope", "scope_id", "modality", "provider_id", "version", "updated_at"
        )

    @app.delete("/api/settings/providers/{provider_id}", dependencies=[Depends(require_token)])
    def disable_provider(
        provider_id: str, confirmation: str = "", expected_version: int | None = None
    ) -> dict[str, object]:
        if confirmation != "DISABLE PROVIDER":
            raise HTTPException(
                status_code=409,
                detail="explicit provider disable confirmation required",
            )
        try:
            disabled = provider_registry.deactivate(provider_id, expected_version=expected_version)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="provider not found") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return public_provider(disabled)

    @app.post("/api/providers/{provider_id}/test-stream", dependencies=[Depends(require_token)])
    def test_provider_stream(provider_id: str) -> StreamingResponse:
        config = provider_registry.get(provider_id)
        if config is None or config.modality is not ProviderModality.TEXT:
            raise HTTPException(status_code=404, detail="provider not found")
        provider = model_provider(config)

        async def real_chunks() -> AsyncIterator[str]:
            try:
                request = ChatRequest(
                    messages=[ChatMessage(role="user", content="Reply with OK only.")],
                    max_tokens=64,
                )
                index = 0
                async for chunk in provider.stream(request):
                    index += 1
                    yield (
                        f"id: {index}\nevent: token\n"
                        f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
                    )
            finally:
                client = getattr(provider, "client", None)
                if client is not None:
                    await client.aclose()

        return StreamingResponse(real_chunks(), media_type="text/event-stream")

    @app.post("/api/providers/{provider_id}/test", dependencies=[Depends(require_token)])
    async def test_provider(provider_id: str, body: ProviderTestRequest) -> dict[str, object]:
        del body
        config = provider_registry.get(provider_id)
        if config is None:
            raise HTTPException(status_code=404, detail="provider not found")
        started = time.perf_counter()
        response_text = ""
        model = config.model
        client: object | None = None
        try:
            if config.modality is ProviderModality.TEXT:
                text_provider = model_provider(config)
                client = getattr(text_provider, "client", None)
                response = await text_provider.chat(
                    ChatRequest(
                        messages=[ChatMessage(role="user", content="Reply with OK only.")],
                        max_tokens=64,
                    )
                )
                if not response.content.strip() and not response.tool_calls:
                    raise ValueError("provider returned an empty completion")
                response_text = (
                    response.content[:100]
                    if response.content.strip()
                    else f"tool_calls:{len(response.tool_calls)}"
                )
                model = response.model
            elif config.modality is ProviderModality.IMAGE:
                visual_provider = image_provider(config)
                client = visual_provider.client
                output = app_settings.resolved_data_dir / "global" / "health" / f"{uuid4()}.bin"
                artifact = await asyncio.to_thread(
                    visual_provider.generate,
                    ImageRequest(
                        prompt="A minimal blue circle on white background.",
                        width=256,
                        height=256,
                        approved=True,
                    ),
                    output,
                )
                response_text = artifact.provenance.sha256[:16]
                output.unlink(missing_ok=True)
            else:
                raise ValueError("embedding health probe is not implemented")
        except Exception as error:
            classified = classify_provider_error(error)
            provider_registry.record_health(provider_id, classified.status, classified.detail)
            raise HTTPException(
                status_code=502,
                detail=classified.model_dump(mode="json"),
            ) from error
        finally:
            if client is not None:
                close = getattr(client, "aclose", None) or getattr(client, "close", None)
                if close is not None:
                    closed = close()
                    if hasattr(closed, "__await__"):
                        await closed
        provider_registry.record_health(provider_id, ProviderHealth.HEALTHY, "probe passed")
        return {
            "provider_id": provider_id,
            "modality": config.modality.value,
            "model": model,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "response": response_text,
            "health_status": ProviderHealth.HEALTHY.value,
        }

    @app.post(
        "/api/projects/{project_id}/images/generate",
        dependencies=[Depends(require_token)],
    )
    async def generate_project_image(
        project_id: str, body: ImageGenerateCreate
    ) -> dict[str, object]:
        if not body.approved:
            raise HTTPException(status_code=409, detail="image generation approval is required")
        config = (
            provider_registry.get(body.provider_id)
            if body.provider_id
            else provider_registry.active(ProviderModality.IMAGE, project_id=project_id)
        )
        if config is None or config.modality is not ProviderModality.IMAGE:
            raise HTTPException(status_code=409, detail="no active image provider is configured")
        if not credentials.has(config.credential_ref):
            raise HTTPException(status_code=409, detail="image provider credential is missing")
        provider = image_provider(config)
        output = (
            databases.project_root(project_id) / "artifacts" / "generated-images" / f"{uuid4()}.png"
        )
        try:
            artifact = await asyncio.to_thread(
                provider.generate,
                ImageRequest(
                    prompt=body.prompt,
                    width=body.width,
                    height=body.height,
                    approved=True,
                ),
                output,
            )
        finally:
            provider.client.close()
        return artifact.model_dump(mode="json") | {
            "provider_snapshot": config.model_dump(mode="json", exclude={"credential_ref", "extra"})
        }

    @app.get("/api/settings/privacy", dependencies=[Depends(require_token)])
    def get_privacy() -> dict[str, object]:
        return {"mode": provider_registry.get_setting("privacy_mode", "standard")}

    @app.put("/api/settings/privacy", dependencies=[Depends(require_token)])
    def set_privacy(body: PrivacySave) -> dict[str, str]:
        provider_registry.set_setting("privacy_mode", body.mode)
        return {"mode": body.mode}

    @app.post("/api/memories", dependencies=[Depends(require_token)])
    def create_memory(body: MemoryCreate) -> dict[str, object]:
        try:
            row = memories.remember(**body.model_dump())
        except (PermissionError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return row_dict(row, "id", "scope", "project_id", "kind", "content", "source", "created_at")

    @app.get("/api/memories", dependencies=[Depends(require_token)])
    def list_memories(scope: str, project_id: str | None = None) -> list[dict[str, object]]:
        return [
            row_dict(row, "id", "scope", "project_id", "kind", "content", "source", "created_at")
            for row in memories.list(scope=scope, project_id=project_id)
        ]

    @app.patch("/api/memories/{memory_id}", dependencies=[Depends(require_token)])
    def update_memory(memory_id: str, body: MemoryUpdate) -> dict[str, object]:
        try:
            row = memories.update(memory_id, body.content)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="memory not found") from error
        return row_dict(row, "id", "scope", "project_id", "kind", "content", "source", "updated_at")

    @app.get("/api/memories/export", dependencies=[Depends(require_token)])
    def export_memories(scope: str, project_id: str | None = None) -> dict[str, object]:
        items = [
            row_dict(row, "id", "scope", "project_id", "kind", "content", "source", "created_at")
            for row in memories.list(scope=scope, project_id=project_id)
        ]
        return {"schema_version": "1.0", "items": items}

    @app.post("/api/memories/clear", dependencies=[Depends(require_token)])
    def clear_memories(body: MemoryClear) -> dict[str, int]:
        try:
            count = memories.clear(**body.model_dump())
        except PermissionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"cleared": count}

    @app.post(
        "/api/projects/{project_id}/knowledge/import",
        dependencies=[Depends(require_token)],
        status_code=201,
    )
    async def import_knowledge(
        project_id: str,
        file: Annotated[UploadFile, File()],
        collection_id: Annotated[str, Form()] = "project",
        confidentiality: Annotated[str, Form()] = "personal",
    ) -> dict[str, object]:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=422, detail="empty upload")
        project_root = databases.project_root(project_id)
        digest = hashlib.sha256(content).hexdigest()
        with databases.project_session(project_id) as session:
            duplicate = session.scalar(select(FileRecord).where(FileRecord.sha256 == digest))
            if duplicate:
                return {
                    "source_id": None,
                    "file_id": duplicate.id,
                    "classification": None,
                    "warnings": ["Duplicate source already imported"],
                    "indexed": 0,
                    "duplicate": True,
                    "items": [],
                }
        stored = ProjectFileStore(project_root).write(
            category="sources",
            name=file.filename or "upload.bin",
            content=content,
            provenance={"origin": "user-upload"},
        )
        with databases.project_session(project_id) as session:
            session.add(
                FileRecord(
                    id=stored.file_id,
                    category="sources",
                    original_name=stored.original_name,
                    relative_path=stored.relative_path,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    provenance_json=json.dumps(stored.provenance),
                )
            )
            session.commit()
        path = ProjectFileStore(project_root).resolve(stored.relative_path)
        try:
            report = default_registry().import_file(path)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        classification_result = DocumentClassifier().classify(report.source)
        try:
            confidentiality_value = Confidentiality(confidentiality)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid confidentiality") from error
        items = report_to_items(
            report,
            classification_result,
            collection_id=collection_id,
            scope=KnowledgeScope.PROJECT,
            project_id=project_id,
            confidentiality=confidentiality_value,
        )
        service = ProjectKnowledgeService(project_root)
        indexed = service.ingest(items)
        return {
            "source_id": str(report.source.id),
            "file_id": stored.file_id,
            "classification": classification_result.primary_type,
            "warnings": report.warnings,
            "indexed": indexed,
            "items": [item.model_dump(mode="json") for item in items],
        }

    @app.get("/api/projects/{project_id}/knowledge/search", dependencies=[Depends(require_token)])
    def search_knowledge(project_id: str, q: str, limit: int = 20) -> dict[str, object]:
        service = ProjectKnowledgeService(databases.project_root(project_id))
        hits = service.search(q, project_id=project_id, limit=min(max(limit, 1), 100))
        return {
            "query": q,
            "hits": [
                hit.__dict__
                | {"retrieval_reason": "SQLite FTS5/BM25 keyword match with project filter"}
                for hit in hits
            ],
        }

    @app.get(
        "/api/projects/{project_id}/knowledge/collections", dependencies=[Depends(require_token)]
    )
    def knowledge_collections(project_id: str) -> list[dict[str, object]]:
        return ProjectKnowledgeService(databases.project_root(project_id)).index.collection_stats()

    @app.get("/api/knowledge/embedding-models", dependencies=[Depends(require_token)])
    def embedding_models() -> list[dict[str, object]]:
        store = EmbeddingModelStore(app_settings.resolved_data_dir / "models" / "embeddings")
        return [store.status(model_id) for model_id in MODEL_MANIFESTS]

    @app.post("/api/projects/{project_id}/knowledge/rebuild", dependencies=[Depends(require_token)])
    def rebuild_knowledge(project_id: str) -> dict[str, int]:
        count = ProjectKnowledgeService(databases.project_root(project_id)).index.rebuild()
        return {"indexed": count}

    @app.delete(
        "/api/projects/{project_id}/knowledge/{item_id}", dependencies=[Depends(require_token)]
    )
    def delete_knowledge(project_id: str, item_id: str, confirmation: str = "") -> dict[str, bool]:
        if confirmation != "DELETE KNOWLEDGE":
            raise HTTPException(status_code=409, detail="explicit confirmation required")
        ProjectKnowledgeService(databases.project_root(project_id)).delete(item_id)
        return {"deleted": True}

    @app.patch(
        "/api/projects/{project_id}/knowledge/{item_id}/classification",
        dependencies=[Depends(require_token)],
    )
    def override_knowledge_classification(
        project_id: str, item_id: str, body: ClassificationOverride
    ) -> dict[str, str]:
        try:
            ProjectKnowledgeService(
                databases.project_root(project_id)
            ).index.override_classification(item_id, body.content_type)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="knowledge item not found") from error
        return {"item_id": item_id, "content_type": body.content_type}

    @app.get("/api/projects/{project_id}/files", dependencies=[Depends(require_token)])
    def list_project_files(project_id: str) -> list[dict[str, object]]:
        with databases.project_session(project_id) as session:
            records = session.scalars(
                select(FileRecord).order_by(FileRecord.created_at.desc())
            ).all()
            return [
                row_dict(
                    record,
                    "id",
                    "category",
                    "original_name",
                    "sha256",
                    "size_bytes",
                    "created_at",
                )
                for record in records
            ]

    @app.get("/api/projects/{project_id}/artifacts", dependencies=[Depends(require_token)])
    def list_project_artifacts(
        project_id: str,
        run_id: str | None = None,
        include_rejected: bool = False,
    ) -> list[dict[str, object]]:
        service = ArtifactService(databases, project_id)
        with databases.project_session(project_id) as session:
            query = select(ArtifactRecord).order_by(ArtifactRecord.created_at.desc())
            if run_id is not None:
                query = query.where(ArtifactRecord.run_id == run_id)
            if not include_rejected:
                query = query.where(ArtifactRecord.delivery_status != "rejected")
            return [service.payload(row) for row in session.scalars(query).all()]

    @app.get(
        "/api/projects/{project_id}/artifacts/lookup",
        dependencies=[Depends(require_token)],
    )
    def lookup_project_artifacts(
        project_id: str,
        conversation_id: str,
        relation: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, object]]:
        return ArtifactService(databases, project_id).lookup(
            conversation_id=conversation_id,
            relation=relation,
            run_id=run_id,
        )

    def artifact_response(project_id: str, artifact_id: str, *, disposition: str) -> FileResponse:
        service = ArtifactService(databases, project_id)
        try:
            artifact = service.get(artifact_id)
            path = service.verify(artifact)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="artifact not found") from error
        except ArtifactIntegrityError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
            ),
            "Accept-Ranges": "bytes",
        }
        if disposition == "attachment":
            events.append(
                project_id,
                "artifact.downloaded",
                {"artifact_id": artifact.id, "sha256": artifact.sha256},
            )
        return FileResponse(
            path,
            filename=artifact.original_name,
            media_type=artifact.mime_type,
            content_disposition_type=disposition,
            headers=headers,
        )

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}/preview",
        dependencies=[Depends(require_token)],
    )
    def preview_artifact(project_id: str, artifact_id: str) -> FileResponse:
        return artifact_response(project_id, artifact_id, disposition="inline")

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}/asset",
        dependencies=[Depends(require_token)],
    )
    def preview_artifact_asset(project_id: str, artifact_id: str, path: str) -> FileResponse:
        artifacts = ArtifactService(databases, project_id)
        try:
            source = artifacts.verify(artifacts.get(artifact_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="artifact not found") from error
        candidate = (source.parent / path).resolve()
        if source.parent != candidate.parent and source.parent not in candidate.parents:
            raise HTTPException(status_code=403, detail="asset path escapes the artifact bundle")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="artifact asset not found")
        return FileResponse(
            candidate,
            media_type=mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}/preview-pdf",
        dependencies=[Depends(require_token)],
    )
    def preview_docx_as_pdf(project_id: str, artifact_id: str) -> FileResponse:
        artifacts = ArtifactService(databases, project_id)
        try:
            artifact = artifacts.get(artifact_id)
            source = artifacts.verify(artifact)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="artifact not found") from error
        if source.suffix.casefold() != ".docx":
            raise HTTPException(
                status_code=422, detail="page conversion is only available for DOCX"
            )
        result = DocxPagePreviewService(databases.project_root(project_id)).convert(
            source, artifact.sha256
        )
        if not result.success or result.path is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": result.error_code,
                    "engine": result.engine,
                    "message": result.message,
                    "fallback": "structured",
                },
            )
        return FileResponse(
            result.path,
            filename=f"{source.stem}.preview.pdf",
            media_type="application/pdf",
            content_disposition_type="inline",
            headers={
                "X-PaperAgent-Preview-Engine": result.engine,
                "X-PaperAgent-Preview-Cache": result.cache_key,
            },
        )

    @app.post(
        "/api/projects/{project_id}/artifacts/{artifact_id}/structured-preview",
        dependencies=[Depends(require_token)],
    )
    def render_artifact_preview(project_id: str, artifact_id: str) -> dict[str, object]:
        artifacts = ArtifactService(databases, project_id)
        try:
            artifact = artifacts.get(artifact_id)
            path = artifacts.verify(artifact)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="artifact not found") from error
        except ArtifactIntegrityError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        service = PreviewService(databases.project_root(project_id))
        document_revision: int | None = None
        if artifact.revision_id:
            with databases.project_session(project_id) as session:
                revision_record = session.get(DocumentRevisionRecord, artifact.revision_id)
                if revision_record is not None:
                    document_revision = revision_record.revision_number
        try:
            preview = service.render(
                path,
                file_id=artifact.id,
                source_hash=artifact.sha256,
                source_name=artifact.original_name,
                options={
                    "artifact_id": artifact.id,
                    "raw_url": (f"/api/projects/{project_id}/artifacts/{artifact.id}/preview"),
                    "provenance": {
                        "document_id": artifact.document_id,
                        "revision_id": artifact.revision_id,
                        "document_revision": document_revision,
                    },
                },
            )
            return preview.model_dump(mode="json")
        finally:
            service.close()

    @app.get(
        "/api/projects/{project_id}/artifacts/{artifact_id}/download",
        dependencies=[Depends(require_token)],
    )
    def download_artifact(project_id: str, artifact_id: str) -> FileResponse:
        return artifact_response(project_id, artifact_id, disposition="attachment")

    @app.post(
        "/api/projects/{project_id}/preview/{file_id}",
        dependencies=[Depends(require_token)],
    )
    def render_preview(project_id: str, file_id: str) -> dict[str, object]:
        record = project_file(project_id, file_id)
        project_root = databases.project_root(project_id)
        path = ProjectFileStore(project_root).resolve(record.relative_path)
        service = PreviewService(project_root)
        try:
            artifact = service.render(
                path,
                file_id=record.id,
                source_hash=record.sha256,
                source_name=record.original_name,
                options={"provenance": json.loads(record.provenance_json)},
            )
            return artifact.model_dump(mode="json")
        finally:
            service.close()

    @app.get(
        "/api/projects/{project_id}/preview/artifacts/{artifact_id}",
        dependencies=[Depends(require_token)],
    )
    def get_preview(project_id: str, artifact_id: str) -> dict[str, object]:
        service = PreviewService(databases.project_root(project_id))
        try:
            return service.artifact(artifact_id).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        finally:
            service.close()

    @app.get(
        "/api/projects/{project_id}/preview/artifacts/{artifact_id}/parts",
        dependencies=[Depends(require_token)],
    )
    def get_preview_parts(
        project_id: str, artifact_id: str, offset: int = 0, limit: int = 100
    ) -> dict[str, object]:
        service = PreviewService(databases.project_root(project_id))
        try:
            parts = service.parts(artifact_id, offset=offset, limit=limit)
            return {
                "offset": max(offset, 0),
                "limit": min(max(limit, 1), 500),
                "parts": [part.model_dump(mode="json") for part in parts],
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        finally:
            service.close()

    @app.get(
        "/api/projects/{project_id}/files/{file_id}/raw",
        dependencies=[Depends(require_token)],
    )
    def raw_project_file(project_id: str, file_id: str) -> FileResponse:
        record = project_file(project_id, file_id)
        path = ProjectFileStore(databases.project_root(project_id)).resolve(record.relative_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="project file content not found")
        return FileResponse(
            path,
            filename=record.original_name,
            content_disposition_type="inline",
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    @app.post(
        "/api/projects/{project_id}/preview/artifacts/{artifact_id}/annotations",
        dependencies=[Depends(require_token)],
        status_code=201,
    )
    def create_preview_annotation(
        project_id: str, artifact_id: str, body: PreviewAnnotationCreate
    ) -> dict[str, object]:
        service = PreviewService(databases.project_root(project_id))
        try:
            annotation = service.annotate(
                Annotation(
                    project_id=project_id,
                    artifact_id=UUID(artifact_id),
                    anchor=body.anchor,
                    body=body.body,
                )
            )
            return annotation.model_dump(mode="json")
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            service.close()

    @app.get(
        "/api/projects/{project_id}/files/{file_id}/annotations",
        dependencies=[Depends(require_token)],
    )
    def list_preview_annotations(project_id: str, file_id: str) -> list[dict[str, object]]:
        record = project_file(project_id, file_id)
        service = PreviewService(databases.project_root(project_id))
        try:
            return [
                annotation.model_dump(mode="json")
                for annotation in service.annotations(file_id, record.sha256)
            ]
        finally:
            service.close()

    @app.post(
        "/api/projects/{project_id}/preview/artifacts/{artifact_id}/selection",
        dependencies=[Depends(require_token)],
    )
    def preview_selection(
        project_id: str, artifact_id: str, body: PreviewSelectionCreate
    ) -> dict[str, object]:
        service = PreviewService(databases.project_root(project_id))
        try:
            artifact = service.artifact(artifact_id)
            if body.anchor.source_file_id != artifact.source_file_id:
                raise HTTPException(status_code=422, detail="selection source mismatch")
            return {
                "action": body.action,
                "text": body.text,
                "anchor": body.anchor.model_dump(mode="json"),
                "source_name": artifact.source_name,
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        finally:
            service.close()

    @app.put(
        "/api/projects/{project_id}/documents/{document_id}",
        dependencies=[Depends(require_token)],
    )
    def save_document_ir(
        project_id: str, document_id: str, document: DocumentIR
    ) -> dict[str, object]:
        if str(document.document_id) != document_id:
            raise HTTPException(status_code=422, detail="document id does not match payload")
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        path = store.save(
            document,
            source_conversation_id=str(document.metadata.get("source_conversation_id") or "")
            or None,
            source_message_id=str(document.metadata.get("source_message_id") or "") or None,
            source_run_id=str(document.metadata.get("source_run_id") or "") or None,
        )
        return {
            "document_id": document_id,
            "revision": document.revision,
            "revision_id": store.revision_id(document.document_id, document.revision),
            "hashes": document.hashes().model_dump(mode="json"),
            "path": path.relative_to(databases.project_root(project_id)).as_posix(),
        }

    @app.get(
        "/api/projects/{project_id}/documents/{document_id}",
        dependencies=[Depends(require_token)],
    )
    def get_document_ir(
        project_id: str, document_id: UUID, revision: int | None = None
    ) -> dict[str, object]:
        try:
            document = DocumentRevisionStore(databases.project_root(project_id)).load(
                document_id, revision
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document revision not found") from error
        return document.canonical_payload()

    def presentation_summary(
        store: DocumentRevisionStore,
        document: DocumentIR,
    ) -> dict[str, object]:
        view = RenderPresentationViewModel.from_document(document)
        hashes = document.hashes()
        return {
            "document_id": str(document.document_id),
            "revision": document.revision,
            "revision_id": store.revision_id(document.document_id, document.revision),
            "presentation_hash": hashes.presentation_hash,
            "numbering_hash": hashes.numbering_hash,
            "content_hash": hashes.content_hash,
            "asset_set_hash": hashes.asset_set_hash,
            "presentation": view.semantic_snapshot(),
            "source_map": {
                "profile_id": document.presentation.source_profile_id,
                "template_id": document.presentation.source_template_id,
                "fields": {
                    item.semantic_key: {
                        "source": item.provenance.source.value,
                        "source_ref": item.provenance.source_ref,
                    }
                    for item in document.presentation.cover.fields
                },
            },
            "diagnostics": {
                "md": {
                    "cover": "semantic",
                    "page_chrome": "preview_only",
                },
                "docx": {"cover": "native", "page_chrome": "native"},
                "pdf": {"cover": "native", "page_chrome": "native"},
            },
            "impact": {
                "rewrites_content": False,
                "reruns_experiment": False,
                "reruns_assets": False,
            },
        }

    @app.get(
        "/api/projects/{project_id}/documents/{document_id}/presentation",
        dependencies=[Depends(require_token)],
    )
    def get_document_presentation(
        project_id: str,
        document_id: UUID,
        revision: int | None = None,
    ) -> dict[str, object]:
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        try:
            document = store.load(document_id, revision)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document revision not found") from error
        return presentation_summary(store, document)

    @app.get(
        "/api/projects/{project_id}/documents/{document_id}/presentation-preview",
        dependencies=[Depends(require_token)],
    )
    def get_document_presentation_preview(
        project_id: str,
        document_id: UUID,
        revision: int | None = None,
    ) -> dict[str, object]:
        store = DocumentRevisionStore(databases.project_root(project_id))
        try:
            document = store.load(document_id, revision)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document revision not found") from error
        output = (
            databases.project_root(project_id)
            / ".paperagent"
            / "presentation-preview"
            / f"{document.document_id}-r{document.revision}.html"
        )
        MarkdownRenderer().render_html_preview(document, output)
        return {
            "document_id": str(document.document_id),
            "revision": document.revision,
            "presentation_hash": document.hashes().presentation_hash,
            "numbering_hash": document.hashes().numbering_hash,
            "html": output.read_text(encoding="utf-8"),
        }

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/presentation/patch",
        dependencies=[Depends(require_token)],
    )
    def patch_document_presentation(
        project_id: str,
        document_id: UUID,
        body: PresentationPatchCreate,
    ) -> dict[str, object]:
        invalid_formats = [
            item for item in body.formats if item not in {"md", "md_bundle", "docx", "pdf"}
        ]
        if invalid_formats:
            raise HTTPException(
                status_code=422,
                detail={"code": "PRESENTATION_FORMAT_INVALID", "formats": invalid_formats},
            )
        project_root = databases.project_root(project_id)
        artifacts = ArtifactService(databases, project_id)
        suite = ExecutionToolSuite(
            data_root=app_settings.resolved_data_dir,
            project_root=project_root,
            run_id=f"presentation-ui-{uuid4()}",
            uv_path=None,
            artifact_service=artifacts,
        )
        try:
            latest = suite.document_pipeline.store.load(document_id)
            if latest.revision != body.revision:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PRESENTATION_REVISION_CONFLICT",
                        "expected_revision": body.revision,
                        "current_revision": latest.revision,
                    },
                )
            patched = suite.document_pipeline.presentation_patch(
                {
                    "document_id": str(document_id),
                    "revision": body.revision,
                    "operations": [item.model_dump(mode="json") for item in body.operations],
                    "requested_formats": cast(list[JsonValue], body.formats),
                }
            )
            if not isinstance(patched, dict) or not isinstance(
                patched.get("document_ir"), dict
            ):
                raise RuntimeError("presentation patch returned no canonical revision")
            canonical = cast(dict[str, JsonValue], patched["document_ir"])
            outputs: list[dict[str, object]] = []
            render_errors: dict[str, dict[str, str]] = {}
            for format_name in body.formats:
                try:
                    rendered = suite.document_render(
                        {
                            "document_id": str(document_id),
                            "revision": int(str(canonical["revision"])),
                            "format": format_name,
                        }
                    )
                    if isinstance(rendered, dict):
                        outputs.append(cast(dict[str, object], rendered))
                except Exception:
                    # The canonical presentation revision is already durable.  A
                    # renderer failure must not make the client pretend that the
                    # revision rolled back, and raw exceptions may contain local
                    # paths.  Return an actionable, redacted per-format diagnostic.
                    render_errors[format_name] = {
                        "code": "PRESENTATION_RENDER_FAILED",
                        "action": "retry_render",
                    }
            document = suite.document_pipeline.store.load(
                document_id,
                int(str(canonical["revision"])),
            )
            events.append(
                project_id,
                "document.presentation_revised",
                {
                    "document_id": str(document_id),
                    "revision": document.revision,
                    "field_count": len(document.presentation.cover.fields),
                    "operation_count": len(body.operations),
                    "formats": body.formats,
                    "presentation_hash": document.hashes().presentation_hash,
                    "numbering_hash": document.hashes().numbering_hash,
                },
            )
            return {
                "summary": presentation_summary(suite.document_pipeline.store, document),
                "artifacts": outputs,
                "rerendered_formats": body.formats,
                "render_errors": render_errors,
            }
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document revision not found") from error
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "PRESENTATION_PATCH_INVALID", "message": str(error)},
            ) from error
        finally:
            suite.close()

    @app.get(
        "/api/projects/{project_id}/documents/{document_id}/lineage",
        dependencies=[Depends(require_token)],
    )
    def get_document_lineage(project_id: str, document_id: UUID) -> list[dict[str, object]]:
        store = DocumentRevisionStore(databases.project_root(project_id))
        try:
            return [item.model_dump(mode="json") for item in store.list_lineage(document_id)]
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/branch",
        dependencies=[Depends(require_token)],
    )
    def branch_document(project_id: str, document_id: UUID, revision: int) -> dict[str, object]:
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        try:
            branch = store.branch(document_id, revision)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        path = store.save(
            branch,
            parent_revision_id=store.revision_id(document_id, revision),
            source_conversation_id=str(branch.metadata.get("source_conversation_id") or "") or None,
        )
        return {
            "document": branch.canonical_payload(),
            "revision_id": store.revision_id(branch.document_id, branch.revision),
            "path": path.relative_to(databases.project_root(project_id)).as_posix(),
        }

    @app.get(
        "/api/projects/{project_id}/document-revisions/latest",
        dependencies=[Depends(require_token)],
    )
    def latest_document_revision(
        project_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, object]:
        try:
            document = DocumentRevisionStore(databases.project_root(project_id)).latest(
                source_conversation_id=conversation_id
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return document.canonical_payload()

    @app.post(
        "/api/projects/{project_id}/document-revisions/resolve",
        dependencies=[Depends(require_token)],
    )
    def resolve_document_revision(
        project_id: str, body: RevisionResolveCreate
    ) -> dict[str, object]:
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        try:
            result = RevisionResolver(store).resolve(
                body.reference,
                document_id=body.document_id,
                revision=body.revision,
                conversation_id=body.conversation_id,
                artifact_id=body.artifact_id,
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return result.model_dump(mode="json")

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/resolve-target",
        dependencies=[Depends(require_token)],
    )
    def resolve_document_target(
        project_id: str, document_id: UUID, body: TargetResolveCreate
    ) -> dict[str, object]:
        store = DocumentRevisionStore(databases.project_root(project_id))
        try:
            document = store.load(document_id, body.revision)
            result = TargetResolver().resolve(
                document,
                body.request,
                section_id=body.section_id,
                block_id=body.block_id,
            )
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=422, detail=f"unknown target: {error}") from error
        return result.model_dump(mode="json")

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/revisions",
        dependencies=[Depends(require_token)],
    )
    def revise_document(
        project_id: str, document_id: UUID, body: DocumentRevisionCreate
    ) -> dict[str, object]:
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        try:
            result = RevisionWorkflow(store).apply(store.load(document_id), body.operation)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return result.model_dump(mode="json")

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/rollback",
        dependencies=[Depends(require_token)],
    )
    def rollback_document(project_id: str, document_id: UUID, revision: int) -> dict[str, object]:
        store = DocumentRevisionStore(
            databases.project_root(project_id),
            databases=databases,
            project_id=project_id,
            artifact_service=ArtifactService(databases, project_id),
        )
        try:
            restored = store.rollback(document_id, revision)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {
            "document": restored.canonical_payload(),
            "revision_id": store.revision_id(document_id, restored.revision),
        }

    def apply_typography_revision(
        project_id: str,
        document: DocumentIR,
        intent: ChangeIntent,
        formats: list[str],
        allow_fallback: bool,
    ) -> dict[str, object]:
        try:
            result = TargetedTypographyService(databases.project_root(project_id)).apply(
                document,
                intent,
                formats=formats,
                allow_fallback=allow_fallback,
            )
        except MissingFontError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "FONT_ACTION_REQUIRED",
                    "fonts": [item.model_dump(mode="json") for item in error.resolutions],
                },
            ) from error
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        files = register_rendered_files(project_id, result.document.revision, result.artifacts)
        visual_diff_files = register_visual_diff_files(
            project_id,
            result.document.document_id,
            result.document.revision,
            result.visual_diff,
        )
        events.append(
            project_id,
            "document.typography_revised",
            {
                "document_id": str(document.document_id),
                "from_revision": document.revision,
                "to_revision": result.document.revision,
                "affected_sections": [str(item) for item in result.impact.affected_sections],
                "affected_blocks": [str(item) for item in result.impact.affected_blocks],
                "artifacts": [str(item.artifact_id) for item in result.artifacts],
            },
        )
        return result.model_dump(mode="json") | {
            "files": files,
            "visual_diff_files": visual_diff_files,
        }

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/typography",
        dependencies=[Depends(require_token)],
    )
    def revise_document_typography(
        project_id: str, document_id: UUID, body: TypographyChangeCreate
    ) -> dict[str, object]:
        try:
            document = DocumentRevisionStore(databases.project_root(project_id)).load(document_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document not found") from error
        return apply_typography_revision(
            project_id,
            document,
            body.intent,
            body.formats,
            body.allow_fallback,
        )

    @app.post(
        "/api/projects/{project_id}/documents/{document_id}/typography/from-annotation",
        dependencies=[Depends(require_token)],
    )
    async def revise_typography_from_annotation(
        project_id: str,
        document_id: UUID,
        body: AnnotationTypographyChangeCreate,
    ) -> dict[str, object]:
        try:
            document = DocumentRevisionStore(databases.project_root(project_id)).load(document_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail="document not found") from error
        provider: ModelProvider | None = None
        configured = next(
            (
                config
                for config in provider_registry.list(modality=ProviderModality.TEXT)
                if (body.provider_id is None or config.id == body.provider_id)
                and Capability.STRUCTURED_OUTPUT in config.capabilities
                and (config.provider_type != "mock" or app_settings.environment == "test")
            ),
            None,
        )
        if configured is not None:
            provider = model_provider(configured)
        try:
            intent = await ChangeIntentAgent(provider).understand(body.body)
        finally:
            client = getattr(provider, "client", None)
            if client is not None:
                await client.aclose()
        block_id = body.block_id
        section_id: UUID | None = None
        if block_id is None and body.anchor.quote:
            matching_blocks = [
                block.block_id
                for section in document.sections
                for block in section.blocks
                if body.anchor.quote.strip() in block.text
            ]
            matching_sections = [
                section.section_id
                for section in document.sections
                if body.anchor.quote.strip() in section.title
            ]
            if len(matching_blocks) == 1:
                block_id = matching_blocks[0]
            elif not matching_blocks and len(matching_sections) == 1:
                section_id = matching_sections[0]
            else:
                raise HTTPException(
                    status_code=422,
                    detail="preview quote cannot be mapped to one stable Document IR target",
                )
        if block_id is not None:
            intent = intent.model_copy(
                update={
                    "scope": ChangeScope.BLOCK,
                    "block_ids": [block_id],
                    "section_ids": [],
                }
            )
        elif section_id is not None:
            intent = intent.model_copy(
                update={
                    "scope": ChangeScope.SECTION,
                    "section_ids": [section_id],
                    "block_ids": [],
                }
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="annotation requires a block id or an unambiguous quote anchor",
            )
        intent = ChangeIntent.model_validate(intent.model_dump())
        return apply_typography_revision(
            project_id,
            document,
            intent,
            body.formats,
            body.allow_fallback,
        )

    @app.delete("/api/projects/{project_id}/preview/cache", dependencies=[Depends(require_token)])
    def clear_preview_cache(project_id: str) -> dict[str, int]:
        service = PreviewService(databases.project_root(project_id))
        try:
            return {"cleared": service.clear_cache()}
        finally:
            service.close()

    @app.post(
        "/api/projects/{project_id}/requirements/analyze",
        dependencies=[Depends(require_token)],
    )
    async def analyze_requirement(
        project_id: str, body: RequirementAnalyzeCreate
    ) -> dict[str, object]:
        databases.project_root(project_id).mkdir(parents=True, exist_ok=True)
        text = body.text.strip()
        lowered = text.casefold()
        typography, _typography_fields = extract_typography(text)
        document_type = next(
            (
                value
                for keyword, value in (
                    ("实验报告", "experiment_report"),
                    ("实践报告", "practice_report"),
                    ("调查报告", "survey_report"),
                    ("课题报告", "project_report"),
                    ("项目报告", "project_report"),
                    ("论文", "academic_paper"),
                )
                if keyword in text
            ),
            None,
        )
        length_match = re.search(r"(\d{2,7})\s*(?:字|词|words?)", text, re.IGNORECASE)
        target_length = None
        if length_match:
            unit = (
                "english_word"
                if re.search(r"词|words?", length_match.group(0), re.I)
                else "chinese_char"
            )
            target_length = {"value": int(length_match.group(1)), "unit": unit}
        formats = [item for item in ("docx", "pdf", "md", "typst", "latex") if item in lowered]
        raw = RawRequest(
            text=text,
            message_ids=tuple(body.message_ids) or ("$api",),
            attachment_ids=tuple(body.attachment_ids),
        )
        candidate_values: dict[str, object] = {
            "normalized_request": text,
            "research_formulation": {"research_topic": text if len(text) >= 4 else None},
            "document_type": document_type,
            "primary_language": "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en",
            "target_length": target_length,
            "audience": "未指定",
            "citation_style": "未指定",
            "typography": typography.model_dump(mode="json"),
            "requires_literature_search": "文献" in text or "论文" in text,
            "requires_experiment": "实验" in text,
            "requires_data_chart": "数据" in text or "图表" in text,
            "requires_generated_image": "现场图" in text or "示意图" in text,
            "output_formats": formats,
        }
        evidence_paths = {
            "normalized_request",
            "research_formulation.research_topic",
            "document_type",
            "primary_language",
            "target_length",
            "output_formats",
        }
        if typography.configured:
            evidence_paths.add("typography")
        candidate_values["field_evidence"] = {
            path: FieldEvidence(
                source_type=EvidenceSource.EXPLICIT_USER,
                source_refs=["$raw"],
                confidence=0.95,
            ).model_dump(mode="json")
            for path in evidence_paths
            if (
                (path == "research_formulation.research_topic" and len(text) >= 4)
                or (
                    path != "research_formulation.research_topic"
                    and candidate_values.get(path) not in (None, [], "")
                )
            )
        }
        candidate = RequirementCandidate.model_validate(candidate_values)
        spec = RequirementSpec.model_validate(
            {"raw_request": raw.model_dump(), **candidate.model_dump(exclude_none=True)}
        )
        provider_config = next(
            (
                config
                for config in provider_registry.list(modality=ProviderModality.TEXT)
                if config.provider_type != "mock"
                and Capability.STRUCTURED_OUTPUT in config.capabilities
            ),
            None,
        )
        if provider_config is None:
            validated = RequirementValidator().evaluate(spec)
        else:
            provider = model_provider(provider_config)
            try:
                validated = await RequirementUnderstandingAgent(provider).understand(raw)
            except Exception as error:
                events.append(
                    project_id,
                    "requirement.model_fallback",
                    {"provider_id": provider_config.id, "error": str(error)[:500]},
                )
                validated = RequirementValidator().evaluate(spec)
            finally:
                client = getattr(provider, "client", None)
                if client is not None:
                    await client.aclose()
        return {
            "requirement": validated.model_dump(mode="json"),
            "plan_preview": [item.model_dump(mode="json") for item in plan_preview(validated)],
        }

    @app.post(
        "/api/projects/{project_id}/requirements/confirm",
        dependencies=[Depends(require_token)],
    )
    def confirm_requirement(project_id: str, body: RequirementConfirmCreate) -> dict[str, object]:
        databases.project_root(project_id).mkdir(parents=True, exist_ok=True)
        candidate = body.requirement.model_copy(update={"open_questions": [], "conflicts": []})
        try:
            confirmed = candidate.confirm()
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        outline = OutlineDesignerAgent().design(confirmed)
        return {
            "requirement": confirmed.model_dump(mode="json"),
            "outline": outline.model_dump(mode="json"),
            "plan_preview": [item.model_dump(mode="json") for item in plan_preview(confirmed)],
        }

    @app.get("/api/recovery", dependencies=[Depends(require_token)])
    def recovery_center(
        project_id: str | None = None, task_id: str | None = None
    ) -> dict[str, object]:
        trace_id: str | None = None
        task_completed = False
        if project_id is not None:
            databases.project_root(project_id)
        if task_id is not None:
            if project_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="task-scoped recovery requires project_id",
                )
            try:
                task_row = tasks.get(project_id, task_id)
                task_payload = json.loads(task_row.payload_json)
                task_completed = TaskStatus(task_row.status) is TaskStatus.COMPLETED
            except KeyError as error:
                raise HTTPException(status_code=404, detail="task not found") from error
            raw_trace_id = task_payload.get("trace_id")
            # A legacy task has no durable trace mapping. An impossible trace
            # intentionally returns an empty task-scoped center rather than
            # leaking unrelated historical side effects into the current run.
            trace_id = (
                str(raw_trace_id)
                if isinstance(raw_trace_id, str) and raw_trace_id
                else f"legacy-task-without-trace:{task_id}"
            )
        return recovery.center(
            project_id,
            trace_id=trace_id,
            task_completed=task_completed,
        )

    @app.post("/api/recovery/{record_id}/decision", dependencies=[Depends(require_token)])
    def recovery_decision(record_id: str, body: RecoveryDecision) -> dict[str, object]:
        try:
            record = recovery.decide(record_id, body.decision)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"id": record.id, "state": record.state.value, "explicit_user_action": True}

    @app.get("/api/first-run", dependencies=[Depends(require_token)])
    def first_run_status() -> dict[str, object]:
        return {
            **first_run.status(),
            "tools": [asdict(item) for item in first_run.detect()],
            "disk": first_run.disk(),
            "gpu": first_run.gpu(),
        }

    @app.post("/api/first-run/complete", dependencies=[Depends(require_token)])
    def complete_first_run(body: FirstRunComplete) -> dict[str, object]:
        return first_run.complete(
            privacy_mode=body.privacy_mode,
            providers_configured=body.providers_configured,
            skipped=body.skipped,
        )

    @app.post("/api/first-run/dependencies/plan", dependencies=[Depends(require_token)])
    def dependency_install_plan(body: DependencyInstallCreate) -> dict[str, object]:
        try:
            plan = first_run.install_plan(
                body.tool, Path(body.destination) if body.destination else None
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(plan)

    @app.post("/api/first-run/dependencies/install", dependencies=[Depends(require_token)])
    def dependency_install(body: DependencyInstallCreate) -> dict[str, object]:
        try:
            job = first_run.start_install(
                body.tool,
                destination=Path(body.destination) if body.destination else None,
                confirmed=body.confirmed,
            )
        except PermissionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return asdict(job)

    @app.get("/api/first-run/dependencies/jobs/{job_id}", dependencies=[Depends(require_token)])
    def dependency_install_status(job_id: str) -> dict[str, object]:
        try:
            return asdict(first_run.install_status(job_id))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Install job not found") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    frontend_dist = app_settings.project_root / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app
