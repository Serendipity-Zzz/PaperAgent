from __future__ import annotations

import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from paperagent.agents.document_ir import (
    BlockKind,
    CitationRef,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.agents.evidence import EvidenceBundle
from paperagent.agents.outline import OutlinePlan, SectionPlan
from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.prompts import (
    CompiledPrompt,
    PromptCompiler,
    PromptSelectionContext,
    default_prompt_compiler,
)
from paperagent.providers import ChatMessage, ChatRequest, ModelProvider


class DraftBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: BlockKind = BlockKind.PARAGRAPH
    text: str = Field(min_length=1)
    evidence_ids: list[UUID] = Field(default_factory=list)
    author_viewpoint: bool = False
    important_claim: bool = True


class SectionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outline_section_id: UUID
    blocks: list[DraftBlock] = Field(min_length=1)


class SectionWriterAgent:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        max_block_chars: int = 3000,
        prompt_compiler: PromptCompiler | None = None,
    ) -> None:
        self.provider = provider
        self.max_block_chars = max_block_chars
        self.prompt_compiler = prompt_compiler or default_prompt_compiler()
        self.last_compiled_prompt: CompiledPrompt | None = None
        self.last_schema_repair_count = 0
        self.last_schema_errors: list[str] = []
        self.last_schema_projection_used = False

    async def generate_section(
        self,
        requirement: RequirementSpec,
        section: SectionPlan,
        evidence: EvidenceBundle,
    ) -> SectionDraft:
        if self.provider is None:
            raise RuntimeError("writer provider is not configured")
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("writing requires confirmed requirements")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        self.last_compiled_prompt = self.prompt_compiler.compile(
            PromptSelectionContext(
                agent_type="writer_agent",
                task="write_section",
                document_type=confirmed.document_type.value,
                language=confirmed.primary_language.value,
            ),
            [
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "section": section.model_dump(mode="json"),
                            "evidence": [
                                item.model_dump(mode="json") for item in evidence.items
                            ],
                            "writer_contract": {
                                "outline_section_id": str(section.section_id),
                                "allowed_evidence_ids": [
                                    str(item.evidence_id) for item in evidence.items
                                ],
                                "rules": [
                                    "Return only the SectionDraft JSON object.",
                                    "Copy outline_section_id exactly.",
                                    "Every prose block must either list one or more allowed "
                                    "evidence_ids or set author_viewpoint=true.",
                                    "Use author_viewpoint=true only for an explicitly framed "
                                    "author interpretation.",
                                    "Do not invent citation IDs, facts, measurements, or sources.",
                                ],
                                "minimal_block_example": {
                                    "kind": "paragraph",
                                    "text": "Write the supported statement here.",
                                    "evidence_ids": [
                                        str(evidence.items[0].evidence_id)
                                    ]
                                    if evidence.items
                                    else [],
                                    "author_viewpoint": False,
                                    "important_claim": True,
                                },
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )
        messages = list(self.last_compiled_prompt.messages)
        self.last_schema_repair_count = 0
        self.last_schema_errors = []
        self.last_schema_projection_used = False
        allowed_evidence = {item.evidence_id for item in evidence.items}
        for attempt in range(3):
            response = await self.provider.chat(
                ChatRequest(
                    messages=messages,
                    response_schema=SectionDraft.model_json_schema(),
                    temperature=0,
                    idempotency_key=(
                        self.last_compiled_prompt.prompt_hash
                        if attempt == 0
                        else f"{self.last_compiled_prompt.prompt_hash}:schema-repair:{attempt}"
                    ),
                )
            )
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip())
            try:
                draft = SectionDraft.model_validate_json(content)
                self._validate_draft(draft, section.section_id, allowed_evidence)
                return draft
            except (ValidationError, ValueError) as error:
                self.last_schema_repair_count += 1
                error_text = str(error)[:6_000]
                self.last_schema_errors.append(error_text)
                projected = self._project_draft(
                    content,
                    section_id=section.section_id,
                    allowed_evidence=allowed_evidence,
                )
                if projected is not None:
                    self.last_schema_projection_used = True
                    return projected
                if attempt == 2:
                    raise
                messages.extend(
                    [
                        ChatMessage(role="assistant", content=response.content),
                        ChatMessage(
                            role="user",
                            content=(
                                "The previous section JSON failed strict schema or evidence "
                                "validation. Rewrite the same section without adding facts. "
                                "Return only one JSON object matching the supplied schema. "
                                f"outline_section_id must be {section.section_id}. Only these "
                                "evidence_ids are allowed: "
                                f"{sorted(str(item) for item in allowed_evidence)}. Important "
                                "Every prose block requires at least one allowed evidence_id or "
                                "an explicit author_viewpoint=true marker. "
                                f"Validation errors:\n{error_text}"
                            ),
                        ),
                    ]
                )
        raise RuntimeError("writer schema repair loop ended without a draft")

    @staticmethod
    def _validate_draft(
        draft: SectionDraft, section_id: UUID, allowed_evidence: set[UUID]
    ) -> None:
        if draft.outline_section_id != section_id:
            raise ValueError("writer returned a draft for the wrong outline section")
        for block in draft.blocks:
            unknown = set(block.evidence_ids) - allowed_evidence
            if unknown:
                raise ValueError(f"writer used unknown evidence ids: {sorted(map(str, unknown))}")
            if block.kind is BlockKind.PARAGRAPH and not (
                block.evidence_ids or block.author_viewpoint
            ):
                raise ValueError(
                    "prose block has neither evidence nor author viewpoint marker"
                )

    @classmethod
    def _project_draft(
        cls,
        content: str,
        *,
        section_id: UUID,
        allowed_evidence: set[UUID],
    ) -> SectionDraft | None:
        """Normalize common section aliases without inventing text or citation IDs."""

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        raw_section_id = raw.get("outline_section_id", raw.get("section_id", section_id))
        raw_blocks = raw.get("blocks", raw.get("content_blocks", raw.get("paragraphs")))
        if raw_blocks is None:
            text = raw.get("text", raw.get("content"))
            if isinstance(text, str) and text.strip():
                raw_blocks = [
                    {
                        "text": text,
                        "evidence_ids": raw.get(
                            "evidence_ids", raw.get("citations", [])
                        ),
                        "author_viewpoint": raw.get("author_viewpoint", False),
                        "important_claim": raw.get("important_claim", True),
                    }
                ]
        if not isinstance(raw_blocks, list) or not raw_blocks:
            return None
        blocks: list[dict[str, object]] = []
        for item in raw_blocks:
            if isinstance(item, str):
                blocks.append({"text": item})
                continue
            if not isinstance(item, dict):
                return None
            text = item.get("text", item.get("content"))
            if not isinstance(text, str) or not text.strip():
                return None
            evidence_ids = item.get("evidence_ids", item.get("citations", []))
            if evidence_ids is None:
                evidence_ids = []
            blocks.append(
                {
                    "kind": item.get("kind", item.get("type", "paragraph")),
                    "text": text,
                    "evidence_ids": evidence_ids,
                    "author_viewpoint": item.get("author_viewpoint") is True,
                    "important_claim": item.get("important_claim", True) is not False,
                }
            )
        try:
            draft = SectionDraft.model_validate(
                {"outline_section_id": raw_section_id, "blocks": blocks}
            )
            cls._validate_draft(draft, section_id, allowed_evidence)
            return draft
        except (ValidationError, ValueError):
            return None

    def assemble(
        self,
        requirement: RequirementSpec,
        outline: OutlinePlan,
        evidence: EvidenceBundle,
        drafts: list[SectionDraft],
        *,
        title: str,
    ) -> DocumentIR:
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("writing requires confirmed requirements")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        available = {item.evidence_id for item in evidence.items}
        evidence_by_id = {item.evidence_id: item for item in evidence.items}
        by_section = {draft.outline_section_id: draft for draft in drafts}
        seen_text: set[str] = set()
        sections: list[DocumentSection] = []
        for planned in outline.sections:
            draft = by_section.get(planned.section_id)
            if draft is None:
                raise ValueError(f"missing draft for section: {planned.title}")
            blocks: list[DocumentBlock] = []
            for candidate in draft.blocks:
                unknown = set(candidate.evidence_ids) - available
                if unknown:
                    raise ValueError(
                        f"writer used unknown evidence ids: {sorted(map(str, unknown))}"
                    )
                if candidate.kind is BlockKind.PARAGRAPH and not (
                    candidate.evidence_ids or candidate.author_viewpoint
                ):
                    raise ValueError(
                        "prose block has neither evidence nor author viewpoint marker"
                    )
                for text in self._split(candidate.text):
                    normalized = re.sub(r"\s+", "", text).casefold()
                    if normalized in seen_text:
                        continue
                    seen_text.add(normalized)
                    blocks.append(
                        DocumentBlock(
                            kind=candidate.kind,
                            text=text,
                            provenance=Provenance(
                                agent="section_writer",
                                evidence_ids=candidate.evidence_ids,
                                author_viewpoint=candidate.author_viewpoint,
                            ),
                            citations=[
                                CitationRef(
                                    evidence_id=evidence_id,
                                    locator=json.dumps(
                                        evidence_by_id[evidence_id].locator,
                                        ensure_ascii=False,
                                    ),
                                    verified=(
                                        evidence_by_id[evidence_id].verification.value
                                        == "verified"
                                    ),
                                )
                                for evidence_id in candidate.evidence_ids
                            ],
                        )
                    )
            sections.append(
                DocumentSection(
                    outline_section_id=planned.section_id,
                    title=planned.title,
                    goal=planned.goal,
                    blocks=blocks,
                )
            )
        return DocumentIR(
            requirement_id=confirmed.requirement_id,
            requirement_version=confirmed.requirement_version,
            outline_id=outline.outline_id,
            title=title,
            language=confirmed.primary_language.value,
            typography=confirmed.typography,
            sections=sections,
            metadata={
                "evidence_manifest": [
                    {
                        "evidence_id": str(item.evidence_id),
                        "title": item.title,
                        "source_uri": item.source_uri,
                        "locator": item.locator,
                        "verification": item.verification.value,
                        "scholarly_citation": item.scholarly_citation,
                    }
                    for item in evidence.items
                ]
            },
        )

    def _split(self, text: str) -> list[str]:
        if len(text) <= self.max_block_chars:
            return [text]
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
        result: list[str] = []
        for paragraph in paragraphs or [text]:
            for start in range(0, len(paragraph), self.max_block_chars):
                result.append(paragraph[start : start + self.max_block_chars])
        return result
