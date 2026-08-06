from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from shipment_automation.alibaba_order_browser import (
    ALIBABA_QUOTE_URL,
    ALIBABA_QUOTE_ORIGIN_CITY,
    ALIBABA_QUOTE_ORIGIN_CITY_OPTION,
    ALIBABA_QUOTE_ORIGIN_COUNTRY,
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


def test_quote_route_prefills_visible_address_fields_without_querying() -> None:
    async def run():
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    """
                    <div class="ant-select"><span class="ant-select-selection-item">中国大陆</span>
                      <input role="combobox" readonly></div>
                    <div class="ant-select ant-cascader"><span class="ant-select-selection-item"></span>
                      <input role="combobox"></div>
                    <div class="ant-select"><span class="ant-select-selection-item">其他目的国</span>
                      <input role="combobox"></div>
                    <input id="destination_zipCode">
                    <div class="ant-select"><span class="ant-select-selection-item">普货</span>
                      <input role="combobox" readonly></div>
                    <div class="ant-select-dropdown origin-city-dropdown">
                      <ul>
                        <li class="ant-cascader-menu-item"
                            role="menuitemcheckbox">广东省 / 佛山市</li>
                        <li class="ant-cascader-menu-item"
                            role="menuitemcheckbox">广东省 / 佛山市 / 禅城区</li>
                        <li class="ant-cascader-menu-item"
                            role="menuitemcheckbox">广东省 / 佛山市 / 南海区</li>
                      </ul>
                    </div>
                    <div class="ant-select-dropdown">
                      <div class="ant-select-item-option">美国(US)</div>
                      <div class="ant-select-item-option">美国本土外小岛屿(UM)</div>
                    </div>
                    <button id="query" onclick="this.dataset.clicked='true'">
                      查询
                    </button>
                    <script>
                      document.querySelector('.ant-select-item-option')
                          .addEventListener('click', event => {
                            document.querySelectorAll('.ant-select span')[2]
                                .textContent = event.target.textContent;
                          });
                      document.querySelectorAll('.ant-cascader-menu-item')
                          .forEach(item => item.addEventListener('click', event => {
                            document.querySelectorAll('.ant-select span')[1]
                                .textContent = event.target.textContent ===
                                    '广东省 / 佛山市'
                                      ? '佛山市'
                                      : event.target.textContent;
                            event.target.dataset.clicked = 'true';
                          }));
                    </script>
                    """
                )
                await AlibabaOrderBrowser(page.context)._fill_quote_route(
                    page,
                    ShippingAddress(
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
                )
                selected = await page.locator(".ant-select span").all_inner_texts()
                return {
                    "selected": selected,
                    "postal": await page.locator(
                        "#destination_zipCode"
                    ).input_value(),
                    "query_clicked": await page.locator("#query").get_attribute(
                        "data-clicked"
                    ),
                    "city_clicked": await page.get_by_role(
                        "menuitemcheckbox",
                        name=ALIBABA_QUOTE_ORIGIN_CITY_OPTION,
                        exact=True,
                    ).get_attribute("data-clicked"),
                    "district_clicked": await page.get_by_role(
                        "menuitemcheckbox",
                        name="广东省 / 佛山市 / 禅城区",
                        exact=True,
                    ).get_attribute("data-clicked"),
                }
            finally:
                await browser.close()

    result = asyncio.run(run())

    assert result["selected"] == [
        ALIBABA_QUOTE_ORIGIN_COUNTRY,
        ALIBABA_QUOTE_ORIGIN_CITY,
        "美国(US)",
        "普货",
    ]
    assert result["postal"] == "90012"
    assert result["query_clicked"] is None
    assert result["city_clicked"] == "true"
    assert result["district_clicked"] is None


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
    <button class="edit icon-margin-right">{edit_label}</button>
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
        "postal": "33182",
        "confirm_clicks": "1",
        "cancel_clicks": "1",
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
                return {
                    "city": await page.locator("#address_city_name").input_value(),
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
        "city": "Miami",
        "confirm_clicks": "1",
        "cancel_clicks": "1",
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
        "cancel_clicks": "1",
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

                async def no_op(*_args, **_kwargs):
                    return None

                monkeypatch.setattr(adapter, "_fill_receiver_address", no_op)
                monkeypatch.setattr(adapter, "_fill_product", no_op)
                monkeypatch.setattr(adapter, "_verify_product", no_op)
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
    assert customer_order == "112-0000000-0000001"


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
    monkeypatch.setattr(browser, "_fill_product", no_op)

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
