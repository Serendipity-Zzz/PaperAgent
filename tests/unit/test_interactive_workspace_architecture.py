from __future__ import annotations

import ast
from pathlib import Path


def test_workspace_contracts_do_not_depend_on_api_agents_or_providers() -> None:
    root = Path(__file__).resolve().parents[2]
    package = root / "backend" / "paperagent" / "workspace"
    forbidden = ("paperagent.api", "paperagent.agents", "paperagent.providers")
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not [item for item in imports if item.startswith(forbidden)]


def test_provider_contract_has_no_inline_secret_field() -> None:
    from paperagent.workspace.contracts import ProviderConfig

    names = set(ProviderConfig.model_fields)
    assert names.isdisjoint({"api_key", "secret", "password", "authorization"})
