"""Authenticated Lingxing background reader used for missing address fallback."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from erp_automation.integrations.lingxing.internal_orders import (
    InternalOrderError,
    LingxingInternalOrderClient,
    ORDER_DETAIL_PATH,
    ORDER_MANAGEMENT_URL,
)

from .alibaba_ordering import AlibabaOrderRuleError


LINGXING_ERP_HOST = "erp.lingxing.com"
LINGXING_ORDER_MANAGEMENT_URL = ORDER_MANAGEMENT_URL
LINGXING_ORDER_DETAIL_API_PATH = ORDER_DETAIL_PATH


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
        request_context = getattr(context, "request", None)
        self._client = (
            LingxingInternalOrderClient(request_context)
            if request_context is not None
            else None
        )

    async def order_detail(self, system_order_no: str) -> dict[str, Any]:
        """Return the complete current detail after proving its order identity.

        The web endpoint keeps the street address under ``receive_info`` but
        some marketplaces keep the buyer email under ``buyer_info``.  Returning
        only ``receive_info`` silently dropped that required field.
        """

        normalized = str(system_order_no or "").strip()
        if not normalized:
            raise AlibabaOrderRuleError("领星系统单号不能为空。")
        if self._client is None:
            raise AlibabaOrderRuleError(
                "本机 Chrome 登录状态无法用于后台读取领星订单详情。"
            )
        try:
            return await self._client.raw_order_detail(normalized)
        except InternalOrderError as exc:
            raise AlibabaOrderRuleError(
                str(exc)
            ) from exc

    async def receive_info(self, system_order_no: str) -> dict[str, Any]:
        """Compatibility helper returning only the verified receive_info."""

        detail = await self.order_detail(system_order_no)
        receive_info = detail.get("receive_info")
        if not isinstance(receive_info, dict) or not receive_info:
            raise AlibabaOrderRuleError("领星网页订单详情缺少收货信息。")
        return dict(receive_info)
