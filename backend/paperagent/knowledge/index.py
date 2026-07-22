from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from paperagent.knowledge.models import KnowledgeItem


@dataclass(frozen=True)
class SearchHit:
    item_id: str
    score: float
    title: str
    content: str
    source_uri: str | None
    locator: dict[str, object]
    trust_level: str
    citation_policy: str
    confidentiality: str
    scope: str
    project_id: str | None
    content_hash: str
    expires_at: str | None = None


def _query_tokens(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]", query.lower())
    return list(dict.fromkeys(words))[:32]


def _fts_text(text: str) -> str:
    cjk = " ".join(re.findall(r"[\u3400-\u9fff]", text))
    return f"{text} {cjk}" if cjk else text


class FtsKnowledgeIndex:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                project_id TEXT,
                content_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_uri TEXT,
                locator_json TEXT NOT NULL,
                trust_level TEXT NOT NULL,
                citation_policy TEXT NOT NULL,
                confidentiality TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                expires_at TEXT,
                review_status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_scope ON knowledge_chunks(scope, project_id);
            CREATE INDEX IF NOT EXISTS ix_knowledge_hash ON knowledge_chunks(content_hash);
            CREATE TABLE IF NOT EXISTS classification_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                previous_type TEXT NOT NULL,
                new_type TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                id UNINDEXED, title, content, tokenize='unicode61 remove_diacritics 2'
            );
            """
        )

    def upsert(self, items: Iterable[KnowledgeItem]) -> int:
        rows = list(items)
        if not rows:
            return 0
        with self.connection:
            for item in rows:
                item_id = str(item.id)
                self.connection.execute("DELETE FROM knowledge_fts WHERE id = ?", (item_id,))
                self.connection.execute(
                    """
                    INSERT INTO knowledge_chunks (
                        id, collection_id, scope, project_id, content_type, title, content,
                        source_uri, locator_json, trust_level, citation_policy, confidentiality,
                        content_hash, expires_at, review_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        collection_id=excluded.collection_id, scope=excluded.scope,
                        project_id=excluded.project_id, content_type=excluded.content_type,
                        title=excluded.title, content=excluded.content,
                        source_uri=excluded.source_uri, locator_json=excluded.locator_json,
                        trust_level=excluded.trust_level, citation_policy=excluded.citation_policy,
                        confidentiality=excluded.confidentiality,
                        content_hash=excluded.content_hash, expires_at=excluded.expires_at,
                        review_status=excluded.review_status
                    """,
                    (
                        item_id,
                        item.collection_id,
                        item.scope.value,
                        item.project_id,
                        item.content_type,
                        item.title,
                        item.content,
                        item.source_uri,
                        item.locator.model_dump_json(),
                        item.trust_level.value,
                        item.citation_policy.value,
                        item.confidentiality.value,
                        item.content_hash,
                        item.expires_at.isoformat() if item.expires_at else None,
                        item.review_status.value,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO knowledge_fts(id, title, content) VALUES (?, ?, ?)",
                    (item_id, _fts_text(item.title), _fts_text(item.content)),
                )
        return len(rows)

    def bulk_insert_synthetic(
        self,
        rows: Iterable[tuple[str, str, str, str | None, str, str, str]],
        *,
        batch_size: int = 5_000,
    ) -> int:
        """Performance-test and migration path for already validated records."""
        pending: list[tuple[str, str, str, str | None, str, str, str]] = []
        inserted = 0

        def flush() -> None:
            nonlocal inserted
            if not pending:
                return
            metadata_rows = [
                (
                    item_id,
                    collection_id,
                    scope,
                    project_id,
                    "synthetic",
                    title,
                    content,
                    None,
                    "{}",
                    "unverified",
                    "internal_only",
                    "personal",
                    content_hash,
                    None,
                    "accepted",
                )
                for (
                    item_id,
                    collection_id,
                    scope,
                    project_id,
                    title,
                    content,
                    content_hash,
                ) in pending
            ]
            fts_rows = [(row[0], _fts_text(row[5]), _fts_text(row[6])) for row in metadata_rows]
            with self.connection:
                self.connection.executemany(
                    """
                    INSERT INTO knowledge_chunks(
                        id, collection_id, scope, project_id, content_type, title, content,
                        source_uri, locator_json, trust_level, citation_policy, confidentiality,
                        content_hash, expires_at, review_status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    metadata_rows,
                )
                self.connection.executemany(
                    "INSERT INTO knowledge_fts(id,title,content) VALUES (?,?,?)", fts_rows
                )
            inserted += len(pending)
            pending.clear()

        for row in rows:
            pending.append(row)
            if len(pending) >= batch_size:
                flush()
        flush()
        return inserted

    def delete(self, item_id: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM knowledge_fts WHERE id = ?", (item_id,))
            self.connection.execute("DELETE FROM knowledge_chunks WHERE id = ?", (item_id,))

    def collection_stats(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            """
            SELECT collection_id, scope, COUNT(*),
                   SUM(CASE WHEN citation_policy='scholarly' THEN 1 ELSE 0 END)
            FROM knowledge_chunks WHERE review_status != 'superseded'
            GROUP BY collection_id, scope ORDER BY collection_id
            """
        ).fetchall()
        return [
            {
                "collection_id": row[0],
                "scope": row[1],
                "item_count": row[2],
                "citable_count": row[3],
            }
            for row in rows
        ]

    def rebuild(self) -> int:
        rows = self.connection.execute(
            """
            SELECT id, title, content FROM knowledge_chunks
            WHERE review_status != 'superseded'
            """
        ).fetchall()
        with self.connection:
            self.connection.execute("DELETE FROM knowledge_fts")
            self.connection.executemany(
                "INSERT INTO knowledge_fts(id,title,content) VALUES (?,?,?)",
                [(row[0], _fts_text(row[1]), _fts_text(row[2])) for row in rows],
            )
        return len(rows)

    def supersede(self, item_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE knowledge_chunks SET review_status='superseded' WHERE id=?", (item_id,)
            )
            self.connection.execute("DELETE FROM knowledge_fts WHERE id=?", (item_id,))

    def override_classification(self, item_id: str, new_type: str) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT content_type FROM knowledge_chunks WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            self.connection.execute(
                """
                INSERT INTO classification_history(item_id, previous_type, new_type)
                VALUES (?,?,?)
                """,
                (item_id, row[0], new_type),
            )
            self.connection.execute(
                "UPDATE knowledge_chunks SET content_type=? WHERE id=?", (new_type, item_id)
            )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        collection_id: str | None = None,
        project_id: str | None = None,
    ) -> list[SearchHit]:
        tokens = _query_tokens(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{token}"' for token in tokens)
        conditions = [
            "knowledge_fts MATCH ?",
            "k.review_status != 'superseded'",
            "(k.expires_at IS NULL OR k.expires_at > CURRENT_TIMESTAMP)",
        ]
        parameters: list[object] = [match]
        if collection_id:
            conditions.append("k.collection_id = ?")
            parameters.append(collection_id)
        if project_id:
            conditions.append("(k.project_id IS NULL OR k.project_id = ?)")
            parameters.append(project_id)
        else:
            conditions.append("k.project_id IS NULL")
        parameters.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT k.id, bm25(knowledge_fts), k.title, k.content, k.source_uri,
                   k.locator_json, k.trust_level, k.citation_policy, k.confidentiality,
                   k.scope, k.project_id, k.content_hash, k.expires_at
            FROM knowledge_fts JOIN knowledge_chunks k ON k.id = knowledge_fts.id
            WHERE {" AND ".join(conditions)}
            ORDER BY bm25(knowledge_fts) LIMIT ?
            """,
            parameters,
        ).fetchall()
        return [
            SearchHit(
                item_id=row[0],
                score=1.0 / (1.0 + abs(float(row[1]))),
                title=row[2],
                content=row[3],
                source_uri=row[4],
                locator=json.loads(row[5]),
                trust_level=row[6],
                citation_policy=row[7],
                confidentiality=row[8],
                scope=row[9],
                project_id=row[10],
                content_hash=row[11],
                expires_at=row[12],
            )
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()


class Embedder(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingModelManifest:
    model_id: str
    display_name: str
    dimension: int
    approximate_download_mb: int
    languages: tuple[str, ...]
    optional: bool


MODEL_MANIFESTS = {
    "intfloat/multilingual-e5-small": EmbeddingModelManifest(
        "intfloat/multilingual-e5-small", "multilingual-e5-small", 384, 450, ("zh", "en"), False
    ),
    "BAAI/bge-m3": EmbeddingModelManifest("BAAI/bge-m3", "BGE-M3", 1024, 2300, ("zh", "en"), True),
}


class EmbeddingModelStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def status(self, model_id: str) -> dict[str, object]:
        manifest = MODEL_MANIFESTS[model_id]
        path = self.root / model_id.replace("/", "--")
        return {
            "model_id": model_id,
            "display_name": manifest.display_name,
            "dimension": manifest.dimension,
            "approximate_download_mb": manifest.approximate_download_mb,
            "languages": manifest.languages,
            "optional": manifest.optional,
            "installed": (path / "installed.json").is_file(),
            "path": str(path),
        }

    def install(
        self,
        model_id: str,
        *,
        approved: bool,
        downloader: Callable[[str, Path], None],
    ) -> Path:
        if not approved:
            raise PermissionError("Embedding model download requires approval")
        manifest = MODEL_MANIFESTS[model_id]
        target = self.root / model_id.replace("/", "--")
        marker = target / "installed.json"
        if marker.is_file():
            return target
        staging = target.with_name(target.name + ".partial")
        staging.mkdir(parents=True, exist_ok=True)
        try:
            downloader(model_id, staging)
            (staging / "installed.json").write_text(
                json.dumps(
                    {
                        "model_id": model_id,
                        "dimension": manifest.dimension,
                        "approximate_download_mb": manifest.approximate_download_mb,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(staging, target)
        except BaseException:
            if staging.exists():
                import shutil

                shutil.rmtree(staging, ignore_errors=True)
            raise
        return target


class DeterministicTestEmbedder:
    model_id = "paperagent/test-multilingual-hash"

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        aliases = {"人工智能": "artificial intelligence", "论文": "paper", "检索": "retrieval"}
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.lower()
            for source, target in aliases.items():
                normalized = normalized.replace(source, target)
            vector = [0.0] * self.dimension
            grams = [normalized[index : index + 3] for index in range(max(len(normalized) - 2, 1))]
            for gram in grams:
                digest = hashlib.sha256(gram.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class LanceKnowledgeIndex:
    def __init__(self, path: Path, embedder: Embedder) -> None:
        import lancedb

        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.database = lancedb.connect(self.path)
        self.state_path = self.path / "index_state.json"

    def index(self, items: Sequence[KnowledgeItem], *, batch_size: int = 128) -> int:
        if not items:
            return 0
        table = None
        names = set(self.database.list_tables().tables)
        if "knowledge" in names:
            table = self.database.open_table("knowledge")
            schema_dimension = (
                len(table.to_arrow().column("vector")[0].as_py())
                if table.count_rows()
                else self.embedder.dimension
            )
            if schema_dimension != self.embedder.dimension:
                self.database.drop_table("knowledge")
                table = None
        indexed = 0
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            vectors = self.embedder.embed([item.content for item in batch])
            rows = [
                {
                    "id": str(item.id),
                    "vector": vector,
                    "title": item.title,
                    "content": item.content,
                    "source_uri": item.source_uri or "",
                    "locator_json": item.locator.model_dump_json(),
                    "trust_level": item.trust_level.value,
                    "citation_policy": item.citation_policy.value,
                    "confidentiality": item.confidentiality.value,
                    "scope": item.scope.value,
                    "project_id": item.project_id or "",
                    "content_hash": item.content_hash,
                    "expires_at": item.expires_at.isoformat() if item.expires_at else "",
                }
                for item, vector in zip(batch, vectors, strict=True)
            ]
            if table is None:
                table = self.database.create_table("knowledge", data=rows)
            else:
                ids = ",".join(f"'{row['id']}'" for row in rows)
                table.delete(f"id IN ({ids})")
                table.add(rows)
            indexed += len(batch)
            self.state_path.write_text(
                json.dumps(
                    {
                        "model_id": self.embedder.model_id,
                        "dimension": self.embedder.dimension,
                        "indexed": indexed,
                        "last_item_id": rows[-1]["id"],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return indexed

    def search(
        self, query: str, *, limit: int = 20, project_id: str | None = None
    ) -> list[SearchHit]:
        if "knowledge" not in set(self.database.list_tables().tables):
            return []
        table = self.database.open_table("knowledge")
        search = table.search(self.embedder.embed([query])[0]).limit(limit * 2)
        rows = search.to_list()
        hits: list[SearchHit] = []
        for row in rows:
            row_project = row["project_id"] or None
            if row_project and row_project != project_id:
                continue
            hits.append(
                SearchHit(
                    item_id=row["id"],
                    score=1.0 / (1.0 + float(row.get("_distance", 0))),
                    title=row["title"],
                    content=row["content"],
                    source_uri=row["source_uri"] or None,
                    locator=json.loads(row["locator_json"]),
                    trust_level=row["trust_level"],
                    citation_policy=row["citation_policy"],
                    confidentiality=row["confidentiality"],
                    scope=row["scope"],
                    project_id=row_project,
                    content_hash=row["content_hash"],
                    expires_at=row.get("expires_at") or None,
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def delete_many(self, item_ids: Sequence[str]) -> None:
        if not item_ids or "knowledge" not in set(self.database.list_tables().tables):
            return
        quoted = ",".join("'" + item_id.replace("'", "''") + "'" for item_id in item_ids)
        self.database.open_table("knowledge").delete(f"id IN ({quoted})")
