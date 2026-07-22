from __future__ import annotations

import ast
from pathlib import Path


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    package = root / "backend" / "paperagent"
    errors: list[str] = []
    forbidden_by_layer = {
        "schemas": ("paperagent.providers", "paperagent.agents", "paperagent.api"),
        "core": ("paperagent.frontend",),
    }
    for layer, forbidden_prefixes in forbidden_by_layer.items():
        layer_root = package / layer
        for path in layer_root.rglob("*.py"):
            for module in imported_modules(path):
                if module.startswith(forbidden_prefixes):
                    relative = path.relative_to(root)
                    errors.append(f"{relative} imports forbidden dependency {module}")
    if errors:
        raise SystemExit("\n".join(errors))
    print("architecture dependency check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
