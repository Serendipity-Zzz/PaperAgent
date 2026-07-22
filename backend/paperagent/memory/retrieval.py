from __future__ import annotations

import re
from collections import defaultdict

from pydantic import Field

from paperagent.memory.manifest import build_manifest
from paperagent.memory.repository import FileMemoryRepository
from paperagent.memory.schemas import MemoryEntry, MemoryScope, MemoryStatus
from paperagent.schemas.common import StrictModel

_TOKEN = re.compile(r"[A-Za-z0-9_-]+|[\u3400-\u9fff]", re.UNICODE)


class MemoryQuery(StrictModel):
    text: str = Field(min_length=1, max_length=10_000)
    scope: MemoryScope
    project_id: str | None = None
    provider_id: str | None = None
    max_topics: int = Field(default=5, ge=1, le=5)
    max_entries: int = Field(default=20, ge=1, le=100)


class MemoryMatch(StrictModel):
    entry: MemoryEntry
    relative_path: str
    score: float = Field(ge=0)
    reasons: list[str]


class MemoryRetriever:
    def __init__(self, repository: FileMemoryRepository) -> None:
        self.repository = repository

    def retrieve(self, query: MemoryQuery) -> list[MemoryMatch]:
        manifest = build_manifest(self.repository, query.scope, query.project_id)
        query_tokens = _tokens(query.text)
        topic_scores: dict[str, float] = defaultdict(float)
        for item in manifest.items:
            if item.status in {MemoryStatus.ARCHIVED, MemoryStatus.SUPERSEDED}:
                continue
            topic_scores[item.topic] += 3 * len(query_tokens & _tokens(item.topic))
            topic_scores[item.topic] += 2 * len(query_tokens & _tokens(item.subject))
        topics = {
            topic
            for topic, _score in sorted(
                topic_scores.items(), key=lambda pair: (-pair[1], pair[0])
            )[: query.max_topics]
            if _score > 0
        }
        if not topics:
            return []
        matches: list[MemoryMatch] = []
        for entry, path in self.repository.list(query.scope, query.project_id):
            if entry.topic not in topics or entry.status in {
                MemoryStatus.ARCHIVED,
                MemoryStatus.SUPERSEDED,
            }:
                continue
            if (
                query.provider_id
                and entry.allowed_providers
                and query.provider_id not in entry.allowed_providers
            ):
                continue
            subject_overlap = query_tokens & _tokens(entry.subject)
            content_overlap = query_tokens & _tokens(entry.content)
            score = topic_scores[entry.topic] + 2 * len(subject_overlap) + len(content_overlap)
            if score <= 0:
                continue
            matches.append(
                MemoryMatch(
                    entry=entry,
                    relative_path=path.relative_to(self.repository.data_dir).as_posix(),
                    score=score,
                    reasons=[
                        f"topic:{entry.topic}",
                        f"subject_overlap:{len(subject_overlap)}",
                        f"content_overlap:{len(content_overlap)}",
                    ],
                )
            )
        return sorted(matches, key=lambda match: (-match.score, match.entry.subject))[
            : query.max_entries
        ]


def _tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _TOKEN.finditer(value)}
