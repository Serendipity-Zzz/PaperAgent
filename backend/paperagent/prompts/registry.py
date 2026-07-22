from __future__ import annotations

import re

from paperagent.prompts.models import PromptModule, PromptSelectionContext


class PromptModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[tuple[str, str], PromptModule] = {}

    def register(self, module: PromptModule) -> PromptModule:
        key = (module.module_id, module.version)
        existing = self._modules.get(key)
        if existing is not None and existing != module:
            raise ValueError(f"prompt module version conflict: {module.version_ref()}")
        self._modules[key] = module
        return module

    def select(self, context: PromptSelectionContext) -> list[PromptModule]:
        latest: dict[str, PromptModule] = {}
        for module in self._modules.values():
            current = latest.get(module.module_id)
            if current is None or self._version_key(module.version) > self._version_key(
                current.version
            ):
                latest[module.module_id] = module
        return sorted(
            (module for module in latest.values() if module.applies(context)),
            key=lambda module: (module.priority, module.module_id),
        )

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int, int, str]:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+]([A-Za-z0-9.-]+))?", version)
        if not match:
            raise ValueError(f"invalid prompt module version: {version}")
        major, minor, patch = (int(match.group(index)) for index in range(1, 4))
        suffix = match.group(4) or ""
        return major, minor, patch, int(not suffix), suffix
