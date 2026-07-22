from __future__ import annotations

from dataclasses import dataclass

from paperagent.knowledge.index import FtsKnowledgeIndex, LanceKnowledgeIndex, SearchHit
from paperagent.knowledge.planner import KnowledgeQueryPlan, QueryKind, classify_query


@dataclass(frozen=True)
class RetrievalResult:
    hit: SearchHit
    score: float
    reason: str


class HybridRetriever:
    def __init__(self, fts: FtsKnowledgeIndex, vector: LanceKnowledgeIndex | None = None) -> None:
        self.fts = fts
        self.vector = vector

    def search(
        self, query: str, *, project_id: str | None = None, limit: int = 10
    ) -> list[RetrievalResult]:
        kind = classify_query(query)
        ranked_lists = [self.fts.search(query, limit=limit * 3, project_id=project_id)]
        if self.vector is not None:
            ranked_lists.append(self.vector.search(query, limit=limit * 3, project_id=project_id))
        scores: dict[str, float] = {}
        hits: dict[str, SearchHit] = {}
        reasons: dict[str, list[str]] = {}
        for channel, ranked in enumerate(ranked_lists):
            for rank, hit in enumerate(ranked, start=1):
                score = 1.0 / (60 + rank)
                if kind is QueryKind.ACADEMIC:
                    if hit.citation_policy == "scholarly":
                        score *= 1.8
                    if hit.citation_policy in {"process_only", "never"}:
                        score *= 0.05
                if kind is QueryKind.TECHNICAL and hit.trust_level in {"normative", "verified"}:
                    score *= 1.5
                scores[hit.item_id] = scores.get(hit.item_id, 0) + score
                hits[hit.item_id] = hit
                reasons.setdefault(hit.item_id, []).append("fts" if channel == 0 else "vector")
        deduplicated: dict[str, RetrievalResult] = {}
        for item_id, score in scores.items():
            hit = hits[item_id]
            result = RetrievalResult(hit, score, f"{kind.value}: {'+'.join(reasons[item_id])}")
            existing = deduplicated.get(hit.content_hash)
            if existing is None or result.score > existing.score:
                deduplicated[hit.content_hash] = result
        return sorted(deduplicated.values(), key=lambda item: item.score, reverse=True)[:limit]

    def execute(self, plan: KnowledgeQueryPlan) -> list[RetrievalResult]:
        ranked_lists: list[list[SearchHit]] = []
        if plan.use_fts:
            ranked_lists.append(
                self.fts.search(
                    plan.query, limit=plan.limit * 3, project_id=plan.project_id
                )
            )
        if plan.use_vector and self.vector is not None:
            ranked_lists.append(
                self.vector.search(
                    plan.query, limit=plan.limit * 3, project_id=plan.project_id
                )
            )
        scores: dict[str, float] = {}
        hits: dict[str, SearchHit] = {}
        reasons: dict[str, list[str]] = {}
        trust_weight = {"normative": 1.5, "verified": 1.3, "unverified": 0.8}
        for channel, ranked in enumerate(ranked_lists):
            for rank, hit in enumerate(ranked, start=1):
                if hit.confidentiality not in plan.allowed_confidentiality:
                    continue
                if plan.citable_only and hit.citation_policy in {
                    "internal_only",
                    "process_only",
                    "never",
                }:
                    continue
                score = 1 / (60 + rank)
                score *= trust_weight.get(hit.trust_level, 1)
                if plan.kind is QueryKind.ACADEMIC and hit.citation_policy == "scholarly":
                    score *= 1.8
                scores[hit.item_id] = scores.get(hit.item_id, 0) + score
                hits[hit.item_id] = hit
                reasons.setdefault(hit.item_id, []).append(
                    "fts" if channel == 0 else "vector"
                )
        results = [
            RetrievalResult(
                hit=hits[item_id],
                score=score,
                reason=f"{plan.kind.value}: {'+'.join(reasons[item_id])}",
            )
            for item_id, score in scores.items()
        ]
        deduplicated: dict[str, RetrievalResult] = {}
        for result in results:
            prior = deduplicated.get(result.hit.content_hash)
            if prior is None or result.score > prior.score:
                deduplicated[result.hit.content_hash] = result
        return sorted(
            deduplicated.values(), key=lambda result: result.score, reverse=True
        )[: plan.limit]
