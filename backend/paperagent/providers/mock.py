from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from paperagent.providers.base import (
    ChatRequest,
    ChatResponse,
    ProviderConfig,
    ProviderError,
    Usage,
)


class MockProvider:
    def __init__(
        self,
        config: ProviderConfig,
        *,
        content: str = "mock response",
        chunks: list[str] | None = None,
        error: ProviderError | None = None,
    ) -> None:
        self.config = config
        self.content = content
        self.chunks = chunks or [content]
        self.error = error
        self.calls = 0

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        delay = self.config.extra.get("delay_ms", 0)
        if isinstance(delay, int) and delay > 0:
            await asyncio.sleep(delay / 1_000)
        if self.error:
            raise self.error
        return ChatResponse(
            content=self.content,
            model=self.config.model,
            usage=Usage(
                input_tokens=sum(len(item.content) for item in request.messages),
                output_tokens=len(self.content),
            ),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        self.calls += 1
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk
