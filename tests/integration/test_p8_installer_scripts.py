from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows user installer")
def test_install_upgrade_rollback_and_uninstall_data_choices(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    release = tmp_path / "release"
    local = tmp_path / "LocalAppData"
    appdata = tmp_path / "AppData"
    target = local / "Programs" / "PaperAgent"
    data = local / "PaperAgent" / "data"
    release.mkdir()
    local.mkdir()
    appdata.mkdir()
    harmless_executable = shutil.which("where.exe")
    assert harmless_executable is not None
    shutil.copy2(harmless_executable, release / "PaperAgent.exe")
    for name in ("install-user.ps1", "uninstall-user.ps1", "rollback-user.ps1"):
        shutil.copy2(root / "scripts" / name, release / name)
    (release / "RELEASE.json").write_text(json.dumps({"version": "v1"}), encoding="utf-8")
    (release / "marker.txt").write_text("v1", encoding="utf-8")
    environment = {**os.environ, "LOCALAPPDATA": str(local), "APPDATA": str(appdata)}

    def powershell(
        script: str, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                *arguments,
            ],
            env=environment,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            check=False,
        )
        if check and result.returncode:
            pytest.fail(
                f"PowerShell failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
            )
        return result

    common = ("-Target", str(target), "-DataRoot", str(data))
    powershell(str(release / "install-user.ps1"), *common, "-NoShortcuts", "-SkipSmoke")
    assert (target / "marker.txt").read_text(encoding="utf-8").strip() == "v1"
    data.mkdir(parents=True, exist_ok=True)
    (data / "memory.md").write_text("before-upgrade", encoding="utf-8")
    (release / "marker.txt").write_text("v2", encoding="utf-8")
    powershell(str(release / "install-user.ps1"), *common, "-NoShortcuts", "-SkipSmoke")
    assert (target / "marker.txt").read_text(encoding="utf-8").strip() == "v2"
    (data / "memory.md").write_text("after-upgrade", encoding="utf-8")

    powershell(str(release / "rollback-user.ps1"), *common, "-SkipSmoke")
    assert (target / "marker.txt").read_text(encoding="utf-8").strip() == "v1"
    assert (data / "memory.md").read_text(encoding="utf-8").strip() == "before-upgrade"
    powershell(str(release / "uninstall-user.ps1"), *common, "-DataAction", "Preserve")
    assert not target.exists() and data.exists()
    refused = powershell(
        str(release / "uninstall-user.ps1"), *common, "-DataAction", "Delete", check=False
    )
    assert refused.returncode != 0 and data.exists()
    powershell(
        str(release / "uninstall-user.ps1"),
        *common,
        "-DataAction",
        "Delete",
        "-ConfirmDataDeletion",
    )
    assert not data.exists()
