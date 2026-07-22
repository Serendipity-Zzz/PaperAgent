from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_release_manifest_tracks_commit_locks_and_noncommercial_license(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "RELEASE.json"
    subprocess.run(
        [sys.executable, str(root / "scripts" / "generate_release_manifest.py"), str(output)],
        cwd=root,
        check=True,
    )
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert len(manifest["commit"]) == 40
    assert manifest["locks"]["uv.lock"]
    assert manifest["license"] == "PolyForm-Noncommercial-1.0.0"
    assert manifest["mock_configuration_bundled"] is False


def test_release_payload_verifier_rejects_plaintext_secret(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    verifier = root / "scripts" / "verify_release.py"
    for name in ("PaperAgent.exe", "RELEASE.json", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        (tmp_path / name).write_text("safe", encoding="utf-8")
    passed = subprocess.run([sys.executable, str(verifier), str(tmp_path)], check=False)
    assert passed.returncode == 0
    fake_secret = "sk-" + "abcdefghijklmnopqrstuv"
    (tmp_path / "config.json").write_text(
        json.dumps({"api_key": fake_secret}), encoding="utf-8"
    )
    failed = subprocess.run([sys.executable, str(verifier), str(tmp_path)], check=False)
    assert failed.returncode == 1
