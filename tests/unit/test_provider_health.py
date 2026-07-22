import httpx

from paperagent.providers.base import ProviderError, ProviderHealth
from paperagent.providers.health import classify_provider_error


def test_provider_health_classifies_retry_and_schema_failures() -> None:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    for status, code, retryable in (
        (401, "AUTH", False),
        (404, "ENDPOINT_OR_MODEL_NOT_FOUND", False),
        (429, "RATE_LIMIT", True),
        (503, "HTTP_ERROR", True),
    ):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError("probe", request=request, response=response)
        result = classify_provider_error(error)
        assert result.code == code
        assert result.retryable is retryable

    schema = classify_provider_error(ValueError("tool_calls must be a list"))
    assert schema.status is ProviderHealth.ERROR and schema.code == "SCHEMA"
    timeout = classify_provider_error(
        ProviderError("UPSTREAM_TIMEOUT", "timed out", retryable=True)
    )
    assert timeout.status is ProviderHealth.DEGRADED and timeout.retryable
