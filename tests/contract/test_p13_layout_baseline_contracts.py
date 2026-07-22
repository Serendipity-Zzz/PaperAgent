from __future__ import annotations

import json
from pathlib import Path

from paperagent.rendering.layout import ArchetypeId, archetype_layout_profile

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "document_typography"


def _style_payload(archetype: ArchetypeId) -> dict[str, object]:
    profile = archetype_layout_profile(archetype)
    return {
        key.value: value.model_dump(mode="json")
        for key, value in profile.styles.styles.items()
    }


def test_academic_experiment_and_technical_styles_are_distinct() -> None:
    academic = _style_payload(ArchetypeId.ACADEMIC_PAPER)
    experiment = _style_payload(ArchetypeId.EXPERIMENT_REPORT)
    technical = _style_payload(ArchetypeId.TECHNICAL_DOCUMENT)

    payloads = {
        json.dumps(item, sort_keys=True) for item in (academic, experiment, technical)
    }
    assert len(payloads) == 3


def test_archetype_themes_have_distinct_style_payloads() -> None:
    payloads = {
        archetype: json.dumps(_style_payload(archetype), sort_keys=True)
        for archetype in (
            ArchetypeId.ACADEMIC_PAPER,
            ArchetypeId.EXPERIMENT_REPORT,
            ArchetypeId.TECHNICAL_DOCUMENT,
            ArchetypeId.BUSINESS_REPORT,
            ArchetypeId.MEETING_MINUTES,
        )
    }
    assert len(set(payloads.values())) == len(payloads)


def test_p13_contract_snapshot_defines_models_tools_and_public_event_boundary() -> None:
    snapshot = json.loads(
        (FIXTURE_ROOT / "p13-contract-snapshot.json").read_text(encoding="utf-8")
    )

    assert set(snapshot["models"]) == {
        "NumberingContract",
        "TypographyTheme",
        "TemplateContractV2",
        "LayoutDecision",
        "VisualQaReport",
        "RepairDecision",
    }
    assert snapshot["tools"]["document.render"] == "2.2.0"
    assert snapshot["tools"]["document.template.inspect"] == "2.0.0"
    assert set(snapshot["forbidden_public_fields"]).isdisjoint(
        snapshot["public_event_allowlist"]
    )
    assert {
        "diagnostic_code",
        "revision",
        "hash",
        "skill_id",
        "skill_version",
        "theme_id",
    } <= set(snapshot["public_event_allowlist"])
