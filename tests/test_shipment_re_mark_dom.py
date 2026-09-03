from __future__ import annotations

import asyncio

import pytest

import lingxing_automation.pages.marked_shipment_update as update_page
from lingxing_automation.pages.marked_shipment_update import (
    MarkedShipmentUpdateEvidence,
    system_marking_contains_waybill,
    update_marked_shipment,
)
from lingxing_automation.pages.system_order_search import (
    exact_system_order_cell_text,
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
