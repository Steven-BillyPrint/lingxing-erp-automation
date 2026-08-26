"""Authenticated Lingxing background reader used for missing address fallback."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from lingxing_automation.browser.session import is_login_page, try_auto_login
from lingxing_automation.models import LoginConfig

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

    def __init__(
        self,
        context: Any,
        login_config: LoginConfig | None = None,
    ) -> None:
        self.context = context
        self.login_config = login_config or LoginConfig()

    @staticmethod
    def _authentication_required(
        response: Any,
        payload: Mapping[str, Any],
    ) -> bool:
        status = int(getattr(response, "status", 0) or 0)
        if status in {401, 403}:
            return True
        detail = str(payload.get("msg") or payload.get("message") or "").casefold()
        return any(
            marker in detail
            for marker in (
                "未登录",
                "登录已过期",
                "登录过期",
                "not logged",
                "login required",
                "login expired",
            )
        )

    async def _request_order_detail(
        self,
        system_order_no: str,
    ) -> tuple[Any, Mapping[str, Any]]:
        request_context = getattr(self.context, "request", None)
        if request_context is None:
            raise AlibabaOrderRuleError(
                "本机 Chrome 登录状态无法用于后台读取领星订单详情。"
            )
        try:
            response = await request_context.get(
                f"https://{LINGXING_ERP_HOST}{LINGXING_ORDER_DETAIL_API_PATH}",
                params={
                    "global_order_no": system_order_no,
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
        return response, payload

    async def _restore_authenticated_session(
        self,
        system_order_no: str,
    ) -> Mapping[str, Any]:
        if not self.login_config.has_credentials:
            raise AlibabaOrderRuleError(
                "本机 Chrome 的领星登录已失效，且当前授权配置没有完整的"
                "领星网页账号密码。浏览器 Cookie 不会跟随授权文件迁移。"
            )

        pages = tuple(getattr(self.context, "pages", ()) or ())
        page = next(
            (
                candidate
                for candidate in pages
                if is_lingxing_erp_url(getattr(candidate, "url", ""))
            ),
            None,
        )
        created_page = page is None
        if page is None:
            try:
                page = await self.context.new_page()
            except Exception as exc:
                raise AlibabaOrderRuleError(
                    "本机 Chrome 的领星登录已失效，且无法打开领星登录页。"
                ) from exc

        try:
            await page.goto(
                LINGXING_ORDER_MANAGEMENT_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            if await is_login_page(page):
                await try_auto_login(page, self.login_config)

            # Login navigation and cookie propagation finish asynchronously.
            # Verify the exact protected endpoint rather than trusting the page URL.
            deadline = asyncio.get_running_loop().time() + 15
            while True:
                response, payload = await self._request_order_detail(system_order_no)
                if not self._authentication_required(response, payload):
                    if created_page:
                        try:
                            await page.close()
                        except Exception:
                            pass
                    return payload
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(1)
        except AlibabaOrderRuleError:
            raise
        except Exception as exc:
            raise AlibabaOrderRuleError(
                "本机 Chrome 的领星登录已失效，自动恢复登录失败。"
            ) from exc

        try:
            await page.bring_to_front()
        except Exception:
            pass
        raise AlibabaOrderRuleError(
            "已在本机 Chrome 打开领星登录页并使用授权配置尝试登录，"
            "但会话仍未生效。请在该页完成验证码、手机或设备验证后重试；"
            "这不是阿里账号登录失败。"
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
        response, payload = await self._request_order_detail(normalized)
        response_ok = bool(getattr(response, "ok", False))
        code = payload.get("code")
        data = payload.get("data")
        if (
            (not response_ok or str(code or "").strip() != "1")
            and self._authentication_required(response, payload)
        ):
            payload = await self._restore_authenticated_session(normalized)
            response_ok = True
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
