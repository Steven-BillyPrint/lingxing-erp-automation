from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import quote


ALIMAIL_BASE_URL = "https://alimail-cn.aliyuncs.com"
CLICKSEND_BASE_URL = "https://rest.clicksend.com"


class NotificationProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderAcceptance:
    message_id: str
    status: str
    raw_code: str = ""


def _response_error(response: Any, provider: str) -> NotificationProviderError:
    status = int(getattr(response, "status_code", 0) or 0)
    retryable = status in {401, 403, 408, 409, 425, 429} or status >= 500
    details: list[str] = []
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, Mapping):
        code = _safe_provider_detail(
            payload.get("code") or payload.get("error") or payload.get("errorCode")
        )
        message = _safe_provider_detail(
            payload.get("message")
            or payload.get("error_description")
            or payload.get("errorMessage")
        )
        request_id = _safe_request_id(
            payload.get("requestId")
            or payload.get("request_id")
            or payload.get("request-id")
        )
        if code:
            details.append(f"code={code}")
        if message and message != code:
            details.append(f"message={message}")
        if request_id:
            details.append(f"request_id={request_id}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return NotificationProviderError(
        f"{provider} request failed with HTTP {status or 'unknown'}{suffix}.",
        retryable=retryable,
    )


def _safe_provider_detail(value: Any, *, max_length: int = 240) -> str:
    """Keep provider diagnostics useful without persisting contact data or secrets."""

    if isinstance(value, Mapping) or isinstance(value, (list, tuple, set)):
        return ""
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[email redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Z0-9._~+/=-]+",
        "bearer [redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
        r"api[_ -]?key|password|secret)\b\s*[:=]\s*[^,;\s]+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9])\+?\d[\d\s().-]{5,}\d(?![A-Za-z0-9])",
        "[number redacted]",
        text,
    )
    return text[:max_length]


def _safe_request_id(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", text):
        return text
    return ""


def _require_mapping(value: Any, provider: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NotificationProviderError(
            f"{provider} returned an unexpected response.", retryable=True
        )
    return value


class AlimailClient:
    """Minimal Enterprise Mail v2 client with an in-memory token cache."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        timeout_seconds: float = 30,
        http_client: Any | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "")
        self.timeout_seconds = timeout_seconds
        self._http = http_client
        self._owns_http = http_client is None
        self._access_token = ""
        self._token_expires_at = 0.0

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError("httpx is required for Alimail delivery") from exc
            self._http = httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            )
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def access_token(self, *, force_refresh: bool = False) -> str:
        if not self.app_id or not self.app_secret:
            raise NotificationProviderError("Alimail credentials are not configured.")
        if (
            not force_refresh
            and self._access_token
            and self._token_expires_at > time.monotonic() + 60
        ):
            return self._access_token
        client = await self._client()
        try:
            response = await client.post(
                f"{ALIMAIL_BASE_URL}/oauth2/v2.0/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                },
            )
        except Exception as exc:
            raise NotificationProviderError(
                "Alimail token request could not reach the provider.", retryable=True
            ) from exc
        if int(response.status_code) != 200:
            raise _response_error(response, "Alimail token")
        try:
            payload = _require_mapping(response.json(), "Alimail token")
        except ValueError as exc:
            raise NotificationProviderError(
                "Alimail token response was not JSON.", retryable=True
            ) from exc
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise NotificationProviderError(
                "Alimail token response did not contain access_token.", retryable=True
            )
        try:
            expires_in = max(60, int(payload.get("expires_in") or 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        self._access_token = token
        self._token_expires_at = time.monotonic() + expires_in
        return token

    async def _authorized_post(
        self, url: str, payload: Mapping[str, Any], *, retry_auth: bool = True
    ) -> Any:
        client = await self._client()
        token = await self.access_token()
        try:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"bearer {token}",
                },
                json=dict(payload),
            )
        except Exception as exc:
            raise NotificationProviderError(
                "Alimail request could not reach the provider.", retryable=True
            ) from exc
        if response.status_code == 401 and retry_auth:
            await self.access_token(force_refresh=True)
            return await self._authorized_post(url, payload, retry_auth=False)
        if not 200 <= int(response.status_code) < 300:
            raise _response_error(response, "Alimail")
        return response

    async def _authorized_get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        client = await self._client()
        token = await self.access_token()
        try:
            response = await client.get(
                url,
                headers={"Authorization": f"bearer {token}"},
                params=dict(params or {}),
            )
        except Exception as exc:
            raise NotificationProviderError(
                "Alimail request could not reach the provider.", retryable=True
            ) from exc
        if response.status_code == 401 and retry_auth:
            await self.access_token(force_refresh=True)
            return await self._authorized_get(url, params=params, retry_auth=False)
        if not 200 <= int(response.status_code) < 300:
            raise _response_error(response, "Alimail")
        return response

    async def send(
        self,
        *,
        sender_email: str,
        sender_name: str,
        recipient_email: str,
        recipient_name: str,
        subject: str,
        body: str,
        idempotency_key: str,
        body_html: str = "",
    ) -> ProviderAcceptance:
        sender = str(sender_email or "").strip()
        recipient = str(recipient_email or "").strip()
        if not sender or not recipient:
            raise NotificationProviderError("Alimail sender and recipient are required.")
        internet_message_id = f"<{idempotency_key}@shipment-automation.billyprint.com>"
        reviewed_html = str(body_html or "")
        if not reviewed_html:
            # Legacy approved drafts predate persisted HTML. Preserve their
            # historical rendering when the exact approved content is retried.
            reviewed_html = "<br>".join(html.escape(body).splitlines())
        message = {
            "message": {
                "internetMessageId": internet_message_id,
                "subject": subject,
                "summary": body[:200],
                "priority": "PRY_NORMAL",
                "from": {"email": sender, "name": sender_name},
                "toRecipients": [{"email": recipient, "name": recipient_name}],
                "replyTo": [{"email": sender, "name": sender_name}],
                "body": {"bodyText": body, "bodyHtml": reviewed_html},
            }
        }
        account_path = quote(sender, safe="@")
        create_response = await self._authorized_post(
            f"{ALIMAIL_BASE_URL}/v2/users/{account_path}/messages", message
        )
        try:
            create_payload = _require_mapping(create_response.json(), "Alimail")
            created_message = _require_mapping(create_payload.get("message"), "Alimail")
        except ValueError as exc:
            raise NotificationProviderError(
                "Alimail draft response was not JSON.", retryable=True
            ) from exc
        draft_id = str(created_message.get("id") or "").strip()
        if not draft_id:
            raise NotificationProviderError(
                "Alimail draft response did not contain an id.", retryable=True
            )
        await self._authorized_post(
            f"{ALIMAIL_BASE_URL}/v2/users/{account_path}/messages/"
            f"{quote(draft_id, safe='')}/send",
            {"saveToSentItems": True},
        )
        return ProviderAcceptance(
            message_id=draft_id,
            status="ACCEPTED",
            raw_code="HTTP_2XX",
        )

    async def receipt(
        self,
        *,
        sender_email: str,
        message_id: str,
        idempotency_key: str = "",
        subject: str = "",
        recipient_email: str = "",
        sent_at: str = "",
    ) -> Mapping[str, Any]:
        sender = str(sender_email or "").strip()
        provider_message_id = str(message_id or "").strip()
        if not sender or not provider_message_id:
            raise NotificationProviderError(
                "Alimail sender and message id are required for status lookup."
            )
        account_path = quote(sender, safe="@")
        stable_key = str(idempotency_key or "").strip()
        expected_internet_message_id = (
            f"<{stable_key}@shipment-automation.billyprint.com>" if stable_key else ""
        )
        search_subject = str(subject or "").strip()
        if expected_internet_message_id and search_subject:
            escaped_subject = search_subject.replace('"', '""')
            recipient = str(recipient_email or "").strip().replace('"', '""')
            query = f'subject:"{escaped_subject}"'
            if recipient:
                query += f' AND toEmail="{recipient}"'
            search_response = await self._authorized_post(
                f"{ALIMAIL_BASE_URL}/v2/users/{account_path}/messages/query"
                "?$select=internetMessageId,sendStatus,sentDateTime",
                {
                    "email": sender,
                    "query": query,
                    "cursor": "",
                    "size": 100,
                },
            )
            try:
                search_payload = _require_mapping(search_response.json(), "Alimail")
            except ValueError as exc:
                raise NotificationProviderError(
                    "Alimail search response was not JSON.", retryable=True
                ) from exc
            messages = search_payload.get("messages")
            if isinstance(messages, list):
                candidates: list[Mapping[str, Any]] = []
                for item in messages:
                    if not isinstance(item, Mapping):
                        continue
                    if str(item.get("internetMessageId") or "").strip() != expected_internet_message_id:
                        candidates.append(item)
                        continue
                    candidates = [item]
                    break
                if len(candidates) > 1 and sent_at:
                    try:
                        expected_time = datetime.fromisoformat(
                            str(sent_at).replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        expected_time = None
                    if expected_time is not None:
                        def _distance(item: Mapping[str, Any]) -> float:
                            try:
                                value = datetime.fromisoformat(
                                    str(item.get("sentDateTime") or "").replace(
                                        "Z", "+00:00"
                                    )
                                ).astimezone(timezone.utc)
                            except (TypeError, ValueError):
                                return float("inf")
                            return abs((value - expected_time).total_seconds())

                        closest = min(candidates, key=_distance)
                        candidates = (
                            [closest] if _distance(closest) <= 3600 else []
                        )
                if len(candidates) == 1:
                    item = candidates[0]
                    send_status = str(item.get("sendStatus") or "").strip().lower()
                    sent_message_id = str(item.get("id") or "").strip()
                    if send_status and sent_message_id:
                        return {
                            "send_status": send_status,
                            "message_id": sent_message_id,
                        }
        response = await self._authorized_get(
            f"{ALIMAIL_BASE_URL}/v2/users/{account_path}/messages/"
            f"{quote(provider_message_id, safe='')}",
            params={"$select": "sendStatus"},
        )
        try:
            payload = _require_mapping(response.json(), "Alimail")
        except ValueError as exc:
            raise NotificationProviderError(
                "Alimail status response was not JSON.", retryable=True
            ) from exc
        message_value = payload.get("message")
        message = message_value if isinstance(message_value, Mapping) else payload
        send_status = str(message.get("sendStatus") or "").strip().lower()
        if not send_status:
            raise NotificationProviderError(
                "Alimail status response did not contain sendStatus.", retryable=True
            )
        return {"send_status": send_status, "message_id": provider_message_id}

    async def test_connection(self) -> bool:
        await self.access_token(force_refresh=True)
        return True


class ClickSendClient:
    def __init__(
        self,
        username: str,
        api_key: str,
        *,
        sender_id: str = "",
        timeout_seconds: float = 30,
        http_client: Any | None = None,
    ) -> None:
        self.username = str(username or "").strip()
        self.api_key = str(api_key or "")
        self.sender_id = str(sender_id or "").strip()
        self.timeout_seconds = timeout_seconds
        self._http = http_client
        self._owns_http = http_client is None

    async def _client(self) -> Any:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError("httpx is required for ClickSend delivery") from exc
            self._http = httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            )
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        if not self.username or not self.api_key:
            raise NotificationProviderError("ClickSend credentials are not configured.")
        client = await self._client()
        try:
            response = await client.request(
                method,
                f"{CLICKSEND_BASE_URL}{path}",
                auth=(self.username, self.api_key),
                headers={"Content-Type": "application/json"},
                **kwargs,
            )
        except Exception as exc:
            raise NotificationProviderError(
                "ClickSend request could not reach the provider.", retryable=True
            ) from exc
        if not 200 <= int(response.status_code) < 300:
            raise _response_error(response, "ClickSend")
        try:
            return _require_mapping(response.json(), "ClickSend")
        except ValueError as exc:
            raise NotificationProviderError(
                "ClickSend response was not JSON.", retryable=True
            ) from exc

    async def send(
        self,
        *,
        to: str,
        body: str,
        idempotency_key: str,
    ) -> ProviderAcceptance:
        message: dict[str, Any] = {
            "to": to,
            "body": body,
            "source": "erp-shipment-automation",
            "custom_string": idempotency_key,
        }
        if self.sender_id:
            message["from"] = self.sender_id
        payload = await self._request(
            "POST", "/v3/sms/send", json={"messages": [message]}
        )
        if str(payload.get("response_code") or "").upper() != "SUCCESS":
            raise NotificationProviderError(
                "ClickSend did not accept the SMS.", retryable=False
            )
        data = _require_mapping(payload.get("data"), "ClickSend")
        messages = data.get("messages")
        first = messages[0] if isinstance(messages, list) and messages else None
        first = _require_mapping(first, "ClickSend")
        message_id = str(first.get("message_id") or "").strip()
        status = str(first.get("status") or "").strip().upper()
        if not message_id or status not in {"SUCCESS", "QUEUED", "SCHEDULED"}:
            raise NotificationProviderError(
                "ClickSend response did not contain an accepted message.",
                retryable=status in {"", "PENDING"},
            )
        return ProviderAcceptance(
            message_id=message_id,
            status=status,
            raw_code=str(payload.get("response_code") or ""),
        )

    async def history(
        self,
        message_id: str,
        *,
        sent_at: str = "",
        max_pages: int = 5,
    ) -> Mapping[str, Any]:
        """Find an outbound SMS in ClickSend history by its exact message id.

        Delivery-receipt endpoints only contain records when the ClickSend
        account has a POLL receipt rule.  SMS history is available independently
        of that optional account automation, so it is the authoritative lookup
        used by the desktop application.
        """

        expected = str(message_id or "").strip()
        if not expected:
            raise ValueError("ClickSend message id is required.")
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=7)
        raw_sent_at = str(sent_at or "").strip()
        if raw_sent_at:
            try:
                parsed = datetime.fromisoformat(raw_sent_at.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                start = parsed.astimezone(timezone.utc) - timedelta(hours=1)
            except ValueError:
                pass
        params: dict[str, Any] = {
            "date_from": int(start.timestamp()),
            "date_to": int((now + timedelta(minutes=5)).timestamp()),
            "limit": 100,
            "order_by": "date:desc",
        }
        expected_upper = expected.upper()
        page_limit = max(1, min(int(max_pages or 1), 20))
        for page in range(1, page_limit + 1):
            params["page"] = page
            payload = await self._request("GET", "/v3/sms/history", params=params)
            data = payload.get("data")
            if isinstance(data, Mapping):
                rows = data.get("data")
                last_page = int(data.get("last_page") or 1)
            else:
                rows = data
                last_page = 1
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                current = str(row.get("message_id") or "").strip().upper()
                if current == expected_upper:
                    return row
            if page >= last_page or not rows:
                break
        return {}

    async def receipt(self, message_id: str) -> Mapping[str, Any]:
        """Compatibility wrapper; delivery checks now use SMS history."""

        return await self.history(message_id)

    async def test_connection(self) -> bool:
        await self._request("GET", "/v3/account")
        return True


__all__ = [
    "ALIMAIL_BASE_URL",
    "CLICKSEND_BASE_URL",
    "AlimailClient",
    "ClickSendClient",
    "NotificationProviderError",
    "ProviderAcceptance",
]
