from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

import yaml

from paperagent.memory.schemas import MemoryEntry, MemoryScope, MemoryWriteResult

FORBIDDEN_KINDS = {"api_key", "credential", "raw_project_source"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class CrossProcessFileLock(AbstractContextManager["CrossProcessFileLock"]):
    def __init__(self, path: Path, timeout: float = 10) -> None:
        self.path = path
        self.timeout = timeout
        self._stream: BinaryIO | None = None

    def __enter__(self) -> CrossProcessFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        stream = self.path.open("r+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock(stream)
                self._stream = stream
                return self
            except OSError as error:
                if time.monotonic() >= deadline:
                    stream.close()
                    raise TimeoutError(
                        f"timed out acquiring memory lock: {self.path}"
                    ) from error
                time.sleep(0.05)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        stream = self._stream
        if stream is not None:
            self._unlock(stream)
            stream.close()
        self._stream = None

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class FileMemoryRepository:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def write(self, entry: MemoryEntry, *, explicit: bool = False) -> MemoryWriteResult:
        if entry.kind in FORBIDDEN_KINDS:
            raise ValueError("sensitive credentials or raw project sources cannot become memory")
        if entry.scope is MemoryScope.GLOBAL and not explicit:
            raise PermissionError("global long-term memory requires explicit user intent")
        root = self._scope_root(entry.scope, entry.project_id)
        with CrossProcessFileLock(root / ".memory.lock"):
            existing = self._find_idempotency(root, entry.idempotency_key)
            if existing:
                return MemoryWriteResult(
                    entry=existing[0],
                    relative_path=self._relative(existing[1]),
                    manifest_path=self._relative(root / "MEMORY.md"),
                    created=False,
                )
            target = root / "topics" / entry.topic / f"{entry.memory_id}.md"
            self._atomic_write(target, self._serialize(entry).encode("utf-8"))
            self._append_daily(root, entry, target)
            self._write_manifest(root)
            return MemoryWriteResult(
                entry=entry,
                relative_path=self._relative(target),
                manifest_path=self._relative(root / "MEMORY.md"),
                created=True,
            )

    def list(
        self, scope: MemoryScope, project_id: str | None = None
    ) -> list[tuple[MemoryEntry, Path]]:
        root = self._scope_root(scope, project_id)
        entries: list[tuple[MemoryEntry, Path]] = []
        for path in sorted((root / "topics").glob("*/*.md")):
            entries.append((self.read(path), path))
        return sorted(entries, key=lambda pair: pair[0].created_at, reverse=True)

    def read(self, path: Path) -> MemoryEntry:
        resolved = path.resolve()
        if self.data_dir != resolved and self.data_dir not in resolved.parents:
            raise ValueError("memory path escapes data directory")
        payload, content = self._parse(resolved.read_text(encoding="utf-8"))
        payload["content"] = content
        return MemoryEntry.model_validate(payload)

    def update(self, entry: MemoryEntry) -> MemoryWriteResult:
        root = self._scope_root(entry.scope, entry.project_id)
        with CrossProcessFileLock(root / ".memory.lock"):
            matches = list((root / "topics").glob(f"*/{entry.memory_id}.md"))
            if len(matches) != 1:
                raise KeyError(str(entry.memory_id))
            current = self.read(matches[0])
            if current.memory_id != entry.memory_id:
                raise ValueError("memory identity changed during update")
            target = root / "topics" / entry.topic / matches[0].name
            self._atomic_write(target, self._serialize(entry).encode("utf-8"))
            if target != matches[0]:
                matches[0].unlink()
            self._write_manifest(root)
            return MemoryWriteResult(
                entry=entry,
                relative_path=self._relative(target),
                manifest_path=self._relative(root / "MEMORY.md"),
                created=False,
            )

    def write_cursor(
        self, project_id: str, thread_id: str, sequence: int
    ) -> Path:
        self._validate_id(project_id, "project_id")
        self._validate_id(thread_id, "thread_id")
        target = (
            self.data_dir
            / "projects"
            / project_id
            / "context"
            / f"{thread_id}-memory-cursor.json"
        )
        payload = json.dumps(
            {"thread_id": thread_id, "sequence": sequence},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self._atomic_write(target, payload)
        return target

    def append_conversation(
        self, project_id: str, thread_id: str, event: dict[str, object]
    ) -> Path:
        self._validate_id(project_id, "project_id")
        self._validate_id(thread_id, "thread_id")
        target = self.data_dir / "projects" / project_id / "conversations" / f"{thread_id}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with (
            CrossProcessFileLock(target.with_suffix(".lock")),
            target.open("a", encoding="utf-8", newline="\n") as stream,
        ):
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return target

    def write_project_state(self, project_id: str, name: str, value: object) -> Path:
        self._validate_id(project_id, "project_id")
        if name not in {"requirement", "task-graph", "document-ir"}:
            raise ValueError("unsupported project state document")
        target = self.data_dir / "projects" / project_id / "state" / f"{name}.json"
        data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self._atomic_write(target, data)
        return target

    def _scope_root(self, scope: MemoryScope, project_id: str | None) -> Path:
        if scope is MemoryScope.GLOBAL:
            if project_id:
                raise ValueError("global scope does not accept project_id")
            root = self.data_dir / "memory"
        else:
            if not project_id:
                raise ValueError("project scope requires project_id")
            self._validate_id(project_id, "project_id")
            root = self.data_dir / "projects" / project_id / "memory"
        (root / "topics").mkdir(parents=True, exist_ok=True)
        (root / "daily").mkdir(parents=True, exist_ok=True)
        (root / "archive").mkdir(parents=True, exist_ok=True)
        return root

    def _find_idempotency(
        self, root: Path, idempotency_key: str
    ) -> tuple[MemoryEntry, Path] | None:
        for path in (root / "topics").glob("*/*.md"):
            entry = self.read(path)
            if entry.idempotency_key == idempotency_key:
                return entry, path
        return None

    def _write_manifest(self, root: Path) -> None:
        entries = []
        for path in (root / "topics").glob("*/*.md"):
            entry = self.read(path)
            entries.append((entry, path))
        entries.sort(key=lambda pair: pair[0].created_at, reverse=True)
        lines = [
            "# PaperAgent Memory",
            "",
            "| Topic | Subject | Scope | Updated | Path |",
            "|---|---|---|---|---|",
        ]
        for entry, path in entries[:150]:
            relative = path.relative_to(root).as_posix()
            subject = entry.subject.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {entry.topic} | {subject} | {entry.scope.value} | "
                f"{entry.created_at.isoformat()} | [{entry.memory_id}]({relative}) |"
            )
        self._atomic_write(root / "MEMORY.md", ("\n".join(lines) + "\n").encode("utf-8"))

    def _append_daily(self, root: Path, entry: MemoryEntry, target: Path) -> None:
        daily = root / "daily" / f"{datetime.now(UTC).date().isoformat()}.md"
        relative = target.relative_to(root).as_posix()
        line = (
            f"- {entry.created_at.isoformat()} [{entry.subject}]({relative}) "
            f"`{entry.status.value}`\n"
        )
        with daily.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.data_dir).as_posix()

    @staticmethod
    def _serialize(entry: MemoryEntry) -> str:
        payload = entry.model_dump(mode="json", exclude={"content"})
        frontmatter = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\n{entry.content.rstrip()}\n"

    @staticmethod
    def _parse(text: str) -> tuple[dict[str, object], str]:
        if not text.startswith("---\n"):
            raise ValueError("memory document is missing YAML front matter")
        try:
            frontmatter, content = text[4:].split("\n---\n", 1)
        except ValueError as error:
            raise ValueError("memory document has invalid YAML front matter") from error
        raw_payload = yaml.safe_load(frontmatter)
        if not isinstance(raw_payload, dict) or not all(
            isinstance(key, str) for key in raw_payload
        ):
            raise ValueError("memory front matter must be an object")
        payload = {str(key): value for key, value in raw_payload.items()}
        return payload, content.strip()

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".paperagent-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"invalid {label}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
