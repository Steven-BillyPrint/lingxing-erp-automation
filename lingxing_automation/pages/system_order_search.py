"""Locator-only helpers for exact Lingxing system-order searches.

These helpers intentionally do not read or calculate element coordinates.
"""

from __future__ import annotations

import re
from typing import Any


async def _visible(locators: Any) -> list[Any]:
    return [
        locators.nth(index)
        for index in range(await locators.count())
        if await locators.nth(index).is_visible()
    ]


async def _one_visible(locators: Any, label: str) -> Any:
    matches = await _visible(locators)
    if len(matches) != 1:
        raise RuntimeError(f"{label}必须且只能有一个可见 DOM 节点，实际 {len(matches)} 个。")
    return matches[0]


async def select_system_order_search(page: Any) -> tuple[Any, Any]:
    """Open the dropdown and explicitly select 系统单号 every time."""

    root = await _one_visible(page.locator("#advanced-input"), "订单搜索控件")
    selector = await _one_visible(
        root.locator(".el-input-group__prepend .el-select"),
        "订单搜索类型下拉框",
    )
    # The user requires opening the dropdown even when the current value is
    # already 系统单号, so stale UI state can never be assumed.
    await selector.click()
    option = await _one_visible(
        page.locator("li.el-select-dropdown__item").filter(
            has_text=re.compile(r"^\s*系统单号\s*$")
        ),
        "系统单号下拉项",
    )
    await option.click()
    await page.wait_for_timeout(150)
    selected = " ".join((await selector.inner_text()).split())
    if "系统单号" not in selected:
        input_value = await selector.locator("input").first.input_value()
        if input_value.strip() != "系统单号":
            raise RuntimeError("订单搜索类型没有切换为系统单号。")
    search_input = await _one_visible(
        root.locator("input.el-input__inner:not([readonly])"),
        "系统单号输入框",
    )
    return root, search_input


async def search_exact_system_order(page: Any, system_order_no: str) -> dict[str, Any]:
    system_order_no = str(system_order_no or "").strip()
    if not system_order_no:
        raise ValueError("系统单号不能为空。")
    root, search_input = await select_system_order_search(page)
    await search_input.fill(system_order_no)
    if await search_input.input_value() != system_order_no:
        raise RuntimeError("系统单号输入框读回值不一致。")
    search_button = await _one_visible(
        root.locator(".lx_combo_search"),
        "系统单号搜索按钮",
    )
    await search_button.click()
    await page.wait_for_timeout(800)
    return {
        "selected_search_type": "系统单号",
        "system_order_no": system_order_no,
    }


async def exact_system_order_fragments(page: Any, system_order_no: str) -> list[Any]:
    normalized = str(system_order_no or "").strip()
    system_header = await _one_visible(
        page.locator("th[colid]").filter(
            has_text=re.compile(r"^\s*系统单号\s*$")
        ),
        "系统单号表头",
    )
    column_id = str(await system_header.get_attribute("colid") or "").strip()
    if not column_id:
        raise RuntimeError("系统单号表头缺少 colid，禁止猜测列位置。")
    logical_rows: list[Any] = []
    for row in await _visible(page.locator("tbody tr")):
        cells = await _visible(row.locator(f':scope > td[colid="{column_id}"]'))
        if len(cells) > 1:
            raise RuntimeError("同一订单行出现多个系统单号单元格。")
        if not cells:
            continue
        text = " ".join((await cells[0].inner_text()).split())
        if re.search(rf"(?<!\d){re.escape(normalized)}(?!\d)", text):
            logical_rows.append(row)
    if len(logical_rows) != 1:
        raise RuntimeError(f"页面没有返回系统单号 {system_order_no}。")
    row_id = str(await logical_rows[0].get_attribute("rowid") or "").strip()
    if not row_id:
        return logical_rows
    escaped_row_id = row_id.replace('"', '\\"')
    fragments = await _visible(page.locator(f'tr[rowid="{escaped_row_id}"]'))
    return fragments or logical_rows


async def exact_system_order_text(page: Any, system_order_no: str) -> str:
    fragments = await exact_system_order_fragments(page, system_order_no)
    # An ``await`` inside a generator expression turns that expression into
    # an async generator, which ``str.join`` cannot consume.  Read each DOM
    # fragment explicitly so the result remains a normal list of strings.
    fragment_texts: list[str] = []
    for fragment in fragments:
        fragment_texts.append(" ".join((await fragment.inner_text()).split()))
    return " ".join(fragment_texts).strip()


async def exact_system_order_cell_text(
    page: Any,
    system_order_no: str,
    header_text: str,
) -> str:
    """Read one named cell from the exact system-order row.

    Lingxing's virtual table can split one logical row into multiple ``tr``
    fragments and its generated ``rowid`` is not always the system order
    number.  Resolve the column through the visible header's ``colid`` and
    then inspect every fragment belonging to the already-verified row.
    """

    normalized_header = str(header_text or "").strip()
    if not normalized_header:
        raise ValueError("表头名称不能为空。")
    header = await _one_visible(
        page.locator("th[colid]").filter(
            has_text=re.compile(rf"^\s*{re.escape(normalized_header)}\s*$")
        ),
        f"{normalized_header}表头",
    )
    column_id = str(await header.get_attribute("colid") or "").strip()
    if not column_id:
        raise RuntimeError(f"{normalized_header}表头缺少 colid，禁止猜测列位置。")
    fragments = await exact_system_order_fragments(page, system_order_no)
    cells: list[Any] = []
    for fragment in fragments:
        cells.extend(
            await _visible(fragment.locator(f':scope > td[colid="{column_id}"]'))
        )
    if len(cells) != 1:
        raise RuntimeError(
            f"系统单号 {system_order_no} 的{normalized_header}单元格必须唯一，"
            f"实际 {len(cells)} 个。"
        )
    return " ".join((await cells[0].inner_text()).split()).strip()


async def _order_checkbox_is_checked(target: Any) -> bool:
    """Read the rendered checkbox state instead of counting VXE icon nodes.

    VXE always keeps checked, unchecked, and indeterminate icons in the DOM and
    switches their CSS visibility.  Counting ``.vxe-checkbox--checked-icon``
    therefore reports an unchecked row as checked and skips the required click.
    """

    if str(await target.get_attribute("aria-checked") or "").casefold() == "true":
        return True
    if await _visible(target.locator(".vxe-checkbox--checked-icon")):
        return True
    if str(await target.get_attribute("type") or "").casefold() == "checkbox":
        try:
            return bool(await target.is_checked())
        except Exception:
            return False
    return False


async def select_exact_system_order(page: Any, system_order_no: str) -> None:
    fragments = await exact_system_order_fragments(page, system_order_no)
    checkbox_targets: list[Any] = []
    for fragment in fragments:
        checkbox_targets.extend(
            await _visible(
                fragment.locator(
                    ".vxe-cell--checkbox,input[type=checkbox],[role=checkbox]"
                )
            )
        )
    if len(checkbox_targets) != 1:
        raise RuntimeError(
            f"系统单号 {system_order_no} 的勾选节点不唯一，实际 {len(checkbox_targets)} 个。"
        )
    target = checkbox_targets[0]
    if not await _order_checkbox_is_checked(target):
        await target.click()
    await page.wait_for_timeout(150)
    if not await _order_checkbox_is_checked(target):
        raise RuntimeError(f"系统单号 {system_order_no} 勾选后未读到已选状态。")


async def click_one_visible_button(scope: Any, text: str, label: str | None = None) -> None:
    flexible_text = r"\s*".join(re.escape(character) for character in text)
    button = await _one_visible(
        scope.get_by_role("button", name=re.compile(rf"^\s*{flexible_text}\s*$")),
        label or f"{text}按钮",
    )
    await button.click()


async def one_visible_dialog(page: Any, required_text: str) -> Any:
    dialogs = await _visible(
        page.locator(
            ".el-dialog__wrapper,.el-message-box__wrapper,.ant-modal-wrap,"
            ".next-dialog-wrapper"
        ).filter(has_text=required_text)
    )
    if len(dialogs) == 1:
        return dialogs[0]
    if len(dialogs) > 1:
        normalized_texts = {
            " ".join((await dialog.inner_text()).split()) for dialog in dialogs
        }
        if len(normalized_texts) == 1:
            ranked: list[tuple[int, int, Any]] = []
            for index, dialog in enumerate(dialogs):
                z_index = await dialog.evaluate(
                    "element => Number(getComputedStyle(element).zIndex) || 0"
                )
                ranked.append((int(z_index), index, dialog))
            return max(ranked, key=lambda item: (item[0], item[1]))[2]
    raise RuntimeError(
        f"包含“{required_text}”的弹窗必须且只能有一个语义目标，实际 {len(dialogs)} 个。"
    )
