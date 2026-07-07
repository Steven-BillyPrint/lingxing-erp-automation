from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tent_sku_planner import TentSkuAdjustmentPlan, TentSkuPlanAction, extract_shipping_address_line


INSTRUCTION_CUSTOMER_REMARK_RE = re.compile(r"(?<![\d.])(?:\d{4}|\d{1,2}\.\d{1,2})发说明书(?![\d.])")


@dataclass
class TentSkuAdjustmentResult:
    """帐篷订单 SKU 调整执行结果。"""

    status: str
    actions: list[str] = field(default_factory=list)
    error: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        """将当前对象转换为日志字典，便于批量流程记录和排查。"""
        return {
            "sku_adjustment_status": self.status,
            "sku_adjustment_actions": self.actions,
            "sku_adjustment_error": self.error,
        }


async def read_detail_shipping_address_text(page) -> str:
    """
    从详情页 DOM 读取收货信息区域文本。

    SKU 阶段只需要判断国家/州/城市是否属于美国非本土地区；
    这里读取页面结构化 DOM 文本，不使用坐标或截图，避免页面缩放导致识别失效。
    """

    try:
        value = await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => String(el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
                const valueOf = (el) => {
                    if (!el) return '';
                    if ('value' in el && String(el.value || '').trim()) return String(el.value || '').trim();
                    return textOf(el);
                };
                const detailRoots = Array.from(document.querySelectorAll('.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'))
                    .filter((el) => {
                        if (el === document.body || el === document.documentElement || !visible(el)) return false;
                        const text = textOf(el);
                        return /系统单号/.test(text) && /收货信息/.test(text);
                    })
                    .map((el) => ({ el, text: textOf(el) }))
                    .sort((a, b) => a.text.length - b.text.length)
                    .map((item) => item.el);
                const root = detailRoots[0] || document.body;
                const shippingRoots = Array.from(root.querySelectorAll('div,section,article,td,ul,li'))
                    .filter((el) => visible(el))
                    .map((el) => ({ el, text: textOf(el) }))
                    .filter((item) => /收货信息/.test(item.text) && (/收件地址/.test(item.text) || /详细地址/.test(item.text)))
                    .sort((a, b) => a.text.length - b.text.length)
                    .map((item) => item.el);
                const scope = shippingRoots[0] || root;
                const cleanValue = (value, label) => {
                    let text = String(value || '').replace(/\\s+/g, ' ').trim();
                    text = text.replace(new RegExp(`^${label}\\s*[:：]?\\s*`), '').trim();
                    return text && text !== '-' ? text : '';
                };
                const extractFromRowText = (rowText, label) => {
                    const text = String(rowText || '').replace(/\\s+/g, ' ').trim();
                    const pattern = new RegExp(`${label}\\s*[:：]?\\s*(.+?)(?=\\s*(?:详细地址|门牌号|邮编|地址类型|买家姓名|收件人|公司|电话|买家邮箱)\\s*[:：]?|$)`);
                    const match = text.match(pattern);
                    return match ? cleanValue(match[1], label) : '';
                };
                const extractByLabel = (label) => {
                    const labels = Array.from(scope.querySelectorAll('span,div,label,p,td,th'))
                        .filter((el) => visible(el) && textOf(el) === label);
                    for (const labelEl of labels) {
                        let node = labelEl.parentElement;
                        for (let depth = 0; depth < 8 && node && node !== document.body; depth += 1) {
                            const rowText = textOf(node);
                            if (rowText.includes(label) && rowText.length <= 700) {
                                const nodes = Array.from(node.querySelectorAll('span,div,label,p,td,th,input,textarea'))
                                    .filter((el) => visible(el));
                                let passedLabel = false;
                                for (const item of nodes) {
                                    if (item === labelEl || item.contains(labelEl)) {
                                        passedLabel = true;
                                        continue;
                                    }
                                    if (!passedLabel) continue;
                                    const value = cleanValue(valueOf(item), label);
                                    if (value && !/^(详细地址|门牌号|邮编|地址类型|买家姓名|收件人|公司|电话|买家邮箱)$/.test(value)) {
                                        return value;
                                    }
                                }
                                const fallback = extractFromRowText(rowText, label);
                                if (fallback) return fallback;
                            }
                            node = node.parentElement;
                        }
                    }
                    return '';
                };
                return extractByLabel('收件地址') || '';
            }
            """
        )
        text = str(value or "").strip()
        if text:
            return text
    except Exception:
        pass

    candidates = page.locator("text=收货信息").locator("xpath=ancestor::*[self::div or self::section][1]")
    try:
        count = await candidates.count()
    except Exception:
        count = 0
    for index in range(count):
        node = candidates.nth(index)
        try:
            text = (await node.inner_text(timeout=1200)).strip()
        except Exception:
            continue
        address_line = extract_shipping_address_line(text)
        if address_line:
            return address_line
        if "收件人" in text or "收件地址" in text or "详细地址" in text:
            return text

    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""
    address_line = extract_shipping_address_line(body_text)
    if address_line:
        return address_line
    # 兜底只返回包含收货关键词附近的 DOM 文本，避免把整页日志写得太大。
    marker = body_text.find("收货信息")
    if marker >= 0:
        return body_text[marker : marker + 1800]
    return body_text[:1800]


async def read_list_shipping_deadline_text(page, system_order_no: str | None, platform_order_no: str | None = None) -> str:
    """从订单列表行读取发货时限文本；读取失败时返回空字符串，由 planner 提示人工备注。"""

    text = await _read_order_list_cell_by_header(
        page,
        system_order_no=system_order_no,
        platform_order_no=platform_order_no,
        header_text="发货时限",
    )
    if text:
        return text

    row = await _find_order_row(page, system_order_no=system_order_no, platform_order_no=platform_order_no)
    if row is None:
        return ""
    for selector in ('td[colid="col_29"]',):
        locator = row.locator(selector)
        try:
            if await locator.count():
                text = (await locator.first.inner_text(timeout=1000)).strip()
                if _looks_like_shipping_deadline(text):
                    return text
        except Exception:
            continue
    return ""


async def _read_order_list_cell_by_header(
    page,
    *,
    system_order_no: str | None,
    platform_order_no: str | None,
    header_text: str,
) -> str:
    """按订单列表表头定位同一行的单元格，适配 vxe 固定列拆分 DOM。"""

    try:
        value = await page.evaluate(
            """
            ({ systemOrderNo, platformOrderNo, headerText }) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const textOf = (el) => String(el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
                const cleanHeader = (text) => String(text || '').replace(/[↕↑↓⇅]+/g, '').replace(/\\s+/g, ' ').trim();
                const valueOf = (el) => {
                    const attrs = ['title', 'aria-label', 'data-title'];
                    for (const attr of attrs) {
                        const value = String(el?.getAttribute?.(attr) || '').replace(/\\s+/g, ' ').trim();
                        if (value) return value;
                    }
                    return textOf(el);
                };
                const rootOf = (el) => el.closest('.vxe-table,.el-table,.ant-table,table,[role="table"]') || document.body;
                const sameColumn = (a, b) => {
                    const aCol = a.getAttribute?.('colid') || a.getAttribute?.('data-colid') || a.dataset?.colid || '';
                    const bCol = b.getAttribute?.('colid') || b.getAttribute?.('data-colid') || b.dataset?.colid || '';
                    return aCol && bCol && aCol === bCol;
                };
                const rowMatchesOrder = (row) => {
                    const text = textOf(row);
                    const rowid = String(row.getAttribute?.('rowid') || row.dataset?.rowid || '');
                    return Boolean(
                        (systemOrderNo && (rowid === systemOrderNo || text.includes(systemOrderNo))) ||
                        (platformOrderNo && text.includes(platformOrderNo))
                    );
                };
                const overlapWidth = (a, b) => Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left));
                const targetHeader = Array.from(document.querySelectorAll([
                    'th',
                    '[role="columnheader"]',
                    '.vxe-header--column',
                    '.vxe-table--header-wrapper .vxe-cell',
                    '.el-table__header-wrapper th',
                    '.el-table__header-wrapper .cell',
                    '.ant-table-thead th',
                ].join(',')))
                    .filter(visible)
                    .map((el) => {
                        const cell = el.closest('th,[role="columnheader"],.vxe-header--column,.el-table__cell') || el;
                        return { el: cell, text: cleanHeader(textOf(cell) || textOf(el)), rect: cell.getBoundingClientRect() };
                    })
                    .filter((item) => item.text.includes(headerText) && item.rect.width > 0 && item.rect.height > 0)
                    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left)[0];
                if (!targetHeader) return '';

                const root = rootOf(targetHeader.el);
                const rows = Array.from(root.querySelectorAll('tbody tr,[role="row"],.vxe-body--row,.el-table__row'))
                    .filter(visible)
                    .filter(rowMatchesOrder);
                const bodyRows = rows.length ? rows : Array.from(document.querySelectorAll('tbody tr,[role="row"],.vxe-body--row,.el-table__row'))
                    .filter(visible)
                    .filter(rowMatchesOrder);
                for (const row of bodyRows) {
                    const cells = Array.from(row.querySelectorAll('td,[role="cell"],.vxe-body--column,.el-table__cell'))
                        .filter(visible);
                    const sameColCell = cells.find((cell) => sameColumn(targetHeader.el, cell));
                    if (sameColCell) {
                        const value = valueOf(sameColCell);
                        if (value) return value;
                    }
                    const headerRect = targetHeader.rect;
                    const headerCenter = (headerRect.left + headerRect.right) / 2;
                    const byX = cells
                        .map((cell) => {
                            const rect = cell.getBoundingClientRect();
                            const center = (rect.left + rect.right) / 2;
                            return { cell, rect, overlap: overlapWidth(headerRect, rect), distance: Math.abs(center - headerCenter) };
                        })
                        .filter((item) => item.rect.width > 0 && item.rect.height > 0 && (item.overlap > 0 || item.distance <= Math.max(8, headerRect.width / 2)))
                        .sort((a, b) => b.overlap - a.overlap || a.distance - b.distance)[0];
                    if (byX) {
                        const value = valueOf(byX.cell);
                        if (value) return value;
                    }
                }
                return '';
            }
            """,
            {
                "systemOrderNo": str(system_order_no or ""),
                "platformOrderNo": str(platform_order_no or ""),
                "headerText": header_text,
            },
        )
    except Exception:
        return ""
    text = str(value or "").strip()
    return text if _looks_like_shipping_deadline(text) else ""


def _looks_like_shipping_deadline(text: str | None) -> bool:
    """判断文本是否像订单发货截止日期。"""
    return bool(re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", str(text or "")))


async def execute_tent_sku_adjustment(page, plan: TentSkuAdjustmentPlan) -> TentSkuAdjustmentResult:
    """
    执行帐篷 SKU 调整。

    该模块只处理 Playwright DOM 操作，不理解文件夹命名规则；
    需要换货、添加哪些 SKU 由 tent_sku_planner 预先计算，保持页面操作和业务规则解耦。
    """

    actions: list[str] = []
    if plan.manual_required:
        return TentSkuAdjustmentResult(status="manual_required", actions=actions, error=plan.manual_reason)
    try:
        row = await _find_order_row(
            page,
            system_order_no=plan.system_order_no,
            platform_order_no=plan.platform_order_no,
        )
        if row is None:
            return TentSkuAdjustmentResult(status="sku_adjustment_row_not_found", error="无法定位当前订单列表行。")

        edit_dialog = await _open_product_edit_dialog(page, row)

        if plan.replace_main_items:
            for item in plan.replace_main_items:
                if not item.sku:
                    continue
                await _replace_main_product(page, edit_dialog, item.sku)
                edit_dialog = await _visible_dialog_by_header_title(page, "编辑商品", timeout_ms=5000)
                if item.quantity != 1:
                    await _set_product_quantity(edit_dialog, item.sku, item.quantity)
                actions.append(f"replace_main:{item.sku}x{item.quantity}")
            plan.replace_main_sku = None

        if plan.replace_main_sku:
            await _replace_main_product(page, edit_dialog, plan.replace_main_sku)
            edit_dialog = await _visible_dialog_by_header_title(page, "编辑商品", timeout_ms=5000)
            if plan.replace_main_quantity != 1:
                await _set_product_quantity(edit_dialog, plan.replace_main_sku, plan.replace_main_quantity)
            actions.append(f"replace_main:{plan.replace_main_sku}x{plan.replace_main_quantity}")

        for item in plan.add_items:
            sku_text = item.sku or ""
            print(f"正在添加 SKU：{sku_text} x{item.quantity}")
            try:
                edit_dialog = await _add_product(page, edit_dialog, item)
            except Exception as exc:
                raise RuntimeError(f"添加 SKU {sku_text} x{item.quantity} 失败：{exc}") from exc
            actions.append(f"add:{item.sku}x{item.quantity}")
            print(f"已添加 SKU：{sku_text} x{item.quantity}")

        await _confirm_product_edit_dialog(page, edit_dialog)
        return TentSkuAdjustmentResult(status="sku_adjustment_complete", actions=actions)
    except Exception as exc:
        await _cancel_visible_dialogs(page)
        return TentSkuAdjustmentResult(status="sku_adjustment_error", actions=actions, error=str(exc))


def _merge_instruction_customer_remark(existing_text: str | None, remark: str) -> tuple[str, str]:
    """合并说明书客服备注，保留其它既有备注。"""

    existing = str(existing_text or "").strip()
    if not remark:
        return existing, "skip"
    if remark in existing:
        return existing, "skip"
    if INSTRUCTION_CUSTOMER_REMARK_RE.search(existing):
        return INSTRUCTION_CUSTOMER_REMARK_RE.sub(remark, existing), "replace"
    if not existing:
        return remark, "append"
    return f"{remark}\n{existing.lstrip()}", "append"


async def _upsert_customer_remark(page, plan: TentSkuAdjustmentPlan) -> str:
    """打开当前订单客服备注编辑器，并追加或替换说明书备注。"""

    return await upsert_instruction_customer_remark(
        page,
        platform_order_no=plan.platform_order_no,
        system_order_no=plan.system_order_no,
        remark=plan.customer_remark or "",
    )


async def upsert_instruction_customer_remark(
    page,
    *,
    platform_order_no: str | None,
    system_order_no: str | None,
    remark: str,
) -> str:
    """打开指定订单行客服备注编辑器，并追加或替换说明书备注。"""

    remark = str(remark or "")
    button = await _find_customer_remark_edit_button(
        page,
        system_order_no=system_order_no,
        platform_order_no=platform_order_no,
    )
    await _click_customer_remark_edit_button(button)
    editor, input_locator = await _find_customer_remark_editor_input(page)
    existing = await _read_customer_remark_input_text(input_locator)
    next_text, action = _merge_instruction_customer_remark(existing, remark)
    if action == "skip":
        await _close_customer_remark_editor(page, editor)
        return action
    await input_locator.fill(next_text)
    await _confirm_customer_remark_editor(page, editor)
    return action


async def _find_customer_remark_edit_button(page, *, system_order_no: str | None, platform_order_no: str | None):
    """
    通过“客服备注”表头和当前订单行定位编辑图标。

    领星 vxe 表格会把一行拆成多个 DOM row；这里不用固定 colid，而是按可见表头位置匹配单元格。
    """

    handle = await page.evaluate_handle(
        """
        ({ systemOrderNo, platformOrderNo }) => {
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity || '1') !== 0;
            };
            const textOf = (el) => String(el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const unique = (items) => items.filter((item, index, all) => all.indexOf(item) === index);
            const rowSelectors = [
                'tbody tr',
                'tr.vxe-body--row',
                'tr.el-table__row',
                '[role="row"]',
                '.vxe-body--row',
                '.el-table__row',
            ].join(',');
            const cellSelectors = 'td,[role="gridcell"],.vxe-body--column,.el-table__cell';
            const headerSelectors = [
                'th',
                '[role="columnheader"]',
                '.vxe-header--column',
                '.vxe-table--header-wrapper .vxe-cell',
                '.el-table__header-wrapper th',
                '.el-table__header-wrapper .cell',
            ].join(',');
            const rowMatches = (row) => {
                const rowid = row.getAttribute('rowid') || row.getAttribute('data-rowid') || '';
                const text = textOf(row);
                if (systemOrderNo && (rowid === systemOrderNo || text.includes(systemOrderNo))) return true;
                if (platformOrderNo && text.includes(platformOrderNo)) return true;
                return false;
            };
            const rows = unique(Array.from(document.querySelectorAll(rowSelectors)).filter(visible).filter(rowMatches));
            const headers = unique(Array.from(document.querySelectorAll(headerSelectors))
                .map((el) => el.closest('th,[role="columnheader"],.vxe-header--column,.el-table__cell') || el)
                .filter(visible))
                .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
                .filter((item) => /客服备注/.test(item.text));
            const editFromCell = (cell) => {
                const selectors = [
                    'i.lx_edit',
                    'i.iconfont.lx_edit',
                    '[class*="edit-remark"] i',
                    '[class*="edit-remark"]',
                    'i[class*="edit"]',
                    '[class*="edit"]',
                    'button',
                    '[role="button"]',
                    'a',
                    'svg',
                ].join(',');
                const candidates = Array.from(cell.querySelectorAll(selectors));
                const visibleCandidate = candidates.find(visible);
                if (visibleCandidate) return visibleCandidate;
                return candidates.find((el) => /edit|lx_edit|remark|备注/i.test(String(el.className || '') + ' ' + textOf(el))) || null;
            };
            const cellsOf = (row) => unique(Array.from(row.querySelectorAll(cellSelectors)).filter(visible));
            let best = null;
            for (const row of rows) {
                const cells = cellsOf(row);
                for (const header of headers) {
                    const left = header.rect.left;
                    const right = header.rect.right;
                    const center = (left + right) / 2;
                    for (const cell of cells) {
                        const rect = cell.getBoundingClientRect();
                        const overlap = Math.max(0, Math.min(right, rect.right) - Math.max(left, rect.left));
                        const centerInside = rect.left <= center && rect.right >= center ? 1 : 0;
                        const distance = Math.abs(((rect.left + rect.right) / 2) - center);
                        const score = overlap * 10 + centerInside * 1000 - distance;
                        if ((overlap > 2 || centerInside) && (!best || score > best.score)) {
                            best = { cell, score };
                        }
                    }
                }
            }
            if (best?.cell) return editFromCell(best.cell) || best.cell;

            // 兜底：部分页面会把备注列写入 colid/title/class，而表头由于横向滚动不可见。
            for (const row of rows) {
                for (const cell of cellsOf(row)) {
                    const marker = [
                        cell.getAttribute('colid') || '',
                        cell.getAttribute('data-colid') || '',
                        cell.getAttribute('title') || '',
                        String(cell.className || ''),
                        textOf(cell),
                    ].join(' ');
                    if (/客服备注|customer.*remark|remark|备注/i.test(marker)) {
                        return editFromCell(cell) || cell;
                    }
                }
            }
            return null;
        }
        """,
        {"systemOrderNo": system_order_no or "", "platformOrderNo": platform_order_no or ""},
    )
    try:
        button = handle.as_element()
    except Exception:
        button = None
    if button is None:
        raise RuntimeError("无法定位当前订单行的客服备注编辑图标。")
    return button


async def _click_customer_remark_edit_button(button) -> None:
    """点击客户备注编辑按钮。"""
    last_error = ""
    for force in (False, True):
        try:
            try:
                await button.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            try:
                await button.hover(timeout=800)
            except Exception:
                pass
            await button.click(timeout=1200, force=force)
            return
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f"客服备注编辑图标点击失败：{last_error}")


async def _find_customer_remark_editor_input(page, *, timeout_ms: int = 5000):
    """查找客户备注编辑器输入框并返回匹配结果。"""
    attempts = max(1, timeout_ms // 200)
    for _ in range(attempts):
        for selector in (".el-dialog", ".vxe-modal--box", ".el-popover", ".el-popper"):
            containers = page.locator(selector)
            try:
                count = await containers.count()
            except Exception:
                count = 0
            for index in range(count - 1, -1, -1):
                container = containers.nth(index)
                try:
                    if not await container.is_visible():
                        continue
                except Exception:
                    continue
                input_locator = await _first_visible_customer_remark_input(container)
                if input_locator is not None:
                    return container, input_locator
        input_locator = await _first_visible_customer_remark_input(page)
        if input_locator is not None:
            return page.locator("body"), input_locator
        await page.wait_for_timeout(200)
    raise RuntimeError("打开客服备注编辑器后，没有找到可编辑的备注输入框。")


async def _first_visible_customer_remark_input(scope):
    """处理第一个可见 客户备注 输入框相关逻辑，并返回后续流程所需结果。"""
    for selector in (
        "textarea.el-textarea__inner",
        "textarea",
        "[contenteditable='true']",
        "input.el-input__inner:not([readonly]):not([disabled])",
        "input:not([readonly]):not([disabled])",
    ):
        locators = scope.locator(selector)
        try:
            count = await locators.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            locator = locators.nth(index)
            try:
                if await locator.is_visible():
                    return locator
            except Exception:
                continue
    return None


async def _read_customer_remark_input_text(input_locator) -> str:
    """读取客户备注输入框文本。"""
    try:
        return await input_locator.input_value(timeout=1200)
    except Exception:
        pass
    try:
        return await input_locator.inner_text(timeout=1200)
    except Exception:
        return ""


async def _confirm_customer_remark_editor(page, editor) -> None:
    """在命令行确认客户备注编辑器。"""
    for text in ("确定", "保存", "提交"):
        if await _click_visible_text_button(editor, text, timeout_ms=1800):
            await page.wait_for_timeout(500)
            return
    raise RuntimeError("客服备注已填写，但没有找到“确定/保存/提交”按钮。")


async def _close_customer_remark_editor(page, editor) -> None:
    """处理关闭 客户备注 编辑器相关逻辑，并返回后续流程所需结果。"""
    for text in ("取消", "关闭"):
        if await _click_visible_text_button(editor, text, timeout_ms=800):
            await page.wait_for_timeout(200)
            return
    for selector in (".el-dialog__headerbtn", ".el-icon-close", "[aria-label='Close']"):
        locators = editor.locator(selector)
        try:
            count = await locators.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            locator = locators.nth(index)
            try:
                if await locator.is_visible():
                    await locator.click(timeout=800)
                    await page.wait_for_timeout(200)
                    return
            except Exception:
                continue
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass


async def _click_visible_text_button(scope, text: str, *, timeout_ms: int) -> bool:
    """点击可见文本按钮。"""
    selectors = (
        f"button:has-text('{text}')",
        f"a:has-text('{text}')",
        f"span:has-text('{text}')",
        f"text={text}",
    )
    for selector in selectors:
        locators = scope.locator(selector)
        try:
            count = await locators.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            locator = locators.nth(index)
            try:
                if await locator.is_visible():
                    await locator.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
    return False


async def _find_order_row(page, *, system_order_no: str | None, platform_order_no: str | None):
    """查找订单行并返回匹配结果。"""
    if system_order_no:
        rows = page.locator(f'tr.vxe-body--row[rowid="{system_order_no}"]')
        try:
            row = await _pick_product_table_row_segment(rows, platform_order_no=platform_order_no)
            if row is not None:
                return row
        except Exception:
            pass
    if platform_order_no:
        rows = page.locator("tr.vxe-body--row, tr.el-table__row").filter(has_text=platform_order_no)
        try:
            row = await _pick_product_table_row_segment(rows, platform_order_no=platform_order_no)
            if row is not None:
                return row
        except Exception:
            pass
    return None


async def _pick_product_table_row_segment(rows, *, platform_order_no: str | None):
    """
    从领星拆分表格中挑出包含“SKU”列的那段行。

    领星的 vxe 表格会把同一订单行拆成左固定列、中间滚动列、右固定列多个 DOM row。
    同一个 rowid 下可能有多个 col_6：中间滚动段只是空占位，左固定段才有 SKU 文本和铅笔。
    因此不能只看 colid，必须优先选择实际含 SKU 文本或编辑入口的那段行。
    """

    try:
        count = await rows.count()
    except Exception:
        return None
    fallback = None
    sku_cell_fallback = None
    for index in range(count):
        row = rows.nth(index)
        try:
            if fallback is None and platform_order_no:
                text = await row.inner_text(timeout=800)
                if platform_order_no in text:
                    fallback = row

            sku_cells = row.locator('td[colid="col_6"]')
            if not await sku_cells.count():
                continue
            cell = sku_cells.first

            # 领星的 SKU 编辑图标默认隐藏，只有鼠标悬停在商品行/单元格上才渲染或显示。
            # 这里先触发 hover，再判断这一段行是否真的是可编辑的 SKU 段。
            try:
                await row.hover(timeout=800)
            except Exception:
                pass
            try:
                await cell.hover(timeout=800)
            except Exception:
                pass

            try:
                cell_text = (await cell.inner_text(timeout=800)).strip()
            except Exception:
                cell_text = ""
            edit_count = await cell.locator(
                '[class*="edit-remark"], [class*="lx_edit"], i[class*="edit"]'
            ).count()
            if cell_text or edit_count:
                return row
            if sku_cell_fallback is None:
                sku_cell_fallback = row
        except Exception:
            continue
    return sku_cell_fallback or fallback


async def _open_product_edit_dialog(page, row):
    """
    打开“编辑商品”弹窗并返回当前可见弹窗。

    领星列表里商品列和 SKU 列都可能显示铅笔入口；但同一行还有标签、备注等铅笔。
    因此这里只在商品列/SKU 列范围内找入口，并且点击后必须确认弹出的是“编辑商品”，
    避免误点到标签弹窗后继续执行换货/添加商品。
    """

    column_candidates = (
        ('td[colid="col_6"]', "SKU列"),
        ('td[colid="col_5"]', "商品列"),
    )
    # 这里必须只点击铅笔图标本身，不能点击 edit-remark 外层容器；
    # 外层容器同时包着 SKU 文本和“共1”，点到它会打开商品信息浮层，而不是编辑商品弹窗。
    # 每次误点都要短超时失败，否则多个候选叠加会让 CMD 等几分钟才报错。
    edit_selectors = (
        'i.lx_edit',
        'i.iconfont.lx_edit',
    )
    last_error = ""
    # 商品/SKU 列的编辑铅笔需要 hover 行后才出现，先悬停整行可以避免误判“找不到按钮”。
    try:
        await row.hover(timeout=1500)
    except Exception:
        pass
    for cell_selector, column_name in column_candidates:
        cell = row.locator(cell_selector).first
        try:
            if not await cell.count():
                continue
            # 部分铅笔只有 hover 后才可见；失败不阻断，后面仍会尝试 force click。
            try:
                await row.hover(timeout=1000)
            except Exception:
                pass
            try:
                await cell.hover(timeout=1500)
            except Exception:
                pass
            for edit_selector in edit_selectors:
                buttons = cell.locator(edit_selector)
                try:
                    button_count = await buttons.count()
                except Exception as exc:
                    last_error = f"{column_name} 查询 {edit_selector} 失败：{exc}"
                    continue
                for index in range(button_count):
                    button = buttons.nth(index)
                    for force in (False, True):
                        try:
                            try:
                                await button.scroll_into_view_if_needed(timeout=500)
                            except Exception:
                                pass
                            await button.click(timeout=900, force=force)
                            return await _visible_dialog(page, "编辑商品", timeout_ms=1000)
                        except Exception as exc:
                            last_error = f"{column_name} 点击 {edit_selector} 后未打开编辑商品弹窗：{exc}"
                            await _dismiss_quick_overlay(page)
                            continue
        except Exception as exc:
            last_error = f"{column_name} 定位失败：{exc}"
            continue
    detail = f"最后错误：{last_error}" if last_error else "商品列和 SKU 列都没有找到可点击的编辑入口。"
    raise RuntimeError(f"无法打开编辑商品弹窗，已停止以避免误点标签编辑。{detail}")


async def _visible_dialog(page, title: str | None = None, *, timeout_ms: int = 8000):
    """
    返回当前真正可见的 Element UI 弹窗。

    领星页面会保留隐藏的历史 .el-dialog；如果直接取 .last，可能一直等隐藏弹窗变可见。
    这里短轮询所有匹配弹窗，从后往前挑 visible 的那个，避免命中隐藏历史节点。
    """

    attempts = max(1, timeout_ms // 200)
    for _ in range(attempts):
        locator = page.locator(".el-dialog")
        if title:
            locator = locator.filter(has_text=title)
        try:
            count = await locator.count()
        except Exception:
            count = 0
        for index in range(count - 1, -1, -1):
            dialog = locator.nth(index)
            try:
                if await dialog.is_visible():
                    return dialog
            except Exception:
                continue
        await page.wait_for_timeout(200)
    title_text = title or "任意"
    raise RuntimeError(f"未找到可见的{title_text}弹窗。")


async def _visible_dialog_by_header_title(page, title: str, *, timeout_ms: int = 8000):
    """
    只按弹窗标题匹配当前可见弹窗。

    “编辑商品”弹窗正文里也有“添加商品”按钮，如果按整段文本匹配，
    会把底层编辑弹窗误判成“添加商品”弹窗，导致选择商品后一直等待错误弹窗关闭。
    """

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
                header = dialog.locator(".el-dialog__title").first
                header_text = ""
                if await header.count():
                    header_text = (await header.inner_text(timeout=300)).strip()
                aria_label = (await dialog.get_attribute("aria-label")) or ""
                if title in header_text or title in aria_label:
                    return dialog
            except Exception:
                continue
        await page.wait_for_timeout(200)
    raise RuntimeError(f"未找到标题为“{title}”的可见弹窗。")


async def _replace_main_product(page, edit_dialog, sku: str) -> None:
    """处理替换主产品相关逻辑，并返回后续流程所需结果。"""
    await _click_next_original_main_product_exchange_button(edit_dialog)
    await _search_and_replace_product(page, sku)


async def _click_next_original_main_product_exchange_button(edit_dialog) -> None:
    """点击下一条仍是原始帐篷主商品行的“换货”按钮。"""

    diagnostics: list[str] = []
    rows = edit_dialog.locator(".product-detail")
    try:
        row_count = await rows.count()
    except Exception:
        row_count = 0
    for index in range(row_count):
        row = rows.nth(index)
        try:
            if not await row.is_visible():
                continue
        except Exception:
            continue
        try:
            row_info = await row.evaluate(
                """
                (row) => {
                    const visible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const rowText = textOf(row);
                    const skuMatch = rowText.match(/SKU\\s*[:：]?\\s*([^\\s]+)(?=\\s*(?:换货|商品ID|ASIN|MSKU|平台单号|参考号|$))/i);
                    return {
                        rowText,
                        currentSku: skuMatch ? skuMatch[1] : '',
                        hasImage: !!row.querySelector('img'),
                        hasExchange: Array.from(row.querySelectorAll('button')).some(
                            (button) => visible(button) && textOf(button) === '换货'
                        ),
                    };
                }
                """
            )
        except Exception:
            row_info = {}
        current_sku = str(row_info.get("currentSku") or "").strip()
        row_text = str(row_info.get("rowText") or "").strip()
        if current_sku or row_text:
            diagnostics.append(current_sku or row_text[:80])
        if not row_info.get("hasImage") or not row_info.get("hasExchange"):
            continue
        if not _is_original_tent_main_sku(current_sku):
            continue
        buttons = row.locator("button:has-text('换货')")
        try:
            button_count = await buttons.count()
        except Exception:
            button_count = 0
        for button_index in range(button_count):
            button = buttons.nth(button_index)
            try:
                if await button.is_visible():
                    await button.click(timeout=5000)
                    return
            except Exception:
                continue

    fallback = edit_dialog.locator("button:has-text('换货')")
    try:
        fallback_count = await fallback.count()
    except Exception:
        fallback_count = 0
    if fallback_count == 1:
        await fallback.first.click(timeout=5000)
        return
    detail = f" 当前可见换货行 SKU：{'；'.join(diagnostics[:8])}" if diagnostics else ""
    raise RuntimeError(f"未找到仍是原始帐篷主 SKU 的可换货商品行。{detail}")


def _is_original_tent_main_sku(sku: str | None) -> bool:
    """判断编辑商品弹窗里的当前 SKU 是否仍是原始带图帐篷主 SKU。"""

    text = str(sku or "").strip().lower()
    if not text:
        return False
    return text.startswith("canopytents") or text.startswith("custom-tent-package")


async def _add_product(page, edit_dialog, item: TentSkuPlanAction):
    """在商品编辑弹窗中新增计划要求的 SKU。"""
    await _click_add_product_button(edit_dialog)
    await _search_and_add_product(page, item.sku or "")
    edit_dialog = await _visible_dialog_by_header_title(page, "编辑商品", timeout_ms=5000)
    if item.sku and item.quantity != 1:
        await _set_added_product_quantity(edit_dialog, item.sku, item.quantity)
    return edit_dialog


async def _click_add_product_button(edit_dialog) -> None:
    """点击商品编辑弹窗中的添加产品按钮。"""
    button = edit_dialog.locator("button:has-text('添加商品')").first
    try:
        if await button.count():
            await button.click(timeout=5000)
            return
    except Exception:
        pass

    text_button = edit_dialog.locator("text=添加商品").first
    if await text_button.count():
        await text_button.click(timeout=5000)
        return
    raise RuntimeError("无法在编辑商品弹窗中找到“添加商品”按钮。")


async def _search_and_replace_product(page, sku: str) -> None:
    """搜索目标 SKU 并替换主商品。"""
    if not sku:
        raise ValueError("SKU 不能为空。")
    dialog = await _visible_dialog_by_header_title(page, "选择产品", timeout_ms=2500)
    search_input = await _find_search_input(dialog)
    await search_input.fill(sku)
    await search_input.press("Enter")
    await page.wait_for_timeout(500)

    row = await _find_product_result_row(page, dialog, sku)
    # 换货弹窗必须点击右侧“选择”，不是勾选复选框；点击后只等待“选择产品”弹窗关闭。
    if await _click_choose_button(dialog, row):
        await _wait_dialog_hidden(dialog, "选择产品")
        return
    raise RuntimeError(f"已搜索到换货 SKU {sku}，但没有找到右侧可点击的“选择”按钮。")


async def _search_and_add_product(page, sku: str) -> None:
    """搜索目标 SKU 并添加到订单商品列表。"""
    if not sku:
        raise ValueError("SKU 不能为空。")
    dialog = await _visible_dialog_by_header_title(page, "添加商品", timeout_ms=2500)
    search_input = await _find_search_input(dialog)
    await search_input.fill(sku)
    await search_input.press("Enter")
    await page.wait_for_timeout(500)

    row = await _find_product_result_row(page, dialog, sku)
    # 添加商品弹窗走批量勾选逻辑：勾选目标行，再点击右下角“确定”。
    await _click_result_checkbox(dialog, row, sku)
    await _click_dialog_button(dialog, "确定", timeout_ms=1500)
    await _wait_dialog_hidden(dialog, "添加商品")


async def _find_product_result_row(page, dialog, sku: str):
    """查找产品结果行并返回匹配结果。"""
    row = await _find_product_result_row_by_exact_sku(page, dialog, sku)
    try:
        await row.hover(timeout=500)
    except Exception:
        pass
    return row


async def _find_product_result_row_by_exact_sku(page, dialog, sku: str):
    """查找产品结果行by精确SKU并返回匹配结果。"""
    rows = dialog.locator("tr, .vxe-body--row, .el-table__row")
    deadline_checks = 10
    for _ in range(deadline_checks):
        try:
            count = await rows.count()
        except Exception:
            count = 0
        for index in range(count):
            row = rows.nth(index)
            try:
                if not await row.is_visible():
                    continue
            except Exception:
                continue
            try:
                row_info = await row.evaluate(
                    """
                    (row) => {
                        const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        const cellTexts = Array.from(row.querySelectorAll('td, .vxe-body--column, .el-table__cell, [class*=cell]'))
                            .map(textOf)
                            .filter(Boolean);
                        const rowText = textOf(row);
                        const skuLabelMatch = rowText.match(/SKU\\s*[:：]?\\s*([^\\s]+(?:-[^\\s]+)*)/i);
                        const skuCell = cellTexts.find((text) => /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(text));
                        return {
                            rowText,
                            cellTexts,
                            skuFromLabel: skuLabelMatch ? skuLabelMatch[1] : null,
                            skuCell: skuCell || null,
                        };
                    }
                    """
                )
            except Exception:
                row_info = {}
            row_text = str(row_info.get("rowText") or "")
            sku_candidates = [
                str(row_info.get("skuFromLabel") or "").strip(),
                str(row_info.get("skuCell") or "").strip(),
            ]
            for text in row_info.get("cellTexts") or []:
                text = str(text or "").strip()
                if text:
                    sku_candidates.append(text)
            if sku in sku_candidates:
                return row
        try:
            await page.wait_for_timeout(250)
        except Exception:
            break
    raise RuntimeError(f"搜索商品后没有找到 SKU 精确等于 {sku} 的结果行。")


async def _click_choose_button(dialog, row) -> bool:
    """点击商品搜索结果中的选择按钮。"""
    selectors = (
        "button:has-text('选择')",
        "a:has-text('选择')",
        "span:has-text('选择')",
        "text=选择",
    )
    for scope in (row, dialog):
        for selector in selectors:
            choices = scope.locator(selector)
            try:
                count = await choices.count()
            except Exception:
                count = 0
            for index in range(count):
                choice = choices.nth(index)
                try:
                    if await choice.is_visible():
                        await choice.click(timeout=1200)
                        return True
                except Exception:
                    continue
    return False


async def _click_result_checkbox(dialog, row, sku: str) -> None:
    """点击结果复选框。"""
    scopes = [
        row,
        dialog.locator(".el-table__body-wrapper, .vxe-table--body-wrapper, tbody").first,
        dialog,
    ]
    selectors = (
        ".vxe-cell--checkbox",
        ".vxe-checkbox--input",
        ".vxe-checkbox--icon",
        ".vxe-checkbox",
        "label.el-checkbox",
        ".el-checkbox__input",
        "input[type='checkbox']",
    )
    for scope in scopes:
        for selector in selectors:
            checkboxes = scope.locator(selector)
            try:
                count = await checkboxes.count()
            except Exception:
                count = 0
            for index in range(count):
                checkbox = checkboxes.nth(index)
                try:
                    if await checkbox.is_visible():
                        await checkbox.click(timeout=1200)
                        return
                except Exception:
                    continue
    first_cell_selectors = (
        "td:first-child",
        ".vxe-body--column:first-child",
        ".el-table__cell:first-child",
    )
    for selector in first_cell_selectors:
        cells = row.locator(selector)
        try:
            count = await cells.count()
        except Exception:
            count = 0
        for index in range(count):
            cell = cells.nth(index)
            try:
                if await cell.is_visible():
                    await cell.click(timeout=1200)
                    return
            except Exception:
                continue
    try:
        clicked = await row.evaluate(
            """
            (row) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const selectors = [
                    '.vxe-cell--checkbox',
                    '.vxe-checkbox--input',
                    '.vxe-checkbox--icon',
                    '.vxe-checkbox',
                    'label.el-checkbox',
                    '.el-checkbox__input',
                    'input[type="checkbox"]',
                ];
                for (const selector of selectors) {
                    const target = Array.from(row.querySelectorAll(selector)).find(visible);
                    if (target) {
                        target.click();
                        return true;
                    }
                }
                const firstCell = Array.from(row.querySelectorAll('td, .vxe-body--column, .el-table__cell')).find(visible);
                if (firstCell) {
                    firstCell.click();
                    return true;
                }
                row.click();
                return true;
            }
            """
        )
        if clicked:
            return
    except Exception:
        pass
    raise RuntimeError(f"已搜索到 SKU {sku}，但没有找到可勾选的商品复选框。")


async def _wait_dialog_hidden(dialog, title: str, *, timeout_ms: int = 1800) -> None:
    """等待指定弹窗关闭。"""
    try:
        await dialog.wait_for(state="hidden", timeout=timeout_ms)
    except Exception as exc:
        raise RuntimeError(f"点击后“{title}”弹窗未关闭，请检查是否点到了正确按钮：{exc}")


async def _find_search_input(dialog):
    """查找搜索输入框并返回匹配结果。"""
    selectors = (
        "input[placeholder*='搜索']",
        "input[placeholder*='请输入']",
        ".el-input__inner",
    )
    for selector in selectors:
        locator = dialog.locator(selector).last
        try:
            if await locator.count():
                await locator.click(timeout=2000)
                return locator
        except Exception:
            continue
    raise RuntimeError("无法找到商品搜索输入框。")


async def _set_added_product_quantity(edit_dialog, sku: str, quantity: int) -> None:
    # 数量框在商品信息列内，不能用包含 SKU 的最深 div；那通常只是 SKU 文本区，没有输入框。
    """设置新增 SKU 的商品数量。"""
    await _set_product_quantity(edit_dialog, sku, quantity)


async def _set_product_quantity(edit_dialog, sku: str, quantity: int) -> None:
    """设置编辑商品弹窗中指定 SKU 行的商品数量。"""
    row = await _find_added_product_row(edit_dialog, sku)
    try:
        await row.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    quantity_input = await _find_quantity_input_in_product_row(row, sku)
    await quantity_input.fill(str(quantity))


async def _find_added_product_row(edit_dialog, sku: str):
    """查找已添加 SKU 对应的商品行。"""
    row = await _find_visible_added_product_row_once(edit_dialog, sku)
    if row is not None:
        return row

    scroll_containers = (
        ".vxe-table--body-wrapper.body--wrapper",
        ".vxe-table--body-wrapper",
        ".el-table__body-wrapper",
        ".el-dialog__body",
    )
    for selector in scroll_containers:
        containers = edit_dialog.locator(selector)
        try:
            container_count = await containers.count()
        except Exception:
            container_count = 0
        for container_index in range(container_count):
            container = containers.nth(container_index)
            try:
                if not await container.is_visible():
                    continue
            except Exception:
                continue
            try:
                await container.evaluate("(el) => { el.scrollTop = 0; }")
            except Exception:
                pass
            for _ in range(24):
                row = await _find_visible_added_product_row_once(edit_dialog, sku)
                if row is not None:
                    return row
                try:
                    state = await container.evaluate(
                        """
                        (el) => {
                            const oldTop = el.scrollTop;
                            const step = Math.max(120, Math.floor((el.clientHeight || 240) * 0.8));
                            el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
                            return {
                                oldTop,
                                newTop: el.scrollTop,
                                clientHeight: el.clientHeight,
                                scrollHeight: el.scrollHeight,
                            };
                        }
                        """
                    )
                except Exception:
                    break
                if int(state.get("newTop") or 0) == int(state.get("oldTop") or 0):
                    break
    row = await _find_visible_added_product_row_once(edit_dialog, sku)
    if row is not None:
        return row
    raise RuntimeError(f"已添加 SKU {sku}，但滚动商品列表后仍没有找到对应商品行。")


async def _find_visible_added_product_row_once(edit_dialog, sku: str):
    """单次查找当前可见的已添加 SKU 商品行。"""
    row_selectors = (
        "tr.vxe-body--row",
        "tr.el-table__row",
        ".vxe-body--row",
        ".el-table__row",
    )
    for selector in row_selectors:
        rows = edit_dialog.locator(selector).filter(has_text=sku)
        try:
            count = await rows.count()
        except Exception:
            count = 0
        for index in range(count):
            row = rows.nth(index)
            try:
                if await row.is_visible():
                    return row
            except Exception:
                continue
    return None


async def _find_quantity_input_in_product_row(row, sku: str):
    """查找数量输入框in产品行并返回匹配结果。"""
    selectors = (
        'td[colid="col_88"] .detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
        'td[colid="col_88"] .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
        '.detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
        '.tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
    )
    for selector in selectors:
        inputs = row.locator(selector)
        try:
            count = await inputs.count()
        except Exception:
            count = 0
        for index in range(count):
            quantity_input = inputs.nth(index)
            try:
                if await quantity_input.is_visible():
                    return quantity_input
            except Exception:
                continue

    try:
        input_handle = await row.evaluate_handle(
            """
            (row) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const candidates = Array.from(row.querySelectorAll('input.el-input__inner, input'))
                    .filter((input) =>
                        visible(input) &&
                        !input.readOnly &&
                        !input.disabled &&
                        input.closest('td[colid="col_88"], .detail-box-right, .product-detail')
                    );
                return candidates[0] || null;
            }
            """
        )
        if input_handle:
            return input_handle.as_element()
    except Exception:
        pass
    raise RuntimeError(f"已添加 SKU {sku}，但没有找到数量输入框。")


async def _confirm_product_edit_dialog(page, edit_dialog) -> None:
    """提交编辑商品弹窗并等待 ERP 完成保存和弹窗关闭。"""

    await _blur_active_element(page)
    confirm_button = await _find_product_edit_confirm_button(edit_dialog)
    await confirm_button.click(timeout=6000)
    await _wait_dialog_hidden(edit_dialog, "编辑商品", timeout_ms=12000)
    await _wait_after_product_edit_save(page)


async def _blur_active_element(page) -> None:
    """让当前输入框失焦，确保数量等行内编辑值写回前端表单状态。"""

    try:
        await page.evaluate(
            """
            () => {
                const active = document.activeElement;
                if (active && typeof active.blur === 'function') {
                    active.blur();
                }
            }
            """
        )
    except Exception:
        return


async def _find_product_edit_confirm_button(edit_dialog):
    """只从编辑商品弹窗底部区域查找提交用的“确定”按钮。"""

    footer_selectors = (
        ".el-dialog__footer",
        ".dialog-footer",
        ".vxe-modal--footer",
        "[class*='footer']",
    )
    footer_buttons = []
    for footer_selector in footer_selectors:
        buttons = edit_dialog.locator(f"{footer_selector} button").filter(has_text="确定")
        try:
            count = await buttons.count()
        except Exception:
            count = 0
        for index in range(count):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                text = (await button.inner_text(timeout=500)).strip()
                if text != "确定":
                    continue
                button_class = (await button.get_attribute("class")) or ""
                footer_buttons.append((button, button_class))
            except Exception:
                continue
    for button, button_class in footer_buttons:
        if "primary" in button_class or "el-button--primary" in button_class:
            return button
    if footer_buttons:
        return footer_buttons[-1][0]
    raise RuntimeError("编辑商品弹窗底部没有找到可点击的“确定”按钮。")


async def _wait_after_product_edit_save(page) -> None:
    """等待编辑商品保存后的页面请求和加载状态短暂稳定。"""

    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(1200)
    except Exception:
        pass


async def _click_dialog_button(dialog, text: str, *, timeout_ms: int = 6000) -> None:
    """点击弹窗按钮。"""
    button = dialog.locator(f"button:has-text('{text}')").last
    if not await button.count():
        button = dialog.locator(f"text={text}").last
    await button.click(timeout=timeout_ms)


async def _dismiss_quick_overlay(page) -> None:
    """快速收起误点产生的浮层或弹窗，避免一次错误点击拖慢整个 SKU 流程。"""

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await _cancel_visible_dialogs(page, click_timeout_ms=300)


async def _cancel_visible_dialogs(page, *, click_timeout_ms: int = 800) -> None:
    """取消可见dialogs并返回对应结果。"""
    for _ in range(4):
        dialogs = page.locator(".el-dialog")
        try:
            dialog_count = await dialogs.count()
        except Exception:
            dialog_count = 0
        visible_dialog_found = False
        for dialog_index in range(dialog_count - 1, -1, -1):
            dialog = dialogs.nth(dialog_index)
            try:
                if not await dialog.is_visible():
                    continue
            except Exception:
                continue
            visible_dialog_found = True
            clicked = False
            for selector in (".el-dialog__headerbtn", "button:has-text('取消')", "button:has-text('关闭')"):
                locators = dialog.locator(selector)
                try:
                    count = await locators.count()
                except Exception:
                    count = 0
                for index in range(count - 1, -1, -1):
                    locator = locators.nth(index)
                    try:
                        if await locator.is_visible():
                            await locator.click(timeout=click_timeout_ms)
                            clicked = True
                            break
                    except Exception:
                        continue
                if clicked:
                    break
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await page.wait_for_timeout(250)
        except Exception:
            pass
        if not visible_dialog_found:
            break
