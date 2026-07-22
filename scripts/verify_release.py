from __future__ import annotations

import re
import sys
from pathlib import Path

SECRET = re.compile(r"(?i)(?:sk-[a-z0-9]{16,}|api[_-]?key\s*[:=]\s*['\"]?[a-z0-9_-]{16,})")
FORBIDDEN_NAMES = {".env", ".pytest_cache", "tests", "fixtures", "paperagent-data"}
TEXT_SUFFIXES = {".json", ".md", ".txt", ".ps1", ".yaml", ".yml", ".toml", ".ini"}


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise SystemExit(f"release directory does not exist: {root}")
    findings: list[str] = []
    for path in root.rglob("*"):
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append(f"forbidden release entry: {path.relative_to(root)}")
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if SECRET.search(text):
            findings.append(f"possible plaintext secret: {path.relative_to(root)}")
    required = ["PaperAgent.exe", "RELEASE.json", "LICENSE", "THIRD_PARTY_NOTICES.md"]
    findings.extend(
        f"missing required release file: {name}"
        for name in required
        if not (root / name).exists()
    )
    if findings:
        print("\n".join(findings))
        return 1
    print("release payload verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
