from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel

from paperagent.skills.registry import tree_checksum
from paperagent.skills.security import SecurityReport, SkillSecurityScanner


class NatureSnapshotLock(BaseModel):
    repository: str
    commit: str
    license: str
    install_mode: str
    required_paths: list[str]


class NatureReview(BaseModel):
    commit: str
    checksum: str
    complete: bool
    security: SecurityReport


class NatureSkillsInstaller:
    def __init__(self, lock_file: Path, scanner: SkillSecurityScanner | None = None) -> None:
        self.lock = NatureSnapshotLock.model_validate_json(lock_file.read_text("utf-8"))
        self.scanner = scanner or SkillSecurityScanner()

    def stage(self, destination: Path, *, download_approved: bool) -> Path:
        if not download_approved:
            raise PermissionError("Nature Skills download requires approval")
        if destination.exists():
            raise FileExistsError(destination)
        result = subprocess.run(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                self.lock.repository,
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f"Nature Skills clone failed: {result.stderr[-1000:]}")
        checkout = subprocess.run(
            ["git", "-C", str(destination), "checkout", "--detach", self.lock.commit],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if checkout.returncode != 0:
            raise RuntimeError(f"Pinned Nature Skills checkout failed: {checkout.stderr[-1000:]}")
        return destination

    def review(self, checkout: Path) -> NatureReview:
        complete = all((checkout / relative).exists() for relative in self.lock.required_paths)
        return NatureReview(
            commit=self.lock.commit,
            checksum=tree_checksum(checkout),
            complete=complete,
            security=self.scanner.scan(
                checkout,
                requested_permissions=[
                    "network.image_api",
                    "process.python_or_r",
                    "filesystem.output",
                ],
            ),
        )

    def install(
        self, checkout: Path, destination_root: Path, review: NatureReview, *, approved: bool
    ) -> Path:
        if not approved:
            raise PermissionError("Nature Skills installation requires approval")
        if not review.complete or review.security.blocked:
            raise PermissionError(
                "Nature Skills snapshot is incomplete or blocked by security review"
            )
        if tree_checksum(checkout) != review.checksum:
            raise ValueError("Nature Skills changed after review")
        destination = destination_root / "nature-skills" / self.lock.commit
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(checkout, destination, ignore=shutil.ignore_patterns(".git"))
        (destination / "PAPERAGENT-INSTALL.json").write_text(
            json.dumps(
                {
                    "commit": self.lock.commit,
                    "checksum": review.checksum,
                    "license": self.lock.license,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return destination
