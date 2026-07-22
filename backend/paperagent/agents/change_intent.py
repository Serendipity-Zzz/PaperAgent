from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from paperagent.agents.document_ir import (
    DocumentIR,
    TypographyOverrideScope,
)
from paperagent.preview.schemas import Annotation
from paperagent.prompts import PromptSelectionContext, default_prompt_compiler
from paperagent.providers import ChatMessage, ChatRequest, ModelProvider
from paperagent.schemas.typography import TypographySpec, extract_typography

if TYPE_CHECKING:
    from paperagent.rendering.artifacts import AnchorBinding


class ChangeScope(StrEnum):
    GLOBAL = "global"
    SECTION = "section"
    BLOCK = "block"


class ChangeIntent(BaseModel):
    scope: ChangeScope
    section_ids: list[UUID] = Field(default_factory=list)
    block_ids: list[UUID] = Field(default_factory=list)
    typography_patch: dict[str, object] = Field(default_factory=dict)
    changes_content: bool = False
    clarification: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> ChangeIntent:
        if self.scope is ChangeScope.SECTION and not self.section_ids:
            raise ValueError("section change requires section_ids")
        if self.scope is ChangeScope.BLOCK and not self.block_ids:
            raise ValueError("block change requires block_ids")
        allowed = set(TypographySpec.model_fields)
        unknown = set(self.typography_patch) - allowed
        if unknown:
            raise ValueError(f"unknown typography fields: {sorted(unknown)}")
        TypographySpec.model_validate(self.typography_patch)
        return self


class TypographyImpact(BaseModel):
    content_regeneration_required: bool
    rerender_formats: list[str]
    affected_sections: list[UUID]
    affected_blocks: list[UUID]
    reason: str


class TypographyPatchPreview(BaseModel):
    intent: ChangeIntent
    impact: TypographyImpact
    before_hashes: dict[str, str]
    after_hashes: dict[str, str]
    content_preserved: bool
    affected_formats: list[str]


class ChangeIntentAgent:
    def __init__(self, provider: ModelProvider | None = None) -> None:
        self.provider = provider

    async def understand(self, request: str) -> ChangeIntent:
        if self.provider is not None:
            compiled = default_prompt_compiler().compile(
                PromptSelectionContext(
                    agent_type="change_intent_agent",
                    task="typography_change",
                    features={"typography"},
                ),
                [ChatMessage(role="user", content=request)],
            )
            response = await self.provider.chat(
                ChatRequest(
                    messages=compiled.messages,
                    response_schema=ChangeIntent.model_json_schema(),
                    temperature=0,
                )
            )
            return ChangeIntent.model_validate_json(response.content)
        typography, matched = extract_typography(request)
        if not matched:
            return ChangeIntent(
                scope=ChangeScope.GLOBAL,
                clarification="未识别到可验证的字体或版式字段; 请说明作用范围和目标值。",
            )
        return ChangeIntent(
            scope=ChangeScope.GLOBAL,
            typography_patch=typography.model_dump(exclude_none=True),
            changes_content=False,
        )

    async def understand_annotation(
        self, annotation: Annotation, binding: AnchorBinding
    ) -> ChangeIntent:
        """Convert a preview annotation into a block-anchored change request."""

        intent = await self.understand(annotation.body)
        if intent.scope is ChangeScope.GLOBAL:
            intent = intent.model_copy(
                update={"scope": ChangeScope.BLOCK, "block_ids": [binding.block_id]}
            )
        return ChangeIntent.model_validate(intent.model_dump())

    @staticmethod
    def apply(document: DocumentIR, intent: ChangeIntent) -> tuple[DocumentIR, TypographyImpact]:
        if intent.clarification:
            raise ValueError(intent.clarification)
        if intent.changes_content:
            raise ValueError("content changes must use the targeted repair chain")
        if intent.scope is ChangeScope.GLOBAL:
            typography = document.typography.model_copy(update=intent.typography_patch)
            updated = document.restyle(typography)
            affected_sections = [section.section_id for section in document.sections]
            affected_blocks = [
                block.block_id for section in document.sections for block in section.blocks
            ]
        elif intent.scope is ChangeScope.SECTION:
            updated = document.restyle_targets(
                scope=TypographyOverrideScope.SECTION,
                target_ids=intent.section_ids,
                patch=intent.typography_patch,
            )
            affected_sections = list(intent.section_ids)
            affected_blocks = [
                block.block_id
                for section in document.sections
                if section.section_id in set(intent.section_ids)
                for block in section.blocks
            ]
        else:
            updated = document.restyle_targets(
                scope=TypographyOverrideScope.BLOCK,
                target_ids=intent.block_ids,
                patch=intent.typography_patch,
            )
            affected_blocks = list(intent.block_ids)
            affected_sections = [
                section.section_id
                for section in document.sections
                if any(block.block_id in set(intent.block_ids) for block in section.blocks)
            ]
        return updated, TypographyImpact(
            content_regeneration_required=False,
            rerender_formats=["docx", "typst", "latex", "pdf"],
            affected_sections=affected_sections,
            affected_blocks=affected_blocks,
            reason=(
                "typography-only patch preserves Document IR text and invalidates only "
                f"the {intent.scope.value} render dependency scope"
            ),
        )

    @staticmethod
    def dry_run(document: DocumentIR, intent: ChangeIntent) -> TypographyPatchPreview:
        """Resolve a typography patch without persisting or rendering a revision."""

        updated, impact = ChangeIntentAgent.apply(document, intent)
        before = document.hashes().model_dump(mode="json")
        after = updated.hashes().model_dump(mode="json")
        return TypographyPatchPreview(
            intent=intent,
            impact=impact,
            before_hashes=before,
            after_hashes=after,
            content_preserved=before["content_hash"] == after["content_hash"],
            affected_formats=impact.rerender_formats,
        )
