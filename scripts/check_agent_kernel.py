from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    app = (root / "backend/paperagent/api/app.py").read_text(encoding="utf-8")
    ui = (root / "frontend/src/App.tsx").read_text(encoding="utf-8")
    if 'mock: "http://' in ui or "mock-configured-model" in ui:
        violations.append("release UI contains a mock Provider preset")
    if "后续阶段将在这里" in ui or "static Agent plan" in ui:
        violations.append("release UI contains an unconnected placeholder control")
    if "from paperagent.providers.mock import MockProvider" in app and (
        'app_settings.environment == "test"' not in app
    ):
        violations.append("production API imports MockProvider outside a test-only guard")
    for path in (root / "backend/paperagent").rglob("*.py"):
        if path.name == "mock.py" or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8")
        if "TODO: fake" in content or "FIXED_DEMO_RESULT" in content:
            violations.append(f"formal source contains fake-result marker: {path}")
    if violations:
        print("Agent kernel anti-fake gate failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("Agent kernel anti-fake gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
