from __future__ import annotations

from dataclasses import dataclass

from paperagent.providers.base import ChatRequest, ChatResponse, ModelProvider, ProviderError


@dataclass(frozen=True)
class RouteDecision:
    provider_id: str
    reason: str


class ProviderRouter:
    def __init__(self, providers: list[ModelProvider], *, budget: float | None = None) -> None:
        self.providers = providers
        self.budget = budget
        self.spent = 0.0

    def select(self, request: ChatRequest) -> tuple[ModelProvider, RouteDecision]:
        if self.budget is not None and self.spent >= self.budget:
            raise ProviderError("BUDGET_EXCEEDED", "Provider budget exhausted", retryable=False)
        required = request.required_capabilities()
        for provider in self.providers:
            if required <= provider.config.capabilities:
                return provider, RouteDecision(provider.config.id, f"supports {sorted(required)}")
        raise ProviderError(
            "NO_PROVIDER", "No provider satisfies required capabilities", retryable=False
        )

    def record_cost(self, cost: float) -> None:
        self.spent += max(cost, 0)

    async def chat(self, request: ChatRequest) -> tuple[ChatResponse, RouteDecision]:
        required = request.required_capabilities()
        candidates = [item for item in self.providers if required <= item.config.capabilities]
        if not candidates:
            raise ProviderError(
                "NO_PROVIDER", "No provider satisfies required capabilities", retryable=False
            )
        last_error: ProviderError | None = None
        for index, provider in enumerate(candidates):
            if self.budget is not None and self.spent >= self.budget:
                raise ProviderError("BUDGET_EXCEEDED", "Provider budget exhausted", retryable=False)
            try:
                response = await provider.chat(request)
                self.record_cost(response.usage.estimated_cost)
                prior_code = last_error.code if last_error else "none"
                reason = "primary" if index == 0 else f"fallback after {prior_code}"
                return response, RouteDecision(provider.config.id, reason)
            except ProviderError as error:
                last_error = error
                if error.state_unknown or not error.retryable:
                    raise
        assert last_error is not None
        raise last_error
