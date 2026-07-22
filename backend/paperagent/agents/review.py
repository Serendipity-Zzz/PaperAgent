from __future__ import annotations

from enum import StrEnum
from typing import ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from paperagent.agents.document_ir import BlockKind, DocumentIR
from paperagent.agents.evidence import EvidenceBundle
from paperagent.agents.outline import OutlinePlan
from paperagent.agents.state import RequirementSpec, RequirementStatus
from paperagent.agents.supervisor import SupervisorPlan
from paperagent.agents.word_count import WordCountReport, WordCountTool


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKING = "blocking"


class ReviewCategory(StrEnum):
    REQUIREMENT = "requirement"
    STRUCTURE = "structure"
    EVIDENCE = "evidence"
    CITATION = "citation"
    FACT = "fact"
    DATA = "data"
    LANGUAGE = "language"
    LENGTH = "length"
    FORMAT = "format"


class IssueStatus(StrEnum):
    OPEN = "open"
    WAIVED = "waived"
    RESOLVED = "resolved"
    REOPENED = "reopened"


class ReviewIssue(BaseModel):
    issue_id: UUID = Field(default_factory=uuid4)
    category: ReviewCategory
    severity: ReviewSeverity
    message: str
    section_id: UUID | None = None
    block_id: UUID | None = None
    rule_id: str
    status: IssueStatus = IssueStatus.OPEN


class ReviewReport(BaseModel):
    stage: str
    passed: bool
    issues: list[ReviewIssue]
    word_count: WordCountReport | None = None


class ReviewPolicy(BaseModel):
    level: str = Field(pattern=r"^(light|standard|strict)$")
    length_tolerance: float = Field(default=0.1, ge=0, le=0.5)

    def blocks(self, severity: ReviewSeverity) -> bool:
        if self.level == "light":
            return severity is ReviewSeverity.BLOCKING
        if self.level == "standard":
            return severity in {ReviewSeverity.ERROR, ReviewSeverity.BLOCKING}
        return severity in {ReviewSeverity.WARNING, ReviewSeverity.ERROR, ReviewSeverity.BLOCKING}


class ReviewAgent:
    def preflight(
        self,
        requirement: RequirementSpec,
        outline: OutlinePlan | None,
        evidence: EvidenceBundle | None,
        supervisor: SupervisorPlan | None,
        policy: ReviewPolicy,
    ) -> ReviewReport:
        issues: list[ReviewIssue] = []
        if requirement.status is not RequirementStatus.CONFIRMED:
            issues.append(
                self.issue(
                    ReviewCategory.REQUIREMENT,
                    ReviewSeverity.BLOCKING,
                    "Requirement Spec 尚未确认",
                    "pre.requirement",
                )
            )
        if outline is None:
            issues.append(
                self.issue(
                    ReviewCategory.STRUCTURE,
                    ReviewSeverity.BLOCKING,
                    "缺少已确认框架",
                    "pre.outline",
                )
            )
        if evidence is None:
            issues.append(
                self.issue(
                    ReviewCategory.EVIDENCE,
                    ReviewSeverity.ERROR,
                    "缺少 Evidence Pack",
                    "pre.evidence",
                )
            )
        elif not evidence.items and requirement.requires_literature_search:
            issues.append(
                self.issue(
                    ReviewCategory.EVIDENCE,
                    ReviewSeverity.ERROR,
                    "需求要求文献但没有可用证据",
                    "pre.empty_evidence",
                )
            )
        if supervisor:
            pending = [
                item.node for item in supervisor.assignments if item.status == "waiting_approval"
            ]
            if pending:
                issues.append(
                    self.issue(
                        ReviewCategory.REQUIREMENT,
                        ReviewSeverity.BLOCKING,
                        f"审批尚未完成: {pending}",
                        "pre.approval",
                    )
                )
        return ReviewReport(
            stage="preflight",
            passed=not any(policy.blocks(item.severity) for item in issues),
            issues=issues,
        )

    def postflight(
        self,
        document: DocumentIR,
        requirement: RequirementSpec,
        evidence: EvidenceBundle,
        policy: ReviewPolicy,
    ) -> ReviewReport:
        issues: list[ReviewIssue] = []
        evidence_ids = {item.evidence_id for item in evidence.items}
        seen: dict[str, UUID] = {}
        for section in document.sections:
            if not section.blocks:
                issues.append(
                    self.issue(
                        ReviewCategory.STRUCTURE,
                        ReviewSeverity.ERROR,
                        "章节没有正文",
                        "post.empty_section",
                        section.section_id,
                    )
                )
            for block in section.blocks:
                if block.kind is BlockKind.PARAGRAPH and not (
                    block.provenance.evidence_ids or block.provenance.author_viewpoint
                ):
                    issues.append(
                        self.issue(
                            ReviewCategory.EVIDENCE,
                            ReviewSeverity.ERROR,
                            "正文主张未绑定证据且未标为作者观点",
                            "post.unsupported",
                            section.section_id,
                            block.block_id,
                        )
                    )
                if set(block.provenance.evidence_ids) - evidence_ids:
                    issues.append(
                        self.issue(
                            ReviewCategory.CITATION,
                            ReviewSeverity.BLOCKING,
                            "正文引用了 Evidence Pack 之外的来源",
                            "post.unknown_evidence",
                            section.section_id,
                            block.block_id,
                        )
                    )
                normalized = "".join(block.text.split()).casefold()
                if normalized and normalized in seen:
                    issues.append(
                        self.issue(
                            ReviewCategory.LANGUAGE,
                            ReviewSeverity.WARNING,
                            "检测到重复内容",
                            "post.duplicate",
                            section.section_id,
                            block.block_id,
                        )
                    )
                seen[normalized] = block.block_id
        report = WordCountTool().count(document)
        target = requirement.target_length.value if requirement.target_length else 0
        count_fields = {
            "chinese_char": "chinese_chars",
            "english_word": "english_words",
            "mixed_score": "mixed_score",
        }
        actual = (
            getattr(report, count_fields[requirement.target_length.unit.value])
            if requirement.target_length
            else 0
        )
        if target and abs(actual - target) / target > policy.length_tolerance:
            issues.append(
                self.issue(
                    ReviewCategory.LENGTH,
                    ReviewSeverity.ERROR,
                    f"有效字数 {actual} 偏离目标 {target}",
                    "post.length",
                )
            )
        return ReviewReport(
            stage="postflight",
            passed=not any(policy.blocks(item.severity) for item in issues),
            issues=issues,
            word_count=report,
        )

    @staticmethod
    def issue(
        category: ReviewCategory,
        severity: ReviewSeverity,
        message: str,
        rule_id: str,
        section_id: UUID | None = None,
        block_id: UUID | None = None,
    ) -> ReviewIssue:
        return ReviewIssue(
            category=category,
            severity=severity,
            message=message,
            rule_id=rule_id,
            section_id=section_id,
            block_id=block_id,
        )


class RepairTask(BaseModel):
    issue_ids: list[UUID]
    responsible_agent: str
    section_ids: list[UUID]
    block_ids: list[UUID]
    action: str
    rerun_nodes: list[str]


class RepairPlan(BaseModel):
    repair_id: UUID = Field(default_factory=uuid4)
    tasks: list[RepairTask]
    round: int = Field(ge=1)
    max_rounds: int = 3
    human_takeover: bool = False


class RepairPlanner:
    AGENTS: ClassVar[dict[ReviewCategory, tuple[str, list[str]]]] = {
        ReviewCategory.CITATION: ("evidence_agent", ["evidence", "writing", "post_review"]),
        ReviewCategory.EVIDENCE: ("writer_agent", ["writing", "post_review"]),
        ReviewCategory.STRUCTURE: ("outline_agent", ["outline", "writing", "post_review"]),
        ReviewCategory.DATA: ("visual_agent", ["data_chart", "writing", "post_review"]),
        ReviewCategory.FORMAT: ("render_agent", ["render"]),
    }

    def plan(self, issues: list[ReviewIssue], *, round: int, max_rounds: int = 3) -> RepairPlan:
        if round > max_rounds:
            return RepairPlan(tasks=[], round=round, max_rounds=max_rounds, human_takeover=True)
        tasks: list[RepairTask] = []
        for issue in issues:
            if issue.status is not IssueStatus.OPEN:
                continue
            agent, rerun = self.AGENTS.get(
                issue.category, ("writer_agent", ["writing", "post_review"])
            )
            tasks.append(
                RepairTask(
                    issue_ids=[issue.issue_id],
                    responsible_agent=agent,
                    section_ids=[issue.section_id] if issue.section_id else [],
                    block_ids=[issue.block_id] if issue.block_id else [],
                    action=issue.message,
                    rerun_nodes=rerun,
                )
            )
        return RepairPlan(tasks=tasks, round=round, max_rounds=max_rounds)

    @staticmethod
    def waive(issue: ReviewIssue) -> ReviewIssue:
        return issue.model_copy(update={"status": IssueStatus.WAIVED})
