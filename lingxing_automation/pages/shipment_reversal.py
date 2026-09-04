"""DOM-only adapter for withdrawing one shipped Lingxing order."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .system_order_search import (
    _one_visible,
    click_one_visible_button,
    exact_system_order_text,
    one_visible_dialog,
    search_exact_system_order,
    select_exact_system_order,
)


ORDER_MANAGEMENT_URL = "https://erp.lingxing.com/erp/mmulti/mpOrderManagement"


def _is_order_management_page(url: object) -> bool:
    parsed = urlparse(str(url or "").strip())
    return (
        parsed.scheme == "https"
        and str(parsed.hostname or "").casefold() == "erp.lingxing.com"
        and parsed.path.rstrip("/") == "/erp/mmulti/mpOrderManagement"
    )


@dataclass(frozen=True)
class ShipmentReversalEvidence:
    system_order_no: str
    row_text: str
    selected_search_type: str = "系统单号"
    withdrawal_target: str = "撤销回待审核"
    reason: str = "其他"


async def _select_order_tab(page: Any, text: str) -> None:
    tab = await _one_visible(
        page.locator(".el-tabs__item,[role=tab]").filter(
            has_text=re.compile(rf"^\s*{re.escape(text)}(?:\(\d+\))?\s*$")
        ),
        f"{text}订单标签",
    )
    await tab.click()
    await page.wait_for_timeout(500)


async def withdraw_shipped_order_to_pending_review(
    page: Any,
    *,
    system_order_no: str,
    platform_order_no: str,
    old_waybill_no: str,
    logistics_no: str,
    before_final_confirm: Callable[[], Awaitable[None]] | None = None,
) -> ShipmentReversalEvidence:
    # The desktop runner has already attached to and authenticated this page.
    # Re-navigating the same SPA route waits for every load event again and was
    # the largest avoidable delay before the system-order search.
    if not _is_order_management_page(getattr(page, "url", "")):
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
    await _select_order_tab(page, "已发货")
    await search_exact_system_order(page, system_order_no)
    row_text = await exact_system_order_text(page, system_order_no)
    for required in (system_order_no, platform_order_no, old_waybill_no, logistics_no):
        if required and required not in row_text:
            raise RuntimeError(f"已发货行缺少预期字段 {required}，禁止撤销。")
    if "手动-" not in row_text:
        raise RuntimeError("已发货行不是手动-xxxx 物流，禁止撤销。")
    await select_exact_system_order(page, system_order_no)
    await click_one_visible_button(page, "撤销发货")

    warning = await one_visible_dialog(page, "特殊订单忽略不执行")
    await click_one_visible_button(warning, "确定", "第一次撤销警告的确定按钮")

    dialog = await one_visible_dialog(page, "撤销发货")
    pending_review = await _one_visible(
        dialog.get_by_text("撤销回待审核", exact=True),
        "撤销回待审核选项",
    )
    await pending_review.click()
    reason = await _one_visible(dialog.get_by_text("其他", exact=True), "其他原因选项")
    await reason.click()
    if before_final_confirm is not None:
        await before_final_confirm()
    await click_one_visible_button(dialog, "确定", "撤销回待审核确定按钮")
    await page.wait_for_timeout(700)
    return ShipmentReversalEvidence(system_order_no, row_text)
