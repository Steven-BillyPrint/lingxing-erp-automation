from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import unquote

from .auth import (
    CredentialProvider,
    InterProcessLock,
    IssuedToken,
    LingxingCredentials,
    TokenManager,
    TokenStore,
)
from .errors import (
    LingxingAPIError,
    LingxingAmbiguousWriteError,
    LingxingAuthError,
    LingxingConfigurationError,
    LingxingError,
    LingxingHTTPError,
    LingxingProtocolError,
    LingxingTransportError,
    redact_sensitive_text,
)
from .signing import LingxingSigner, canonical_json_bytes


DEFAULT_BASE_URL = "https://openapi.lingxing.com"
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_TOKEN_REJECTION_CODES = frozenset({"2001003", "2001005"})
_SIGN_REJECTION_CODES = frozenset({"2001006", "2001007"})
_READ_RETRY_API_CODES = frozenset({"3001008"})
_RATE_LIMIT_RETRY_BASE_DELAY_SECONDS = 2.0


class AsyncHTTPClient(Protocol):
    async def request(self, method: str, url: str, **kwargs): ...

    async def aclose(self) -> None: ...


class OperationKind(str, Enum):
    READ = "read"
    WRITE = "write"


class ResponseKind(str, Enum):
    JSON = "json"
    BINARY = "binary"


@dataclass(frozen=True)
class EndpointPolicy:
    name: str
    path: str
    method: str = "POST"
    operation_kind: OperationKind = OperationKind.READ
    response_kind: ResponseKind = ResponseKind.JSON
    success_codes: frozenset[str] = frozenset({"0"})

    @property
    def may_retry_transport(self) -> bool:
        return self.operation_kind is OperationKind.READ


def _endpoint(
    name: str,
    path: str,
    *,
    method: str = "POST",
    write: bool = False,
    binary: bool = False,
    success_codes: Sequence[object] = (0,),
) -> EndpointPolicy:
    return EndpointPolicy(
        name=name,
        path=path,
        method=method,
        operation_kind=OperationKind.WRITE if write else OperationKind.READ,
        response_kind=ResponseKind.BINARY if binary else ResponseKind.JSON,
        success_codes=frozenset(str(code) for code in success_codes),
    )


ENDPOINTS: dict[str, EndpointPolicy] = {
    "list_orders": _endpoint("list_orders", "/pb/mp/order/v2/list"),
    "get_fbm_order_detail": _endpoint(
        "get_fbm_order_detail", "/erp/sc/routing/order/Order/getOrderDetail"
    ),
    "download_order_attachment": _endpoint(
        "download_order_attachment",
        "/filestream/api/cepf/attachment/download",
        binary=True,
    ),
    # Compatibility endpoint name retained for callers that predate the
    # explicit distinction between FBM order attachments and custom files.
    "download_attachment": _endpoint(
        "download_attachment",
        "/filestream/api/cepf/attachment/download",
        binary=True,
    ),
    "download_custom_attachment": _endpoint(
        "download_custom_attachment",
        "/erp/sc/routing/customized/file/download",
    ),
    "update_order": _endpoint(
        "update_order", "/pb/mp/order/v2/updateOrder", write=True, success_codes=(10002,)
    ),
    "set_order_remark": _endpoint(
        "set_order_remark", "/pb/mp/order/setRemark", write=True, success_codes=(10002,)
    ),
    "split_order": _endpoint("split_order", "/pb/mp/order/v2/splitOrder", write=True),
    "list_warehouses": _endpoint(
        "list_warehouses", "/erp/sc/data/local_inventory/warehouse"
    ),
    "list_logistics_types": _endpoint(
        "list_logistics_types", "/erp/sc/routing/wms/WmsLogistics/listUsedLogisticsType"
    ),
    "edit_order_logistics": _endpoint(
        "edit_order_logistics", "/pb/mp/order/editOrder", write=True
    ),
    "review_orders": _endpoint(
        "review_orders", "/basicOpen/openapi/multiplatform/order/review", write=True
    ),
    "list_wms_orders": _endpoint(
        "list_wms_orders", "/erp/sc/routing/wms/order/wmsOrderList"
    ),
    "set_tracking_no": _endpoint(
        "set_tracking_no", "/basicOpen/logisticsOrdering/setTrackingNo", write=True
    ),
    "deliver_orders": _endpoint(
        "deliver_orders", "/basicOpen/selfShipmentOrder/deliveryGoods", write=True
    ),
    "fast_outbound": _endpoint(
        "fast_outbound", "/pb/mp/order/v2/fastOutbound", write=True
    ),
    "get_fast_outbound_result": _endpoint(
        "get_fast_outbound_result", "/pb/mp/order/v2/getFastOutboundResult"
    ),
    "list_multi_platform_shops": _endpoint(
        "list_multi_platform_shops", "/pb/mp/shop/v2/getSellerList"
    ),
    "list_amazon_sellers": _endpoint(
        "list_amazon_sellers", "/erp/sc/data/seller/lists", method="GET"
    ),
    "submit_fulfillment": _endpoint(
        "submit_fulfillment", "/pb/mp/order/submitFulfillment", write=True
    ),
    "get_fulfillment_result": _endpoint(
        "get_fulfillment_result", "/pb/mp/order/getFulfillmentResult"
    ),
}


@dataclass(frozen=True)
class APIResponse:
    code: str
    message: str
    data: Any
    request_id: str | None
    response_time: str | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class BinaryResponse:
    content: bytes = b""
    filename: str | None = None
    content_type: str | None = None
    request_id: str | None = None


def _header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    getter = getattr(headers, "get", None)
    if getter:
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is None:
            value = getter(name.upper())
        return str(value) if value is not None else None
    return None


def _request_id(response: object, payload: Mapping[str, Any] | None = None) -> str | None:
    if payload:
        value = (
            payload.get("request_id")
            or payload.get("requestId")
            or payload.get("traceId")
            or payload.get("trace_id")
        )
        if value is not None:
            return str(value)
    return _header(response, "x-request-id") or _header(response, "request-id")


def _is_transport_exception(exc: BaseException) -> bool:
    if isinstance(exc, (OSError, TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(exc, httpx.TransportError)


def _json_payload(response: object, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()  # type: ignore[attr-defined]
    except Exception:
        try:
            payload = json.loads(bytes(getattr(response, "content", b"")).decode("utf-8"))
        except Exception as exc:
            raise LingxingProtocolError(
                f"Lingxing returned invalid JSON during {operation}",
                request_id=_request_id(response),
            ) from exc
    if not isinstance(payload, dict):
        raise LingxingProtocolError(
            f"Lingxing returned a non-object JSON response during {operation}",
            request_id=_request_id(response),
        )
    return payload


class LingxingTokenEndpoint:
    """Official multipart access-token and one-time refresh-token endpoints."""

    def __init__(self, http_client: AsyncHTTPClient, *, base_url: str = DEFAULT_BASE_URL) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")

    async def issue_token(self, credentials: LingxingCredentials) -> IssuedToken:
        credentials.validate()
        return await self._post_token_form(
            "issue_access_token",
            "/api/auth-server/oauth/access-token",
            {"appId": credentials.app_id, "appSecret": credentials.app_secret},
            extra_secrets=(credentials.app_secret,),
        )

    async def refresh_token(self, app_id: str, refresh_token: str) -> IssuedToken:
        if not app_id or not refresh_token:
            raise LingxingConfigurationError("AppID and refresh token are required")
        return await self._post_token_form(
            "refresh_access_token",
            "/api/auth-server/oauth/refresh",
            {"appId": app_id, "refreshToken": refresh_token},
            extra_secrets=(refresh_token,),
        )

    async def _post_token_form(
        self,
        operation: str,
        path: str,
        fields: Mapping[str, str],
        *,
        extra_secrets: Sequence[str],
    ) -> IssuedToken:
        files = {name: (None, value) for name, value in fields.items()}
        try:
            response = await self._http.request(
                "POST",
                f"{self._base_url}{path}",
                files=files,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            if _is_transport_exception(exc):
                raise LingxingTransportError(operation) from None
            raise

        status = int(getattr(response, "status_code", 0))
        if status != 200:
            if status in _RETRYABLE_HTTP_STATUSES:
                raise LingxingTransportError(operation)
            raise LingxingHTTPError(
                operation,
                status,
                request_id=_request_id(response),
                retryable=False,
            )

        payload = _json_payload(response, operation)
        code = str(payload.get("code", ""))
        request_id = _request_id(response, payload)
        if code != "200":
            message = redact_sensitive_text(
                payload.get("msg") or payload.get("message") or "token request rejected",
                extra_secrets,
            )
            raise LingxingAuthError(
                operation,
                code or "missing_code",
                message,
                request_id=request_id,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise LingxingProtocolError(
                f"Lingxing token response has no data object during {operation}",
                request_id=request_id,
            )
        try:
            access_token = str(data["access_token"])
            refresh_token = str(data["refresh_token"])
            expires_in = int(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LingxingProtocolError(
                f"Lingxing token response is incomplete during {operation}",
                request_id=request_id,
            ) from exc
        return IssuedToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )


class LingxingOpenAPIClient:
    """Async Lingxing OpenAPI client with signed calls and conservative retries."""

    def __init__(
        self,
        http_client: AsyncHTTPClient,
        token_manager: TokenManager,
        *,
        app_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_read_retries: int = 2,
        retry_base_delay: float = 0.25,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        owns_http_client: bool = False,
    ) -> None:
        self._http = http_client
        self._token_manager = token_manager
        self._signer = LingxingSigner(app_id)
        self._app_id = app_id
        self._base_url = base_url.rstrip("/")
        self._timeout = max(0.1, float(timeout))
        self._max_read_retries = max(0, int(max_read_retries))
        self._retry_base_delay = max(0.0, float(retry_base_delay))
        self._clock = clock
        self._sleeper = sleeper
        self._owns_http_client = owns_http_client

    @classmethod
    async def create(
        cls,
        credential_provider: CredentialProvider,
        token_store: TokenStore,
        interprocess_lock: InterProcessLock,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        refresh_skew_seconds: float = 600.0,
        max_read_retries: int = 2,
    ) -> "LingxingOpenAPIClient":
        """Create the production httpx transport while keeping secrets injected."""

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on deployment packaging
            raise LingxingConfigurationError(
                "The httpx package is required for the Lingxing OpenAPI client"
            ) from exc

        value = credential_provider.get_credentials()
        credentials = await value if inspect.isawaitable(value) else value
        if not isinstance(credentials, LingxingCredentials):
            raise LingxingConfigurationError("CredentialProvider returned an invalid object")
        credentials.validate()

        http_client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        endpoint = LingxingTokenEndpoint(http_client, base_url=base_url)
        manager = TokenManager(
            credential_provider,
            token_store,
            interprocess_lock,
            endpoint,
            refresh_skew_seconds=refresh_skew_seconds,
        )
        return cls(
            http_client,
            manager,
            app_id=credentials.app_id,
            base_url=base_url,
            timeout=timeout,
            max_read_retries=max_read_retries,
            owns_http_client=True,
        )

    async def __aenter__(self) -> "LingxingOpenAPIClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http.aclose()
            self._owns_http_client = False

    async def call(
        self,
        endpoint: str | EndpointPolicy,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> APIResponse | BinaryResponse:
        policy = ENDPOINTS[endpoint] if isinstance(endpoint, str) else endpoint
        return await self._request(policy, query=query, body=body)

    async def _request(
        self,
        policy: EndpointPolicy,
        *,
        query: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None,
    ) -> APIResponse | BinaryResponse:
        read_retries_used = 0
        token_recovery_used = False
        sign_recovery_used = False

        while True:
            token = await self._token_manager.get_token()
            try:
                return await self._send_once(
                    policy,
                    query=query,
                    body=body,
                    access_token=token.access_token,
                )
            except LingxingAPIError as exc:
                if exc.code in _TOKEN_REJECTION_CODES and not token_recovery_used:
                    # Authentication rejection occurs before business handling,
                    # so one token-recovery retry is safe even for a write.
                    await self._token_manager.get_token(
                        force_refresh=True,
                        stale_access_token=token.access_token,
                    )
                    token_recovery_used = True
                    continue
                if exc.code in _SIGN_REJECTION_CODES and not sign_recovery_used:
                    # A rejected/expired signature also proves the write was not
                    # accepted. Recreate timestamp/sign exactly once.
                    sign_recovery_used = True
                    continue
                if (
                    policy.may_retry_transport
                    and exc.code in _READ_RETRY_API_CODES
                    and read_retries_used < self._max_read_retries
                ):
                    await self._read_retry_sleep(
                        read_retries_used,
                        rate_limited=True,
                    )
                    read_retries_used += 1
                    continue
                raise
            except (LingxingTransportError, LingxingHTTPError) as exc:
                retryable = isinstance(exc, LingxingTransportError) or exc.retryable
                if (
                    policy.may_retry_transport
                    and retryable
                    and read_retries_used < self._max_read_retries
                ):
                    await self._read_retry_sleep(read_retries_used)
                    read_retries_used += 1
                    continue
                if policy.operation_kind is OperationKind.WRITE and retryable:
                    raise LingxingAmbiguousWriteError(
                        policy.name,
                        request_id=getattr(exc, "request_id", None),
                        cause=exc,
                    ) from exc
                raise

    async def _read_retry_sleep(
        self,
        retry_number: int,
        *,
        rate_limited: bool = False,
    ) -> None:
        base_delay = self._retry_base_delay
        if rate_limited:
            base_delay = max(
                base_delay,
                _RATE_LIMIT_RETRY_BASE_DELAY_SECONDS,
            )
        await self._sleeper(base_delay * (2**retry_number))

    async def _send_once(
        self,
        policy: EndpointPolicy,
        *,
        query: Mapping[str, Any] | None,
        body: Mapping[str, Any] | None,
        access_token: str,
    ) -> APIResponse | BinaryResponse:
        business_query = dict(query or {})
        business_body = dict(body) if body is not None else None

        timestamp = str(int(self._clock()))
        signature_params: dict[str, Any] = {}
        if business_body:
            signature_params.update(business_body)
        if business_query:
            signature_params.update(business_query)
        signature_params.update(
            {
                "access_token": access_token,
                "app_key": self._app_id,
                "timestamp": timestamp,
            }
        )
        signature = self._signer.sign(signature_params)

        # Pass the raw Base64 signature as a query value. httpx performs the
        # required URL encoding once; passing ``url_encoded`` here would encode
        # percent signs a second time.
        request_query = dict(business_query)
        request_query.update(
            {
                "access_token": access_token,
                "app_key": self._app_id,
                "timestamp": timestamp,
                "sign": signature.raw,
            }
        )
        # The custom-order workflow downloads ``newAttachments.file_id`` from
        # this order-attachment endpoint. Lingxing support confirmed that this
        # endpoint requires ``Accept: */*``; keep every other endpoint on its
        # existing media-type policy.
        accepts_any_media_type = policy.name in {
            "download_order_attachment",
            "download_attachment",
        }
        headers = {
            "Accept": "*/*"
            if accepts_any_media_type
            else "application/octet-stream"
            if policy.response_kind is ResponseKind.BINARY
            else "application/json"
        }
        kwargs: dict[str, Any] = {
            "params": request_query,
            "headers": headers,
            "timeout": self._timeout,
        }
        if business_body is not None:
            headers["Content-Type"] = "application/json"
            kwargs["content"] = canonical_json_bytes(business_body)

        try:
            response = await self._http.request(
                policy.method,
                f"{self._base_url}{policy.path}",
                **kwargs,
            )
        except Exception as exc:
            if isinstance(exc, LingxingError):
                raise
            if _is_transport_exception(exc):
                raise LingxingTransportError(policy.name) from None
            raise

        status = int(getattr(response, "status_code", 0))
        request_id = _request_id(response)
        if not 200 <= status < 300:
            raise LingxingHTTPError(
                policy.name,
                status,
                request_id=request_id,
                retryable=status in _RETRYABLE_HTTP_STATUSES,
            )

        if policy.response_kind is ResponseKind.BINARY:
            return self._parse_binary_response(
                policy,
                response,
                access_token=access_token,
            )
        return self._parse_api_response(
            policy,
            response,
            access_token=access_token,
        )

    def _parse_api_response(
        self,
        policy: EndpointPolicy,
        response: object,
        *,
        access_token: str,
    ) -> APIResponse:
        payload = _json_payload(response, policy.name)
        request_id = _request_id(response, payload)
        if "code" not in payload:
            raise LingxingProtocolError(
                f"Lingxing response has no code during {policy.name}",
                request_id=request_id,
            )
        code = str(payload.get("code"))
        message = redact_sensitive_text(
            payload.get("message") or payload.get("msg") or "",
            (access_token,),
        )
        if code not in policy.success_codes:
            raise LingxingAPIError(
                policy.name,
                code,
                message,
                request_id=request_id,
                payload=payload,
            )
        response_time = payload.get("response_time") or payload.get("responseTime")
        return APIResponse(
            code=code,
            message=message,
            data=payload.get("data"),
            request_id=request_id,
            response_time=str(response_time) if response_time is not None else None,
            raw=payload,
        )

    def _parse_binary_response(
        self,
        policy: EndpointPolicy,
        response: object,
        *,
        access_token: str,
    ) -> BinaryResponse:
        content_type = _header(response, "content-type") or ""
        content = bytes(getattr(response, "content", b""))
        if "json" in content_type.lower() or content.lstrip().startswith(b"{"):
            payload = _json_payload(response, policy.name)
            request_id = _request_id(response, payload)
            code = str(payload.get("code", "missing_code"))
            message = redact_sensitive_text(
                payload.get("message") or payload.get("msg") or "binary download rejected",
                (access_token,),
            )
            raise LingxingAPIError(
                policy.name,
                code,
                message,
                request_id=request_id,
                payload=payload,
            )
        disposition = _header(response, "content-disposition") or ""
        filename = self._filename_from_disposition(disposition)
        return BinaryResponse(
            content=content,
            filename=filename,
            content_type=content_type or None,
            request_id=_request_id(response),
        )

    @staticmethod
    def _filename_from_disposition(disposition: str) -> str | None:
        extended = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
        if extended:
            value = unquote(extended.group(1).strip())
        else:
            plain = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', disposition, flags=re.IGNORECASE)
            if not plain:
                return None
            value = (plain.group(1) or plain.group(2) or "").strip()
        value = value.replace("\x00", "")
        safe_name = Path(value.replace("\\", "/")).name
        return safe_name or None

    # Project-facing endpoint methods -------------------------------------------------

    async def list_orders(self, *, offset: int = 0, length: int = 500, **filters: Any) -> APIResponse:
        body = {"offset": offset, "length": length, **filters}
        return await self._json_call("list_orders", body=body)

    async def get_fbm_order_detail(self, order_number: str) -> APIResponse:
        return await self._json_call(
            "get_fbm_order_detail",
            body={"order_number": str(order_number)},
        )

    async def download_order_attachment(self, file_id: str | int) -> BinaryResponse:
        """Download an attachment returned by FBM order ``newAttachments``."""

        result = await self.call(
            "download_order_attachment",
            body={"file_id": str(file_id)},
        )
        if not isinstance(result, BinaryResponse):  # pragma: no cover - policy invariant
            raise LingxingProtocolError("Order attachment endpoint did not return binary content")
        return result

    async def download_attachment(self, file_id: str | int) -> BinaryResponse:
        """Compatibility alias for :meth:`download_order_attachment`."""

        return await self.download_order_attachment(file_id)

    async def download_custom_attachment(self, file_id: str | int) -> APIResponse:
        """Read the documented JSON/base64 customization attachment response."""

        return await self._json_call(
            "download_custom_attachment",
            body={"file_id": str(file_id)},
        )

    async def update_orders(self, order_list: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self._json_call("update_order", body={"order_list": list(order_list)})

    async def update_order(self, order_list: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self.update_orders(order_list)

    async def set_order_remarks(self, orders: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self._json_call("set_order_remark", body={"orders": list(orders)})

    async def set_order_remark(self, orders: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self.set_order_remarks(orders)

    async def split_order(
        self,
        *,
        split_mod: int,
        global_order_no: str | int,
        order_item: Sequence[Any],
    ) -> APIResponse:
        return await self._json_call(
            "split_order",
            body={
                "split_mod": int(split_mod),
                "global_order_no": str(global_order_no),
                "order_item": list(order_item),
            },
        )

    async def list_warehouses(
        self,
        *,
        warehouse_type: int = 1,
        sub_type: int | None = None,
        is_delete: int | str = 0,
        offset: int = 0,
        length: int = 1000,
    ) -> APIResponse:
        body: dict[str, Any] = {
            "type": warehouse_type,
            "is_delete": is_delete,
            "offset": offset,
            "length": length,
        }
        if sub_type is not None:
            body["sub_type"] = sub_type
        return await self._json_call("list_warehouses", body=body)

    async def list_logistics_types(
        self,
        *,
        provider_type: int,
        page: int = 1,
        length: int = 100,
    ) -> APIResponse:
        return await self._json_call(
            "list_logistics_types",
            body={"param": {"provider_type": provider_type, "page": page, "length": length}},
        )

    async def edit_order_logistics(self, order_list: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self._json_call(
            "edit_order_logistics",
            body={"order_list": list(order_list)},
        )

    async def review_orders(self, global_order_nos: Sequence[str | int]) -> APIResponse:
        return await self._json_call(
            "review_orders",
            body={"global_order_no": [str(value) for value in global_order_nos]},
        )

    async def list_wms_orders(self, **filters: Any) -> APIResponse:
        return await self._json_call("list_wms_orders", body=filters)

    async def set_tracking_no(
        self,
        *,
        waybill_no: str,
        wo_number: str,
        tracking_no: str | None = None,
        logistics_freight: str | int | float | None = None,
        logistics_freight_currency_code: str | None = None,
        pkg_fee_weight: str | int | float | None = None,
        pkg_fee_weight_unit: str | None = None,
    ) -> APIResponse:
        body: dict[str, Any] = {"waybill_no": waybill_no, "wo_number": wo_number}
        optional = {
            "tracking_no": tracking_no,
            "logistics_freight": logistics_freight,
            "logistics_freight_currency_code": logistics_freight_currency_code,
            "pkg_fee_weight": pkg_fee_weight,
            "pkg_fee_weight_unit": pkg_fee_weight_unit,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return await self._json_call("set_tracking_no", body=body)

    async def deliver_orders(self, order_numbers: Sequence[str | int]) -> APIResponse:
        return await self._json_call(
            "deliver_orders",
            body={"order_number_list": ",".join(str(value) for value in order_numbers)},
        )

    async def fast_outbound(self, packages: Sequence[Mapping[str, Any]]) -> APIResponse:
        return await self._json_call("fast_outbound", body={"package": list(packages)})

    async def get_fast_outbound_result(self, global_order_nos: Sequence[str | int]) -> APIResponse:
        return await self._json_call(
            "get_fast_outbound_result",
            body={"global_order_no": [str(value) for value in global_order_nos]},
        )

    async def list_multi_platform_shops(
        self,
        platform_codes: Sequence[int | str] | None = None,
    ) -> APIResponse:
        body = {"platform_code": list(platform_codes)} if platform_codes is not None else {}
        return await self._json_call("list_multi_platform_shops", body=body)

    async def list_amazon_sellers(self) -> APIResponse:
        return await self._json_call("list_amazon_sellers", query={})

    async def submit_fulfillment(
        self,
        *,
        region: str,
        seller_id: str,
        marketplace_id: str,
        order_list: Sequence[Mapping[str, Any]],
    ) -> APIResponse:
        return await self._json_call(
            "submit_fulfillment",
            body={
                "region": region,
                "seller_id": seller_id,
                "marketplace_id": marketplace_id,
                "order_list": list(order_list),
            },
        )

    async def get_fulfillment_result(
        self,
        *,
        seller_id: str,
        task_ids: Sequence[str | int],
    ) -> APIResponse:
        return await self._json_call(
            "get_fulfillment_result",
            body={"seller_id": seller_id, "task_id": list(task_ids)},
        )

    async def _json_call(
        self,
        endpoint: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> APIResponse:
        result = await self.call(endpoint, query=query, body=body)
        if not isinstance(result, APIResponse):  # pragma: no cover - policy invariant
            raise LingxingProtocolError(f"{endpoint} unexpectedly returned binary content")
        return result


LingxingClient = LingxingOpenAPIClient
