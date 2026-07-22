import pytest

from paperagent.privacy import PrivacyMode, PrivacyPolicy
from paperagent.privacy.policy import PrivacyGuardedProvider
from paperagent.providers import Capability, ChatMessage, ChatRequest, ProviderConfig, ProviderError
from paperagent.providers.mock import MockProvider
from paperagent.providers.routing import ProviderRouter


def config(provider_id: str, capabilities: set[Capability]) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        provider_type="mock",
        base_url="http://127.0.0.1:9999/v1",
        model="mock-model",
        capabilities=capabilities,
    )


def test_privacy_controlled_redacts_and_offline_blocks() -> None:
    controlled = PrivacyPolicy(PrivacyMode.CONTROLLED, {"allowed"})
    preview = controlled.preview(
        "allowed", "chat", "contact me@example.test password=not-a-real-value"
    )
    assert preview.allowed and preview.redactions == 2
    assert "example.test" not in preview.content
    offline = PrivacyPolicy(PrivacyMode.OFFLINE)
    with pytest.raises(PermissionError):
        offline.assert_network_allowed("any", "chat")


def test_router_matches_capability_and_enforces_budget() -> None:
    basic = MockProvider(config("basic", {Capability.CHAT}))
    tools = MockProvider(config("tools", {Capability.CHAT, Capability.TOOLS}))
    router = ProviderRouter([basic, tools], budget=1)
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hello")],
        tools=[{"type": "function"}],
    )
    provider, decision = router.select(request)
    assert provider.config.id == decision.provider_id == "tools"
    router.record_cost(1)
    with pytest.raises(ProviderError, match="budget"):
        router.select(request)


@pytest.mark.anyio
async def test_offline_guard_prevents_provider_invocation() -> None:
    provider = MockProvider(config("remote", {Capability.CHAT}))
    guarded = PrivacyGuardedProvider(provider, PrivacyPolicy(PrivacyMode.OFFLINE))
    request = ChatRequest(messages=[ChatMessage(role="user", content="private")])
    with pytest.raises(PermissionError):
        await guarded.chat(request)
    assert provider.calls == 0


@pytest.mark.anyio
async def test_router_fallback_and_unknown_state_protection() -> None:
    retryable = MockProvider(
        config("retry", {Capability.CHAT}),
        error=ProviderError("RATE_LIMIT", "retry", retryable=True),
    )
    fallback = MockProvider(config("fallback", {Capability.CHAT}), content="ok")
    router = ProviderRouter([retryable, fallback])
    response, decision = await router.chat(
        ChatRequest(messages=[ChatMessage(role="user", content="hello")])
    )
    assert response.content == "ok" and decision.provider_id == "fallback"

    unknown = MockProvider(
        config("unknown", {Capability.CHAT}),
        error=ProviderError("TIMEOUT", "unknown", retryable=True, state_unknown=True),
    )
    untouched = MockProvider(config("must-not-call", {Capability.CHAT}))
    with pytest.raises(ProviderError):
        await ProviderRouter([unknown, untouched]).chat(
            ChatRequest(messages=[ChatMessage(role="user", content="paid request")])
        )
    assert untouched.calls == 0
