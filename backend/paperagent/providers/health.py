from __future__ import annotations

import socket
import ssl

import httpx
from pydantic import BaseModel

from paperagent.providers.base import ProviderError, ProviderHealth


class ProviderHealthResult(BaseModel):
    status: ProviderHealth
    code: str
    detail: str
    retryable: bool = False


def classify_provider_error(error: Exception) -> ProviderHealthResult:
    if isinstance(error, ProviderError):
        status = ProviderHealth.DEGRADED if error.retryable else ProviderHealth.ERROR
        return ProviderHealthResult(
            status=status,
            code=error.code,
            detail=str(error),
            retryable=error.retryable,
        )
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return ProviderHealthResult(
            status=ProviderHealth.DEGRADED,
            code="TIMEOUT",
            detail=str(error),
            retryable=True,
        )
    if isinstance(error, (ssl.SSLError, httpx.ProxyError)):
        return ProviderHealthResult(
            status=ProviderHealth.ERROR, code="TLS_OR_PROXY", detail=str(error)
        )
    if isinstance(error, (socket.gaierror, httpx.ConnectError)):
        detail = str(error)
        code = "DNS" if "name" in detail.casefold() else "CONNECT"
        return ProviderHealthResult(
            status=ProviderHealth.DEGRADED, code=code, detail=detail, retryable=True
        )
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        code = {
            401: "AUTH",
            403: "AUTH",
            404: "ENDPOINT_OR_MODEL_NOT_FOUND",
            429: "RATE_LIMIT",
        }.get(status_code, "HTTP_ERROR")
        return ProviderHealthResult(
            status=ProviderHealth.DEGRADED if status_code == 429 else ProviderHealth.ERROR,
            code=code,
            detail=f"HTTP {status_code}: {error!s}",
            retryable=status_code == 429 or status_code >= 500,
        )
    if isinstance(error, PermissionError):
        return ProviderHealthResult(
            status=ProviderHealth.BLOCKED, code="CREDENTIAL_OR_APPROVAL", detail=str(error)
        )
    if isinstance(error, (KeyError, TypeError, ValueError)):
        return ProviderHealthResult(status=ProviderHealth.ERROR, code="SCHEMA", detail=str(error))
    return ProviderHealthResult(
        status=ProviderHealth.ERROR,
        code="UNKNOWN",
        detail=f"{type(error).__name__}: {error!s}",
    )
