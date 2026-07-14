from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar


class Capability(StrEnum):
    LIST_ORDERS = "list_orders"
    GET_ORDER_DETAIL = "get_order_detail"
    DOWNLOAD_ATTACHMENT = "download_attachment"
    UPDATE_PHONE = "update_phone"
    UPDATE_BUYER_EMAIL = "update_buyer_email"
    UPDATE_ORDER_ITEMS = "update_order_items"
    SPLIT_ORDER = "split_order"
    UPDATE_REMARK = "update_remark"
    SET_SHIPPING_CHANNEL = "set_shipping_channel"
    REVIEW_ORDER = "review_order"
    UPDATE_TRACKING = "update_tracking"
    OUTBOUND_ORDER = "outbound_order"
    READ_FULL_ADDRESS = "read_full_address"
    ALIBABA_LOGISTICS = "alibaba_logistics"
    SEND_EMAIL = "send_email"


class CapabilityMode(StrEnum):
    API_PREFERRED = "api_preferred"
    BROWSER_ONLY = "browser_only"
    MANUAL_APPROVAL = "manual_approval"
    DISABLED = "disabled"


DEFAULT_CAPABILITY_MODES: dict[Capability, CapabilityMode] = {
    Capability.LIST_ORDERS: CapabilityMode.API_PREFERRED,
    Capability.GET_ORDER_DETAIL: CapabilityMode.API_PREFERRED,
    Capability.DOWNLOAD_ATTACHMENT: CapabilityMode.API_PREFERRED,
    Capability.UPDATE_PHONE: CapabilityMode.API_PREFERRED,
    Capability.UPDATE_BUYER_EMAIL: CapabilityMode.BROWSER_ONLY,
    Capability.UPDATE_ORDER_ITEMS: CapabilityMode.API_PREFERRED,
    Capability.SPLIT_ORDER: CapabilityMode.MANUAL_APPROVAL,
    Capability.UPDATE_REMARK: CapabilityMode.API_PREFERRED,
    Capability.SET_SHIPPING_CHANNEL: CapabilityMode.API_PREFERRED,
    Capability.REVIEW_ORDER: CapabilityMode.API_PREFERRED,
    Capability.UPDATE_TRACKING: CapabilityMode.API_PREFERRED,
    Capability.OUTBOUND_ORDER: CapabilityMode.API_PREFERRED,
    Capability.READ_FULL_ADDRESS: CapabilityMode.BROWSER_ONLY,
    Capability.ALIBABA_LOGISTICS: CapabilityMode.BROWSER_ONLY,
    Capability.SEND_EMAIL: CapabilityMode.DISABLED,
}


class MutationState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class MutationResult:
    state: MutationState
    source: str
    request_id: str | None = None
    message: str = ""
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None
    definitely_not_executed: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)


class CapabilityUnavailable(RuntimeError):
    """当前适配器不支持某项能力。"""


class ManualReviewRequired(RuntimeError):
    def __init__(self, capability: Capability, message: str, *, result: MutationResult | None = None):
        super().__init__(message)
        self.capability = capability
        self.result = result


T = TypeVar("T")
MaybeAsync = T | Awaitable[T]


async def _resolve(value: MaybeAsync[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


class CapabilityRouter:
    """API/网页能力级路由器。

    读取可以在 API 明确失败时自动切网页；写入结果为 UNKNOWN 时永不自动
    回退，防止同一个拆单、审核或出库动作执行两次。
    """

    def __init__(
        self,
        modes: Mapping[Capability | str, CapabilityMode | str] | None = None,
        *,
        writes_enabled: bool | Callable[[], bool] = True,
        force_legacy: bool = False,
    ):
        self._modes = dict(DEFAULT_CAPABILITY_MODES)
        for key, value in (modes or {}).items():
            self._modes[Capability(key)] = CapabilityMode(value)
        self._writes_enabled = writes_enabled
        self.force_legacy = force_legacy

    @property
    def writes_enabled(self) -> bool:
        value = self._writes_enabled
        return bool(value()) if callable(value) else bool(value)

    @writes_enabled.setter
    def writes_enabled(self, value: bool | Callable[[], bool]) -> None:
        self._writes_enabled = value

    def mode_for(self, capability: Capability | str) -> CapabilityMode:
        capability = Capability(capability)
        if self.force_legacy and capability != Capability.SEND_EMAIL:
            return CapabilityMode.BROWSER_ONLY
        return self._modes.get(capability, CapabilityMode.BROWSER_ONLY)

    def set_mode(self, capability: Capability | str, mode: CapabilityMode | str) -> None:
        self._modes[Capability(capability)] = CapabilityMode(mode)

    async def execute_read(
        self,
        capability: Capability | str,
        *,
        api: Callable[[], MaybeAsync[T]] | None,
        browser: Callable[[], MaybeAsync[T]] | None,
    ) -> T:
        capability = Capability(capability)
        mode = self.mode_for(capability)
        if mode == CapabilityMode.DISABLED:
            raise CapabilityUnavailable(f"能力已禁用：{capability}")
        if mode == CapabilityMode.BROWSER_ONLY:
            if browser is None:
                raise CapabilityUnavailable(f"没有网页实现：{capability}")
            return await _resolve(browser())
        if api is None:
            if browser is None:
                raise CapabilityUnavailable(f"没有 API 或网页实现：{capability}")
            return await _resolve(browser())
        try:
            return await _resolve(api())
        except (CapabilityUnavailable, TimeoutError, ConnectionError):
            if browser is None:
                raise
            return await _resolve(browser())

    async def execute_write(
        self,
        capability: Capability | str,
        *,
        api: Callable[[], MaybeAsync[MutationResult]] | None,
        browser: Callable[[], MaybeAsync[MutationResult]] | None,
        approve_browser_fallback: Callable[[Capability, MutationResult | None], MaybeAsync[bool]] | None = None,
    ) -> MutationResult:
        capability = Capability(capability)
        if not self.writes_enabled:
            return MutationResult(
                state=MutationState.DISABLED,
                source="router",
                message="ERP 写入紧急开关已关闭。",
            )
        mode = self.mode_for(capability)
        if mode == CapabilityMode.DISABLED:
            return MutationResult(
                state=MutationState.DISABLED,
                source="router",
                message=f"能力已禁用：{capability}",
            )
        if mode == CapabilityMode.BROWSER_ONLY:
            if browser is None:
                raise CapabilityUnavailable(f"没有网页实现：{capability}")
            return await _resolve(browser())

        api_result: MutationResult | None = None
        if api is not None:
            try:
                api_result = await _resolve(api())
            except CapabilityUnavailable:
                api_result = MutationResult(
                    state=MutationState.FAILED,
                    source="api",
                    message="API 不支持当前订单。",
                    definitely_not_executed=True,
                )
            except (TimeoutError, ConnectionError) as exc:
                api_result = MutationResult(
                    state=MutationState.UNKNOWN,
                    source="api",
                    message=str(exc),
                )
            if api_result.state == MutationState.SUCCEEDED:
                return api_result
            if api_result.state in {MutationState.UNKNOWN, MutationState.MANUAL_REVIEW}:
                raise ManualReviewRequired(
                    capability,
                    "API 写入结果不明确，必须读回或人工确认，禁止自动网页重试。",
                    result=api_result,
                )
            if not api_result.definitely_not_executed:
                raise ManualReviewRequired(
                    capability,
                    "无法证明 API 未执行，禁止自动网页回退。",
                    result=api_result,
                )

        if browser is None:
            if api_result is not None:
                return api_result
            raise CapabilityUnavailable(f"没有可用实现：{capability}")
        if api_result is not None and api_result.details.get("browser_fallback_forbidden"):
            raise ManualReviewRequired(
                capability,
                "API 响应已明确禁止网页回退，必须人工核对。",
                result=api_result,
            )
        if mode == CapabilityMode.MANUAL_APPROVAL or api_result is not None:
            approved = False
            if approve_browser_fallback is not None:
                approved = bool(await _resolve(approve_browser_fallback(capability, api_result)))
            if not approved:
                raise ManualReviewRequired(
                    capability,
                    "网页写入需要人工确认。",
                    result=api_result,
                )
        return await _resolve(browser())
