from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..models import ContactInfo
from ..parsers.contact import normalize_phone
from .order_detail_navigation import (
    assert_current_detail_order,
    close_order_detail_dialog,
    click_system_order,
    wait_for_detail,
)

WriteConfirmCallback = Callable[[dict[str, Any]], Awaitable[bool]]


async def has_editable_contact_controls(page) -> bool:
    """判断详情页是否已经出现可编辑的联系方式控件。"""
    return bool(
        await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && rect.top > 150 &&
                        style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && !el.readOnly;
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const findShippingRoot = () => {
                    const headings = Array.from(document.querySelectorAll('span,div,p,section'))
                        .filter((el) => visible(el) && textOf(el) === '收货信息');
                    const candidates = [];
                    for (const heading of headings) {
                        let node = heading.parentElement;
                        for (let i = 0; i < 8 && node && node !== document.body; i += 1) {
                            const text = textOf(node);
                            if (/收货信息/.test(text) && /电话/.test(text) && /买家邮箱/.test(text)) {
                                const rect = node.getBoundingClientRect();
                                candidates.push({ el: node, area: rect.width * rect.height, textLength: text.length });
                            }
                            node = node.parentElement;
                        }
                    }
                    candidates.sort((a, b) => a.textLength - b.textLength || a.area - b.area);
                    return candidates[0]?.el || null;
                };
                const root = findShippingRoot();
                if (!root) return false;
                const controls = Array.from(root.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]'))
                    .filter(visible);
                const hasPhone = controls.some((el) => {
                    const rect = el.getBoundingClientRect();
                    return Array.from(root.querySelectorAll('span,div,label,p')).some((label) => {
                        if (!visible(label)) return false;
                        const labelRect = label.getBoundingClientRect();
                        const labelText = textOf(label);
                        return /^电话\\*?$/.test(labelText) &&
                            rect.left >= labelRect.right - 10 &&
                            rect.top <= labelRect.bottom + 14 &&
                            rect.bottom >= labelRect.top - 14;
                    });
                });
                const hasEmail = controls.some((el) => {
                    const rect = el.getBoundingClientRect();
                    return Array.from(root.querySelectorAll('span,div,label,p')).some((label) => {
                        if (!visible(label)) return false;
                        const labelRect = label.getBoundingClientRect();
                        const labelText = textOf(label);
                        return /^买家邮箱\\*?$/.test(labelText) &&
                            rect.left >= labelRect.right - 10 &&
                            rect.top <= labelRect.bottom + 14 &&
                            rect.bottom >= labelRect.top - 14;
                    });
                });
                return hasPhone || hasEmail;
            }
            """
        )
    )

async def try_open_edit_mode(page) -> None:
    """尝试打开订单详情页编辑模式。"""
    if await has_editable_contact_controls(page):
        return
    clicked_tab_row_edit = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.top >= 100 && rect.top <= window.innerHeight - 80 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const centerY = (rect) => (rect.top + rect.bottom) / 2;
            const nodes = Array.from(document.querySelectorAll('button,a,span,div,li'));
            const exactNodes = (label) => nodes
                .filter((el) => visible(el) && textOf(el) === label)
                .map((el) => ({ el, rect: el.getBoundingClientRect() }));
            const basicTabs = exactNodes('基本信息')
                .filter((item) =>
                    exactNodes('报关信息').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left) &&
                    exactNodes('操作日志').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left)
                )
                .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const tab = basicTabs[0];
            if (!tab) return false;
            const tabY = centerY(tab.rect);
            const editCandidates = nodes
                .filter((el) => visible(el) && /^(编辑|修改)$/.test(textOf(el)))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const target = el.closest('button,a') || el;
                    return { el: target, rect, text: textOf(el) };
                })
                .filter((item) =>
                    Math.abs(centerY(item.rect) - tabY) <= 34 &&
                    item.rect.left > tab.rect.left + 360
                )
                .sort((a, b) => b.rect.left - a.rect.left);
            const button = editCandidates[0]?.el;
            if (!button) return false;
            button.click();
            return true;
        }
        """
    )
    if clicked_tab_row_edit:
        await page.wait_for_timeout(1500)
        if await has_editable_contact_controls(page):
            return

    clicked_basic_info_edit = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const findBasicInfoPanel = () => {
                const candidates = Array.from(document.querySelectorAll('div,section,article'))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = textOf(el);
                        return { el, rect, text, area: rect.width * rect.height };
                    })
                    .filter((item) =>
                        item.rect.top >= 160 &&
                        item.rect.top <= 760 &&
                        item.rect.width >= 620 &&
                        item.rect.height >= 140 &&
                        item.text.includes('基本信息') &&
                        item.text.includes('报关信息') &&
                        item.text.includes('操作日志') &&
                        item.text.includes('收货信息')
                    )
                    .sort((a, b) => a.text.length - b.text.length || a.area - b.area);
                return candidates[0] || null;
            };
                const panel = findBasicInfoPanel();
                if (!panel) return false;
                const buttons = Array.from(panel.el.querySelectorAll('button,a,span,div'))
                .filter((el) => visible(el) && /^(编辑|修改)$/.test(textOf(el)))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return { el, rect };
                })
                .filter((item) =>
                    item.rect.top >= panel.rect.top - 4 &&
                    item.rect.top <= panel.rect.top + 72 &&
                    item.rect.left >= panel.rect.right - 180 &&
                    item.rect.left <= panel.rect.right + 8
                )
                .sort((a, b) => a.rect.top - b.rect.top || b.rect.left - a.rect.left);
            const button = buttons[0]?.el;
            if (!button) return false;
            button.click();
            return true;
        }
        """
    )
    if clicked_basic_info_edit:
        await page.wait_for_timeout(1200)
        if await has_editable_contact_controls(page):
            return

    for label in ["编辑收货信息", "修改收货信息", "编辑订单", "修改订单", "编辑", "修改"]:
        clicked = await page.evaluate(
            """
            (label) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const nodes = Array.from(document.querySelectorAll('button,a,span,div'))
                    .filter((el) => visible(el) && textOf(el) === label)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        let node = el.parentElement;
                        let around = '';
                        for (let i = 0; i < 5 && node; i += 1) {
                            around += ` ${textOf(node)}`;
                            node = node.parentElement;
                        }
                        let score = 0;
                        if (/系统单号|基本信息|收货信息/.test(around)) score += 50;
                        if (rect.top < 460) score += 30;
                        if (rect.left > window.innerWidth * 0.55) score += 20;
                        if (rect.top < 170) score -= 90;
                        if (/更多商品信息|商品信息/.test(around) && rect.top > 520) score -= 80;
                        return { el, score, top: rect.top, left: rect.left };
                    })
                    .filter((item) => item.score > -20)
                    .sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
                const node = nodes[0]?.el;
                if (!node) return false;
                node.click();
                return true;
            }
            """,
            label,
        )
        if clicked:
            await page.wait_for_timeout(1200)
            if await has_editable_contact_controls(page):
                return

async def fill_shipping_contact_field(page, field: str, value: str) -> bool:
    """填写收货联系方式字段，并尽量兼容不同控件结构。"""
    return bool(
        await page.evaluate(
            """
            ({ field, value }) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && rect.top > 130 &&
                        style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled && !el.readOnly;
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const setValue = (el, nextValue) => {
                    if (el.getAttribute && el.getAttribute('contenteditable') === 'true') {
                        el.focus();
                        el.textContent = nextValue;
                    } else {
                        const proto = el.tagName.toLowerCase() === 'textarea'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        el.focus();
                        if (setter) setter.call(el, nextValue);
                        else el.value = nextValue;
                    }
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: nextValue }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                };
                const findShippingRoot = () => {
                    const headings = Array.from(document.querySelectorAll('span,div,p,section'))
                        .filter((el) => visible(el) && textOf(el) === '收货信息');
                    const candidates = [];
                    for (const heading of headings) {
                        let node = heading.parentElement;
                        for (let i = 0; i < 8 && node && node !== document.body; i += 1) {
                            const text = textOf(node);
                            if (/收货信息/.test(text) && /电话/.test(text) && /买家邮箱/.test(text)) {
                                const rect = node.getBoundingClientRect();
                                candidates.push({ el: node, area: rect.width * rect.height, textLength: text.length });
                            }
                            node = node.parentElement;
                        }
                    }
                    candidates.sort((a, b) => a.textLength - b.textLength || a.area - b.area);
                    return candidates[0]?.el || null;
                };
                const findRow = (labelEl) => {
                    let node = labelEl;
                    const labelText = textOf(labelEl);
                    for (let i = 0; i < 7 && node && node.parentElement; i += 1) {
                        const text = textOf(node);
                        const controls = Array.from(node.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]'))
                            .filter(visible);
                        if (controls.length && text.includes(labelText) && text.length < 700) return node;
                        node = node.parentElement;
                    }
                    return labelEl.parentElement;
                };
                const root = findShippingRoot();
                if (!root) return false;
                const expectedLabel = field === 'phone' ? /^电话\\*?$/ : /^买家邮箱\\*?$/;
                const labels = Array.from(root.querySelectorAll('span,div,label,p'))
                    .filter((el) => visible(el) && expectedLabel.test(textOf(el)))
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return { el, rect };
                    })
                    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
                for (const label of labels) {
                    const row = findRow(label.el);
                    const rowControls = Array.from(row.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]'))
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const verticalOverlap = rect.top <= label.rect.bottom + 18 && rect.bottom >= label.rect.top - 18;
                            const rightDistance = rect.left - label.rect.right;
                            return { el, rect, verticalOverlap, rightDistance };
                        })
                        .filter((item) => item.verticalOverlap && item.rightDistance >= -12)
                        .sort((a, b) => Math.abs(a.rightDistance) - Math.abs(b.rightDistance) || a.rect.left - b.rect.left);
                    const control = rowControls[0]?.el;
                    if (control) {
                        setValue(control, value);
                        return true;
                    }
                }

                const allControls = Array.from(root.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]'))
                    .filter(visible);
                for (const label of labels) {
                    const fallback = allControls
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const verticalDistance = Math.abs((rect.top + rect.bottom) / 2 - (label.rect.top + label.rect.bottom) / 2);
                            const rightDistance = rect.left - label.rect.right;
                            return { el, rect, verticalDistance, rightDistance };
                        })
                        .filter((item) => item.rightDistance >= -12 && item.verticalDistance <= 26)
                        .sort((a, b) => a.verticalDistance - b.verticalDistance || Math.abs(a.rightDistance) - Math.abs(b.rightDistance))[0];
                    if (fallback) {
                        setValue(fallback.el, value);
                        return true;
                    }
                }
                return false;
            }
            """,
            {"field": field, "value": value},
        )
    )

async def click_save_button(page) -> bool:
    """点击详情页保存按钮并等待保存动作生效。"""
    clicked_tab_row_save = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.top >= 100 && rect.top <= window.innerHeight - 80 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const centerY = (rect) => (rect.top + rect.bottom) / 2;
            const nodes = Array.from(document.querySelectorAll('button,a,span,div,li'));
            const exactNodes = (label) => nodes
                .filter((el) => visible(el) && textOf(el) === label)
                .map((el) => ({ el, rect: el.getBoundingClientRect() }));
            const basicTabs = exactNodes('基本信息')
                .filter((item) =>
                    exactNodes('报关信息').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left) &&
                    exactNodes('操作日志').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left)
                )
                .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const tab = basicTabs[0];
            if (!tab) return false;
            const tabY = centerY(tab.rect);
            const saveCandidates = nodes
                .filter((el) => visible(el) && textOf(el) === '保存')
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const target = el.closest('button,a') || el;
                    return { el: target, rect };
                })
                .filter((item) =>
                    Math.abs(centerY(item.rect) - tabY) <= 34 &&
                    item.rect.left > tab.rect.left + 360
                )
                .sort((a, b) => b.rect.left - a.rect.left);
            const button = saveCandidates[0]?.el;
            if (!button) return false;
            button.click();
            return true;
        }
        """
    )
    if clicked_tab_row_save:
        await page.wait_for_timeout(1800)
        return True

    clicked_basic_info_save = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const findBasicInfoPanel = () => {
                const candidates = Array.from(document.querySelectorAll('div,section,article'))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = textOf(el);
                        return { el, rect, text, area: rect.width * rect.height };
                    })
                    .filter((item) =>
                        item.rect.top >= 160 &&
                        item.rect.top <= 760 &&
                        item.rect.width >= 620 &&
                        item.rect.height >= 140 &&
                        item.text.includes('基本信息') &&
                        item.text.includes('报关信息') &&
                        item.text.includes('操作日志') &&
                        item.text.includes('收货信息')
                    )
                    .sort((a, b) => a.text.length - b.text.length || a.area - b.area);
                return candidates[0] || null;
            };
            const panel = findBasicInfoPanel();
            if (!panel) return false;
            const buttons = Array.from(panel.el.querySelectorAll('button,a,span,div'))
                .filter((el) => visible(el) && textOf(el) === '保存')
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return { el, rect };
                })
                .filter((item) =>
                    item.rect.top >= panel.rect.top - 4 &&
                    item.rect.top <= panel.rect.top + 72 &&
                    item.rect.left >= panel.rect.right - 180 &&
                    item.rect.left <= panel.rect.right + 8
                )
                .sort((a, b) => a.rect.top - b.rect.top || b.rect.left - a.rect.left);
            const button = buttons[0]?.el;
            if (!button) return false;
            button.click();
            return true;
        }
        """
    )
    if clicked_basic_info_save:
        await page.wait_for_timeout(1800)
        return True

    for label in ["保存", "确定", "提交", "确认"]:
        clicked = await page.evaluate(
            """
            (label) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const nodes = Array.from(document.querySelectorAll('button,a,span'))
                    .filter((el) => visible(el) && textOf(el) === label)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        let node = el.parentElement;
                        let around = '';
                        for (let i = 0; i < 6 && node; i += 1) {
                            around += ` ${textOf(node)}`;
                            node = node.parentElement;
                        }
                        let score = 0;
                        if (/系统单号|基本信息|收货信息|电话|买家邮箱/.test(around)) score += 60;
                        if (/取消\\s*保存|保存\\s*取消/.test(around)) score += 30;
                        if (rect.top < 520) score += 20;
                        if (rect.left > window.innerWidth * 0.55) score += 15;
                        if (rect.top < 170) score -= 90;
                        if (/更多商品信息|商品信息/.test(around) && rect.top > 520) score -= 80;
                        return { el, score, top: rect.top, left: rect.left };
                    })
                    .filter((item) => item.score > -20)
                    .sort((a, b) => b.score - a.score || a.top - b.top || b.left - a.left);
                const node = nodes[0]?.el;
                if (!node) return false;
                node.click();
                return true;
            }
            """,
            label,
        )
        if clicked:
            await page.wait_for_timeout(1800)
            return True
    return False

async def click_cancel_edit_button(page) -> bool:
    """点击取消编辑按钮，退出详情页编辑状态。"""
    clicked = await page.evaluate(
        """
        () => {
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.top >= 100 && rect.top <= window.innerHeight - 80 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const centerY = (rect) => (rect.top + rect.bottom) / 2;
            const nodes = Array.from(document.querySelectorAll('button,a,span,div,li'));
            const exactNodes = (label) => nodes
                .filter((el) => visible(el) && textOf(el) === label)
                .map((el) => ({ el, rect: el.getBoundingClientRect() }));
            const basicTabs = exactNodes('基本信息')
                .filter((item) =>
                    exactNodes('报关信息').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left) &&
                    exactNodes('操作日志').some((other) => Math.abs(centerY(other.rect) - centerY(item.rect)) <= 28 && other.rect.left > item.rect.left)
                )
                .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const tab = basicTabs[0];
            if (!tab) return false;
            const tabY = centerY(tab.rect);
            const cancelCandidates = nodes
                .filter((el) => visible(el) && textOf(el) === '取消')
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const target = el.closest('button,a') || el;
                    return { el: target, rect };
                })
                .filter((item) =>
                    Math.abs(centerY(item.rect) - tabY) <= 34 &&
                    item.rect.left > tab.rect.left + 360
                )
                .sort((a, b) => b.rect.left - a.rect.left);
            const button = cancelCandidates[0]?.el;
            if (!button) return false;
            button.click();
            return true;
        }
        """
    )
    if clicked:
        await page.wait_for_timeout(900)
        return True
    return False

async def read_shipping_contact_values(page) -> dict[str, str]:
    # 只返回电话/买家邮箱的实际值，避免把整块“收货信息”表单文字误当成邮箱。
    """读取详情页当前展示的收货电话和邮箱。"""
    return dict(
        await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && rect.top > 130 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const valueOf = (el) => {
                    if (!el) return '';
                    if ('value' in el) return String(el.value || '').trim();
                    return textOf(el);
                };
                const extractValueFromText = (field, rawText) => {
                    const text = String(rawText || '').replace(/\\s+/g, ' ').trim();
                    if (!text) return '';
                    if (field === 'email') {
                        // 客户邮箱的前缀可能包含重音字母或撇号；读回页面值时不能用 ASCII 正则从中间截断。
                        const match = text.match(/[^\\s/@<>()\\[\\]";,:：]+@[A-Z0-9][A-Z0-9.\\-]*\\.[A-Z]{2,}/iu);
                        return match ? match[0] : '';
                    }
                    const afterLabel = text.replace(/^.*?电话\\*?\\s*/u, ' ');
                    const match = afterLabel.match(/\\+?\\d[\\d\\s().\\-]{5,34}\\d/);
                    return match ? match[0].replace(/\\s+/g, '').trim() : '';
                };
                const findShippingRoot = () => {
                    const headings = Array.from(document.querySelectorAll('span,div,p,section'))
                        .filter((el) => visible(el) && textOf(el) === '收货信息');
                    const candidates = [];
                    for (const heading of headings) {
                        let node = heading.parentElement;
                        for (let i = 0; i < 8 && node && node !== document.body; i += 1) {
                            const text = textOf(node);
                            if (/收货信息/.test(text) && /电话/.test(text) && /买家邮箱/.test(text)) {
                                const rect = node.getBoundingClientRect();
                                candidates.push({ el: node, area: rect.width * rect.height, textLength: text.length });
                            }
                            node = node.parentElement;
                        }
                    }
                    candidates.sort((a, b) => a.textLength - b.textLength || a.area - b.area);
                    return candidates[0]?.el || null;
                };
                const findValue = (field) => {
                    const root = findShippingRoot();
                    if (!root) return '';
                    const expectedLabel = field === 'phone' ? /^电话\\*?$/ : /^买家邮箱\\*?$/;
                    const expectedValue = field === 'phone' ? /\\d{5,}/ : /@/;
                    const labelText = field === 'phone' ? '电话' : '买家邮箱';
                    const labels = Array.from(root.querySelectorAll('span,div,label,p'))
                        .filter((el) => visible(el) && expectedLabel.test(textOf(el)))
                        .map((el) => ({ el, rect: el.getBoundingClientRect() }))
                        .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
                    const controls = Array.from(root.querySelectorAll('input:not([type="hidden"]),textarea,[contenteditable="true"]'))
                        .filter(visible)
                        .map((el) => ({ el, rect: el.getBoundingClientRect() }));
                    for (const label of labels) {
                        const control = controls
                            .map((item) => {
                                const verticalDistance = Math.abs((item.rect.top + item.rect.bottom) / 2 - (label.rect.top + label.rect.bottom) / 2);
                                const rightDistance = item.rect.left - label.rect.right;
                                return { ...item, verticalDistance, rightDistance };
                            })
                            .filter((item) => item.rightDistance >= -12 && item.verticalDistance <= 28)
                            .sort((a, b) => a.verticalDistance - b.verticalDistance || Math.abs(a.rightDistance) - Math.abs(b.rightDistance))[0];
                        if (control) {
                            const value = valueOf(control.el);
                            return extractValueFromText(field, value) || value;
                        }
                        let node = label.el.parentElement;
                        for (let i = 0; i < 5 && node && node !== root.parentElement; i += 1) {
                            const rowText = textOf(node);
                            if (rowText.includes(labelText) && rowText.length <= 260) {
                                const extracted = extractValueFromText(field, rowText);
                                if (extracted) return extracted;
                            }
                            node = node.parentElement;
                        }
                    }
                    if (field === 'email') {
                        const fallbackControl = controls
                            .map((item) => ({ ...item, value: valueOf(item.el) }))
                            .filter((item) => item.value && expectedValue.test(item.value))
                            .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left)[0];
                        if (fallbackControl) return fallbackControl.value;
                    }
                    return '';
                };
                return { phone: findValue('phone'), email: findValue('email') };
            }
            """
        )
    )


async def fill_contact_fields(page, contact: ContactInfo) -> tuple[bool, str]:
    """把提取到的电话和邮箱写入详情页对应字段。"""
    await try_open_edit_mode(page)
    changed: list[str] = []

    if contact.phone:
        filled_phone = await fill_shipping_contact_field(page, "phone", contact.phone)
        if filled_phone:
            changed.append("电话")

    if contact.email:
        filled_email = await fill_shipping_contact_field(page, "email", contact.email)
        if filled_email:
            changed.append("买家邮箱")

    if not changed:
        return False, "没有在详情页“基本信息-收货信息”区域找到可编辑的电话/买家邮箱输入框。"

    saved = await click_save_button(page)
    if not saved:
        return False, f"已填入 {'、'.join(changed)}，但没有找到保存按钮，请在浏览器里检查后手动保存。"
    return True, f"已填入并点击保存：{'、'.join(changed)}。"


def verify_saved_contact_values(contact: ContactInfo, saved_values: dict[str, str]) -> str | None:
    """保存后复核页面值；失败时返回错误说明，成功时返回 None。"""
    if contact.phone:
        expected_phone = normalize_phone(contact.phone)
        actual_phone = normalize_phone(saved_values.get("phone") or "")
        if not actual_phone:
            return "保存后没有重新读取到电话，已停止标记成功。"
        if expected_phone != actual_phone:
            return f"保存后电话校验失败：期望 {contact.phone}，页面为 {saved_values.get('phone') or '-'}。"

    if contact.email:
        expected_email = contact.email.strip().lower()
        actual_email = (saved_values.get("email") or "").strip().lower()
        if not actual_email:
            return "保存后没有重新读取到买家邮箱，已停止标记成功。"
        if expected_email != actual_email:
            return f"保存后买家邮箱校验失败：期望 {contact.email}，页面为 {saved_values.get('email') or '-'}。"

    return None

async def wait_for_saved_contact_values(
    page,
    contact: ContactInfo,
    *,
    timeout_ms: int = 10000,
    interval_ms: int = 500,
) -> tuple[dict[str, str], str | None]:
    """保存后轮询收货信息，等待 ERP 把新值刷新到详情页。

    领星保存后有时会先短暂显示旧值，如果只读一次会把已经成功保存的订单误判失败。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last_values: dict[str, str] = {}
    last_error: str | None = None
    while True:
        values = await read_shipping_contact_values(page)
        error = verify_saved_contact_values(contact, values)
        if error is None:
            return values, None
        last_values = values
        last_error = error
        if time.monotonic() >= deadline:
            return last_values, f"{last_error}（已等待 {timeout_ms // 1000} 秒刷新）"
        await page.wait_for_timeout(interval_ms)


async def update_current_detail_contact(
    page,
    contact: ContactInfo,
    *,
    expected_system_order_no: str,
    expected_platform_order_no: str | None = None,
    source_system_order_no: str | None = None,
    confirm_callback: WriteConfirmCallback | None = None,
) -> tuple[bool, str]:
    """更新当前详情弹窗中的联系方式并返回写回结果。"""
    before_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "写入前",
    )
    await try_open_edit_mode(page)
    edit_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "进入编辑后/写入前",
    )
    before_values = await read_shipping_contact_values(page)

    if confirm_callback is None:
        return False, "缺少保存前 CMD 二次确认回调，已停止写入。"

    changed: list[str] = []
    if contact.phone:
        filled_phone = await fill_shipping_contact_field(page, "phone", contact.phone)
        if filled_phone:
            changed.append("电话")
    if contact.email:
        filled_email = await fill_shipping_contact_field(page, "email", contact.email)
        if filled_email:
            changed.append("买家邮箱")

    if not changed:
        return False, "没有在详情页“基本信息-收货信息”区域找到可编辑的电话/买家邮箱输入框。"

    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "已填入/保存前",
    )
    after_fill_values = await read_shipping_contact_values(page)
    # 保存按钮前先校验填入后的页面值；如果页面读回值和待写入值不一致，
    # 必须取消编辑，避免用户确认后保存错误联系方式。
    fill_verify_message = verify_saved_contact_values(contact, after_fill_values)
    if fill_verify_message:
        canceled = await click_cancel_edit_button(page)
        cancel_message = "已点击取消，未保存。" if canceled else "未找到取消按钮，请在浏览器里手动取消或保存。"
        return False, f"填入后页面值校验失败，已停止保存：{fill_verify_message}{cancel_message}"
    confirmed = await confirm_callback(
        {
            "expected_system_order_no": expected_system_order_no,
            "expected_platform_order_no": expected_platform_order_no,
            "current_identity": edit_identity,
            "source_system_order_no": source_system_order_no or expected_system_order_no,
            "phone": contact.phone,
            "email": contact.email,
            "before_values": before_values,
            "after_fill_values": after_fill_values,
            "source_excerpt": contact.source_excerpt,
        }
    )
    if not confirmed:
        canceled = await click_cancel_edit_button(page)
        cancel_message = "已点击取消，未保存。" if canceled else "未找到取消按钮，请在浏览器里手动取消或保存。"
        return False, f"用户未在 CMD 中确认保存，已跳过。{cancel_message}"
    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "用户确认后/保存前",
    )
    saved = await click_save_button(page)
    if not saved:
        return False, f"已填入 {'、'.join(changed)}，但没有找到保存按钮，请在浏览器里检查后手动保存。"
    await page.wait_for_timeout(800)
    await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "保存后/等待校验前",
    )
    # 保存完成后重新读取页面值；本次写入了哪些字段，就严格校验哪些字段。
    after_save_values, verify_message = await wait_for_saved_contact_values(page, contact)
    if verify_message:
        return False, verify_message
    after_identity = await assert_current_detail_order(
        page,
        expected_system_order_no,
        expected_platform_order_no,
        "保存后",
    )
    return (
        True,
        "已校验订单上下文并保存："
        f"{'、'.join(changed)}。"
        f" 写入前值={before_values}，填入后值={after_fill_values}，保存后值={after_save_values}，"
        f"保存后系统单号={after_identity.get('system_order_no')}。",
    )

async def update_contact_for_system_orders(
    page,
    system_order_nos: list[str],
    contact: ContactInfo,
    *,
    expected_platform_order_no: str | None = None,
    source_system_order_no: str | None = None,
    confirm_callback: WriteConfirmCallback | None = None,
) -> tuple[list[str], list[str]]:
    """遍历系统订单列表并为匹配订单写回联系方式。"""
    updated: list[str] = []
    messages: list[str] = []
    for system_order_no in system_order_nos:
        await close_order_detail_dialog(page)
        await click_system_order(page, system_order_no)
        await wait_for_detail(page, system_order_no)
        saved, message = await update_current_detail_contact(
            page,
            contact,
            expected_system_order_no=system_order_no,
            expected_platform_order_no=expected_platform_order_no,
            source_system_order_no=source_system_order_no,
            confirm_callback=confirm_callback,
        )
        messages.append(f"{system_order_no}: {message}")
        if saved:
            updated.append(system_order_no)
    return updated, messages
