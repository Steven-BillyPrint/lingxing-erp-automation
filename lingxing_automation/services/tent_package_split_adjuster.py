from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tent_package_split_planner import TentPackageSplitItem, TentPackageSplitPackage, TentPackageSplitPlan


@dataclass
class TentPackageSplitResult:
    """帐篷订单拆分包裹页面执行结果。"""

    status: str
    actions: list[str] = field(default_factory=list)
    system_order_nos: list[str] = field(default_factory=list)
    error: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        """转换为批量日志字段，便于记录拆包页面执行结果。"""

        return {
            "package_split_status": self.status,
            "package_split_actions": self.actions,
            "package_split_system_order_nos": self.system_order_nos,
            "package_split_error": self.error,
        }


async def execute_tent_package_split(page, plan: TentPackageSplitPlan) -> TentPackageSplitResult:
    """按拆包计划在领星订单管理页执行拆分包裹操作。"""

    if not plan.required:
        return TentPackageSplitResult(status="package_split_not_required")
    actions: list[str] = []
    try:
        await _ensure_order_checked(page, plan.system_order_no, plan.platform_order_no)
        actions.append("check_order_row")
        dialog = await _open_order_split_dialog(page)
        actions.append("open_split_dialog")
        for package in plan.packages_to_split:
            await _split_package_from_original(page, dialog, package)
            actions.append(f"split_package:{package.package_key}")
            dialog = await _visible_dialog_by_header_title(page, "订单拆分", timeout_ms=5000)
        await _click_dialog_button(dialog, "立即拆分")
        actions.append("submit_split")
        system_order_nos = await _wait_split_success_dialog(page)
        return TentPackageSplitResult(
            status="package_split_complete",
            actions=actions,
            system_order_nos=system_order_nos,
        )
    except Exception as exc:
        await _cancel_split_dialog_if_visible(page)
        return TentPackageSplitResult(status="package_split_error", actions=actions, error=str(exc))


async def _ensure_order_checked(page, system_order_no: str | None, platform_order_no: str | None) -> None:
    """确保目标订单列表行最左侧复选框处于选中状态。"""

    row = await _find_order_checkbox_row(page, system_order_no=system_order_no, platform_order_no=platform_order_no)
    if row is None:
        raise RuntimeError("无法定位需要拆分包裹的订单行复选框。")
    checkbox = row.locator('td[colid="col_2"] .vxe-cell--checkbox, td.col--checkbox .vxe-cell--checkbox')
    if not await checkbox.count():
        raise RuntimeError("目标订单行没有找到可点击的复选框。")
    target = checkbox.nth(0)
    checkbox_class = (await target.get_attribute("class")) or ""
    if "is--checked" in checkbox_class:
        return
    await target.click(timeout=5000)


async def _find_order_checkbox_row(page, *, system_order_no: str | None, platform_order_no: str | None):
    """从 vxe 拆分行段中找到包含真实复选框的订单行。"""

    candidates = []
    if system_order_no:
        candidates.append(page.locator(f'tr.vxe-body--row[rowid="{system_order_no}"]'))
    if platform_order_no:
        candidates.append(page.locator("tr.vxe-body--row").filter(has_text=platform_order_no))
    for rows in candidates:
        try:
            count = await rows.count()
        except Exception:
            count = 0
        for index in range(count):
            row = rows.nth(index)
            try:
                checkbox = row.locator('td[colid="col_2"] .vxe-cell--checkbox, td.col--checkbox .vxe-cell--checkbox')
                if await checkbox.count():
                    return row
            except Exception:
                continue
    return None


async def _open_order_split_dialog(page):
    """打开顶部“订单处理 -> 拆分”弹窗并返回可见弹窗。"""

    await _click_visible_button(page, "订单处理")
    await _click_visible_menu_item(page, "拆分")
    return await _visible_dialog_by_header_title(page, "订单拆分", timeout_ms=8000)


async def _click_visible_button(scope, text: str) -> None:
    """点击当前页面或弹窗中可见且文本匹配的按钮。"""

    buttons = scope.locator("button").filter(has_text=text)
    try:
        count = await buttons.count()
    except Exception:
        count = 0
    for index in range(count):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            label = _normalize_text(await button.inner_text(timeout=500))
            if text not in label:
                continue
            await button.click(timeout=5000)
            return
        except Exception:
            continue
    raise RuntimeError(f"没有找到可点击的“{text}”按钮。")


async def _click_visible_menu_item(page, text: str) -> None:
    """点击当前可见下拉菜单中的指定菜单项。"""

    items = page.locator(".ak-button-group-popover .ak-dropdown-item").filter(has_text=text)
    try:
        count = await items.count()
    except Exception:
        count = 0
    for index in range(count):
        item = items.nth(index)
        try:
            if not await item.is_visible():
                continue
            if _normalize_text(await item.inner_text(timeout=500)) != text:
                continue
            await item.click(timeout=5000)
            return
        except Exception:
            continue
    raise RuntimeError(f"没有找到可点击的“{text}”菜单项。")


async def _visible_dialog_by_header_title(page, title: str, *, timeout_ms: int = 8000):
    """按 Element UI 弹窗标题查找当前真正可见的弹窗。"""

    attempts = max(1, timeout_ms // 200)
    for _ in range(attempts):
        dialogs = page.locator(".el-dialog")
        try:
            count = await dialogs.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            dialog = dialogs.nth(index)
            try:
                if not await dialog.is_visible():
                    continue
                header = dialog.locator(".el-dialog__title")
                header_text = ""
                if await header.count():
                    header_text = _normalize_text(await header.nth(0).inner_text(timeout=300))
                aria_label = (await dialog.get_attribute("aria-label")) or ""
                if title in header_text or title in aria_label:
                    return dialog
            except Exception:
                continue
        await page.wait_for_timeout(200)
    raise RuntimeError(f"未找到标题为“{title}”的可见弹窗。")


async def _split_package_from_original(page, dialog, package: TentPackageSplitPackage) -> None:
    """从订单包裹 1 中选中目标 SKU 并拆成一个新包裹。"""

    if not package.items:
        return
    before_count = await _count_split_packages(page)
    for item in package.items:
        await _set_split_item_quantity(page, item)
    await _click_dialog_button(dialog, "拆分成新包裹")
    await page.wait_for_timeout(600)
    after_count = await _count_split_packages(page)
    if after_count <= before_count:
        raise RuntimeError(f"{package.title} 点击拆分成新包裹后没有生成新包裹。")


async def _set_split_item_quantity(page, item: TentPackageSplitItem) -> None:
    """在拆分弹窗中按 SKU 找到商品行，勾选并填写拆分数量。"""

    remaining = max(1, int(item.quantity))
    requested = remaining
    selected_total = 0
    used_rowids: set[str] = set()
    while remaining > 0:
        try:
            state, row = await _find_split_row_for_sku(page, item.sku, exclude_rowids=used_rowids)
        except RuntimeError as exc:
            if selected_total:
                raise RuntimeError(
                    f"SKU {item.sku} 计划拆分数量 {requested} 超过可拆送货量 {selected_total}。"
                ) from exc
            raise
        rowid = str(row.get("rowid") or "")
        if rowid:
            used_rowids.add(rowid)
        ship_qty = _parse_int(row.get("shipQty"))
        split_qty = _parse_int(row.get("splitQty")) or 0
        capacity = remaining if ship_qty is None else max(0, ship_qty - split_qty)
        if capacity <= 0:
            continue
        quantity = min(remaining, capacity)
        await _set_split_row_quantity(page, state, row, item.sku, quantity)
        remaining -= quantity
        selected_total += quantity


async def _set_split_row_quantity(
    page,
    state: dict[str, Any],
    row: dict[str, Any],
    sku: str,
    quantity: int,
) -> None:
    """勾选单个拆分行并填写该行的拆分数量。"""

    ship_qty = _parse_int(row.get("shipQty"))
    split_qty = _parse_int(row.get("splitQty")) or 0
    if ship_qty is not None and quantity > max(0, ship_qty - split_qty):
        raise RuntimeError(f"SKU {sku} 计划拆分数量 {quantity} 超过送货量 {ship_qty}。")
    dialog = await _visible_dialog_by_header_title(page, "订单拆分", timeout_ms=3000)
    row_locator = dialog.locator(f'tr.vxe-body--row[rowid="{row["rowid"]}"]')
    if await row_locator.count() != 1:
        raise RuntimeError(f"SKU {sku} 对应拆分行数量异常。")
    row_element = row_locator.nth(0)
    checkbox = row_element.locator(f'td[colid="{state["checkboxColId"]}"] .vxe-cell--checkbox')
    if not await checkbox.count():
        raise RuntimeError(f"SKU {sku} 对应行没有复选框。")
    checkbox_element = checkbox.nth(0)
    checkbox_class = (await checkbox_element.get_attribute("class")) or ""
    if "is--checked" not in checkbox_class:
        await checkbox_element.click(timeout=5000)
    split_input = row_element.locator(f'td[colid="{state["splitQtyColId"]}"] input.el-input__inner')
    if not await split_input.count():
        raise RuntimeError(f"SKU {sku} 对应行没有拆分数量输入框。")
    await split_input.nth(0).fill(str(quantity), timeout=5000)


async def _find_split_row_for_sku(
    page,
    sku: str,
    *,
    exclude_rowids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """循环读取并滚动订单包裹 1 表格，直到目标 SKU 行处于可见范围。"""

    visited_scroll_tops: set[int] = set()
    last_state: dict[str, Any] | None = None
    for _ in range(20):
        state = await _read_original_package_table_state(page)
        last_state = state
        row = _matching_visible_row(state, sku, exclude_rowids=exclude_rowids)
        if row is not None:
            return state, row
        target_row = _matching_any_row(state, sku, exclude_rowids=exclude_rowids)
        if target_row is not None:
            await _scroll_split_row_into_view(page, target_row["rowid"])
            await page.wait_for_timeout(200)
            continue
        scroll_top = int(float(state.get("scrollTop") or 0))
        if scroll_top in visited_scroll_tops and not _can_scroll_down(state):
            break
        visited_scroll_tops.add(scroll_top)
        if not _can_scroll_down(state):
            break
        await _scroll_original_package_table(page, int(state.get("clientHeight") or 260) - 48)
        await page.wait_for_timeout(200)
    raise RuntimeError(
        f"拆分弹窗中没有找到 SKU 精确等于 {sku} 的可见行。"
        f"当前订单包裹 1 SKU：{_summarize_split_table_skus(last_state)}。"
    )


async def _read_original_package_table_state(page) -> dict[str, Any]:
    """读取订单包裹 1 表格的表头、滚动容器和可见行状态。"""

    return await page.evaluate(
        """
        () => {
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
            const rectOf = (el) => {
                const rect = el.getBoundingClientRect();
                return {top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right};
            };
            const skuValueOf = (el) => {
                const value = textOf(el);
                const parts = value.split(/\\s+/).filter(Boolean);
                return parts.length ? parts[parts.length - 1] : value;
            };
            const dialogs = Array.from(document.querySelectorAll('.el-dialog')).filter(visible);
            const dialog = dialogs.find((item) => textOf(item.querySelector('.el-dialog__title')).includes('订单拆分'));
            if (!dialog) throw new Error('没有找到可见的订单拆分弹窗。');
            const cards = Array.from(dialog.querySelectorAll('.splitList_warp, .el-card')).filter(visible);
            const originalCard = cards.find((card) => textOf(card).includes('订单包裹 1')) || cards[0];
            if (!originalCard) throw new Error('没有找到订单包裹 1。');
            const table = Array.from(originalCard.querySelectorAll('.vxe-table')).find(visible);
            if (!table) throw new Error('订单包裹 1 没有找到商品表格。');
            const wrapper = table.querySelector('.vxe-table--body-wrapper.body--wrapper');
            if (!wrapper) throw new Error('订单包裹 1 没有找到表格滚动容器。');
            const headers = Array.from(table.querySelectorAll('.vxe-header--column')).map((header) => ({
                colid: header.getAttribute('colid'),
                text: textOf(header),
            }));
            const findCol = (label) => {
                const header = headers.find((item) => item.text === label);
                return header ? header.colid : null;
            };
            const checkboxColId = findCol('序号');
            const skuColId = findCol('品名/SKU');
            const shipQtyColId = findCol('送货量');
            const splitQtyColId = findCol('拆分数量');
            if (!checkboxColId || !skuColId || !shipQtyColId || !splitQtyColId) {
                throw new Error('订单拆分表格缺少必要列。');
            }
            const wrapperRect = rectOf(wrapper);
            const rows = Array.from(table.querySelectorAll('.vxe-body--row')).map((row) => {
                const rowRect = rectOf(row);
                const visibleInsideWrapper = rowRect.bottom > wrapperRect.top && rowRect.top < wrapperRect.bottom;
                const skuCell = row.querySelector(`td[colid="${skuColId}"]`);
                const shipQtyCell = row.querySelector(`td[colid="${shipQtyColId}"]`);
                const splitInput = row.querySelector(`td[colid="${splitQtyColId}"] input`);
                return {
                    rowid: row.getAttribute('rowid'),
                    text: textOf(row),
                    skuText: textOf(skuCell),
                    skuValue: skuValueOf(skuCell),
                    shipQty: textOf(shipQtyCell),
                    splitQty: splitInput ? splitInput.value : null,
                    visibleInsideWrapper,
                    top: rowRect.top,
                    bottom: rowRect.bottom,
                };
            });
            return {
                checkboxColId,
                skuColId,
                shipQtyColId,
                splitQtyColId,
                scrollTop: wrapper.scrollTop,
                scrollHeight: wrapper.scrollHeight,
                clientHeight: wrapper.clientHeight,
                rows,
            };
        }
        """
    )


async def _scroll_original_package_table(page, delta: int) -> None:
    """在订单包裹 1 的 vxe 表格滚动容器中向下滚动。"""

    await page.evaluate(
        """
        (delta) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
            const dialog = Array.from(document.querySelectorAll('.el-dialog'))
                .filter(visible)
                .find((item) => textOf(item.querySelector('.el-dialog__title')).includes('订单拆分'));
            const card = Array.from(dialog.querySelectorAll('.splitList_warp, .el-card')).filter(visible)
                .find((item) => textOf(item).includes('订单包裹 1'));
            const wrapper = card.querySelector('.vxe-table--body-wrapper.body--wrapper');
            wrapper.scrollTop = Math.min(wrapper.scrollHeight - wrapper.clientHeight, wrapper.scrollTop + delta);
            wrapper.dispatchEvent(new Event('scroll', {bubbles: true}));
        }
        """,
        delta,
    )


async def _scroll_split_row_into_view(page, rowid: str) -> None:
    """把目标拆分商品行滚动到订单包裹 1 表格可视范围内。"""

    await page.evaluate(
        """
        (rowid) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const textOf = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
            const dialog = Array.from(document.querySelectorAll('.el-dialog'))
                .filter(visible)
                .find((item) => textOf(item.querySelector('.el-dialog__title')).includes('订单拆分'));
            const card = Array.from(dialog.querySelectorAll('.splitList_warp, .el-card')).filter(visible)
                .find((item) => textOf(item).includes('订单包裹 1'));
            const wrapper = card.querySelector('.vxe-table--body-wrapper.body--wrapper');
            const row = card.querySelector(`tr.vxe-body--row[rowid="${rowid}"]`);
            if (!row) throw new Error(`没有找到拆分商品行 ${rowid}`);
            const wrapperRect = wrapper.getBoundingClientRect();
            const rowRect = row.getBoundingClientRect();
            if (rowRect.top < wrapperRect.top) {
                wrapper.scrollTop -= wrapperRect.top - rowRect.top;
            } else if (rowRect.bottom > wrapperRect.bottom) {
                wrapper.scrollTop += rowRect.bottom - wrapperRect.bottom;
            }
            wrapper.dispatchEvent(new Event('scroll', {bubbles: true}));
        }
        """,
        rowid,
    )


def _matching_visible_row(
    state: dict[str, Any],
    sku: str,
    *,
    exclude_rowids: set[str] | None = None,
) -> dict[str, Any] | None:
    """从表格状态中查找 SKU 匹配且处于可视范围内的行。"""

    excluded = exclude_rowids or set()
    for row in state.get("rows") or []:
        if str(row.get("rowid") or "") in excluded:
            continue
        if row.get("visibleInsideWrapper") and _split_row_sku_matches(row, sku):
            return row
    return None


def _matching_any_row(
    state: dict[str, Any],
    sku: str,
    *,
    exclude_rowids: set[str] | None = None,
) -> dict[str, Any] | None:
    """从表格状态中查找任意 SKU 匹配行，不要求当前可见。"""

    excluded = exclude_rowids or set()
    for row in state.get("rows") or []:
        if str(row.get("rowid") or "") in excluded:
            continue
        if _split_row_sku_matches(row, sku):
            return row
    return None


def _can_scroll_down(state: dict[str, Any]) -> bool:
    """判断订单包裹 1 表格是否还能继续向下滚动。"""

    scroll_top = float(state.get("scrollTop") or 0)
    scroll_height = float(state.get("scrollHeight") or 0)
    client_height = float(state.get("clientHeight") or 0)
    return scroll_top + client_height + 1 < scroll_height


def _sku_text_matches(row_sku_text: str | None, sku: str | None) -> bool:
    """判断拆分表格品名/SKU 单元格是否精确包含目标 SKU。"""

    haystack = _normalize_text(row_sku_text)
    needle = _normalize_text(sku)
    if not haystack or not needle:
        return False
    for candidate in _sku_text_candidates(haystack):
        if candidate.casefold() == needle.casefold():
            return True
    pattern = rf"(?<![A-Za-z0-9._-]){re.escape(needle)}(?![A-Za-z0-9._-])"
    return re.search(pattern, haystack, flags=re.IGNORECASE) is not None


def _split_row_sku_matches(row: dict[str, Any], sku: str | None) -> bool:
    return _sku_text_matches(row.get("skuValue"), sku) or _sku_text_matches(row.get("skuText"), sku)


def _sku_text_candidates(text: str | None) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    candidates = [normalized]
    for token in re.split(r"[\s,;，；()（）]+", normalized):
        token = token.strip()
        if token:
            candidates.append(token)
    return candidates


def _summarize_split_table_skus(state: dict[str, Any] | None) -> str:
    """汇总拆分弹窗当前包裹 1 的 SKU 文本，用于缺 SKU 报错诊断。"""

    if not state:
        return "未读取到表格"
    skus: list[str] = []
    for row in state.get("rows") or []:
        text = _normalize_text(row.get("skuText"))
        if text and text not in skus:
            skus.append(text)
    if not skus:
        return "无"
    return "；".join(skus[:8])


async def _click_dialog_button(dialog, text: str) -> None:
    """点击弹窗内文本精确匹配的可见按钮。"""

    buttons = dialog.locator("button").filter(has_text=text)
    try:
        count = await buttons.count()
    except Exception:
        count = 0
    for index in range(count):
        button = buttons.nth(index)
        try:
            if not await button.is_visible():
                continue
            if _normalize_text(await button.inner_text(timeout=500)) != text:
                continue
            await button.click(timeout=5000)
            return
        except Exception:
            continue
    raise RuntimeError(f"弹窗内没有找到可点击的“{text}”按钮。")


async def _count_split_packages(page) -> int:
    """统计订单拆分弹窗中当前包裹数量。"""

    return int(
        await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
                const dialog = Array.from(document.querySelectorAll('.el-dialog'))
                    .filter(visible)
                    .find((item) => textOf(item.querySelector('.el-dialog__title')).includes('订单拆分'));
                if (!dialog) return 0;
                return Array.from(dialog.querySelectorAll('.item_title')).filter((item) => /^订单包裹\\s+\\d+/.test(textOf(item))).length;
            }
            """
        )
    )


async def _wait_split_success_dialog(page, *, timeout_ms: int = 12000) -> list[str]:
    """等待拆分成功提示弹窗出现并提取新系统单号。"""

    attempts = max(1, timeout_ms // 300)
    for _ in range(attempts):
        payload = await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
                const dialog = Array.from(document.querySelectorAll('.el-dialog')).filter(visible)
                    .find((item) => textOf(item).includes('成功拆分'));
                if (!dialog) return null;
                const text = textOf(dialog);
                return {text, systemOrderNos: Array.from(new Set(text.match(/\\d{15,}/g) || []))};
            }
            """
        )
        if payload:
            await _click_success_ack_if_visible(page)
            return list(payload.get("systemOrderNos") or [])
        await page.wait_for_timeout(300)
    raise RuntimeError("点击立即拆分后没有等到成功拆分提示。")


async def _click_success_ack_if_visible(page) -> None:
    """成功提示出现后点击“我知道了”关闭结果弹窗。"""

    buttons = page.locator("button").filter(has_text="我知道了")
    try:
        count = await buttons.count()
    except Exception:
        count = 0
    for index in range(count):
        button = buttons.nth(index)
        try:
            if await button.is_visible():
                await button.click(timeout=3000)
                return
        except Exception:
            continue


async def _cancel_split_dialog_if_visible(page) -> None:
    """发生异常时尽量取消拆分弹窗，避免页面停在半操作状态。"""

    try:
        dialog = await _visible_dialog_by_header_title(page, "订单拆分", timeout_ms=800)
        await _click_dialog_button(dialog, "取消")
    except Exception:
        return


def _normalize_text(value: str | None) -> str:
    """压缩页面文本空白，便于按钮和菜单项比较。"""

    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_int(value: Any) -> int | None:
    """从页面数量文本中解析整数，解析失败时返回空值。"""

    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None
