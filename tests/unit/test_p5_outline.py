import pytest

from paperagent.agents.outline import (
    OutlineDesignerAgent,
    OutlineSource,
    TemplateSection,
    UserOutlineTemplate,
)
from paperagent.agents.state import RawRequest, RequirementSpec


def confirmed_requirement(document_type: str) -> RequirementSpec:
    return RequirementSpec.model_validate(
        {
            "raw_request": RawRequest(text=f"明确的 {document_type} 需求"),
            "normalized_request": "撰写可追溯的研究文档",
            "research_formulation": {"research_topic": "本地论文智能体"},
            "document_type": document_type,
            "primary_language": "zh",
            "target_length": {"value": 5003, "unit": "chinese_char"},
            "audience": "计算机专业学生",
            "citation_style": "GB/T 7714",
            "requires_literature_search": True,
            "requires_experiment": document_type == "experiment_report",
            "requires_data_chart": document_type in {"experiment_report", "survey_report"},
            "requires_generated_image": document_type == "practice_report",
            "output_formats": ["docx", "pdf", "md"],
        }
    ).confirm()


@pytest.mark.parametrize(
    "document_type",
    [
        "academic_paper",
        "experiment_report",
        "project_report",
        "practice_report",
        "survey_report",
        "other",
    ],
)
def test_framework_selection_goals_evidence_and_exact_budget(document_type: str) -> None:
    plan = OutlineDesignerAgent().design(confirmed_requirement(document_type))
    assert plan.framework_id == f"builtin-{document_type}-v1"
    assert document_type in plan.selection_reason
    assert sum(section.target_length for section in plan.sections) == 5003
    assert all(section.goal for section in plan.sections)
    if document_type == "experiment_report":
        assert any(
            need.kind == "experiment"
            for section in plan.sections
            for need in section.evidence_needs
        )


def test_unconfirmed_requirement_is_never_accepted() -> None:
    draft = RequirementSpec(raw_request=RawRequest(text="模糊需求"))
    with pytest.raises(PermissionError, match="confirmed"):
        OutlineDesignerAgent().design(draft)


def test_user_template_overrides_builtin_and_completed_body_is_not_copied() -> None:
    secret_body = "这是旧论文正文, 绝不能复制到新论文"
    template = UserOutlineTemplate(
        template_id="uploaded-template-v1",
        completed_sample=True,
        sample_body=secret_body,
        sections=[
            TemplateSection(title="学校指定章节", goal="满足学校模板要求", weight=2),
            TemplateSection(title="结语", weight=1),
        ],
    )
    plan = OutlineDesignerAgent().design(confirmed_requirement("academic_paper"), template)
    assert plan.source is OutlineSource.COMPLETED_SAMPLE
    assert [section.title for section in plan.sections] == ["学校指定章节", "结语"]
    assert secret_body not in plan.model_dump_json()
    assert plan.structure_only is True


def test_user_can_adjust_outline_without_losing_requirement_binding() -> None:
    original = OutlineDesignerAgent().design(confirmed_requirement("project_report"))
    adjusted = OutlineDesignerAgent.adjust(
        original,
        [
            TemplateSection(title="自定义主体", weight=3),
            TemplateSection(title="自定义结论", weight=1),
        ],
        "用户要求合并实施和成果章节",
    )
    assert adjusted.source is OutlineSource.USER_ADJUSTED
    assert adjusted.revision == original.revision + 1
    assert adjusted.requirement_id == original.requirement_id
    assert sum(section.target_length for section in adjusted.sections) == original.target_length
