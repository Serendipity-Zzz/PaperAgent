from uuid import uuid4

import pytest

from paperagent.agents.change_intent import ChangeIntentAgent
from paperagent.agents.document_ir import DocumentIR, DocumentSection
from paperagent.schemas.typography import TypographySpec, extract_typography


@pytest.mark.anyio
async def test_typography_only_change_preserves_text_and_targets_rendering() -> None:
    document = DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="Title",
        language="en",
        sections=[DocumentSection(title="Body", goal="Explain", blocks=[])],
        typography=TypographySpec(body_font="Times New Roman"),
    )
    intent = await ChangeIntentAgent().understand(
        "body: SimSun, body font size: 12pt, line spacing: 1.5"
    )
    updated, impact = ChangeIntentAgent.apply(document, intent)
    assert updated.typography.body_font == "SimSun"
    assert updated.typography.body_size_pt == 12
    assert updated.typography.line_spacing == 1.5
    assert updated.title == document.title and updated.sections == document.sections
    assert not impact.content_regeneration_required
    assert "pdf" in impact.rerender_formats


@pytest.mark.anyio
async def test_unknown_font_request_requires_clarification() -> None:
    intent = await ChangeIntentAgent().understand("make it look more premium")
    assert intent.clarification


def test_global_chinese_font_and_named_size_are_extracted() -> None:
    typography, matched = extract_typography("字体改为宋体, 正文小四")
    assert typography.body_font == "宋体"
    assert typography.body_size_pt == 12
    assert {"body_font", "body_size_pt"} <= matched
