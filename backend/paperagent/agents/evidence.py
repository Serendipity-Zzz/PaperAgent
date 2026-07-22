from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from paperagent.agents.outline import OutlinePlan
from paperagent.knowledge.models import CitationPolicy, KnowledgeItem, TrustLevel
from paperagent.literature import LiteratureRecord


class EvidenceKind(StrEnum):
    LITERATURE = "literature"
    INTERNAL = "internal"
    EXPERIMENT = "experiment"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    STALE = "stale"
    REJECTED = "rejected"


class EvidenceItem(BaseModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    kind: EvidenceKind
    title: str
    content: str
    source_uri: str | None = None
    source_id: str
    verification: VerificationStatus
    scholarly_citation: bool = False
    locator: dict[str, object] = Field(default_factory=dict)
    reason: str


class ClaimRequest(BaseModel):
    claim_id: UUID = Field(default_factory=uuid4)
    section_id: UUID
    text: str
    keywords: list[str] = Field(default_factory=list)


class ClaimEvidence(BaseModel):
    claim_id: UUID
    evidence_ids: list[UUID]
    supported: bool
    reason: str


class EvidenceBundle(BaseModel):
    queries: list[str]
    items: list[EvidenceItem]
    claim_map: list[ClaimEvidence]
    reference_evidence_ids: list[UUID]
    warnings: list[str] = Field(default_factory=list)
    offline: bool = False


def normalized_title(value: str) -> str:
    return re.sub(r"\W+", "", value).casefold()


class LiteratureEvidenceAgent:
    def build(
        self,
        outline: OutlinePlan,
        *,
        literature: list[LiteratureRecord],
        knowledge: list[KnowledgeItem],
        claims: list[ClaimRequest] | None = None,
        offline: bool = False,
    ) -> EvidenceBundle:
        queries = self.queries(outline)
        items: list[EvidenceItem] = []
        seen: set[str] = set()
        warnings: list[str] = []
        for record in literature:
            key = (
                record.doi.casefold()
                if record.doi
                else f"{normalized_title(record.title)}:{record.year}"
            )
            if key in seen:
                continue
            seen.add(key)
            verified = bool(record.title and record.authors and record.source_uri)
            scholarly = verified and bool(record.doi or record.source in {"arxiv", "openalex"})
            status = VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED
            item = EvidenceItem(
                kind=EvidenceKind.LITERATURE,
                title=record.title,
                content=record.abstract or record.title,
                source_uri=record.source_uri,
                source_id=record.doi or record.source_uri,
                verification=status,
                scholarly_citation=scholarly,
                reason=(
                    "元数据和公开来源可核验"
                    if scholarly
                    else "缺少完整作者、来源或可核验标识, 不进入交付参考文献"
                ),
            )
            items.append(item)
            if not scholarly:
                warnings.append(f"文献未通过引用核验: {record.title}")
        now = datetime.now(UTC)
        for source in knowledge:
            stale = source.expires_at is not None and source.expires_at < now
            allowed = source.citation_policy not in {
                CitationPolicy.NEVER,
                CitationPolicy.PROCESS_ONLY,
            }
            verification = (
                VerificationStatus.STALE
                if stale
                else VerificationStatus.VERIFIED
                if source.trust_level in {TrustLevel.VERIFIED, TrustLevel.NORMATIVE}
                else VerificationStatus.UNVERIFIED
            )
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.INTERNAL,
                    title=source.title,
                    content=source.content,
                    source_uri=source.source_uri,
                    source_id=str(source.id),
                    verification=verification,
                    scholarly_citation=(
                        allowed
                        and not stale
                        and source.citation_policy is CitationPolicy.SCHOLARLY
                        and verification is VerificationStatus.VERIFIED
                    ),
                    locator=source.locator.model_dump(exclude_none=True),
                    reason="内部证据可用于过程追溯" if allowed else "引用策略禁止作为事实证据",
                )
            )
            if stale:
                warnings.append(f"知识条目已过期: {source.title}")
        claim_map = [self.match_claim(claim, items) for claim in claims or []]
        for mapping in claim_map:
            if not mapping.supported:
                warnings.append(f"主张缺少证据: {mapping.claim_id}")
        return EvidenceBundle(
            queries=queries,
            items=items,
            claim_map=claim_map,
            reference_evidence_ids=[item.evidence_id for item in items if item.scholarly_citation],
            warnings=warnings,
            offline=offline,
        )

    @staticmethod
    def queries(outline: OutlinePlan) -> list[str]:
        result: list[str] = []
        for section in outline.sections:
            for need in section.evidence_needs:
                result.append(f'"{section.title}" {need.kind} {need.purpose}')
        return list(dict.fromkeys(result))

    @staticmethod
    def match_claim(claim: ClaimRequest, items: list[EvidenceItem]) -> ClaimEvidence:
        terms = {term.casefold() for term in claim.keywords if len(term) > 1}
        if not terms:
            terms = {term.casefold() for term in re.findall(r"[\w-]{2,}", claim.text)}
        matches = [
            item.evidence_id
            for item in items
            if item.verification is VerificationStatus.VERIFIED
            and terms
            and any(term in f"{item.title} {item.content}".casefold() for term in terms)
        ]
        return ClaimEvidence(
            claim_id=claim.claim_id,
            evidence_ids=matches,
            supported=bool(matches),
            reason="关键词命中已核验证据" if matches else "没有可核验且语义相关的证据",
        )
