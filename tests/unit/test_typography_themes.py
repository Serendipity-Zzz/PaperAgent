from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
)
from paperagent.rendering import DocxRenderer
from paperagent.rendering.fonts import FontResolver
from paperagent.rendering.layout import (
    BUILTIN_TYPOGRAPHY_THEMES,
    ArchetypeId,
    CascadeLayer,
    FontPlanService,
    FontRequest,
    FontRole,
    NamedStyle,
    StyleCascade,
    StyleOverride,
    ThemeResolver,
    archetype_layout_profile,
)

EXPECTED_THEME_IDS = {
    "academic-classic-zh",
    "academic-classic-en",
    "experiment-lab-zh",
    "technical-clean",
    "business-modern",
    "formal-chinese",
    "tutorial-readable",
    "meeting-compact",
}


def test_builtin_catalog_contains_eight_versioned_renderer_neutral_themes() -> None:
    assert set(BUILTIN_TYPOGRAPHY_THEMES) == EXPECTED_THEME_IDS
    hashes = set()
    for theme in BUILTIN_TYPOGRAPHY_THEMES.values():
        assert theme.version == "1.0.0"
        assert theme.source == "paperagent-builtin"
        assert len(theme.styles.styles) == len(NamedStyle)
        assert len(theme.theme_hash) == 64
        hashes.add(theme.theme_hash)
        payload = theme.model_dump_json()
        assert "<w:" not in payload and "documentclass" not in payload
    assert len(hashes) == 8


def test_theme_resolver_is_deterministic_and_language_aware() -> None:
    resolver = ThemeResolver()
    first = resolver.resolve(ArchetypeId.ACADEMIC_PAPER, language="en")
    second = resolver.resolve(ArchetypeId.ACADEMIC_PAPER, language="en")
    assert first == second
    assert first.theme is not None
    assert first.theme.id == "academic-classic-en"

    explicit = resolver.resolve(
        ArchetypeId.ACADEMIC_PAPER,
        language="zh",
        explicit_theme="technical-clean",
    )
    assert explicit.theme is not None and explicit.theme.id == "technical-clean"
    assert explicit.source == "user"


def test_low_confidence_theme_resolution_returns_preview_candidates() -> None:
    decision = ThemeResolver().resolve(
        ArchetypeId.EXPERIMENT_REPORT,
        language="zh",
        confidence=0.5,
    )
    assert decision.theme is None
    assert decision.confirmation_required is True
    assert 2 <= len(decision.candidates) <= 3
    assert decision.candidates[0] == "experiment-lab-zh"


def test_archetype_profiles_have_distinct_style_page_and_theme_hashes() -> None:
    profiles = [
        archetype_layout_profile(item)
        for item in (
            ArchetypeId.ACADEMIC_PAPER,
            ArchetypeId.EXPERIMENT_REPORT,
            ArchetypeId.TECHNICAL_DOCUMENT,
            ArchetypeId.BUSINESS_REPORT,
            ArchetypeId.MEETING_MINUTES,
        )
    ]
    assert len({item.theme_hash for item in profiles}) == len(profiles)
    assert len({item.styles.model_dump_json() for item in profiles}) == len(profiles)
    assert len({item.page.model_dump_json() for item in profiles}) >= 4


def test_font_plan_reports_actual_family_missing_and_approval(tmp_path: Path) -> None:
    empty = tmp_path / "fonts"
    empty.mkdir()
    plan = FontPlanService(FontResolver([empty])).plan(
        [
            FontRequest(role=FontRole.EAST_ASIA, requested="Missing CJK Font"),
            FontRequest(role=FontRole.CODE, requested="Missing Code Font"),
        ]
    )
    assert plan.installation_approval_required is True
    assert plan.preview_required is True
    assert plan.accepted is False
    assert plan.actual_families[FontRole.EAST_ASIA] is None
    assert {item.code for item in plan.diagnostics} == {"FONT_MISSING"}


def test_style_cascade_tracks_sources_and_template_lock_conflicts() -> None:
    cascade = StyleCascade()
    overrides = [
        StyleOverride(
            layer=CascadeLayer.THEME,
            source="theme:technical-clean",
            style=NamedStyle.BODY_TEXT,
            properties={"font_size_pt": 10.5, "font_family": "Calibri"},
        ),
        StyleOverride(
            layer=CascadeLayer.TEMPLATE,
            source="template:uploaded",
            style=NamedStyle.BODY_TEXT,
            properties={"font_size_pt": 11},
            locked_properties={"font_size_pt"},
        ),
        StyleOverride(
            layer=CascadeLayer.PROJECT,
            source="project:preference",
            style=NamedStyle.BODY_TEXT,
            properties={"font_size_pt": 12},
        ),
        StyleOverride(
            layer=CascadeLayer.TASK,
            source="user:explicit",
            style=NamedStyle.BODY_TEXT,
            properties={"font_size_pt": 14},
        ),
    ]
    resolved = cascade.resolve(NamedStyle.BODY_TEXT, overrides)
    assert resolved.properties.font_size_pt == 14
    assert resolved.properties.font_family == "Calibri"
    assert resolved.sources["font_size_pt"] == "user:explicit"
    assert {item.code for item in resolved.diagnostics} == {
        "STYLE_PROPERTY_LOCKED",
        "TEMPLATE_LOCK_OVERRIDDEN_BY_USER",
    }


def test_native_docx_consumes_distinct_theme_tokens(tmp_path: Path) -> None:
    style_xml: dict[str, str] = {}
    expected_fonts = {
        ArchetypeId.ACADEMIC_PAPER: "SimSun",
        ArchetypeId.EXPERIMENT_REPORT: "Microsoft YaHei",
        ArchetypeId.TECHNICAL_DOCUMENT: "Calibri",
    }
    for archetype, expected_font in expected_fonts.items():
        document = DocumentIR(
            requirement_id=uuid4(),
            requirement_version=1,
            outline_id=uuid4(),
            title="Theme contract",
            language="mixed",
            metadata={"archetype": archetype.value},
            sections=[
                DocumentSection(
                    title="方法 Method",
                    goal="theme verification",
                    blocks=[
                        DocumentBlock(
                            kind=BlockKind.PARAGRAPH,
                            text="中文 English 123",
                            provenance=Provenance(agent="test"),
                        )
                    ],
                )
            ],
        )
        output = DocxRenderer().render(document, tmp_path / f"{archetype.value}.docx")
        with ZipFile(output) as archive:
            xml = archive.read("word/styles.xml").decode("utf-8")
        assert expected_font in xml
        style_xml[archetype.value] = xml
    assert len(set(style_xml.values())) == len(style_xml)
