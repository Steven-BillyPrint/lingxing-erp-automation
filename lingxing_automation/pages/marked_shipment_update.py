"""DOM-only adapter for Lingxing 订单标发 > 可更新."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any

from .system_order_search import (
    _one_visible,
    click_one_visible_button,
    exact_system_order_cell_text,
    exact_system_order_text,
    one_visible_dialog,
    search_exact_system_order,
    select_exact_system_order,
)


ORDER_MARKING_URL = "https://erp.lingxing.com/erp/mmulti/mpOrderMarking"


def system_marking_contains_waybill(text: object, waybill_no: object) -> bool:
    normalized = str(waybill_no or "").strip()
    return bool(
        normalized
        and re.search(
            rf"(?<![A-Za-z0-9]){re.escape(normalized)}(?![A-Za-z0-9])",
            str(text or ""),
            re.IGNORECASE,
        )
    )


@dataclass(frozen=True)
class MarkedShipmentUpdateEvidence:
    system_order_no: str
    before_submit_row_text: str
    before_submit_system_marking_text: str
    after_submit_row_text: str
    after_submit_system_marking_text: str
    selected_search_type: str = "系统单号"


async def update_marked_shipment(
    page: Any,
    *,
    system_order_no: str,
    new_waybill_no: str,
    before_final_confirm: Callable[[], Awaitable[None]] | None = None,
) -> MarkedShipmentUpdateEvidence:
    await page.goto(ORDER_MARKING_URL)
    updateable = await _one_visible(
        page.locator(".el-tabs__item,[role=tab]").filter(
            has_text=re.compile(r"^\s*可更新(?:\(\d+\))?\s*$")
        ),
        "可更新标签",
    )
    await updateable.click()
    await page.wait_for_timeout(500)
    await search_exact_system_order(page, system_order_no)
    before_submit_row_text = await exact_system_order_text(page, system_order_no)
    before_submit_system_marking_text = await exact_system_order_cell_text(
        page,
        system_order_no,
        "系统标发单号",
    )
    if not system_marking_contains_waybill(
        before_submit_system_marking_text,
        new_waybill_no,
    ):
        raise RuntimeError("可更新行的系统标发单号不是本周期新运单号，禁止标发。")
    await select_exact_system_order(page, system_order_no)
    primary_mark = await _one_visible(
        page.locator("button.el-button--primary").filter(
            has_text=re.compile(r"^\s*标发\s*$")
        ),
        "批量标发按钮",
    )
    await primary_mark.click()
    confirm = await one_visible_dialog(page, "确认对接平台标记发货")
    if before_final_confirm is not None:
        await before_final_confirm()
    await click_one_visible_button(confirm, "确定", "标发确认按钮")
    success = await one_visible_dialog(page, "全部操作成功")
    await click_one_visible_button(success, "我知道了", "标发成功确认按钮")
    # The success dialog closes only after Lingxing has moved the row to its
    # async marking state.  A single DOM read of 系统标发单号 is the durable UI
    # acknowledgement; do not poll 标发中/已完成 afterwards.
    await page.wait_for_timeout(150)
    after_submit_row_text = await exact_system_order_text(page, system_order_no)
    after_submit_system_marking_text = await exact_system_order_cell_text(
        page,
        system_order_no,
        "系统标发单号",
    )
    if not system_marking_contains_waybill(
        after_submit_system_marking_text,
        new_waybill_no,
    ):
        raise RuntimeError("标发成功弹窗关闭后，系统标发单号未更新为本周期新运单号。")
    return MarkedShipmentUpdateEvidence(
        system_order_no=system_order_no,
        before_submit_row_text=before_submit_row_text,
        before_submit_system_marking_text=before_submit_system_marking_text,
        after_submit_row_text=after_submit_row_text,
        after_submit_system_marking_text=after_submit_system_marking_text,
    )
