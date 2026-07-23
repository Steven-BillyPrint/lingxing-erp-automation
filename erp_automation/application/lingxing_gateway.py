"""Project-facing, capability-routed Lingxing OpenAPI gateway.

The OpenAPI client deliberately stays close to Lingxing's wire contracts.  This
module gives the rest of the application stable result types, conservative
write-state semantics, and the browser fallbacks that are actually supported by
the project.  It does not invent endpoints: buyer-email editing remains a
browser-only capability because no official OpenAPI endpoint is available.
"""

from __future__ import annotations

import base64
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from erp_automation.integrations.lingxing import (
    APIResponse,
    BinaryResponse,
    LingxingAPIError,
    LingxingAmbiguousWriteError,
    LingxingAuthError,
    LingxingConfigurationError,
    LingxingError,
    LingxingHTTPError,
    LingxingOpenAPIClient,
    LingxingProtocolError,
    LingxingTransportError,
)

from .capabilities import (
    Capability,
    CapabilityRouter,
    CapabilityUnavailable,
    MaybeAsync,
    MutationResult,
    MutationState,
)


T = TypeVar("T")
ReadFallback = Callable[[], MaybeAsync[T]]
MutationFallback = Callable[[], MaybeAsync[MutationResult]]
FallbackApproval = Callable[[Capability, MutationResult | None], MaybeAsync[bool]]


# ``getFastOutboundResult`` returns two arrays rather than the common ``list``
# envelope.  Keep the public return value as a tuple for compatibility and add
# this private-looking-but-documented discriminator to normalized copies only.
# The original response mappings are never mutated.
FAST_OUTBOUND_RESULT_STATE_KEY = "_lingxing_result_state"
FAST_OUTBOUND_SUCCEEDED = "success"
FAST_OUTBOUND_FAILED = "failure"


@dataclass(frozen=True)
class PageResult(Generic[T]):
    items: tuple[T, ...]
    offset: int
    length: int
    total: int | None = None
    request_id: str | None = None

    @property
    def next_offset(self) -> int | None:
        consumed = self.offset + len(self.items)
        if self.total is not None:
            return consumed if consumed < self.total else None
        return consumed if len(self.items) >= self.length else None


@dataclass(frozen=True)
class OrderRecord:
    """An order row with stable identifiers and its documented payload."""

    global_order_no: str | None
    order_number: str | None
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class OrderPage:
    items: tuple[OrderRecord, ...]
    offset: int
    length: int
    total: int | None = None
    request_id: str | None = None

    @property
    def next_offset(self) -> int | None:
        consumed = self.offset + len(self.items)
        if self.total is not None:
            return consumed if consumed < self.total else None
        return consumed if len(self.items) >= self.length else None


@dataclass(frozen=True)
class OrderDetail:
    order_number: str
    payload: Mapping[str, Any] = field(repr=False)
    request_id: str | None = None


@dataclass(frozen=True)
class AttachmentData:
    content: bytes = field(repr=False)
    filename: str | None = None
    content_type: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class LookupRecord:
    identifier: str | None
    name: str | None
    payload: Mapping[str, Any] = field(repr=False)


class VerificationOutcome(StrEnum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class MutationVerification:
    """Result returned by a caller-owned read-after-write verifier.

    ``CONFIRMED_NOT_APPLIED`` is a strong assertion: it authorizes a later,
    explicitly approved browser fallback.  Eventual-consistency or unavailable
    reads must return ``INCONCLUSIVE`` instead.
    """

    outcome: VerificationOutcome
    message: str = ""
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


VerificationHook = Callable[[MutationResult], MaybeAsync[MutationVerification]]


_AUTHENTICATION_REJECTION_CODES = frozenset(
    {
        "2001001",  # AppID rejected before a business request is accepted.
        "2001002",  # AppSecret rejected.
        "2001003",  # access token rejected.
        "2001005",  # access token does not match the app.
        "2001006",  # signature rejected.
        "2001007",  # signature timestamp rejected.
    }
)


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _required_text(value: object, label: str) -> str:
    normalized = _text(value)
    if normalized is None:
        raise ValueError(f"{label}不能为空。")
    return normalized


def _mapping_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items()}


def _response_mapping(response: APIResponse, operation: str) -> dict[str, Any]:
    if not isinstance(response.data, Mapping):
        raise CapabilityUnavailable(f"领星 API 的{operation}响应格式不符合预期。")
    return _mapping_copy(response.data)


def _non_negative_integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and normalized.isascii() and normalized.isdecimal():
            try:
                return int(normalized)
            except ValueError:
                return None
    return None


def _page_payload(
    response: APIResponse,
    operation: str,
) -> tuple[list[Mapping[str, Any]], int | None]:
    data = response.data
    total: int | None = None
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, Mapping):
        raw_items = data.get("list")
        total = _non_negative_integer(data.get("total"))
    else:
        raise CapabilityUnavailable(f"领星 API 的{operation}响应格式不符合预期。")
    if total is None and isinstance(response.raw, Mapping):
        total = _non_negative_integer(response.raw.get("total"))
    if not isinstance(raw_items, list):
        raise CapabilityUnavailable(f"领星 API 的{operation}响应缺少 list。")
    if any(not isinstance(item, Mapping) for item in raw_items):
        raise CapabilityUnavailable(f"领星 API 的{operation}列表包含无效数据。")
    return list(raw_items), total


async def _resolve(value: MaybeAsync[T]) -> T:
    return await value if inspect.isawaitable(value) else value


class LingxingGateway:
    """Capability-aware API adapter used by the desktop application."""

    buyer_email_api_supported = False

    def __init__(self, client: LingxingOpenAPIClient, router: CapabilityRouter) -> None:
        self.client = client
        self.router = router

    # Read operations -------------------------------------------------------------

    async def list_orders(
        self,
        *,
        offset: int = 0,
        length: int = 500,
        filters: Mapping[str, Any] | None = None,
        browser: ReadFallback[OrderPage] | None = None,
    ) -> OrderPage:
        if offset < 0:
            raise ValueError("offset 不能小于 0。")
        if length <= 0:
            raise ValueError("length 必须大于 0。")

        async def api_read() -> OrderPage:
            response = await self._read_api(
                "订单列表",
                lambda: self.client.list_orders(
                    offset=offset,
                    length=length,
                    **dict(filters or {}),
                ),
            )
            rows, total = _page_payload(response, "订单列表")
            orders = tuple(
                OrderRecord(
                    global_order_no=_text(row.get("global_order_no")),
                    order_number=_text(row.get("order_number")),
                    payload=_mapping_copy(row),
                )
                for row in rows
            )
            return OrderPage(
                items=orders,
                offset=offset,
                length=length,
                total=total,
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.LIST_ORDERS,
            api=api_read,
            browser=browser,
        )

    async def get_order_detail(
        self,
        order_number: str,
        *,
        browser: ReadFallback[OrderDetail] | None = None,
    ) -> OrderDetail:
        normalized_order_number = _required_text(order_number, "订单号")

        async def api_read() -> OrderDetail:
            response = await self._read_api(
                "订单详情",
                lambda: self.client.get_fbm_order_detail(normalized_order_number),
            )
            return OrderDetail(
                order_number=normalized_order_number,
                payload=_response_mapping(response, "订单详情"),
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.GET_ORDER_DETAIL,
            api=api_read,
            browser=browser,
        )

    async def download_order_attachment(
        self,
        file_id: str | int,
        *,
        browser: ReadFallback[AttachmentData] | None = None,
    ) -> AttachmentData:
        """Download a file referenced by FBM order ``newAttachments``."""

        normalized_file_id = _required_text(file_id, "订单附件 ID")

        async def api_read() -> AttachmentData:
            try:
                response = await self.client.download_order_attachment(normalized_file_id)
            except LingxingError as exc:
                raise CapabilityUnavailable(
                    self._read_error(
                        "订单附件下载 [/filestream/api/cepf/attachment/download]",
                        exc,
                    )
                ) from None
            if not isinstance(response, BinaryResponse):
                raise CapabilityUnavailable("领星 API 的订单附件下载响应格式不符合预期。")
            return AttachmentData(
                content=response.content,
                filename=response.filename,
                content_type=response.content_type,
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.DOWNLOAD_ATTACHMENT,
            api=api_read,
            browser=browser,
        )

    async def download_attachment(
        self,
        file_id: str | int,
        *,
        browser: ReadFallback[AttachmentData] | None = None,
    ) -> AttachmentData:
        """Compatibility alias for :meth:`download_order_attachment`."""

        return await self.download_order_attachment(file_id, browser=browser)

    async def download_custom_attachment(
        self,
        file_id: str | int,
        *,
        browser: ReadFallback[AttachmentData] | None = None,
    ) -> AttachmentData:
        """Download a customization bundle through the dedicated official API."""

        normalized_file_id = _required_text(file_id, "定制附件 ID")

        async def api_read() -> AttachmentData:
            try:
                response = await self.client.download_custom_attachment(normalized_file_id)
            except LingxingError as exc:
                raise CapabilityUnavailable(self._read_error("定制附件下载", exc)) from None
            if not isinstance(response, APIResponse):
                raise CapabilityUnavailable("领星 API 的定制附件下载响应格式不符合预期。")
            rows = response.data
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
                raise CapabilityUnavailable("领星 API 的定制附件响应必须且只能包含一个文件。")
            row = rows[0]
            encoded = row.get("content")
            if not isinstance(encoded, str) or not encoded:
                raise CapabilityUnavailable("领星 API 的定制附件缺少 base64 内容。")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise CapabilityUnavailable("领星 API 的定制附件内容不是有效 base64。") from None
            raw_filename = _text(row.get("file_name"))
            filename = Path(raw_filename.replace("\\", "/")).name if raw_filename else None
            return AttachmentData(
                content=content,
                filename=filename,
                content_type=_text(row.get("mime_type")),
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.DOWNLOAD_ATTACHMENT,
            api=api_read,
            browser=browser,
        )

    async def list_warehouses(
        self,
        *,
        warehouse_type: int = 1,
        sub_type: int | None = None,
        is_delete: int | str = 0,
        offset: int = 0,
        length: int = 1000,
        browser: ReadFallback[PageResult[LookupRecord]] | None = None,
    ) -> PageResult[LookupRecord]:
        async def api_read() -> PageResult[LookupRecord]:
            response = await self._read_api(
                "仓库列表",
                lambda: self.client.list_warehouses(
                    warehouse_type=warehouse_type,
                    sub_type=sub_type,
                    is_delete=is_delete,
                    offset=offset,
                    length=length,
                ),
            )
            rows, total = _page_payload(response, "仓库列表")
            return PageResult(
                items=tuple(self._lookup_record(row, id_keys=("wid", "warehouse_id", "id")) for row in rows),
                offset=offset,
                length=length,
                total=total,
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.SET_SHIPPING_CHANNEL,
            api=api_read,
            browser=browser,
        )

    async def list_logistics_types(
        self,
        *,
        provider_type: int,
        page: int = 1,
        length: int = 100,
        browser: ReadFallback[PageResult[LookupRecord]] | None = None,
    ) -> PageResult[LookupRecord]:
        if page <= 0 or length <= 0:
            raise ValueError("page 和 length 必须大于 0。")

        async def api_read() -> PageResult[LookupRecord]:
            response = await self._read_api(
                "物流渠道列表",
                lambda: self.client.list_logistics_types(
                    provider_type=provider_type,
                    page=page,
                    length=length,
                ),
            )
            rows, total = _page_payload(response, "物流渠道列表")
            return PageResult(
                items=tuple(
                    self._lookup_record(
                        row,
                        id_keys=(
                            "logistics_type_id",
                            "type_id",
                            "logistics_id",
                            "id",
                        ),
                    )
                    for row in rows
                ),
                offset=(page - 1) * length,
                length=length,
                total=total,
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.SET_SHIPPING_CHANNEL,
            api=api_read,
            browser=browser,
        )

    async def list_wms_orders(
        self,
        *,
        filters: Mapping[str, Any] | None = None,
        offset: int = 0,
        length: int = 100,
        browser: ReadFallback[PageResult[Mapping[str, Any]]] | None = None,
    ) -> PageResult[Mapping[str, Any]]:
        async def api_read() -> PageResult[Mapping[str, Any]]:
            request_filters = dict(filters or {})
            # This endpoint is an exception to the other paginated APIs: the
            # documented wire contract uses page/page_size and caps page_size
            # at 200.  Do not send undocumented offset/length fields.
            request_filters.setdefault("page", (offset // length) + 1)
            request_filters.setdefault("page_size", min(length, 200))
            response = await self._read_api(
                "WMS 运单列表",
                lambda: self.client.list_wms_orders(**request_filters),
            )
            rows, total = _page_payload(response, "WMS 运单列表")
            return PageResult(
                items=tuple(_mapping_copy(row) for row in rows),
                offset=offset,
                length=length,
                total=total,
                request_id=response.request_id,
            )

        return await self.router.execute_read(
            Capability.LIST_ORDERS,
            api=api_read,
            browser=browser,
        )

    async def get_fast_outbound_result(
        self,
        global_order_nos: Sequence[str | int],
        *,
        browser: ReadFallback[tuple[Mapping[str, Any], ...]] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        normalized = self._identifier_list(global_order_nos, "领星订单号")

        async def api_read() -> tuple[Mapping[str, Any], ...]:
            response = await self._read_api(
                "快速出库结果",
                lambda: self.client.get_fast_outbound_result(normalized),
            )
            data = response.data
            if isinstance(data, list):
                rows = data
            elif isinstance(data, Mapping) and isinstance(data.get("list"), list):
                rows = data["list"]
            elif isinstance(data, Mapping):
                success = data.get("success")
                failure = data.get("failure")
                if not isinstance(success, list) or not isinstance(failure, list):
                    raise CapabilityUnavailable("领星 API 的快速出库结果响应格式不符合预期。")
                if any(not isinstance(row, Mapping) for row in [*success, *failure]):
                    raise CapabilityUnavailable("领星 API 的快速出库结果包含无效数据。")
                rows = [
                    {**_mapping_copy(row), FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED}
                    for row in success
                ]
                rows.extend(
                    {
                        **_mapping_copy(row),
                        FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
                    }
                    for row in failure
                )
            else:
                raise CapabilityUnavailable("领星 API 的快速出库结果响应格式不符合预期。")
            if any(not isinstance(row, Mapping) for row in rows):
                raise CapabilityUnavailable("领星 API 的快速出库结果包含无效数据。")
            return tuple(_mapping_copy(row) for row in rows)

        return await self.router.execute_read(
            Capability.OUTBOUND_ORDER,
            api=api_read,
            browser=browser,
        )

    # Write operations ------------------------------------------------------------

    async def update_phone(
        self,
        global_order_no: str | int,
        receiver_tel: str,
        *,
        order_item_list: Sequence[Mapping[str, Any]],
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        order_no = _required_text(global_order_no, "领星订单号")
        phone = _required_text(receiver_tel, "收件电话")
        items = [_mapping_copy(item) for item in order_item_list]
        payload = {
            "global_order_no": order_no,
            "address_info": {"receiver_tel": phone},
            "order_item_list": items,
        }
        return await self._route_write(
            Capability.UPDATE_PHONE,
            "更新电话",
            lambda: self.client.update_orders([payload]),
            validate_response=lambda result: self._validate_update_order_ack(result, order_no),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def update_buyer_email(
        self,
        *,
        browser: MutationFallback | None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        """Use the browser implementation; Lingxing exposes no official API."""

        if browser is None:
            raise CapabilityUnavailable(
                "领星官方 OpenAPI 没有买家邮箱修改接口，请使用保留的网页流程。"
            )
        return await self.router.execute_write(
            Capability.UPDATE_BUYER_EMAIL,
            api=None,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def set_order_remark(
        self,
        global_order_no: str | int,
        remark: str,
        *,
        append: bool = False,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        order_no = _required_text(global_order_no, "领星订单号")
        text = _required_text(remark, "备注")
        payload = {
            "global_order_no": order_no,
            "remark": text,
            "remark_is_append": bool(append),
        }
        return await self._route_write(
            Capability.UPDATE_REMARK,
            "更新备注",
            lambda: self.client.set_order_remarks([payload]),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def update_order_items(
        self,
        global_order_no: str | int,
        order_item_list: Sequence[Mapping[str, Any]],
        *,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        order_no = _required_text(global_order_no, "领星订单号")
        items = [_mapping_copy(item) for item in order_item_list]
        if not items:
            raise ValueError("订单商品列表不能为空。")
        return await self._route_write(
            Capability.UPDATE_ORDER_ITEMS,
            "编辑订单商品",
            lambda: self.client.update_orders(
                [{"global_order_no": order_no, "order_item_list": items}]
            ),
            validate_response=lambda result: self._validate_update_order_ack(result, order_no),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def split_order(
        self,
        global_order_no: str | int,
        package_groups: Sequence[Sequence[Mapping[str, Any]]],
        *,
        split_mod: int = 1,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        order_no = _required_text(global_order_no, "领星订单号")
        groups = [
            [_mapping_copy(item) for item in group]
            for group in package_groups
        ]
        if len(groups) < 2 or any(not group for group in groups):
            raise ValueError("拆单至少需要两个非空包裹组。")
        return await self._route_write(
            Capability.SPLIT_ORDER,
            "拆单",
            lambda: self.client.split_order(
                split_mod=split_mod,
                global_order_no=order_no,
                order_item=groups,
            ),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def set_shipping_channel(
        self,
        order_list: Sequence[Mapping[str, Any]],
        *,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        orders = [_mapping_copy(item) for item in order_list]
        if not orders:
            raise ValueError("物流修改订单列表不能为空。")
        return await self._route_write(
            Capability.SET_SHIPPING_CHANNEL,
            "修改物流渠道",
            lambda: self.client.edit_order_logistics(orders),
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def review_orders(
        self,
        global_order_nos: Sequence[str | int],
        *,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        order_nos = self._identifier_list(global_order_nos, "领星订单号")
        return await self._route_write(
            Capability.REVIEW_ORDER,
            "审核订单",
            lambda: self.client.review_orders(order_nos),
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

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
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        waybill = _required_text(waybill_no, "运单号")
        work_order = _required_text(wo_number, "物流商单号")
        return await self._route_write(
            Capability.UPDATE_TRACKING,
            "更新运单",
            lambda: self.client.set_tracking_no(
                waybill_no=waybill,
                wo_number=work_order,
                tracking_no=_text(tracking_no),
                logistics_freight=logistics_freight,
                logistics_freight_currency_code=_text(logistics_freight_currency_code),
                pkg_fee_weight=pkg_fee_weight,
                pkg_fee_weight_unit=_text(pkg_fee_weight_unit),
            ),
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def deliver_orders(
        self,
        order_numbers: Sequence[str | int],
        *,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        normalized = self._identifier_list(order_numbers, "系统订单号")
        return await self._route_write(
            Capability.OUTBOUND_ORDER,
            "自发货出库",
            lambda: self.client.deliver_orders(normalized),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    async def fast_outbound(
        self,
        packages: Sequence[Mapping[str, Any]],
        *,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None = None,
        approve_browser_fallback: FallbackApproval | None = None,
    ) -> MutationResult:
        normalized = [_mapping_copy(package) for package in packages]
        if not normalized:
            raise ValueError("快速出库包裹列表不能为空。")
        return await self._route_write(
            Capability.OUTBOUND_ORDER,
            "快速出库",
            lambda: self.client.fast_outbound(normalized),
            verify=verify,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    # Internal normalization ------------------------------------------------------

    async def _read_api(
        self,
        operation: str,
        call: Callable[[], Awaitable[APIResponse]],
    ) -> APIResponse:
        try:
            response = await call()
        except LingxingError as exc:
            raise CapabilityUnavailable(self._read_error(operation, exc)) from None
        if not isinstance(response, APIResponse):
            raise CapabilityUnavailable(f"领星 API 的{operation}响应格式不符合预期。")
        return response

    @staticmethod
    def _read_error(operation: str, exc: LingxingError) -> str:
        details = [f"error={exc.__class__.__name__}"]
        request_id = _text(getattr(exc, "request_id", None))
        payload = getattr(exc, "payload", None)
        trace_id: str | None = None
        if isinstance(exc, LingxingAPIError):
            details.append(f"operation={exc.operation}")
            details.append(f"code={exc.code}")
            server_message = " ".join(str(exc.server_message or "").split())[:240]
            if server_message:
                details.append(f"message={server_message}")
            if isinstance(payload, Mapping):
                trace_id = _text(payload.get("traceId") or payload.get("trace_id"))
        elif isinstance(exc, LingxingHTTPError):
            details.append(f"status={exc.status_code}")
        if request_id:
            details.append(f"request_id={request_id}")
        if trace_id:
            details.append(f"traceId={trace_id}")
        return f"领星 API {operation}失败（{', '.join(details)}）。"

    async def _route_write(
        self,
        capability: Capability,
        operation: str,
        call: Callable[[], Awaitable[APIResponse]],
        *,
        validate_response: Callable[[MutationResult], MutationResult] | None = None,
        verify: VerificationHook | None = None,
        browser: MutationFallback | None,
        approve_browser_fallback: FallbackApproval | None,
    ) -> MutationResult:
        async def api_write() -> MutationResult:
            initial = await self._mutation(operation, call)
            if validate_response is not None:
                initial = validate_response(initial)
            if initial.details.get("verification_blocked_by_ack"):
                # A structurally ambiguous documented acknowledgement cannot
                # be promoted to success by a later readback.  It remains an
                # UNKNOWN/manual-review result so operators do not repeat it.
                return initial
            if verify is None or initial.state not in {
                MutationState.SUCCEEDED,
                MutationState.UNKNOWN,
                MutationState.MANUAL_REVIEW,
            }:
                return initial
            return await self._verify_mutation(initial, verify)

        return await self.router.execute_write(
            capability,
            api=api_write,
            browser=browser,
            approve_browser_fallback=approve_browser_fallback,
        )

    @staticmethod
    def _validate_update_order_ack(
        initial: MutationResult,
        target_global_order_no: str,
    ) -> MutationResult:
        """Interpret updateOrder's per-order ``error_details`` envelope."""

        api_code = str(initial.details.get("api_code") or "")
        partial_exception = (
            initial.state is MutationState.UNKNOWN
            and api_code in {"10000", "10001"}
        )
        if initial.state is not MutationState.SUCCEEDED and not partial_exception:
            return initial
        data = initial.details.get("data")
        if not isinstance(data, Mapping):
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="updateOrder 响应缺少可验证的 data.error_details，必须人工复核。",
                details={
                    **dict(initial.details),
                    "ack_validation": "missing_data",
                    "verification_blocked_by_ack": True,
                    "browser_fallback_forbidden": True,
                },
            )
        errors = data.get("error_details")
        if not isinstance(errors, list):
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="updateOrder 响应的 error_details 结构不明确，必须人工复核。",
                details={
                    **dict(initial.details),
                    "ack_validation": "invalid_error_details",
                    "verification_blocked_by_ack": True,
                    "browser_fallback_forbidden": True,
                },
            )
        if not errors and not partial_exception:
            return initial
        if not errors:
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="updateOrder 部分结果缺少可对应订单的失败详情，必须人工复核。",
                details={
                    **dict(initial.details),
                    "ack_validation": "partial_without_errors",
                    "verification_blocked_by_ack": True,
                    "browser_fallback_forbidden": True,
                },
            )
        if any(not isinstance(row, Mapping) for row in errors):
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="updateOrder 返回了无法对应订单的失败详情，必须人工复核。",
                details={
                    **dict(initial.details),
                    "ack_validation": "ambiguous_errors",
                    "verification_blocked_by_ack": True,
                    "browser_fallback_forbidden": True,
                },
            )
        matched = [
            row
            for row in errors
            if _text(
                row.get("global_order_no")
                or row.get("globalOrderNo")
                or row.get("order_number")
            )
            == target_global_order_no
        ]
        if matched:
            return MutationResult(
                state=MutationState.FAILED,
                source=initial.source,
                request_id=initial.request_id,
                message=f"updateOrder 明确报告系统订单 {target_global_order_no} 更新失败。",
                definitely_not_executed=True,
                details={
                    **dict(initial.details),
                    "ack_validation": "target_failed",
                    "verification_blocked_by_ack": True,
                    "browser_fallback_forbidden": True,
                },
            )
        return MutationResult(
            state=MutationState.UNKNOWN,
            source=initial.source,
            request_id=initial.request_id,
            message="updateOrder 的失败详情不能唯一对应当前订单，必须人工复核。",
            details={
                **dict(initial.details),
                "ack_validation": "target_ambiguous",
                "verification_blocked_by_ack": True,
                "browser_fallback_forbidden": True,
            },
        )

    async def _mutation(
        self,
        operation: str,
        call: Callable[[], Awaitable[APIResponse]],
    ) -> MutationResult:
        try:
            response = await call()
        except LingxingAuthError as exc:
            return self._failed_before_execution(operation, exc)
        except LingxingConfigurationError as exc:
            return self._failed_before_execution(operation, exc)
        except LingxingAPIError as exc:
            if exc.code in _AUTHENTICATION_REJECTION_CODES:
                return self._failed_before_execution(operation, exc)
            exception_details: dict[str, Any] = {}
            if isinstance(exc.payload, Mapping):
                # updateOrder uses application-level partial codes (notably
                # 10000/10001) while carrying the actionable per-order result
                # in payload.data.error_details.  Preserve that envelope so
                # the operation-specific acknowledgement validator can decide
                # whether this exact target was rejected.
                api_payload = dict(exc.payload)
                exception_details["api_payload"] = api_payload
                if "data" in api_payload:
                    exception_details["data"] = api_payload.get("data")
            return self._unknown(
                operation,
                exc,
                api_code=exc.code,
                extra_details=exception_details,
            )
        except (
            LingxingAmbiguousWriteError,
            LingxingTransportError,
            LingxingHTTPError,
            LingxingProtocolError,
        ) as exc:
            return self._unknown(operation, exc)
        except LingxingError as exc:
            # Future integration-layer errors are conservative by default: a
            # business request may already have crossed the process boundary.
            return self._unknown(operation, exc)
        except (TimeoutError, ConnectionError) as exc:
            return self._unknown(operation, exc)

        if not isinstance(response, APIResponse):
            return MutationResult(
                state=MutationState.UNKNOWN,
                source="lingxing_api",
                message=f"{operation}收到无法确认的响应，禁止自动网页重试。",
            )
        details: dict[str, Any] = {"operation": operation, "api_code": response.code}
        if response.data is not None:
            details["data"] = response.data
        return MutationResult(
            state=MutationState.SUCCEEDED,
            source="lingxing_api",
            request_id=response.request_id,
            message=response.message or f"{operation}已由领星 API 接受。",
            after=_mapping_copy(response.data) if isinstance(response.data, Mapping) else None,
            details=details,
        )

    async def _verify_mutation(
        self,
        initial: MutationResult,
        verify: VerificationHook,
    ) -> MutationResult:
        try:
            verification = await _resolve(verify(initial))
        except Exception:
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="执行后的读回验证失败，结果仍不明确，禁止自动网页重试。",
                before=initial.before,
                after=initial.after,
                details={**dict(initial.details), "verification": "failed"},
            )
        if not isinstance(verification, MutationVerification):
            return MutationResult(
                state=MutationState.UNKNOWN,
                source=initial.source,
                request_id=initial.request_id,
                message="读回验证器返回了无效结果，禁止自动网页重试。",
                details={**dict(initial.details), "verification": "invalid"},
            )
        combined_details = {
            **dict(initial.details),
            **dict(verification.details),
            "verification": verification.outcome.value,
        }
        if verification.outcome is VerificationOutcome.CONFIRMED_APPLIED:
            return MutationResult(
                state=MutationState.SUCCEEDED,
                source=initial.source,
                request_id=initial.request_id,
                message=verification.message or "已通过读回确认写入生效。",
                before=verification.before or initial.before,
                after=verification.after or initial.after,
                details=combined_details,
            )
        if verification.outcome is VerificationOutcome.CONFIRMED_NOT_APPLIED:
            return MutationResult(
                state=MutationState.FAILED,
                source=initial.source,
                request_id=initial.request_id,
                message=verification.message or "已通过读回确认写入未生效。",
                before=verification.before or initial.before,
                after=verification.after,
                definitely_not_executed=True,
                details=combined_details,
            )
        return MutationResult(
            state=MutationState.UNKNOWN,
            source=initial.source,
            request_id=initial.request_id,
            message=verification.message or "读回结果不足以确认写入状态，禁止自动网页重试。",
            before=verification.before or initial.before,
            after=verification.after or initial.after,
            details=combined_details,
        )

    @staticmethod
    def _failed_before_execution(operation: str, exc: LingxingError) -> MutationResult:
        request_id = getattr(exc, "request_id", None)
        code = getattr(exc, "code", None)
        details = {"operation": operation}
        if code is not None:
            details["api_code"] = str(code)
        return MutationResult(
            state=MutationState.FAILED,
            source="lingxing_api",
            request_id=request_id,
            message=f"{operation}在业务请求执行前被拒绝。",
            definitely_not_executed=True,
            details=details,
        )

    @staticmethod
    def _unknown(
        operation: str,
        exc: BaseException,
        *,
        api_code: str | None = None,
        extra_details: Mapping[str, Any] | None = None,
    ) -> MutationResult:
        request_id = getattr(exc, "request_id", None)
        details: dict[str, Any] = {"operation": operation}
        if api_code is not None:
            details["api_code"] = api_code
        if extra_details:
            details.update(dict(extra_details))
        return MutationResult(
            state=MutationState.UNKNOWN,
            source="lingxing_api",
            request_id=request_id,
            message=f"{operation}结果不明确，必须读回或人工核对，禁止自动网页重试。",
            details=details,
        )

    @staticmethod
    def _identifier_list(values: Sequence[str | int], label: str) -> list[str]:
        normalized = [_required_text(value, label) for value in values]
        if not normalized:
            raise ValueError(f"{label}列表不能为空。")
        return normalized

    @staticmethod
    def _lookup_record(
        row: Mapping[str, Any],
        *,
        id_keys: Sequence[str],
    ) -> LookupRecord:
        identifier = next((_text(row.get(key)) for key in id_keys if _text(row.get(key))), None)
        name = next(
            (
                _text(row.get(key))
                for key in ("name", "warehouse_name", "logistics_name", "logistics_type_name")
                if _text(row.get(key))
            ),
            None,
        )
        return LookupRecord(identifier=identifier, name=name, payload=_mapping_copy(row))


__all__ = [
    "AttachmentData",
    "FAST_OUTBOUND_FAILED",
    "FAST_OUTBOUND_RESULT_STATE_KEY",
    "FAST_OUTBOUND_SUCCEEDED",
    "LingxingGateway",
    "LookupRecord",
    "MutationVerification",
    "OrderDetail",
    "OrderPage",
    "OrderRecord",
    "PageResult",
    "VerificationHook",
    "VerificationOutcome",
]
