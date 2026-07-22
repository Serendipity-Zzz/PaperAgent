from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "dist" / "RELEASE.json"
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8"
    ).strip()
    manifest = {
        "schema_version": 1,
        "name": metadata["project"]["name"],
        "version": metadata["project"]["version"],
        "commit": commit,
        "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "license": "PolyForm-Noncommercial-1.0.0",
        "commercial_use": "requires separate written authorization",
        "runtime_data_bundled": False,
        "mock_configuration_bundled": False,
        "locks": {
            "uv.lock": sha256(root / "uv.lock"),
            "frontend/package-lock.json": sha256(root / "frontend" / "package-lock.json"),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
