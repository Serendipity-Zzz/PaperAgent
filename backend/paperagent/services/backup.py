from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    source_name: str
    database_file: str
    sha256: str
    size_bytes: int
    created_at: str
    reason: str = "manual"
    schema_version: int = 1


class BackupService:
    def __init__(self, backup_root: Path) -> None:
        self.root = backup_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, source: Path, *, reason: str = "manual") -> BackupManifest:
        source = source.resolve()
        backup_id = str(uuid4())
        target_dir = self.root / backup_id
        target_dir.mkdir(parents=True)
        target = target_dir / "database.db"
        with (
            closing(sqlite3.connect(source)) as source_db,
            closing(sqlite3.connect(target)) as target_db,
        ):
            source_db.backup(target_db)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = BackupManifest(
            backup_id=backup_id,
            source_name=source.name,
            database_file="database.db",
            sha256=digest,
            size_bytes=target.stat().st_size,
            created_at=datetime.now(UTC).isoformat(),
            reason=reason,
        )
        (target_dir / "manifest.json").write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    def create_daily(self, source: Path, *, keep: int = 7) -> BackupManifest:
        today = datetime.now(UTC).date().isoformat()
        for manifest in self.list_backups():
            if manifest.reason == "daily" and manifest.created_at.startswith(today):
                return manifest
        created = self.create(source, reason="daily")
        self.prune(keep=keep, reason="daily")
        return created

    def before_migration(self, source: Path) -> BackupManifest:
        return self.create(source, reason="pre-migration")

    def list_backups(self) -> list[BackupManifest]:
        manifests: list[BackupManifest] = []
        for path in self.root.glob("*/manifest.json"):
            try:
                manifests.append(BackupManifest(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(manifests, key=lambda item: item.created_at, reverse=True)

    def prune(self, *, keep: int = 7, reason: str | None = None) -> list[str]:
        candidates = [
            item for item in self.list_backups() if reason is None or item.reason == reason
        ]
        removed: list[str] = []
        for item in candidates[keep:]:
            directory = self._backup_dir(item.backup_id)
            self.verify(item.backup_id)
            shutil.rmtree(directory)
            removed.append(item.backup_id)
        return removed

    def export_project(self, project_root: Path, destination: Path) -> Path:
        project_root = project_root.resolve()
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_suffix(destination.suffix + ".tmp")
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(project_root.rglob("*")):
                if path.is_file() and path.name not in {"project.db-wal", "project.db-shm"}:
                    archive.write(path, path.relative_to(project_root).as_posix())
        os.replace(temp, destination)
        return destination

    def recovery_drill(self, backup_id: str, workspace: Path) -> dict[str, object]:
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        restored = workspace / f"{backup_id}.db"
        try:
            manifest = self.restore(backup_id, restored)
            with closing(sqlite3.connect(restored)) as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            return {
                "backup_id": backup_id,
                "status": "passed",
                "quick_check": quick_check,
                "sha256": manifest.sha256,
            }
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            return {"backup_id": backup_id, "status": "failed", "error": str(exc)}
        finally:
            restored.unlink(missing_ok=True)

    def verify(self, backup_id: str) -> BackupManifest:
        target_dir = self._backup_dir(backup_id)
        data = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest = BackupManifest(**data)
        database = target_dir / manifest.database_file
        digest = hashlib.sha256(database.read_bytes()).hexdigest()
        if digest != manifest.sha256 or database.stat().st_size != manifest.size_bytes:
            raise ValueError("Backup checksum mismatch")
        with closing(sqlite3.connect(database)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Backup database is corrupt")
        return manifest

    def restore(self, backup_id: str, destination: Path) -> BackupManifest:
        manifest = self.verify(backup_id)
        source = self._backup_dir(backup_id) / manifest.database_file
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self.create(destination)
        temp = destination.with_name(f".{destination.name}.{uuid4().hex}.restore")
        try:
            with (
                closing(sqlite3.connect(source)) as source_db,
                closing(sqlite3.connect(temp)) as destination_db,
            ):
                source_db.backup(destination_db)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return manifest

    def _backup_dir(self, backup_id: str) -> Path:
        if not backup_id or any(char not in "0123456789abcdef-" for char in backup_id.lower()):
            raise ValueError("Invalid backup id")
        path = (self.root / backup_id).resolve()
        if self.root not in path.parents:
            raise ValueError("Backup path escapes root")
        return path


@dataclass(frozen=True)
class PortableBackupManifest:
    schema_version: int
    created_at: str
    root_name: str
    files: dict[str, str]
    indexes_included: bool


class PortableDataBackup:
    """Portable file-first archive; databases and indexes are never the only content source."""

    def export(
        self,
        source_root: Path,
        destination: Path,
        *,
        include_indexes: bool = False,
    ) -> PortableBackupManifest:
        source_root = source_root.resolve()
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        selected: list[tuple[Path, str]] = []
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.name.endswith(("-wal", "-shm")):
                continue
            relative = path.relative_to(source_root).as_posix()
            if not include_indexes and any(part == "indexes" for part in Path(relative).parts):
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files[relative] = digest
            selected.append((path, relative))
        manifest = PortableBackupManifest(
            schema_version=1,
            created_at=datetime.now(UTC).isoformat(),
            root_name=source_root.name,
            files=files,
            indexes_included=include_indexes,
        )
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, relative in selected:
                    archive.write(path, f"data/{relative}")
                archive.writestr(
                    "manifest.json",
                    json.dumps(asdict(manifest), ensure_ascii=False, indent=2, sort_keys=True),
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest

    def restore(self, archive_path: Path, destination: Path) -> PortableBackupManifest:
        archive_path = archive_path.resolve()
        destination = destination.resolve()
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError("restore destination must be empty")
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            raw_manifest = json.loads(archive.read("manifest.json"))
            manifest = PortableBackupManifest(**raw_manifest)
            for relative, expected in manifest.files.items():
                self._safe_relative(relative)
                content = archive.read(f"data/{relative}")
                digest = hashlib.sha256(content).hexdigest()
                if digest != expected:
                    raise ValueError(f"portable backup checksum mismatch: {relative}")
                target = (destination / relative).resolve()
                if destination != target and destination not in target.parents:
                    raise ValueError("portable backup path escapes destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        return manifest

    @staticmethod
    def _safe_relative(value: str) -> None:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("portable backup contains unsafe path")
