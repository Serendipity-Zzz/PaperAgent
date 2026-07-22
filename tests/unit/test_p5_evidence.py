from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paperagent.agents.evidence import ClaimRequest, LiteratureEvidenceAgent
from paperagent.agents.outline import EvidenceNeed, OutlinePlan, SectionPlan
from paperagent.ingestion.schemas import Locator
from paperagent.knowledge.models import (
    CitationPolicy,
    Confidentiality,
    KnowledgeItem,
    KnowledgeScope,
    TrustLevel,
)
from paperagent.literature import LiteratureRecord


def outline() -> OutlinePlan:
    requirement_id = uuid4()
    return OutlinePlan(
        requirement_id=requirement_id,
        requirement_version=1,
        document_type="academic_paper",
        framework_id="test",
        source="builtin",
        selection_reason="test",
        length_unit="chinese_char",
        target_length=1000,
        sections=[
            SectionPlan(
                title="相关研究",
                goal="比较本地智能体",
                target_length=1000,
                evidence_needs=[EvidenceNeed(kind="literature", purpose="比较智能体")],
            )
        ],
    )


def knowledge(title: str, *, expired: bool = False) -> KnowledgeItem:
    return KnowledgeItem(
        collection_id="project",
        scope=KnowledgeScope.PROJECT,
        project_id="project-1",
        content_type="technical_doc",
        title=title,
        content="本地智能体采用 SQLite checkpoint",
        language="zh",
        source_kind="user_upload",
        source_file_id="file-1",
        confidentiality=Confidentiality.PERSONAL,
        trust_level=TrustLevel.VERIFIED,
        citation_policy=CitationPolicy.INTERNAL_ONLY,
        locator=Locator(line_start=1, line_end=1),
        content_hash="a" * 64,
        expires_at=datetime.now(UTC) - timedelta(days=1) if expired else None,
    )


def test_dedup_verification_internal_stale_offline_and_claim_mapping() -> None:
    verified = LiteratureRecord(
        title="Local Agent Persistence",
        authors=("A. Author",),
        year=2026,
        doi="10.1000/local-agent",
        source="crossref",
        source_uri="https://doi.org/10.1000/local-agent",
        abstract="SQLite enables local agent persistence",
    )
    duplicate = LiteratureRecord(
        title="LOCAL AGENT PERSISTENCE",
        authors=("A. Author",),
        year=2026,
        doi="10.1000/local-agent",
        source="openalex",
        source_uri="https://openalex.org/W1",
    )
    unverifiable = LiteratureRecord(
        title="Unknown claim",
        authors=(),
        year=None,
        doi=None,
        source="cache",
        source_uri="",
    )
    section_id = outline().sections[0].section_id
    claims = [
        ClaimRequest(section_id=section_id, text="SQLite persistence", keywords=["SQLite"]),
        ClaimRequest(section_id=section_id, text="quantum result", keywords=["quantum"]),
    ]
    bundle = LiteratureEvidenceAgent().build(
        outline(),
        literature=[verified, duplicate, unverifiable],
        knowledge=[knowledge("内部手册"), knowledge("过期技术文档", expired=True)],
        claims=claims,
        offline=True,
    )
    assert len([item for item in bundle.items if item.kind == "literature"]) == 2
    assert len(bundle.reference_evidence_ids) == 1
    assert bundle.claim_map[0].supported
    assert not bundle.claim_map[1].supported
    assert bundle.offline
    assert any("过期" in warning for warning in bundle.warnings)
    assert any("缺少证据" in warning for warning in bundle.warnings)
    internal = next(item for item in bundle.items if item.title == "内部手册")
    assert not internal.scholarly_citation
    assert internal.locator["line_start"] == 1
