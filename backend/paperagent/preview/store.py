from __future__ import annotations

import sqlite3
from pathlib import Path

from paperagent.preview.schemas import Annotation, PreviewArtifact, PreviewPart


class PreviewStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS preview_artifacts(
                id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE,
                source_file_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS preview_parts(
                artifact_id TEXT NOT NULL, part_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(artifact_id, part_index)
            );
            CREATE TABLE IF NOT EXISTS annotations(
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
                source_file_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )

    def find(self, cache_key: str) -> PreviewArtifact | None:
        row = self.connection.execute(
            "SELECT payload_json FROM preview_artifacts WHERE cache_key=?", (cache_key,)
        ).fetchone()
        return PreviewArtifact.model_validate_json(row[0]) if row else None

    def get(self, artifact_id: str) -> PreviewArtifact | None:
        row = self.connection.execute(
            "SELECT payload_json FROM preview_artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        return PreviewArtifact.model_validate_json(row[0]) if row else None

    def save(self, artifact: PreviewArtifact, parts: list[PreviewPart] | None = None) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO preview_artifacts(
                    id,cache_key,source_file_id,source_hash,payload_json,updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    str(artifact.id),
                    artifact.cache_key,
                    artifact.source_file_id,
                    artifact.source_hash,
                    artifact.model_dump_json(),
                    artifact.updated_at.isoformat(),
                ),
            )
            if parts is not None:
                self.connection.execute(
                    "DELETE FROM preview_parts WHERE artifact_id=?", (str(artifact.id),)
                )
                self.connection.executemany(
                    "INSERT INTO preview_parts(artifact_id,part_index,payload_json) VALUES (?,?,?)",
                    [(str(artifact.id), part.index, part.model_dump_json()) for part in parts],
                )

    def parts(self, artifact_id: str, *, offset: int = 0, limit: int = 50) -> list[PreviewPart]:
        rows = self.connection.execute(
            """
            SELECT payload_json FROM preview_parts WHERE artifact_id=? AND part_index>=?
            ORDER BY part_index LIMIT ?
            """,
            (artifact_id, offset, limit),
        ).fetchall()
        return [PreviewPart.model_validate_json(row[0]) for row in rows]

    def annotate(self, annotation: Annotation) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO annotations(
                    id,artifact_id,source_file_id,source_hash,payload_json
                ) VALUES (?,?,?,?,?)
                """,
                (
                    str(annotation.id),
                    str(annotation.artifact_id),
                    annotation.anchor.source_file_id,
                    annotation.anchor.source_hash,
                    annotation.model_dump_json(),
                ),
            )

    def annotations(self, source_file_id: str) -> list[Annotation]:
        rows = self.connection.execute(
            "SELECT payload_json FROM annotations WHERE source_file_id=?", (source_file_id,)
        ).fetchall()
        return [Annotation.model_validate_json(row[0]) for row in rows]

    def close(self) -> None:
        self.connection.close()

    def clear_cache(self) -> int:
        with self.connection:
            count = self.connection.execute("SELECT COUNT(*) FROM preview_artifacts").fetchone()[0]
            self.connection.execute("DELETE FROM preview_parts")
            self.connection.execute("DELETE FROM preview_artifacts")
        return int(count)
