from __future__ import annotations

import re
import time
from typing import Any

from ..parsers.orders import validate_search_snapshot
from .order_detail_navigation import (
    close_order_detail_dialog,
    dismiss_known_blocking_dialogs,
    dismiss_order_search_overlays,
)


_ORDER_SEARCH_RESULT_TIMEOUT_MS = 30_000


def _normalize_search_label(value: str | None) -> str:
    text = " ".join(str(value or "").split())
    if "系统单号" in text:
        return "系统单号"
    if "平台订单号" in text or "平台单号" in text:
        return "平台单号"
    return text


async def _order_search_root(page):
    """返回唯一可见的订单号组合搜索控件，不按坐标挑选副本。"""
    roots = page.locator("#advanced-input:visible")
    candidates = []
    for index in range(await roots.count()):
        root = roots.nth(index)
        if (
            await root.locator(".search-input > input.el-input__inner").count() == 1
            and await root.locator(".el-input-group__prepend .el-select").count() == 1
            and await root.locator(".lx_combo_search").count() == 1
        ):
            candidates.append(root)
    if len(candidates) != 1:
        raise RuntimeError(
            "没有唯一识别到订单号搜索控件："
            f"当前找到 {len(candidates)} 个可见候选。"
        )
    return candidates[0]


async def _selected_search_label(root) -> str:
    dropdown = root.locator(".el-input-group__prepend .el-select")
    label_input = dropdown.locator("input.el-input__inner")
    value = ""
    if await label_input.count():
        value = await label_input.first.input_value()
    if not value:
        value = await dropdown.inner_text()
    return _normalize_search_label(value)


async def _search_input_index(search_input, fallback: int | None = None) -> int | None:
    try:
        index = await search_input.evaluate(
            "(el) => Array.from(document.querySelectorAll('input')).indexOf(el)"
        )
    except Exception:
        index = fallback
    return index if isinstance(index, int) and index >= 0 else None


async def close_search_overlays(page) -> None:
    """关闭已知公告和订单搜索临时弹层，不点击坐标或页面空白处。"""
    await dismiss_known_blocking_dialogs(page)
    await dismiss_order_search_overlays(page)


async def get_order_search_snapshot(
    page,
    search_input_index: int | None = None,
) -> dict[str, Any]:
    """读取订单搜索区域当前状态，用于校验搜索条件。"""
    roots = page.locator("#advanced-input:visible")
    try:
        root = await _order_search_root(page)
    except RuntimeError:
        return {
            "selectedLabel": None,
            "dropdownRect": None,
            "hasAdvancedInput": False,
            "advancedInputCount": await roots.count(),
            "searchInputIndex": None,
            "inputs": [],
        }

    search_input = root.locator(".search-input > input.el-input__inner")
    resolved_index = await _search_input_index(search_input, search_input_index)
    all_inputs = page.locator("input")
    inputs: list[dict[str, Any]] = []
    for index in range(await all_inputs.count()):
        input_locator = all_inputs.nth(index)
        try:
            placeholder = str(await input_locator.get_attribute("placeholder") or "")
            input_id = str(await input_locator.get_attribute("id") or "")
            name = str(await input_locator.get_attribute("name") or "")
            aria_label = str(await input_locator.get_attribute("aria-label") or "")
            inputs.append(
                {
                    "index": index,
                    "value": await input_locator.input_value(),
                    "placeholder": placeholder,
                    "type": str(await input_locator.get_attribute("type") or ""),
                    "around": " ".join(
                        part for part in (placeholder, name, input_id, aria_label) if part
                    )[:160],
                    "visible": await input_locator.is_visible(),
                    "isSearchInput": index == resolved_index,
                }
            )
        except Exception:
            inputs.append(
                {
                    "index": index,
                    "value": "",
                    "placeholder": "",
                    "type": "",
                    "around": "页面刷新时输入框已被替换",
                    "visible": False,
                    "isSearchInput": index == resolved_index,
                }
            )

    return {
        "selectedLabel": await _selected_search_label(root),
        "dropdownRect": None,
        "hasAdvancedInput": True,
        "advancedInputCount": await roots.count(),
        "searchInputIndex": resolved_index,
        "inputs": inputs,
    }


async def select_order_search_type(page, search_kind: str) -> str:
    """通过可操作的下拉定位器选择平台单号或系统单号。"""
    target_label = "系统单号" if search_kind == "system" else "平台单号"
    await close_search_overlays(page)
    root = await _order_search_root(page)
    current_label = await _selected_search_label(root)
    if current_label == target_label:
        return target_label

    dropdown = root.locator(".el-input-group__prepend .el-select")
    await dropdown.click(timeout=3000)

    matching_options = []
    for _ in range(20):
        matching_options = []
        options = page.locator("li.el-select-dropdown__item:visible")
        for index in range(await options.count()):
            option = options.nth(index)
            text = " ".join((await option.inner_text()).split())
            class_name = str(await option.get_attribute("class") or "")
            if (
                _normalize_search_label(text) == target_label
                and "is-disabled" not in class_name
                and await option.is_enabled()
            ):
                matching_options.append(option)
        if matching_options:
            break
        await page.wait_for_timeout(100)

    if len(matching_options) != 1:
        raise RuntimeError(
            f"没有唯一找到订单搜索类型 {target_label}："
            f"当前可操作候选 {len(matching_options)} 个。"
        )
    await matching_options[0].click(timeout=3000)

    for _ in range(20):
        if await _selected_search_label(root) == target_label:
            return target_label
        await page.wait_for_timeout(100)
    raise RuntimeError(
        f"搜索类型切换失败：期望 {target_label}，"
        f"当前 {await _selected_search_label(root) or '未知'}。"
    )


async def find_order_search_input_index(page) -> int:
    """按订单搜索控件 DOM 关系定位输入框索引。"""
    root = await _order_search_root(page)
    index = await _search_input_index(
        root.locator(".search-input > input.el-input__inner")
    )
    if index is not None:
        return index
    raise RuntimeError("没有找到平台/系统单号下拉右侧的订单号输入框。")


async def _order_search_result_state(
    page,
    order_no: str,
    *,
    search_kind: str,
) -> dict[str, Any]:
    """读取订单表筛选状态，区分目标行与仍残留的其他订单行。"""
    table_roots = page.locator(".vxe-table:visible").filter(
        has_text=re.compile(r"系统单号.*平台单号", re.S)
    )
    rows_by_id: dict[str, str] = {}
    for root_index in range(await table_roots.count()):
        root = table_roots.nth(root_index)
        rows = root.locator(
            ".vxe-table--fixed-left-wrapper "
            "tr.vxe-body--row[rowid]:visible"
        )
        if await rows.count() == 0:
            rows = root.locator("tr.vxe-body--row[rowid]:visible")
        for row_index in range(await rows.count()):
            row = rows.nth(row_index)
            row_id = str(await row.get_attribute("rowid") or "").strip()
            if not re.fullmatch(r"\d{15,24}", row_id):
                continue
            content = " ".join((await row.inner_text()).split())
            previous = rows_by_id.get(row_id, "")
            if len(content) > len(previous):
                rows_by_id[row_id] = content

    matching_row_ids: list[str] = []
    mismatched_row_ids: list[str] = []
    for row_id, content in rows_by_id.items():
        matches = (
            row_id == order_no
            if search_kind == "system"
            else order_no in content
        )
        (matching_row_ids if matches else mismatched_row_ids).append(row_id)

    loading_count = await page.locator(
        ".el-loading-mask:visible,.vxe-loading:visible,"
        ".ant-spin-spinning:visible,[class*='loading-mask']:visible"
    ).count()
    pager_totals: list[int] = []
    pagers = page.locator(".vxe-pager:visible,.el-pagination:visible")
    for pager_index in range(await pagers.count()):
        pager_text = " ".join((await pagers.nth(pager_index).inner_text()).split())
        total_match = re.search(r"共\s*(\d+)\s*条", pager_text)
        if total_match:
            pager_totals.append(int(total_match.group(1)))
    unique_pager_totals = sorted(set(pager_totals))
    total_count = unique_pager_totals[0] if len(unique_pager_totals) == 1 else None
    zero_total = total_count == 0

    settled = loading_count == 0 and (
        zero_total
        or (
            bool(matching_row_ids)
            and not mismatched_row_ids
            and (
                total_count is None
                or total_count == len(matching_row_ids)
            )
        )
    )
    return {
        "settled": settled,
        "matching_row_ids": matching_row_ids,
        "mismatched_row_ids": mismatched_row_ids,
        "loading_count": loading_count,
        "zero_total": zero_total,
        "total_count": total_count,
        "pager_totals": unique_pager_totals,
    }


async def _wait_for_settled_order_search_result(
    page,
    order_no: str,
    *,
    search_kind: str,
    timeout_ms: int = _ORDER_SEARCH_RESULT_TIMEOUT_MS,
) -> dict[str, Any]:
    """等待搜索结果只剩目标订单（或权威零结果），避免读取旧列表。"""
    deadline = time.monotonic() + timeout_ms / 1000
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = await _order_search_result_state(
            page,
            order_no,
            search_kind=search_kind,
        )
        if state.get("settled"):
            return state
        await page.wait_for_timeout(150)
    return state


async def click_order_search_button(
    page,
    search_input_index: int,
    order_no: str,
    search_kind: str,
) -> bool:
    """点击同一组合搜索控件，并以页面最终结果处理点击确认超时。"""
    root = await _order_search_root(page)
    search_input = root.locator(".search-input > input.el-input__inner")
    resolved_index = await _search_input_index(search_input)
    if resolved_index != search_input_index:
        raise RuntimeError("订单号搜索输入框在触发查询前已被页面替换。")
    if str(await search_input.input_value()).strip() != order_no:
        raise RuntimeError("订单号搜索输入框在触发查询前内容已变化。")

    buttons = root.locator(".lx_combo_search:visible")
    if await buttons.count() != 1:
        return False
    click_error: Exception | None = None
    try:
        # 领星搜索会在 click 事件中启动异步列表刷新。真实页面可能已经显示
        # 搜索结果，但 Playwright 仍在等待事件关联的导航，继而把已生效点击
        # 误报成超时。这里不等待导航；若底层仍报错，再以列表中的精确订单号
        # 作为唯一成功证据，且绝不重复点击。
        await buttons.first.click(timeout=10_000, no_wait_after=True)
    except Exception as exc:
        click_error = exc
    await page.wait_for_timeout(150)
    await dismiss_order_search_overlays(page)
    result_state = await _wait_for_settled_order_search_result(
        page,
        order_no,
        search_kind=search_kind,
    )
    if not result_state.get("settled"):
        error = RuntimeError(
            "订单搜索没有进入目标筛选结果："
            f"匹配行 {result_state.get('matching_row_ids') or []}，"
            f"其他可见行 {result_state.get('mismatched_row_ids') or []}，"
            f"分页总数 {result_state.get('total_count')}，"
            f"加载层 {result_state.get('loading_count') or 0}。"
        )
        if click_error is not None:
            raise error from click_error
        raise error
    return True


async def fill_order_search(page, order_no: str, search_kind: str) -> dict[str, Any]:
    """填写订单搜索条件并触发查询，全程使用 DOM 关系和可操作定位器。"""
    await close_order_detail_dialog(page)
    await close_search_overlays(page)
    selected_label = await select_order_search_type(page, search_kind)
    root = await _order_search_root(page)
    search_input = root.locator(".search-input > input.el-input__inner")
    search_input_index = await _search_input_index(search_input)
    if search_input_index is None:
        raise RuntimeError("没有找到平台/系统单号下拉右侧的订单号输入框。")

    await search_input.fill(order_no, timeout=3000)
    await page.wait_for_timeout(100)
    snapshot = await get_order_search_snapshot(page, search_input_index)
    ok, message = validate_search_snapshot(
        order_no,
        selected_label,
        snapshot.get("selectedLabel"),
        snapshot.get("inputs", []),
        search_input_index,
    )
    search_value = next(
        (
            str(item.get("value") or "")
            for item in snapshot.get("inputs", [])
            if item.get("index") == search_input_index
        ),
        None,
    )
    if not ok:
        return {
            "selected_search_type": snapshot.get("selectedLabel"),
            "search_input_value": search_value,
            "search_validation_message": message,
            "search_input_index": search_input_index,
            "search_validation_ok": False,
        }
    if not await click_order_search_button(
        page,
        search_input_index,
        order_no,
        search_kind,
    ):
        raise RuntimeError("没有找到订单号输入框右侧的搜索按钮。")
    return {
        "selected_search_type": selected_label,
        "search_input_value": search_value,
        "search_validation_message": message,
        "search_input_index": search_input_index,
        "search_validation_ok": True,
    }
