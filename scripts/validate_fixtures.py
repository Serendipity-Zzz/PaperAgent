from __future__ import annotations

import hashlib
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    fixture_root = root / "tests" / "fixtures"
    manifest_path = fixture_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("Unsupported fixture manifest schema")
    for item in manifest.get("fixtures", []):
        required = {"path", "sha256", "license", "origin"}
        missing = required - item.keys()
        if missing:
            raise ValueError(f"Fixture fields missing: {sorted(missing)}")
        path = (fixture_root / item["path"]).resolve()
        if fixture_root.resolve() not in path.parents:
            raise ValueError(f"Fixture escapes root: {item['path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"Fixture hash mismatch: {item['path']}")
        if not item["license"].strip() or not item["origin"].strip():
            raise ValueError(f"Fixture provenance incomplete: {item['path']}")
    print(f"validated {len(manifest.get('fixtures', []))} fixture(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
