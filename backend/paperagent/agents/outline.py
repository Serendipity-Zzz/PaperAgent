from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from paperagent.agents.state import (
    ConfirmedRequirement,
    DocumentType,
    LengthUnit,
    RequirementSpec,
    RequirementStatus,
)


class OutlineSource(StrEnum):
    BUILTIN = "builtin"
    USER_TEMPLATE = "user_template"
    COMPLETED_SAMPLE = "completed_sample"
    USER_ADJUSTED = "user_adjusted"


class EvidenceNeed(BaseModel):
    kind: str
    purpose: str
    required: bool = True


class SectionPlan(BaseModel):
    section_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    target_length: int = Field(ge=0)
    evidence_needs: list[EvidenceNeed] = Field(default_factory=list)
    subsections: list[str] = Field(default_factory=list)


class OutlinePlan(BaseModel):
    outline_id: UUID = Field(default_factory=uuid4)
    requirement_id: UUID
    requirement_version: int = Field(ge=1)
    document_type: DocumentType
    framework_id: str
    source: OutlineSource
    selection_reason: str
    length_unit: LengthUnit
    target_length: int = Field(gt=0)
    revision: int = Field(default=1, ge=1)
    sections: list[SectionPlan] = Field(min_length=1)
    structure_only: bool = True

    @model_validator(mode="after")
    def validate_budget(self) -> OutlinePlan:
        if sum(section.target_length for section in self.sections) != self.target_length:
            raise ValueError("section budgets must equal the confirmed target length")
        return self


class TemplateSection(BaseModel):
    title: str
    goal: str | None = None
    weight: float = Field(default=1, gt=0)
    subsections: list[str] = Field(default_factory=list)


class UserOutlineTemplate(BaseModel):
    template_id: str
    sections: list[TemplateSection] = Field(min_length=1)
    completed_sample: bool = False
    sample_body: str | None = None


FRAMEWORKS: dict[DocumentType, tuple[tuple[str, str, float], ...]] = {
    DocumentType.ACADEMIC_PAPER: (
        ("摘要", "概述问题、方法、主要发现边界与贡献", 0.08),
        ("引言", "说明背景、问题、研究目标与文章结构", 0.15),
        ("相关研究", "综合比较可核验文献并定位研究空白", 0.17),
        ("研究方法", "说明研究设计、对象、数据与分析方法", 0.2),
        ("结果", "客观呈现分析或实验结果", 0.18),
        ("讨论", "解释结果、局限和适用边界", 0.15),
        ("结论", "回答研究问题并提出审慎展望", 0.07),
    ),
    DocumentType.EXPERIMENT_REPORT: (
        ("摘要", "概述实验目标、设置、结果和结论边界", 0.08),
        ("实验目标", "定义待验证问题、指标与验收标准", 0.12),
        ("环境与方法", "记录环境、数据、步骤、变量和复现条件", 0.25),
        ("实验结果", "呈现原始结果、统计与数据图", 0.25),
        ("分析与讨论", "解释误差、对照、局限和异常", 0.22),
        ("结论", "汇总可由实验支持的结论", 0.08),
    ),
    DocumentType.PROJECT_REPORT: (
        ("项目概述", "说明目标、范围、利益相关者与约束", 0.15),
        ("需求与方案", "追溯需求并比较方案选择", 0.2),
        ("实施过程", "记录设计、实现、管理和关键决策", 0.25),
        ("成果与验收", "按验收标准展示成果和证据", 0.2),
        ("风险与复盘", "分析风险、偏差、经验和改进", 0.15),
        ("结论", "总结项目价值和后续工作", 0.05),
    ),
    DocumentType.PRACTICE_REPORT: (
        ("实践背景", "说明场景、目标、角色和真实性边界", 0.15),
        ("实践方案", "说明流程、资源、方法和计划", 0.2),
        ("实践过程", "按时间或阶段记录关键活动", 0.3),
        ("成果评价", "使用记录、数据或访谈评价结果", 0.2),
        ("反思与建议", "总结局限、经验和可行建议", 0.15),
    ),
    DocumentType.SURVEY_REPORT: (
        ("摘要", "概述调查目的、样本边界、方法和主要结果", 0.08),
        ("调查背景", "定义问题、对象、范围与调查目标", 0.15),
        ("调查设计", "说明抽样、工具、变量和伦理边界", 0.2),
        ("调查结果", "客观呈现数据与统计结果", 0.3),
        ("分析与建议", "解释结果并提出证据支持的建议", 0.2),
        ("结论与局限", "总结发现和调查限制", 0.07),
    ),
    DocumentType.OTHER: (
        ("背景与目标", "说明写作背景、核心目标和范围", 0.2),
        ("主体分析", "按用户目标组织材料和论证", 0.55),
        ("结论", "总结主要内容、边界和后续事项", 0.25),
    ),
}


def allocate_budget(total: int, weights: list[float]) -> list[int]:
    denominator = sum(weights)
    budgets = [int(total * weight / denominator) for weight in weights]
    for index in range(total - sum(budgets)):
        budgets[index % len(budgets)] += 1
    return budgets


class OutlineDesignerAgent:
    def design(
        self, requirement: RequirementSpec, template: UserOutlineTemplate | None = None
    ) -> OutlinePlan:
        if requirement.status is not RequirementStatus.CONFIRMED:
            raise PermissionError("outline design requires a confirmed Requirement Spec")
        confirmed = requirement.confirmed_requirement
        assert confirmed is not None
        if template:
            definitions = [
                (
                    section.title,
                    section.goal or f"完成模板要求的“{section.title}”内容",
                    section.weight,
                    section.subsections,
                )
                for section in template.sections
            ]
            source = (
                OutlineSource.COMPLETED_SAMPLE
                if template.completed_sample
                else OutlineSource.USER_TEMPLATE
            )
            framework_id = template.template_id
            reason = "优先采用用户上传模板的章节结构; 已完成样例仅提取结构, 不复制正文。"
        else:
            definitions = [(*item, []) for item in FRAMEWORKS[confirmed.document_type]]
            source = OutlineSource.BUILTIN
            framework_id = f"builtin-{confirmed.document_type.value}-v1"
            reason = self._reason(confirmed)
        budgets = allocate_budget(
            confirmed.target_length.value, [definition[2] for definition in definitions]
        )
        sections = [
            SectionPlan(
                title=title,
                goal=goal,
                target_length=budgets[index],
                evidence_needs=self._evidence(title, confirmed),
                subsections=subsections,
            )
            for index, (title, goal, _weight, subsections) in enumerate(definitions)
        ]
        return OutlinePlan(
            requirement_id=confirmed.requirement_id,
            requirement_version=confirmed.requirement_version,
            document_type=confirmed.document_type,
            framework_id=framework_id,
            source=source,
            selection_reason=reason,
            length_unit=confirmed.target_length.unit,
            target_length=confirmed.target_length.value,
            sections=sections,
        )

    @staticmethod
    def adjust(plan: OutlinePlan, sections: list[TemplateSection], reason: str) -> OutlinePlan:
        budgets = allocate_budget(plan.target_length, [section.weight for section in sections])
        return OutlinePlan(
            requirement_id=plan.requirement_id,
            requirement_version=plan.requirement_version,
            document_type=plan.document_type,
            framework_id=f"{plan.framework_id}-adjusted",
            source=OutlineSource.USER_ADJUSTED,
            selection_reason=reason,
            length_unit=plan.length_unit,
            target_length=plan.target_length,
            revision=plan.revision + 1,
            sections=[
                SectionPlan(
                    title=section.title,
                    goal=section.goal or f"完成“{section.title}”的章节目标",
                    target_length=budgets[index],
                    subsections=section.subsections,
                )
                for index, section in enumerate(sections)
            ],
        )

    @staticmethod
    def _reason(requirement: ConfirmedRequirement) -> str:
        return (
            f"需求已确认为 {requirement.document_type.value}, 因此选择与该文体的证据链、"
            f"方法和审验目标匹配的内置框架, 并按 {requirement.target_length.value} "
            f"{requirement.target_length.unit.value} 分配章节预算。"
        )

    @staticmethod
    def _evidence(title: str, requirement: ConfirmedRequirement) -> list[EvidenceNeed]:
        needs: list[EvidenceNeed] = []
        if requirement.requires_literature_search and any(
            keyword in title for keyword in ("引言", "背景", "相关研究", "分析", "讨论")
        ):
            needs.append(EvidenceNeed(kind="literature", purpose="支持背景、比较或解释性主张"))
        if requirement.requires_experiment and any(
            keyword in title for keyword in ("方法", "环境", "结果", "分析", "讨论")
        ):
            needs.append(EvidenceNeed(kind="experiment", purpose="绑定实验配置、日志或结果"))
        if requirement.requires_data_chart and "结果" in title:
            needs.append(EvidenceNeed(kind="data_chart", purpose="绑定真实数据及图表来源"))
        return needs
