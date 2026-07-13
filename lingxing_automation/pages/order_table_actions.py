from __future__ import annotations

import time
from typing import Any

from .order_search import fill_order_search


_CASCADER_SCROLL_MAX_ATTEMPTS = 40


async def switch_order_tab(page, tab_text: str) -> None:
    """Switch the order-management main tab by visible text."""

    clicked = await page.evaluate(
        """
        (tabText) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const tabs = Array.from(document.querySelectorAll('.el-tabs__item'))
                .filter((el) => visible(el) && textOf(el).startsWith(tabText))
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            if (!tabs.length) return false;
            tabs[0].click();
            return true;
        }
        """,
        tab_text,
    )
    if not clicked:
        raise RuntimeError(f"没有找到订单页标签：{tab_text}")
    await page.wait_for_timeout(1000)


async def reset_order_filters(page) -> None:
    """Reset the visible order-list filters before taking a complete snapshot."""

    reset = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const buttons = Array.from(document.querySelectorAll('button'))
                .filter((el) => visible(el) && textOf(el) === '重置')
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            if (!buttons.length) return false;
            buttons[0].click();
            return true;
        }
        """
    )
    if not reset:
        raise RuntimeError("没有找到订单列表重置按钮。")
    await page.wait_for_timeout(1200)


async def read_order_table_total_count(page) -> int | None:
    """Read the current order-list total from the visible pager."""

    value = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll('.el-pagination,.vxe-pager,[class*="pagination"],[class*="Pagination"]'))
                .filter((el) => visible(el) && /共\\s*\\d+\\s*条/.test(textOf(el)))
                .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
            for (const root of roots) {
                const match = textOf(root).match(/共\\s*(\\d+)\\s*条/);
                if (match) return Number(match[1]);
            }
            return null;
        }
        """
    )
    return int(value) if value is not None else None


async def search_platform_order(page, platform_order_no: str) -> None:
    result = await fill_order_search(page, platform_order_no, "platform")
    if not result.get("search_validation_ok"):
        raise RuntimeError(result.get("search_validation_message") or "平台单号搜索失败。")


async def wait_for_order_row(
    page,
    *,
    system_order_no: str,
    platform_order_no: str,
    timeout_sec: int = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        row = await find_order_row(page, system_order_no=system_order_no, platform_order_no=platform_order_no)
        if row.get("found"):
            return row
        await page.wait_for_timeout(800)
    raise RuntimeError(f"没有在列表中找到系统单号 {system_order_no} / 平台单号 {platform_order_no}。")


async def find_order_row(page, *, system_order_no: str, platform_order_no: str) -> dict[str, Any]:
    """Find and merge visible VXE row fragments for a system/platform order."""

    return await page.evaluate(
        """
        ({ systemOrderNo, platformOrderNo }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const rows = Array.from(document.querySelectorAll('tr[rowid], .vxe-body--row[rowid]'))
                .filter(visible)
                .map((el) => ({ el, rowid: el.getAttribute('rowid') || '', text: textOf(el), rect: el.getBoundingClientRect() }));
            const grouped = new Map();
            for (const item of rows) {
                const key = item.rowid || '';
                if (!key) continue;
                if (!grouped.has(key)) grouped.set(key, []);
                grouped.get(key).push(item);
            }
            const matches = [];
            for (const [rowid, fragments] of grouped.entries()) {
                const combinedText = fragments.map((item) => item.text).join(' ');
                if (systemOrderNo && rowid !== systemOrderNo && !combinedText.includes(systemOrderNo)) continue;
                if (platformOrderNo && !combinedText.includes(platformOrderNo)) continue;
                const top = Math.min(...fragments.map((item) => item.rect.top));
                matches.push({ rowid, text: combinedText, top, fragmentCount: fragments.length });
            }
            matches.sort((a, b) => a.top - b.top);
            if (!matches.length) {
                return { found: false, rowid: systemOrderNo || '', text: '', matches: [] };
            }
            return { found: true, ...matches[0], matches };
        }
        """,
        {"systemOrderNo": system_order_no, "platformOrderNo": platform_order_no},
    )


async def select_order_row(page, rowid: str) -> None:
    selected = await page.evaluate(
        """
        (rowid) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const iconVisible = (cell, className) => {
                const icon = cell.querySelector(className);
                if (!icon) return false;
                return visible(icon);
            };
            const cells = Array.from(document.querySelectorAll(`tr[rowid="${rowid}"] td[colid="col_2"]`))
                .filter(visible)
                .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
            if (!cells.length) return { ok: false, reason: '没有找到可见勾选格。' };
            const cell = cells[0];
            if (iconVisible(cell, '.vxe-checkbox--checked-icon')) return { ok: true, alreadySelected: true };
            const target = cell.querySelector('.vxe-cell--checkbox') || cell;
            target.click();
            return { ok: true, alreadySelected: false };
        }
        """,
        rowid,
    )
    if not selected.get("ok"):
        raise RuntimeError(selected.get("reason") or f"勾选系统单号 {rowid} 失败。")
    await page.wait_for_timeout(350)
    if not await is_order_row_selected(page, rowid):
        raise RuntimeError(f"系统单号 {rowid} 勾选后未检测到已选状态。")


async def is_order_row_selected(page, rowid: str) -> bool:
    return bool(
        await page.evaluate(
            """
            (rowid) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const cells = Array.from(document.querySelectorAll(`tr[rowid="${rowid}"] td[colid="col_2"]`)).filter(visible);
                return cells.some((cell) => {
                    const icon = cell.querySelector('.vxe-checkbox--checked-icon');
                    return icon && visible(icon);
                });
            }
            """,
            rowid,
        )
    )


async def open_row_operation_menu(page, rowid: str) -> None:
    opened = await page.evaluate(
        """
        (rowid) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const rows = Array.from(document.querySelectorAll(`tr[rowid="${rowid}"]`)).filter(visible);
            const buttons = [];
            for (const row of rows) {
                buttons.push(...Array.from(row.querySelectorAll('button, .el-button')).filter((el) => visible(el) && textOf(el) === '操作'));
            }
            if (!buttons.length) return false;
            buttons.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
            buttons[0].click();
            return true;
        }
        """,
        rowid,
    )
    if not opened:
        raise RuntimeError(f"没有找到系统单号 {rowid} 的行操作按钮。")
    await page.wait_for_timeout(450)


async def click_visible_menu_item(page, item_text: str) -> None:
    clicked = await page.evaluate(
        """
        (itemText) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const items = Array.from(document.querySelectorAll('.ak-button-group-popover .ak-dropdown-item, .el-dropdown-menu__item, [role="menuitem"]'))
                .filter((el) => visible(el) && textOf(el) === itemText)
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            if (!items.length) return false;
            items[0].click();
            return true;
        }
        """,
        item_text,
    )
    if not clicked:
        raise RuntimeError(f"没有找到菜单项：{item_text}")
    await page.wait_for_timeout(700)


async def click_toolbar_button(page, button_text: str) -> None:
    clicked = await page.evaluate(
        """
        (buttonText) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const buttons = Array.from(document.querySelectorAll('button, .el-button'))
                .filter((el) => visible(el) && textOf(el) === buttonText)
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            if (!buttons.length) return false;
            buttons[0].click();
            return true;
        }
        """,
        button_text,
    )
    if not clicked:
        raise RuntimeError(f"没有找到工具栏按钮：{button_text}")
    await page.wait_for_timeout(500)


async def click_dialog_button(page, dialog_text: str, button_text: str) -> None:
    clicked = await page.evaluate(
        """
        ({ dialogText, buttonText }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const compact = (text) => String(text || '').replace(/\\s+/g, '');
            const dialogs = Array.from(document.querySelectorAll('.el-dialog, .el-message-box, [role="dialog"]'))
                .filter((el) => visible(el) && textOf(el).includes(dialogText))
                .sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
            if (!dialogs.length) return false;
            const targetText = compact(buttonText);
            const buttons = Array.from(dialogs[0].querySelectorAll('button, .el-button'))
                .filter((el) => visible(el) && compact(textOf(el)) === targetText);
            if (!buttons.length) return false;
            buttons[0].click();
            return true;
        }
        """,
        {"dialogText": dialog_text, "buttonText": button_text},
    )
    if not clicked:
        raise RuntimeError(f"没有在弹窗 {dialog_text} 中找到按钮：{button_text}")
    await page.wait_for_timeout(700)


async def dismiss_result_dialog(page, timeout_sec: int = 10) -> bool:
    """Dismiss post-action result dialogs such as success/info prompts."""

    deadline = time.monotonic() + timeout_sec
    button_texts = ["我知道了", "知道了", "确定", "关闭"]
    while time.monotonic() < deadline:
        result = await page.evaluate(
            """
            (buttonTexts) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const compact = (text) => String(text || '').replace(/\\s+/g, '');
                const ackTexts = buttonTexts.map(compact);
                const allButtons = Array.from(document.querySelectorAll('button, .el-button, [role="button"], a'))
                    .filter((el) => visible(el))
                    .map((el) => ({ el, text: compact(textOf(el)), rect: el.getBoundingClientRect() }));
                const exactKnownButton = allButtons
                    .filter((item) => item.text === compact('我知道了'))
                    .sort((a, b) => Math.abs((a.rect.left + a.rect.right) / 2 - window.innerWidth / 2) -
                        Math.abs((b.rect.left + b.rect.right) / 2 - window.innerWidth / 2))[0];
                if (exactKnownButton) {
                    exactKnownButton.el.click();
                    return { found: true, clicked: true, method: 'global-button' };
                }
                const resultPattern = /(成功|完成|处理中|审核中|正在审核|已提交|出库)/;
                const dialogs = Array.from(document.querySelectorAll(
                    '.el-message-box, .el-dialog, .ant-modal, .next-dialog, .modal, .ak-modal, [role="dialog"]'
                ))
                    .filter((el) => visible(el))
                    .map((el) => ({ el, text: textOf(el), rect: el.getBoundingClientRect() }))
                    .filter((item) => resultPattern.test(item.text) || ackTexts.some((text) => compact(item.text).includes(text)))
                    .sort((a, b) => b.rect.top - a.rect.top);
                if (!dialogs.length) return { found: false, clicked: false };
                const dialog = dialogs[0].el;
                const buttons = Array.from(dialog.querySelectorAll('button, .el-button, [role="button"], a')).filter(visible);
                for (const targetText of ackTexts) {
                    const button = buttons.find((el) => compact(textOf(el)) === targetText);
                    if (button) {
                        button.click();
                        return { found: true, clicked: true, method: 'button' };
                    }
                }
                const closeButton = Array.from(
                    dialog.querySelectorAll('.el-message-box__close, .el-dialog__headerbtn, [aria-label="Close"]')
                ).find(visible);
                if (closeButton) {
                    closeButton.click();
                    return { found: true, clicked: true, method: 'close' };
                }
                return { found: true, clicked: false };
            }
            """,
            button_texts,
        )
        if result.get("clicked"):
            await page.wait_for_timeout(500)
            return True
        await page.wait_for_timeout(300)
    return False


async def fill_dialog_form(page, dialog_text: str, values_by_label: dict[str, str]) -> None:
    result = await page.evaluate(
        """
        ({ dialogText, valuesByLabel }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const setInputValue = (input, value) => {
                const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                if (valueSetter) valueSetter.call(input, value);
                else input.value = value;
                input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            };
            const dialogs = Array.from(document.querySelectorAll('.el-dialog, [role="dialog"]'))
                .filter((el) => visible(el) && textOf(el).includes(dialogText));
            if (!dialogs.length) return { ok: false, reason: `没有找到弹窗：${dialogText}` };
            const dialog = dialogs[dialogs.length - 1];
            const missing = [];
            for (const [labelText, value] of Object.entries(valuesByLabel)) {
                const items = Array.from(dialog.querySelectorAll('.el-form-item'))
                    .filter((item) => visible(item) && textOf(item.querySelector('.el-form-item__label') || item).includes(labelText));
                if (!items.length) {
                    missing.push(labelText);
                    continue;
                }
                const input = Array.from(items[0].querySelectorAll('input.el-input__inner, input'))
                    .find((el) => visible(el) && !el.readOnly && !el.disabled);
                if (!input) {
                    missing.push(labelText);
                    continue;
                }
                input.focus();
                setInputValue(input, value);
            }
            return { ok: missing.length === 0, missing };
        }
        """,
        {"dialogText": dialog_text, "valuesByLabel": values_by_label},
    )
    if not result.get("ok"):
        missing = ", ".join(result.get("missing") or [])
        raise RuntimeError(result.get("reason") or f"弹窗表单字段填写失败：{missing}")
    await page.wait_for_timeout(300)


async def select_cascader_path(page, dialog_text: str, form_label: str, path: list[str]) -> None:
    opened = await page.evaluate(
        """
        ({ dialogText, formLabel }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const findInputNearLabel = (dialog, labelText) => {
                const labels = Array.from(dialog.querySelectorAll('*'))
                    .filter((el) => visible(el) && textOf(el) === labelText)
                    .map((el) => ({ el, rect: el.getBoundingClientRect() }));
                const inputs = Array.from(dialog.querySelectorAll('input.el-input__inner, input'))
                    .filter(visible)
                    .map((el) => ({ el, rect: el.getBoundingClientRect() }));
                for (const label of labels) {
                    const candidates = inputs
                        .filter((item) =>
                            item.rect.left >= label.rect.left &&
                            Math.abs((item.rect.top + item.rect.bottom) / 2 - (label.rect.top + label.rect.bottom) / 2) < 45
                        )
                        .sort((a, b) => Math.abs(a.rect.left - label.rect.right) - Math.abs(b.rect.left - label.rect.right));
                    if (candidates.length) return candidates[0].el;
                }
                return null;
            };
            const dialogs = Array.from(document.querySelectorAll('.el-dialog, [role="dialog"]'))
                .filter((el) => visible(el) && textOf(el).includes(dialogText));
            if (!dialogs.length) return false;
            const dialog = dialogs[dialogs.length - 1];
            const item = Array.from(dialog.querySelectorAll('.el-form-item'))
                .find((el) => visible(el) && textOf(el.querySelector('.el-form-item__label') || el).includes(formLabel));
            const input = item
                ? Array.from(item.querySelectorAll('input.el-input__inner, input')).find(visible)
                : findInputNearLabel(dialog, formLabel);
            const target = input || item;
            if (!target) return false;
            target.click();
            return true;
        }
        """,
        {"dialogText": dialog_text, "formLabel": form_label},
    )
    if not opened:
        raise RuntimeError(f"没有找到弹窗 {dialog_text} 的字段：{form_label}")
    await page.wait_for_timeout(500)

    for level, value in enumerate(path):
        scanned_labels: set[str] = set()
        clicked = False
        menu_reset = False
        last_result: dict[str, Any] = {}
        for attempt in range(_CASCADER_SCROLL_MAX_ATTEMPTS):
            last_result = await page.evaluate(
                """
                ({ level, value, reset }) => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const menus = Array.from(document.querySelectorAll('.el-cascader-menu'))
                        .filter(visible)
                        .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                    const menu = menus[level];
                    if (!menu) return { clicked: false, canContinue: true, reason: 'menu_missing', labels: [] };

                    const scrollCandidates = Array.from(menu.querySelectorAll(
                        '.el-cascader-menu__wrap, .el-scrollbar__wrap'
                    ));
                    const scroller = scrollCandidates.find((el) => el.scrollHeight > el.clientHeight) ||
                        scrollCandidates[0] || menu;
                    const dispatchScroll = () => scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
                    if (reset) {
                        scroller.scrollTop = 0;
                        dispatchScroll();
                        return {
                            clicked: false,
                            canContinue: true,
                            reset: true,
                            labels: [],
                            scrollTop: scroller.scrollTop,
                            scrollHeight: scroller.scrollHeight,
                            clientHeight: scroller.clientHeight,
                        };
                    }

                    const nodes = Array.from(menu.querySelectorAll('.el-cascader-node'));
                    const labels = nodes.map((el) =>
                        textOf(el.querySelector('.el-cascader-node__label') || el)
                    ).filter(Boolean);
                    const target = nodes.find((el) =>
                        textOf(el.querySelector('.el-cascader-node__label') || el) === value
                    );
                    if (target) {
                        target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                        target.click();
                        return {
                            clicked: true,
                            canContinue: false,
                            labels,
                            scrollTop: scroller.scrollTop,
                            scrollHeight: scroller.scrollHeight,
                            clientHeight: scroller.clientHeight,
                        };
                    }

                    const previousTop = scroller.scrollTop;
                    const step = Math.max(80, Math.floor(scroller.clientHeight * 0.75));
                    const maxTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
                    scroller.scrollTop = Math.min(maxTop, previousTop + step);
                    dispatchScroll();
                    return {
                        clicked: false,
                        canContinue: scroller.scrollTop > previousTop,
                        labels,
                        scrollTop: scroller.scrollTop,
                        scrollHeight: scroller.scrollHeight,
                        clientHeight: scroller.clientHeight,
                    };
                }
                """,
                {"level": level, "value": value, "reset": not menu_reset},
            )
            if last_result.get("reset"):
                menu_reset = True
            scanned_labels.update(last_result.get("labels") or [])
            if last_result.get("clicked"):
                clicked = True
                break
            if not last_result.get("canContinue"):
                break
            await page.wait_for_timeout(120)

        if not clicked:
            scanned = "、".join(sorted(scanned_labels)) or "无"
            reason = (
                "尚未加载" if last_result.get("reason") == "menu_missing" else "已滚动到底"
            )
            raise RuntimeError(
                f"没有找到级联选项：{' > '.join(path[: level + 1])}；"
                f"第 {level + 1} 列{reason}，扫描到：{scanned}"
            )
        await page.wait_for_timeout(450)


async def wait_for_dialog(page, dialog_text: str, timeout_sec: int = 15) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        found = await page.evaluate(
            """
            (dialogText) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                return Array.from(document.querySelectorAll('.el-dialog, .el-message-box, [role="dialog"]'))
                    .some((el) => visible(el) && textOf(el).includes(dialogText));
            }
            """,
            dialog_text,
        )
        if found:
            return
        await page.wait_for_timeout(500)
    raise RuntimeError(f"等待弹窗超时：{dialog_text}")
