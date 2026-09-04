from __future__ import annotations

import asyncio

import pytest

import lingxing_automation.pages.marked_shipment_update as update_page
import lingxing_automation.pages.shipment_reversal as reversal_page
from lingxing_automation.pages.marked_shipment_update import (
    MarkedShipmentUpdateEvidence,
    system_marking_contains_waybill,
    update_marked_shipment,
)
from lingxing_automation.pages.system_order_search import (
    exact_system_order_cell_text,
    exact_system_order_text,
    select_exact_system_order,
)


SYSTEM_ORDER_NO = "103735075688785273"
NEW_WAYBILL_NO = "1LSD01R0018AGMD"


def test_system_marking_waybill_match_requires_exact_token() -> None:
    assert system_marking_contains_waybill(
        f"OnTrac ： {NEW_WAYBILL_NO} 标发中",
        NEW_WAYBILL_NO,
    )
    assert not system_marking_contains_waybill(
        f"OnTrac ： X{NEW_WAYBILL_NO}9 标发中",
        NEW_WAYBILL_NO,
    )


def test_system_marking_cell_is_read_by_header_colid_and_dynamic_rowid() -> None:
    html = f"""
    <style>.hidden {{ display: none; }}</style>
    <table>
      <thead><tr>
        <th colid="col_8">系统单号</th>
        <th colid="col_11">系统标发单号</th>
      </tr></thead>
      <tbody>
        <tr rowid="row_20">
          <td colid="col_8">{SYSTEM_ORDER_NO}<br>仓库已出库</td>
          <td colid="col_11">OnTrac ： {NEW_WAYBILL_NO}<br>标发中</td>
        </tr>
        <tr rowid="row_21">
          <td colid="col_8">103735075688785274</td>
          <td colid="col_11">OnTrac ： OTHER</td>
        </tr>
      </tbody>
    </table>
    """

    async def run() -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await exact_system_order_cell_text(
                    page,
                    SYSTEM_ORDER_NO,
                    "系统标发单号",
                )
            finally:
                await browser.close()

    assert asyncio.run(run()) == f"OnTrac ： {NEW_WAYBILL_NO} 标发中"


def test_exact_system_order_text_reads_fragments_without_async_generator() -> None:
    html = f"""
    <table>
      <thead><tr><th colid="col_8">系统单号</th></tr></thead>
      <tbody>
        <tr rowid="row_20"><td colid="col_8">{SYSTEM_ORDER_NO}</td></tr>
        <tr rowid="row_20"><td colid="col_9">手动-OnTrac {NEW_WAYBILL_NO}</td></tr>
      </tbody>
    </table>
    """

    async def run() -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                return await exact_system_order_text(page, SYSTEM_ORDER_NO)
            finally:
                await browser.close()

    assert asyncio.run(run()) == (
        f"{SYSTEM_ORDER_NO} 手动-OnTrac {NEW_WAYBILL_NO}"
    )


def test_select_exact_system_order_clicks_rendered_unchecked_vxe_checkbox_once() -> None:
    html = f"""
    <style>
      .vxe-cell--checkbox {{ display: inline-block; width: 14px; height: 14px; }}
      .vxe-checkbox--icon {{ width: 14px; height: 14px; }}
      .vxe-checkbox--checked-icon {{ display: none; }}
      .vxe-checkbox--unchecked-icon {{ display: block; }}
      tr.row--checked .vxe-checkbox--checked-icon {{ display: block; }}
      tr.row--checked .vxe-checkbox--unchecked-icon {{ display: none; }}
    </style>
    <table>
      <thead><tr><th colid="col_8">系统单号</th></tr></thead>
      <tbody>
        <tr rowid="{SYSTEM_ORDER_NO}" class="vxe-body--row">
          <td class="col--checkbox">
            <span class="vxe-cell--checkbox">
              <span class="vxe-checkbox--icon vxe-checkbox--checked-icon"></span>
              <span class="vxe-checkbox--icon vxe-checkbox--unchecked-icon"></span>
              <span class="vxe-checkbox--icon vxe-checkbox--indeterminate-icon"></span>
            </span>
          </td>
          <td colid="col_8">{SYSTEM_ORDER_NO}</td>
        </tr>
      </tbody>
    </table>
    <script>
      window.checkboxClicks = 0;
      document.querySelector('.vxe-cell--checkbox').addEventListener('click', () => {{
        window.checkboxClicks += 1;
        document.querySelector('tr[rowid]').classList.add('row--checked');
      }});
    </script>
    """

    async def run() -> tuple[str, int]:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(html)
                await select_exact_system_order(page, SYSTEM_ORDER_NO)
                # A retry must observe the rendered checked icon and remain
                # idempotent instead of toggling the row back off.
                await select_exact_system_order(page, SYSTEM_ORDER_NO)
                row_class = await page.locator(
                    f'tr[rowid="{SYSTEM_ORDER_NO}"]'
                ).get_attribute("class")
                click_count = await page.evaluate("window.checkboxClicks")
                return str(row_class or ""), int(click_count)
            finally:
                await browser.close()

    row_class, click_count = asyncio.run(run())

    assert "row--checked" in row_class
    assert click_count == 1


def test_withdraw_reuses_authenticated_order_management_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ExistingPage:
        url = reversal_page.ORDER_MANAGEMENT_URL

        async def goto(self, *_args, **_kwargs) -> None:
            raise AssertionError("already-loaded order page must not be navigated again")

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    class Dialog:
        def get_by_text(self, text: str, *, exact: bool):
            assert exact
            events.append(f"option:{text}")
            return object()

    async def record(name: str, *_args, **_kwargs):
        events.append(name)

    async def row_text(*_args, **_kwargs) -> str:
        return (
            f"{SYSTEM_ORDER_NO} 113-1341773-1145022 "
            f"手动-OnTrac {NEW_WAYBILL_NO} ALS01915029156"
        )

    async def dialog(*_args, **_kwargs):
        return Dialog()

    async def one_visible(*_args, **_kwargs):
        return _Clickable()

    async def before_confirm() -> None:
        events.append("intent")

    monkeypatch.setattr(
        reversal_page,
        "_select_order_tab",
        lambda *_args, **_kwargs: record("tab"),
    )
    monkeypatch.setattr(
        reversal_page,
        "search_exact_system_order",
        lambda *_args, **_kwargs: record("search"),
    )
    monkeypatch.setattr(reversal_page, "exact_system_order_text", row_text)
    monkeypatch.setattr(
        reversal_page,
        "select_exact_system_order",
        lambda *_args, **_kwargs: record("select"),
    )
    monkeypatch.setattr(
        reversal_page,
        "click_one_visible_button",
        lambda *_args, **_kwargs: record("button"),
    )
    monkeypatch.setattr(reversal_page, "one_visible_dialog", dialog)
    monkeypatch.setattr(reversal_page, "_one_visible", one_visible)

    evidence = asyncio.run(
        reversal_page.withdraw_shipped_order_to_pending_review(
            ExistingPage(),
            system_order_no=SYSTEM_ORDER_NO,
            platform_order_no="113-1341773-1145022",
            old_waybill_no=NEW_WAYBILL_NO,
            logistics_no="ALS01915029156",
            before_final_confirm=before_confirm,
        )
    )

    assert evidence.system_order_no == SYSTEM_ORDER_NO
    assert events[:3] == ["tab", "search", "select"]
    assert events[-2:] == ["intent", "button"]


class _Locator:
    def filter(self, **_kwargs):
        return self


class _Page:
    def __init__(self) -> None:
        self.waits: list[int] = []

    async def goto(self, _url: str) -> None:
        return None

    def locator(self, _selector: str) -> _Locator:
        return _Locator()

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class _Clickable:
    async def click(self) -> None:
        return None


def test_update_reads_system_marking_cell_once_after_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    row_texts = iter(("可更新行", "标发中行"))
    marking_texts = iter(
        (
            f"OnTrac ： {NEW_WAYBILL_NO} 待标发",
            f"OnTrac ： {NEW_WAYBILL_NO} 标发中",
        )
    )

    async def one_visible(*_args, **_kwargs):
        return _Clickable()

    async def no_op(*_args, **_kwargs):
        return None

    async def row_text(*_args, **_kwargs) -> str:
        value = next(row_texts)
        events.append(f"row:{value}")
        return value

    async def cell_text(*_args, **_kwargs) -> str:
        value = next(marking_texts)
        events.append(f"cell:{value}")
        return value

    async def dialog(_page, required_text: str):
        events.append(f"dialog:{required_text}")
        return _Clickable()

    async def click_button(_scope, text: str, _label: str | None = None):
        events.append(f"click:{text}")

    monkeypatch.setattr(update_page, "_one_visible", one_visible)
    monkeypatch.setattr(update_page, "search_exact_system_order", no_op)
    monkeypatch.setattr(update_page, "select_exact_system_order", no_op)
    monkeypatch.setattr(update_page, "exact_system_order_text", row_text)
    monkeypatch.setattr(update_page, "exact_system_order_cell_text", cell_text)
    monkeypatch.setattr(update_page, "one_visible_dialog", dialog)
    monkeypatch.setattr(update_page, "click_one_visible_button", click_button)

    async def before_confirm() -> None:
        events.append("intent")

    evidence = asyncio.run(
        update_marked_shipment(
            _Page(),
            system_order_no=SYSTEM_ORDER_NO,
            new_waybill_no=NEW_WAYBILL_NO,
            before_final_confirm=before_confirm,
        )
    )

    assert evidence == MarkedShipmentUpdateEvidence(
        system_order_no=SYSTEM_ORDER_NO,
        before_submit_row_text="可更新行",
        before_submit_system_marking_text=f"OnTrac ： {NEW_WAYBILL_NO} 待标发",
        after_submit_row_text="标发中行",
        after_submit_system_marking_text=f"OnTrac ： {NEW_WAYBILL_NO} 标发中",
    )
    assert events[-3:] == [
        "click:我知道了",
        "row:标发中行",
        f"cell:OnTrac ： {NEW_WAYBILL_NO} 标发中",
    ]
    assert events.count(f"cell:OnTrac ： {NEW_WAYBILL_NO} 标发中") == 1


def test_update_fails_closed_when_post_submit_system_marking_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row_texts = iter(("可更新行", "标发中行"))
    marking_texts = iter(
        (
            f"OnTrac ： {NEW_WAYBILL_NO} 待标发",
            "万邦速达 ： WNBAA0494424973YQ 标发中",
        )
    )

    async def one_visible(*_args, **_kwargs):
        return _Clickable()

    async def no_op(*_args, **_kwargs):
        return None

    async def row_text(*_args, **_kwargs) -> str:
        return next(row_texts)

    async def cell_text(*_args, **_kwargs) -> str:
        return next(marking_texts)

    async def dialog(*_args, **_kwargs):
        return _Clickable()

    monkeypatch.setattr(update_page, "_one_visible", one_visible)
    monkeypatch.setattr(update_page, "search_exact_system_order", no_op)
    monkeypatch.setattr(update_page, "select_exact_system_order", no_op)
    monkeypatch.setattr(update_page, "exact_system_order_text", row_text)
    monkeypatch.setattr(update_page, "exact_system_order_cell_text", cell_text)
    monkeypatch.setattr(update_page, "one_visible_dialog", dialog)
    monkeypatch.setattr(update_page, "click_one_visible_button", no_op)

    with pytest.raises(RuntimeError, match="系统标发单号未更新"):
        asyncio.run(
            update_marked_shipment(
                _Page(),
                system_order_no=SYSTEM_ORDER_NO,
                new_waybill_no=NEW_WAYBILL_NO,
            )
        )
