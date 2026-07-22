from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from paperagent.agents.document_ir import (
    CURRENT_DOCUMENT_IR_SCHEMA,
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
    diff_documents,
    migrate_document_ir,
)
from paperagent.execution.document_pipeline import DocumentPipelineTools, document_pipeline_specs
from paperagent.presentation import (
    apply_presentation_patch,
    expectation_from_presentation,
    presentation_from_requirement,
)
from paperagent.rendering.preflight import RenderPreflight
from paperagent.schemas.presentation import (
    PageChromeToken,
    PageChromeTokenKind,
    PresentationExpectationManifest,
    PresentationPatchKind,
    PresentationPatchOperation,
    PresentationSource,
    RequirementCoverField,
    RequirementCoverSpec,
    RequirementPageChromeSpec,
    RequirementPresentationSpec,
)


def _requirement_presentation() -> RequirementPresentationSpec:
    return RequirementPresentationSpec(
        cover=RequirementCoverSpec(
            enabled=True,
            fields=[
                RequirementCoverField(
                    semantic_key="author",
                    label="姓名",
                    value="张三",
                    order=10,
                    source=PresentationSource.USER,
                    source_ref="$raw",
                ),
                RequirementCoverField(
                    semantic_key="institution",
                    label="学校",
                    value="某某大学",
                    order=20,
                    source=PresentationSource.USER,
                    source_ref="$raw",
                ),
            ],
        ),
        page_chrome=RequirementPageChromeSpec(
            header_center="大学物理实验报告",
            page_number=True,
            total_pages=True,
            hide_on_cover=True,
        ),
    )


def _document() -> DocumentIR:
    document_id = uuid4()
    return DocumentIR(
        document_id=document_id,
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="驻波实验报告",
        language="zh",
        presentation=presentation_from_requirement(
            _requirement_presentation(),
            document_id=document_id,
        ),
        sections=[
            DocumentSection(
                title="正文",
                goal="test",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="验证驻波特性。",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )


def test_canonical_presentation_has_stable_field_ids_and_strict_page_tokens() -> None:
    document_id = uuid4()
    first = presentation_from_requirement(_requirement_presentation(), document_id=document_id)
    second = presentation_from_requirement(_requirement_presentation(), document_id=document_id)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert [item.semantic_key for item in first.cover.fields] == ["author", "institution"]
    footer = first.page_chrome.default.footer.center
    assert [item.kind for item in footer] == [
        PageChromeTokenKind.TEXT,
        PageChromeTokenKind.PAGE_NUMBER,
        PageChromeTokenKind.TEXT,
        PageChromeTokenKind.TOTAL_PAGES,
        PageChromeTokenKind.TEXT,
    ]


def test_presentation_patch_changes_only_presentation_hash() -> None:
    before = _document()
    after = apply_presentation_patch(
        before,
        [
            PresentationPatchOperation(
                kind=PresentationPatchKind.UPSERT_COVER_FIELD,
                semantic_key="institution",
                label="学校",
                value="更新后的大学",
            ),
            PresentationPatchOperation(
                kind=PresentationPatchKind.REMOVE_COVER_FIELD,
                semantic_key="author",
            ),
            PresentationPatchOperation(
                kind=PresentationPatchKind.SET_HEADER_REGION,
                region="center",
                tokens=[
                    PageChromeToken(
                        kind=PageChromeTokenKind.TEXT,
                        value="更新后的页眉",
                    )
                ],
            ),
        ],
    )
    assert isinstance(after, DocumentIR)
    changes = diff_documents(before, after)
    assert changes.presentation_changed
    assert not changes.content_changed
    assert not changes.structure_changed
    assert not changes.style_changed
    assert not changes.assets_changed
    assert not changes.citations_changed
    assert after.revision == before.revision + 1


def test_document_ir_20_migration_is_idempotent_and_preserves_legacy_values() -> None:
    legacy = _document().model_dump(mode="json")
    legacy["schema_version"] = "2.0"
    legacy.pop("presentation")
    legacy["front_matter"] = {
        "subtitle": "课程实验",
        "authors": ["张三"],
        "organization": "某某大学",
        "date": "2026-07-20",
        "custom": {"班级": "物理一班", "指导老师": "李老师"},
    }
    legacy["metadata"] = {"header_text": "大学物理实验报告", "footer_text": "内部文档"}
    migrated = migrate_document_ir(deepcopy(legacy))
    repeated = migrate_document_ir(migrated.canonical_payload())
    assert migrated.schema_version == CURRENT_DOCUMENT_IR_SCHEMA == "2.2"
    assert migrated.model_dump(mode="json") == repeated.model_dump(mode="json")
    values = {item.semantic_key: item.value for item in migrated.presentation.cover.fields}
    assert values == {
        "author": "张三",
        "institution": "某某大学",
        "date": "2026-07-20",
        "class_name": "物理一班",
        "advisor": "李老师",
    }
    assert migrated.presentation.cover.subtitle == "课程实验"


def test_pipeline_resolve_compose_and_atomic_presentation_patch(tmp_path) -> None:
    tools = DocumentPipelineTools(tmp_path)
    resolved = tools.presentation_resolve(
        {
            "latest": _requirement_presentation().model_dump(mode="json"),
        }
    )
    assert isinstance(resolved, dict)
    composed = tools.compose(
        {
            "document_id": resolved["document_id"],
            "title": "驻波实验报告",
            "content": "# 驻波实验报告\n\n## 目的\n\n验证驻波特性。",
            "language": "zh",
            "presentation": resolved["presentation"],
        }
    )
    assert isinstance(composed, dict)
    assert composed["hashes"]["presentation_hash"]
    patched = tools.presentation_patch(
        {
            "document_id": resolved["document_id"],
            "operations": [
                {
                    "kind": "upsert_cover_field",
                    "semantic_key": "class_name",
                    "label": "班级",
                    "value": "物理一班",
                }
            ],
        }
    )
    assert isinstance(patched, dict)
    assert patched["diff"]["presentation_changed"] is True
    assert patched["diff"]["content_changed"] is False
    assert patched["document_ir"]["revision"] == 2


def test_tool_registry_exposes_strict_presentation_tools() -> None:
    specs = {item.name: item for item in document_pipeline_specs()}
    assert specs["document.compose"].version == "2.0.0"
    assert specs["document.presentation.resolve"].side_effect.value == "none"
    assert specs["document.presentation.patch"].side_effect.value == "local_write"


def test_presentation_expectation_preflight_is_fail_closed_by_format() -> None:
    document = _document()
    expectation = expectation_from_presentation(document.presentation)
    assert expectation.expectation_hash == expectation.model_copy().expectation_hash

    docx = RenderPreflight().validate(
        document,
        format_name="docx",
        presentation_expectation=expectation,
    )
    assert docx.passed

    markdown = RenderPreflight().validate(
        document,
        format_name="md",
        presentation_expectation=expectation,
    )
    assert not markdown.passed
    assert any("portable Markdown" in item.message for item in markdown.issues)

    accepted_degradation = expectation.model_copy(update={"allow_format_degradation": True})
    assert (
        RenderPreflight()
        .validate(
            document,
            format_name="md",
            presentation_expectation=accepted_degradation,
        )
        .passed
    )


def test_presentation_expectation_rejects_missing_required_cover_field() -> None:
    document = _document()
    expectation = PresentationExpectationManifest(
        required_cover_keys=["author", "custom.laboratory"],
    )
    result = RenderPreflight().validate(
        document,
        format_name="docx",
        presentation_expectation=expectation,
    )
    assert not result.passed
    assert any("custom.laboratory" in item.message for item in result.issues)
