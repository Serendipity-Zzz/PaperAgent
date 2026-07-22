from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from paperagent.agents.document_ir import DocumentIR, DocumentSection, FrontMatter
from paperagent.execution.document_pipeline import document_pipeline_specs
from paperagent.rendering.capabilities import (
    Fidelity,
    OutputFormat,
    SemanticElement,
    capability_for,
)
from paperagent.rendering.latex_native import NativeLatexRenderer
from paperagent.services.progress import public_payload

FIXTURES = Path(__file__).parents[1] / "fixtures" / "document_presentation"


def _minimal_document(**updates: object) -> DocumentIR:
    values: dict[str, object] = {
        "requirement_id": uuid4(),
        "requirement_version": 1,
        "outline_id": uuid4(),
        "title": "驻波验收报告",
        "language": "zh",
        "sections": [DocumentSection(title="正文", goal="characterization")],
    }
    values.update(updates)
    return DocumentIR.model_validate(values)


def test_presentation_fixture_uses_only_synthetic_open_fields() -> None:
    payload = json.loads((FIXTURES / "presentation-contract.json").read_text(encoding="utf-8"))
    fields = payload["cover"]["fields"]
    assert [item["semantic_key"] for item in fields][-1] == "custom.laboratory"
    assert {item["label"] for item in fields} >= {"姓名", "学号", "班级", "学校", "指导老师"}
    assert all(item["value"] for item in fields)


def test_compose_v2_contract_accepts_structured_presentation() -> None:
    compose = next(item for item in document_pipeline_specs() if item.name == "document.compose")
    assert compose.version == "2.0.0"
    assert set(compose.input_schema["properties"]) == {
        "title",
        "document_id",
        "content",
        "language",
        "conversation_id",
        "image_required",
        "presentation",
    }
    assert compose.input_schema["properties"]["presentation"] == {"type": "object"}


def test_legacy_front_matter_has_open_custom_data_but_latex_does_not_render_it() -> None:
    document = _minimal_document(
        front_matter=FrontMatter(
            authors=["张三"],
            organization="某某大学",
            date="2026-07-20",
            custom={"class_name": "物理一班", "advisor": "李老师"},
        )
    )
    source = NativeLatexRenderer(None, lambda *_: None).source(document)  # type: ignore[arg-type]
    assert "张三" in source
    assert "物理一班" not in source
    assert "李老师" not in source
    assert r"\date{}" in source


def test_page_only_features_are_not_claimed_for_portable_markdown() -> None:
    for element in (
        SemanticElement.HEADER,
        SemanticElement.FOOTER,
        SemanticElement.PAGE_NUMBER,
    ):
        capability = capability_for(element, OutputFormat.MARKDOWN)
        assert capability.fidelity is Fidelity.UNSUPPORTED
        assert capability.limitation


def test_public_presentation_event_contract_contains_no_field_values() -> None:
    raw = {
        "field_count": 5,
        "field_keys": ["author", "student_id", "class_name", "institution", "advisor"],
        "presentation_hash": "sha256:synthetic",
        "revision": 2,
    }
    safe = public_payload(raw)
    serialized = json.dumps(safe, ensure_ascii=False)
    assert "张三" not in serialized
    assert "20260001" not in serialized
    assert safe == raw
