from __future__ import annotations

import re

from pydantic import Field

from paperagent.memory.repository import FileMemoryRepository
from paperagent.memory.schemas import MemoryEntry, MemoryScope, MemoryStatus
from paperagent.schemas.common import StrictModel

_SECRET = re.compile(
    r"(?i)(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9_-]{12,}\b"
)


class MemoryCandidate(StrictModel):
    topic: str
    subject: str
    kind: str
    content: str
    source_type: str
    source_id: str
    source_locator: str | None = None
    confidence: float = Field(ge=0, le=1)
    explicit_long_term_intent: bool = False


class MemoryExtractionService:
    def __init__(self, repository: FileMemoryRepository) -> None:
        self.repository = repository

    def persist_candidates(
        self,
        candidates: list[MemoryCandidate],
        *,
        scope: MemoryScope,
        project_id: str | None,
        thread_id: str,
        sequence: int,
    ) -> list[MemoryEntry]:
        stored: list[MemoryEntry] = []
        for index, candidate in enumerate(candidates):
            self._validate(candidate, scope)
            entry = MemoryEntry(
                topic=candidate.topic,
                subject=candidate.subject,
                scope=scope,
                project_id=project_id,
                kind=candidate.kind,
                content=candidate.content,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                source_locator=candidate.source_locator,
                confidence=candidate.confidence,
                status=(
                    MemoryStatus.CONFIRMED
                    if candidate.explicit_long_term_intent
                    else MemoryStatus.CANDIDATE
                ),
                idempotency_key=f"extract:{thread_id}:{sequence}:{index}",
            )
            result = self.repository.write(
                entry,
                explicit=scope is MemoryScope.GLOBAL
                and candidate.explicit_long_term_intent,
            )
            stored.append(result.entry)
        if project_id:
            self.repository.write_cursor(project_id, thread_id, sequence)
        return stored

    @staticmethod
    def _validate(candidate: MemoryCandidate, scope: MemoryScope) -> None:
        if _SECRET.search(candidate.content):
            raise ValueError("candidate contains a credential-like secret")
        if candidate.kind in {"raw_project_source", "credential", "api_key"}:
            raise ValueError("candidate kind is forbidden")
        if scope is MemoryScope.GLOBAL and not candidate.explicit_long_term_intent:
            raise PermissionError("global extraction requires explicit long-term intent")
