from __future__ import annotations

import re
from typing import Any, Iterable


_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(appSecret|access_token|refreshToken|refresh_token|sign)"
    r"(\s*[=:]\s*|%3[dD])"
    r"([^&\s,}\]]+|\"[^\"]*\")"
)


def redact_sensitive_text(value: object, extra_secrets: Iterable[str] = ()) -> str:
    """Return diagnostic text without authentication material.

    Lingxing requires authentication values in the query string, so transport
    exceptions can otherwise leak them through a rendered URL.  Callers should
    still avoid logging request URLs; this helper is a final defensive layer.
    """

    text = str(value)
    for secret in extra_secrets:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    return _SENSITIVE_FIELD_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)


class LingxingError(Exception):
    """Base class for errors raised by the Lingxing OpenAPI integration."""


class LingxingConfigurationError(LingxingError):
    """The local OpenAPI configuration is incomplete or invalid."""


class LingxingProtocolError(LingxingError):
    """The server response does not match the documented contract."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        self.request_id = request_id
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"{message}{suffix}")


class LingxingTransportError(LingxingError):
    """A request failed before a usable HTTP response was obtained."""

    def __init__(self, operation: str, *, retryable: bool = True) -> None:
        self.operation = operation
        self.retryable = retryable
        super().__init__(f"Lingxing transport failed during {operation}")


class LingxingHTTPError(LingxingError):
    """Lingxing returned a non-successful HTTP status."""

    def __init__(
        self,
        operation: str,
        status_code: int,
        *,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.operation = operation
        self.status_code = int(status_code)
        self.request_id = request_id
        self.retryable = retryable
        suffix = f", request_id={request_id}" if request_id else ""
        super().__init__(f"Lingxing HTTP {status_code} during {operation}{suffix}")


class LingxingAPIError(LingxingError):
    """A JSON response contains a Lingxing application-level error code."""

    def __init__(
        self,
        operation: str,
        code: object,
        message: str = "",
        *,
        request_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.operation = operation
        self.code = str(code)
        self.server_message = message
        self.request_id = request_id
        self.payload = payload
        detail = f": {message}" if message else ""
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"Lingxing API {self.code} during {operation}{detail}{suffix}")


class LingxingAuthError(LingxingAPIError):
    """The token endpoint rejected the request."""


class LingxingAmbiguousWriteError(LingxingError):
    """A write may have reached Lingxing and therefore must not be retried blindly."""

    def __init__(
        self,
        operation: str,
        *,
        request_id: str | None = None,
        cause: LingxingError | None = None,
    ) -> None:
        self.operation = operation
        self.request_id = request_id
        self.cause = cause
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(
            f"Lingxing write outcome is ambiguous during {operation}; reconcile state before retrying{suffix}"
        )
