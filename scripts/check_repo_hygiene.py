from __future__ import annotations

import re
import subprocess
from pathlib import Path

FORBIDDEN_PATHS = (
    re.compile(r"(^|/)(\.env($|\.)|secrets?|credentials?)(/|$)", re.IGNORECASE),
    re.compile(r"\.(db|sqlite3?|safetensors|onnx|ckpt|pth|pt)$", re.IGNORECASE),
    re.compile(r"(^|/)(node_modules|\.venv)(/|$)", re.IGNORECASE),
    re.compile(r"^(models|runtimes|artifacts|logs)(/|$)", re.IGNORECASE),
)
SECRET_CONTENT = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|api[_-]?key\s*[:=]\s*['\"][^'\"]{8,})",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".json", ".toml", ".md", ".yml", ".yaml", ".ps1"}
SAFE_TEST_SECRET_MARKERS = (
    "fixture",
    "fake",
    "test-key",
    "not-a-real",
    "plaintext-secret",
)


def contains_possible_secret(content: str) -> bool:
    return any(
        not any(marker in match.group(0).casefold() for marker in SAFE_TEST_SECRET_MARKERS)
        for match in SECRET_CONTENT.finditer(content)
    )


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, check=True, capture_output=True, text=True
    )
    return [root / line for line in result.stdout.splitlines() if line]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors: list[str] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        if any(pattern.search(relative) for pattern in FORBIDDEN_PATHS):
            errors.append(f"forbidden tracked path: {relative}")
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            if contains_possible_secret(content):
                errors.append(f"possible secret in tracked file: {relative}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("repository hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
