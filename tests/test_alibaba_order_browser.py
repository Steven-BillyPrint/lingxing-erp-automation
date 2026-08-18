from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from shipment_automation.alibaba_order_browser import (
    ALIBABA_QUOTE_URL,
    AlibabaDraftFacts,
    AlibabaOrderBrowser,
    choose_new_draft_url,
    is_alibaba_draft_url,
    is_alibaba_quote_url,
)
from shipment_automation.alibaba_ordering import (
    AlibabaOrderRuleError,
    AlibabaRoute,
    ShippingAddress,
    TentDeclaration,
)
from shipment_automation.config import AlibabaLoginConfig


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


def test_quote_page_accepts_safe_query_parameters_but_not_lookalike_hosts() -> None:
    assert is_alibaba_quote_url(ALIBABA_QUOTE_URL)
    assert is_alibaba_quote_url(f"{ALIBABA_QUOTE_URL}?spm=safe")
    assert not is_alibaba_quote_url(
        "https://i.alibaba.com.evil.example/logistics/web/shipping/query"
    )


def test_open_quote_page_only_brings_the_ready_page_to_front(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakePage:
        async def bring_to_front(self):
            observed["brought_to_front"] = True

    browser = AlibabaOrderBrowser(object())

    async def prepare_quote_page(*, login_config=None):
        observed["login_config"] = login_config
        return FakePage()

    monkeypatch.setattr(browser, "prepare_quote_page", prepare_quote_page)
    login_config = AlibabaLoginConfig(account="account", password="password")

    asyncio.run(browser.open_quote_page(login_config=login_config))

    assert observed == {
        "login_config": login_config,
        "brought_to_front": True,
    }


def test_local_chrome_attach_retries_transient_cdp_startup_failure(
    monkeypatch,
) -> None:
    from playwright import async_api
    from shipment_automation import alibaba_order_browser

    context = object()
    observed: dict[str, object] = {
        "connect_calls": 0,
        "connect_timeouts": [],
        "retry_delays": [],
        "stop_calls": 0,
    }

    class Chromium:
        async def connect_over_cdp(self, endpoint, *, timeout):
            observed["connect_calls"] = int(observed["connect_calls"]) + 1
            observed["endpoint"] = endpoint
            observed["connect_timeouts"].append(timeout)
            if observed["connect_calls"] < 3:
                raise ConnectionError("Chrome websocket is still starting")
            return SimpleNamespace(contexts=[context])

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            observed["stop_calls"] = int(observed["stop_calls"]) + 1

    class PlaywrightStarter:
        async def start(self):
            return Playwright()

    async def retry_sleep(delay):
        observed["retry_delays"].append(delay)

    monkeypatch.setattr(async_api, "async_playwright", lambda: PlaywrightStarter())
    monkeypatch.setattr(alibaba_order_browser.asyncio, "sleep", retry_sleep)

    async def run():
        async with alibaba_order_browser.attached_alibaba_context(
            "http://127.0.0.1:28076"
        ) as attached:
            assert attached is context

    asyncio.run(run())

    assert observed == {
        "connect_calls": 3,
        "connect_timeouts": [4_000, 4_000, 4_000],
        "retry_delays": [0.35, 0.35],
        "stop_calls": 1,
        "endpoint": "http://127.0.0.1:28076",
    }


def test_local_chrome_attach_reports_bounded_retry_exhaustion(monkeypatch) -> None:
    from playwright import async_api
    from shipment_automation import alibaba_order_browser

    observed = {"connect_calls": 0, "stop_calls": 0}

    class Chromium:
        async def connect_over_cdp(self, _endpoint, *, timeout):
            assert timeout == 4_000
            observed["connect_calls"] += 1
            raise ConnectionError("Chrome websocket unavailable")

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            observed["stop_calls"] += 1

    class PlaywrightStarter:
        async def start(self):
            return Playwright()

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(async_api, "async_playwright", lambda: PlaywrightStarter())
    monkeypatch.setattr(alibaba_order_browser.asyncio, "sleep", no_sleep)

    async def run():
        with pytest.raises(
            AlibabaOrderRuleError,
            match="已自动重试 3 次",
        ):
            async with alibaba_order_browser.attached_alibaba_context(
                "http://127.0.0.1:28076"
            ):
                pytest.fail("persistent CDP failure must not yield a context")

    asyncio.run(run())

    assert observed == {"connect_calls": 3, "stop_calls": 1}


def test_quote_login_uses_configured_credentials_and_returns_to_quote(
    monkeypatch,
) -> None:
    from shipment_automation import alibaba_order_browser
    observed = {}
    login_states = iter((True, False, False, False))

    async def is_login(_page):
        return next(login_states)

    async def auto_login(_page, config):
        observed["account"] = config.account
        observed["password"] = config.password
        page.url = "https://www.alibaba.com/"
        return True

    class FakePage:
        url = "https://login.alibaba.com/"

        def __init__(self):
            self.gotos = []
            self.waits = []

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        async def goto(self, url, **_kwargs):
            self.gotos.append(url)
            self.url = url

    page = FakePage()
    monkeypatch.setattr(alibaba_order_browser, "is_alibaba_login_page", is_login)
    monkeypatch.setattr(alibaba_order_browser, "try_alibaba_auto_login", auto_login)

    asyncio.run(
        AlibabaOrderBrowser._ensure_quote_login(
            page,
            AlibabaLoginConfig(
                account="configured@example.com",
                password="configured-password",
                auto_login=True,
            ),
        )
    )

    assert observed == {
        "account": "configured@example.com",
        "password": "configured-password",
    }
    assert page.gotos == [ALIBABA_QUOTE_URL]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (AlibabaLoginConfig(account="user", password="password", auto_login=False), "关闭"),
        (AlibabaLoginConfig(account="user", password=None, auto_login=True), "没有完整填写"),
    ],
)
def test_quote_login_requires_enabled_complete_configuration(
    config,
    message,
    monkeypatch,
) -> None:
    from shipment_automation import alibaba_order_browser

    async def is_login(_page):
        return True

    monkeypatch.setattr(alibaba_order_browser, "is_alibaba_login_page", is_login)

    async def run():
        with pytest.raises(AlibabaOrderRuleError, match=message):
            await AlibabaOrderBrowser._ensure_quote_login(object(), config)

    asyncio.run(run())


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
                customer_order_no="SYS-1",
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


def _receiver_address_dialog_html(
    country_value: str,
    *,
    edit_label: str = "编辑",
    dialog_label: str = "修改地址",
    country_delay_ms: int = 0,
    select_mirror_delay_ms: int = 0,
) -> str:
    return f"""
    <button class="edit icon-margin-right">{edit_label}</button>
    <section id="receiver-card">
      <button class="edit icon-margin-right">{edit_label}</button>
      <span id="receiver-summary"></span>
    </section>
    <div role="dialog" aria-label="{dialog_label}"
         class="ant-modal custom-address-dialog" style="display:none">
      <div class="ant-select ant-select-disabled ant-select-show-search">
        <div class="ant-select-selector">
          <span class="ant-select-selection-wrap">
            <span class="ant-select-selection-search">
              <input id="address_country" value="" readonly>
            </span>
          </span>
        </div>
      </div>
      <input id="companyNameEn">
      <div class="ant-select">
        <div class="ant-select-selector">
          <span class="ant-select-selection-wrap">
            <span class="ant-select-selection-search">
              <input id="address_province" aria-controls="address_province_list">
            </span>
          </span>
        </div>
      </div>
      <div class="ant-select">
        <div class="ant-select-selector">
          <span class="ant-select-selection-wrap">
            <span class="ant-select-selection-search">
              <input id="address_city" aria-controls="address_city_list">
            </span>
          </span>
        </div>
      </div>
      <div class="ant-select">
        <div class="ant-select-selector">
          <span class="ant-select-selection-wrap">
            <span class="ant-select-selection-search">
              <input id="address_address" aria-controls="address_address_list">
            </span>
          </span>
        </div>
      </div>
      <div class="ant-select-dropdown"><div>
        <div id="address_province_list"></div>
        <div class="ant-select-item-option" title="Florida">Florida</div>
      </div></div>
      <div class="ant-select-dropdown"><div>
        <div id="address_city_list"></div>
        <div class="ant-select-item-option" title="Miami Beach">Miami Beach</div>
        <div class="ant-select-item-option" title="Miami">Miami</div>
      </div></div>
      <div class="ant-select-dropdown"><div>
        <div id="address_address_list"></div>
        <div class="ant-select-item-option" title="123 Main Street">
          123 Main Street<br>Miami, FL, USA
        </div>
        <div class="ant-select-item-option" title="123 Main St">
          123 Main St<br>Miami, IA, USA
        </div>
        <div class="ant-select-item-option" title="123 Main Avenue">
          123 Main Avenue<br>Miami, NE, USA
        </div>
      </div></div>
      <input id="address_province_name">
      <input id="address_city_name">
      <input id="address_address2">
      <input id="address_zip">
      <input id="contactPerson">
      <input id="contact_phoneCode">
      <input id="contact_mobileNo">
      <input id="contact_email">
      <div class="ant-modal-footer">
        <button id="cancel">取消</button>
        <button id="confirm">确定</button>
      </div>
    </div>
    <script>
      const dialog = document.querySelector('[role="dialog"]');
      document.querySelectorAll('.edit')[1].addEventListener('click', () => {{
        dialog.style.display = 'block';
        const countryRoot = document.querySelector('#address_country')
            .closest('.ant-select');
        if (!countryRoot.querySelector('.ant-select-selection-item')) {{
          setTimeout(() => {{
            const item = document.createElement('span');
            item.className = 'ant-select-selection-item';
            item.title = '{country_value}';
            item.textContent = '{country_value}';
            document.querySelector('#address_country')
                .closest('.ant-select-selection-wrap')
                .appendChild(item);
          }}, {country_delay_ms});
        }}
      }});
      document.querySelectorAll('.ant-select-item-option').forEach(option => {{
        option.addEventListener('click', event => {{
          event.currentTarget.dataset.clicked = 'true';
          const list = event.currentTarget.parentElement
              .querySelector('[id$="_list"]');
          const inputId = list.id.replace(/_list$/, '');
          const input = document.getElementById(inputId);
          const selectedValue = event.currentTarget.title;
          if (inputId === 'address_address') {{
            input.value = selectedValue;
            return;
          }}
          input.value = '';
          const wrap = input.closest('.ant-select-selection-wrap');
          let item = wrap.querySelector('.ant-select-selection-item');
          if (!item) {{
            item = document.createElement('span');
            item.className = 'ant-select-selection-item';
            wrap.appendChild(item);
          }}
           item.title = selectedValue;
           item.textContent = selectedValue;
           const mirror = document.getElementById(`${{inputId}}_name`);
           if (mirror) {{
             setTimeout(() => {{ mirror.value = selectedValue; }},
                        {select_mirror_delay_ms});
           }}
        }});
      }});
      document.getElementById('address_address').addEventListener(
          'keydown', event => {{
            if (event.key !== 'Enter') return;
            event.currentTarget.value = document.querySelector(
                '#address_address_list ~ .ant-select-item-option'
            ).title;
          }}
      );
      document.getElementById('confirm').addEventListener('click', event => {{
        event.target.dataset.clicks = String(
          Number(event.target.dataset.clicks || '0') + 1
        );
        const selected = id => {{
          const item = document.getElementById(id)
              .closest('.ant-select')
              .querySelector('.ant-select-selection-item');
          return item ? item.textContent : '';
        }};
        document.getElementById('receiver-summary').textContent = [
          document.getElementById('address_address').value,
          selected('address_city'),
          selected('address_province'),
          document.getElementById('address_zip').value,
        ].join(' ');
        dialog.style.display = 'none';
      }});
      document.getElementById('cancel').addEventListener('click', event => {{
        event.target.dataset.clicks = String(
          Number(event.target.dataset.clicks || '0') + 1
        );
        dialog.style.display = 'none';
      }});
    </script>
    """


def _receiver_address() -> ShippingAddress:
    return ShippingAddress(
        company="Example Cooperative",
        recipient="Jane Smith",
        country_code="US",
        country_name="United States",
        province="Florida",
        city="Miami",
        address1="123 Main Street",
        address2="Suite 2",
        postal_code="33182",
        dial_code="1",
        phone="3055550199",
        email="jane@example.com",
    )


@pytest.mark.parametrize(
    ("edit_label", "dialog_label", "country_delay_ms"),
    [
        ("编辑", "修改地址", 0),
        ("긍서", "가공주소", 300),
    ],
)
def test_receiver_country_is_read_after_dialog_hydration_without_cancelling(
    edit_label: str,
    dialog_label: str,
    country_delay_ms: int,
) -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    _receiver_address_dialog_html(
                        "United States",
                        edit_label=edit_label,
                        dialog_label=dialog_label,
                        country_delay_ms=country_delay_ms,
                    )
                )
                await AlibabaOrderBrowser(page.context)._fill_receiver_address(
                    page,
                    _receiver_address(),
                )
                return {
                    "company": await page.locator("#companyNameEn").input_value(),
                    "province": await page.locator(
                        "#address_province_name"
                    ).input_value(),
                    "city": await page.locator("#address_city_name").input_value(),
                    "address1": await page.locator(
                        "#address_address"
                    ).input_value(),
                    "address_suggestion_clicked": await page.locator(
                        '#address_address_list ~ .ant-select-item-option'
                    ).first.get_attribute("data-clicked"),
                    "postal": await page.locator("#address_zip").input_value(),
                    "confirm_clicks": await page.locator("#confirm").get_attribute(
                        "data-clicks"
                    ),
                    "cancel_clicks": await page.locator("#cancel").get_attribute(
                        "data-clicks"
                    ),
                    "dialog_visible": await page.locator(
                        ".ant-modal.custom-address-dialog"
                    ).is_visible(),
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result == {
        "company": "Example Cooperative",
        "province": "Florida",
        "city": "Miami",
        "address1": "123 Main Street",
        "address_suggestion_clicked": None,
        "postal": "33182",
        "confirm_clicks": "1",
        "cancel_clicks": None,
        "dialog_visible": False,
    }


def test_receiver_city_verification_accepts_alibaba_canonical_case() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    _receiver_address_dialog_html(
                        "United States",
                        select_mirror_delay_ms=350,
                    )
                )
                await AlibabaOrderBrowser(page.context)._fill_receiver_address(
                    page,
                    replace(_receiver_address(), city="MIAMI"),
                )
                city_selection = page.locator("#address_city").locator(
                    "xpath=ancestor::*[contains(concat(' ', "
                    "normalize-space(@class), ' '), ' ant-select ')][1]"
                ).locator(".ant-select-selection-item")
                return {
                    "city_title": await city_selection.get_attribute("title"),
                    "confirm_clicks": await page.locator("#confirm").get_attribute(
                        "data-clicks"
                    ),
                    "cancel_clicks": await page.locator("#cancel").get_attribute(
                        "data-clicks"
                    ),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "city_title": "Miami",
        "confirm_clicks": "1",
        "cancel_clicks": None,
    }


def test_receiver_city_verification_uses_visible_selection_not_hidden_mirror() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    _receiver_address_dialog_html(
                        "United States",
                        select_mirror_delay_ms=60_000,
                    )
                )
                await page.locator("#address_city_name").evaluate(
                    "element => { element.value = 'Los Angeles'; }"
                )
                await AlibabaOrderBrowser(page.context)._fill_receiver_address(
                    page,
                    _receiver_address(),
                )
                city_control = page.locator("#address_city")
                city_selection = city_control.locator(
                    "xpath=ancestor::*[contains(concat(' ', "
                    "normalize-space(@class), ' '), ' ant-select ')][1]"
                ).locator(".ant-select-selection-item")
                return {
                    "city_title": await city_selection.get_attribute("title"),
                    "stale_hidden_city": await page.locator(
                        "#address_city_name"
                    ).input_value(),
                    "confirm_clicks": await page.locator("#confirm").get_attribute(
                        "data-clicks"
                    ),
                    "cancel_clicks": await page.locator("#cancel").get_attribute(
                        "data-clicks"
                    ),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "city_title": "Miami",
        "stale_hidden_city": "Los Angeles",
        "confirm_clicks": "1",
        "cancel_clicks": None,
    }


def test_address_and_product_inputs_fill_concurrently_without_reopening() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    _receiver_address_dialog_html("United States")
                    + """
                    <input id="formData_product_0_nameCn">
                    <input id="formData_product_0_nameEn">
                    <input id="formData_product_0_material">
                    <input id="formData_product_0_purpose">
                    <input id="formData_product_0_quantity" role="spinbutton">
                    <input id="formData_product_0_declarationValue"
                           role="spinbutton">
                    """
                )
                adapter = AlibabaOrderBrowser(page.context)
                declaration = TentDeclaration(
                    declared_unit_price_usd=Decimal("8.00")
                )
                started = time.perf_counter()
                await asyncio.gather(
                    adapter._fill_receiver_address(page, _receiver_address()),
                    adapter._fill_product_inputs(page, declaration),
                )
                elapsed = time.perf_counter() - started
                return {
                    "elapsed": elapsed,
                    "dialog_visible": await page.locator(
                        ".ant-modal.custom-address-dialog"
                    ).is_visible(),
                    "confirm_clicks": await page.locator(
                        "#confirm"
                    ).get_attribute("data-clicks"),
                    "cancel_clicks": await page.locator(
                        "#cancel"
                    ).get_attribute("data-clicks"),
                    "product": await adapter._read_input_values(
                        page,
                        (
                            "#formData_product_0_nameCn",
                            "#formData_product_0_nameEn",
                            "#formData_product_0_material",
                            "#formData_product_0_purpose",
                            "#formData_product_0_quantity",
                            "#formData_product_0_declarationValue",
                        ),
                        field_group="商品",
                    ),
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result["elapsed"] < 5
    assert result["dialog_visible"] is False
    assert result["confirm_clicks"] == "1"
    assert result["cancel_clicks"] is None
    assert result["product"] == {
        "#formData_product_0_nameCn": "帐篷布顶",
        "#formData_product_0_nameEn": "Canopy Tent",
        "#formData_product_0_material": "Polyester Fabri",
        "#formData_product_0_purpose": "display",
        "#formData_product_0_quantity": "1",
        "#formData_product_0_declarationValue": "8.00",
    }


def test_receiver_country_mismatch_keeps_dialog_open_for_review() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_receiver_address_dialog_html("Germany"))
                with pytest.raises(AlibabaOrderRuleError, match="弹窗已保留"):
                    await AlibabaOrderBrowser(page.context)._fill_receiver_address(
                        page,
                        _receiver_address(),
                    )
                return {
                    "company": await page.locator("#companyNameEn").input_value(),
                    "cancel_clicks": await page.locator("#cancel").get_attribute(
                        "data-clicks"
                    ),
                    "dialog_visible": await page.get_by_role(
                        "dialog",
                        name="修改地址",
                    ).is_visible(),
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result == {
        "company": "",
        "cancel_clicks": None,
        "dialog_visible": True,
    }


def test_country_wait_does_not_treat_ca_as_a_substring_match() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div class="ant-select">
                      <input id="country" value="" readonly>
                      <span class="ant-select-selection-item"
                            title="United States of America">
                        United States of America
                      </span>
                    </div>
                    <script>
                      setTimeout(() => {
                        const item = document.querySelector(
                          ".ant-select-selection-item"
                        );
                        item.title = "Canada";
                        item.textContent = "Canada";
                      }, 120);
                    </script>
                    """
                )
                return await AlibabaOrderBrowser._wait_for_ant_values(
                    page.locator("#country"),
                    ("ca",),
                    contains=("canada",),
                    timeout_ms=600,
                )
            finally:
                await browser.close()

    values = asyncio.run(run())

    assert "canada" in values
    assert "united states of america" not in values


def test_independent_inputs_fill_and_read_in_two_browser_round_trips() -> None:
    async def run():
        from playwright.async_api import async_playwright

        class RecordingPage:
            def __init__(self, page):
                self.page = page
                self.evaluate_calls = 0

            async def evaluate(self, *args, **kwargs):
                self.evaluate_calls += 1
                return await self.page.evaluate(*args, **kwargs)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="one"><input id="two"><input id="three">
                    <script>
                      window.fieldEvents = {};
                      document.querySelectorAll('input').forEach(input => {
                        window.fieldEvents[input.id] = [];
                        input.addEventListener('input', () => {
                          window.fieldEvents[input.id].push('input');
                        });
                        input.addEventListener('change', () => {
                          window.fieldEvents[input.id].push('change');
                        });
                      });
                    </script>
                    """
                )
                recording = RecordingPage(page)
                adapter = AlibabaOrderBrowser(page.context)
                expected = {"#one": "A", "#two": "B", "#three": "C"}
                await adapter._fill_input_values(
                    recording,
                    expected,
                    field_group="性能测试",
                )
                actual = await adapter._read_input_values(
                    recording,
                    tuple(expected),
                    field_group="性能测试",
                )
                return {
                    "evaluate_calls": recording.evaluate_calls,
                    "actual": actual,
                    "events": await page.evaluate("window.fieldEvents"),
                    "active": await page.evaluate("document.activeElement.id"),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "evaluate_calls": 2,
        "actual": {"#one": "A", "#two": "B", "#three": "C"},
        "events": {
            "one": ["input", "change"],
            "two": ["input", "change"],
            "three": ["input", "change"],
        },
        "active": "",
    }


def test_batch_input_fill_waits_for_controlled_state_between_fields() -> None:
    """A later controlled-field render must not erase earlier batch values."""

    async def run():
        from playwright.async_api import async_playwright

        class RecordingPage:
            def __init__(self, page):
                self.page = page
                self.evaluate_calls = 0

            async def evaluate(self, *args, **kwargs):
                self.evaluate_calls += 1
                return await self.page.evaluate(*args, **kwargs)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="one"><input id="two"><input id="three">
                    <script>
                      window.controlledState = {one: '', two: '', three: ''};
                      const render = () => {
                        Object.entries(window.controlledState)
                          .forEach(([id, value]) => {
                            document.getElementById(id).value = value;
                          });
                      };
                      document.querySelectorAll('input').forEach(input => {
                        input.addEventListener('input', event => {
                          // Model the stale-snapshot failure of a controlled
                          // form when several updates share one JS task.
                          const next = {
                            ...window.controlledState,
                            [event.currentTarget.id]: event.currentTarget.value,
                          };
                          queueMicrotask(() => {
                            window.controlledState = next;
                            render();
                          });
                        });
                      });
                    </script>
                    """
                )
                recording = RecordingPage(page)
                expected = {"#one": "A", "#two": "B", "#three": "C"}
                await AlibabaOrderBrowser._fill_input_values(
                    recording,
                    expected,
                    field_group="受控表单",
                )
                return {
                    "evaluate_calls": recording.evaluate_calls,
                    "values": await AlibabaOrderBrowser._read_input_values(
                        page,
                        tuple(expected),
                        field_group="受控表单",
                    ),
                    "state": await page.evaluate("window.controlledState"),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "evaluate_calls": 1,
        "values": {"#one": "A", "#two": "B", "#three": "C"},
        "state": {"one": "A", "two": "B", "three": "C"},
    }


def test_batch_input_fill_commits_ant_number_fields_and_accepts_formatting() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="quantity" role="spinbutton">
                    <input id="price" role="spinbutton">
                    <output id="total">0.00</output>
                    <script>
                      const state = {quantity: 0, price: 0};
                      const update = () => {
                        document.getElementById('total').textContent = (
                          state.quantity * state.price
                        ).toFixed(2);
                      };
                      document.getElementById('quantity')
                        .addEventListener('input', event => {
                          state.quantity = Number(event.currentTarget.value);
                          update();
                        });
                      document.getElementById('price')
                        .addEventListener('input', event => {
                          state.price = Number(event.currentTarget.value);
                          update();
                        });
                      document.getElementById('price')
                        .addEventListener('change', event => {
                          event.currentTarget.value = String(state.price);
                        });
                    </script>
                    """
                )
                await AlibabaOrderBrowser._fill_input_values(
                    page,
                    {"#quantity": "1", "#price": "800.00"},
                    field_group="商品",
                )
                return {
                    "quantity": await page.locator("#quantity").input_value(),
                    "price": await page.locator("#price").input_value(),
                    "total": await page.locator("#total").inner_text(),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "quantity": "1",
        "price": "800",
        "total": "800.00",
    }


def test_batch_input_fill_rejects_disabled_fields() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<input id="disabled" disabled>')
                with pytest.raises(
                    AlibabaOrderRuleError,
                    match="不可见或不可编辑",
                ):
                    await AlibabaOrderBrowser._fill_input_values(
                        page,
                        {"#disabled": "must-not-write"},
                        field_group="性能测试",
                    )
                return await page.locator("#disabled").input_value()
            finally:
                await browser.close()

    assert asyncio.run(run()) == ""


def test_product_search_selects_exact_option_without_fixed_sleep() -> None:
    async def run():
        from playwright.async_api import async_playwright

        class RecordingPage:
            def __init__(self, page):
                self.page = page
                self.waits: list[int] = []

            def __getattr__(self, name):
                return getattr(self.page, name)

            async def wait_for_timeout(self, milliseconds: int):
                self.waits.append(milliseconds)
                await self.page.wait_for_timeout(milliseconds)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input
                      id="formData_product_0_hscode"
                      role="combobox"
                      aria-controls="hscode-list"
                    >
                    <div id="hscode-dropdown" class="ant-select-dropdown">
                      <div id="hscode-list"></div>
                      <div id="exact-hscode" class="ant-select-item-option"
                           title="6306220010">
                        6306220010 - Tents
                      </div>
                      <div class="ant-select-item-option" title="6306299000">
                        6306299000 - Other tents
                      </div>
                    </div>
                    <script>
                      window.clickedHs = "";
                      document.getElementById("exact-hscode")
                        .addEventListener("click", event => {
                          window.clickedHs = event.currentTarget.title;
                          document.getElementById("hscode-dropdown")
                            .remove();
                        });
                    </script>
                    """
                )
                recording = RecordingPage(page)
                adapter = AlibabaOrderBrowser(page.context)
                await adapter._fill_product_search_value(
                    recording,
                    "#formData_product_0_hscode",
                    "6306220010",
                    "中国 HS 编码",
                )
                return {
                    "value": await page.locator(
                        "#formData_product_0_hscode"
                    ).input_value(),
                    "clicked": await page.evaluate("window.clickedHs"),
                    "waits": recording.waits,
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "value": "6306220010",
        "clicked": "6306220010",
        "waits": [],
    }


def test_product_search_accepts_spaced_hs_option_and_country_prefix() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="formData_product_0_hscode"
                           role="combobox"
                           aria-controls="hscode-list">
                    <div id="hscode-dropdown" class="ant-select-dropdown">
                      <div id="hscode-list"></div>
                      <div id="spaced-hscode" class="ant-select-item-option"
                           title="3926 9090 90">
                        3926 9090 90 其他塑料制品
                      </div>
                    </div>
                    <script>
                      document.getElementById('spaced-hscode')
                        .addEventListener('click', event => {
                          document.getElementById(
                            'formData_product_0_hscode'
                          ).value = '中国 3926909090';
                          event.currentTarget.parentElement.remove();
                        });
                    </script>
                    """
                )
                adapter = AlibabaOrderBrowser(page.context)
                await adapter._fill_product_search_value(
                    page,
                    "#formData_product_0_hscode",
                    "3926909090",
                    "中国 HS 编码",
                )
                return await page.locator(
                    "#formData_product_0_hscode"
                ).input_value()
            finally:
                await browser.close()

    assert asyncio.run(run()) == "中国 3926909090"
    assert AlibabaOrderBrowser._search_value_matches(
        "3926909090",
        "3926 9090 90 其他塑料制品",
    )
    assert AlibabaOrderBrowser._search_value_matches(
        "3926909989",
        (
            "3926 9099 89 Other articles of plastics and articles of other "
            "materials of headings 3901 to 3914"
        ),
    )
    assert not AlibabaOrderBrowser._search_value_matches(
        "3926909090",
        (
            "3926 9099 89 Other articles of plastics and articles of other "
            "materials of headings 3901 to 3914"
        ),
    )


def test_product_search_commits_prefilled_expanded_destination_candidate() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="formData_product_0_destinationHscode"
                           role="combobox"
                           aria-expanded="true"
                           aria-controls="destination-hscode-list"
                           value="3926909989">
                    <div id="destination-hscode-dropdown"
                         class="ant-select-dropdown">
                      <div id="destination-hscode-list"></div>
                      <div id="destination-hscode-option"
                           class="ant-select-item-option">
                        3926 9099 89 Other articles of plastics and articles of
                        other materials of headings 3901 to 3914
                      </div>
                    </div>
                    <script>
                      window.destinationCandidateClicked = false;
                      document.getElementById('destination-hscode-option')
                        .addEventListener('click', event => {
                          window.destinationCandidateClicked = true;
                          event.currentTarget.dataset.clicked = 'true';
                          const input = document.getElementById(
                            'formData_product_0_destinationHscode'
                          );
                          input.value = '3926909989';
                          input.setAttribute('aria-expanded', 'false');
                          event.currentTarget.parentElement.remove();
                        });
                    </script>
                    """
                )
                adapter = AlibabaOrderBrowser(page.context)
                await adapter._fill_product_search_value(
                    page,
                    "#formData_product_0_destinationHscode",
                    "3926909989",
                    "目的国 HS 编码",
                )
                return {
                    "value": await page.locator(
                        "#formData_product_0_destinationHscode"
                    ).input_value(),
                    "clicked": await page.locator(
                        "body"
                    ).evaluate("() => window.destinationCandidateClicked"),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "value": "3926909989",
        "clicked": True,
    }


def test_product_fields_select_exact_readonly_logistics_attribute() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <input id="formData_product_0_nameCn">
                    <input id="formData_product_0_nameEn">
                    <input id="formData_product_0_material">
                    <input id="formData_product_0_purpose">
                    <input id="formData_product_0_hscode" role="combobox">
                    <input id="formData_product_0_destinationHscode"
                           role="combobox">
                    <input id="formData_product_0_quantity">
                    <input id="formData_product_0_declarationValue">
                    <div class="ant-select ant-cascader">
                      <div class="ant-select-selector">
                        <span class="ant-select-selection-wrap">
                          <div class="ant-select-selection-overflow">
                            <div class="ant-select-selection-search">
                              <input id="formData_product_0_productType"
                                     role="combobox" readonly>
                            </div>
                          </div>
                        </span>
                      </div>
                    </div>
                    <div class="product-type-dropdown">
                      <div class="ant-cascader-menu-item"
                           role="menuitemcheckbox" title="带磁">带磁</div>
                      <div class="ant-cascader-menu-item"
                           role="menuitemcheckbox" title="普货">普货</div>
                    </div>
                    <script>
                      document.querySelectorAll('.ant-cascader-menu-item')
                          .forEach(option => option.addEventListener('click', event => {
                            event.currentTarget.dataset.clicked = 'true';
                            const item = document.createElement('span');
                            item.className = 'ant-select-selection-item';
                            item.title = event.currentTarget.title;
                            item.textContent = event.currentTarget.title;
                            document.querySelector('.ant-select-selection-overflow')
                                .appendChild(item);
                          }));
                    </script>
                    """
                )
                declaration = TentDeclaration(
                    declared_unit_price_usd=Decimal("2.50")
                )
                adapter = AlibabaOrderBrowser(page.context)
                await adapter._fill_product(page, declaration)
                await adapter._verify_product(page, declaration)
                await adapter._fill_product(page, declaration)
                await adapter._verify_product(page, declaration)
                return {
                    "selected": await page.locator(
                        ".ant-select-selection-item"
                    ).get_attribute("title"),
                    "general_clicked": await page.get_by_role(
                        "menuitemcheckbox",
                        name="普货",
                        exact=True,
                    ).get_attribute("data-clicked"),
                    "magnetic_clicked": await page.get_by_role(
                        "menuitemcheckbox",
                        name="带磁",
                        exact=True,
                    ).get_attribute("data-clicked"),
                    "selected_count": await page.locator(
                        ".ant-select-selection-item"
                    ).count(),
                }
            finally:
                await browser.close()

    assert asyncio.run(run()) == {
        "selected": "普货",
        "general_clicked": "true",
        "magnetic_clicked": None,
        "selected_count": 1,
    }


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
                observed: dict[str, int | bool] = {}

                async def no_op(*_args, **_kwargs):
                    return None

                async def fill_address(*_args, **_kwargs):
                    observed["address_started"] = True
                    await asyncio.sleep(0.05)
                    assert observed.get("product_inputs_started") is True
                    observed["address_finished"] = True

                async def fill_product_inputs(*_args, **_kwargs):
                    call_count = int(observed.get("product_input_calls") or 0) + 1
                    observed["product_input_calls"] = call_count
                    if call_count == 1:
                        observed["product_inputs_started"] = True
                        await asyncio.sleep(0)
                        assert observed.get("address_started") is True
                    elif call_count == 2:
                        assert observed.get("address_finished") is True
                    elif call_count == 3:
                        assert observed.get("selectors_called") is True

                async def fill_product_selectors(*_args, **_kwargs):
                    assert observed.get("address_finished") is True
                    assert observed.get("product_input_calls") == 2
                    observed["selectors_called"] = True

                async def unexpected_inspection(*_args, **_kwargs):
                    pytest.fail("prefetched draft facts must not be read twice")

                monkeypatch.setattr(adapter, "_fill_receiver_address", fill_address)
                monkeypatch.setattr(
                    adapter,
                    "_fill_product_inputs",
                    fill_product_inputs,
                )
                monkeypatch.setattr(
                    adapter,
                    "_fill_product_selectors",
                    fill_product_selectors,
                )
                monkeypatch.setattr(adapter, "_verify_product", no_op)
                monkeypatch.setattr(adapter, "inspect_draft", unexpected_inspection)
                result = await adapter.fill_draft(
                    page,
                    customer_order_no="112-0000000-0000001",
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
                    facts=AlibabaDraftFacts(
                        url=DRAFT_A,
                        route=AlibabaRoute("Express Expedited"),
                        total_weight_kg=Decimal("6"),
                        route_is_expedited=True,
                        signature_available=True,
                    ),
                )
                clicked = await page.locator("#final-submit").get_attribute(
                    "data-clicked"
                )
                customer_order = await page.get_by_role(
                    "textbox",
                    name="客户订单号",
                ).input_value()
                return result, clicked, customer_order, observed
            finally:
                await browser_process.close()

    result, clicked, customer_order, observed = asyncio.run(run())

    assert result.signature_selected is False
    assert clicked is None
    assert customer_order == "112-0000000-0000001"
    assert observed == {
        "address_started": True,
        "address_finished": True,
        "product_inputs_started": True,
        "product_input_calls": 3,
        "selectors_called": True,
    }


def test_fill_draft_requires_one_customer_order_field(monkeypatch) -> None:
    browser = AlibabaOrderBrowser(None)

    async def inspect_draft(_page):
        return AlibabaDraftFacts(
            url=DRAFT_A,
            route=AlibabaRoute("标准线路"),
            total_weight_kg=Decimal("6"),
            route_is_expedited=False,
            signature_available=False,
        )

    class CountLocator:
        def __init__(self, count):
            self._count = count

        async def count(self):
            return self._count

    class MissingCustomerOrderPage:
        def locator(self, _selector):
            return CountLocator(1)

        def get_by_role(self, role, **_kwargs):
            return CountLocator(0 if role == "textbox" else 0)

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(browser, "inspect_draft", inspect_draft)
    monkeypatch.setattr(browser, "_fill_receiver_address", no_op)
    monkeypatch.setattr(browser, "_fill_product_inputs", no_op)
    monkeypatch.setattr(browser, "_fill_product_selectors", no_op)

    async def run():
        with pytest.raises(AlibabaOrderRuleError, match="客户订单号字段"):
            await browser.fill_draft(
                MissingCustomerOrderPage(),
                customer_order_no="112-0000000-0000001",
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
                expedited=False,
                signature_requested=False,
            )

    asyncio.run(run())
