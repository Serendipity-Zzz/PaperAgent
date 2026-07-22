from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from paperagent.schemas.common import StrictModel


class QueryKind(StrEnum):
    ACADEMIC = "academic"
    TECHNICAL = "technical"
    PROCESS = "process"
    GENERAL = "general"


def classify_query(query: str) -> QueryKind:
    lowered = query.lower()
    signals = {
        QueryKind.ACADEMIC: ("论文", "研究", "证据", "引用", "doi", "literature"),
        QueryKind.TECHNICAL: ("api", "cuda", "版本", "安装", "错误", "配置", "兼容"),
        QueryKind.PROCESS: ("会议", "聊天", "决定", "行动项", "需求变更"),
    }
    scores = {
        kind: sum(lowered.count(term) for term in terms)
        for kind, terms in signals.items()
    }
    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best if scores[best] else QueryKind.GENERAL


class KnowledgeQueryPlan(StrictModel):
    query: str = Field(min_length=1, max_length=20_000)
    kind: QueryKind
    use_fts: bool = True
    use_vector: bool = False
    citable_only: bool = False
    allowed_confidentiality: set[str] = Field(
        default_factory=lambda: {"public", "personal"}
    )
    project_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class KnowledgeQueryPlanner:
    def plan(
        self,
        query: str,
        *,
        project_id: str | None,
        vector_available: bool,
        citable_only: bool = False,
        limit: int = 10,
    ) -> KnowledgeQueryPlan:
        kind = classify_query(query)
        return KnowledgeQueryPlan(
            query=query,
            kind=kind,
            use_vector=vector_available and (kind is not QueryKind.PROCESS),
            citable_only=citable_only,
            project_id=project_id,
            limit=limit,
        )
