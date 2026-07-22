from __future__ import annotations

import re
from uuid import UUID, uuid4

from pydantic import Field

from paperagent.knowledge.retrieval import RetrievalResult
from paperagent.schemas.common import StrictModel


class EvidenceCandidate(StrictModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    item_id: str
    title: str
    excerpt: str
    source_uri: str | None = None
    locator: dict[str, object]
    content_hash: str
    trust_level: str
    citation_policy: str
    eligible_for_claims: bool
    eligible_for_references: bool
    retrieval_reason: str


class ClaimEvidenceLink(StrictModel):
    claim_id: str
    evidence_ids: list[UUID]
    supported: bool
    reason: str


class EvidencePack(StrictModel):
    query: str
    candidates: list[EvidenceCandidate]
    claim_links: list[ClaimEvidenceLink] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EvidencePackBuilder:
    def build(
        self,
        query: str,
        results: list[RetrievalResult],
        claims: dict[str, str] | None = None,
    ) -> EvidencePack:
        candidates: list[EvidenceCandidate] = []
        warnings: list[str] = []
        for result in results:
            hit = result.hit
            eligible = hit.trust_level in {"normative", "verified", "user_confirmed"}
            eligible = eligible and hit.citation_policy not in {"never", "process_only"}
            reference = eligible and hit.citation_policy == "scholarly" and bool(hit.source_uri)
            candidate = EvidenceCandidate(
                item_id=hit.item_id,
                title=hit.title,
                excerpt=hit.content,
                source_uri=hit.source_uri,
                locator=hit.locator,
                content_hash=hit.content_hash,
                trust_level=hit.trust_level,
                citation_policy=hit.citation_policy,
                eligible_for_claims=eligible,
                eligible_for_references=reference,
                retrieval_reason=result.reason,
            )
            candidates.append(candidate)
            if not eligible:
                warnings.append(f"证据未获事实主张资格: {hit.item_id}")
        links = [
            self._link_claim(claim_id, text, candidates)
            for claim_id, text in (claims or {}).items()
        ]
        warnings.extend(
            f"主张缺少合格证据: {link.claim_id}" for link in links if not link.supported
        )
        return EvidencePack(
            query=query,
            candidates=candidates,
            claim_links=links,
            warnings=warnings,
        )

    @staticmethod
    def _link_claim(
        claim_id: str, text: str, candidates: list[EvidenceCandidate]
    ) -> ClaimEvidenceLink:
        terms = {term.casefold() for term in re.findall(r"[\w-]{2,}", text)}
        matched = [
            candidate.evidence_id
            for candidate in candidates
            if candidate.eligible_for_claims
            and any(
                term in f"{candidate.title} {candidate.excerpt}".casefold()
                for term in terms
            )
        ]
        return ClaimEvidenceLink(
            claim_id=claim_id,
            evidence_ids=matched,
            supported=bool(matched),
            reason="matched eligible evidence" if matched else "no eligible evidence matched",
        )
