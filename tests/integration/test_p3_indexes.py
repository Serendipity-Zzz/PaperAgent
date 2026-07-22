import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from paperagent.ingestion.schemas import Locator
from paperagent.knowledge.index import (
    DeterministicTestEmbedder,
    EmbeddingModelStore,
    FtsKnowledgeIndex,
    LanceKnowledgeIndex,
)
from paperagent.knowledge.models import (
    CitationPolicy,
    KnowledgeItem,
    KnowledgeScope,
    TrustLevel,
)
from paperagent.knowledge.retrieval import HybridRetriever


def item(
    text: str,
    *,
    project_id: str | None = None,
    citation: CitationPolicy = CitationPolicy.INTERNAL_ONLY,
    trust: TrustLevel = TrustLevel.UNVERIFIED,
) -> KnowledgeItem:
    return KnowledgeItem(
        collection_id="test",
        scope=KnowledgeScope.PROJECT if project_id else KnowledgeScope.GLOBAL,
        project_id=project_id,
        content_type="paper" if citation is CitationPolicy.SCHOLARLY else "chat",
        title=text[:30],
        content=text,
        language="mixed",
        source_kind="open_access" if citation is CitationPolicy.SCHOLARLY else "user_upload",
        source_uri="https://example.test/source" if citation is CitationPolicy.SCHOLARLY else None,
        trust_level=trust,
        citation_policy=citation,
        locator=Locator(line_start=1),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        review_status="accepted",
    )


def test_fts_incremental_project_isolation_and_hybrid_policy(tmp_path: Path) -> None:
    fts = FtsKnowledgeIndex(tmp_path / "knowledge.db")
    scholarly = item(
        "人工智能教育研究证据",
        citation=CitationPolicy.SCHOLARLY,
        trust=TrustLevel.VERIFIED,
    )
    chat = item("聊天中提到人工智能教育研究", project_id="project-a")
    isolated = item("人工智能教育研究 private", project_id="project-b")
    expired = item("outdated CUDA compatibility", trust=TrustLevel.VERIFIED)
    expired.scope = KnowledgeScope.DYNAMIC
    expired.expires_at = datetime.now(UTC) - timedelta(days=1)
    assert fts.upsert([scholarly, chat, isolated, expired]) == 4
    results = HybridRetriever(fts).search("人工智能教育研究证据", project_id="project-a")
    assert results[0].hit.item_id == str(scholarly.id)
    assert all(result.hit.project_id != "project-b" for result in results)
    assert not fts.search("outdated CUDA")
    fts.supersede(str(scholarly.id))
    assert all(
        hit.item_id != str(scholarly.id) for hit in fts.search("证据", project_id="project-a")
    )


def test_lancedb_cross_language_checkpoint_and_dimension_rebuild(tmp_path: Path) -> None:
    records = [item("人工智能论文"), item("database transaction"), item("visual chart")]
    index = LanceKnowledgeIndex(tmp_path / "vectors", DeterministicTestEmbedder(64))
    assert index.index(records, batch_size=1) == 3
    assert index.state_path.is_file()
    hits = index.search("artificial intelligence paper")
    assert hits[0].item_id == str(records[0].id)
    rebuilt = LanceKnowledgeIndex(tmp_path / "vectors", DeterministicTestEmbedder(32))
    assert rebuilt.index(records) == 3
    assert rebuilt.search("artificial intelligence paper")


def test_embedding_model_download_requires_approval_and_is_shared(tmp_path: Path) -> None:
    store = EmbeddingModelStore(tmp_path / "global-models")
    model_id = "intfloat/multilingual-e5-small"
    status = store.status(model_id)
    assert status["approximate_download_mb"] == 450
    assert not status["installed"]
    try:
        store.install(model_id, approved=False, downloader=lambda _model, _path: None)
    except PermissionError:
        pass
    else:
        raise AssertionError("download without approval must fail")
    calls = 0

    def downloader(_model: str, path: Path) -> None:
        nonlocal calls
        calls += 1
        (path / "weights.mock").write_bytes(b"mock")

    first = store.install(model_id, approved=True, downloader=downloader)
    second = store.install(model_id, approved=True, downloader=downloader)
    assert first == second and calls == 1


def test_100k_fts_synthetic_chunks_performance(tmp_path: Path) -> None:
    index = FtsKnowledgeIndex(tmp_path / "performance.db")

    def rows():
        for number in range(100_000):
            text = f"synthetic knowledge row {number} retrieval marker"
            yield (
                f"id-{number}",
                "performance",
                "global",
                None,
                f"Row {number}",
                text,
                hashlib.sha256(text.encode()).hexdigest(),
            )

    started = time.perf_counter()
    assert index.bulk_insert_synthetic(rows()) == 100_000
    duration = time.perf_counter() - started
    assert index.search("retrieval marker", limit=5)
    assert duration < 30
