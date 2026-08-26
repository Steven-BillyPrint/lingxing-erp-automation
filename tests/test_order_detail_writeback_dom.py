from __future__ import annotations

import asyncio
import inspect

import pytest
from playwright.async_api import async_playwright

from lingxing_automation.models import ContactInfo
from lingxing_automation.pages import order_detail_writeback


SYSTEM_ORDER_NO = "103737209528929820"
OLD_PHONE = "+1 210-728-4548 ext. 43781"
OLD_EMAIL = "masked@marketplace.amazon.com"
NEW_PHONE = "5514970464"
NEW_EMAIL = "buyer@example.com"


def test_active_contact_writeback_does_not_use_screen_coordinates() -> None:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            order_detail_writeback._shipping_root_locator,
            order_detail_writeback._basic_info_action_group,
            order_detail_writeback._contact_field_locator,
            order_detail_writeback.try_open_edit_mode,
            order_detail_writeback.fill_shipping_contact_field,
            order_detail_writeback.click_save_button,
            order_detail_writeback.click_cancel_edit_button,
        )
    )

    for forbidden in (
        "page.mouse",
        "getBoundingClientRect",
        "elementFromPoint",
        "window.innerWidth",
        "rect.top",
        "rect.left",
    ):
        assert forbidden not in source


def _detail_html(*, save_mode: str = "persist") -> str:
    return f"""
    <style>
      body {{ margin: 0; }}
      .order-detail-dialog {{ padding: 16px; }}
      .layout-spacer {{ height: 38vh; }}
      .receive-info {{ display: grid; gap: 8px; }}
      .info-wrapper {{ display: flex; gap: 12px; }}
    </style>
    <span class="ak-pointer list-system-order">{SYSTEM_ORDER_NO}</span>
    <div class="el-dialog__wrapper order-detail-dialog">
      <div class="el-dialog">
        <header class="el-dialog__header">
          系统单号 {SYSTEM_ORDER_NO}
          <button id="header-edit">编辑</button>
          <button id="detail-close">关闭</button>
        </header>
        <div class="base-info-tabs-contain">
          <div class="tabs-contain">
            <nav>基本信息 报关信息 操作日志</nav>
            <div class="operate-contain"></div>
          </div>
          <div class="layout-spacer"></div>
          <section class="receive-info"></section>
        </div>
        <section class="product-info">
          商品信息
          <button id="product-edit">编辑</button>
          <button id="unrelated-save">保存</button>
        </section>
      </div>
    </div>
    <script>
      window.headerEditClicks = 0;
      window.productEditClicks = 0;
      window.contactEditClicks = 0;
      window.listOrderClicks = 0;
      window.persistedPhone = {OLD_PHONE!r};
      window.persistedEmail = {OLD_EMAIL!r};
      window.saveMode = {save_mode!r};

      document.querySelector('#header-edit').onclick = () => window.headerEditClicks += 1;
      document.querySelector('#product-edit').onclick = () => window.productEditClicks += 1;
      document.querySelector('#detail-close').onclick = () => {{
        document.querySelector('.order-detail-dialog').style.display = 'none';
      }};
      document.querySelector('.list-system-order').onclick = () => {{
        window.listOrderClicks += 1;
        window.renderContact(false);
        document.querySelector('.order-detail-dialog').style.display = 'block';
      }};

      window.renderContact = (editing) => {{
        const action = document.querySelector('.operate-contain');
        const contact = document.querySelector('.receive-info');
        if (!editing) {{
          action.innerHTML =
            '<div class="info-title-edit-box"><button id="contact-edit"><span> 编辑 </span></button></div>';
          contact.innerHTML =
            '<div class="receive-info-title">收货信息</div>' +
            '<div class="info-wrapper email-row"><span class="label"> 买家邮箱 </span><div class="value oneLine">' +
              window.persistedEmail + '</div></div>' +
            '<div class="info-wrapper phone-row"><span class="label">电话</span><div class="value oneLine">' +
              window.persistedPhone + '</div></div>';
          document.querySelector('#contact-edit').onclick = () => {{
            window.contactEditClicks += 1;
            window.renderContact(true);
          }};
          return;
        }}
        action.innerHTML =
          '<div class="info-title-edit-box">' +
            '<button id="contact-cancel"><span> 取消 </span></button>' +
            '<button id="contact-save"><span> 保存 </span></button>' +
          '</div>';
        contact.innerHTML =
          '<div class="receive-info-title">收货信息</div>' +
          '<div class="info-wrapper email-row"><span class="label"> 买家邮箱 </span><input maxlength="80" value="' +
            window.persistedEmail + '"></div>' +
          '<div class="info-wrapper phone-row"><span class="label">电话</span><input maxlength="100" value="' +
            window.persistedPhone + '"></div>';
        document.querySelector('#contact-cancel').onclick = () => window.renderContact(false);
        document.querySelector('#contact-save').onclick = () => {{
          if (window.saveMode === 'noop') return;
          window.persistedPhone = document.querySelector('.phone-row input').value;
          window.persistedEmail = document.querySelector('.email-row input').value;
          window.renderContact(false);
        }};
      }};
      window.renderContact(false);
    </script>
    """


@pytest.mark.parametrize("zoom", [0.8, 0.9, 1.0, 1.25])
def test_contact_writeback_uses_scoped_dom_actions_at_any_zoom(zoom: float) -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1000, "height": 620})
                await page.set_content(_detail_html())
                await page.evaluate("(zoom) => document.body.style.zoom = String(zoom)", zoom)

                before = await order_detail_writeback.read_shipping_contact_values(page)
                assert before == {"phone": "+1210-728-4548", "email": OLD_EMAIL}

                await order_detail_writeback.try_open_edit_mode(page)
                assert await order_detail_writeback.fill_shipping_contact_field(
                    page, "phone", NEW_PHONE
                )
                assert await order_detail_writeback.fill_shipping_contact_field(
                    page, "email", NEW_EMAIL
                )
                assert await order_detail_writeback.read_shipping_contact_values(page) == {
                    "phone": NEW_PHONE,
                    "email": NEW_EMAIL,
                }

                assert await order_detail_writeback.click_save_button(page)
                assert await order_detail_writeback.read_shipping_contact_values(page) == {
                    "phone": NEW_PHONE,
                    "email": NEW_EMAIL,
                }
                assert await page.evaluate("window.contactEditClicks") == 1
                assert await page.evaluate("window.headerEditClicks") == 0
                assert await page.evaluate("window.productEditClicks") == 0
            finally:
                await browser.close()

    asyncio.run(run())


def test_contact_save_rejects_false_positive_when_form_stays_editable() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_detail_html(save_mode="noop"))
                await order_detail_writeback.try_open_edit_mode(page)
                assert await order_detail_writeback.fill_shipping_contact_field(
                    page, "phone", NEW_PHONE
                )

                with pytest.raises(RuntimeError, match="表单仍处于编辑状态"):
                    await order_detail_writeback.click_save_button(
                        page, state_timeout_ms=400
                    )

                assert await order_detail_writeback.has_editable_contact_controls(page)
                assert await page.locator("#contact-save").is_visible()
                assert await page.evaluate("window.persistedPhone") == OLD_PHONE
            finally:
                await browser.close()

    asyncio.run(run())


def test_contact_cancel_handles_real_button_whitespace_without_saving() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_detail_html())
                await order_detail_writeback.try_open_edit_mode(page)
                assert await order_detail_writeback.fill_shipping_contact_field(
                    page, "phone", NEW_PHONE
                )

                assert await order_detail_writeback.click_cancel_edit_button(page)
                assert not await order_detail_writeback.has_editable_contact_controls(page)
                assert await page.evaluate("window.persistedPhone") == OLD_PHONE
            finally:
                await browser.close()

    asyncio.run(run())


def test_contact_writeback_reopens_real_dom_and_verifies_persisted_values() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_detail_html())

                async def confirm(_context):
                    return True

                saved, message = await order_detail_writeback.update_current_detail_contact(
                    page,
                    ContactInfo(NEW_PHONE, NEW_EMAIL, 1, "both"),
                    expected_system_order_no=SYSTEM_ORDER_NO,
                    confirm_callback=confirm,
                )

                assert saved is True
                assert "重新打开后系统单号" in message
                assert await page.evaluate("window.persistedPhone") == NEW_PHONE
                assert await page.evaluate("window.persistedEmail") == NEW_EMAIL
                assert await page.evaluate("window.listOrderClicks") == 1
                assert not await page.locator(".order-detail-dialog").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_contact_save_never_uses_unrelated_global_save_button() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_detail_html())
                await order_detail_writeback.try_open_edit_mode(page)
                await page.locator("#contact-save").evaluate("(el) => el.remove()")

                assert await order_detail_writeback.click_save_button(page) is False
                assert await order_detail_writeback.has_editable_contact_controls(page)
                assert await page.locator("#unrelated-save").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_contact_save_rejects_multiple_buttons_in_its_own_action_group() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(_detail_html())
                await order_detail_writeback.try_open_edit_mode(page)
                await page.locator(".operate-contain").evaluate(
                    "(el) => el.insertAdjacentHTML('beforeend', '<button>保存</button>')"
                )

                with pytest.raises(RuntimeError, match="2 个联系方式保存按钮"):
                    await order_detail_writeback.click_save_button(page)
                assert await order_detail_writeback.has_editable_contact_controls(page)
            finally:
                await browser.close()

    asyncio.run(run())
