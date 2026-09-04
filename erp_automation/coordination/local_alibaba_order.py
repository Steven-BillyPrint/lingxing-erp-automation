"""Alibaba order actions executed beside the submitting desktop's Chrome."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from erp_automation.contracts.models import (
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
    LOCAL_BROWSER_ACTION_ALIBABA_ORDER_PREPARE,
    DesktopWriteAction,
    DesktopWriteConfirmation,
)


class LocalAlibabaOrderActionExecutor:
    """Run the two whitelisted order-page operations on the submitting PC."""

    def __init__(self, browser_endpoint: str) -> None:
        self.browser_endpoint = str(browser_endpoint or "").strip()

    def execute(
        self,
        action: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        normalized = str(action or "").strip()
        values = dict(payload or {})
        if normalized == LOCAL_BROWSER_ACTION_ALIBABA_ORDER_PREPARE:
            return asyncio.run(self._prepare(values))
        if normalized == LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL:
            return asyncio.run(self._fill(values))
        raise ValueError("本机浏览器步骤不在允许列表中。")

    @staticmethod
    def _mapping(value: object, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"本机浏览器步骤缺少{label}。")
        return dict(value)

    @classmethod
    def _login_config(cls, payload: Mapping[str, Any]) -> Any:
        from shipment_automation.config import AlibabaLoginConfig

        values = cls._mapping(payload.get("login_config"), "阿里登录配置")
        return AlibabaLoginConfig(
            account=str(values.get("account") or "") or None,
            password=str(values.get("password") or "") or None,
            auto_login=bool(values.get("auto_login", True)),
        )

    @staticmethod
    async def _shipping_address(
        detail: Mapping[str, Any],
    ) -> tuple[Any, str]:
        from shipment_automation.alibaba_ordering import (
            AlibabaOrderRuleError,
            extract_shipping_address,
        )

        try:
            return extract_shipping_address(detail), "lingxing_openapi"
        except AlibabaOrderRuleError as exc:
            raise AlibabaOrderRuleError(
                f"领星公开 API 订单列表地址不完整：{exc}"
            ) from exc

    async def _prepare(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        from shipment_automation.alibaba_order_browser import (
            AlibabaOrderBrowser,
            attached_alibaba_context,
        )

        detail = self._mapping(payload.get("detail"), "订单详情")
        system_order_no = str(payload.get("system_order_no") or "").strip()
        if not system_order_no:
            raise ValueError("本机浏览器步骤缺少系统单号。")
        login_config = self._login_config(payload)
        async with attached_alibaba_context(self.browser_endpoint) as context:
            browser = AlibabaOrderBrowser(context)
            baseline = await browser.draft_urls()
            address_task = asyncio.create_task(
                self._shipping_address(detail)
            )
            quote_task = asyncio.create_task(
                browser.prepare_quote_page(login_config=login_config)
            )
            tasks = (address_task, quote_task)
            try:
                (address, address_source), quote_page = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await quote_page.bring_to_front()
        return {
            "address": asdict(address),
            "address_source": address_source,
            "baseline_draft_urls": list(baseline),
        }

    async def _fill(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        from shipment_automation.alibaba_order_browser import (
            AlibabaOrderBrowser,
            attached_alibaba_context,
            choose_new_draft_url,
        )
        from shipment_automation.alibaba_ordering import (
            AlibabaOrderRuleError,
            ProductCategory,
            product_declaration,
        )
        from shipment_automation.alibaba_product_classification import (
            classify_order_product,
            order_contains_tent_frame_sku,
        )

        detail = self._mapping(payload.get("detail"), "订单详情")
        confirmation_payload = self._mapping(
            payload.get("confirmation"),
            "桌面写入确认",
        )
        command_order_no = str(payload.get("command_order_no") or "").strip()
        confirmation = DesktopWriteConfirmation.from_payload(
            {DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation_payload}
        )
        confirmation.require_matches(
            DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT,
            command_order_no,
            system_order_no=command_order_no,
        )
        system_order_no = str(payload.get("system_order_no") or "").strip()
        platform_order_no = str(payload.get("platform_order_no") or "").strip()
        baseline = tuple(
            str(value)
            for value in payload.get("baseline_draft_urls") or ()
            if str(value or "").strip()
        )
        login_config = self._login_config(payload)
        expedited = bool(payload.get("expedited"))
        signature_requested = bool(payload.get("signature_requested"))
        heavy_or_frame = bool(payload.get("heavy_or_frame")) or (
            order_contains_tent_frame_sku(detail)
        )
        expected_category = str(payload.get("category") or "").strip()
        if expected_category:
            classification = classify_order_product(detail)
            if expected_category != str(classification.category):
                raise AlibabaOrderRuleError(
                    "本机识别的商品分类与查价准备记录不一致，请重新准备阿里查价。"
                )
            category = classification.category
        else:
            # Older queued local actions were tent-only and did not include a
            # category. Preserve that safe compatibility path during upgrades.
            category = ProductCategory.TENT

        async with attached_alibaba_context(self.browser_endpoint) as context:
            browser = AlibabaOrderBrowser(context)

            async def load_page_and_facts() -> tuple[Any, Any]:
                target_url = choose_new_draft_url(
                    await browser.draft_urls(),
                    baseline,
                )
                page = await browser.page_for_url(target_url)
                await browser.ensure_logged_in(
                    page,
                    login_config,
                    return_url=target_url,
                    page_label="阿里下单草稿页",
                )
                return page, await browser.inspect_draft(page)

            address_task = asyncio.create_task(
                self._shipping_address(detail)
            )
            draft_task = asyncio.create_task(load_page_and_facts())
            tasks = (address_task, draft_task)
            try:
                (address, address_source), (page, facts) = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            declaration = product_declaration(
                category=category,
                destination_country_code=address.country_code,
                total_weight_kg=facts.total_weight_kg,
                route=facts.route,
                expedited=expedited,
                heavy_or_frame=heavy_or_frame,
            )
            started = time.monotonic()
            result = await browser.fill_draft(
                page,
                customer_order_no=platform_order_no or system_order_no,
                address=address,
                declaration=declaration,
                expedited=expedited,
                signature_requested=signature_requested,
                facts=facts,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)

        return {
            "address_source": address_source,
            "route_name": result.route_name,
            "total_weight_kg": str(result.total_weight_kg),
            "declared_unit_price_usd": str(result.declared_unit_price_usd),
            "signature_selected": result.signature_selected,
            "signature_fee_text": result.signature_fee_text,
            "form_fill_elapsed_ms": elapsed_ms,
            "heavy_or_frame": heavy_or_frame,
            "alibaba_submit_calls": 0,
        }
