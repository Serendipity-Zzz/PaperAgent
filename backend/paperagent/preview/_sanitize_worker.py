from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import bleach
from bs4 import BeautifulSoup

ALLOWED_TAGS = frozenset(
    {
        "p",
        "div",
        "span",
        "h1",
        "h2",
        "h3",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "strong",
        "em",
        "code",
        "pre",
        "svg",
        "path",
        "circle",
        "rect",
        "line",
        "text",
    }
)


def sanitize(path: Path) -> dict[str, object]:
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Active-content preview exceeds the 20 MiB isolation limit")
    raw = path.read_text(encoding="utf-8", errors="replace")
    sanitized = bleach.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes={
            "*": ["class"],
            "svg": ["viewBox", "width", "height", "role", "aria-label"],
            "path": ["d", "fill", "stroke", "stroke-width"],
            "circle": ["cx", "cy", "r", "fill", "stroke", "stroke-width"],
            "rect": ["x", "y", "width", "height", "rx", "fill", "stroke"],
            "line": ["x1", "y1", "x2", "y2", "stroke", "stroke-width"],
            "text": ["x", "y", "fill", "font-size", "text-anchor"],
        },
        protocols={"https"},
        strip=True,
    )
    sanitized = re.sub(r"(?i)(javascript:|data:|https?://[^\s'\"]+)", "", sanitized)
    text = BeautifulSoup(sanitized, "html.parser").get_text(" ", strip=True)
    return {"html": sanitized, "text": text, "changed": sanitized != raw}


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    result = sanitize(Path(sys.argv[1]))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
