from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum

from paperagent.providers import ChatRequest, ChatResponse, ModelProvider


class PrivacyMode(StrEnum):
    STANDARD = "standard"
    CONTROLLED = "privacy-controlled"
    OFFLINE = "offline"


@dataclass(frozen=True)
class OutboundPreview:
    provider_id: str
    purpose: str
    content: str
    redactions: int
    allowed: bool


class PrivacyPolicy:
    def __init__(self, mode: PrivacyMode, allowlist: set[str] | None = None) -> None:
        self.mode = mode
        self.allowlist = allowlist or set()

    def preview(self, provider_id: str, purpose: str, content: str) -> OutboundPreview:
        if self.mode is PrivacyMode.OFFLINE:
            return OutboundPreview(provider_id, purpose, "", 0, False)
        if self.allowlist and provider_id not in self.allowlist:
            return OutboundPreview(provider_id, purpose, "", 0, False)
        outgoing = content
        redactions = 0
        if self.mode is PrivacyMode.CONTROLLED:
            patterns = [
                r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
                r"(?i)(api[-_ ]?key|password|secret)\s*[:=]\s*\S+",
            ]
            for pattern in patterns:
                outgoing, count = re.subn(pattern, "[REDACTED]", outgoing)
                redactions += count
        return OutboundPreview(provider_id, purpose, outgoing, redactions, True)

    def assert_network_allowed(self, provider_id: str, purpose: str) -> None:
        preview = self.preview(provider_id, purpose, "")
        if not preview.allowed:
            raise PermissionError(f"Outbound call blocked for {provider_id}/{purpose}")


class PrivacyGuardedProvider:
    def __init__(self, provider: ModelProvider, policy: PrivacyPolicy) -> None:
        self.provider = provider
        self.policy = policy
        self.config = provider.config

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.policy.assert_network_allowed(self.config.id, "chat")
        return await self.provider.chat(request)

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        self.policy.assert_network_allowed(self.config.id, "chat")
        async for chunk in self.provider.stream(request):
            yield chunk
