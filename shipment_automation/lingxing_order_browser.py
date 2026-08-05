"""Authenticated Lingxing page adapter used only for missing address fallback."""

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

    async def _authenticated_page(self) -> Any:
        page = next(
            (
                item
                for item in self.context.pages
                if is_lingxing_erp_url(getattr(item, "url", ""))
            ),
            None,
        )
        if page is None:
            page = await self.context.new_page()
            try:
                await page.goto(
                    LINGXING_ORDER_MANAGEMENT_URL,
                    wait_until="domcontentloaded",
                )
            except Exception as exc:
                raise AlibabaOrderRuleError(
                    "领星订单页面打开失败，请检查网络并确认本机领星网页已登录。"
                ) from exc
        if not is_lingxing_erp_url(getattr(page, "url", "")):
            raise AlibabaOrderRuleError(
                "本机 Chrome 未进入领星 ERP，请先完成领星网页登录或验证后重试。"
            )
        return page

    async def receive_info(self, system_order_no: str) -> dict[str, Any]:
        """Return current receive_info and prove it belongs to the requested order."""

        normalized = str(system_order_no or "").strip()
        if not normalized:
            raise AlibabaOrderRuleError("领星系统单号不能为空。")
        page = await self._authenticated_page()
        try:
            result = await page.evaluate(
                """
                async ({ systemOrderNo, path }) => {
                    const sequence = `${path}$$4`;
                    const query = new URLSearchParams({
                        global_order_no: systemOrderNo,
                        req_time_sequence: sequence,
                    });
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 10000);
                    try {
                        const response = await fetch(`${path}?${query.toString()}`, {
                            method: 'GET',
                            credentials: 'include',
                            headers: { Accept: 'application/json' },
                            signal: controller.signal,
                        });
                        let payload = null;
                        try {
                            payload = await response.json();
                        } catch (_error) {
                            return {
                                ok: false,
                                error: '领星网页订单详情接口没有返回有效 JSON。',
                                http_status: response.status,
                            };
                        }
                        const data = payload && typeof payload.data === 'object'
                            ? payload.data
                            : {};
                        const receiveInfo = data && typeof data.receive_info === 'object'
                            ? data.receive_info
                            : null;
                        return {
                            ok: response.ok && Number(payload?.code) === 1,
                            http_status: response.status,
                            code: payload?.code,
                            message: String(payload?.msg || ''),
                            request_id: String(payload?.require_id || ''),
                            global_order_no: String(data?.global_order_no || ''),
                            receive_info: receiveInfo,
                        };
                    } catch (error) {
                        return {
                            ok: false,
                            error: error && error.name === 'AbortError'
                                ? '领星网页订单详情接口读取超时。'
                                : '领星网页订单详情接口读取失败。',
                        };
                    } finally {
                        clearTimeout(timer);
                    }
                }
                """,
                {
                    "systemOrderNo": normalized,
                    "path": LINGXING_ORDER_DETAIL_API_PATH,
                },
            )
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "无法通过本机领星网页读取订单地址，请确认登录状态后重试。"
            ) from exc
        if not isinstance(result, Mapping):
            raise AlibabaOrderRuleError("领星网页订单详情接口返回结构无效。")
        if not result.get("ok"):
            detail = str(
                result.get("error")
                or result.get("message")
                or f"HTTP {result.get('http_status') or '-'}"
            ).strip()
            raise AlibabaOrderRuleError(
                f"领星网页订单详情读取失败：{detail}"
            )
        returned = str(result.get("global_order_no") or "").strip()
        if returned != normalized:
            raise AlibabaOrderRuleError(
                "领星网页订单详情返回的系统单号与请求不一致，已停止以避免填写错误地址。"
            )
        receive_info = result.get("receive_info")
        if not isinstance(receive_info, Mapping) or not receive_info:
            raise AlibabaOrderRuleError("领星网页订单详情缺少收货信息。")
        return dict(receive_info)
