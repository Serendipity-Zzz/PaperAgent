from __future__ import annotations

from pydantic import Field

from paperagent.memory.repository import FileMemoryRepository
from paperagent.memory.schemas import MemoryScope, MemoryStatus
from paperagent.schemas.common import StrictModel


class MemoryManifestItem(StrictModel):
    memory_id: str
    topic: str
    subject: str
    status: MemoryStatus
    sensitivity: str
    allowed_providers: list[str]
    relative_path: str
    source_id: str


class MemoryManifest(StrictModel):
    scope: MemoryScope
    project_id: str | None = None
    items: list[MemoryManifestItem] = Field(default_factory=list)


def build_manifest(
    repository: FileMemoryRepository,
    scope: MemoryScope,
    project_id: str | None = None,
) -> MemoryManifest:
    items = [
        MemoryManifestItem(
            memory_id=str(entry.memory_id),
            topic=entry.topic,
            subject=entry.subject,
            status=entry.status,
            sensitivity=entry.sensitivity.value,
            allowed_providers=entry.allowed_providers,
            relative_path=path.relative_to(repository.data_dir).as_posix(),
            source_id=entry.source_id,
        )
        for entry, path in repository.list(scope, project_id)
    ]
    return MemoryManifest(scope=scope, project_id=project_id, items=items)
