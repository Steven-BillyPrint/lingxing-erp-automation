"""Authenticated Lingxing background reader used for missing address fallback."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .alibaba_ordering import AlibabaOrderRuleError


LINGXING_ERP_HOST = "erp.lingxing.com"
LINGXING_ORDER_MANAGEMENT_URL = (
    "https://erp.lingxing.com/erp/mmulti/mpOrderManagement"
)
LINGXING_ORDER_DETAIL_API_PATH = "/api/platforms/oms/order_list/detail"


def is_lingxing_erp_url(value: object) -> bool:
    parsed = urlparse(str(value or ""))
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == LINGXING_ERP_HOST
    )


class LingxingOrderBrowser:
    """Read one verified order detail through the submitting user's ERP login."""

    def __init__(self, context: Any) -> None:
        self.context = context

    async def order_detail(self, system_order_no: str) -> dict[str, Any]:
        """Return the complete current detail after proving its order identity.

        The web endpoint keeps the street address under ``receive_info`` but
        some marketplaces keep the buyer email under ``buyer_info``.  Returning
        only ``receive_info`` silently dropped that required field.
        """

        normalized = str(system_order_no or "").strip()
        if not normalized:
            raise AlibabaOrderRuleError("领星系统单号不能为空。")
        request_context = getattr(self.context, "request", None)
        if request_context is None:
            raise AlibabaOrderRuleError(
                "本机 Chrome 登录状态无法用于后台读取领星订单详情。"
            )
        try:
            response = await request_context.get(
                f"https://{LINGXING_ERP_HOST}{LINGXING_ORDER_DETAIL_API_PATH}",
                params={
                    "global_order_no": normalized,
                    "req_time_sequence": f"{LINGXING_ORDER_DETAIL_API_PATH}$$4",
                },
                headers={
                    "Accept": "application/json",
                    "Referer": LINGXING_ORDER_MANAGEMENT_URL,
                },
                timeout=10000,
            )
            payload = await response.json()
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "无法通过本机 Chrome 登录状态后台读取领星订单地址，"
                "请确认领星登录仍有效后重试。"
            ) from exc
        if not isinstance(payload, Mapping):
            raise AlibabaOrderRuleError("领星网页订单详情接口返回结构无效。")
        response_ok = bool(getattr(response, "ok", False))
        code = payload.get("code")
        data = payload.get("data")
        if not response_ok or str(code or "").strip() != "1":
            detail = str(
                payload.get("msg")
                or payload.get("message")
                or f"HTTP {getattr(response, 'status', '-')}"
            ).strip()
            raise AlibabaOrderRuleError(
                f"领星网页订单详情读取失败：{detail}"
            )
        if not isinstance(data, Mapping):
            raise AlibabaOrderRuleError("领星网页订单详情接口缺少订单数据。")
        returned = str(data.get("global_order_no") or "").strip()
        if returned != normalized:
            raise AlibabaOrderRuleError(
                "领星网页订单详情返回的系统单号与请求不一致，已停止以避免填写错误地址。"
            )
        receive_info = data.get("receive_info")
        if not isinstance(receive_info, Mapping) or not receive_info:
            raise AlibabaOrderRuleError("领星网页订单详情缺少收货信息。")
        return dict(data)

    async def receive_info(self, system_order_no: str) -> dict[str, Any]:
        """Compatibility helper returning only the verified receive_info."""

        detail = await self.order_detail(system_order_no)
        receive_info = detail.get("receive_info")
        if not isinstance(receive_info, Mapping) or not receive_info:
            raise AlibabaOrderRuleError("领星网页订单详情缺少收货信息。")
        return dict(receive_info)
