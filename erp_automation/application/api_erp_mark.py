"""Lingxing OpenAPI implementation of the shipment ERP-mark callback.

The callback matches ``shipment_automation.erp_mark_ship.MarkItemFunc`` and is
therefore injected through ``args.mark_item_func``.  It never opens a browser
and never falls back to one.  Every write is gated by the gateway kill switch,
an explicit user confirmation, strict response inspection, and (where the API
supports it) a bounded read-after-write check.

Logistics route identifiers are deliberately configuration, not heuristics.
Carrier display names are not sufficient to safely infer warehouse/provider
IDs, so an unmapped carrier becomes a manual-review item.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from shipment_automation.alibaba_logistics import normalize_carrier_name
from shipment_automation.erp_mark_ship import (
    CHECKPOINT_RANK,
    ErpMarkManualReview,
    ErpMarkUserAbort,
    clean_money_amount,
    format_chargeable_weight_g,
    validate_ready_item,
)
from shipment_automation.models import (
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_NONE,
    ERP_CHECKPOINT_OUTBOUNDED,
    ReadyToMarkItem,
)

from .capabilities import (
    CapabilityUnavailable,
    ManualReviewRequired,
    MutationResult,
    MutationState,
)
from .lingxing_gateway import (
    FAST_OUTBOUND_FAILED,
    FAST_OUTBOUND_RESULT_STATE_KEY,
    FAST_OUTBOUND_SUCCEEDED,
    LingxingGateway,
)


ConfirmFunc = Callable[[str], Awaitable[bool]]
SleepFunc = Callable[[float], Awaitable[None]]
ConfigurationProvider = Callable[[], Mapping[str, Any]]


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


GatewayFactory = Callable[[], Awaitable[tuple[LingxingGateway, AsyncCloseable]]]


_SUPPORTED_FREIGHT_CURRENCIES = frozenset(
    {
        "CNY",
        "USD",
        "EUR",
        "JPY",
        "AUD",
        "CAD",
        "MXN",
        "GBP",
        "INR",
        "AED",
        "SGD",
        "SAR",
        "BRL",
        "SEK",
        "PLN",
        "TRY",
        "HKD",
    }
)


class OutboundStrategy(StrEnum):
    """Documented outbound route used by one adapter instance."""

    STAGED = "staged"
    FAST_OUTBOUND = "fast_outbound"


@dataclass(frozen=True)
class ErpLogisticsRoute:
    """Explicit Lingxing IDs for a normalized overseas carrier."""

    warehouse_id: int
    logistics_type_id: int
    # The fast-outbound API documents this field as a string and some accounts
    # use a provider/type compound value.  It must therefore be supplied as the
    # exact wire value instead of being constructed from other IDs.
    fast_logistics_type_id: str | None = None
    freight_currency_code: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.warehouse_id, bool) or int(self.warehouse_id) <= 0:
            raise ValueError("warehouse_id 必须是正整数。")
        if isinstance(self.logistics_type_id, bool) or int(self.logistics_type_id) <= 0:
            raise ValueError("logistics_type_id 必须是正整数。")
        object.__setattr__(self, "warehouse_id", int(self.warehouse_id))
        object.__setattr__(self, "logistics_type_id", int(self.logistics_type_id))
        if self.fast_logistics_type_id is not None:
            value = str(self.fast_logistics_type_id).strip()
            if not value:
                raise ValueError("fast_logistics_type_id 不能为空字符串。")
            object.__setattr__(self, "fast_logistics_type_id", value)
        if self.freight_currency_code is not None:
            currency = str(self.freight_currency_code).strip().upper()
            if currency not in _SUPPORTED_FREIGHT_CURRENCIES:
                raise ValueError(f"领星接口不支持物流运费币种：{currency or '-'}")
            object.__setattr__(self, "freight_currency_code", currency)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是正整数。")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是正整数。") from exc
    if parsed <= 0:
        raise ValueError(f"{label} 必须是正整数。")
    return parsed


def _nonnegative_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是大于等于 0 的数字。") from exc
    if parsed < 0:
        raise ValueError(f"{label} 必须是大于等于 0 的数字。")
    return parsed


def _configuration_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} 不是有效 JSON。") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} 必须是对象。")
    return value


def routes_from_configuration(configuration: Mapping[str, Any]) -> dict[str, ErpLogisticsRoute]:
    """Load strict per-carrier IDs from the encrypted configuration mapping.

    Expected shape::

        lingxing.erp_mark.routes = {
          "UPS": {
            "warehouse_id": 50,
            "logistics_type_id": 825,
            "fast_logistics_type_id": "6-825",
            "freight_currency_code": "USD"
          }
        }

    The value may also be a JSON string to ease migration from flat settings.
    No route ID is guessed when a key is missing.
    """

    raw = configuration.get("lingxing.erp_mark.routes", {})
    routes = _configuration_mapping(raw, "lingxing.erp_mark.routes")
    normalized: dict[str, ErpLogisticsRoute] = {}
    for carrier, route_value in routes.items():
        carrier_key = normalize_carrier_name(str(carrier))
        if not carrier_key:
            raise ValueError("物流映射包含空承运商名称。")
        route = _configuration_mapping(route_value, f"物流映射 {carrier_key}")
        warehouse_id = route.get("warehouse_id", route.get("sys_wid", route.get("wid")))
        logistics_type_id = route.get("logistics_type_id", route.get("type_id"))
        normalized[carrier_key] = ErpLogisticsRoute(
            warehouse_id=_positive_int(warehouse_id, f"{carrier_key}.warehouse_id"),
            logistics_type_id=_positive_int(
                logistics_type_id, f"{carrier_key}.logistics_type_id"
            ),
            fast_logistics_type_id=route.get("fast_logistics_type_id"),
            freight_currency_code=route.get("freight_currency_code"),
        )
    return normalized


class ApiErpMarkAdapter:
    """Callable API-only ERP mark-and-outbound workflow."""

    def __init__(
        self,
        gateway: LingxingGateway,
        routes: Mapping[str, ErpLogisticsRoute],
        *,
        outbound_strategy: OutboundStrategy | str = OutboundStrategy.STAGED,
        wms_poll_attempts: int = 5,
        wms_poll_interval_seconds: float = 1.0,
        fast_result_attempts: int = 10,
        fast_result_interval_seconds: float = 1.0,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> None:
        self.gateway = gateway
        self.routes = {
            normalize_carrier_name(carrier): route for carrier, route in routes.items()
        }
        self.outbound_strategy = OutboundStrategy(outbound_strategy)
        self.wms_poll_attempts = _positive_int(wms_poll_attempts, "wms_poll_attempts")
        self.wms_poll_interval_seconds = _nonnegative_float(
            wms_poll_interval_seconds, "wms_poll_interval_seconds"
        )
        self.fast_result_attempts = _positive_int(fast_result_attempts, "fast_result_attempts")
        self.fast_result_interval_seconds = _nonnegative_float(
            fast_result_interval_seconds, "fast_result_interval_seconds"
        )
        self.sleeper = sleeper

    @classmethod
    def from_configuration(
        cls,
        gateway: LingxingGateway,
        configuration: Mapping[str, Any],
        *,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> "ApiErpMarkAdapter":
        strategy = configuration.get(
            "lingxing.erp_mark.outbound_strategy", OutboundStrategy.STAGED
        )
        return cls(
            gateway,
            routes_from_configuration(configuration),
            outbound_strategy=str(strategy),
            wms_poll_attempts=_positive_int(
                configuration.get("lingxing.erp_mark.wms_poll_attempts", 5),
                "lingxing.erp_mark.wms_poll_attempts",
            ),
            wms_poll_interval_seconds=_nonnegative_float(
                configuration.get("lingxing.erp_mark.wms_poll_interval_seconds", 1),
                "lingxing.erp_mark.wms_poll_interval_seconds",
            ),
            fast_result_attempts=_positive_int(
                configuration.get("lingxing.erp_mark.fast_result_attempts", 10),
                "lingxing.erp_mark.fast_result_attempts",
            ),
            fast_result_interval_seconds=_nonnegative_float(
                configuration.get("lingxing.erp_mark.fast_result_interval_seconds", 1),
                "lingxing.erp_mark.fast_result_interval_seconds",
            ),
            sleeper=sleeper,
        )

    async def __call__(
        self,
        _page: Any,
        item: ReadyToMarkItem,
        confirm_func: ConfirmFunc,
    ) -> str:
        validate_ready_item(item)
        rank = CHECKPOINT_RANK.get(item.erp_checkpoint or ERP_CHECKPOINT_NONE)
        if rank is None:
            raise ErpMarkManualReview(f"队列包含未知 ERP 检查点：{item.erp_checkpoint}")
        if rank >= CHECKPOINT_RANK[ERP_CHECKPOINT_OUTBOUNDED]:
            return ERP_CHECKPOINT_OUTBOUNDED
        self._ensure_write_switch()
        route = self._route_for(item)
        if self.outbound_strategy is OutboundStrategy.FAST_OUTBOUND:
            if rank != CHECKPOINT_RANK[ERP_CHECKPOINT_NONE]:
                raise ErpMarkManualReview(
                    "订单已有分阶段标发检查点，禁止改用快速出库以免重复写入。"
                )
            return await self._fast_outbound(item, route, confirm_func)
        return await self._staged_outbound(item, route, rank, confirm_func)

    def _ensure_write_switch(self) -> None:
        router = getattr(self.gateway, "router", None)
        if router is not None and not bool(getattr(router, "writes_enabled", True)):
            raise ErpMarkManualReview(
                "领星 API ERP 写入紧急开关未开启，未执行任何写入。"
            )

    def _route_for(self, item: ReadyToMarkItem) -> ErpLogisticsRoute:
        carrier = normalize_carrier_name(item.carrier)
        route = self.routes.get(carrier)
        if route is None:
            raise ErpMarkManualReview(
                f"承运商 {carrier or item.carrier or '-'} 尚未配置明确的领星仓库/物流方式 ID，"
                "禁止按名称猜测。"
            )
        return route

    async def _staged_outbound(
        self,
        item: ReadyToMarkItem,
        route: ErpLogisticsRoute,
        rank: int,
        confirm_func: ConfirmFunc,
    ) -> str:
        freight, currency, fee_weight_g = self._logistics_values(item, route)
        # A successful external write followed by a local checkpoint failure
        # must not cause the next run to replay review/outbound.  The sales
        # outbound document is the authoritative read-before-write guard.
        existing_row = await self._preflight_wms_row(item)
        if existing_row is not None:
            existing_status = _status(existing_row)
            if existing_status == 3:
                return ERP_CHECKPOINT_OUTBOUNDED
            if existing_status not in {1, 2}:
                raise ErpMarkManualReview(
                    f"销售出库单当前状态 {existing_status!r} 不能安全自动续跑。"
                )
            rank = max(rank, CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED])
            tracking_present = bool(
                str(existing_row.get("waybill_no") or "").strip()
                or str(existing_row.get("tracking_no") or "").strip()
            )
            tracking_matches = self._tracking_matches(
                existing_row,
                item=item,
                freight=freight,
                currency=currency,
                fee_weight_g=fee_weight_g,
            )
            if tracking_matches:
                rank = max(rank, CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED])
            elif tracking_present or existing_status == 2:
                raise ErpMarkManualReview(
                    "销售出库单已有与本任务不一致的物流信息，禁止覆盖或继续出库。"
                )

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_CHANNEL_SET]:
            await self._confirm(
                confirm_func,
                item,
                "设置仓库物流",
                f"仓库 ID={route.warehouse_id}，物流方式 ID={route.logistics_type_id}",
            )
            channel = await self._write(
                "设置仓库物流",
                self.gateway.set_shipping_channel(
                    [
                        {
                            "global_order_no": item.system_order_no,
                            "logistics": {
                                "logistics_type_id": route.logistics_type_id,
                                "sys_wid": route.warehouse_id,
                            },
                        }
                    ],
                    browser=None,
                ),
            )
            self._validate_channel_response(channel, item)

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED]:
            await self._confirm(confirm_func, item, "审核发货", "审核后将生成销售出库单")
            review = await self._write(
                "审核发货",
                self.gateway.review_orders([item.system_order_no], browser=None),
            )
            self._validate_review_response(review, item)

        row = existing_row or await self._poll_wms_row(
            item, predicate=lambda value: True, action="读取销售出库单"
        )
        if _status(row) == 3:
            return ERP_CHECKPOINT_OUTBOUNDED

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]:
            await self._confirm(
                confirm_func,
                item,
                "写入运单/跟踪号",
                (
                    f"运单号={item.international_tracking_no}，跟踪号={item.logistics_no}，"
                    f"运费={freight} {currency or '(未指定币种)'}，计费重={fee_weight_g}g"
                ),
            )
            await self._write(
                "写入运单/跟踪号",
                self.gateway.set_tracking_no(
                    waybill_no=str(item.international_tracking_no),
                    wo_number=str(row.get("wo_number") or ""),
                    tracking_no=item.logistics_no,
                    logistics_freight=freight,
                    logistics_freight_currency_code=currency,
                    pkg_fee_weight=fee_weight_g,
                    pkg_fee_weight_unit="g",
                    browser=None,
                ),
            )

        row = await self._poll_wms_row(
            item,
            predicate=lambda value: self._tracking_matches(
                value,
                item=item,
                freight=freight,
                currency=currency,
                fee_weight_g=fee_weight_g,
            )
            and _status(value) in {2, 3},
            action="验证运单/跟踪号写入",
        )
        if _status(row) == 3:
            return ERP_CHECKPOINT_OUTBOUNDED

        await self._confirm(confirm_func, item, "出库发货", "该操作将扣减库存")
        delivery = await self._write(
            "出库发货",
            # The endpoint calls this field order_number_list; it is the ERP
            # system/global order number, not the marketplace order number.
            self.gateway.deliver_orders([item.system_order_no], browser=None),
        )
        self._validate_delivery_response(delivery, item)
        await self._poll_wms_row(
            item,
            predicate=lambda value: _status(value) == 3,
            action="验证出库结果",
        )
        return ERP_CHECKPOINT_OUTBOUNDED

    async def _fast_outbound(
        self,
        item: ReadyToMarkItem,
        route: ErpLogisticsRoute,
        confirm_func: ConfirmFunc,
    ) -> str:
        if not route.fast_logistics_type_id:
            raise ErpMarkManualReview(
                f"承运商 {normalize_carrier_name(item.carrier)} 未配置快速出库专用物流方式值，"
                "禁止拼接或猜测。"
            )
        previous_state, previous_message = await self._read_fast_outbound_state(item)
        if previous_state == FAST_OUTBOUND_SUCCEEDED:
            return ERP_CHECKPOINT_OUTBOUNDED
        if previous_state == FAST_OUTBOUND_FAILED and previous_message == "正在处理":
            await self._poll_fast_outbound_result(item)
            return ERP_CHECKPOINT_OUTBOUNDED
        if not (
            previous_state == FAST_OUTBOUND_FAILED
            and previous_message == "订单没提交快速出库"
        ):
            raise ErpMarkManualReview(
                "提交快速出库前无法证明该订单没有既存任务，禁止重复提交。"
            )
        freight, currency, fee_weight_g = self._logistics_values(item, route)
        package: dict[str, Any] = {
            "global_order_no": item.system_order_no,
            "wid": route.warehouse_id,
            "logistics_type_id": route.fast_logistics_type_id,
            "waybill_no": item.international_tracking_no,
            "tracking_no": item.logistics_no,
            "weight_unit": "g",
            "fee_weight": fee_weight_g,
            "logistics_freight": freight,
        }
        if currency:
            package["logistics_freight_currency_code"] = currency
        await self._confirm(
            confirm_func,
            item,
            "快速出库",
            (
                f"仓库 ID={route.warehouse_id}，物流方式={route.fast_logistics_type_id}；"
                "该异步操作将直接扣减库存"
            ),
        )
        result = await self._write(
            "提交快速出库",
            self.gateway.fast_outbound([package], browser=None),
        )
        if result.details.get("data") is not True:
            raise self._manual_result("提交快速出库", result, "API 未返回明确的 true。")
        await self._poll_fast_outbound_result(item)
        return ERP_CHECKPOINT_OUTBOUNDED

    async def _write(
        self,
        operation: str,
        call: Awaitable[MutationResult],
    ) -> MutationResult:
        try:
            result = await call
        except ManualReviewRequired as exc:
            result = exc.result
            suffix = self._request_suffix(result)
            raise ErpMarkManualReview(
                f"领星 API {operation}结果不明确{suffix}，禁止自动重试或网页回退。"
            ) from None
        except CapabilityUnavailable as exc:
            raise ErpMarkManualReview(f"领星 API {operation}不可用：{exc}") from None
        except Exception as exc:
            # A future gateway exception is conservative by default.  This is
            # a write boundary, so the queue must be blocked instead of retried.
            raise ErpMarkManualReview(
                f"领星 API {operation}出现未确认异常，禁止自动重试：{exc}"
            ) from None
        if not isinstance(result, MutationResult):
            raise ErpMarkManualReview(
                f"领星 API {operation}返回无效结果，禁止自动重试。"
            )
        if result.state is not MutationState.SUCCEEDED:
            raise self._manual_result(operation, result, result.message or "写入未成功。")
        return result

    @staticmethod
    def _request_suffix(result: MutationResult | None) -> str:
        request_id = result.request_id if result is not None else None
        return f"（request_id={request_id}）" if request_id else ""

    def _manual_result(
        self,
        operation: str,
        result: MutationResult,
        reason: str,
    ) -> ErpMarkManualReview:
        suffix = self._request_suffix(result)
        return ErpMarkManualReview(
            f"领星 API {operation}未获明确成功{suffix}：{reason} 禁止自动重试或网页回退。"
        )

    async def _confirm(
        self,
        confirm_func: ConfirmFunc,
        item: ReadyToMarkItem,
        operation: str,
        details: str,
    ) -> None:
        prompt = (
            f"\n即将通过领星 API 执行【{operation}】。\n"
            f"系统单号：{item.system_order_no}\n"
            f"平台单号：{item.platform_order_no}\n"
            f"物流单号：{item.logistics_no}\n"
            f"{details}\n"
            "请输入 y 确认，其他输入跳过当前订单："
        )
        if not await confirm_func(prompt):
            raise ErpMarkUserAbort(
                f"用户未确认领星 API {operation}：{item.platform_order_no} / {item.logistics_no}"
            )

    def _validate_channel_response(
        self, result: MutationResult, item: ReadyToMarkItem
    ) -> None:
        data = result.details.get("data")
        if not isinstance(data, Mapping):
            raise self._manual_result("设置仓库物流", result, "响应缺少 error_details。")
        errors = data.get("error_details")
        if not isinstance(errors, list):
            raise self._manual_result("设置仓库物流", result, "error_details 格式无效。")
        if errors:
            raise self._manual_result(
                "设置仓库物流",
                result,
                f"系统单号 {item.system_order_no} 出现在失败详情中。",
            )

    def _validate_review_response(self, result: MutationResult, item: ReadyToMarkItem) -> None:
        data = result.details.get("data")
        if not isinstance(data, Mapping):
            raise self._manual_result("审核发货", result, "响应缺少成功/失败详情。")
        success = data.get("success_info")
        failure = data.get("failure_info")
        if not isinstance(success, list) or not isinstance(failure, list):
            raise self._manual_result("审核发货", result, "成功/失败详情格式无效。")
        order_no = str(item.system_order_no)
        failed = [
            row
            for row in failure
            if isinstance(row, Mapping) and str(row.get("global_order_no") or "") == order_no
        ]
        succeeded = any(
            isinstance(row, Mapping) and str(row.get("global_order_no") or "") == order_no
            for row in success
        )
        if failed or not succeeded:
            reason = str(failed[0].get("message") or "") if failed else "成功列表不含该订单。"
            raise self._manual_result("审核发货", result, reason or "审核失败。")

    def _validate_delivery_response(
        self, result: MutationResult, item: ReadyToMarkItem
    ) -> None:
        data = result.details.get("data")
        if not isinstance(data, Mapping):
            raise self._manual_result("出库发货", result, "响应缺少成功/失败详情。")
        success = data.get("success_list")
        failure = data.get("fail_list")
        if not isinstance(success, list) or not isinstance(failure, list):
            raise self._manual_result("出库发货", result, "成功/失败详情格式无效。")
        order_no = str(item.system_order_no)
        failed = [
            row
            for row in failure
            if isinstance(row, Mapping) and str(row.get("order_number") or "") == order_no
        ]
        succeeded = any(
            isinstance(row, Mapping) and str(row.get("order_number") or "") == order_no
            for row in success
        )
        if failed or not succeeded:
            reason = str(failed[0].get("err_msg") or "") if failed else "成功列表不含该订单。"
            raise self._manual_result("出库发货", result, reason or "出库失败。")

    async def _read_wms_rows(self, item: ReadyToMarkItem) -> list[Mapping[str, Any]]:
        page = await self.gateway.list_wms_orders(
            filters={
                "page": 1,
                "page_size": 200,
                "order_number_arr": [item.system_order_no],
            },
            offset=0,
            length=200,
            browser=None,
        )
        matches = [
            row
            for row in page.items
            if str(row.get("order_number") or "") == str(item.system_order_no)
        ]
        if len(matches) > 1:
            raise ErpMarkManualReview(
                "同一系统单号对应多个销售出库单，禁止猜测要修改哪一条。"
            )
        if matches:
            platform_numbers = _string_values(matches[0].get("platform_order_no"))
            if platform_numbers and item.platform_order_no not in platform_numbers:
                raise ErpMarkManualReview(
                    "销售出库单的系统单号与平台单号不一致，禁止继续写入。"
                )
        return matches

    async def _preflight_wms_row(
        self, item: ReadyToMarkItem
    ) -> Mapping[str, Any] | None:
        try:
            matches = await self._read_wms_rows(item)
        except ErpMarkManualReview:
            raise
        except Exception as exc:
            raise ErpMarkManualReview(
                f"写入前无法检查既有销售出库单，禁止冒险重放操作：{exc}"
            ) from None
        return matches[0] if matches else None

    async def _poll_wms_row(
        self,
        item: ReadyToMarkItem,
        *,
        predicate: Callable[[Mapping[str, Any]], bool],
        action: str,
    ) -> Mapping[str, Any]:
        last_reason = "未返回该系统单号的销售出库单。"
        for attempt in range(self.wms_poll_attempts):
            try:
                matches = await self._read_wms_rows(item)
                if matches:
                    row = matches[0]
                    if predicate(row):
                        return row
                    last_reason = "销售出库单尚未达到预期状态。"
            except ErpMarkManualReview:
                raise
            except Exception as exc:
                last_reason = f"读取失败：{exc}"
            if attempt + 1 < self.wms_poll_attempts:
                await self.sleeper(self.wms_poll_interval_seconds)
        raise ErpMarkManualReview(
            f"{action}在限定次数内无法确认：{last_reason} 禁止自动重写。"
        )

    async def _read_fast_outbound_state(
        self, item: ReadyToMarkItem
    ) -> tuple[str | None, str]:
        try:
            rows = await self.gateway.get_fast_outbound_result(
                [item.system_order_no], browser=None
            )
        except Exception as exc:
            raise ErpMarkManualReview(
                f"提交快速出库前无法查询既存任务，禁止重复提交：{exc}"
            ) from None
        matches = [
            row
            for row in rows
            if str(row.get("global_order_no") or "") == str(item.system_order_no)
        ]
        if len(matches) != 1:
            raise ErpMarkManualReview(
                "快速出库既存任务查询未返回唯一结果，禁止重复提交。"
            )
        row = matches[0]
        state = str(row.get(FAST_OUTBOUND_RESULT_STATE_KEY) or "") or None
        if state not in {FAST_OUTBOUND_SUCCEEDED, FAST_OUTBOUND_FAILED}:
            raise ErpMarkManualReview("快速出库结果缺少明确的成功/失败标记。")
        return state, str(row.get("error_message") or "").strip()

    async def _poll_fast_outbound_result(self, item: ReadyToMarkItem) -> None:
        last_reason = "结果尚未返回。"
        for attempt in range(self.fast_result_attempts):
            try:
                rows = await self.gateway.get_fast_outbound_result(
                    [item.system_order_no], browser=None
                )
                matches = [
                    row
                    for row in rows
                    if str(row.get("global_order_no") or "") == str(item.system_order_no)
                ]
                if len(matches) > 1:
                    raise ErpMarkManualReview(
                        "快速出库结果同时出现多条同系统单号记录，需人工复核。"
                    )
                if matches:
                    row = matches[0]
                    state = str(row.get(FAST_OUTBOUND_RESULT_STATE_KEY) or "")
                    if state == FAST_OUTBOUND_SUCCEEDED:
                        return
                    if state != FAST_OUTBOUND_FAILED:
                        raise ErpMarkManualReview("快速出库结果缺少明确的成功/失败标记。")
                    message = str(row.get("error_message") or "").strip()
                    if message != "正在处理":
                        raise ErpMarkManualReview(
                            f"快速出库失败：{message or '未返回失败原因'}"
                        )
                    last_reason = message
            except ErpMarkManualReview:
                raise
            except Exception as exc:
                last_reason = f"结果查询失败：{exc}"
            if attempt + 1 < self.fast_result_attempts:
                await self.sleeper(self.fast_result_interval_seconds)
        raise ErpMarkManualReview(
            f"快速出库结果在限定次数内仍不明确（{last_reason}），禁止重复提交。"
        )

    def _logistics_values(
        self,
        item: ReadyToMarkItem,
        route: ErpLogisticsRoute,
    ) -> tuple[str, str | None, str]:
        freight = clean_money_amount(item.actual_total)
        try:
            if Decimal(freight) < 0:
                raise ErpMarkManualReview("物流运费不能为负数。")
        except InvalidOperation as exc:  # defensive; clean_money_amount already validates
            raise ErpMarkManualReview("物流运费不是有效数字。") from exc
        currency_match = re.search(r"\b([A-Z]{3})\b", str(item.actual_total or "").upper())
        currency = currency_match.group(1) if currency_match else route.freight_currency_code
        if currency is not None and currency not in _SUPPORTED_FREIGHT_CURRENCIES:
            raise ErpMarkManualReview(f"领星接口不支持物流运费币种：{currency}")
        fee_weight_g = format_chargeable_weight_g(item.chargeable_weight_kg)
        try:
            if Decimal(fee_weight_g) <= 0:
                raise ErpMarkManualReview("计费重必须大于 0。")
        except InvalidOperation as exc:  # defensive; formatter already validates
            raise ErpMarkManualReview("计费重不是有效数字。") from exc
        return freight, currency, fee_weight_g

    @staticmethod
    def _tracking_matches(
        row: Mapping[str, Any],
        *,
        item: ReadyToMarkItem,
        freight: str,
        currency: str | None,
        fee_weight_g: str,
    ) -> bool:
        if str(row.get("waybill_no") or "").strip() != str(
            item.international_tracking_no or ""
        ).strip():
            return False
        if str(row.get("tracking_no") or "").strip() != item.logistics_no:
            return False
        if not _numeric_equal(row.get("logistics_freight"), freight):
            return False
        if currency is not None and str(
            row.get("logistics_freight_currency_code") or ""
        ).upper() != currency:
            return False
        if not _numeric_equal(row.get("pkg_fee_weight"), fee_weight_g):
            return False
        if str(row.get("pkg_fee_weight_unit") or "").lower() != "g":
            return False
        return True


class ManagedApiErpMarkFunc:
    """Event-loop-safe callback for ``DesktopTaskRunner.erp_mark_func``.

    ``DesktopTaskRunner`` enters a fresh ``asyncio.run`` for every desktop
    command.  A production ``httpx.AsyncClient`` must therefore not be retained
    on a long-lived adapter.  The supplied factory must create a fresh gateway
    and client for every callback invocation; this wrapper always closes that
    client, including manual-review and user-abort paths.

    A typical factory closure calls ``DesktopApiServices.create_gateway`` with
    the controller's current settings.  The configuration provider should read
    the encrypted configuration at invocation time so route edits take effect
    without restarting the application.
    """

    def __init__(
        self,
        gateway_factory: GatewayFactory,
        configuration_provider: ConfigurationProvider,
        *,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.configuration_provider = configuration_provider
        self.sleeper = sleeper

    async def __call__(
        self,
        page: Any,
        item: ReadyToMarkItem,
        confirm_func: ConfirmFunc,
    ) -> str:
        gateway, client = await self.gateway_factory()
        try:
            adapter = ApiErpMarkAdapter.from_configuration(
                gateway,
                self.configuration_provider(),
                sleeper=self.sleeper,
            )
            return await adapter(page, item, confirm_func)
        finally:
            try:
                await client.aclose()
            except Exception:
                # Cleanup failure must never replace SUCCEEDED with RETRYABLE,
                # or replace an UNKNOWN/manual-review result with a generic
                # exception that the queue might retry.  The per-call client is
                # discarded either way; workflow state remains authoritative.
                pass


def _status(row: Mapping[str, Any]) -> int | None:
    value = row.get("status")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item).strip()}
    text = str(value or "").strip()
    return {text} if text else set()


def _numeric_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left).strip()) == Decimal(str(right).strip())
    except (InvalidOperation, ValueError):
        return False


__all__ = [
    "ApiErpMarkAdapter",
    "AsyncCloseable",
    "ErpLogisticsRoute",
    "GatewayFactory",
    "ManagedApiErpMarkFunc",
    "OutboundStrategy",
    "routes_from_configuration",
]
