from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from paperagent.ingestion.classification import Classification
from paperagent.ingestion.schemas import ImportReport
from paperagent.knowledge.evidence_pack import EvidencePack, EvidencePackBuilder
from paperagent.knowledge.index import FtsKnowledgeIndex, LanceKnowledgeIndex, SearchHit
from paperagent.knowledge.models import (
    CitationPolicy,
    Confidentiality,
    KnowledgeItem,
    KnowledgeScope,
    ReviewStatus,
    TrustLevel,
)
from paperagent.knowledge.planner import KnowledgeQueryPlanner
from paperagent.knowledge.retrieval import HybridRetriever, RetrievalResult


def report_to_items(
    report: ImportReport,
    classification: Classification,
    *,
    collection_id: str,
    scope: KnowledgeScope,
    project_id: str | None,
    source_uri: str | None = None,
    confidentiality: Confidentiality = Confidentiality.PERSONAL,
) -> list[KnowledgeItem]:
    items: list[KnowledgeItem] = []
    for chunk in report.source.chunks:
        citation = CitationPolicy.INTERNAL_ONLY
        if chunk.citation_policy.value == "process_only":
            citation = CitationPolicy.PROCESS_ONLY
        elif chunk.kind == "bibliography" and chunk.metadata.get("doi"):
            citation = CitationPolicy.SCHOLARLY
        uri = source_uri
        if chunk.metadata.get("doi"):
            uri = f"https://doi.org/{chunk.metadata['doi']}"
        items.append(
            KnowledgeItem(
                collection_id=collection_id,
                scope=scope,
                project_id=project_id,
                content_type=classification.primary_type,
                title=report.source.name,
                content=chunk.text,
                language="mixed",
                source_kind="user_upload",
                source_uri=uri,
                source_file_id=str(report.source.id),
                confidentiality=confidentiality,
                trust_level=TrustLevel.UNVERIFIED,
                citation_policy=citation,
                instruction_trust=False,
                locator=chunk.locator,
                content_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
                tags=[classification.primary_type, *classification.secondary_types],
                review_status=ReviewStatus.ACCEPTED,
            )
        )
    return items


class ProjectKnowledgeService:
    def __init__(
        self,
        project_root: Path,
        vector_index: LanceKnowledgeIndex | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.source_root = self.project_root / "sources" / "knowledge-items"
        self.source_root.mkdir(parents=True, exist_ok=True)
        self.index = FtsKnowledgeIndex(self.project_root / "indexes" / "knowledge.db")
        self.vector_index = vector_index
        self.retriever = HybridRetriever(self.index, vector_index)
        self.planner = KnowledgeQueryPlanner()

    def ingest(self, items: list[KnowledgeItem]) -> int:
        if not items:
            return 0
        for item in items:
            self._write_source(item)
        ids = [str(item.id) for item in items]
        try:
            self.index.upsert(items)
            if self.vector_index is not None:
                self.vector_index.index(items)
        except Exception:
            for item_id in ids:
                self.index.delete(item_id)
            if self.vector_index is not None:
                self.vector_index.delete_many(ids)
            raise
        return len(items)

    def search(self, query: str, *, project_id: str, limit: int = 20) -> list[SearchHit]:
        return self.index.search(query, project_id=project_id, limit=limit)

    def delete(self, item_id: str) -> None:
        self.index.delete(item_id)
        if self.vector_index is not None:
            self.vector_index.delete_many([item_id])

    def retrieve(
        self,
        query: str,
        *,
        project_id: str | None,
        citable_only: bool = False,
        limit: int = 10,
    ) -> list[RetrievalResult]:
        plan = self.planner.plan(
            query,
            project_id=project_id,
            vector_available=self.vector_index is not None,
            citable_only=citable_only,
            limit=limit,
        )
        return self.retriever.execute(plan)

    def evidence_pack(
        self,
        query: str,
        *,
        project_id: str | None,
        claims: dict[str, str] | None = None,
        limit: int = 20,
    ) -> EvidencePack:
        results = self.retrieve(
            query,
            project_id=project_id,
            citable_only=False,
            limit=limit,
        )
        return EvidencePackBuilder().build(query, results, claims)

    def rebuild(self) -> int:
        items = [
            KnowledgeItem.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.source_root.glob("*.json"))
        ]
        self.index.rebuild()
        if self.vector_index is not None:
            self.vector_index.index(items)
        return len(items)

    def _write_source(self, item: KnowledgeItem) -> None:
        target = self.source_root / f"{item.id}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".paperagent-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(item.model_dump(mode="json"), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
