from __future__ import annotations

import hashlib
import mimetypes
import os
import subprocess
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Protocol

from paperagent.ingestion.schemas import ImportReport, SourceDocument


class Parser(Protocol):
    name: str
    extensions: Collection[str]

    def parse(self, path: Path, digest: str) -> ImportReport: ...


class IngestionRegistry:
    def __init__(self) -> None:
        self.parsers: dict[str, Parser] = {}
        self.by_extension: dict[str, Parser] = {}
        self.imported_hashes: dict[str, SourceDocument] = {}

    def register(self, parser: Parser) -> None:
        if parser.name in self.parsers:
            raise ValueError(f"Parser already registered: {parser.name}")
        self.parsers[parser.name] = parser
        for extension in parser.extensions:
            self.by_extension[extension.lower()] = parser

    def import_file(
        self, path: Path, *, cancelled: Callable[[], bool] | None = None
    ) -> ImportReport:
        path = path.resolve()
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Source file is missing or empty")
        if cancelled and cancelled():
            source = SourceDocument(
                name=path.name,
                media_type="application/octet-stream",
                sha256="0" * 64,
                parser="cancelled",
            )
            return ImportReport(source=source, cancelled=True)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in self.imported_hashes:
            existing = self.imported_hashes[digest]
            return ImportReport(source=existing, duplicate_of=existing.id)
        parser = self.by_extension.get(path.suffix.lower())
        if parser is None:
            raise ValueError(f"No parser for {path.suffix or 'unknown format'}")
        report = parser.parse(path, digest)
        guessed, _ = mimetypes.guess_type(path.name)
        if guessed and report.source.media_type != guessed:
            report.warnings.append(
                f"Extension MIME {guessed} differs from detected {report.source.media_type}"
            )
        self.imported_hashes[digest] = report.source
        return report

    def import_directory(
        self,
        root: Path,
        *,
        cancelled: Callable[[], bool] | None = None,
        max_files: int = 10_000,
    ) -> list[ImportReport]:
        root = root.resolve()
        if not root.is_dir():
            raise ValueError("Source directory does not exist")
        excluded = {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}
        reports: list[ImportReport] = []
        commit_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        commit = commit_result.stdout.strip() if commit_result.returncode == 0 else None
        for current, directories, files in os.walk(root):
            directories[:] = [item for item in directories if item not in excluded]
            for name in files:
                if cancelled and cancelled():
                    return reports
                path = Path(current) / name
                if path.suffix.lower() not in self.by_extension:
                    continue
                report = self.import_file(path)
                report.source.metadata["relative_path"] = path.relative_to(root).as_posix()
                if commit:
                    report.source.metadata["git_commit"] = commit
                reports.append(report)
                if len(reports) >= max_files:
                    raise ValueError("Directory import exceeds file limit")
        return reports
