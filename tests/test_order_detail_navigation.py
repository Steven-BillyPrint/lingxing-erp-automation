from __future__ import annotations

import asyncio
import inspect

import pytest
from playwright.async_api import async_playwright

from lingxing_automation.pages import order_detail_navigation


SYSTEM_ORDER_NO = "103727324802185912"
OTHER_SYSTEM_ORDER_NO = "103737374585189453"


def test_active_order_navigation_does_not_use_screen_coordinates() -> None:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            order_detail_navigation.dismiss_known_blocking_dialogs,
            order_detail_navigation.close_order_detail_dialog,
            order_detail_navigation.click_system_order,
            order_detail_navigation.wait_for_detail,
        )
    )

    for forbidden in (
        "page.mouse",
        "getBoundingClientRect",
        "elementFromPoint",
        "rect.top",
        "rect.left",
    ):
        assert forbidden not in source


@pytest.mark.parametrize(
    ("viewport_width", "link_margin"),
    [(760, 8), (1200, 240), (2133, 900)],
)
def test_click_system_order_dismisses_notice_and_ignores_layout(
    viewport_width: int,
    link_margin: int,
) -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": 720}
                )
                await page.set_content(
                    f"""
                    <style>
                      .init-dialog {{
                        position: fixed; inset: 0; z-index: 2000;
                        display: grid; place-items: center; background: rgba(0,0,0,.2);
                      }}
                      .order-row {{ margin-left: {link_margin}px; margin-top: 540px; }}
                      .ak-pointer {{ cursor: pointer; color: blue; }}
                    </style>
                    <div class="el-dialog__wrapper init-dialog">
                      <div class="el-dialog">
                        <p>【自发货管理】更新公告</p>
                        <button id="notice-close"><span> 知道了 </span></button>
                      </div>
                    </div>
                    <div class="order-row">
                      <span class="ak-blue ak-pointer"> {SYSTEM_ORDER_NO} </span>
                    </div>
                    <script>
                      window.orderClicks = 0;
                      document.querySelector('#notice-close').addEventListener('click', () => {{
                        document.querySelector('.init-dialog').style.display = 'none';
                      }});
                      document.querySelector('.ak-pointer').addEventListener('click', () => {{
                        window.orderClicks += 1;
                      }});
                    </script>
                    """
                )

                await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)

                assert await page.evaluate("window.orderClicks") == 1
                assert not await page.locator(".init-dialog").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_uses_plain_cell_text_and_parent_click_handler() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 680, "height": 420})
                await page.set_content(
                    f"""
                    <div class="vxe-table--body-wrapper">
                      <table>
                        <tbody>
                          <tr class="vxe-body--row" rowid="{SYSTEM_ORDER_NO}">
                            <td class="vxe-body--column">
                              <div class="vxe-cell">
                                <span class="ak-blue">{SYSTEM_ORDER_NO}</span>
                              </div>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <script>
                      window.openedOrder = '';
                      document.querySelector('td').addEventListener('click', () => {{
                        window.openedOrder = '{SYSTEM_ORDER_NO}';
                      }});
                    </script>
                    """
                )

                await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)

                assert await page.evaluate("window.openedOrder") == SYSTEM_ORDER_NO
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_requeries_delayed_virtual_table_row() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <div class="vxe-table--body-wrapper">
                      <table><tbody id="order-body"></tbody></table>
                    </div>
                    <script>
                      window.orderClicks = 0;
                      const renderRow = () => {{
                        document.querySelector('#order-body').innerHTML = `
                          <tr class="vxe-body--row" rowid="{SYSTEM_ORDER_NO}">
                            <td><span><span>{SYSTEM_ORDER_NO[:9]}</span><span>{SYSTEM_ORDER_NO[9:]}</span></span></td>
                          </tr>`;
                        document.querySelector('td').addEventListener(
                          'click', () => window.orderClicks += 1
                        );
                      }};
                      setTimeout(renderRow, 120);
                      setTimeout(renderRow, 180);
                    </script>
                    """
                )

                await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)

                assert await page.evaluate("window.orderClicks") == 1
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_reuses_matching_detail_that_already_covers_list() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <style>
                      .order-detail-dialog {{ position: fixed; inset: 0; z-index: 2029; background: white; }}
                    </style>
                    <table><tr class="vxe-body--row" rowid="{SYSTEM_ORDER_NO}">
                      <td><span class="ak-pointer">{SYSTEM_ORDER_NO}</span></td>
                    </tr></table>
                    <div class="el-dialog__wrapper order-detail-dialog">
                      <div class="el-dialog">
                        <header class="el-dialog__header">系统单号 {SYSTEM_ORDER_NO}</header>
                        <section class="receive-info">收货信息 电话 买家邮箱</section>
                        <section>商品信息</section>
                      </div>
                    </div>
                    <script>
                      window.orderClicks = 0;
                      document.querySelector('.ak-pointer').onclick = () => window.orderClicks += 1;
                    </script>
                    """
                )

                await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)

                assert await page.evaluate("window.orderClicks") == 0
                assert await page.locator(".order-detail-dialog").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_closes_different_detail_before_clicking_list() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <style>
                      .order-detail-dialog {{ position: fixed; inset: 0; z-index: 2029; background: white; }}
                    </style>
                    <table><tr class="vxe-body--row" rowid="{SYSTEM_ORDER_NO}">
                      <td><span class="ak-pointer">{SYSTEM_ORDER_NO}</span></td>
                    </tr></table>
                    <div class="el-dialog__wrapper order-detail-dialog">
                      <div class="el-dialog">
                        <header class="el-dialog__header">
                          系统单号 {OTHER_SYSTEM_ORDER_NO}
                          <button> 关闭 </button>
                        </header>
                        <section class="receive-info">收货信息 电话 买家邮箱</section>
                        <section>商品信息</section>
                      </div>
                    </div>
                    <script>
                      window.orderClicks = 0;
                      document.querySelector('.el-dialog__header button').onclick = () => {{
                        document.querySelector('.order-detail-dialog').style.display = 'none';
                      }};
                      document.querySelector('.ak-pointer').onclick = () => window.orderClicks += 1;
                    </script>
                    """
                )

                await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)

                assert await page.evaluate("window.orderClicks") == 1
                assert not await page.locator(".order-detail-dialog").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_reports_unclosable_detail_instead_of_waiting_on_list() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <style>
                      .order-detail-dialog {{ position: fixed; inset: 0; z-index: 2029; background: white; }}
                    </style>
                    <span class="ak-pointer">{SYSTEM_ORDER_NO}</span>
                    <div class="el-dialog__wrapper order-detail-dialog">
                      <div class="el-dialog">
                        <header class="el-dialog__header">系统单号 {OTHER_SYSTEM_ORDER_NO}</header>
                        <section class="receive-info">收货信息 电话 买家邮箱</section>
                        <section>商品信息</section>
                      </div>
                    </div>
                    """
                )

                with pytest.raises(RuntimeError, match="另一个订单详情正在遮挡"):
                    await order_detail_navigation.click_system_order(page, SYSTEM_ORDER_NO)
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_stops_for_unknown_notice_dialog() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <div class="el-dialog__wrapper init-dialog">
                      <div class="el-dialog">
                        <p>未知业务公告</p>
                        <button>去设置</button>
                      </div>
                    </div>
                    <span class="ak-pointer">{SYSTEM_ORDER_NO}</span>
                    <script>
                      window.orderClicks = 0;
                      document.querySelector('.ak-pointer').addEventListener(
                        'click', () => window.orderClicks += 1
                      );
                    </script>
                    """
                )
                with pytest.raises(RuntimeError, match="没有找到安全的关闭按钮"):
                    await order_detail_navigation.click_system_order(
                        page, SYSTEM_ORDER_NO
                    )
                assert await page.evaluate("window.orderClicks") == 0
            finally:
                await browser.close()

    asyncio.run(run())


def test_click_system_order_reports_non_notice_blocking_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    f"""
                    <style>
                      .el-loading-mask {{ position: fixed; inset: 0; z-index: 3000; }}
                    </style>
                    <span class="ak-pointer">{SYSTEM_ORDER_NO}</span>
                    <div class="el-loading-mask"></div>
                    """
                )
                with pytest.raises(RuntimeError, match="页面遮挡"):
                    await order_detail_navigation.click_system_order(
                        page, SYSTEM_ORDER_NO
                    )
            finally:
                await browser.close()

    monkeypatch.setattr(order_detail_navigation, "_ORDER_CLICK_READY_TIMEOUT_MS", 400)
    asyncio.run(run())


def test_wait_for_detail_uses_semantic_root_and_replaces_raw_timeout() -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 760, "height": 480})
                await page.set_content(
                    f"""
                    <div class="el-dialog__wrapper order-detail-dialog">
                      <div class="el-dialog">
                        <header>系统单号 {SYSTEM_ORDER_NO}</header>
                        <section class="receive-info">收货信息 电话 买家邮箱</section>
                        <section>商品信息</section>
                      </div>
                    </div>
                    """
                )
                await order_detail_navigation.wait_for_detail(
                    page, SYSTEM_ORDER_NO, timeout_ms=1500
                )

                await page.set_content("<main>订单列表</main>")
                with pytest.raises(
                    RuntimeError, match="领星订单详情.*没有完成加载"
                ) as exc_info:
                    await order_detail_navigation.wait_for_detail(
                        page, SYSTEM_ORDER_NO, timeout_ms=300
                    )
                assert "Page.wait_for_function" not in str(exc_info.value)
            finally:
                await browser.close()

    asyncio.run(run())
