from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock, Thread
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psutil
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from paperagent.services.resources import ProcessLedger


class EnvironmentRecord(BaseModel):
    environment_id: UUID = Field(default_factory=uuid4)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    manager: str = "uv"
    path: str
    lock_hash: str
    python_version: str
    cuda_version: str | None = None
    size_bytes: int = 0
    last_used: datetime = Field(default_factory=lambda: datetime.now(UTC))
    refcount: int = 0
    pinned: bool = False
    status: str = "ready"


class EnvironmentRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "environments.db", check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS environments("
            "fingerprint TEXT PRIMARY KEY,payload TEXT NOT NULL)"
        )
        self.lock = Lock()

    @staticmethod
    def fingerprint(lock_content: str, python_version: str, cuda_version: str | None) -> str:
        normalized = "\n".join(line.strip() for line in lock_content.splitlines() if line.strip())
        return hashlib.sha256(
            json.dumps(
                {
                    "lock": normalized,
                    "python": python_version,
                    "cuda": cuda_version,
                    "platform": platform.system(),
                    "machine": platform.machine(),
                    "policy": "managed-python-v1",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def get(self, fingerprint: str) -> EnvironmentRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM environments WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return EnvironmentRecord.model_validate_json(row[0]) if row else None

    def save(self, record: EnvironmentRecord) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO environments VALUES (?,?)",
                (record.fingerprint, record.model_dump_json()),
            )

    def records(self) -> list[EnvironmentRecord]:
        return [
            EnvironmentRecord.model_validate_json(row[0])
            for row in self.connection.execute("SELECT payload FROM environments").fetchall()
        ]

    def cleanup_candidates(self, soft_limit_bytes: int) -> list[EnvironmentRecord]:
        records = sorted(self.records(), key=lambda item: item.last_used)
        total = sum(item.size_bytes for item in records)
        candidates: list[EnvironmentRecord] = []
        for item in records:
            if total <= soft_limit_bytes:
                break
            if item.refcount == 0 and not item.pinned:
                candidates.append(item)
                total -= item.size_bytes
        return candidates

    def delete(self, fingerprint: str, *, approved: bool) -> None:
        record = self.get(fingerprint)
        if record is None:
            raise KeyError(fingerprint)
        if not approved:
            raise PermissionError("environment deletion requires confirmation")
        if record.refcount or record.pinned:
            raise PermissionError("in-use or pinned environment cannot be deleted")
        target = Path(record.path).resolve()
        venv_root = (self.root / "venvs").resolve()
        if venv_root not in target.parents:
            raise ValueError("environment path is outside the managed venv root")
        shutil.rmtree(target, ignore_errors=False)
        with self.connection:
            self.connection.execute("DELETE FROM environments WHERE fingerprint=?", (fingerprint,))


class EnvironmentManager:
    _creation_lock = Lock()

    def __init__(self, registry: EnvironmentRegistry, uv_path: Path, cache_dir: Path) -> None:
        if not uv_path.is_absolute() or not uv_path.is_file():
            raise ValueError("uv path must be an existing absolute file")
        self.registry = registry
        self.uv_path = uv_path
        self.cache_dir = cache_dir.resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def ensure(
        self,
        lock_content: str,
        *,
        python_version: str,
        cuda_version: str | None = None,
        approved_source_build: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> EnvironmentRecord:
        fingerprint = self.registry.fingerprint(lock_content, python_version, cuda_version)
        existing = self.registry.get(fingerprint)
        if existing and Path(existing.path).is_dir():
            existing.last_used = datetime.now(UTC)
            existing.refcount += 1
            self.registry.save(existing)
            return existing
        if cancelled and cancelled():
            raise InterruptedError("environment creation cancelled")
        if not lock_content.strip():
            raise ValueError("a resolved dependency lock is required")
        if (
            re.search(r"(?:git\+|https?://|\.tar\.gz|\.zip)", lock_content)
            and not approved_source_build
        ):
            raise PermissionError("source or URL dependency requires explicit approval")
        destination = self.registry.root / "venvs" / fingerprint
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._creation_lock.acquire()
        try:
            existing = self.registry.get(fingerprint)
            if existing and Path(existing.path).is_dir():
                existing.last_used = datetime.now(UTC)
                existing.refcount += 1
                self.registry.save(existing)
                return existing
            temporary_root = self.cache_dir / "tmp"
            temporary_root.mkdir(parents=True, exist_ok=True)
            child_environment = {
                name: os.environ[name]
                for name in ("PATH", "SYSTEMROOT", "WINDIR")
                if name in os.environ
            }
            child_environment.update(
                {
                    "UV_CACHE_DIR": str(self.cache_dir),
                    "TEMP": str(temporary_root),
                    "TMP": str(temporary_root),
                    "PYTHONUTF8": "1",
                }
            )
            result = subprocess.run(
                [str(self.uv_path), "venv", str(destination), "--python", python_version],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
                env=child_environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                raise RuntimeError(f"uv venv failed: {(result.stderr or '')[-1000:]}")
            record = EnvironmentRecord(
                fingerprint=fingerprint,
                path=str(destination),
                lock_hash=hashlib.sha256(lock_content.encode()).hexdigest(),
                python_version=python_version,
                cuda_version=cuda_version,
                refcount=1,
            )
            dependencies = [
                line
                for line in lock_content.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if dependencies:
                requirements = self.cache_dir / "locks" / f"{fingerprint}.txt"
                requirements.parent.mkdir(parents=True, exist_ok=True)
                requirements.write_text("\n".join(dependencies) + "\n", encoding="utf-8")
                python = destination / "Scripts" / "python.exe"
                install = subprocess.run(
                    [
                        str(self.uv_path),
                        "pip",
                        "install",
                        "--python",
                        str(python),
                        "--requirements",
                        str(requirements),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=900,
                    check=False,
                    env=child_environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if install.returncode != 0:
                    raise RuntimeError(
                        f"uv dependency sync failed: {(install.stderr or '')[-2000:]}"
                    )
            self.registry.save(record)
            return record
        finally:
            self._creation_lock.release()


class FeasibilityVerdict(StrEnum):
    RUNNABLE = "runnable"
    DEGRADED = "degraded"
    UNREASONABLE = "unreasonable"


class CapabilityReport(BaseModel):
    verdict: FeasibilityVerdict
    reasons: list[str]
    python_requirement: str | None = None
    cuda_requirement: str | None = None
    estimated_download_gb: float | None = None
    ram_gb: float
    gpu_name: str | None = None
    vram_gb: float | None = None
    disk_free_gb: float


class CapabilityAnalyzer:
    def analyze(
        self,
        repository: Path,
        *,
        gpu_name: str | None = None,
        vram_gb: float | None = None,
        disk_free_gb: float | None = None,
    ) -> CapabilityReport:
        files = [
            path
            for name in ("README.md", "pyproject.toml", "requirements.txt", "uv.lock", "Dockerfile")
            if (path := repository / name).is_file()
        ]
        text = "\n".join(path.read_text("utf-8", errors="replace") for path in files)
        cuda = (
            match.group(1)
            if (match := re.search(r"CUDA\s*(?:>=|==|:)?\s*([0-9.]+)", text, re.I))
            else None
        )
        python = (
            match.group(1)
            if (match := re.search(r"python\s*(?:>=|==|:)?\s*([0-9.]+)", text, re.I))
            else None
        )
        model_size = (
            float(match.group(1))
            if (match := re.search(r"([0-9.]+)\s*GB\s*(?:model|weights|download)", text, re.I))
            else None
        )
        ram = psutil.virtual_memory().total / 1024**3
        disk = (
            disk_free_gb
            if disk_free_gb is not None
            else shutil.disk_usage(repository).free / 1024**3
        )
        reasons: list[str] = []
        verdict = FeasibilityVerdict.RUNNABLE
        if cuda and not gpu_name:
            verdict = FeasibilityVerdict.UNREASONABLE
            reasons.append(f"Repository requires CUDA {cuda}, but no GPU is available")
        if model_size and vram_gb is not None and model_size > vram_gb:
            verdict = (
                FeasibilityVerdict.DEGRADED if vram_gb >= 8 else FeasibilityVerdict.UNREASONABLE
            )
            reasons.append(f"Estimated {model_size} GB model exceeds {vram_gb} GB VRAM")
        if model_size and model_size * 2 > disk:
            verdict = FeasibilityVerdict.UNREASONABLE
            reasons.append("Insufficient disk for model and installation workspace")
        if not reasons:
            reasons.append("Declared requirements fit the supplied hardware signals")
        return CapabilityReport(
            verdict=verdict,
            reasons=reasons,
            python_requirement=python,
            cuda_requirement=cuda,
            estimated_download_gb=model_size,
            ram_gb=ram,
            gpu_name=gpu_name,
            vram_gb=vram_gb,
            disk_free_gb=disk,
        )


class ExecutionApproval(BaseModel):
    command: list[str]
    working_directory: str
    writable_paths: list[str]
    network_allowed: bool
    timeout_seconds: int = Field(gt=0, le=86400)
    max_log_bytes: int = Field(default=2_000_000, gt=0)
    approved: bool = False


class ExecutionResult(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    status: str
    return_code: int | None
    stdout: str
    stderr: str
    truncated: bool = False


class ProcessExecutor:
    def __init__(
        self, ledger: ProcessLedger | None = None, *, owner_run_id: str | None = None
    ) -> None:
        self.ledger = ledger
        self.owner_run_id = owner_run_id

    def run(
        self,
        approval: ExecutionApproval,
        *,
        on_output: Callable[[str, str], None] | None = None,
    ) -> ExecutionResult:
        if not approval.approved:
            raise PermissionError("execution approval is required")
        cwd = Path(approval.working_directory).resolve()
        if not cwd.is_dir():
            raise ValueError("approved working directory does not exist")
        writable = [Path(item).resolve() for item in approval.writable_paths]
        if any(path != cwd and cwd not in path.parents for path in writable):
            raise ValueError("approved writable path is outside the run workspace")

        def snapshot() -> dict[Path, tuple[int, int]]:
            return {
                path.resolve(): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in cwd.rglob("*")
                if path.is_file()
            }

        before = snapshot()
        child_environment = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "WINDIR")
            if name in os.environ
        }
        child_environment.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "TEMP": str(cwd),
                "TMP": str(cwd),
            }
        )
        process = subprocess.Popen(
            approval.command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        process_id = (
            self.ledger.register(
                self.owner_run_id or f"standalone:{uuid4()}", process.pid, approval.command
            )
            if self.ledger is not None
            else None
        )
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        def drain(channel: str, parts: list[str]) -> None:
            stream = process.stdout if channel == "stdout" else process.stderr
            if stream is None:
                return
            try:
                for chunk in iter(stream.readline, ""):
                    parts.append(chunk)
                    if on_output is not None:
                        on_output(channel, chunk)
            finally:
                stream.close()

        readers = [
            Thread(target=drain, args=("stdout", stdout_parts), daemon=True),
            Thread(target=drain, args=("stderr", stderr_parts), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=approval.timeout_seconds)
            status = "completed" if process.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            process.wait(timeout=30)
            status = "timeout"
        for reader in readers:
            reader.join(timeout=5)
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        combined = len(stdout.encode()) + len(stderr.encode())
        truncated = combined > approval.max_log_bytes
        if truncated:
            limit = approval.max_log_bytes // 2
            stdout, stderr = stdout[-limit:], stderr[-limit:]
        after = snapshot()
        deleted = [path for path in before if path not in after]
        violations = [
            path
            for path, signature in after.items()
            if before.get(path) != signature
            and not any(path == allowed or allowed in path.parents for allowed in writable)
        ]
        if violations:
            status = "policy_violation"
            stderr += "\nWrites exceeded approval scope: " + ", ".join(
                str(path.relative_to(cwd)) for path in violations[:20]
            )
        if deleted:
            status = "policy_violation"
            stderr += "\nDeletion requires explicit approval: " + ", ".join(
                str(path.relative_to(cwd)) for path in deleted[:20]
            )
        if process_id is not None and self.ledger is not None:
            self.ledger.complete(process_id)
        return ExecutionResult(
            status=status,
            return_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
        )


class ExperimentResultPackage(BaseModel):
    run_id: UUID = Field(default_factory=uuid4)
    repository: str
    commit: str
    environment_fingerprint: str
    command: list[str]
    seed: int | None = None
    hardware: dict[str, object]
    metrics: dict[str, float] = Field(default_factory=dict)
    data_files: list[str] = Field(default_factory=list)
    figures: list[str] = Field(default_factory=list)
    source_artifact_id: str | None = None
    manifest_artifact_id: str | None = None
    environment_lock_artifact_id: str | None = None
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    data_artifact_ids: list[str] = Field(default_factory=list)
    figure_artifact_ids: list[str] = Field(default_factory=list)
    status: str = Field(pattern=r"^(completed|partial|failed|cancelled)$")
    simulated_data: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def eligible_as_experiment_evidence(self) -> bool:
        return self.status == "completed" and not self.simulated_data
