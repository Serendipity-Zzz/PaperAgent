from __future__ import annotations

from typing import Protocol

from paperagent.tools.contracts import ToolCall, ToolResult, ToolSpec


class ToolHooks(Protocol):
    async def before(self, call: ToolCall, spec: ToolSpec) -> None: ...

    async def after(self, call: ToolCall, spec: ToolSpec, result: ToolResult) -> None: ...


class NoopToolHooks:
    async def before(self, call: ToolCall, spec: ToolSpec) -> None:
        del call, spec

    async def after(self, call: ToolCall, spec: ToolSpec, result: ToolResult) -> None:
        del call, spec, result
