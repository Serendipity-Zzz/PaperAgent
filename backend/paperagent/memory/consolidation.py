from __future__ import annotations

from pydantic import Field

from paperagent.memory.repository import FileMemoryRepository
from paperagent.memory.schemas import MemoryEntry, MemoryScope, MemoryStatus
from paperagent.schemas.common import StrictModel


class ConsolidationResult(StrictModel):
    duplicate_ids: list[str] = Field(default_factory=list)
    contested_ids: list[str] = Field(default_factory=list)
    superseded_ids: list[str] = Field(default_factory=list)


class MemoryConsolidator:
    def __init__(self, repository: FileMemoryRepository) -> None:
        self.repository = repository

    def consolidate(
        self, scope: MemoryScope, project_id: str | None = None
    ) -> ConsolidationResult:
        entries = [entry for entry, _path in self.repository.list(scope, project_id)]
        by_subject: dict[tuple[str, str], list[MemoryEntry]] = {}
        for entry in entries:
            if entry.status is MemoryStatus.ARCHIVED:
                continue
            by_subject.setdefault((entry.topic, entry.subject.casefold()), []).append(entry)
        duplicates: list[str] = []
        contested: list[str] = []
        superseded: list[str] = []
        for related in by_subject.values():
            related.sort(key=lambda entry: entry.created_at)
            hashes: dict[str | None, MemoryEntry] = {}
            for entry in related:
                prior = hashes.get(entry.content_hash)
                if prior is not None:
                    updated = entry.model_copy(
                        update={
                            "status": MemoryStatus.SUPERSEDED,
                            "supersedes_id": prior.memory_id,
                        }
                    )
                    self.repository.update(updated)
                    duplicates.append(str(entry.memory_id))
                    superseded.append(str(entry.memory_id))
                else:
                    hashes[entry.content_hash] = entry
            active = [
                entry
                for entry in related
                if str(entry.memory_id) not in set(superseded)
            ]
            if len({entry.content_hash for entry in active}) > 1:
                active_ids = [entry.memory_id for entry in active]
                for entry in active:
                    conflicts = [
                        memory_id
                        for memory_id in active_ids
                        if memory_id != entry.memory_id
                    ]
                    self.repository.update(
                        entry.model_copy(
                            update={
                                "status": MemoryStatus.CONTESTED,
                                "conflict_ids": conflicts,
                            }
                        )
                    )
                    contested.append(str(entry.memory_id))
        return ConsolidationResult(
            duplicate_ids=duplicates,
            contested_ids=sorted(set(contested)),
            superseded_ids=superseded,
        )
