from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
    migrate_document_ir,
)
from paperagent.rendering.numbering import NumberingInspector
from paperagent.schemas.numbering import (
    NumberingContract,
    NumberingFormat,
    NumberingNormalizer,
    NumberingOwner,
    NumberingOwnerResolver,
    NumberingScheme,
)


def _document(*, title: str = "实验目的", level: int = 1) -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="编号治理",
        language="zh",
        sections=[
            DocumentSection(
                title=title,
                goal=title,
                level=level,
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="正文 2026 年实验结果保持不变。",
                        provenance=Provenance(agent="test"),
                    )
                ],
            )
        ],
    )


def test_numbering_scheme_enforces_one_active_owner_and_consistent_format() -> None:
    with pytest.raises(ValidationError):
        NumberingScheme.model_validate(
            {"owner": ["template", "renderer"], "format": "decimal"}
        )
    with pytest.raises(ValidationError):
        NumberingScheme(owner=NumberingOwner.NONE, format=NumberingFormat.DECIMAL)
    with pytest.raises(ValidationError):
        NumberingContract.model_validate({"unknown": True})


def test_fixed_point_normalizer_is_reversible_and_structure_scoped() -> None:
    normalizer = NumberingNormalizer()
    result = normalizer.dry_run("一、1.1 实验原理", node_kind="heading")
    assert result.semantic == "实验原理"
    assert result.changed is True
    assert [item.family for item in result.prefixes] == ["chinese", "arabic-decimal"]
    assert "".join(item.raw for item in result.prefixes) + result.semantic == result.original
    assert result.prefixes[0].span_start == 0
    assert result.prefixes[-1].span_end <= len(result.original)

    paragraph = normalizer.normalize("1.1 正文中的值", node_kind="paragraph")
    assert paragraph.semantic == "1.1 正文中的值"
    assert paragraph.protected_reason == "non-structural-node"


def test_normalizer_handles_caption_labels_and_protects_numeric_terms() -> None:
    normalizer = NumberingNormalizer()
    caption = normalizer.normalize("图 2.1\uff1a驻波分布", node_kind="caption_label")
    assert caption.semantic == "驻波分布"
    protected = normalizer.normalize("2.4 GHz 信号分析", node_kind="heading")
    assert protected.semantic == "2.4 GHz 信号分析"
    assert protected.protected_reason == "unit"


def test_document_ir_normalizes_labels_and_records_provenance() -> None:
    document = _document(title="第一章 1.1 实验原理")
    section = document.sections[0]
    assert section.title == "实验原理"
    assert section.goal == "实验原理"
    label = next(
        item
        for item in document.numbering.label_map
        if item.node_id == str(section.section_id)
    )
    assert label.original == "第一章 1.1 实验原理"
    assert label.semantic == "实验原理"
    assert [item.family for item in label.prefixes] == ["chinese-chapter", "arabic-decimal"]
    assert len(document.numbering.normalized_label_hash) == 64


def test_numbering_only_revision_changes_only_numbering_hash() -> None:
    document = _document()
    before = document.hashes()
    contract = document.numbering.model_copy(
        update={
            "headings": NumberingScheme(
                owner=NumberingOwner.NONE,
                format=NumberingFormat.NONE,
                pattern="",
                source="user-explicit",
            )
        }
    )
    changed = document.renumber(contract)
    after = changed.hashes()
    assert changed.revision == document.revision + 1
    assert after.numbering_hash != before.numbering_hash
    assert after.content_hash == before.content_hash
    assert after.structure_hash == before.structure_hash
    assert after.style_hash == before.style_hash
    assert after.asset_set_hash == before.asset_set_hash
    assert after.citation_set_hash == before.citation_set_hash
    assert after.presentation_hash == before.presentation_hash


def test_21_migration_adds_numbering_contract_idempotently() -> None:
    payload = _document().model_dump(mode="json")
    payload["schema_version"] = "2.1"
    payload.pop("numbering")
    migrated = migrate_document_ir(payload)
    repeated = migrate_document_ir(migrated.canonical_payload())
    assert migrated.schema_version == "2.2"
    assert repeated == migrated
    assert repeated.hashes() == migrated.hashes()


def test_owner_resolver_uses_explicit_template_renderer_precedence() -> None:
    resolver = NumberingOwnerResolver()
    resolved = resolver.resolve(template_has_heading_numbering=True)
    assert resolved.headings.owner is NumberingOwner.TEMPLATE
    fallback = resolver.resolve(preference="template", template_has_heading_numbering=False)
    assert fallback.headings.owner is NumberingOwner.RENDERER
    assert fallback.diagnostics[0].code == "NUMBERING_TEMPLATE_UNAVAILABLE"
    disabled = resolver.resolve(preference="none")
    assert disabled.headings.owner is NumberingOwner.NONE


def test_numbering_inspector_classifies_duplicate_owner_and_level_failures() -> None:
    document = _document(level=1)
    late_section = DocumentSection(title="讨论", goal="讨论", level=3)
    document = document.model_copy(
        deep=True,
        update={
            "sections": [
                document.sections[0].model_copy(update={"title": "1. 实验目的"}),
                late_section,
            ],
            "metadata": {
                "template_heading_numbering_active": True,
                "renderer_heading_numbering_active": True,
            },
        },
    )
    report = NumberingInspector().inspect(document)
    codes = {item.code for item in report.diagnostics}
    assert "NUMBERING_OWNER_CONFLICT" in codes
    assert "DUPLICATE_HEADING_NUMBER_RISK" in codes
    assert "NUMBERING_LEVEL_JUMP" in codes
    assert report.passed is False
