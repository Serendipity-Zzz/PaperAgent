import asyncio
import json
from uuid import uuid4

import pytest

from paperagent.agents.document_ir import BlockKind, DocumentBlock, Provenance
from paperagent.agents.evidence import EvidenceBundle, EvidenceItem
from paperagent.agents.outline import OutlinePlan, SectionPlan
from paperagent.agents.review import (
    RepairPlanner,
    ReviewAgent,
    ReviewCategory,
    ReviewPolicy,
)
from paperagent.agents.state import RawRequest, RequirementSpec
from paperagent.agents.writer import DraftBlock, SectionDraft, SectionWriterAgent
from paperagent.providers import Capability, ChatRequest, ChatResponse, ProviderConfig
from paperagent.providers.mock import MockProvider


class SchemaRepairProvider(MockProvider):
    def __init__(self, responses: list[str]) -> None:
        super().__init__(
            ProviderConfig(
                id="writer-mock",
                provider_type="mock",
                base_url="http://127.0.0.1:9999/v1",
                model="writer-mock",
                capabilities={Capability.CHAT, Capability.STRUCTURED_OUTPUT},
            ),
            content=responses[0],
        )
        self.responses = responses
        self.request_log: list[ChatRequest] = []

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.request_log.append(request)
        self.content = self.responses[min(self.calls, len(self.responses) - 1)]
        return await super().chat(request)


def requirement() -> RequirementSpec:
    return RequirementSpec.model_validate(
        {
            "raw_request": RawRequest(text="写 8 个英文词"),
            "normalized_request": "Write eight words",
            "research_formulation": {"research_topic": "local agents"},
            "document_type": "academic_paper",
            "primary_language": "en",
            "target_length": {"value": 8, "unit": "english_word"},
            "audience": "researchers",
            "citation_style": "APA",
            "requires_literature_search": True,
            "requires_experiment": False,
            "requires_data_chart": False,
            "requires_generated_image": False,
            "output_formats": ["md"],
        }
    ).confirm()


def outline() -> OutlinePlan:
    spec = requirement().confirmed_requirement
    assert spec is not None
    return OutlinePlan(
        requirement_id=spec.requirement_id,
        requirement_version=1,
        document_type="academic_paper",
        framework_id="test",
        source="builtin",
        selection_reason="test",
        length_unit="english_word",
        target_length=8,
        sections=[SectionPlan(title="Result", goal="state result", target_length=8)],
    )


def evidence() -> EvidenceBundle:
    item = EvidenceItem(
        kind="literature",
        title="Verified",
        content="Local agents retain state",
        source_uri="https://example.test/paper",
        source_id="10.1/test",
        verification="verified",
        scholarly_citation=True,
        reason="verified",
    )
    return EvidenceBundle(
        queries=[], items=[item], claim_map=[], reference_evidence_ids=[item.evidence_id]
    )


def test_writer_requires_evidence_splits_long_blocks_and_deduplicates() -> None:
    plan, pack = outline(), evidence()
    evidence_id = pack.items[0].evidence_id
    draft = SectionDraft(
        outline_section_id=plan.sections[0].section_id,
        blocks=[
            DraftBlock(
                text="Local agents retain state across safe restarts", evidence_ids=[evidence_id]
            ),
            DraftBlock(
                text="Local agents retain state across safe restarts", evidence_ids=[evidence_id]
            ),
        ],
    )
    document = SectionWriterAgent(max_block_chars=30).assemble(
        requirement(), plan, pack, [draft], title="Agent"
    )
    assert len(document.sections[0].blocks) == 2
    with pytest.raises(ValueError, match="neither evidence"):
        SectionWriterAgent().assemble(
            requirement(),
            plan,
            pack,
            [
                SectionDraft(
                    outline_section_id=plan.sections[0].section_id,
                    blocks=[DraftBlock(text="Unsupported claim")],
                )
            ],
            title="bad",
        )
    with pytest.raises(ValueError, match="unknown evidence"):
        SectionWriterAgent().assemble(
            requirement(),
            plan,
            pack,
            [
                SectionDraft(
                    outline_section_id=plan.sections[0].section_id,
                    blocks=[DraftBlock(text="Fake", evidence_ids=[uuid4()])],
                )
            ],
            title="bad",
        )


def test_pre_post_review_structured_issues_waive_and_targeted_repair() -> None:
    plan, pack = outline(), evidence()
    evidence_id = pack.items[0].evidence_id
    draft = SectionDraft(
        outline_section_id=plan.sections[0].section_id,
        blocks=[
            DraftBlock(
                text="Local agents retain state across safe restarts", evidence_ids=[evidence_id]
            )
        ],
    )
    document = SectionWriterAgent().assemble(requirement(), plan, pack, [draft], title="Agent")
    preflight = ReviewAgent().preflight(
        requirement(), plan, pack, None, ReviewPolicy(level="standard")
    )
    assert preflight.passed
    document.sections[0].blocks.append(
        DocumentBlock(
            kind=BlockKind.PARAGRAPH,
            text="unsupported",
            provenance=Provenance(agent="writer"),
        )
    )
    report = ReviewAgent().postflight(
        document, requirement(), pack, ReviewPolicy(level="standard", length_tolerance=0)
    )
    assert not report.passed
    unsupported = next(item for item in report.issues if item.category is ReviewCategory.EVIDENCE)
    assert unsupported.section_id and unsupported.block_id
    repair = RepairPlanner().plan(report.issues, round=1)
    task = next(item for item in repair.tasks if unsupported.issue_id in item.issue_ids)
    assert task.responsible_agent == "writer_agent"
    assert task.block_ids == [unsupported.block_id]
    assert RepairPlanner.waive(unsupported).status == "waived"
    assert RepairPlanner().plan(report.issues, round=4).human_takeover


def test_writer_repairs_schema_with_evidence_constraints() -> None:
    plan, pack = outline(), evidence()
    section_id = plan.sections[0].section_id
    evidence_id = pack.items[0].evidence_id
    valid = SectionDraft(
        outline_section_id=section_id,
        blocks=[DraftBlock(text="Supported result", evidence_ids=[evidence_id])],
    )
    provider = SchemaRepairProvider(
        [
            json.dumps({"section_id": str(section_id), "title": "missing content"}),
            valid.model_dump_json(),
        ]
    )
    writer = SectionWriterAgent(provider)

    draft = asyncio.run(writer.generate_section(requirement(), plan.sections[0], pack))

    assert draft == valid
    assert writer.last_schema_repair_count == 1
    assert provider.request_log[1].messages[-1].role == "user"
    assert (
        "Only these evidence_ids are allowed"
        in provider.request_log[1].messages[-1].content
    )
    initial_payload = json.loads(provider.request_log[0].messages[-1].content)
    assert initial_payload["writer_contract"]["outline_section_id"] == str(section_id)
    assert str(evidence_id) in initial_payload["writer_contract"]["allowed_evidence_ids"]


def test_writer_projection_preserves_text_and_rejects_unknown_citation() -> None:
    plan, pack = outline(), evidence()
    section_id = plan.sections[0].section_id
    evidence_id = pack.items[0].evidence_id
    projected = SectionWriterAgent._project_draft(
        json.dumps(
            {
                "section_id": str(section_id),
                "content": "Supported result",
                "citations": [str(evidence_id)],
            }
        ),
        section_id=section_id,
        allowed_evidence={evidence_id},
    )

    assert projected is not None
    assert projected.blocks[0].evidence_ids == [evidence_id]
    assert (
        SectionWriterAgent._project_draft(
            json.dumps(
                {
                    "section_id": str(section_id),
                    "content": "Unsupported",
                    "citations": [str(uuid4())],
                }
            ),
            section_id=section_id,
            allowed_evidence={evidence_id},
        )
        is None
    )
