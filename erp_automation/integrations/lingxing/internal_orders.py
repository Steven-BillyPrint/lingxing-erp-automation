"""Authenticated Lingxing ERP internal-detail adapter.

All private endpoint and raw payload knowledge is isolated in this module.
Callers receive stable contracts from :mod:`erp_automation.contracts` and
never need a page, locator, viewport coordinate, or DOM readback.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from erp_automation.contracts.internal_orders import (
    ContactPatch,
    ContactSnapshot,
    ContactWriteOutcome,
    ContactWriteStatus,
    InternalOrderDetail,
)


LINGXING_ERP_BASE_URL = "https://erp.lingxing.com"
ORDER_MANAGEMENT_URL = f"{LINGXING_ERP_BASE_URL}/erp/mmulti/mpOrderManagement"
ORDER_DETAIL_PATH = "/api/platforms/oms/order_list/detail"
ORDER_EDIT_PATH = "/api/platforms/oms/order_edit/edit"
DEFAULT_READBACK_DELAYS_SECONDS = (
    0.0,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    45.0,
    60.0,
    60.0,
    60.0,
)

_PLACEHOLDERS = {"", "-", "--", "null", "none", "undefined", "***"}
_PHONE_NON_DIGITS = re.compile(r"\D+")


class InternalOrderError(RuntimeError):
    """Base error for reads that cannot produce a verified detail."""


class InternalOrderAuthenticationError(InternalOrderError):
    """The submitting browser session is not authorized for the endpoint."""


class InternalOrderProtocolError(InternalOrderError):
    """The endpoint returned an invalid or identity-mismatched response."""


@dataclass(frozen=True)
class _FetchedDetail:
    detail: InternalOrderDetail
    raw: dict[str, Any]


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.casefold() in _PLACEHOLDERS:
        return None
    return text


def _first_text(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_text(source.get(key))
        if value is not None:
            return value
    return None


def normalize_phone(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    digits = _PHONE_NON_DIGITS.sub("", text)
    return digits or None


def normalize_email(value: Any) -> str | None:
    text = _optional_text(value)
    return text.casefold() if text is not None else None


def _platform_order_nos(data: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    rows = data.get("order_item_info")
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for key in ("platform_order_no", "platform_order_name"):
                value = _optional_text(row.get(key))
                if value and value not in values:
                    values.append(value)
    for key in ("platform_order_no", "platform_order_name"):
        value = _optional_text(data.get(key))
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _shipping_address_text(receive: Mapping[str, Any]) -> str:
    parts = [
        _first_text(
            receive,
            "address_line1",
            "address_line_1",
            "address1",
            "receiver_address",
            "receiver_address1",
            "receiver_address_line1",
            "street1",
        ),
        _first_text(
            receive,
            "address_line2",
            "address_line_2",
            "address2",
            "receiver_address2",
            "receiver_address_line2",
            "street2",
        ),
        _first_text(
            receive,
            "address_line3",
            "address_line_3",
            "address3",
            "receiver_address3",
            "receiver_address_line3",
            "street3",
        ),
        _first_text(receive, "city", "receiver_city"),
        _first_text(receive, "state_or_region", "state", "province"),
        _first_text(
            receive,
            "postal_code",
            "receiver_postal_code",
            "zip_code",
            "zipcode",
            "zip",
        ),
        _first_text(
            receive,
            "receiver_country_name",
            "receiver_country",
            "receiver_country_code",
        ),
    ]
    return ", ".join(value for value in parts if value)


def _contact_matches(snapshot: ContactSnapshot, patch: ContactPatch) -> bool:
    return bool(
        (patch.phone is None or snapshot.phone == normalize_phone(patch.phone))
        and (patch.email is None or snapshot.email == normalize_email(patch.email))
    )


_ORDER_ITEM_EDIT_KEYS = (
    "id",
    "new_attachment",
    "remark",
    "pid",
    "quantity",
    "local_sku",
    "local_product_name",
    "is_delete",
    "unit_price_amount",
    "stock_deduct_id",
    "return_tracking_carrier",
    "return_tracking_number",
    "cg_box_pcs",
    "custom_fields",
)
_LOGISTICS_EDIT_KEYS = (
    "wid",
    "warehouse_type",
    "logistics_type_id",
    "first_mile_type_id",
    "first_mile_provider_id",
)


def _project(mapping: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(mapping.get(key)) for key in keys if key in mapping}


def _tag_no(data: Mapping[str, Any]) -> Any:
    if "tag_no" in data:
        return copy.deepcopy(data.get("tag_no"))
    tags = data.get("tags")
    if isinstance(tags, Sequence) and not isinstance(tags, (str, bytes)):
        for tag in tags:
            if isinstance(tag, Mapping) and str(tag.get("type") or "") == "2":
                return copy.deepcopy(tag.get("tag_no") or tag.get("id"))
    return None


def build_order_edit_payload(
    data: Mapping[str, Any],
    patch: ContactPatch,
) -> dict[str, Any]:
    """Mirror the ERP edit form while changing only requested contacts.

    This function is intentionally pure so fixture tests can detect frontend
    schema drift without launching a browser.
    """

    receive_raw = data.get("receive_info")
    buyer_raw = data.get("buyer_info")
    receive = copy.deepcopy(dict(receive_raw)) if isinstance(receive_raw, Mapping) else {}
    buyer = dict(buyer_raw) if isinstance(buyer_raw, Mapping) else {}
    if "buyer_name" in buyer:
        receive["buyer_name"] = copy.deepcopy(buyer.get("buyer_name"))
    if "buyer_email" in buyer:
        receive["buyer_email"] = copy.deepcopy(buyer.get("buyer_email"))
    if patch.phone is not None:
        receive["receiver_mobile"] = str(patch.phone).strip()
    if patch.email is not None:
        receive["buyer_email"] = str(patch.email).strip()

    raw_items = data.get("order_item_info")
    items = (
        [_project(row, _ORDER_ITEM_EDIT_KEYS) for row in raw_items if isinstance(row, Mapping)]
        if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes))
        else []
    )
    logistics_raw = data.get("logistics_info")
    logistics = (
        _project(logistics_raw, _LOGISTICS_EDIT_KEYS)
        if isinstance(logistics_raw, Mapping)
        else {}
    )
    remark_raw = data.get("remark_info")
    remark = dict(remark_raw) if isinstance(remark_raw, Mapping) else {}
    if "remark" not in remark and "remark" in data:
        remark["remark"] = copy.deepcopy(data.get("remark"))
    if "remark_attachment" not in remark and "remark_attachment" in data:
        remark["remark_attachment"] = copy.deepcopy(data.get("remark_attachment"))

    tax_info_raw = data.get("tax_info")
    tax_info = copy.deepcopy(dict(tax_info_raw)) if isinstance(tax_info_raw, Mapping) else {}
    tax_list_raw = data.get("tax_list")
    tax_list = (
        copy.deepcopy(list(tax_list_raw))
        if isinstance(tax_list_raw, Sequence) and not isinstance(tax_list_raw, (str, bytes))
        else []
    )
    extra_raw = data.get("extra_info")
    extra = copy.deepcopy(dict(extra_raw)) if isinstance(extra_raw, Mapping) else {}
    if "documents" not in extra and "customs_declaration_documents" in data:
        extra["documents"] = copy.deepcopy(data.get("customs_declaration_documents"))

    payload: dict[str, Any] = {
        "global_order_no": copy.deepcopy(data.get("global_order_no")),
        "order_item_info": items,
        "receive_info": receive,
        "remark_info": remark,
        "logistics_info": logistics,
        "tag_no": _tag_no(data),
        "sync_pair": copy.deepcopy(data.get("sync_pair", False)),
        "is_reorder": copy.deepcopy(data.get("is_reorder", 0)),
        "tax_info": tax_info,
        "tax_list": tax_list,
        "taxs": [copy.deepcopy(tax_info), *copy.deepcopy(tax_list)],
        "extra_info": extra,
        "custom_fields": copy.deepcopy(data.get("custom_fields") or []),
    }
    if "declared_info" in data:
        payload["declared_info"] = copy.deepcopy(data.get("declared_info"))
    return payload


def _revision(data: Mapping[str, Any]) -> str:
    # Hash the edit projection with an empty patch.  Volatile response metadata
    # cannot create false optimistic-lock conflicts.
    canonical = json.dumps(
        build_order_edit_payload(data, ContactPatch()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_internal_order_detail(
    data: Mapping[str, Any],
    *,
    expected_system_order_no: str,
    expected_platform_order_no: str,
    request_id: str | None = None,
) -> InternalOrderDetail:
    returned_system = str(data.get("global_order_no") or "").strip()
    if returned_system != expected_system_order_no:
        raise InternalOrderProtocolError(
            "领星内部详情返回的系统单号不一致，已停止以避免修改错误订单。"
        )
    platforms = _platform_order_nos(data)
    if expected_platform_order_no and expected_platform_order_no not in platforms:
        raise InternalOrderProtocolError(
            "领星内部详情返回的平台单号不一致，已停止以避免修改错误订单。"
        )
    receive_raw = data.get("receive_info")
    buyer_raw = data.get("buyer_info")
    receive = receive_raw if isinstance(receive_raw, Mapping) else {}
    buyer = buyer_raw if isinstance(buyer_raw, Mapping) else {}
    phone = normalize_phone(
        _first_text(receive, "receiver_mobile", "receiver_tel", "phone")
    )
    email = normalize_email(
        _first_text(buyer, "buyer_email") or _first_text(receive, "buyer_email")
    )
    return InternalOrderDetail(
        system_order_no=returned_system,
        platform_order_nos=platforms,
        recipient_name=(
            _first_text(receive, "receiver_name", "recipient_name", "buyer_name")
            or _first_text(buyer, "buyer_name")
        ),
        address_line1=_first_text(
            receive,
            "address_line1",
            "address_line_1",
            "address1",
            "receiver_address",
            "receiver_address1",
            "receiver_address_line1",
            "street1",
        ),
        address_line2=_first_text(
            receive,
            "address_line2",
            "address_line_2",
            "address2",
            "receiver_address2",
            "receiver_address_line2",
            "street2",
        ),
        address_line3=_first_text(
            receive,
            "address_line3",
            "address_line_3",
            "address3",
            "receiver_address3",
            "receiver_address_line3",
            "street3",
        ),
        city=_first_text(receive, "city", "receiver_city"),
        state_or_region=_first_text(receive, "state_or_region", "state", "province"),
        country_code=_first_text(receive, "receiver_country_code", "country_code"),
        country_name=_first_text(
            receive,
            "receiver_country_name",
            "receiver_country",
            "country_name",
        ),
        postal_code=_first_text(
            receive,
            "postal_code",
            "receiver_postal_code",
            "zip_code",
            "zipcode",
            "zip",
        ),
        shipping_address_text=_shipping_address_text(receive),
        contact=ContactSnapshot(phone=phone, email=email),
        status=str(data.get("status") or data.get("order_status") or "").strip(),
        revision=_revision(data),
        request_id=request_id,
    )


class LingxingInternalOrderClient:
    """Internal-order operations backed by an authenticated request context."""

    def __init__(
        self,
        request_context: Any,
        *,
        readback_delays_seconds: Sequence[float] = DEFAULT_READBACK_DELAYS_SECONDS,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_url: str = LINGXING_ERP_BASE_URL,
        timeout_ms: int = 10000,
    ) -> None:
        if request_context is None:
            raise ValueError("request_context is required")
        self._request = request_context
        self._readback_delays = tuple(max(0.0, float(value)) for value in readback_delays_seconds)
        self._sleeper = sleeper
        self._base_url = str(base_url).rstrip("/")
        self._timeout_ms = int(timeout_ms)

    @property
    def request_context(self) -> Any:
        """Compatibility access for legacy raw-detail readers."""

        return self._request

    async def _response_payload(self, response: Any, *, operation: str) -> Mapping[str, Any]:
        status = int(getattr(response, "status", 0) or 0)
        if status in {401, 403}:
            raise InternalOrderAuthenticationError(
                "领星登录状态已失效（not logged in），内部订单接口拒绝访问。"
            )
        try:
            payload = await response.json()
        except Exception as exc:
            raise InternalOrderProtocolError(
                f"领星内部订单{operation}没有返回有效 JSON。"
            ) from exc
        if not isinstance(payload, Mapping):
            raise InternalOrderProtocolError(
                f"领星内部订单{operation}返回结构无效。"
            )
        if not bool(getattr(response, "ok", False)):
            raise InternalOrderProtocolError(
                f"领星内部订单{operation}失败（HTTP {status or '-'}）。"
            )
        return payload

    async def _fetch(
        self,
        system_order_no: str,
        expected_platform_order_no: str,
    ) -> _FetchedDetail:
        system = str(system_order_no or "").strip()
        platform = str(expected_platform_order_no or "").strip()
        if not system or not platform:
            raise InternalOrderProtocolError("内部订单详情必须同时指定系统单号和平台单号。")
        try:
            response = await self._request.get(
                f"{self._base_url}{ORDER_DETAIL_PATH}",
                params={
                    "global_order_no": system,
                    "req_time_sequence": f"{ORDER_DETAIL_PATH}$$4",
                },
                headers={"Accept": "application/json", "Referer": ORDER_MANAGEMENT_URL},
                timeout=self._timeout_ms,
            )
            payload = await self._response_payload(response, operation="详情读取")
        except InternalOrderError:
            raise
        except Exception as exc:
            raise InternalOrderError("领星内部订单详情读取失败，请检查登录状态后重试。") from exc
        if str(payload.get("code") or "").strip() != "1":
            raise InternalOrderProtocolError("领星内部订单详情接口拒绝了读取请求。")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise InternalOrderProtocolError("领星内部订单详情缺少订单数据。")
        request_id = _optional_text(payload.get("require_id") or payload.get("request_id"))
        raw = copy.deepcopy(dict(data))
        detail = parse_internal_order_detail(
            raw,
            expected_system_order_no=system,
            expected_platform_order_no=platform,
            request_id=request_id,
        )
        return _FetchedDetail(detail=detail, raw=raw)

    async def get_order_detail(
        self,
        system_order_no: str,
        expected_platform_order_no: str,
    ) -> InternalOrderDetail:
        return (await self._fetch(system_order_no, expected_platform_order_no)).detail

    async def raw_order_detail(
        self,
        system_order_no: str,
        expected_platform_order_no: str = "",
    ) -> dict[str, Any]:
        """Compatibility bridge; new business code must use the stable DTO."""

        platform = str(expected_platform_order_no or "").strip()
        if platform:
            return (await self._fetch(system_order_no, platform)).raw
        # Legacy callers did not know the platform order number.  Read once,
        # discover it, then still parse and validate the system identity.
        system = str(system_order_no or "").strip()
        try:
            response = await self._request.get(
                f"{self._base_url}{ORDER_DETAIL_PATH}",
                params={
                    "global_order_no": system,
                    "req_time_sequence": f"{ORDER_DETAIL_PATH}$$4",
                },
                headers={"Accept": "application/json", "Referer": ORDER_MANAGEMENT_URL},
                timeout=self._timeout_ms,
            )
            payload = await self._response_payload(response, operation="详情读取")
        except InternalOrderError:
            raise
        except Exception as exc:
            raise InternalOrderError("领星内部订单详情读取失败，请检查登录状态后重试。") from exc
        data = payload.get("data") if str(payload.get("code") or "").strip() == "1" else None
        if not isinstance(data, Mapping):
            raise InternalOrderProtocolError("领星内部订单详情缺少订单数据。")
        if str(data.get("global_order_no") or "").strip() != system:
            raise InternalOrderProtocolError(
                "领星内部详情返回的系统单号与请求不一致。"
            )
        return copy.deepcopy(dict(data))

    async def update_contacts(
        self,
        system_order_no: str,
        expected_platform_order_no: str,
        patch: ContactPatch,
        *,
        expected_revision: str,
    ) -> ContactWriteOutcome:
        latest = await self._fetch(system_order_no, expected_platform_order_no)
        before = latest.detail.contact
        if patch.empty or _contact_matches(before, patch):
            return ContactWriteOutcome(
                status=ContactWriteStatus.ALREADY_CURRENT,
                attempted=False,
                before=before,
                after=before,
                message="内部订单详情已确认联系方式一致，无需保存。",
                request_id=latest.detail.request_id,
            )
        if not expected_revision or latest.detail.revision != expected_revision:
            return ContactWriteOutcome(
                status=ContactWriteStatus.CONFLICT,
                attempted=False,
                before=before,
                after=before,
                message="订单详情在确认后发生变化，本次未提交联系方式修改。",
                request_id=latest.detail.request_id,
            )

        payload = build_order_edit_payload(latest.raw, patch)
        request_id = latest.detail.request_id
        try:
            response = await self._request.post(
                f"{self._base_url}{ORDER_EDIT_PATH}",
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Referer": ORDER_MANAGEMENT_URL,
                },
                timeout=self._timeout_ms,
            )
            post_payload = await self._response_payload(response, operation="联系方式写入")
            request_id = _optional_text(
                post_payload.get("require_id") or post_payload.get("request_id")
            ) or request_id
        except InternalOrderAuthenticationError as exc:
            return ContactWriteOutcome(
                status=ContactWriteStatus.REJECTED,
                attempted=True,
                before=before,
                after=None,
                message=str(exc),
                request_id=request_id,
            )
        except InternalOrderProtocolError as exc:
            return ContactWriteOutcome(
                status=ContactWriteStatus.REJECTED,
                attempted=True,
                before=before,
                after=None,
                message=str(exc),
                request_id=request_id,
            )
        except Exception:
            return ContactWriteOutcome(
                status=ContactWriteStatus.INCONCLUSIVE,
                attempted=True,
                before=before,
                after=None,
                message="联系方式提交结果未知，已停止重复写入并等待人工复核。",
                request_id=request_id,
            )
        if str(post_payload.get("code") or "").strip() != "1":
            return ContactWriteOutcome(
                status=ContactWriteStatus.REJECTED,
                attempted=True,
                before=before,
                after=None,
                message="领星内部订单接口拒绝了联系方式修改。",
                request_id=request_id,
            )

        attempts = 0
        waited = 0.0
        last_after: ContactSnapshot | None = None
        for delay in self._readback_delays:
            if delay:
                await self._sleeper(delay)
                waited += delay
            attempts += 1
            try:
                observed = await self._fetch(system_order_no, expected_platform_order_no)
            except InternalOrderError:
                continue
            last_after = observed.detail.contact
            request_id = observed.detail.request_id or request_id
            if _contact_matches(last_after, patch):
                return ContactWriteOutcome(
                    status=ContactWriteStatus.CONFIRMED_APPLIED,
                    attempted=True,
                    before=before,
                    after=last_after,
                    message="联系方式已写入，并通过内部订单详情复核。",
                    request_id=request_id,
                    attempts=attempts,
                    waited_seconds=waited,
                )
        return ContactWriteOutcome(
            status=ContactWriteStatus.INCONCLUSIVE,
            attempted=True,
            before=before,
            after=last_after,
            message="联系方式提交后未在内部订单详情中确认，已停止重复写入并等待人工复核。",
            request_id=request_id,
            attempts=attempts,
            waited_seconds=waited,
        )


__all__ = [
    "DEFAULT_READBACK_DELAYS_SECONDS",
    "InternalOrderAuthenticationError",
    "InternalOrderError",
    "InternalOrderProtocolError",
    "LingxingInternalOrderClient",
    "build_order_edit_payload",
    "normalize_email",
    "normalize_phone",
    "parse_internal_order_detail",
]
