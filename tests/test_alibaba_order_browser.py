from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from shipment_automation.alibaba_order_browser import (
    ALIBABA_QUOTE_URL,
    AlibabaDraftFacts,
    AlibabaOrderBrowser,
    choose_new_draft_url,
    is_alibaba_draft_url,
)
from shipment_automation.alibaba_ordering import (
    AlibabaOrderRuleError,
    AlibabaRoute,
    ShippingAddress,
    TentDeclaration,
)


DRAFT_A = (
    "https://scm.alibaba.com/web/express/order.htm?"
    "source=MARKET&cacheKey=one"
)
DRAFT_B = (
    "https://scm.alibaba.com/web/express/order.htm?"
    "source=MARKET&cacheKey=two"
)


def test_only_exact_https_alibaba_draft_page_is_allowed() -> None:
    assert is_alibaba_draft_url(DRAFT_A)
    assert not is_alibaba_draft_url(ALIBABA_QUOTE_URL)
    assert not is_alibaba_draft_url(
        "https://scm.alibaba.com.evil.example/web/express/order.htm"
    )
    assert not is_alibaba_draft_url("http://scm.alibaba.com/web/express/order.htm")


def test_new_draft_is_selected_relative_to_prepare_baseline() -> None:
    assert choose_new_draft_url((DRAFT_A, DRAFT_B), (DRAFT_A,)) == DRAFT_B


def test_no_new_draft_is_blocked() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="没有找到"):
        choose_new_draft_url((DRAFT_A,), (DRAFT_A,))


def test_multiple_new_drafts_are_blocked() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="多个"):
        choose_new_draft_url((DRAFT_A, DRAFT_B), ())


def test_inspect_draft_reads_stable_route_weight_and_signature_semantics() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div class="solution-line-container">
                      <span class="logistics-brand-tag-title-content">
                        Express-HK Expedited DDP
                      </span>
                    </div>
                    <input id="formData_package_0_weight" value="6">
                    <input id="formData_package_0_quantity" value="2">
                    <input id="formData_package_1_weight" value="3.5">
                    <input id="formData_package_1_quantity" value="1">
                    <label>
                      <input type="checkbox"
                             aria-label="快递签收服务 CNY 25.00">
                      快递签收服务 CNY 25.00
                    </label>
                    """
                )
                return await AlibabaOrderBrowser(page.context).inspect_draft(page)
            finally:
                await browser.close()

    facts = asyncio.run(run())

    assert facts.route.name == "Express-HK Expedited DDP"
    assert facts.route.is_ddp is True
    assert facts.route_is_expedited is True
    assert facts.total_weight_kg == Decimal("15.5")
    assert facts.signature_available is True


def test_open_product_template_dialog_blocks_draft_inspection() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div role="dialog" aria-label="选择商品">选择商品</div>
                    <div class="solution-line-container">
                      <span class="logistics-brand-tag-title-content">标准线路</span>
                    </div>
                    <input id="formData_package_0_weight" value="6">
                    """
                )
                with pytest.raises(AlibabaOrderRuleError, match="选择商品"):
                    await AlibabaOrderBrowser(page.context).inspect_draft(page)
            finally:
                await browser.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("route_is_expedited", "expedited", "message"),
    [
        (False, True, "不含 Expedited"),
        (True, False, "没有勾选"),
    ],
)
def test_expedited_checkbox_must_match_selected_route(
    route_is_expedited: bool,
    expedited: bool,
    message: str,
    monkeypatch,
) -> None:
    browser = AlibabaOrderBrowser(None)

    async def inspect_draft(_page):
        return AlibabaDraftFacts(
            url=DRAFT_A,
            route=AlibabaRoute(
                "Express Expedited" if route_is_expedited else "标准线路"
            ),
            total_weight_kg=Decimal("6"),
            route_is_expedited=route_is_expedited,
            signature_available=True,
        )

    monkeypatch.setattr(browser, "inspect_draft", inspect_draft)

    async def run():
        with pytest.raises(AlibabaOrderRuleError, match=message):
            await browser.fill_draft(
                object(),
                system_order_no="SYS-1",
                address=ShippingAddress(
                    company="Company",
                    recipient="Jane",
                    country_code="US",
                    country_name="United States",
                    province="California",
                    city="Los Angeles",
                    address1="123 Main Street",
                    address2="",
                    postal_code="90012",
                    dial_code="1",
                    phone="2135550188",
                    email="jane@example.com",
                ),
                declaration=TentDeclaration(),
                expedited=expedited,
                signature_requested=False,
            )

    asyncio.run(run())


def test_fill_draft_never_clicks_final_submit(monkeypatch) -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser_process = await playwright.chromium.launch(headless=True)
            try:
                page = await browser_process.new_page()
                await page.set_content(
                    """
                    <div class="solution-line-container">
                      <span class="logistics-brand-tag-title-content">
                        Express Expedited
                      </span>
                    </div>
                    <input id="formData_package_0_weight" value="6">
                    <input id="formData_package_0_quantity" value="1">
                    <input id="formData_product_0_nameCn" value="帐篷布顶">
                    <label>
                      <input type="checkbox"
                             aria-label="快递签收服务 CNY 25.00">
                      快递签收服务 CNY 25.00
                    </label>
                    <label>
                      客户订单号
                      <input aria-label="客户订单号">
                    </label>
                    <button id="final-submit"
                            onclick="this.dataset.clicked='true'">
                      同意协议并下单
                    </button>
                    """
                )
                adapter = AlibabaOrderBrowser(page.context)

                async def no_op(*_args, **_kwargs):
                    return None

                monkeypatch.setattr(adapter, "_fill_receiver_address", no_op)
                monkeypatch.setattr(adapter, "_fill_product", no_op)
                monkeypatch.setattr(adapter, "_verify_product", no_op)
                result = await adapter.fill_draft(
                    page,
                    system_order_no="SYS-1",
                    address=ShippingAddress(
                        company="Company",
                        recipient="Jane",
                        country_code="US",
                        country_name="United States",
                        province="California",
                        city="Los Angeles",
                        address1="123 Main Street",
                        address2="",
                        postal_code="90012",
                        dial_code="1",
                        phone="2135550188",
                        email="jane@example.com",
                    ),
                    declaration=TentDeclaration(
                        declared_unit_price_usd=Decimal("8.00")
                    ),
                    expedited=True,
                    signature_requested=False,
                )
                clicked = await page.locator("#final-submit").get_attribute(
                    "data-clicked"
                )
                customer_order = await page.get_by_role(
                    "textbox",
                    name="客户订单号",
                ).input_value()
                return result, clicked, customer_order
            finally:
                await browser_process.close()

    result, clicked, customer_order = asyncio.run(run())

    assert result.signature_selected is False
    assert clicked is None
    assert customer_order == "SYS-1"
