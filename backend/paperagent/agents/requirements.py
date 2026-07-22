from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from paperagent.agents.state import (
    Assumption,
    DocumentType,
    EvidenceSource,
    FieldEvidence,
    OpenQuestion,
    OutputFormat,
    PrimaryLanguage,
    QuestionPriority,
    RawRequest,
    RequirementSpec,
    RequirementStatus,
    RequirementVersionHistory,
    ResearchFormulation,
    ReviewLevel,
    TargetLength,
    TranslationRequirement,
)
from paperagent.presentation import enrich_requirement_presentation
from paperagent.prompts import (
    CompiledPrompt,
    PromptCompiler,
    PromptSelectionContext,
    default_prompt_compiler,
)
from paperagent.providers import ChatMessage, ChatRequest, ModelProvider
from paperagent.schemas.presentation import RequirementPresentationSpec
from paperagent.schemas.typography import TypographySpec

REQUIRED_FIELDS = (
    "normalized_request",
    "research_formulation.research_topic",
    "document_type",
    "primary_language",
    "target_length",
    "output_formats",
)
IMPACT = {
    "document_type": {"outline", "evidence", "writing", "review", "render"},
    "research_formulation": {"outline", "evidence", "experiment", "writing", "review"},
    "target_length": {"outline", "writing", "word_count", "review", "render"},
    "requires_experiment": {"experiment", "data_chart", "writing", "review"},
    "requires_data_chart": {"data_chart", "writing", "review", "render"},
    "requires_generated_image": {"image", "writing", "review", "render"},
    "output_formats": {"render"},
    "template_id": {"outline", "render", "review"},
    "presentation": {"presentation", "render", "review"},
    "typography": {"render", "review"},
}


class ContextFact(BaseModel):
    fact_id: str
    source_type: EvidenceSource
    text: str
    locator: str | None = None
    confidence: float = Field(default=1, ge=0, le=1)
    instruction_trust: bool = False
    field_path: str | None = None
    value: object | None = None


class LocalFeasibility(BaseModel):
    feasible: bool | None = None
    reason: str | None = None
    requires_approval: bool = False


class RequirementCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalized_request: str = ""
    research_formulation: ResearchFormulation = Field(default_factory=ResearchFormulation)
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
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    field_evidence: dict[str, FieldEvidence] = Field(default_factory=dict)
    acceptance_criteria: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)


class FieldDecisionKind(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    MODIFY = "modify"


class FieldDecision(BaseModel):
    field_path: str
    decision: FieldDecisionKind
    value: object | None = None


class RequirementFieldDiff(BaseModel):
    field_path: str
    before: object | None = None
    after: object | None = None


class RequirementChange(BaseModel):
    previous_version: int
    current_version: int
    changes: list[RequirementFieldDiff]
    invalidated_nodes: set[str]


class PlannedCapability(BaseModel):
    node: str
    reason: str
    approval_required: bool = False


def read_path(model: BaseModel, path: str) -> object:
    value: object = model
    for part in path.split("."):
        if isinstance(value, BaseModel):
            value = getattr(value, part)
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def set_path(payload: dict[str, object], path: str, value: object) -> None:
    target = payload
    parts = path.split(".")
    for part in parts[:-1]:
        nested = target.setdefault(part, {})
        if not isinstance(nested, dict):
            raise ValueError(f"field path crosses scalar: {path}")
        target = nested
    target[parts[-1]] = value


class RequirementValidator:
    def __init__(self, threshold: float = 0.65) -> None:
        self.threshold = threshold

    def evaluate(
        self,
        spec: RequirementSpec,
        *,
        context: list[ContextFact] | None = None,
        feasibility: LocalFeasibility | None = None,
    ) -> RequirementSpec:
        facts = {fact.fact_id: fact for fact in context or []}
        questions: list[OpenQuestion] = []
        conflicts = list(spec.conflicts)
        conflicts.extend(self.context_conflicts(list(facts.values())))
        for path in REQUIRED_FIELDS:
            value = read_path(spec, path)
            evidence = spec.field_evidence.get(path)
            if value is None or value == "" or value == []:
                questions.append(self.question(path, f"请确认 {path}。", "必要字段缺失"))
            elif evidence is None:
                questions.append(self.question(path, f"请确认 {path} 的来源。", "字段没有证据"))
            else:
                conflicts.extend(self.evidence_conflicts(path, evidence, spec.raw_request, facts))
                if evidence.confidence < self.threshold:
                    questions.append(
                        self.question(path, f"请补充或修正 {path}。", "字段置信度低于门限")
                    )
                elif evidence.requires_confirmation or evidence.source_type in {
                    EvidenceSource.AGENT_INFERENCE,
                    EvidenceSource.MEMORY,
                }:
                    questions.append(
                        self.question(
                            path,
                            f"是否接受系统对 {path} 的候选理解?",
                            f"来源为 {evidence.source_type.value}",
                            QuestionPriority.IMPORTANT,
                        )
                    )
        conflicts.extend(self.numeric_conflicts(spec, facts))
        questions.extend(self.ambiguities(spec))
        for ambiguity in spec.presentation.unresolved:
            questions.append(
                self.question(
                    ambiguity.field_path,
                    ambiguity.message,
                    ambiguity.code,
                )
            )
        if (
            spec.requires_experiment
            and feasibility
            and (feasibility.feasible is False or feasibility.requires_approval)
        ):
            questions.append(
                self.question(
                    "requires_experiment",
                    "是否调整实验范围或授权环境扫描?",
                    feasibility.reason or "实验需可行性或权限确认",
                )
            )
        for conflict in conflicts:
            questions.append(self.question("conflicts", "存在冲突, 请选择采用的版本。", conflict))
        priority = {QuestionPriority.BLOCKING: 0, QuestionPriority.IMPORTANT: 1}
        unique: dict[tuple[str, str], OpenQuestion] = {}
        for question in questions:
            unique.setdefault((question.affected_fields[0], question.question), question)
        ranked = sorted(unique.values(), key=lambda item: priority.get(item.priority, 2))[:5]
        status = (
            RequirementStatus.NEEDS_INPUT
            if conflicts or any(item.priority is QuestionPriority.BLOCKING for item in ranked)
            else RequirementStatus.AWAITING_CONFIRMATION
        )
        return RequirementSpec.model_validate(
            spec.model_dump()
            | {
                "status": status,
                "open_questions": [item.model_dump() for item in ranked],
                "conflicts": list(dict.fromkeys(conflicts)),
            }
        )

    @staticmethod
    def question(
        path: str,
        text: str,
        reason: str,
        priority: QuestionPriority = QuestionPriority.BLOCKING,
    ) -> OpenQuestion:
        return OpenQuestion(question=text, reason=reason, affected_fields=[path], priority=priority)

    @staticmethod
    def evidence_conflicts(
        path: str,
        evidence: FieldEvidence,
        raw: RawRequest,
        facts: dict[str, ContextFact],
    ) -> list[str]:
        result: list[str] = []
        if evidence.source_type is EvidenceSource.EXPLICIT_USER and not set(
            evidence.source_refs
        ) <= (set(raw.message_ids) | {"$raw"}):
            result.append(f"{path} 将附件或未知来源误标为 explicit_user")
        for ref in evidence.source_refs:
            fact = facts.get(ref)
            if fact and (fact.instruction_trust or fact.source_type != evidence.source_type):
                result.append(f"{path} 的来源类型或指令权限不合法: {ref}")
        return result

    @staticmethod
    def numeric_conflicts(spec: RequirementSpec, facts: dict[str, ContextFact]) -> list[str]:
        source = " ".join([spec.raw_request.text, *(fact.text for fact in facts.values())])
        generated = " ".join(
            [
                spec.normalized_request,
                spec.research_formulation.research_topic or "",
                spec.research_formulation.research_objective or "",
                *spec.research_formulation.research_questions,
                *spec.research_formulation.hypotheses,
                *spec.research_formulation.data_requirements,
            ]
        )
        pattern = r"\b\d+(?:\.\d+)?%?\b"
        invented = (
            set(re.findall(pattern, generated))
            - set(re.findall(pattern, source))
            - {str(item.proposed_value) for item in spec.assumptions}
        )
        return [f"科学化表述出现无法追溯的数值 {value}" for value in sorted(invented)]

    @staticmethod
    def context_conflicts(facts: list[ContextFact]) -> list[str]:
        values: dict[str, set[str]] = {}
        for fact in facts:
            if fact.field_path and fact.value is not None:
                values.setdefault(fact.field_path, set()).add(
                    json.dumps(fact.value, ensure_ascii=False, sort_keys=True)
                )
        return [
            f"附件、模板或记忆对 {path} 给出了互相冲突的值"
            for path, candidates in values.items()
            if len(candidates) > 1
        ]

    @staticmethod
    def ambiguities(spec: RequirementSpec) -> list[OpenQuestion]:
        rules = (
            (r"有数据|数据支撑", "requires_data_chart", "数据从何处取得, 是否为真实数据?"),
            (r"跑实验|做实验|复现实验", "requires_experiment", "实验代码、数据和授权边界是什么?"),
            (r"现场图", "requires_generated_image", "现场图是 AI 示意图还是真实记录?"),
            (r"引用文献|参考文献", "requires_literature_search", "是否允许联网检索开放文献?"),
        )
        return [
            RequirementValidator.question(path, question, "真实性、来源或授权边界不清")
            for pattern, path, question in rules
            if re.search(pattern, spec.raw_request.text)
        ]


class RequirementUnderstandingAgent:
    def __init__(
        self,
        provider: ModelProvider,
        validator: RequirementValidator | None = None,
        prompt_compiler: PromptCompiler | None = None,
    ):
        self.provider = provider
        self.validator = validator or RequirementValidator()
        self.prompt_compiler = prompt_compiler or default_prompt_compiler()
        self.last_compiled_prompt: CompiledPrompt | None = None
        self.last_schema_repair_count = 0
        self.last_schema_errors: list[str] = []
        self.last_schema_projection_used = False

    async def understand(
        self,
        raw_request: RawRequest,
        *,
        context: list[ContextFact] | None = None,
        feasibility: LocalFeasibility | None = None,
    ) -> RequirementSpec:
        facts = context or []
        self.last_compiled_prompt = self.prompt_compiler.compile(
            PromptSelectionContext(
                agent_type="requirement_agent",
                task="understand_requirement",
            ),
            [
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "raw_request": raw_request.model_dump(mode="json"),
                            "context": [fact.model_dump(mode="json") for fact in facts],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )
        messages = list(self.last_compiled_prompt.messages)
        candidate: RequirementCandidate | None = None
        self.last_schema_repair_count = 0
        self.last_schema_errors = []
        self.last_schema_projection_used = False
        for attempt in range(3):
            response = await self.provider.chat(
                ChatRequest(
                    messages=messages,
                    temperature=0,
                    response_schema=RequirementCandidate.model_json_schema(),
                    idempotency_key=(
                        self.last_compiled_prompt.prompt_hash
                        if attempt == 0
                        else f"{self.last_compiled_prompt.prompt_hash}:schema-repair:{attempt}"
                    ),
                )
            )
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.content.strip())
            try:
                candidate = RequirementCandidate.model_validate_json(content)
                break
            except ValidationError as error:
                self.last_schema_repair_count += 1
                error_text = str(error)[:4_000]
                self.last_schema_errors.append(error_text)
                if attempt == 2:
                    projected = self._project_candidate(content)
                    if projected is not None:
                        candidate = projected
                        self.last_schema_projection_used = True
                        break
                    raise
                messages.extend(
                    [
                        ChatMessage(role="assistant", content=response.content),
                        ChatMessage(
                            role="user",
                            content=(
                                "The previous JSON failed strict schema validation. Correct the "
                                "same requirement extraction without adding new facts. Return only "
                                "one JSON object matching the supplied schema; remove unknown "
                                "fields. "
                                f"Validation errors:\n{error_text}"
                            ),
                        ),
                    ]
                )
        assert candidate is not None
        presentation = enrich_requirement_presentation(
            candidate.presentation,
            raw_request.text,
        ).presentation
        spec = RequirementSpec.model_validate(
            {
                "raw_request": raw_request.model_dump(),
                **candidate.model_dump(exclude_none=True),
                "presentation": presentation.model_dump(mode="json"),
            }
        )
        return self.validator.evaluate(spec, context=facts, feasibility=feasibility)

    @staticmethod
    def _project_candidate(content: str) -> RequirementCandidate | None:
        """Last-resort strict projection after error-feedback rewrites were exhausted."""

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        projected = {
            name: value for name, value in raw.items() if name in RequirementCandidate.model_fields
        }
        formats = projected.get("output_formats")
        if isinstance(formats, list):
            aliases = {
                "markdown": "md",
                "md": "md",
                "markdown_bundle": "md_bundle",
                "md_bundle": "md_bundle",
                "markdown bundle": "md_bundle",
                "markdown zip": "md_bundle",
                "docx": "docx",
                "word": "docx",
                "pdf": "pdf",
                "typst": "typst",
                "latex": "latex",
                "tex": "latex",
            }
            projected["output_formats"] = [
                aliases.get(str(value).strip().casefold(), str(value).strip().casefold())
                for value in formats
            ]
        for field in ("document_type", "primary_language", "translation_required"):
            if isinstance(projected.get(field), str):
                projected[field] = str(projected[field]).strip().casefold()
        validated: dict[str, object] = {}
        for name, value in projected.items():
            annotation = RequirementCandidate.model_fields[name].annotation
            try:
                validated[name] = TypeAdapter(annotation).validate_python(value)
            except ValidationError:
                continue
        try:
            return RequirementCandidate.model_validate(validated)
        except ValidationError:
            return None


class RequirementVersionService:
    @staticmethod
    def revise(
        history: RequirementVersionHistory, decisions: list[FieldDecision]
    ) -> tuple[RequirementVersionHistory, RequirementChange]:
        current = history.versions[-1]
        payload = current.model_dump(mode="json")
        payload.update(
            requirement_version=current.requirement_version + 1,
            status=RequirementStatus.DRAFT,
            confirmed_requirement=None,
            confirmed_at=None,
            open_questions=[],
            conflicts=[],
        )
        changes: list[RequirementFieldDiff] = []
        for decision in decisions:
            before = read_path(current, decision.field_path)
            after = before
            if decision.decision is FieldDecisionKind.REJECT:
                after = None
            elif decision.decision is FieldDecisionKind.MODIFY:
                if decision.value is None:
                    raise ValueError("modify requires value")
                after = decision.value
            set_path(payload, decision.field_path, after)
            if before != after:
                changes.append(
                    RequirementFieldDiff(field_path=decision.field_path, before=before, after=after)
                )
        old = current.model_dump(mode="json")
        old.update(
            status=RequirementStatus.SUPERSEDED, confirmed_requirement=None, confirmed_at=None
        )
        previous = RequirementSpec.model_validate(old)
        revised = RequirementSpec.model_validate(payload)
        revised_history = RequirementVersionHistory(
            requirement_id=history.requirement_id,
            versions=[*history.versions[:-1], previous, revised],
        )
        invalidated: set[str] = set()
        for change in changes:
            invalidated.update(IMPACT.get(change.field_path.split(".")[0], set()))
        return revised_history, RequirementChange(
            previous_version=current.requirement_version,
            current_version=revised.requirement_version,
            changes=changes,
            invalidated_nodes=invalidated,
        )

    @staticmethod
    def confirm(history: RequirementVersionHistory) -> RequirementVersionHistory:
        current = history.versions[-1].model_copy(update={"open_questions": [], "conflicts": []})
        return RequirementVersionHistory(
            requirement_id=history.requirement_id,
            versions=[*history.versions[:-1], current.confirm()],
        )


def plan_preview(spec: RequirementSpec) -> list[PlannedCapability]:
    plan = [PlannedCapability(node="outline", reason="根据已确认文体和目标设计框架")]
    optional = (
        (spec.requires_literature_search, "evidence", "需求包含文献与证据", False),
        (spec.requires_experiment, "experiment", "需求包含实验或复现", True),
        (spec.requires_data_chart, "data_chart", "需求包含真实数据图", False),
        (spec.requires_generated_image, "image", "需求包含非数据生成图", True),
    )
    for enabled, node, reason, approval in optional:
        if enabled:
            plan.append(PlannedCapability(node=node, reason=reason, approval_required=approval))
    plan.extend(
        [
            PlannedCapability(node="writing", reason="按章节生成可追溯正文"),
            PlannedCapability(node="review", reason=f"执行 {spec.review_level.value} 级审验"),
            PlannedCapability(node="render", reason="生成用户要求的交付格式"),
        ]
    )
    return plan
