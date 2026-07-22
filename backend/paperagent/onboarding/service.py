from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, ClassVar
from uuid import uuid4


@dataclass(frozen=True)
class ToolStatus:
    name: str
    available: bool
    path: str | None
    purpose: str
    optional_size: str | None = None


@dataclass(frozen=True)
class DependencyInstallPlan:
    tool: str
    method: str
    source: str
    destination: str | None
    estimated_bytes: int
    requires_confirmation: bool = True


@dataclass(frozen=True)
class DependencyInstallJob:
    job_id: str
    tool: str
    status: str
    destination: str | None
    source: str
    pid: int | None
    log_file: str
    started_at: str
    finished_at: str | None = None
    exit_code: int | None = None


class FirstRunService:
    TOOLS: ClassVar[dict[str, tuple[str, str | None]]] = {
        "uv": ("隔离并复用实验环境", "约 20 MB"),
        "typst": ("快速 PDF 排版", "约 50 MB"),
        "pandoc": ("文档格式转换", "约 200 MB"),
        "xelatex": ("复杂学术 LaTeX 排版 (TeX Live)", "完整安装约 5 GB"),
        "winword": ("高保真 Word 转 PDF", None),
        "MpCmdRun.exe": ("Windows Defender 安全扫描", None),
        "nvidia-smi": ("NVIDIA GPU/显存检测", None),
        "node": ("可选代码实验", "约 100 MB"),
        "cmake": ("可选原生代码实验", "约 200 MB"),
        "Rscript": ("可选 R 实验", None),
    }
    WINGET_PACKAGES: ClassVar[dict[str, tuple[str, int]]] = {
        "uv": ("astral-sh.uv", 100 * 1024**2),
        "typst": ("Typst.Typst", 200 * 1024**2),
        "pandoc": ("JohnMacFarlane.Pandoc", 500 * 1024**2),
    }
    TEXLIVE_SOURCE: ClassVar[str] = (
        "https://mirror.ctan.org/systems/texlive/tlnet/install-tl-windows.exe"
    )
    TEXLIVE_ESTIMATED_BYTES: ClassVar[int] = 7 * 1024**3

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.marker = self.data_dir / "global" / "first-run.json"
        self.install_root = self.data_dir / "runtimes" / "install-jobs"

    def detect(self) -> list[ToolStatus]:
        results: list[ToolStatus] = []
        for command, (purpose, size) in self.TOOLS.items():
            path = self._detect_command(command)
            results.append(ToolStatus(command, path is not None, path, purpose, size))
        return results

    def _detect_command(self, command: str) -> str | None:
        from_path = shutil.which(command)
        if from_path:
            return from_path
        if command != "uv":
            return None
        candidates = [
            os.getenv("PAPERAGENT_UV_PATH"),
            str(self.data_dir / "runtimes" / "uv" / "uv.exe"),
            r"E:\App\uv\current\uv.exe",
            str(Path(os.getenv("LOCALAPPDATA", "")) / "uv" / "uv.exe"),
            str(Path(os.getenv("USERPROFILE", "")) / ".local" / "bin" / "uv.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate).resolve())
        return None

    def disk(self, path: Path | None = None) -> dict[str, int | bool]:
        target = (path or self.data_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(target)
        probe = target / ".write-probe"
        writable = True
        try:
            probe.write_text("ok", encoding="utf-8")
        except OSError:
            writable = False
        finally:
            probe.unlink(missing_ok=True)
        return {"total": usage.total, "free": usage.free, "writable": writable}

    def gpu(self) -> dict[str, object]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            return {"available": False}
        try:
            output = subprocess.run(
                [
                    executable,
                    "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).stdout.strip()
            return {"available": True, "devices": output.splitlines()}
        except (OSError, subprocess.SubprocessError):
            return {"available": False, "error": "检测命令不可用"}

    def complete(
        self, *, privacy_mode: str, providers_configured: bool, skipped: list[str]
    ) -> dict[str, object]:
        if privacy_mode not in {"standard", "privacy-controlled", "offline"}:
            raise ValueError("Invalid privacy mode")
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        value: dict[str, object] = {
            "schema_version": 1,
            "privacy_mode": privacy_mode,
            "providers_configured": providers_configured,
            "skipped": skipped,
        }
        self.marker.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return value

    def status(self) -> dict[str, object]:
        if not self.marker.exists():
            return {"completed": False}
        return {"completed": True, **json.loads(self.marker.read_text(encoding="utf-8"))}

    def install_plan(self, tool: str, destination: Path | None = None) -> DependencyInstallPlan:
        resolved = self._safe_destination(destination) if destination else None
        if tool in self.WINGET_PACKAGES:
            package, estimated = self.WINGET_PACKAGES[tool]
            return DependencyInstallPlan(
                tool=tool,
                method="winget",
                source=package,
                destination=str(resolved) if resolved else None,
                estimated_bytes=estimated,
            )
        if tool == "xelatex":
            texlive = resolved or (self.data_dir / "runtimes" / "texlive").resolve()
            self._safe_destination(texlive)
            return DependencyInstallPlan(
                tool=tool,
                method="texlive-official",
                source=self.TEXLIVE_SOURCE,
                destination=str(texlive),
                estimated_bytes=self.TEXLIVE_ESTIMATED_BYTES,
            )
        raise ValueError(f"Tool is not managed by PaperAgent: {tool}")

    def start_install(
        self, tool: str, *, destination: Path | None = None, confirmed: bool = False
    ) -> DependencyInstallJob:
        plan = self.install_plan(tool, destination)
        if not confirmed:
            raise PermissionError("Dependency installation requires explicit user confirmation")
        disk_target = Path(plan.destination) if plan.destination else self.data_dir
        disk = self.disk(disk_target)
        if not disk["writable"]:
            raise OSError("Installation destination is not writable")
        if int(disk["free"]) < plan.estimated_bytes:
            raise OSError("Insufficient disk space for the selected dependency")
        self.install_root.mkdir(parents=True, exist_ok=True)
        job_id = str(uuid4())
        job_dir = self.install_root / job_id
        job_dir.mkdir(parents=True)
        log_file = job_dir / "install.log"
        command = self._install_command(plan, job_dir)
        log_stream = log_file.open("ab")
        try:
            process = subprocess.Popen(
                command,
                cwd=job_dir,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            log_stream.close()
            raise
        job = DependencyInstallJob(
            job_id=job_id,
            tool=tool,
            status="running",
            destination=plan.destination,
            source=plan.source,
            pid=process.pid,
            log_file=str(log_file),
            started_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        self._write_job(job)
        threading.Thread(
            target=self._watch_install,
            args=(job, process, log_stream),
            name=f"install-{tool}-{job_id[:8]}",
            daemon=True,
        ).start()
        return job

    def install_status(self, job_id: str) -> DependencyInstallJob:
        job = self._read_job(job_id)
        if job.status != "running" or (job.pid is not None and self._pid_running(job.pid)):
            return job
        available = self._installed_at(job.tool, job.destination)
        recovered = DependencyInstallJob(
            **{
                **job.__dict__,
                "status": "completed" if available else "interrupted",
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
        self._write_job(recovered)
        return recovered

    def _install_command(self, plan: DependencyInstallPlan, job_dir: Path) -> list[str]:
        if plan.method == "winget":
            winget = shutil.which("winget")
            if not winget:
                raise FileNotFoundError("winget is unavailable on this Windows installation")
            command = [
                winget,
                "install",
                "--exact",
                "--id",
                plan.source,
                "--scope",
                "user",
                "--silent",
                "--disable-interactivity",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ]
            if plan.destination:
                command.extend(["--location", plan.destination])
            return command
        if os.name != "nt":
            raise OSError("The managed TeX Live installer currently supports Windows only")
        installer = job_dir / "install-tl-windows.exe"
        temporary = installer.with_suffix(".part")
        urllib.request.urlretrieve(plan.source, temporary)
        os.replace(temporary, installer)
        destination = Path(plan.destination or self.data_dir / "runtimes" / "texlive")
        profile = job_dir / "texlive.profile"
        profile.write_text(self._texlive_profile(destination), encoding="utf-8")
        return [str(installer), "--profile", str(profile), "--no-gui", "--non-admin"]

    @staticmethod
    def _texlive_profile(destination: Path) -> str:
        root = destination.as_posix()
        return "\n".join(
            [
                "selected_scheme scheme-full",
                f"TEXDIR {root}",
                f"TEXMFCONFIG {root}/texmf-config",
                f"TEXMFVAR {root}/texmf-var",
                f"TEXMFHOME {root}/texmf-home",
                f"TEXMFLOCAL {root}/texmf-local",
                f"TEXMFSYSCONFIG {root}/texmf-config",
                f"TEXMFSYSVAR {root}/texmf-var",
                "instopt_portable 1",
                "tlpdbopt_install_docfiles 1",
                "tlpdbopt_install_srcfiles 0",
                "tlpdbopt_create_formats 1",
                "",
            ]
        )

    def _watch_install(
        self,
        job: DependencyInstallJob,
        process: subprocess.Popen[bytes],
        log_stream: BinaryIO,
    ) -> None:
        exit_code = process.wait()
        log_stream.close()
        completed = exit_code == 0 and self._installed_at(job.tool, job.destination)
        finished = DependencyInstallJob(
            **{
                **job.__dict__,
                "status": "completed" if completed else "failed",
                "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "exit_code": exit_code,
            }
        )
        self._write_job(finished)

    def _installed_at(self, tool: str, destination: str | None) -> bool:
        if shutil.which(tool):
            return True
        if tool == "xelatex" and destination:
            return (Path(destination) / "bin" / "windows" / "xelatex.exe").exists()
        return False

    def _write_job(self, job: DependencyInstallJob) -> None:
        path = self.install_root / job.job_id / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _read_job(self, job_id: str) -> DependencyInstallJob:
        if not job_id or any(char not in "0123456789abcdef-" for char in job_id.lower()):
            raise ValueError("Invalid install job id")
        path = (self.install_root / job_id / "job.json").resolve()
        if self.install_root.resolve() not in path.parents:
            raise ValueError("Install job path escapes root")
        if not path.exists():
            raise KeyError(job_id)
        return DependencyInstallJob(**json.loads(path.read_text(encoding="utf-8")))

    def _safe_destination(self, destination: Path) -> Path:
        resolved = destination.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("Installation destination cannot be a drive root")
        protected = [
            Path(value).resolve()
            for name in ("WINDIR", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)")
            if (value := os.getenv(name))
        ]
        if any(resolved == root or root in resolved.parents for root in protected):
            raise ValueError("Installation destination is a protected system directory")
        return resolved

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
