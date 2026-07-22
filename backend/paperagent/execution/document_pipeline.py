from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4
from zipfile import ZipFile

from pydantic import JsonValue
from pypdf import PdfReader
from sqlalchemy import select

from paperagent.agents.document_ir import diff_documents, migrate_document_ir
from paperagent.artifacts import ArtifactService
from paperagent.db.models import DocumentDeliveryRecord
from paperagent.orchestration.document_production import RenderPlanService
from paperagent.presentation import (
    PresentationResolver,
    presentation_from_requirement,
)
from paperagent.rendering import (
    ARCHETYPES,
    ArchetypeId,
    AssetAssembler,
    AssetBarrier,
    BibliographicItem,
    CitationStyle,
    CitationStyleService,
    DocumentArchetypeClassifier,
    DocumentRevisionStore,
    RevisionOperation,
    RevisionResolver,
    RevisionWorkflow,
    archetype_layout_profile,
)
from paperagent.rendering.asset_binding import (
    ArtifactBinder,
    AssetBarrierCheckpointStore,
    manifest_from_document,
)
from paperagent.rendering.markdown_parser import parse_markdown_sections
from paperagent.rendering.numbering import NumberingInspector
from paperagent.rendering.preflight import RenderedArtifactValidator, RenderPreflight
from paperagent.schemas.presentation import (
    DocumentPresentationSpec,
    PresentationExpectationManifest,
    PresentationPatchOperation,
    RequirementPresentationSpec,
)
from paperagent.tools import ConcurrencyPolicy, SideEffect, ToolSpec

DOCUMENT_PIPELINE_TOOL_NAMES = (
    "document.classify",
    "document.structure.plan",
    "document.presentation.resolve",
    "document.compose",
    "document.bind_assets",
    "asset.resolve",
    "asset.derive",
    "citation.format",
    "document.layout.resolve",
    "document.qa",
    "document.validate_delivery",
    "document.revision.lookup",
    "document.revision.patch",
    "document.presentation.patch",
)


def document_pipeline_specs() -> list[ToolSpec]:
    read_agents = {
        "supervisor",
        "requirement_agent",
        "writer_agent",
        "render_agent",
        "review_agent",
        "repair_planner",
    }
    return [
        ToolSpec(
            name="document.classify",
            version="1.0.0",
            description="Classify a document archetype with confidence and evidence.",
            input_schema={
                "type": "object",
                "properties": {"request": {"type": "string"}, "explicit": {"type": "string"}},
                "required": ["request"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"document", "classification", "read"},
            allowed_agents=read_agents,
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.structure.plan",
            version="1.0.0",
            description="Resolve required and optional semantic sections for an archetype.",
            input_schema={
                "type": "object",
                "properties": {
                    "archetype": {"type": "string"},
                    "include_optional": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["archetype"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"document", "structure", "read"},
            allowed_agents=read_agents,
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.presentation.resolve",
            version="1.0.0",
            description=(
                "Deterministically resolve user, template, current and default document "
                "presentation into one canonical cover and page-chrome specification."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "defaults": {"type": "object"},
                    "archetype": {"type": "object"},
                    "template": {"type": "object"},
                    "current": {"type": "object"},
                    "latest": {"type": "object"},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"document", "presentation", "read"},
            allowed_agents=read_agents,
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.compose",
            version="2.0.0",
            description=(
                "Create one validated canonical DocumentIR from structured Markdown content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "language": {"type": "string", "enum": ["zh", "en", "mixed"]},
                    "conversation_id": {"type": "string"},
                    "image_required": {"type": "boolean"},
                    "presentation": {"type": "object"},
                },
                "required": ["title", "content"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"document", "compose", "local_write"},
            allowed_agents={"writer_agent", "repair_planner"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
        ),
        ToolSpec(
            name="document.bind_assets",
            version="1.0.0",
            description=(
                "Bind legacy or typed figure references to verified artifacts inside the "
                "canonical revision source scope."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "source_run_id": {"type": "string"},
                    "source_message_id": {"type": "string"},
                    "image_required": {"type": "boolean"},
                },
                "required": ["document_id"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"asset", "document", "revision", "local_write"},
            allowed_agents={"visual_agent", "render_agent", "repair_planner"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
        ),
        ToolSpec(
            name="asset.resolve",
            version="1.0.0",
            description="Resolve and verify all figure assets referenced by a DocumentIR.",
            input_schema={
                "type": "object",
                "properties": {
                    "document_ir": {"type": "object"},
                    "image_required": {"type": "boolean"},
                    "source_run_id": {"type": "string"},
                    "source_message_id": {"type": "string"},
                },
                "required": ["document_ir"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"asset", "document", "read"},
            allowed_agents={"visual_agent", "render_agent", "repair_planner"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="asset.derive",
            version="1.0.0",
            description="Build renderer-specific derivatives behind an asset barrier.",
            input_schema={
                "type": "object",
                "properties": {
                    "document_ir": {"type": "object"},
                    "formats": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["document_ir", "formats"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"asset", "document", "local_write"},
            allowed_agents={"visual_agent", "render_agent", "repair_planner"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
        ),
        ToolSpec(
            name="citation.format",
            version="1.0.0",
            description="Format verified bibliographic items without inventing sources.",
            input_schema={
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "object"}},
                    "style": {"type": "string", "enum": ["gb-t-7714", "apa", "ieee"]},
                },
                "required": ["items", "style"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"citation", "document", "read"},
            allowed_agents={"evidence_agent", "writer_agent", "review_agent"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.layout.resolve",
            version="1.0.0",
            description=(
                "Resolve an A4 layout profile and renderer capability plan before rendering."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "archetype": {"type": "string"},
                    "formats": {"type": "array", "items": {"type": "string"}},
                    "word_available": {"type": "boolean"},
                    "tex_available": {"type": "boolean"},
                },
                "required": ["archetype", "formats"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"layout", "document", "read"},
            allowed_agents=read_agents,
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.qa",
            version="1.0.0",
            description=(
                "Validate rendered artifacts, revision links, native structure, embedded "
                "media and placeholder leakage."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "require_images": {"type": "boolean"},
                    "presentation_expectation": {"type": "object"},
                },
                "required": ["artifact_ids"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"quality", "document", "read"},
            allowed_agents={"review_agent", "repair_planner", "render_agent"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.validate_delivery",
            version="1.0.0",
            description=(
                "Validate delivered MD, bundle, DOCX or PDF against one canonical revision."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "presentation_expectation": {"type": "object"},
                },
                "required": ["document_id", "revision", "artifact_ids"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"quality", "document", "delivery", "read"},
            allowed_agents={"review_agent", "repair_planner", "render_agent"},
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.revision.lookup",
            version="1.0.0",
            description=(
                "Resolve a canonical document revision from natural or explicit references."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "reference": {"type": "string"},
                    "document_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "conversation_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"revision", "document", "read"},
            allowed_agents=read_agents,
            side_effect=SideEffect.NONE,
            concurrency_policy=ConcurrencyPolicy.SAFE,
        ),
        ToolSpec(
            name="document.revision.patch",
            version="1.0.0",
            description="Apply a targeted, immutable revision operation to a canonical document.",
            input_schema={
                "type": "object",
                "properties": {"document_id": {"type": "string"}, "operation": {"type": "object"}},
                "required": ["document_id", "operation"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"revision", "document", "local_write"},
            allowed_agents={"repair_planner", "render_agent"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
        ),
        ToolSpec(
            name="document.presentation.patch",
            version="1.0.0",
            description=(
                "Apply atomic cover or page-chrome operations to a canonical revision without "
                "rewriting document content or rerunning assets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "revision": {"type": "integer", "minimum": 1},
                    "operations": {"type": "array", "items": {"type": "object"}},
                    "requested_formats": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["md", "md_bundle", "docx", "pdf"]},
                    },
                },
                "required": ["document_id", "operations"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            capabilities={"revision", "document", "presentation", "local_write"},
            allowed_agents={"requirement_agent", "render_agent", "repair_planner"},
            side_effect=SideEffect.LOCAL_WRITE,
            concurrency_policy=ConcurrencyPolicy.EXCLUSIVE,
        ),
    ]


class DocumentPipelineTools:
    PRIVATE_MARKERS = ("Verified source content is supplied by the renderer.",)

    def __init__(
        self,
        project_root: Path,
        *,
        artifact_service: ArtifactService | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.artifact_service = artifact_service
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.message_id = message_id

    @property
    def store(self) -> DocumentRevisionStore:
        if self.artifact_service is None:
            return DocumentRevisionStore(self.project_root)
        return DocumentRevisionStore(
            self.project_root,
            databases=self.artifact_service.databases,
            project_id=self.artifact_service.project_id,
            artifact_service=self.artifact_service,
        )

    def adapters(
        self,
    ) -> dict[str, Callable[[dict[str, JsonValue]], JsonValue]]:
        return {
            "document.classify": self.classify,
            "document.structure.plan": self.structure_plan,
            "document.presentation.resolve": self.presentation_resolve,
            "document.compose": self.compose,
            "document.bind_assets": self.bind_assets,
            "asset.resolve": self.asset_resolve,
            "asset.derive": self.asset_derive,
            "citation.format": self.citation_format,
            "document.layout.resolve": self.layout_resolve,
            "document.qa": self.qa,
            "document.validate_delivery": self.validate_delivery,
            "document.revision.lookup": self.revision_lookup,
            "document.revision.patch": self.revision_patch,
            "document.presentation.patch": self.presentation_patch,
        }

    def classify(self, arguments: dict[str, JsonValue]) -> JsonValue:
        explicit = arguments.get("explicit")
        decision = DocumentArchetypeClassifier().classify(
            str(arguments["request"]),
            explicit=ArchetypeId(str(explicit)) if explicit else None,
        )
        return decision.model_dump(mode="json")

    def structure_plan(self, arguments: dict[str, JsonValue]) -> JsonValue:
        archetype = ARCHETYPES[ArchetypeId(str(arguments["archetype"]))]
        optional = arguments.get("include_optional")
        selected = [str(item) for item in optional] if isinstance(optional, list) else []
        return cast(
            JsonValue,
            {
                "archetype": archetype.id.value,
                "sections": list(archetype.required_sections) + selected,
                "required_sections": list(archetype.required_sections),
                "optional_sections": list(archetype.optional_sections),
                "components": list(archetype.default_components),
                "numbering": archetype.numbering,
                "qa_rules": list(archetype.qa_rules),
            },
        )

    def presentation_resolve(self, arguments: dict[str, JsonValue]) -> JsonValue:
        def layer(name: str) -> RequirementPresentationSpec | None:
            raw = arguments.get(name)
            if raw is None:
                return None
            if not isinstance(raw, dict):
                raise ValueError(f"{name} presentation layer must be an object")
            return RequirementPresentationSpec.model_validate(raw)

        resolution = PresentationResolver().resolve(
            defaults=layer("defaults"),
            archetype=layer("archetype"),
            template=layer("template"),
            current=layer("current"),
            latest=layer("latest"),
        )
        document_id = UUID(str(arguments.get("document_id") or uuid4()))
        presentation = presentation_from_requirement(
            resolution.presentation,
            document_id=document_id,
        )
        return cast(
            JsonValue,
            {
                "document_id": str(document_id),
                "presentation": presentation.model_dump(mode="json"),
                "source_map": {key: value.value for key, value in resolution.source_map.items()},
                "diagnostics": resolution.diagnostics,
                "unresolved": [
                    item.model_dump(mode="json") for item in resolution.presentation.unresolved
                ],
            },
        )

    def compose(self, arguments: dict[str, JsonValue]) -> JsonValue:
        content = str(arguments["content"])
        if any(marker in content for marker in self.PRIVATE_MARKERS):
            raise ValueError("private placeholder content is forbidden")
        title = str(arguments["title"])
        document_id = UUID(str(arguments.get("document_id") or uuid4()))
        raw_presentation = arguments.get("presentation")
        if raw_presentation is None:
            presentation = presentation_from_requirement(
                RequirementPresentationSpec(),
                document_id=document_id,
            )
        elif isinstance(raw_presentation, dict):
            try:
                presentation = DocumentPresentationSpec.model_validate(raw_presentation)
            except ValueError:
                presentation = presentation_from_requirement(
                    RequirementPresentationSpec.model_validate(raw_presentation),
                    document_id=document_id,
                )
        else:
            raise ValueError("presentation must be an object")
        sections = parse_markdown_sections(
            content,
            title=title,
            agent="writer_agent",
        )
        document = migrate_document_ir(
            {
                "schema_version": "1.1",
                "requirement_id": str(uuid4()),
                "requirement_version": 1,
                "outline_id": str(uuid4()),
                "document_id": str(document_id),
                "title": title,
                "language": str(arguments.get("language", "mixed")),
                "metadata": {
                    "source_run_id": self.run_id,
                    "source_message_id": self.message_id,
                    "source_conversation_id": str(
                        arguments.get("conversation_id") or self.conversation_id or ""
                    ),
                },
                "presentation": presentation.model_dump(mode="json"),
                "sections": [section.model_dump(mode="json") for section in sections],
            }
        )
        figures = [block for block in document.iter_blocks() if block.figure is not None]
        document.asset_manifest = manifest_from_document(
            document,
            image_required=bool(arguments.get("image_required", bool(figures))),
            source_run_id=self.run_id,
        )
        if self.artifact_service is not None and figures:
            binding = ArtifactBinder(self.artifact_service).bind(
                document,
                source_run_id=self.run_id,
                image_required=document.asset_manifest.image_required,
            )
            document = binding.document
        self.store.save(
            document,
            source_message_id=self.message_id,
            source_run_id=self.run_id,
            source_conversation_id=self.conversation_id,
        )
        return cast(JsonValue, document.canonical_payload())

    def presentation_patch(self, arguments: dict[str, JsonValue]) -> JsonValue:
        document_id = UUID(str(arguments["document_id"]))
        revision = int(str(arguments["revision"])) if arguments.get("revision") else None
        before = self.store.load(document_id, revision)
        raw_operations = arguments.get("operations")
        if not isinstance(raw_operations, list):
            raise ValueError("presentation operations must be a list")
        operations = [PresentationPatchOperation.model_validate(item) for item in raw_operations]
        requested = arguments.get("requested_formats")
        if requested is not None and not isinstance(requested, list):
            raise ValueError("requested_formats must be a list")
        rerender_formats = list(
            dict.fromkeys(str(item) for item in (requested or []) if str(item))
        )
        if not rerender_formats and self.artifact_service is not None:
            parent_revision_id = self.store.revision_id(before.document_id, before.revision)
            with self.artifact_service.databases.project_session(
                self.artifact_service.project_id
            ) as session:
                rerender_formats = list(
                    dict.fromkeys(
                        session.scalars(
                            select(DocumentDeliveryRecord.format).where(
                                DocumentDeliveryRecord.revision_id == parent_revision_id,
                                DocumentDeliveryRecord.status == "delivered",
                            )
                        )
                    )
                )
        if not rerender_formats:
            rerender_formats = ["pdf"]
        result = RevisionWorkflow(self.store).apply(
            before,
            RevisionOperation(
                kind="presentation",
                presentation_operations=operations,
            ),
        )
        hashes = result.document.hashes()
        diff = diff_documents(before, result.document)
        return cast(
            JsonValue,
            {
                "document_ir": result.document.canonical_payload(),
                "parent_revision_id": result.parent_revision_id,
                "presentation_hash": hashes.presentation_hash,
                "numbering_hash": hashes.numbering_hash,
                "affected_domains": ["presentation"],
                "rerender_formats": rerender_formats,
                "diff": diff.model_dump(mode="json"),
            },
        )

    def asset_resolve(self, arguments: dict[str, JsonValue]) -> JsonValue:
        document_ir = arguments["document_ir"]
        if not isinstance(document_ir, dict):
            raise ValueError("document_ir must be an object")
        document = migrate_document_ir(cast(dict[str, object], document_ir))
        source_run_id = (
            str(
                arguments.get("source_run_id")
                or document.metadata.get("source_run_id")
                or self.run_id
                or ""
            )
            or None
        )
        source_message_id = str(arguments.get("source_message_id") or "") or None
        if self.artifact_service is not None:
            binding = ArtifactBinder(self.artifact_service).bind(
                document,
                source_run_id=source_run_id,
                source_message_id=source_message_id,
                image_required=(
                    bool(arguments["image_required"])
                    if arguments.get("image_required") is not None
                    else None
                ),
            )
            document = binding.document
            if not binding.ready:
                raise ValueError(
                    "document asset binding failed: "
                    f"missing={binding.missing}, pending={binding.pending}, "
                    f"invalid={binding.invalid}, "
                    f"ambiguous={[item.logical_id for item in binding.ambiguous]}"
                )
        figures = [block for block in document.iter_blocks() if block.figure is not None]
        unresolved = [
            str(block.block_id)
            for block in figures
            if block.figure is None or block.figure.artifact_id is None
        ]
        if unresolved:
            raise ValueError(f"figures are missing verified artifact ids: {unresolved}")
        if self.artifact_service is not None:
            for block in figures:
                assert block.figure is not None and block.figure.artifact_id is not None
                self.artifact_service.verify(
                    self.artifact_service.get(str(block.figure.artifact_id), verify=True)
                )
        required = [
            str(block.figure.artifact_id)
            for block in figures
            if block.figure is not None and block.figure.artifact_id is not None
        ]
        manifest = document.asset_manifest or manifest_from_document(document)
        barrier = (
            AssetBarrier(self.artifact_service).evaluate(
                required,
                image_required=manifest.image_required,
                expected_count=manifest.required_figure_count,
            )
            if self.artifact_service is not None
            else None
        )
        if barrier is not None and not barrier.ready:
            raise ValueError(f"document asset barrier failed: {barrier.model_dump(mode='json')}")
        return cast(
            JsonValue,
            {
                "document_ir": document.canonical_payload(),
                "resolved": len(required),
                "unresolved": [],
                "asset_barrier": "ready",
                "expected_count": manifest.required_figure_count,
                "ready_count": barrier.ready_count if barrier is not None else len(required),
            },
        )

    def bind_assets(self, arguments: dict[str, JsonValue]) -> JsonValue:
        document_id = UUID(str(arguments["document_id"]))
        store = self.store
        document = store.load(
            document_id,
            int(str(arguments["revision"])) if arguments.get("revision") is not None else None,
        )
        if self.artifact_service is None:
            raise RuntimeError("document asset binding requires an artifact service")
        binding = ArtifactBinder(self.artifact_service).bind(
            document,
            source_run_id=str(
                arguments.get("source_run_id")
                or document.metadata.get("source_run_id")
                or self.run_id
                or ""
            )
            or None,
            source_message_id=str(arguments.get("source_message_id") or "") or None,
            image_required=(
                bool(arguments["image_required"])
                if arguments.get("image_required") is not None
                else None
            ),
        )
        if not binding.ready:
            status = (
                "pending"
                if binding.pending and not (binding.missing or binding.invalid or binding.ambiguous)
                else "blocked"
            )
            if status == "pending":
                checkpoint_store = AssetBarrierCheckpointStore(self.artifact_service.project_root)
                existing = checkpoint_store.load(document.document_id, document.revision)
                barrier_status = (
                    "pending_timeout"
                    if existing is not None and existing.status == "pending" and existing.expired
                    else "pending"
                )
                checkpoint = checkpoint_store.save_pending(
                    document_id=document.document_id,
                    revision=document.revision,
                    pending_logical_ids=binding.pending,
                    source_run_id=str(arguments.get("source_run_id") or "") or None,
                    source_message_id=str(arguments.get("source_message_id") or "") or None,
                )
                return cast(
                    JsonValue,
                    {
                        "document_id": str(document.document_id),
                        "revision": document.revision,
                        "asset_barrier": barrier_status,
                        "ready": False,
                        "pending": binding.pending,
                        "missing": [],
                        "invalid": [],
                        "ambiguous": [],
                        "resume_from": "document_asset_barrier",
                        "checkpoint": checkpoint.model_dump(mode="json"),
                    },
                )
            raise ValueError(
                "document asset binding failed: "
                f"missing={binding.missing}, pending={binding.pending}, "
                f"invalid={binding.invalid}, "
                f"ambiguous={[item.logical_id for item in binding.ambiguous]}"
            )
        bound = binding.document
        if bound.hashes().asset_set_hash != document.hashes().asset_set_hash:
            bound = bound.model_copy(update={"revision": document.revision + 1})
            store.save(
                bound,
                parent_revision_id=store.revision_id(document.document_id, document.revision),
                source_run_id=str(
                    arguments.get("source_run_id")
                    or document.metadata.get("source_run_id")
                    or self.run_id
                    or ""
                )
                or None,
                source_conversation_id=str(
                    document.metadata.get("source_conversation_id") or self.conversation_id or ""
                )
                or None,
            )
        AssetBarrierCheckpointStore(self.artifact_service.project_root).mark_ready(
            bound.document_id, bound.revision
        )
        return cast(
            JsonValue,
            {
                "document": bound.canonical_payload(),
                "document_id": str(bound.document_id),
                "revision": bound.revision,
                "bindings": [item.model_dump(mode="json") for item in binding.bindings],
                "asset_barrier": "ready",
                "ready": True,
                "expected_count": (
                    bound.asset_manifest.required_figure_count
                    if bound.asset_manifest is not None
                    else 0
                ),
                "bound_count": len(binding.bindings),
                "ready_count": len(binding.bindings),
            },
        )

    def asset_derive(self, arguments: dict[str, JsonValue]) -> JsonValue:
        if self.artifact_service is None:
            raise RuntimeError("asset derivatives require an artifact service")
        document_ir = arguments["document_ir"]
        if not isinstance(document_ir, dict):
            raise ValueError("document_ir must be an object")
        document = migrate_document_ir(cast(dict[str, object], document_ir))
        if self.artifact_service is not None:
            binding = ArtifactBinder(self.artifact_service).bind(
                document,
                source_run_id=str(document.metadata.get("source_run_id") or self.run_id or "")
                or None,
            )
            if not binding.ready:
                raise ValueError(
                    "document asset binding failed before derivative assembly: "
                    f"missing={binding.missing}, pending={binding.pending}, "
                    f"invalid={binding.invalid}"
                )
            document = binding.document
        formats = arguments.get("formats")
        if not isinstance(formats, list):
            raise ValueError("formats must be a list")
        assembled = AssetAssembler(self.artifact_service).assemble(
            document, target_formats=[str(item) for item in formats]
        )
        return cast(
            JsonValue,
            {
                "document_ir": assembled.document.canonical_payload(),
                "derivatives": [item.model_dump(mode="json") for item in assembled.derivatives],
                "asset_barrier": "ready",
            },
        )

    def citation_format(self, arguments: dict[str, JsonValue]) -> JsonValue:
        raw = arguments.get("items")
        if not isinstance(raw, list):
            raise ValueError("citation items must be a list")
        style = CitationStyle(str(arguments["style"]))
        formatted = [
            CitationStyleService()
            .format(BibliographicItem.model_validate(item), style, sequence=index)
            .model_dump(mode="json")
            for index, item in enumerate(raw, start=1)
        ]
        return cast(JsonValue, {"style": style.value, "citations": formatted})

    def layout_resolve(self, arguments: dict[str, JsonValue]) -> JsonValue:
        archetype = ArchetypeId(str(arguments["archetype"]))
        raw_formats = arguments.get("formats")
        if not isinstance(raw_formats, list):
            raise ValueError("formats must be a list")
        profile = archetype_layout_profile(
            archetype,
            language=str(arguments.get("language") or "zh"),
            explicit_theme=str(arguments.get("theme_id") or "") or None,
            project_theme=str(arguments.get("project_theme_id") or "") or None,
        )
        plan = RenderPlanService().negotiate(
            [str(item) for item in raw_formats],
            layout_profile=archetype.value,
            word_available=bool(arguments.get("word_available", True)),
            tex_available=bool(arguments.get("tex_available", True)),
        )
        return cast(
            JsonValue,
            {
                "layout_profile": profile.model_dump(mode="json"),
                "render_plan": plan.model_dump(mode="json"),
            },
        )

    def qa(self, arguments: dict[str, JsonValue]) -> JsonValue:
        if self.artifact_service is None:
            raise RuntimeError("document QA requires an artifact service")
        raw_ids = arguments.get("artifact_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("artifact_ids must be a list")
        issues: list[dict[str, str | None]] = []
        checked: list[str] = []
        checked_documents: set[str] = set()
        raw_expectation = arguments.get("presentation_expectation")
        if raw_expectation is not None and not isinstance(raw_expectation, dict):
            raise ValueError("presentation_expectation must be an object")
        expectation = (
            PresentationExpectationManifest.model_validate(raw_expectation)
            if isinstance(raw_expectation, dict)
            else None
        )
        for artifact_id in raw_ids:
            artifact = self.artifact_service.get(str(artifact_id), verify=True)
            path = self.artifact_service.verify(artifact)
            checked.append(artifact.id)
            if not artifact.document_id or not artifact.revision_id:
                issues.append({"artifact_id": artifact.id, "code": "REVISION_LINK_MISSING"})
            elif artifact.document_id not in checked_documents:
                checked_documents.add(artifact.document_id)
                document = self.store.load(UUID(artifact.document_id))
                if expectation is not None:
                    format_name = {
                        ".md": "md",
                        ".zip": "md_bundle",
                        ".docx": "docx",
                        ".pdf": "pdf",
                    }.get(path.suffix.casefold(), "")
                    preflight = RenderPreflight(self.artifact_service).validate(
                        document,
                        format_name=format_name,
                        presentation_expectation=expectation,
                    )
                    issues.extend(
                        {
                            "artifact_id": artifact.id,
                            "code": "PRESENTATION_PREFLIGHT_FAILED",
                        }
                        for _ in preflight.issues
                    )
                numbering_report = NumberingInspector().inspect(document)
                issues.extend(
                    {
                        "artifact_id": artifact.id,
                        "code": diagnostic.code,
                        "node_id": diagnostic.node_id,
                        "repair_node": diagnostic.repair_node,
                    }
                    for diagnostic in numbering_report.diagnostics
                    if diagnostic.severity.value in {"warning", "error"}
                )
            text = ""
            if path.suffix.casefold() == ".md":
                text = path.read_text(encoding="utf-8")
                if re.search(r"^\\(?:#|\*|-)", text, re.M):
                    issues.append({"artifact_id": artifact.id, "code": "MARKDOWN_ESCAPE_LEAK"})
            elif path.suffix.casefold() == ".docx":
                with ZipFile(path) as archive:
                    names = set(archive.namelist())
                    if "word/document.xml" not in names:
                        issues.append(
                            {"artifact_id": artifact.id, "code": "DOCX_STRUCTURE_MISSING"}
                        )
                    else:
                        text = archive.read("word/document.xml").decode("utf-8", errors="replace")
                    if bool(arguments.get("require_images")) and not any(
                        name.startswith("word/media/") for name in names
                    ):
                        issues.append(
                            {"artifact_id": artifact.id, "code": "EMBEDDED_IMAGE_MISSING"}
                        )
            elif path.suffix.casefold() == ".pdf":
                reader = PdfReader(path)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                if not reader.pages:
                    issues.append({"artifact_id": artifact.id, "code": "PDF_PAGE_MISSING"})
                if bool(arguments.get("require_images")) and not any(
                    page.images for page in reader.pages
                ):
                    issues.append({"artifact_id": artifact.id, "code": "EMBEDDED_IMAGE_MISSING"})
            if any(marker in text for marker in self.PRIVATE_MARKERS):
                issues.append({"artifact_id": artifact.id, "code": "PRIVATE_PLACEHOLDER"})
        return cast(JsonValue, {"passed": not issues, "checked": checked, "issues": issues})

    def validate_delivery(self, arguments: dict[str, JsonValue]) -> JsonValue:
        if self.artifact_service is None:
            raise RuntimeError("delivery validation requires an artifact service")
        document_id = UUID(str(arguments["document_id"]))
        revision = int(str(arguments["revision"]))
        document = self.store.load(document_id, revision)
        raw_expectation = arguments.get("presentation_expectation")
        if raw_expectation is not None and not isinstance(raw_expectation, dict):
            raise ValueError("presentation_expectation must be an object")
        expectation = (
            PresentationExpectationManifest.model_validate(raw_expectation)
            if isinstance(raw_expectation, dict)
            else None
        )
        raw_ids = arguments.get("artifact_ids")
        if not isinstance(raw_ids, list):
            raise ValueError("artifact_ids must be a list")
        required_images = (
            document.asset_manifest.required_figure_count
            if document.asset_manifest is not None
            else len([block for block in document.iter_blocks() if block.figure is not None])
        )
        issues: list[dict[str, object]] = []
        checked: list[str] = []
        expected_revision_id = self.store.revision_id(document_id, revision)
        for raw_id in raw_ids:
            artifact = self.artifact_service.get(str(raw_id), verify=True)
            path = self.artifact_service.verify(artifact)
            checked.append(artifact.id)
            if (
                artifact.document_id != str(document_id)
                or artifact.revision_id != expected_revision_id
            ):
                issues.append(
                    {
                        "category": "validation_error",
                        "artifact_id": artifact.id,
                        "message": "artifact lineage does not match the requested revision",
                        "repair_node": "document_render",
                    }
                )
                continue
            if artifact.delivery_status != "delivered":
                issues.append(
                    {
                        "category": "validation_error",
                        "artifact_id": artifact.id,
                        "message": "artifact is not in delivered state",
                        "repair_node": "document_render",
                    }
                )
                continue
            format_name = (
                "md_bundle" if path.suffix.casefold() == ".zip" else path.suffix.casefold()[1:]
            )
            if expectation is not None:
                preflight = RenderPreflight(self.artifact_service).validate(
                    document,
                    format_name=format_name,
                    presentation_expectation=expectation,
                )
                issues.extend(item.model_dump(mode="json") for item in preflight.issues)
            result = RenderedArtifactValidator().validate(
                path,
                format_name=format_name,
                required_image_count=required_images,
                document_id=document_id,
                revision=revision,
                document=document,
                presentation_expectation=expectation,
            )
            issues.extend(item.model_dump(mode="json") for item in result.issues)
        return cast(
            JsonValue,
            {
                "passed": not issues,
                "checked": checked,
                "document_id": str(document_id),
                "revision": revision,
                "required_image_count": required_images,
                "presentation_hash": document.hashes().presentation_hash,
                "numbering_hash": document.hashes().numbering_hash,
                "presentation_expectation_hash": (
                    expectation.expectation_hash if expectation is not None else None
                ),
                "issues": issues,
            },
        )

    def revision_lookup(self, arguments: dict[str, JsonValue]) -> JsonValue:
        result = RevisionResolver(self.store).resolve(
            str(arguments.get("reference", "")),
            document_id=(
                UUID(str(arguments["document_id"])) if arguments.get("document_id") else None
            ),
            revision=(
                int(str(arguments["revision"])) if arguments.get("revision") is not None else None
            ),
            conversation_id=str(arguments.get("conversation_id") or self.conversation_id or "")
            or None,
        )
        return result.model_dump(mode="json")

    def revision_patch(self, arguments: dict[str, JsonValue]) -> JsonValue:
        document_id = UUID(str(arguments["document_id"]))
        operation = RevisionOperation.model_validate(arguments["operation"])
        return (
            RevisionWorkflow(self.store)
            .apply(self.store.load(document_id), operation)
            .model_dump(mode="json")
        )
