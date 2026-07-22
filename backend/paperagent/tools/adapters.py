from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from pydantic import JsonValue, TypeAdapter

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
ToolFunction = Callable[[dict[str, JsonValue]], JsonValue | Awaitable[JsonValue]]


class CallableToolAdapter:
    """Typed bridge for existing deterministic services; policy remains in the executor."""

    def __init__(self, function: ToolFunction) -> None:
        self.function = function

    async def invoke(self, arguments: dict[str, JsonValue]) -> JsonValue:
        result = self.function(arguments)
        if inspect.isawaitable(result):
            result = await result
        return _JSON.validate_python(result)
