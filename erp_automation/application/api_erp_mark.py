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
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from shipment_automation.alibaba_logistics import (
    normalize_carrier_name,
    normalize_service_line,
)
from shipment_automation.erp_mark_ship import (
    CHECKPOINT_RANK,
    alibaba_route_mode_for_service_line,
    ErpMarkManualReview,
    ErpMarkEmergencyStopped,
    ErpMarkUserAbort,
    RuntimeGuardFunc,
    clean_money_amount,
    format_chargeable_weight_g,
    validate_ready_item,
    execute_erp_mark_item,
    ensure_erp_write_allowed,
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
from .readback import (
    iter_readback_attempts,
    normalize_readback_delays,
    readback_delays_from_configuration,
)


ConfirmFunc = Callable[[str], Awaitable[bool]]
CheckpointFunc = Callable[[str, dict[str, str | None]], Awaitable[None]]
ApprovalFunc = Callable[[str, str], Awaitable[None]]
WriteAuditFunc = Callable[[str, dict[str, Any]], Awaitable[None]]
SleepFunc = Callable[[float], Awaitable[None]]
ConfigurationProvider = Callable[[], Mapping[str, Any]]
BrowserPageProvider = Callable[[], Awaitable[Any]]


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


GatewayFactory = Callable[[], Awaitable[tuple[LingxingGateway, AsyncCloseable]]]


class ErpApiFallbackEligible(RuntimeError):
    def __init__(self, operation: str, result: MutationResult):
        super().__init__(result.message or operation)
        self.operation = operation
        self.result = result


class ErpApiAmbiguousWrite(ErpMarkManualReview):
    def __init__(self, operation: str, result: MutationResult | None):
        request_id = result.request_id if result is not None else None
        suffix = f"（request_id={request_id}）" if request_id else ""
        super().__init__(
            f"领星 API {operation}结果不明确{suffix}，禁止自动重试或网页回退。"
        )
        self.operation = operation
        self.result = result


async def _noop_checkpoint(_checkpoint: str, _values: dict[str, str | None]) -> None:
    return None


async def _noop_approval(_confirmation_type: str, _payload_hash: str) -> None:
    return None


async def _noop_write_audit(
    _event_type: str,
    _details: dict[str, Any],
) -> None:
    return None


def _format_write_parameters(
    title: str,
    parameters: Sequence[tuple[str, str, Any]],
) -> str:
    lines = [f"即将发送的{title}参数："]
    lines.extend(
        f"{chinese_name}：{value if value not in (None, '') else '-'}"
        for _field_name, chinese_name, value in parameters
    )
    return "\n".join(lines)


def _route_review_label(
    item: ReadyToMarkItem,
    route: "ErpLogisticsRoute",
    route_mode: str,
) -> str:
    """Describe a configured route without exposing implementation IDs."""

    if route.channel_name:
        return route.channel_name
    carrier = normalize_carrier_name(item.carrier) or str(item.carrier or "").strip()
    mode = {
        "full": "全程物流",
        "tail": "尾程物流",
        "default": "默认线路",
    }.get(str(route_mode or "").strip().casefold(), "已配置线路")
    return f"{carrier or '已配置物流方式'}（{mode}）"


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
    channel_name: str | None = None

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
        if self.channel_name is not None:
            channel_name = str(self.channel_name).strip()
            object.__setattr__(self, "channel_name", channel_name or None)


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


ErpRouteVariants = dict[str, ErpLogisticsRoute]
ErpRouteConfiguration = ErpLogisticsRoute | ErpRouteVariants


def _route_from_configuration(
    route: Mapping[str, Any],
    *,
    label: str,
) -> ErpLogisticsRoute:
    warehouse_id = route.get("warehouse_id", route.get("sys_wid", route.get("wid")))
    logistics_type_id = route.get("logistics_type_id", route.get("type_id"))
    return ErpLogisticsRoute(
        warehouse_id=_positive_int(warehouse_id, f"{label}.warehouse_id"),
        logistics_type_id=_positive_int(
            logistics_type_id, f"{label}.logistics_type_id"
        ),
        fast_logistics_type_id=route.get("fast_logistics_type_id"),
        freight_currency_code=route.get("freight_currency_code"),
        channel_name=route.get("channel_name"),
    )


def routes_from_configuration(
    configuration: Mapping[str, Any],
) -> dict[str, ErpRouteConfiguration]:
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
    normalized: dict[str, ErpRouteConfiguration] = {}
    for carrier, route_value in routes.items():
        carrier_key = normalize_carrier_name(str(carrier))
        if not carrier_key:
            raise ValueError("物流映射包含空承运商名称。")
        route = _configuration_mapping(route_value, f"物流映射 {carrier_key}")
        variant_names = tuple(
            name for name in ("default", "full", "tail") if name in route
        )
        if variant_names:
            variants: ErpRouteVariants = {}
            for name in variant_names:
                variant = _configuration_mapping(
                    route[name], f"物流映射 {carrier_key}.{name}"
                )
                variants[name] = _route_from_configuration(
                    variant, label=f"{carrier_key}.{name}"
                )
            normalized[carrier_key] = variants
        else:
            normalized[carrier_key] = _route_from_configuration(
                route, label=carrier_key
            )
    return normalized


class ApiErpMarkAdapter:
    """Callable API-only ERP mark-and-outbound workflow."""

    def __init__(
        self,
        gateway: LingxingGateway,
        routes: Mapping[str, ErpRouteConfiguration],
        *,
        outbound_strategy: OutboundStrategy | str = OutboundStrategy.STAGED,
        wms_poll_attempts: int = 5,
        wms_poll_interval_seconds: float = 1.0,
        fast_result_attempts: int = 10,
        fast_result_interval_seconds: float = 1.0,
        readback_delays_seconds: Sequence[float] | None = None,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> None:
        self.gateway = gateway
        self.routes: dict[str, ErpRouteVariants] = {}
        for carrier, route in routes.items():
            carrier_key = normalize_carrier_name(carrier)
            self.routes[carrier_key] = (
                dict(route)
                if isinstance(route, Mapping)
                else {"default": route}
            )
        self.outbound_strategy = OutboundStrategy(outbound_strategy)
        self.wms_poll_attempts = _positive_int(wms_poll_attempts, "wms_poll_attempts")
        self.wms_poll_interval_seconds = _nonnegative_float(
            wms_poll_interval_seconds, "wms_poll_interval_seconds"
        )
        self.fast_result_attempts = _positive_int(fast_result_attempts, "fast_result_attempts")
        self.fast_result_interval_seconds = _nonnegative_float(
            fast_result_interval_seconds, "fast_result_interval_seconds"
        )
        self.wms_readback_delays = normalize_readback_delays(
            readback_delays_seconds
            if readback_delays_seconds is not None
            else (
                0.0,
                *(self.wms_poll_interval_seconds for _ in range(self.wms_poll_attempts - 1)),
            )
        )
        self.fast_result_readback_delays = normalize_readback_delays(
            readback_delays_seconds
            if readback_delays_seconds is not None
            else (
                0.0,
                *(
                    self.fast_result_interval_seconds
                    for _ in range(self.fast_result_attempts - 1)
                ),
            )
        )
        self.sleeper = sleeper
        self._selected_wms_wo_numbers: dict[str, str] = {}
        self._wms_selection_func: Callable[[list[dict[str, Any]]], Awaitable[str]] | None = None

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
            readback_delays_seconds=readback_delays_from_configuration(configuration),
            sleeper=sleeper,
        )

    async def __call__(
        self,
        page: Any,
        item: ReadyToMarkItem,
        confirm_func: ConfirmFunc,
        checkpoint_func: CheckpointFunc | None = None,
        approval_func: ApprovalFunc | None = None,
        runtime_guard_func: RuntimeGuardFunc | None = None,
        browser_page_provider: BrowserPageProvider | None = None,
    ) -> str:
        validate_ready_item(item)
        rank = CHECKPOINT_RANK.get(item.erp_checkpoint or ERP_CHECKPOINT_NONE)
        if rank is None:
            raise ErpMarkManualReview(f"队列包含未知 ERP 检查点：{item.erp_checkpoint}")
        if rank >= CHECKPOINT_RANK[ERP_CHECKPOINT_OUTBOUNDED]:
            return ERP_CHECKPOINT_OUTBOUNDED
        self._ensure_write_switch()
        route, route_mode = self._route_for(item)
        base_checkpoint = checkpoint_func or _noop_checkpoint
        approval_func = approval_func or _noop_approval
        audit_callback = getattr(confirm_func, "write_audit", None)
        write_audit_func: WriteAuditFunc = (
            audit_callback if callable(audit_callback) else _noop_write_audit
        )
        selector = getattr(confirm_func, "select_wms_row", None)
        self._wms_selection_func = selector if callable(selector) else None
        current_checkpoint = item.erp_checkpoint or ERP_CHECKPOINT_NONE

        async def record_checkpoint(
            checkpoint: str,
            values: dict[str, str | None],
        ) -> None:
            nonlocal current_checkpoint
            await base_checkpoint(checkpoint, values)
            current_checkpoint = checkpoint

        try:
            if self.outbound_strategy is OutboundStrategy.FAST_OUTBOUND:
                if rank != CHECKPOINT_RANK[ERP_CHECKPOINT_NONE]:
                    raise ErpMarkManualReview(
                        "订单已有分阶段标发检查点，禁止改用快速出库以免重复写入。"
                    )
                result = await self._fast_outbound(
                    item,
                    route,
                    route_mode,
                    confirm_func,
                    approval_func=approval_func,
                    runtime_guard_func=runtime_guard_func,
                )
                await record_checkpoint(ERP_CHECKPOINT_OUTBOUNDED, {})
                return result
            return await self._staged_outbound(
                item,
                route,
                route_mode,
                rank,
                confirm_func,
                checkpoint_func=record_checkpoint,
                approval_func=approval_func,
                write_audit_func=write_audit_func,
                pending_review_intent=getattr(
                    confirm_func,
                    "pending_erp_review_intent",
                    None,
                ),
                runtime_guard_func=runtime_guard_func,
            )
        except ErpApiFallbackEligible as exc:
            request_suffix = self._request_suffix(exc.result)
            prompt = (
                f"\n领星 API【{exc.operation}】已明确拒绝，且能够证明本次写入尚未执行"
                f"{request_suffix}。\n"
                f"系统单号：{item.system_order_no}\n平台单号：{item.platform_order_no}\n"
                f"物流单号：{item.logistics_no}\n"
                "是否改用原网页流程，从已保存的阶段继续？"
            )
            if not await confirm_func(prompt):
                raise ErpMarkUserAbort(
                    f"用户拒绝 ERP 标发网页回退：{item.platform_order_no} / {item.logistics_no}"
                ) from None
            if page is None and browser_page_provider is not None:
                page = await browser_page_provider()
            if page is None:
                raise ErpMarkManualReview("网页回退已获确认，但当前任务没有可用 ERP 页面。")
            fallback_item = replace(item, erp_checkpoint=current_checkpoint)
            fallback_kwargs = {
                "checkpoint_func": record_checkpoint,
                "approval_func": approval_func,
            }
            if runtime_guard_func is not None:
                fallback_kwargs["runtime_guard_func"] = runtime_guard_func
            return await execute_erp_mark_item(
                page,
                fallback_item,
                confirm_func,
                **fallback_kwargs,
            )

    def _ensure_write_switch(self) -> None:
        router = getattr(self.gateway, "router", None)
        if router is not None and not bool(getattr(router, "writes_enabled", True)):
            raise ErpMarkEmergencyStopped(
                "领星 API ERP 写入紧急开关未开启（已停止），未执行任何写入。"
            )

    def _route_for(self, item: ReadyToMarkItem) -> tuple[ErpLogisticsRoute, str]:
        carrier = normalize_carrier_name(item.carrier)
        routes = self.routes.get(carrier)
        if routes is None:
            raise ErpMarkManualReview(
                f"承运商 {carrier or item.carrier or '-'} 尚未配置明确的领星仓库物流渠道，"
                "禁止按名称猜测。"
            )
        if set(routes) == {"default"}:
            return routes["default"], "default"
        if (
            not normalize_service_line(item.service_line)
            and CHECKPOINT_RANK.get(
                item.erp_checkpoint or ERP_CHECKPOINT_NONE,
                -1,
            )
            >= CHECKPOINT_RANK[ERP_CHECKPOINT_CHANNEL_SET]
        ):
            existing_route = routes.get("full") or routes.get("default")
            if existing_route is None:
                raise ErpMarkManualReview(
                    f"承运商 {carrier or item.carrier or '-'} 的历史任务已设置渠道，"
                    "但缺少可用于后续运单填写的兼容路由配置。"
                )
            return existing_route, "existing"
        route_mode = alibaba_route_mode_for_service_line(
            carrier, item.service_line
        )
        if route_mode is None:
            route_mode = "default"
        route = routes.get(route_mode) or routes.get("default")
        if route is None:
            raise ErpMarkManualReview(
                f"承运商 {carrier or item.carrier or '-'} 缺少 {route_mode} 路由 ID，"
                "禁止回退到另一条全程/尾程线路。"
            )
        return route, route_mode

    async def _staged_outbound(
        self,
        item: ReadyToMarkItem,
        route: ErpLogisticsRoute,
        route_mode: str,
        rank: int,
        confirm_func: ConfirmFunc,
        *,
        checkpoint_func: CheckpointFunc,
        approval_func: ApprovalFunc,
        write_audit_func: WriteAuditFunc,
        pending_review_intent: Mapping[str, Any] | None,
        runtime_guard_func: RuntimeGuardFunc | None,
    ) -> str:
        freight, currency, fee_weight_g = self._logistics_values(item, route)
        # A successful external write followed by a local checkpoint failure
        # must not cause the next run to replay review/outbound.  The sales
        # outbound document is the authoritative read-before-write guard.
        existing_row = await self._preflight_wms_row(item)
        if existing_row is not None:
            existing_status = _status(existing_row)
            if existing_status == 3:
                await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})
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
            inferred_checkpoint = {
                CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED]: ERP_CHECKPOINT_AUDITED,
                CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]: ERP_CHECKPOINT_LOGISTICS_SAVED,
            }.get(rank)
            if inferred_checkpoint and rank > CHECKPOINT_RANK.get(item.erp_checkpoint, 0):
                await checkpoint_func(inferred_checkpoint, {})

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_CHANNEL_SET]:
            shipping_payload = {
                "global_order_no": item.system_order_no,
                "logistics": {
                    "logistics_type_id": route.logistics_type_id,
                    "sys_wid": route.warehouse_id,
                },
            }
            await ensure_erp_write_allowed(runtime_guard_func)
            await self._confirm(
                confirm_func,
                item,
                "设置仓库物流",
                _format_write_parameters(
                    "设置仓库物流",
                    (
                        ("global_order_no", "系统单号", shipping_payload["global_order_no"]),
                        (
                            "configured_route",
                            "仓库物流渠道",
                            _route_review_label(item, route, route_mode),
                        ),
                    ),
                ),
            )
            await ensure_erp_write_allowed(runtime_guard_func)
            channel = await self._write(
                "设置仓库物流",
                self.gateway.set_shipping_channel(
                    [shipping_payload],
                    browser=None,
                ),
            )
            self._validate_channel_response(channel, item)
            channel_payload = {
                "carrier": normalize_carrier_name(item.carrier),
                "service_line": normalize_service_line(item.service_line),
                "route_mode": route_mode,
                "warehouse_id": route.warehouse_id,
                "logistics_type_id": route.logistics_type_id,
            }
            channel_payload_hash = hashlib.sha256(
                json.dumps(
                    channel_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            await checkpoint_func(
                ERP_CHECKPOINT_CHANNEL_SET,
                {
                    "channel_path": _route_review_label(
                        item,
                        route,
                        route_mode,
                    ),
                    "channel_payload_hash": channel_payload_hash,
                },
            )

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_AUDITED]:
            if pending_review_intent is None:
                await ensure_erp_write_allowed(runtime_guard_func)
                await self._confirm(
                    confirm_func,
                    item,
                    "审核发货",
                    _format_write_parameters(
                        "审核发货",
                        (
                            (
                                "global_order_no",
                                "系统单号列表",
                                json.dumps([item.system_order_no], ensure_ascii=False),
                            ),
                        ),
                    ),
                )
                await ensure_erp_write_allowed(runtime_guard_func)
            existing_row = await self._review_with_readback(
                item,
                checkpoint_func=checkpoint_func,
                write_audit_func=write_audit_func,
                pending_intent=pending_review_intent,
            )

        row = existing_row or await self._poll_wms_row(
            item, predicate=lambda value: True, action="读取销售出库单"
        )
        if _status(row) == 3:
            await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})
            return ERP_CHECKPOINT_OUTBOUNDED

        # An ambiguous review may be reconciled after another actor has already
        # entered logistics data.  Never overwrite it merely because the local
        # checkpoint was behind the authoritative WMS document.
        tracking_present = bool(
            str(row.get("waybill_no") or "").strip()
            or str(row.get("tracking_no") or "").strip()
        )
        if tracking_present or _status(row) == 2:
            if not self._tracking_matches(
                row,
                item=item,
                freight=freight,
                currency=currency,
                fee_weight_g=fee_weight_g,
            ):
                raise ErpMarkManualReview(
                    "销售出库单已有与本任务不一致的物流信息，禁止覆盖或继续出库。"
                )
            if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]:
                await checkpoint_func(ERP_CHECKPOINT_LOGISTICS_SAVED, {})
                rank = CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]

        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]:
            tracking_payload = {
                "waybill_no": str(item.international_tracking_no),
                "wo_number": str(row.get("wo_number") or ""),
                "tracking_no": item.logistics_no,
                "logistics_freight": freight,
                "logistics_freight_currency_code": currency,
                "pkg_fee_weight": fee_weight_g,
                "pkg_fee_weight_unit": "g",
            }
            payload_hash = hashlib.sha256(
                json.dumps(
                    tracking_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            await ensure_erp_write_allowed(runtime_guard_func)
            await self._confirm(
                confirm_func,
                item,
                "审核运单填写信息",
                _format_write_parameters(
                    "运单填写",
                    (
                        ("waybill_no", "国际物流单号", tracking_payload["waybill_no"]),
                        ("wo_number", "销售出库单号", tracking_payload["wo_number"]),
                        ("tracking_no", "阿里物流单号", tracking_payload["tracking_no"]),
                        ("logistics_freight", "运费", tracking_payload["logistics_freight"]),
                        (
                            "logistics_freight_currency_code",
                            "运费币种",
                            tracking_payload["logistics_freight_currency_code"],
                        ),
                        ("pkg_fee_weight", "计费重量", tracking_payload["pkg_fee_weight"]),
                        (
                            "pkg_fee_weight_unit",
                            "计费重量单位",
                            tracking_payload["pkg_fee_weight_unit"],
                        ),
                    ),
                ),
            )
            await ensure_erp_write_allowed(runtime_guard_func)
            await approval_func("logistics", payload_hash)
            # The approval audit writes only local state.  Re-check the shared
            # emergency stop immediately before creating/sending the ERP write.
            await ensure_erp_write_allowed(runtime_guard_func)
            await self._write(
                "写入运单/跟踪号",
                self.gateway.set_tracking_no(**tracking_payload, browser=None),
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
            await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})
            return ERP_CHECKPOINT_OUTBOUNDED
        if rank < CHECKPOINT_RANK[ERP_CHECKPOINT_LOGISTICS_SAVED]:
            await checkpoint_func(ERP_CHECKPOINT_LOGISTICS_SAVED, {})

        await ensure_erp_write_allowed(runtime_guard_func)
        await self._confirm(
            confirm_func,
            item,
            "出库发货",
            _format_write_parameters(
                "出库发货",
                (("order_number_list", "系统单号列表", item.system_order_no),),
            ),
        )
        await ensure_erp_write_allowed(runtime_guard_func)
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
        await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {})
        return ERP_CHECKPOINT_OUTBOUNDED

    async def _fast_outbound(
        self,
        item: ReadyToMarkItem,
        route: ErpLogisticsRoute,
        route_mode: str,
        confirm_func: ConfirmFunc,
        *,
        approval_func: ApprovalFunc,
        runtime_guard_func: RuntimeGuardFunc | None,
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
        payload_hash = hashlib.sha256(
            json.dumps(
                package,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        await ensure_erp_write_allowed(runtime_guard_func)
        await self._confirm(
            confirm_func,
            item,
            "审核快速出库运单信息",
            _format_write_parameters(
                "快速出库",
                (
                    ("global_order_no", "系统单号", package["global_order_no"]),
                    (
                        "configured_route",
                        "仓库物流渠道",
                        _route_review_label(item, route, route_mode),
                    ),
                    ("waybill_no", "国际物流单号", package["waybill_no"]),
                    ("tracking_no", "阿里物流单号", package["tracking_no"]),
                    ("fee_weight", "计费重量", package["fee_weight"]),
                    ("weight_unit", "计费重量单位", package["weight_unit"]),
                    ("logistics_freight", "运费", package["logistics_freight"]),
                    *(
                        (
                            (
                                "logistics_freight_currency_code",
                                "运费币种",
                                package["logistics_freight_currency_code"],
                            ),
                        )
                        if "logistics_freight_currency_code" in package
                        else ()
                    ),
                ),
            ),
        )
        await ensure_erp_write_allowed(runtime_guard_func)
        await approval_func("logistics", payload_hash)
        await ensure_erp_write_allowed(runtime_guard_func)
        result = await self._write(
            "提交快速出库",
            self.gateway.fast_outbound([package], browser=None),
        )
        if result.details.get("data") is not True:
            raise self._manual_result("提交快速出库", result, "API 未返回明确的 true。")
        await self._poll_fast_outbound_result(item)
        return ERP_CHECKPOINT_OUTBOUNDED

    async def _review_with_readback(
        self,
        item: ReadyToMarkItem,
        *,
        checkpoint_func: CheckpointFunc,
        write_audit_func: WriteAuditFunc,
        pending_intent: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        """Write review once and reconcile an ambiguous response by WMS reads.

        Reconciliation can prove that review was applied, but an absent row can
        never prove the opposite because Lingxing projections are eventually
        consistent.  The latter therefore remains a manual-review hold.
        """

        operation = "review_orders"
        target_order_nos = [str(item.system_order_no)]
        payload_hash = hashlib.sha256(
            json.dumps(
                {"global_order_no": target_order_nos},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if pending_intent is not None:
            pending_operation = str(pending_intent.get("operation") or "")
            pending_system_order = str(
                pending_intent.get("system_order_no") or ""
            )
            pending_payload_hash = str(pending_intent.get("payload_hash") or "")
            pending_attempt_id = str(pending_intent.get("attempt_id") or "")
            if (
                pending_operation != operation
                or pending_system_order != str(item.system_order_no)
                or pending_payload_hash != payload_hash
                or not pending_attempt_id
            ):
                raise ErpMarkManualReview(
                    "检测到与当前订单不一致的未完成 ERP 审核写入意图，"
                    "禁止重新提交审核请求。"
                )
            return await self._complete_ambiguous_review_readback(
                item,
                ambiguous=ErpApiAmbiguousWrite("审核发货", None),
                common_audit={
                    "attempt_id": pending_attempt_id,
                    "operation": operation,
                    "payload_hash": payload_hash,
                    "system_order_no": str(item.system_order_no),
                    "platform_order_no": str(item.platform_order_no),
                    "recovered_after_restart": True,
                },
                checkpoint_func=checkpoint_func,
                write_audit_func=write_audit_func,
            )

        attempt_id = uuid.uuid4().hex
        common_audit = {
            "attempt_id": attempt_id,
            "operation": operation,
            "payload_hash": payload_hash,
            "system_order_no": str(item.system_order_no),
            "platform_order_no": str(item.platform_order_no),
        }
        # This event is the durable write-ahead boundary.  If it cannot be
        # persisted, the network mutation must not be attempted.
        await write_audit_func(
            "ERP_WRITE_INTENT_RECORDED",
            {
                **common_audit,
                "checkpoint_before": ERP_CHECKPOINT_CHANNEL_SET,
                "baseline_active_wms_rows": 0,
            },
        )

        try:
            review = await self._write(
                "审核发货",
                self.gateway.review_orders(target_order_nos, browser=None),
            )
        except ErpApiAmbiguousWrite as exc:
            return await self._complete_ambiguous_review_readback(
                item,
                ambiguous=exc,
                common_audit=common_audit,
                checkpoint_func=checkpoint_func,
                write_audit_func=write_audit_func,
            )

        try:
            self._validate_review_response(review, item)
        except ErpApiFallbackEligible as exc:
            await self._best_effort_write_audit(
                write_audit_func,
                "ERP_WRITE_REJECTED",
                {
                    **common_audit,
                    "request_id": exc.result.request_id,
                    "definitely_not_executed": exc.result.definitely_not_executed,
                },
            )
            raise
        except ErpMarkManualReview as exc:
            # A malformed or incomplete acknowledgement is just as ambiguous
            # as a lost response.  It is safe to promote only after the same
            # authoritative WMS readback used for transport timeouts.
            return await self._complete_ambiguous_review_readback(
                item,
                ambiguous=ErpApiAmbiguousWrite("审核发货", review),
                common_audit={**common_audit, "ack_validation_error": str(exc)},
                checkpoint_func=checkpoint_func,
                write_audit_func=write_audit_func,
            )
        await self._best_effort_write_audit(
            write_audit_func,
            "ERP_WRITE_ACKNOWLEDGED",
            {
                **common_audit,
                "request_id": review.request_id,
                "result_state": review.state.value,
            },
        )
        await checkpoint_func(ERP_CHECKPOINT_AUDITED, {})
        return None

    async def _complete_ambiguous_review_readback(
        self,
        item: ReadyToMarkItem,
        *,
        ambiguous: ErpApiAmbiguousWrite,
        common_audit: dict[str, Any],
        checkpoint_func: CheckpointFunc,
        write_audit_func: WriteAuditFunc,
    ) -> Mapping[str, Any]:
        result = ambiguous.result
        await self._best_effort_write_audit(
            write_audit_func,
            "ERP_WRITE_RESULT_AMBIGUOUS",
            {
                **common_audit,
                "request_id": result.request_id if result else None,
                "result_state": result.state.value if result else "unknown",
                "result_source": result.source if result else "",
                "exception_type": (
                    str(result.details.get("exception_type") or "")
                    if result
                    else ""
                ),
            },
        )
        try:
            row, evidence = await self._reconcile_ambiguous_review(item)
        except ErpMarkManualReview as readback_error:
            await self._best_effort_write_audit(
                write_audit_func,
                "ERP_WRITE_READBACK_INCONCLUSIVE",
                {
                    **common_audit,
                    "request_id": result.request_id if result else None,
                    "reason": str(readback_error),
                },
            )
            raise ErpMarkManualReview(
                f"{ambiguous} 已执行只读销售出库单核验，但仍无法确认：{readback_error}"
            ) from None

        await self._best_effort_write_audit(
            write_audit_func,
            "ERP_WRITE_READBACK_CONFIRMED",
            {
                **common_audit,
                "request_id": result.request_id if result else None,
                **evidence,
            },
        )
        await checkpoint_func(ERP_CHECKPOINT_AUDITED, {})
        return row

    async def _reconcile_ambiguous_review(
        self,
        item: ReadyToMarkItem,
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        last_reason = "未返回该系统单号的销售出库单。"
        last_attempt = None
        transient_error_count = 0
        async for attempt in iter_readback_attempts(
            self.wms_readback_delays,
            sleeper=self.sleeper,
        ):
            last_attempt = attempt
            try:
                matches = await self._read_wms_rows(item, allow_selection=False)
            except ErpMarkManualReview:
                # Conflicting/multiple/unknown rows are positive evidence of an
                # unsafe state, not a transient read failure to wait through.
                raise
            except Exception as exc:
                transient_error_count += 1
                last_reason = f"读取失败：{type(exc).__name__}"
                continue
            if not matches:
                last_reason = "尚未返回该系统单号的销售出库单。"
                continue
            row = matches[0]
            status = _status(row)
            if status not in {1, 2, 3}:
                raise ErpMarkManualReview(
                    f"销售出库单状态 {status!r} 不能证明审核已安全生效。"
                )
            return row, {
                **attempt.details(),
                "transient_read_error_count": transient_error_count,
                "wms_status": status,
                "wo_number": str(row.get("wo_number") or "").strip(),
                "evidence": "unique_active_wms_order",
            }

        attempts = last_attempt.number if last_attempt else 0
        waited = last_attempt.waited_seconds if last_attempt else 0
        raise ErpMarkManualReview(
            f"只读核验 {attempts} 次、等待约 {waited:g} 秒后仍无法确认审核已生效："
            f"{last_reason} 未重新发送审核请求。"
        )

    @staticmethod
    async def _best_effort_write_audit(
        write_audit_func: WriteAuditFunc,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        try:
            await write_audit_func(event_type, details)
        except Exception:
            # The committed intent event remains the crash-recovery marker.
            # A post-write audit failure must not turn an ambiguous write into
            # an automatically retryable exception.
            return

    async def _write(
        self,
        operation: str,
        call: Awaitable[MutationResult],
    ) -> MutationResult:
        try:
            result = await call
        except ManualReviewRequired as exc:
            raise ErpApiAmbiguousWrite(operation, exc.result) from None
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
        if result.state is MutationState.DISABLED:
            raise ErpMarkEmergencyStopped(
                result.message or "ERP 写入已紧急停止；当前阶段已暂停。"
            )
        if result.state is not MutationState.SUCCEEDED:
            if (
                result.state is MutationState.FAILED
                and result.definitely_not_executed
                and not bool(result.details.get("browser_fallback_forbidden"))
            ):
                raise ErpApiFallbackEligible(operation, result)
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
        prompt = f"{details}\n请输入 y 确认，其他输入跳过当前订单："
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
            target = str(item.system_order_no)
            matched = [
                row
                for row in errors
                if isinstance(row, Mapping)
                and str(
                    row.get("global_order_no")
                    or row.get("order_number")
                    or row.get("globalOrderNo")
                    or ""
                )
                == target
            ]
            if len(matched) == 1:
                raise ErpApiFallbackEligible(
                    "设置仓库物流",
                    self._explicit_rejection(
                        result,
                        f"系统单号 {target} 出现在失败详情中。",
                    ),
                )
            raise self._manual_result("设置仓库物流", result, "失败详情无法唯一对应当前订单。")

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
            if len(failed) == 1:
                raise ErpApiFallbackEligible(
                    "审核发货",
                    self._explicit_rejection(result, reason or "审核失败。"),
                )
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
            if len(failed) == 1:
                raise ErpApiFallbackEligible(
                    "出库发货",
                    self._explicit_rejection(result, reason or "出库失败。"),
                )
            raise self._manual_result("出库发货", result, reason or "出库失败。")

    @staticmethod
    def _explicit_rejection(result: MutationResult, message: str) -> MutationResult:
        return MutationResult(
            state=MutationState.FAILED,
            source=result.source,
            request_id=result.request_id,
            message=message,
            before=result.before,
            after=result.after,
            definitely_not_executed=True,
            details={**dict(result.details), "ack_validation": "target_rejected"},
        )

    async def _read_wms_rows(
        self,
        item: ReadyToMarkItem,
        *,
        allow_selection: bool = True,
    ) -> list[Mapping[str, Any]]:
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
        for row in matches:
            platform_numbers = _string_values(row.get("platform_order_no"))
            if platform_numbers and item.platform_order_no not in platform_numbers:
                raise ErpMarkManualReview(
                    "销售出库单的系统单号与平台单号不一致，禁止继续写入。"
                )

        # Lingxing keeps cut-off sales outbound documents in the WMS query.
        # They are historical terminal records: selecting one cannot resume the
        # staged outbound flow, and after a new review Lingxing may return the
        # old cut-off rows together with the newly-created active document.
        # Exclude only the documented cut-off state.  Every other unknown state
        # remains fail-closed so a new API state cannot be mistaken for a safe
        # writable document.
        active_matches: list[Mapping[str, Any]] = []
        unknown_matches: list[Mapping[str, Any]] = []
        for row in matches:
            if _is_cut_off_wms_row(row):
                continue
            if _status(row) in {1, 2, 3}:
                active_matches.append(row)
            else:
                unknown_matches.append(row)
        if unknown_matches:
            descriptions = ", ".join(
                _wms_row_status_description(row) for row in unknown_matches[:5]
            )
            raise ErpMarkManualReview(
                "销售出库单包含无法安全识别的状态，禁止过滤或猜测："
                f"{descriptions or '-'}"
            )

        selected = str(
            self._selected_wms_wo_numbers.get(str(item.system_order_no))
            or item.selected_wms_wo_number
            or ""
        ).strip()
        if selected:
            selected_rows = [
                row
                for row in active_matches
                if str(row.get("wo_number") or "").strip() == selected
            ]
            if len(selected_rows) != 1:
                raise ErpMarkManualReview(
                    f"已选销售出库单 {selected} 已不存在或不再唯一，或者当前状态已截单；"
                    "禁止改用其他记录。"
                )
            return selected_rows
        if len(active_matches) > 1:
            wo_numbers = [
                str(row.get("wo_number") or "").strip() for row in active_matches
            ]
            if any(not value for value in wo_numbers) or len(set(wo_numbers)) != len(wo_numbers):
                raise ErpMarkManualReview(
                    "同一系统单号的销售出库单缺少唯一 wo_number，无法安全选择。"
                )
            if not allow_selection:
                raise ErpMarkManualReview(
                    "审核结果不明确后的只读核验发现多个销售出库单，"
                    "无法自动证明本次审核对应哪一条。"
                )
            if self._wms_selection_func is None:
                raise ErpMarkManualReview(
                    "同一系统单号对应多个销售出库单，需要用户明确选择一条。"
                )
            chosen = str(
                await self._wms_selection_func([dict(row) for row in active_matches])
                or ""
            ).strip()
            selected_rows = [
                row
                for row in active_matches
                if str(row.get("wo_number") or "").strip() == chosen
            ]
            if len(selected_rows) != 1:
                raise ErpMarkManualReview("用户选择的销售出库单不在当前候选中。")
            self._selected_wms_wo_numbers[str(item.system_order_no)] = chosen
            return selected_rows
        return active_matches

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
        last_attempt = None
        async for attempt in iter_readback_attempts(
            self.wms_readback_delays,
            sleeper=self.sleeper,
        ):
            last_attempt = attempt
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
        attempts = last_attempt.number if last_attempt else 0
        waited = last_attempt.waited_seconds if last_attempt else 0
        raise ErpMarkManualReview(
            f"{action}连续读回 {attempts} 次、等待约 {waited:g} 秒后仍无法确认："
            f"{last_reason} 禁止自动重写。"
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
        last_attempt = None
        async for attempt in iter_readback_attempts(
            self.fast_result_readback_delays,
            sleeper=self.sleeper,
        ):
            last_attempt = attempt
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
        attempts = last_attempt.number if last_attempt else 0
        waited = last_attempt.waited_seconds if last_attempt else 0
        raise ErpMarkManualReview(
            f"快速出库结果连续读回 {attempts} 次、等待约 {waited:g} 秒后仍不明确"
            f"（{last_reason}），禁止重复提交。"
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
        # Ordinary marking is API-only.  The desktop attaches to the
        # operator's Chrome lazily after an explicit fallback approval.
        self.requires_browser_fallback = False
        self.supports_lazy_browser_fallback = True
        self.manages_checkpoints = True
        self.supports_runtime_guard = True

    async def __call__(
        self,
        page: Any,
        item: ReadyToMarkItem,
        confirm_func: ConfirmFunc,
        checkpoint_func: CheckpointFunc | None = None,
        approval_func: ApprovalFunc | None = None,
        runtime_guard_func: RuntimeGuardFunc | None = None,
        browser_page_provider: BrowserPageProvider | None = None,
    ) -> str:
        gateway, client = await self.gateway_factory()
        try:
            adapter = ApiErpMarkAdapter.from_configuration(
                gateway,
                self.configuration_provider(),
                sleeper=self.sleeper,
            )
            return await adapter(
                page,
                item,
                confirm_func,
                checkpoint_func=checkpoint_func,
                approval_func=approval_func,
                runtime_guard_func=runtime_guard_func,
                browser_page_provider=browser_page_provider,
            )
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


def _is_cut_off_wms_row(row: Mapping[str, Any]) -> bool:
    status = _status(row)
    status_name = str(row.get("status_name") or "").strip()
    if status_name and "截单" in status_name:
        return status in {None, 4}
    return status == 4 and not status_name


def _wms_row_status_description(row: Mapping[str, Any]) -> str:
    wo_number = str(row.get("wo_number") or "-").strip() or "-"
    status = _status(row)
    status_name = str(row.get("status_name") or "").strip()
    display = status_name or (str(status) if status is not None else "空白")
    return f"{wo_number}={display}"


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
