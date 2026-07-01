from __future__ import annotations

from typing import Any

from ..parsers.orders import validate_search_snapshot
from .order_detail_navigation import close_order_detail_dialog


async def close_search_overlays(page) -> None:
    try:
        await page.keyboard.press("Escape")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(250)
    except Exception:
        pass

async def get_order_search_snapshot(page, search_input_index: int | None = None) -> dict[str, Any]:
    return await page.evaluate(
        """
        (searchInputIndex) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll('#advanced-input'))
                .filter(visible)
                .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
                .filter((item) => /平台单号|系统单号|平台订单号/.test(item.text) || item.el.querySelector('.lx_combo_search'))
                .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const root = roots.length ? roots[0].el : document.querySelector('#advanced-input');
            const dropdown = root ? root.querySelector('.el-input-group__prepend .el-select') : null;
            const searchInput = root ? root.querySelector('.search-input > input.el-input__inner') : null;
            const allInputs = Array.from(document.querySelectorAll('input'));
            const resolvedSearchInputIndex = searchInput ? allInputs.indexOf(searchInput) : searchInputIndex;
            const selectedLabel = (() => {
                if (!dropdown) return null;
                const text = textOf(dropdown);
                const match = text.match(/平台单号|系统单号|平台订单号/);
                return match ? match[0] : text || null;
            })();
            const inputs = Array.from(document.querySelectorAll('input')).map((el, index) => {
                const rect = el.getBoundingClientRect();
                const aroundParts = [
                    el.placeholder || '',
                    el.name || '',
                    el.id || '',
                    el.getAttribute('aria-label') || '',
                ];
                let node = el.parentElement;
                for (let i = 0; i < 3 && node; i += 1) {
                    aroundParts.push(textOf(node));
                    node = node.parentElement;
                }
                return {
                    index,
                    value: el.value || '',
                    placeholder: el.placeholder || '',
                    type: el.type || '',
                    around: aroundParts.join(' ').replace(/\\s+/g, ' ').trim().slice(0, 160),
                    visible: visible(el),
                    top: rect.top,
                    bottom: rect.bottom,
                    left: rect.left,
                    right: rect.right,
                    width: rect.width,
                    height: rect.height,
                    isSearchInput: index === resolvedSearchInputIndex,
                };
            });
            return {
                selectedLabel,
                dropdownRect: dropdown ? {
                    top: dropdown.getBoundingClientRect().top,
                    bottom: dropdown.getBoundingClientRect().bottom,
                    left: dropdown.getBoundingClientRect().left,
                    right: dropdown.getBoundingClientRect().right,
                } : null,
                hasAdvancedInput: Boolean(root),
                advancedInputCount: roots.length,
                searchInputIndex: resolvedSearchInputIndex >= 0 ? resolvedSearchInputIndex : null,
                inputs,
            };
        }
        """,
        search_input_index,
    )

async def select_order_search_type(page, search_kind: str) -> str:
    target_label = "系统单号" if search_kind == "system" else "平台单号"
    await close_search_overlays(page)
    snapshot = await get_order_search_snapshot(page)
    if not snapshot.get("hasAdvancedInput"):
        raise RuntimeError("没有找到订单号搜索控件 #advanced-input。")
    if snapshot.get("selectedLabel") == target_label:
        return target_label

    state = await page.evaluate(
        """
        (targetLabel) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll('#advanced-input'))
                .filter(visible)
                .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
                .filter((item) => /平台单号|系统单号|平台订单号/.test(item.text) || item.el.querySelector('.lx_combo_search'))
                .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const root = roots.length ? roots[0].el : document.querySelector('#advanced-input');
            const dropdown = root ? root.querySelector('.el-input-group__prepend .el-select') : null;
            if (!dropdown || !visible(dropdown)) {
                return { ok: false, reason: '没有找到平台单号/系统单号下拉筛选项。' };
            }
            const rect = dropdown.getBoundingClientRect();
            const currentText = textOf(dropdown);
            const current = (currentText.match(/平台单号|系统单号|平台订单号/) || [currentText])[0];
            dropdown.click();
            return {
                ok: true,
                selectedLabel: current,
                clicked: true,
                anchor: {
                    left: rect.left,
                    right: rect.right,
                    top: rect.top,
                    bottom: rect.bottom,
                },
            };
        }
        """,
        target_label,
    )
    if not state.get("ok"):
        raise RuntimeError(state.get("reason") or "没有找到订单号搜索类型下拉。")

    await page.wait_for_timeout(350)
    clicked = await page.evaluate(
        """
        ({ targetLabel, anchor }) => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const options = Array.from(document.querySelectorAll('li.el-select-dropdown__item'))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return { el, rect, text: textOf(el), disabled: /is-disabled/.test(String(el.className || '')) };
                })
                .filter((item) =>
                    !item.disabled &&
                    visible(item.el) &&
                    item.text === targetLabel &&
                    item.rect.left >= anchor.left - 60 &&
                    item.rect.left <= anchor.right + 160 &&
                    item.rect.top >= anchor.bottom - 10
                )
                .sort((a, b) => a.rect.top - b.rect.top || Math.abs(a.rect.left - anchor.left) - Math.abs(b.rect.left - anchor.left));
            if (!options.length) return false;
            options[0].el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
            options[0].el.click();
            return true;
        }
        """,
        {"targetLabel": target_label, "anchor": state.get("anchor")},
    )
    if not clicked:
        raise RuntimeError(f"没有在红框下拉菜单中找到 {target_label}。")
    await page.wait_for_timeout(500)

    snapshot = await get_order_search_snapshot(page)
    selected_label = snapshot.get("selectedLabel")
    if selected_label != target_label:
        raise RuntimeError(f"搜索类型切换失败：期望 {target_label}，当前 {selected_label or '未知'}。")
    return target_label

async def find_order_search_input_index(page) -> int:
    snapshot = await get_order_search_snapshot(page)
    index = snapshot.get("searchInputIndex")
    if isinstance(index, int) and index >= 0:
        return index
    raise RuntimeError("没有找到红框中平台/系统单号下拉右侧的订单号输入框。")

async def click_order_search_button(page, search_input_index: int) -> bool:
    return bool(
        await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll('#advanced-input'))
                    .filter(visible)
                    .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
                    .filter((item) => /平台单号|系统单号|平台订单号/.test(item.text) || item.el.querySelector('.lx_combo_search'))
                    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
                const root = roots.length ? roots[0].el : document.querySelector('#advanced-input');
                const icon = root ? root.querySelector('.lx_combo_search') : null;
                if (!icon || !visible(icon)) return false;
                icon.click();
                return true;
            }
            """,
        )
    )

async def fill_order_search(page, order_no: str, search_kind: str) -> dict[str, Any]:
    await close_order_detail_dialog(page)
    await close_search_overlays(page)
    selected_label = await select_order_search_type(page, search_kind)
    search_input_index = await find_order_search_input_index(page)
    filled = await page.evaluate(
        """
        ({ searchInputIndex, orderNo }) => {
            const input = Array.from(document.querySelectorAll('input'))[searchInputIndex];
            if (!input) return false;
            const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
            const setValue = (value) => {
                if (valueSetter) valueSetter.call(input, value);
                else input.value = value;
            };
            input.click();
            input.focus();
            setValue('');
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
            setValue(orderNo);
            input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: orderNo }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }
        """,
        {"searchInputIndex": search_input_index, "orderNo": order_no},
    )
    if not filled:
        raise RuntimeError("没有找到红框中平台/系统单号下拉右侧的订单号输入框。")
    await page.wait_for_timeout(100)
    snapshot = await get_order_search_snapshot(page, search_input_index)
    search_value = next(
        (str(item.get("value") or "") for item in snapshot.get("inputs", []) if item.get("index") == search_input_index),
        None,
    )
    if search_value != order_no:
        search_input = page.locator("input").nth(search_input_index)
        await search_input.click()
        await search_input.fill(order_no)
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
        (str(item.get("value") or "") for item in snapshot.get("inputs", []) if item.get("index") == search_input_index),
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
    if not await click_order_search_button(page, search_input_index):
        raise RuntimeError("没有找到订单号输入框右侧的搜索按钮。")
    await page.wait_for_timeout(1800)
    return {
        "selected_search_type": selected_label,
        "search_input_value": search_value,
        "search_validation_message": message,
        "search_input_index": search_input_index,
        "search_validation_ok": True,
    }
