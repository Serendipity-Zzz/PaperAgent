from __future__ import annotations

import ast
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast
from uuid import UUID, uuid4

import psutil
from pydantic import JsonValue

from paperagent.agents.document_ir import BlockKind, DocumentIR
from paperagent.execution.document_pipeline import (
    DOCUMENT_PIPELINE_TOOL_NAMES,
    DocumentPipelineTools,
    document_pipeline_specs,
)
from paperagent.experiments.runtime import (
    CapabilityAnalyzer,
    EnvironmentManager,
    EnvironmentRecord,
    EnvironmentRegistry,
    ExecutionApproval,
    ProcessExecutor,
)
from paperagent.presentation import expectation_from_presentation
from paperagent.rendering import (
    AssetAssembler,
    DocumentPdfRenderer,
    DocumentRevisionStore,
    DocxRenderer,
    MarkdownRenderer,
    PdfRenderMode,
    RevisionResolver,
    TargetResolver,
)
from paperagent.rendering.asset_binding import ArtifactBinder, manifest_from_document
from paperagent.rendering.delivery import DeliveryStatus, RevisionStatus
from paperagent.rendering.delivery_store import DocumentDeliveryStore
from paperagent.rendering.preflight import RenderedArtifactValidator, RenderPreflight
from paperagent.schemas.presentation import PresentationExpectationManifest
from paperagent.schemas.typography import TypographySpec
from paperagent.tools import (
    ConcurrencyPolicy,
    PermissionPolicy,
    SideEffect,
    ToolRegistry,
    ToolSpec,
)
from paperagent.tools.adapters import CallableToolAdapter

if TYPE_CHECKING:
    from paperagent.artifacts import ArtifactService


DOCUMENT_RENDERER_VERSION = "2.1.0"


class UnsafeSourceError(PermissionError):
    pass


class PythonSourceGuard(ast.NodeVisitor):
    DELETE_CALLS: ClassVar[frozenset[str]] = frozenset(
        {
            "os.remove",
            "os.unlink",
            "os.rmdir",
            "shutil.rmtree",
            "pathlib.Path.unlink",
            "pathlib.Path.rmdir",
        }
    )
    SHELL_CALLS: ClassVar[frozenset[str]] = frozenset(
        {"os.system", "subprocess.call", "subprocess.run", "subprocess.Popen"}
    )

    def __init__(self) -> None:
        self.violations: list[str] = []

    @staticmethod
    def _name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = PythonSourceGuard._name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""

    def visit_Call(self, node: ast.Call) -> None:
        name = self._name(node.func)
        short_name = name.rsplit(".", 1)[-1]
        if name in self.DELETE_CALLS or short_name in {"unlink", "rmdir", "remove", "rmtree"}:
            self.violations.append(f"deletion call requires approval: {name}")
        if name in self.SHELL_CALLS:
            self.violations.append(f"nested process execution is not allowed: {name}")
        if name in {"open", "Path.open", "pathlib.Path.open"} and node.args:
            value = node.args[0]
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and Path(value.value).is_absolute()
            ):
                self.violations.append("absolute file writes are outside the managed run root")
        self.generic_visit(node)

    @classmethod
    def validate(cls, source: str) -> None:
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            raise ValueError(f"Python source is invalid: {error}") from error
        guard = cls()
        guard.visit(tree)
        if guard.violations:
            raise UnsafeSourceError("; ".join(dict.fromkeys(guard.violations)))


class ExecutionToolSuite:
    """Request-scoped adapters for managed local execution and document rendering."""

    TOOL_NAMES = (
        "machine.inspect",
        "repository.inspect",
        "environment.prepare",
        "code.materialize",
        "process.execute",
        "result.collect",
        "document.resolve_revision",
        "document.resolve_target",
        "document.render",
        *DOCUMENT_PIPELINE_TOOL_NAMES,
    )

    def __init__(
        self,
        *,
        data_root: Path,
        project_root: Path,
        run_id: str,
        uv_path: Path | None,
        artifact_service: ArtifactService | None = None,
        requested_typography: TypographySpec | None = None,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
    ) -> None:
        self.data_root = data_root.resolve()
        self.project_root = project_root.resolve()
        self.run_id = run_id
        self.run_root = (self.project_root / "runs" / run_id).resolve()
        self.artifact_root = (self.project_root / "artifacts").resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.uv_path = self._resolve_uv(uv_path)
        self.artifact_service = artifact_service
        self.requested_typography = requested_typography
        self.source_conversation_id = source_conversation_id
        self.source_message_id = source_message_id
        self.last_document: DocumentIR | None = None
        self.last_document_source: tuple[str, str] | None = None
        self.environment_registry = EnvironmentRegistry(self.data_root / "runtimes")
        self.last_environment: EnvironmentRecord | None = None
        self.document_pipeline = DocumentPipelineTools(
            self.project_root,
            artifact_service=self.artifact_service,
            run_id=self.run_id,
            conversation_id=self.source_conversation_id,
            message_id=self.source_message_id,
        )

    def set_source_message(self, message_id: str) -> None:
        self.source_message_id = message_id
        self.document_pipeline.message_id = message_id

    @staticmethod
    def _resolve_uv(configured: Path | None) -> Path | None:
        candidates = [
            configured,
            Path(found) if (found := shutil.which("uv")) else None,
            Path(r"E:\App\uv\current\uv.exe"),
        ]
        return next(
            (candidate.resolve() for candidate in candidates if candidate and candidate.is_file()),
            None,
        )

    def close(self) -> None:
        self.environment_registry.connection.close()

    def register(self, registry: ToolRegistry) -> None:
        adapters = {
            "machine.inspect": self.machine_inspect,
            "repository.inspect": self.repository_inspect,
            "environment.prepare": self.environment_prepare,
            "code.materialize": self.code_materialize,
            "process.execute": self.process_execute,
            "result.collect": self.result_collect,
            "document.resolve_revision": self.document_resolve_revision,
            "document.resolve_target": self.document_resolve_target,
            "document.render": self.document_render,
        }
        adapters.update(self.document_pipeline.adapters())
        for spec in self.specs():
            registry.register(spec, CallableToolAdapter(adapters[spec.name]))

    @classmethod
    def specs(cls) -> list[ToolSpec]:
        experiment_agents = {"experiment_agent", "supervisor", "repair_planner"}
        return [
            ToolSpec(
                name="machine.inspect",
                version="1.0.0",
                description="Inspect local CPU, RAM, disk, Python, uv and optional GPU signals.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                capabilities={"machine", "hardware", "read"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.NONE,
                concurrency_policy=ConcurrencyPolicy.SAFE,
            ),
            ToolSpec(
                name="repository.inspect",
                version="1.0.0",
                description=(
                    "Inspect an in-scope repository for Python, CUDA and model requirements."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"repository": {"type": "string"}},
                    "required": ["repository"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"repository", "hardware", "read"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.NONE,
                concurrency_policy=ConcurrencyPolicy.SAFE,
            ),
            ToolSpec(
                name="environment.prepare",
                version="1.0.0",
                description="Create or reuse a uv environment under the managed runtime root.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "python_version": {"type": "string"},
                        "cuda_version": {"type": ["string", "null"]},
                    },
                    "required": ["dependencies"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"environment", "code_execution", "local_write"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.LOCAL_WRITE,
                concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
                permission_policy=PermissionPolicy.DETERMINISTIC,
            ),
            ToolSpec(
                name="code.materialize",
                version="1.0.0",
                description="Persist the exact reviewed Python source as a run-scoped artifact.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "filename": {"type": "string", "pattern": r"^[A-Za-z0-9_.-]+\.py$"},
                        "content": {"type": "string"},
                        "typography": {"type": "object"},
                    },
                    "required": ["filename", "content"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"code", "source", "local_write"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.LOCAL_WRITE,
                concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
            ),
            ToolSpec(
                name="process.execute",
                version="1.0.0",
                description=(
                    "Execute one materialized Python script in the managed run workspace "
                    "without a shell. argv[0] must be python and argv[1] must be either the "
                    "script basename or the managed relative_path returned by "
                    "code.materialize; shell commands and python -c are not accepted."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"process", "code_execution", "local_write"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.LOCAL_WRITE,
                concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
            ),
            ToolSpec(
                name="result.collect",
                version="1.0.0",
                description="Collect and hash source, data, figures and logs from the current run.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                capabilities={"artifact", "result", "read"},
                allowed_agents=experiment_agents,
                side_effect=SideEffect.NONE,
                concurrency_policy=ConcurrencyPolicy.SAFE,
            ),
            ToolSpec(
                name="document.resolve_revision",
                version="2.0.0",
                description=(
                    "Resolve natural or explicit references to a canonical document revision."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string"},
                        "document_id": {"type": "string"},
                        "revision": {"type": "integer", "minimum": 1},
                        "conversation_id": {"type": "string"},
                        "artifact_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"document", "revision", "read"},
                allowed_agents={"render_agent", "repair_planner", "supervisor"},
                side_effect=SideEffect.NONE,
                concurrency_policy=ConcurrencyPolicy.SAFE,
            ),
            ToolSpec(
                name="document.resolve_target",
                version="2.0.0",
                description="Resolve a natural-language edit target to stable DocumentIR anchors.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "revision": {"type": "integer", "minimum": 1},
                        "request": {"type": "string"},
                        "section_id": {"type": "string"},
                        "block_id": {"type": "string"},
                    },
                    "required": ["document_id", "request"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"document", "revision", "read"},
                allowed_agents={"render_agent", "repair_planner", "supervisor"},
                side_effect=SideEffect.NONE,
                concurrency_policy=ConcurrencyPolicy.SAFE,
            ),
            ToolSpec(
                name="document.render",
                version="2.0.0",
                description=(
                    "Render one persisted canonical revision to MD, portable MD bundle, "
                    "DOCX or PDF."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "revision": {"type": "integer", "minimum": 1},
                        "format": {
                            "type": "string",
                            "enum": ["md", "md_bundle", "docx", "pdf"],
                        },
                        "filename": {"type": "string"},
                        "pdf_mode": {
                            "type": "string",
                            "enum": ["auto", "xelatex", "word_parity"],
                        },
                        "template_artifact_id": {"type": "string"},
                        "presentation_expectation": {"type": "object"},
                    },
                    "required": ["document_id", "revision", "format"],
                    "additionalProperties": False,
                },
                output_schema={"type": "object"},
                capabilities={"document", "render", "local_write"},
                allowed_agents={"render_agent", "repair_planner"},
                side_effect=SideEffect.LOCAL_WRITE,
                concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
            ),
            *document_pipeline_specs(),
        ]

    def machine_inspect(self, _arguments: dict[str, JsonValue]) -> JsonValue:
        disk = shutil.disk_usage(self.data_root)
        gpu = self._command_signal(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ]
        )
        cuda = self._command_signal(["nvcc", "--version"])
        compiler = self._command_signal(["cl"])
        return {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": psutil.cpu_count(logical=True) or 1,
            "ram_gb": round(psutil.virtual_memory().total / 1024**3, 2),
            "disk_free_gb": round(disk.free / 1024**3, 2),
            "uv_path": str(self.uv_path) if self.uv_path else None,
            "uv_available": self.uv_path is not None,
            "gpu": gpu,
            "cuda_compiler": cuda,
            "msvc": compiler,
        }

    def repository_inspect(self, arguments: dict[str, JsonValue]) -> JsonValue:
        repository = self._project_path(str(arguments["repository"]), require_directory=True)
        return CapabilityAnalyzer().analyze(repository).model_dump(mode="json")

    def environment_prepare(self, arguments: dict[str, JsonValue]) -> JsonValue:
        if self.uv_path is None:
            raise FileNotFoundError("uv executable is not configured")
        raw = arguments.get("dependencies", [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("dependencies must be a string array")
        dependencies = cast(list[str], raw)
        lock = (
            "\n".join(item.strip() for item in dependencies if item.strip())
            or "# empty environment"
        )
        manager = EnvironmentManager(
            self.environment_registry,
            self.uv_path,
            self.data_root / "runtimes" / "cache",
        )
        self.last_environment = manager.ensure(
            lock,
            python_version=str(arguments.get("python_version", "3.12")),
            cuda_version=(
                str(arguments["cuda_version"])
                if arguments.get("cuda_version") is not None
                else None
            ),
        )
        return self.last_environment.model_dump(mode="json")

    def code_materialize(self, arguments: dict[str, JsonValue]) -> JsonValue:
        filename = Path(str(arguments["filename"])).name
        if not filename.endswith(".py") or filename != str(arguments["filename"]):
            raise ValueError("source filename must be a safe .py basename")
        content = str(arguments["content"])
        PythonSourceGuard.validate(content)
        target = self.run_root / filename
        if target.exists():
            if target.read_text(encoding="utf-8") == content:
                return self._file_payload(
                    target,
                    relation="source",
                    producer_tool="code.materialize",
                )
            target = self._versioned_target(target)
        target.write_text(content, encoding="utf-8")
        return self._file_payload(target, relation="source", producer_tool="code.materialize")

    def process_execute(self, arguments: dict[str, JsonValue]) -> JsonValue:
        raw = arguments.get("argv")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValueError("argv must be a non-empty string array")
        argv = list(cast(list[str], raw))
        if Path(argv[0]).name.casefold() not in {"python", "python.exe", "py", "py.exe"}:
            raise PermissionError("managed process execution currently accepts Python only")
        python = self._environment_python()
        if not self._allowed_executable(python):
            raise PermissionError("Python executable is outside managed environments")
        if len(argv) < 2 or not argv[1].endswith(".py"):
            raise ValueError("process.execute requires a run-scoped Python script")
        source = self._run_path(argv[1])
        PythonSourceGuard.validate(source.read_text(encoding="utf-8"))
        # sandbox_runner already executes relative to run_root.  Normalize the
        # project-relative path emitted by code.materialize so callers can safely
        # pass that exact value back without creating runs/<id>/runs/<id>/... .
        argv[1] = source.relative_to(self.run_root).as_posix()
        runner = Path(__file__).with_name("sandbox_runner.py").resolve()
        command = [
            str(python),
            str(runner),
            "--root",
            str(self.run_root),
            "--",
            *argv[1:],
        ]
        timeout = arguments.get("timeout_seconds", 300)
        if not isinstance(timeout, int):
            raise ValueError("timeout_seconds must be an integer")
        approval = ExecutionApproval(
            command=command,
            working_directory=str(self.run_root),
            writable_paths=[str(self.run_root)],
            network_allowed=False,
            timeout_seconds=timeout,
            approved=True,
        )
        started_at = datetime.now(UTC)
        result = ProcessExecutor(owner_run_id=self.run_id).run(approval)
        finished_at = datetime.now(UTC)
        log_artifacts: list[dict[str, JsonValue]] = []
        for name, content in (("stdout.log", result.stdout), ("stderr.log", result.stderr)):
            target = self.run_root / name
            if target.exists():
                target = self._versioned_target(target)
            target.write_text(content, encoding="utf-8")
            log_artifacts.append(
                self._file_payload(target, relation="log", producer_tool="process.execute")
            )
        artifact_refs = [
            str(item["artifact_id"])
            for item in log_artifacts
            if item.get("artifact_id") is not None
        ]
        payload = result.model_dump(mode="json")
        payload["artifacts"] = log_artifacts
        payload["artifact_refs"] = artifact_refs
        if self.artifact_service is not None:
            sources = [
                item for item in self.artifact_service.for_run(self.run_id) if item.kind == "source"
            ]
            status_map = {
                "completed": "succeeded",
                "policy_violation": "policy_violation",
                "timeout": "failed",
                "failed": "failed",
            }
            self.artifact_service.record_execution(
                id=str(uuid4()),
                run_id=self.run_id,
                request_id=str(uuid4()),
                tool_call_id=None,
                source_artifact_id=sources[-1].id if sources else None,
                environment_ref=(
                    self.last_environment.fingerprint if self.last_environment else None
                ),
                command_hash=hashlib.sha256(
                    json.dumps(command, ensure_ascii=False).encode()
                ).hexdigest(),
                command_json=json.dumps(command, ensure_ascii=False),
                cwd_relative=self.run_root.relative_to(self.project_root).as_posix(),
                status=status_map.get(result.status, "failed"),
                exit_code=result.return_code,
                stdout_ref=artifact_refs[0] if artifact_refs else None,
                stderr_ref=artifact_refs[1] if len(artifact_refs) > 1 else None,
                side_effects_json=json.dumps({"artifact_refs": artifact_refs}, ensure_ascii=False),
                started_at=started_at,
                finished_at=finished_at,
            )
        return cast(JsonValue, payload)

    def result_collect(self, _arguments: dict[str, JsonValue]) -> JsonValue:
        artifacts = [
            self._file_payload(
                path,
                relation=self._relation(path),
                producer_tool="result.collect",
            )
            for path in sorted(self.run_root.rglob("*"))
            if path.is_file()
        ]
        artifact_refs = [
            str(item["artifact_id"]) for item in artifacts if item.get("artifact_id") is not None
        ]
        return cast(
            JsonValue,
            {
                "run_id": self.run_id,
                "artifacts": artifacts,
                "artifact_refs": artifact_refs,
            },
        )

    def document_render(self, arguments: dict[str, JsonValue]) -> JsonValue:
        store = self._revision_store()
        if arguments.get("document_id") is None or arguments.get("revision") is None:
            raise ValueError(
                "document.render requires canonical document_id and revision; raw content "
                "and arbitrary DocumentIR payloads are rejected"
            )
        document_id = UUID(str(arguments["document_id"]))
        revision = int(str(arguments["revision"]))
        document = store.load(document_id, revision)
        document = self._lazy_migrate_for_render(document, store)
        source_revision = document.revision
        if self.requested_typography is not None and self.requested_typography.configured:
            source_key = (str(document.document_id), str(source_revision))
            if self.last_document_source == source_key and self.last_document is not None:
                document = self.last_document
            else:
                patch = self.requested_typography.model_dump(exclude_none=True)
                desired = document.typography.model_copy(update=patch)
                if desired != document.typography:
                    parent_revision_id = store.revision_id(
                        document.document_id, document.revision
                    )
                    document = document.restyle(desired)
                    if document.asset_manifest is not None:
                        document.asset_manifest = document.asset_manifest.model_copy(
                            update={"revision": document.revision}
                        )
                    store.save(
                        document,
                        parent_revision_id=parent_revision_id,
                        source_message_id=self.source_message_id,
                        source_run_id=self.run_id,
                        source_conversation_id=self.source_conversation_id,
                    )
                self.last_document_source = source_key
                self.last_document = document
        format_name = str(arguments["format"])
        raw_expectation = arguments.get("presentation_expectation")
        if raw_expectation is not None and not isinstance(raw_expectation, dict):
            raise ValueError("presentation_expectation must be an object")
        expectation = (
            PresentationExpectationManifest.model_validate(raw_expectation)
            if isinstance(raw_expectation, dict)
            else expectation_from_presentation(
                document.presentation,
                allow_format_degradation=format_name in {"md", "md_bundle"},
            )
        )
        preflight = RenderPreflight(self.artifact_service).validate(
            document,
            format_name=format_name,
            presentation_expectation=expectation,
        )
        if not preflight.passed:
            self._mark_revision_repair_required(store, document)
            raise ValueError(
                "document render preflight failed: "
                + preflight.model_dump_json(exclude_none=True)
            )
        derivative_formats = {
            "md": ["markdown"],
            "md_bundle": ["markdown"],
            "docx": ["docx"],
            "pdf": [
                "docx"
                if str(arguments.get("pdf_mode", "auto")) == "word_parity"
                else "xelatex"
            ],
        }[format_name]
        if self.artifact_service is not None and any(
            block.figure is not None for block in document.iter_blocks()
        ):
            document = (
                AssetAssembler(self.artifact_service)
                .assemble(
                    document,
                    target_formats=derivative_formats,
                )
                .document
            )
        self.last_document = document
        revision_id = store.revision_id(document.document_id, document.revision)
        canonical_artifact_id = store.canonical_artifact_id(
            document.document_id, document.revision
        )
        template: Path | None = None
        template_artifact_id = arguments.get("template_artifact_id")
        if template_artifact_id is not None:
            if self.artifact_service is None:
                raise RuntimeError("template rendering requires an artifact service")
            template_artifact = self.artifact_service.get(str(template_artifact_id), verify=True)
            template = self.artifact_service.verify(template_artifact)
            if template.suffix.casefold() != ".docx":
                raise ValueError("document template artifact must be a DOCX file")
        extension = "zip" if format_name == "md_bundle" else format_name
        filename = Path(str(arguments.get("filename", f"paperagent-result.{extension}"))).name
        if Path(filename).suffix.casefold() != f".{extension}":
            filename = f"{Path(filename).stem}.{extension}"
        options_hash = hashlib.sha256(
            json.dumps(
                {
                    "document_id": str(document.document_id),
                    "revision": document.revision,
                    "format": format_name,
                    "pdf_mode": arguments.get("pdf_mode", "auto"),
                    "template_artifact_id": arguments.get("template_artifact_id"),
                    "typography_hash": document.hashes().style_hash,
                    "presentation_hash": document.hashes().presentation_hash,
                    "numbering_hash": document.hashes().numbering_hash,
                    "presentation_expectation_hash": expectation.expectation_hash,
                    "renderer_version": DOCUMENT_RENDERER_VERSION,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        delivery_store = (
            DocumentDeliveryStore(
                self.artifact_service.databases, self.artifact_service.project_id
            )
            if self.artifact_service is not None
            else None
        )
        figure_artifact_ids = [
            str(block.figure.artifact_id)
            for block in document.iter_blocks()
            if block.figure is not None and block.figure.artifact_id is not None
        ]
        renderer_name = {
            "md": "markdown",
            "md_bundle": "markdown-bundle",
            "docx": "python-docx",
            "pdf": "document-pdf",
        }[format_name]
        delivery = (
            delivery_store.create(
                revision_id=revision_id,
                format_name=format_name,
                renderer=renderer_name,
                renderer_version=DOCUMENT_RENDERER_VERSION,
                options_hash=options_hash,
                figure_artifact_ids=figure_artifact_ids,
                source_run_id=self.run_id,
                source_message_id=self.source_message_id,
            )
            if delivery_store is not None
            else None
        )
        if delivery is not None:
            assert delivery_store is not None
        if delivery is not None and delivery.status == DeliveryStatus.DELIVERED.value:
            if delivery.artifact_id is None:
                raise RuntimeError("delivered record has no artifact")
            return cast(
                JsonValue,
                self._artifact_payload(
                    delivery.artifact_id,
                    delivery.id,
                    document_revision=document.revision,
                ),
            )
        if delivery is not None:
            assert delivery_store is not None
            delivery = delivery_store.transition(
                delivery.id,
                DeliveryStatus.RENDERING,
                expected_version=delivery.version,
            )
        self._mark_revision_rendering(store, document)
        staging_root = self.artifact_root / ".staging" / (
            delivery.id if delivery is not None else str(uuid4())
        )
        staging_root.mkdir(parents=True, exist_ok=True)
        output = staging_root / filename
        render_metadata: dict[str, JsonValue] = {}
        try:
            if format_name == "md":
                render_document = self._document_for_format(document, "markdown")
                rendered = MarkdownRenderer().render(render_document, output)
            elif format_name == "md_bundle":
                render_document = self._document_for_format(document, "markdown")
                rendered = MarkdownRenderer().render_bundle(render_document, output)
            elif format_name == "docx":
                render_document = self._document_for_format(document, "docx")
                rendered = DocxRenderer().render(render_document, output, template=template)
            elif format_name == "pdf":
                mode = PdfRenderMode(str(arguments.get("pdf_mode", "auto")))
                target_format = "docx" if mode is PdfRenderMode.WORD_PARITY else "xelatex"
                render_document = self._document_for_format(document, target_format)
                result = DocumentPdfRenderer().render(
                    render_document,
                    output,
                    mode=mode,
                    template=template,
                )
                if not result.success or result.output is None:
                    raise RuntimeError(
                        f"PDF rendering failed: {result.error_code}: {result.log[-1000:]}"
                    )
                rendered = result.output
                render_metadata = {
                    "render_mode": result.decision.selected.value,
                    "render_mode_reason": result.decision.reason,
                    "render_engine": result.engine,
                }
            else:
                raise ValueError(f"unsupported render format: {format_name}")
        except Exception:
            if delivery is not None:
                assert delivery_store is not None
                delivery_store.transition(
                    delivery.id,
                    DeliveryStatus.REPAIR_REQUIRED,
                    expected_version=delivery.version,
                )
            self._mark_revision_repair_required(store, document)
            raise
        if delivery is not None:
            assert delivery_store is not None
            delivery = delivery_store.transition(
                delivery.id,
                DeliveryStatus.VALIDATING,
                expected_version=delivery.version,
            )
        required_images = (
            document.asset_manifest.required_figure_count
            if document.asset_manifest is not None
            else len(figure_artifact_ids)
        )
        validation = RenderedArtifactValidator().validate(
            rendered,
            format_name=format_name,
            required_image_count=required_images,
            document_id=document.document_id,
            revision=document.revision,
            document=document,
            presentation_expectation=expectation,
        )
        if not validation.passed:
            rejected = self.artifact_root / "rejected" / (
                delivery.id if delivery is not None else str(uuid4())
            ) / filename
            rejected.parent.mkdir(parents=True, exist_ok=True)
            rendered.replace(rejected)
            rejected_payload = self._file_payload(
                rejected,
                relation="output",
                producer_tool="document.render",
                document_id=str(document.document_id),
                revision_id=revision_id,
                derived_from_artifact_id=canonical_artifact_id,
                validation_status="rejected",
                delivery_status="rejected",
                renderer_version=DOCUMENT_RENDERER_VERSION,
                lineage={
                    "canonical_artifact_id": canonical_artifact_id,
                    "figure_artifact_ids": figure_artifact_ids,
                    "presentation_schema_version": document.presentation.schema_version,
                    "presentation_hash": document.hashes().presentation_hash,
                    "numbering_hash": document.hashes().numbering_hash,
                    "presentation_expectation_hash": expectation.expectation_hash,
                    "source_run_id": self.run_id,
                    "source_message_id": self.source_message_id,
                },
            )
            if delivery is not None:
                assert delivery_store is not None
                delivery_store.transition(
                    delivery.id,
                    DeliveryStatus.REJECTED,
                    expected_version=delivery.version,
                    artifact_id=str(rejected_payload.get("artifact_id")),
                    validation_report=validation.model_dump(mode="json"),
                )
            self._mark_revision_repair_required(store, document)
            raise RuntimeError(
                "rendered artifact rejected by delivery QA: "
                + validation.model_dump_json(exclude_none=True)
            )
        final_output = self.artifact_root / filename
        if final_output.exists():
            final_output = self._versioned_target(final_output)
        if format_name == "md":
            self._publish_markdown_assets(
                rendered,
                final_output,
                delivery.id if delivery is not None else options_hash[:16],
            )
        rendered.replace(final_output)
        payload = self._file_payload(
            final_output,
            relation="output",
            producer_tool="document.render",
            document_id=str(document.document_id),
            revision_id=revision_id,
            derived_from_artifact_id=canonical_artifact_id,
            validation_status="valid",
            delivery_status="delivered",
            renderer_version=DOCUMENT_RENDERER_VERSION,
            lineage={
                "canonical_artifact_id": canonical_artifact_id,
                "figure_artifact_ids": figure_artifact_ids,
                "presentation_schema_version": document.presentation.schema_version,
                "presentation_hash": document.hashes().presentation_hash,
                "numbering_hash": document.hashes().numbering_hash,
                "presentation_expectation_hash": expectation.expectation_hash,
                "source_run_id": self.run_id,
                "source_message_id": self.source_message_id,
            },
        )
        if delivery is not None:
            assert delivery_store is not None
            delivery = delivery_store.transition(
                delivery.id,
                DeliveryStatus.DELIVERED,
                expected_version=delivery.version,
                artifact_id=str(payload.get("artifact_id")),
                validation_report=validation.model_dump(mode="json"),
            )
            payload["delivery_id"] = delivery.id
            payload["delivery_status"] = delivery.status
        self._mark_revision_delivered(store, document)
        payload.update(render_metadata)
        payload["document_id"] = str(document.document_id)
        payload["document_revision"] = document.revision
        payload["presentation_schema_version"] = document.presentation.schema_version
        payload["presentation_hash"] = document.hashes().presentation_hash
        payload["numbering_hash"] = document.hashes().numbering_hash
        payload["presentation_expectation_hash"] = expectation.expectation_hash
        return payload

    def _lazy_migrate_for_render(
        self, document: DocumentIR, store: DocumentRevisionStore
    ) -> DocumentIR:
        figures = [block for block in document.iter_blocks() if block.kind is BlockKind.FIGURE]
        needs_migration = document.asset_manifest is None or any(
            block.figure is None or block.figure.artifact_id is None for block in figures
        )
        if not needs_migration:
            return document
        latest = store.load(document.document_id)
        migrated_from = latest.metadata.get("lazy_migrated_from_revision")
        if latest.revision != document.revision:
            if migrated_from == document.revision:
                return latest
            raise ValueError(
                "legacy revision cannot be migrated implicitly because a newer canonical "
                "revision exists; resolve the target revision explicitly"
            )
        migrated = document.model_copy(deep=True)
        if self.artifact_service is not None:
            binding = ArtifactBinder(self.artifact_service).bind(
                migrated,
                source_run_id=str(document.metadata.get("source_run_id") or "") or None,
                source_message_id=str(document.metadata.get("source_message_id") or "") or None,
            )
            if not binding.ready:
                raise ValueError(
                    "legacy asset migration failed closed: "
                    f"missing={binding.missing}, pending={binding.pending}, "
                    f"invalid={binding.invalid}, "
                    f"ambiguous={[item.logical_id for item in binding.ambiguous]}"
                )
            migrated = binding.document
        elif figures:
            raise ValueError("legacy figures require the project artifact catalog for migration")
        if migrated.asset_manifest is None:
            migrated.asset_manifest = manifest_from_document(migrated)
        new_revision = document.revision + 1
        manifest = migrated.asset_manifest.model_copy(update={"revision": new_revision})
        metadata = dict(migrated.metadata)
        metadata["lazy_migrated_from_revision"] = document.revision
        migrated = migrated.model_copy(
            update={
                "revision": new_revision,
                "asset_manifest": manifest,
                "metadata": metadata,
            }
        )
        store.save(
            migrated,
            parent_revision_id=store.revision_id(document.document_id, document.revision),
            source_run_id=str(metadata.get("source_run_id") or self.run_id or "") or None,
            source_message_id=str(metadata.get("source_message_id") or "") or None,
            source_conversation_id=str(
                metadata.get("source_conversation_id") or self.source_conversation_id or ""
            )
            or None,
        )
        return migrated

    @staticmethod
    def _revision_transition(
        store: DocumentRevisionStore,
        document: DocumentIR,
        target: RevisionStatus,
    ) -> None:
        try:
            current = store.status(document.document_id, document.revision)
            if current is target:
                return
            store.transition_status(document.document_id, document.revision, target)
        except RuntimeError:
            return

    def _mark_revision_rendering(
        self, store: DocumentRevisionStore, document: DocumentIR
    ) -> None:
        self._revision_transition(store, document, RevisionStatus.RENDERING)

    def _mark_revision_delivered(
        self, store: DocumentRevisionStore, document: DocumentIR
    ) -> None:
        self._revision_transition(store, document, RevisionStatus.DELIVERED)

    def _mark_revision_repair_required(
        self, store: DocumentRevisionStore, document: DocumentIR
    ) -> None:
        self._revision_transition(store, document, RevisionStatus.REPAIR_REQUIRED)

    def _artifact_payload(
        self,
        artifact_id: str,
        delivery_id: str,
        *,
        document_revision: int,
    ) -> dict[str, JsonValue]:
        if self.artifact_service is None:
            raise RuntimeError("artifact payload requires the project artifact catalog")
        artifact = self.artifact_service.get(artifact_id, verify=True)
        return cast(
            dict[str, JsonValue],
            {
                "artifact_id": artifact.id,
                "artifact_refs": [artifact.id],
                "name": artifact.original_name,
                "relative_path": artifact.relative_path,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "relation": artifact.kind,
                "document_id": artifact.document_id,
                "document_revision": document_revision,
                "revision_id": artifact.revision_id,
                "delivery_id": delivery_id,
                "delivery_status": artifact.delivery_status,
            },
        )

    @staticmethod
    def _publish_markdown_assets(source: Path, final_output: Path, key: str) -> None:
        staging_assets = source.parent / "assets"
        if not staging_assets.is_dir():
            return
        target_assets = final_output.parent / "assets" / key
        target_assets.mkdir(parents=True, exist_ok=True)
        for item in sorted(staging_assets.iterdir()):
            if item.is_file():
                target = target_assets / item.name
                if not target.exists():
                    item.replace(target)
        markdown = source.read_text(encoding="utf-8")
        markdown = markdown.replace(
            "](assets/", f"](assets/{key}/"
        )
        source.write_text(markdown, encoding="utf-8", newline="\n")

    def document_resolve_revision(self, arguments: dict[str, JsonValue]) -> JsonValue:
        resolution = RevisionResolver(self._revision_store()).resolve(
            str(arguments.get("reference", "")),
            document_id=UUID(str(arguments["document_id"]))
            if arguments.get("document_id")
            else None,
            revision=(
                int(str(arguments["revision"])) if arguments.get("revision") is not None else None
            ),
            conversation_id=str(
                arguments.get("conversation_id") or self.source_conversation_id or ""
            )
            or None,
            artifact_id=UUID(str(arguments["artifact_id"]))
            if arguments.get("artifact_id")
            else None,
        )
        return cast(JsonValue, resolution.model_dump(mode="json"))

    def document_resolve_target(self, arguments: dict[str, JsonValue]) -> JsonValue:
        document = self._revision_store().load(
            UUID(str(arguments["document_id"])),
            int(str(arguments["revision"])) if arguments.get("revision") is not None else None,
        )
        resolution = TargetResolver().resolve(
            document,
            str(arguments["request"]),
            section_id=UUID(str(arguments["section_id"])) if arguments.get("section_id") else None,
            block_id=UUID(str(arguments["block_id"])) if arguments.get("block_id") else None,
        )
        return cast(JsonValue, resolution.model_dump(mode="json"))

    def _revision_store(self) -> DocumentRevisionStore:
        if self.artifact_service is None:
            return DocumentRevisionStore(self.project_root)
        return DocumentRevisionStore(
            self.project_root,
            databases=self.artifact_service.databases,
            project_id=self.artifact_service.project_id,
            artifact_service=self.artifact_service,
        )

    def _document_for_format(self, document: DocumentIR, target_format: str) -> DocumentIR:
        if self.artifact_service is None:
            return document
        resolved = document.model_copy(deep=True)
        for block in resolved.iter_blocks():
            if block.kind is not BlockKind.FIGURE or block.figure is None:
                continue
            derivative_id = block.figure.derivative_artifact_ids.get(target_format)
            if derivative_id is None:
                continue
            derivative = self.artifact_service.get(str(derivative_id), verify=True)
            path = self.artifact_service.verify(derivative)
            block.figure.path = str(path)
            block.data["path"] = str(path)
        return resolved

    def _environment_python(self) -> Path:
        if self.last_environment is not None:
            candidate = Path(self.last_environment.path) / "Scripts" / "python.exe"
            if candidate.is_file():
                return candidate
        return Path(sys.executable).resolve()

    def _allowed_executable(self, executable: Path) -> bool:
        roots = [
            (self.data_root / "runtimes" / "venvs").resolve(),
            Path(sys.executable).resolve().parent,
        ]
        return any(executable == root or root in executable.parents for root in roots)

    def _project_path(self, value: str, *, require_directory: bool = False) -> Path:
        target = Path(value)
        target = (
            target.resolve() if target.is_absolute() else (self.project_root / target).resolve()
        )
        if target != self.project_root and self.project_root not in target.parents:
            raise PermissionError("path is outside the project scope")
        if require_directory and not target.is_dir():
            raise FileNotFoundError(target)
        return target

    def _run_path(self, value: str) -> Path:
        raw = Path(value)
        if raw.is_absolute():
            target = raw.resolve()
        else:
            target = (self.run_root / raw).resolve()
            if not target.is_file():
                project_relative = (self.project_root / raw).resolve()
                if project_relative == self.run_root or self.run_root in project_relative.parents:
                    target = project_relative
        if target != self.run_root and self.run_root not in target.parents:
            raise PermissionError("path is outside the managed run workspace")
        if not target.is_file():
            raise FileNotFoundError(target)
        return target

    def _file_payload(
        self,
        path: Path,
        *,
        relation: str,
        producer_tool: str,
        document_id: str | None = None,
        revision_id: str | None = None,
        derived_from_artifact_id: str | None = None,
        validation_status: str = "valid",
        delivery_status: str = "not_applicable",
        renderer_version: str | None = None,
        lineage: dict[str, object] | None = None,
    ) -> dict[str, JsonValue]:
        content = path.read_bytes()
        payload: dict[str, JsonValue] = {
            "name": path.name,
            "relative_path": path.relative_to(self.project_root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "relation": relation,
        }
        if self.artifact_service is not None:
            record = self.artifact_service.register(
                path,
                kind=relation,
                producer_tool=producer_tool,
                run_id=self.run_id,
                document_id=document_id,
                revision_id=revision_id,
                derived_from_artifact_id=derived_from_artifact_id,
                validation_status=validation_status,
                delivery_status=delivery_status,
                renderer_version=renderer_version,
                lineage=lineage,
                environment_ref=(
                    self.last_environment.fingerprint if self.last_environment else None
                ),
            )
            self.artifact_service.link(
                record.id,
                relation=relation,
                run_id=self.run_id,
                label=record.original_name,
            )
            payload["artifact_id"] = record.id
            payload["artifact_refs"] = [record.id]
        return payload

    @staticmethod
    def _versioned_target(path: Path) -> Path:
        revision = 2
        while True:
            candidate = path.with_name(f"{path.stem}-r{revision}{path.suffix}")
            if not candidate.exists():
                return candidate
            revision += 1

    @staticmethod
    def _command_signal(command: list[str]) -> str | None:
        executable = shutil.which(command[0])
        if executable is None:
            return None
        try:
            result = subprocess.run(
                [executable, *command[1:]],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        content = (result.stdout or result.stderr).strip()
        return content[-2_000:] if content else None

    @staticmethod
    def _relation(path: Path) -> str:
        suffix = path.suffix.casefold()
        if suffix == ".py":
            return "source"
        if suffix in {".png", ".jpg", ".jpeg", ".svg", ".webp"}:
            return "figure"
        if suffix in {".csv", ".json", ".xlsx", ".parquet"}:
            return "data"
        if suffix in {".log", ".txt"}:
            return "log"
        return "output"
