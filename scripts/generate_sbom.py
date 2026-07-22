from __future__ import annotations

import hashlib
import json
from importlib import metadata
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    components: list[dict[str, object]] = []
    for distribution in sorted(
        metadata.distributions(), key=lambda item: item.metadata["Name"].lower()
    ):
        name = distribution.metadata["Name"]
        version = distribution.version
        license_name = distribution.metadata.get("License") or "NOASSERTION"
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{version}",
                "licenses": [{"license": {"name": license_name[:200]}}],
            }
        )
    lock = root / "uv.lock"
    serial = hashlib.sha256(lock.read_bytes()).hexdigest()
    serial_number = (
        f"urn:uuid:{serial[:8]}-{serial[8:12]}-{serial[12:16]}-"
        f"{serial[16:20]}-{serial[20:32]}"
    )
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial_number,
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "paperagent", "version": "0.1.0"}
        },
        "components": components,
    }
    destination = root / "docs" / "release" / "sbom.cdx.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {destination} with {len(components)} components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
