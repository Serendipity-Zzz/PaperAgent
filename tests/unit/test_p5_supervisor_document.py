from uuid import uuid4

import pytest

from paperagent.agents.document_ir import (
    BlockKind,
    DocumentBlock,
    DocumentIR,
    DocumentSection,
    Provenance,
    diff_documents,
    migrate_document_ir,
)
from paperagent.agents.evidence import EvidenceBundle
from paperagent.agents.outline import OutlineDesignerAgent
from paperagent.agents.state import RawRequest, RequirementSpec
from paperagent.agents.supervisor import AssignmentStatus, SupervisorAgent
from paperagent.agents.word_count import CountPolicy, WordCountTool
from paperagent.orchestration.failure import FailureAnalyzer


def requirement(*, experiment: bool = True, image: bool = True) -> RequirementSpec:
    return RequirementSpec.model_validate(
        {
            "raw_request": RawRequest(text="明确报告需求"),
            "normalized_request": "撰写实验报告",
            "research_formulation": {"research_topic": "本地智能体"},
            "document_type": "experiment_report",
            "primary_language": "mixed",
            "target_length": {"value": 1000, "unit": "mixed_score"},
            "audience": "研究人员",
            "citation_style": "APA",
            "requires_literature_search": True,
            "requires_experiment": experiment,
            "requires_data_chart": True,
            "requires_generated_image": image,
            "output_formats": ["docx", "pdf"],
        }
    ).confirm()


def test_supervisor_routes_parallel_approval_rejection_failure_and_loop_limit() -> None:
    spec = requirement()
    outline = OutlineDesignerAgent().design(spec)
    evidence = EvidenceBundle(
        queries=[], items=[], claim_map=[], reference_evidence_ids=[], warnings=[]
    )
    plan = SupervisorAgent().plan(spec, outline, evidence)
    assert plan.parallel_groups == [["experiment", "image"]]
    experiment = next(item for item in plan.assignments if item.node == "experiment")
    rejected = SupervisorAgent.decide_approval(
        plan, experiment.approval.approval_id, approved=False
    )
    assert (
        next(item for item in rejected.assignments if item.node == "experiment").status
        is AssignmentStatus.CANCELLED
    )
    retried = plan
    for _ in range(3):
        retried = SupervisorAgent.replan_failure(retried, "writing")
    assert (
        next(item for item in retried.assignments if item.node == "writing").status
        is AssignmentStatus.HUMAN_REQUIRED
    )
    for _ in range(plan.max_repair_rounds + 1):
        plan = SupervisorAgent.next_repair_round(plan)
    assert (
        next(item for item in plan.assignments if item.node == "repair").status
        is AssignmentStatus.HUMAN_REQUIRED
    )
    with pytest.raises(PermissionError):
        SupervisorAgent().plan(
            RequirementSpec(raw_request=RawRequest(text="vague")), outline, evidence
        )


def test_supervisor_records_failure_and_changes_semantic_recovery_strategy() -> None:
    spec = requirement()
    plan = SupervisorAgent().plan(
        spec,
        OutlineDesignerAgent().design(spec),
        EvidenceBundle(
            queries=[], items=[], claim_map=[], reference_evidence_ids=[], warnings=[]
        ),
    )
    failure = FailureAnalyzer.analyze("writing", ValueError("invalid JSON schema"))

    first = SupervisorAgent.replan_failure(plan, "writing", failure)
    second = SupervisorAgent.replan_failure(first, "writing", failure)
    assignment = next(item for item in second.assignments if item.node == "writing")

    assert assignment.strategy_history == [
        "schema_repair_with_error_feedback",
        "reduce_output_scope_and_split",
    ]
    assert len(assignment.failure_history) == 2


def document() -> DocumentIR:
    return DocumentIR(
        requirement_id=uuid4(),
        requirement_version=1,
        outline_id=uuid4(),
        title="混合文档",
        language="mixed",
        sections=[
            DocumentSection(
                title="正文",
                goal="test",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="中文内容 supports local-first agents.",
                        provenance=Provenance(agent="writer", author_viewpoint=True),
                    ),
                    DocumentBlock(
                        kind=BlockKind.TABLE,
                        text="表格 excluded words",
                        caption="Table caption",
                        provenance=Provenance(agent="writer"),
                    ),
                ],
            ),
            DocumentSection(
                title="参考文献",
                goal="refs",
                blocks=[
                    DocumentBlock(
                        kind=BlockKind.PARAGRAPH,
                        text="Reference should be excluded",
                        provenance=Provenance(agent="writer"),
                    )
                ],
            ),
        ],
    )


def test_document_ir_roundtrip_patch_diff_migration_and_word_count() -> None:
    original = document()
    restored = DocumentIR.model_validate_json(original.model_dump_json())
    block_id = restored.sections[0].blocks[0].block_id
    changed = restored.patch_block(block_id, {"text": "中文改动 with three words"})
    diff = diff_documents(restored, changed)
    assert diff.changed_blocks == [block_id]
    report = WordCountTool().count(changed)
    assert report.chinese_chars == 4
    assert report.english_words == 3
    assert report.mixed_score == 7
    assert report.excluded_blocks == 2
    included = WordCountTool().count(
        changed,
        CountPolicy(exclude_references=False, exclude_tables=False, include_captions=True),
    )
    assert included.english_words > report.english_words
    legacy = original.model_dump(mode="json")
    legacy["schema_version"] = "0.1"
    legacy["sections"] = [{"title": "Legacy", "content": "old body"}]
    migrated = migrate_document_ir(legacy)
    assert migrated.sections[0].blocks[0].text == "old body"
