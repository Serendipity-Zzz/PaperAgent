from __future__ import annotations

from paperagent.presentation import (
    PresentationResolver,
    enrich_requirement_presentation,
    extract_explicit_presentation,
    presentation_confirmation_summary,
)
from paperagent.schemas.presentation import (
    PresentationSource,
    RequirementCoverField,
    RequirementCoverSpec,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
    normalize_cover_key,
)


def _field(
    key: str,
    label: str,
    value: str,
    *,
    source: PresentationSource,
    slot: bool = False,
) -> RequirementCoverField:
    return RequirementCoverField(
        semantic_key=key,
        label=label,
        value=value,
        source=source,
        slot=slot,
    )


def test_explicit_chinese_fields_and_page_chrome_are_extracted_without_rewriting() -> None:
    result = extract_explicit_presentation(
        "封面写姓名\uff1a张三\uff1b学号\uff1a20260001\uff1b"
        "班级\uff1a物理一班\uff1b学校\uff1a某某大学\uff1b"
        "指导老师\uff1a李老师。页眉\uff1a大学物理实验报告\uff0c"
        "封面不显示页眉\uff0c页脚需要页码和总页数。"
    )
    assert result.cover is not None
    values = {item.semantic_key: item.value for item in result.cover.fields}
    assert values == {
        "author": "张三",
        "student_id": "20260001",
        "class_name": "物理一班",
        "institution": "某某大学",
        "advisor": "李老师",
    }
    assert result.page_chrome is not None
    assert result.page_chrome.header_center == "大学物理实验报告"
    assert result.page_chrome.hide_on_cover is True
    assert result.page_chrome.page_number is True
    assert result.page_chrome.total_pages is True


def test_natural_quoted_cover_fields_and_dynamic_footer_are_not_collapsed() -> None:
    result = extract_explicit_presentation(
        "封面标题为\u201c驻波实验报告\u201d, 封面信息为: "
        "姓名\u201c合成验收用户\u201d、学号\u201c2026072002\u201d、学校\u201c合成理工大学\u201d、"
        "班级\u201c物理实验A班\u201d、指导教师\u201c合成指导教师\u201d、课程\u201c大学物理实验\u201d。"
        "封面不显示页眉页脚; 正文页眉居中为\u201c大学物理实验报告\u201d, "
        "页脚为\u201c第 {page} 页 / 共 {pages} 页\u201d。"
    )

    assert result.cover is not None
    assert result.cover.title == "驻波实验报告"
    assert {item.semantic_key: item.value for item in result.cover.fields} == {
        "author": "合成验收用户",
        "student_id": "2026072002",
        "institution": "合成理工大学",
        "class_name": "物理实验A班",
        "advisor": "合成指导教师",
        "course": "大学物理实验",
    }
    assert result.page_chrome is not None
    assert result.page_chrome.header_center == "大学物理实验报告"
    assert result.page_chrome.footer_center is None
    assert result.page_chrome.hide_on_cover is True
    assert result.page_chrome.page_number is True
    assert result.page_chrome.total_pages is True


def test_open_cover_field_uses_stable_custom_key_in_cover_context() -> None:
    result = extract_explicit_presentation(
        "请在封面加入实验室\uff1a量子光学中心\uff1b报告版本\uff1a终稿"
    )
    assert result.cover is not None
    by_label = {item.label: item for item in result.cover.fields}
    assert by_label["实验室"].semantic_key.startswith("custom.")
    assert by_label["实验室"].value == "量子光学中心"
    assert by_label["报告版本"].value == "终稿"
    assert normalize_cover_key("实验室") == by_label["实验室"].semantic_key


def test_missing_personal_information_is_an_unresolved_ambiguity() -> None:
    result = extract_explicit_presentation("首页请放上我的个人信息")
    assert result.unresolved[0].code == "COVER_FIELDS_MISSING"
    assert result.unresolved[0].field_path == "presentation.cover.fields"


def test_resolver_precedence_slots_and_sources_are_deterministic() -> None:
    template = RequirementPresentationSpec(
        cover=RequirementCoverSpec(
            enabled=True,
            fields=[
                _field(
                    "author",
                    "姓名",
                    "",
                    source=PresentationSource.TEMPLATE,
                    slot=True,
                ),
                _field(
                    "institution",
                    "学校",
                    "模板示例大学",
                    source=PresentationSource.TEMPLATE,
                ),
            ],
        ),
        page_chrome=RequirementPageChromeSpec(header_center="模板页眉"),
    )
    latest = RequirementPresentationSpec(
        cover=RequirementCoverSpec(
            fields=[
                _field("author", "姓名", "张三", source=PresentationSource.USER),
                _field("institution", "学校", "某某大学", source=PresentationSource.USER),
            ]
        ),
        page_chrome=RequirementPageChromeSpec(
            header_center="大学物理实验报告",
            hide_on_cover=True,
        ),
    )
    resolver = PresentationResolver()
    first = resolver.resolve(template=template, latest=latest)
    second = resolver.resolve(template=template, latest=latest)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.presentation.cover is not None
    assert {item.semantic_key: item.value for item in first.presentation.cover.fields} == {
        "author": "张三",
        "institution": "某某大学",
    }
    assert first.source_map["cover.fields.author"] is PresentationSource.USER
    assert first.source_map["page_chrome.header_center"] is PresentationSource.USER


def test_fallback_enrichment_never_overwrites_model_only_fields() -> None:
    model = RequirementPresentationSpec(
        cover=RequirementCoverSpec(
            fields=[
                _field("course", "课程", "大学物理实验", source=PresentationSource.USER),
            ]
        )
    )
    resolved = enrich_requirement_presentation(model, "姓名\uff1a张三")
    assert resolved.presentation.cover is not None
    values = {item.semantic_key: item.value for item in resolved.presentation.cover.fields}
    assert values == {"course": "大学物理实验", "author": "张三"}


def test_confirmation_summary_is_complete_while_public_events_can_use_only_keys() -> None:
    value = extract_explicit_presentation(
        "封面姓名\uff1a张三\uff1b学校\uff1a某某大学\uff1b页眉\uff1a课程报告"
    )
    summary = presentation_confirmation_summary(value)
    fields = summary["cover_fields"]
    assert isinstance(fields, list)
    assert fields[0]["value"] == "张三"
    public_summary = {
        "field_count": len(fields),
        "field_keys": [item["key"] for item in fields],
    }
    assert "张三" not in str(public_summary)
