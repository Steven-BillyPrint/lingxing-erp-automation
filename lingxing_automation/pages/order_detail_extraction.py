from __future__ import annotations

import re

from ..models import ContactInfo, OrderCustomizationItem
from ..parsers.contact import extract_complete_contact_candidates, extract_contact_info, missing_contact_fields, normalize_text
from .order_detail_navigation import close_order_detail_dialog, click_system_order, wait_for_detail



async def move_mouse_to_safe_area(page) -> None:
    """把鼠标移回详情内容区，避免悬停弹窗残留影响下一次采集。"""
    try:
        point = await page.evaluate(
            """
            () => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = Array.from(document.querySelectorAll('.el-dialog__wrapper,.vxe-modal--wrapper,.order-detail-dialog,.el-drawer,.el-dialog,main,section,article,div'))
                    .filter((el) => el !== document.body && el !== document.documentElement && visible(el))
                    .map((el) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
                    .filter((item) => /系统单号/.test(item.text) && /(基本信息|收货信息|商品信息)/.test(item.text))
                    .sort((a, b) => a.text.length - b.text.length)[0];
                if (root) {
                    return {
                        x: Math.max(16, Math.min(root.rect.left + 24, window.innerWidth * 0.45)),
                        y: Math.max(180, Math.min(root.rect.top + 90, window.innerHeight - 16)),
                    };
                }
                return { x: 24, y: Math.min(Math.max(220, window.innerHeight / 2), window.innerHeight - 16) };
            }
            """
        )
        await page.mouse.move(float(point["x"]), float(point["y"]))
    except Exception:
        pass

async def collect_detail_text_candidates(page, system_order_no: str) -> list[str]:
    """收集订单详情页中可用于提取联系方式的文本候选。"""
    await move_mouse_to_safe_area(page)
    texts: list[str] = await page.evaluate(
        """
        (systemOrderNo) => {
            const output = [];
            const seen = new Set();
            const useful = /更多商品信息|Custom|Configuration|Options|texting number|email address|phone number|@|买家邮箱|电话|ASIN|父ASIN|B0[A-Z0-9]{8}|付款|付款时间/i;
            const attrs = ['title', 'aria-label', 'data-title', 'data-original-title', 'data-content'];
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
            };
            const push = (value) => {
                if (!value) return;
                const text = String(value).replace(/\\s+/g, ' ').trim();
                if (!text || seen.has(text)) return;
                seen.add(text);
                output.push(text);
            };
            const collectAttrs = (root) => {
                const nodes = [root, ...Array.from(root.querySelectorAll ? root.querySelectorAll('*') : [])];
                for (const node of nodes) {
                    for (const attr of attrs) {
                        const value = node.getAttribute && node.getAttribute(attr);
                        if (value && useful.test(value)) push(value);
                    }
                    const href = node.getAttribute && node.getAttribute('href');
                    if (href && useful.test(`${node.textContent || ''} ${href}`)) push(`${node.textContent || ''} ${href}`);
                }
            };
            const allNodes = Array.from(document.querySelectorAll('*'));
            const detailRoots = allNodes
                .filter((el) => visible(el))
                .map((el) => ({ el, text: el.innerText || el.textContent || '' }))
                .filter((item) =>
                    item.text.includes(systemOrderNo) &&
                    /基本信息|更多商品信息|收货信息/.test(item.text) &&
                    item.text.length >= 80 &&
                    item.text.length < 30000
                )
                .sort((a, b) => a.text.length - b.text.length)
                .slice(0, 5)
                .map((item) => item.el);
            const roots = detailRoots.length ? detailRoots : [];
            const nodes = roots.flatMap((root) => [root, ...Array.from(root.querySelectorAll('*'))]);
            for (const el of nodes) {
                const visibleText = visible(el) ? (el.innerText || '') : '';
                const rawText = el.textContent || '';
                const candidateText = visibleText || rawText;
                if (!useful.test(candidateText)) continue;
                if (candidateText.length < 5000) push(candidateText);
                let node = el;
                for (let i = 0; i < 7 && node && node !== document.body; i += 1) {
                    const text = node.innerText || node.textContent || '';
                    if (text.length >= 10 && text.length < 8000 && useful.test(text)) {
                        push(text);
                        collectAttrs(node);
                    }
                    node = node.parentElement;
                }
            }
            return output;
        }
        """,
        system_order_no,
    )
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
            const nodes = Array.from(document.querySelectorAll('a,span,div,td,th,p'));
            const detailRoots = Array.from(document.querySelectorAll('.el-dialog__wrapper,.vxe-modal--wrapper,.order-detail-dialog,.el-drawer,.el-dialog'))
                .filter((el) => visible(el))
                .map((el) => ({ el, text: textOf(el) }))
                .filter((item) =>
                    /系统单号|更多商品信息|商品信息|收货信息|基本信息/.test(item.text) &&
                    item.text.length >= 80 &&
                    item.text.length < 50000
                )
                .sort((a, b) => a.text.length - b.text.length)
                .map((item) => item.el);
            const scope = detailRoots[0] || document.body;
            const scopeNodes = Array.from(scope.querySelectorAll('a,span,div,td,th,p'));
            const target =
                scopeNodes.find((el) => visible(el) && textOf(el) === '更多商品信息') ||
                scopeNodes.find((el) => visible(el) && /Custom.+Config|Package Config|Frame Options/i.test(textOf(el)));
            if (target) {
                target.scrollIntoView({ block: 'center', inline: 'center' });
            }
        }
        """
    )
    await page.wait_for_timeout(350)
    hover_points: list[dict[str, float]] = await page.evaluate(
        """
        (systemOrderNo) => {
            const useful = /更多商品信息|Custom|Configuration|Config|Package|Options|Please provide|texting number|email address|phone number|emergencies|@|ASIN|父ASIN|B0[A-Z0-9]{8}|付款|付款时间/i;
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.top >= 0 && rect.left >= 0 &&
                    rect.top < window.innerHeight && rect.left < window.innerWidth &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const attrText = (el) => [
                el.getAttribute('title') || '',
                el.getAttribute('aria-label') || '',
                el.getAttribute('data-title') || '',
                el.getAttribute('data-original-title') || '',
                el.getAttribute('data-content') || '',
            ].join(' ');
            const explicitRoots = Array.from(document.querySelectorAll('.el-dialog__wrapper,.vxe-modal--wrapper,.order-detail-dialog,.el-drawer,.el-dialog'));
            const genericRoots = Array.from(document.querySelectorAll('main,section,article,div'))
                .filter((el) => el !== document.body && el !== document.documentElement);
            const roots = [...explicitRoots, ...genericRoots]
                .filter((el) => visible(el))
                .map((el) => ({ el, text: textOf(el) }))
                .filter((item) =>
                    (item.text.includes(systemOrderNo) || /系统单号|更多商品信息|商品信息/.test(item.text)) &&
                    /基本信息|更多商品信息|收货信息|商品信息/.test(item.text) &&
                    item.text.length >= 80 &&
                    item.text.length < 50000
                )
                .sort((a, b) => a.text.length - b.text.length)
                .slice(0, 4)
                .map((item) => item.el);
            // 只在订单详情内容区内找悬停点，避免误点顶部导航或其它全局控件。
            const scanRoots = roots;
            if (!scanRoots.length) return [];
            const insideRoot = (rect, root) => {
                const rootRect = root.getBoundingClientRect();
                return rect.top >= rootRect.top + 40 &&
                    rect.bottom <= rootRect.bottom + 8 &&
                    rect.left >= rootRect.left - 8 &&
                    rect.right <= rootRect.right + 8 &&
                    rect.top > 150;
            };
            const candidates = [];
            for (const root of scanRoots) {
                const rootNodes = Array.from(root.querySelectorAll('a,span,div,td,th,p'));
                const moreInfoHeaders = rootNodes
                    .filter((el) => visible(el) && textOf(el) === '更多商品信息')
                    .map((el) => el.getBoundingClientRect());
                for (const header of moreInfoHeaders) {
                    for (const el of rootNodes) {
                        if (!visible(el)) continue;
                        const rect = el.getBoundingClientRect();
                        if (!insideRoot(rect, root)) continue;
                        if (rect.top <= header.bottom - 2) continue;
                        if (rect.left < header.left - 80 || rect.left > header.right + 260) continue;
                        const text = `${textOf(el)} ${attrText(el)}`.trim();
                        const className = String(el.className || '');
                        if (!text || text === '更多商品信息') continue;
                        const looksHoverable = useful.test(text) || /tooltip|popover|ellipsis|link|blue/i.test(className);
                        if (!looksHoverable) continue;
                        candidates.push({
                            x: Math.min(Math.max(rect.left + Math.min(rect.width / 2, 90), 1), window.innerWidth - 2),
                            y: Math.min(Math.max(rect.top + rect.height / 2, 1), window.innerHeight - 2),
                            score: 100 + (useful.test(text) ? 30 : 0) + (/Custom|Config|Package/i.test(text) ? 20 : 0),
                            top: rect.top,
                            left: rect.left,
                        });
                    }
                }
                const nodes = Array.from(root.querySelectorAll('a,span,div,td,p'));
                for (const el of nodes) {
                    if (!visible(el)) continue;
                    const text = `${textOf(el)} ${attrText(el)}`;
                    const className = String(el.className || '');
                    if (!useful.test(text) && !/tooltip|popover|ellipsis/i.test(className)) continue;
                    const rect = el.getBoundingClientRect();
                    if (!insideRoot(rect, root)) continue;
                    if (rect.width < 5 || rect.height < 5) continue;
                    candidates.push({
                        x: Math.min(Math.max(rect.left + rect.width / 2, 1), window.innerWidth - 2),
                        y: Math.min(Math.max(rect.top + rect.height / 2, 1), window.innerHeight - 2),
                        score: (useful.test(text) ? 10 : 0) + (className.includes('ellipsis') ? 3 : 0) + (rect.width > 80 ? 1 : 0),
                        top: rect.top,
                        left: rect.left,
                    });
                }
            }
            candidates.sort((a, b) => b.score - a.score || b.left - a.left || a.top - b.top);
            const seen = new Set();
            const output = [];
            for (const item of candidates) {
                const key = `${Math.round(item.x / 6)}:${Math.round(item.y / 6)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                output.push({ x: item.x, y: item.y });
                // 多商品订单里每个商品都有自己的“更多商品信息”，多扫一些悬停点，避免漏掉后面的商品。
                if (output.length >= 40) break;
            }
            return output;
        }
        """,
        system_order_no,
    )

    seen = {normalize_text(text) for text in texts if normalize_text(text)}
    for point in hover_points:
        try:
            await page.mouse.move(point["x"], point["y"])
            await page.wait_for_timeout(650)
            popup_texts: list[str] = await page.evaluate(
                """
                () => {
                    const useful = /Custom|Configuration|Config|Package|Options|Please provide|texting number|email address|phone number|emergencies|@|更多商品信息|ASIN|父ASIN|B0[A-Z0-9]{8}|付款|付款时间/i;
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '')
                        .replace(/[\\t\\f\\v ]+/g, ' ')
                        .replace(/\\s*\\n\\s*/g, '\\n')
                        .trim();
                    const explicit = Array.from(document.querySelectorAll('.el-tooltip__popper,.el-popper,.vxe-table--tooltip-wrapper,.ak-tooltip,.tooltip,.popover'));
                    const floating = Array.from(document.querySelectorAll('*')).filter((el) => {
                        if (!visible(el)) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
                        const text = textOf(el);
                        return text.length >= 20 &&
                            text.length < 7000 &&
                            useful.test(text) &&
                            (style.position === 'absolute' || style.position === 'fixed' || zIndex >= 100 || rect.width >= 260);
                    });
                    return [...explicit, ...floating]
                        .filter((el) => visible(el))
                        .map((el) => ({ el, text: textOf(el) }))
                        .filter((item) => item.text && useful.test(item.text))
                        .map((item) => item.text);
                }
                """
            )
            for popup_text in popup_texts:
                cleaned = re.sub(r"[\t\f\v ]+", " ", popup_text).strip()
                normalized = normalize_text(cleaned)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    texts.append(cleaned)
        except Exception:
            pass
        finally:
            await move_mouse_to_safe_area(page)
    await move_mouse_to_safe_area(page)
    return texts

async def extract_contact_from_system_order(page, system_order_no: str) -> tuple[ContactInfo, list[str]]:
    """从指定系统订单详情中提取联系方式。"""
    await close_order_detail_dialog(page)
    await click_system_order(page, system_order_no)
    await wait_for_detail(page, system_order_no)
    texts = await collect_detail_text_candidates(page, system_order_no)
    contact = extract_contact_info(texts)
    return contact, texts

async def collect_detail_contact_candidates(page, system_order_no: str) -> tuple[list[ContactInfo], list[str]]:
    """收集详情页每个商品“更多商品信息”里独立完整的电话/邮箱候选。"""
    texts = await collect_detail_text_candidates(page, system_order_no)
    contacts = extract_complete_contact_candidates(texts)
    return contacts, texts


async def collect_detail_customization_items(page, system_order_no: str) -> list[OrderCustomizationItem]:
    """按详情页商品行收集 ASIN/SKU 与完整定制化 tooltip 文本。

    文件夹数量来自 Amazon API，不在这里读取 DOM 数量；这里仅负责把每个商品行
    的“更多商品信息”文本和对应 ASIN/SKU 绑定，避免同一订单多商品时串用定制信息。
    """

    await move_mouse_to_safe_area(page)
    row_targets: list[dict[str, object]] = await page.evaluate(
        """
        (systemOrderNo) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    rect.bottom >= 0 && rect.top <= window.innerHeight &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const extractAsin = (text) => {
                const match = String(text || '').toUpperCase().match(/B0[A-Z0-9]{8}/);
                return match ? match[0] : '';
            };
            const extractSku = (text) => {
                const match = String(text || '').match(/SKU\\s+(.+?)(?=\\s+(?:商品ID|ASIN|MSKU|参考号|订单信息|交易信息|其他信息|更多商品信息)\\b|$)/i);
                return match ? match[1].replace(/\\s+共\\s*\\d+\\s*$/, '').trim() : '';
            };
            const detailRoots = Array.from(document.querySelectorAll(
                '.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'
            ))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const text = textOf(el);
                    return text.includes(systemOrderNo) && /商品信息/.test(text) && /B0[A-Z0-9]{8}/i.test(text);
                })
                .map((el) => ({ el, text: textOf(el) }))
                .filter((item) => item.text.length >= 80 && item.text.length < 80000)
                .sort((a, b) => a.text.length - b.text.length)
                .map((item) => item.el);
            const root = detailRoots[0] || document.body;
            const asinNodes = Array.from(root.querySelectorAll('span,div,p,td,th,b,strong,a'))
                .filter((el) => visible(el) && /^B0[A-Z0-9]{8}$/i.test(textOf(el)));
            const rowCandidates = [];
            const addCandidate = (el, asinNode) => {
                if (!el || el === document.body || el === document.documentElement || !visible(el)) return;
                const text = textOf(el);
                const asin = extractAsin(text);
                if (!asin || text.length < 120 || text.length > 16000) return;
                const hasProductSignals = /SKU|商品ID|ASIN|MSKU|订单信息|交易信息|其他信息|更多商品信息/.test(text);
                if (!hasProductSignals) return;
                const rect = el.getBoundingClientRect();
                rowCandidates.push({ el, text, asin, rect, asinTop: asinNode.getBoundingClientRect().top });
            };
            for (const asinNode of asinNodes) {
                let node = asinNode;
                for (let depth = 0; depth < 18 && node && node !== document.body; depth += 1) {
                    addCandidate(node, asinNode);
                    node = node.parentElement;
                }
            }
            rowCandidates.sort((a, b) => {
                const score = (item) =>
                    (/更多商品信息/.test(item.text) ? 1000 : 0) +
                    (/订单信息/.test(item.text) ? 200 : 0) +
                    (/交易信息/.test(item.text) ? 100 : 0) -
                    Math.abs(item.rect.top - item.asinTop) -
                    item.text.length / 100;
                return score(b) - score(a);
            });
            const chosenRows = [];
            const seenAsinTop = new Set();
            for (const row of rowCandidates) {
                const key = `${row.asin}:${Math.round(row.asinTop / 8)}`;
                if (seenAsinTop.has(key)) continue;
                seenAsinTop.add(key);
                chosenRows.push(row);
            }
            chosenRows.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const output = [];
            for (const [index, row] of chosenRows.entries()) {
                const nodes = Array.from(row.el.querySelectorAll('a,span,div,p,td,th,b,strong,button'));
                const hoverTargets = nodes
                    .filter((el) => {
                        if (!visible(el)) return false;
                        const text = textOf(el);
                        const className = String(el.className || '');
                        return /Customize|Custom|Design|Surface|Thickness|Magnet|Options?|Background|Image|更多商品信息/i.test(text) ||
                            /tooltip|ellipsis|popover/i.test(className);
                    })
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = textOf(el);
                        const score =
                            (/Customize|Custom|Design|Surface|Thickness|Magnet|Options?/i.test(text) ? 100 : 0) +
                            (/更多商品信息/.test(text) ? 30 : 0) +
                            (rect.left > row.rect.left + row.rect.width * 0.55 ? 20 : 0) -
                            Math.min(text.length / 80, 40);
                        return { el, rect, text, score };
                    })
                    .filter((item) => item.rect.width > 3 && item.rect.height > 3)
                    .sort((a, b) => b.score - a.score);
                const target = hoverTargets[0];
                if (!target) continue;
                output.push({
                    row_index: index,
                    asin: row.asin,
                    sku: extractSku(row.text),
                    row_text: row.text.slice(0, 500),
                    x: Math.min(Math.max(target.rect.left + target.rect.width / 2, 1), window.innerWidth - 2),
                    y: Math.min(Math.max(target.rect.top + target.rect.height / 2, 1), window.innerHeight - 2),
                });
            }
            return output;
        }
        """,
        system_order_no,
    )
    items: list[OrderCustomizationItem] = []
    seen: set[tuple[str | None, str | None, str]] = set()
    for target in row_targets:
        try:
            await page.mouse.move(float(target["x"]), float(target["y"]))
            await page.wait_for_timeout(650)
            popup_texts: list[str] = await page.evaluate(
                """
                () => {
                    const useful = /Customize|Custom|Design|Surface Material Option|Choose Your Magnet Thickness|Corner|Shapes|Please provide|Magnet|Background|Image/i;
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '')
                        .replace(/[\\t\\f\\v ]+/g, ' ')
                        .replace(/\\s*\\n\\s*/g, '\\n')
                        .trim();
                    const roots = Array.from(document.querySelectorAll('.el-tooltip__popper,.el-popper,.vxe-table--tooltip-wrapper,.ak-tooltip,.tooltip,.popover,[role="tooltip"],body > div'))
                        .filter((el) => visible(el))
                        .map((el) => ({ el, text: textOf(el), rect: el.getBoundingClientRect(), z: Number.parseInt(window.getComputedStyle(el).zIndex || '0', 10) || 0 }))
                        .filter((item) => item.text.length >= 20 && item.text.length < 9000 && useful.test(item.text))
                        .sort((a, b) => b.z - a.z || a.text.length - b.text.length);
                    return roots.map((item) => item.text);
                }
                """
            )
            text = next((value.strip() for value in popup_texts if value and value.strip()), "")
            if not text:
                text = str(target.get("row_text") or "").strip()
            normalized = normalize_text(text)
            key = (str(target.get("asin") or "") or None, str(target.get("sku") or "") or None, normalized)
            if normalized and key not in seen:
                seen.add(key)
                items.append(
                    OrderCustomizationItem(
                        asin=str(target.get("asin") or "") or None,
                        sku=str(target.get("sku") or "") or None,
                        customization_text=text,
                        row_index=int(target.get("row_index") or 0),
                    )
                )
        except Exception:
            pass
        finally:
            await move_mouse_to_safe_area(page)
    return items

_RECIPIENT_STOP_LABELS = (
    "公司",
    "电话",
    "买家邮箱",
    "收件地址",
    "详细地址",
    "门牌号",
    "邮编",
    "地址类型",
    "买家姓名",
    "收件人",
)


def _clean_detail_recipient_name(value: str | None) -> str | None:
    """清理详情页提取到的收件人，过滤金额、电话、邮编等误读值。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:收件人|买家姓名)\s*[:：]?\s*", "", text).strip()
    for label in _RECIPIENT_STOP_LABELS:
        match = re.search(rf"\s+{re.escape(label)}\s*[:：]?", text)
        if match:
            text = text[: match.start()].strip()
    text = text.strip(" :：-")
    if not text or text == "-":
        return None
    if text in _RECIPIENT_STOP_LABELS:
        return None
    if re.fullmatch(r"[\d\s()+\-–—.,#/]+", text):
        return None
    if re.fullmatch(r"\$?\d[\d,]*(?:\.\d+)?", text):
        return None
    if "@" in text:
        return None
    if re.search(r"商品金额|订单收入|毛利润|物流|运单号|SKU|ASIN|MSKU|COD订单|发货仓库|税|成本|利润", text, re.I):
        return None
    if len(text) > 90:
        return None
    return text


async def read_detail_recipient_name(page) -> str | None:
    """从详情页收货信息 DOM 中读取收件人，失败时再读买家姓名。"""
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
            const valueOf = (el) => {
                if (!el) return '';
                if ('value' in el && String(el.value || '').trim()) return String(el.value || '').trim();
                return textOf(el);
            };
            const escapeRegExp = (value) => String(value || '').replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
            const labels = ['公司', '电话', '买家邮箱', '收件地址', '详细地址', '门牌号', '邮编', '地址类型', '买家姓名', '收件人'];
            const badText = /商品金额|订单收入|毛利润|物流|运单号|SKU|ASIN|MSKU|COD订单|发货仓库|税|成本|利润/i;
            const cleanValue = (value, label) => {
                let text = String(value || '').replace(/\\s+/g, ' ').trim();
                text = text.replace(new RegExp(`^${escapeRegExp(label)}\\s*[:：]?\\s*`), '').trim();
                for (const stopLabel of labels) {
                    const match = text.match(new RegExp(`\\s+${escapeRegExp(stopLabel)}\\s*[:：]?`));
                    if (match) {
                        text = text.slice(0, match.index).trim();
                    }
                }
                text = text.replace(/^[：:\\-\\s]+|[：:\\-\\s]+$/g, '').trim();
                if (!text || text === '-' || labels.includes(text)) return '';
                if (/^[\\d\\s()+\\-–—.,#/]+$/.test(text)) return '';
                if (/^\\$?\\d[\\d,]*(?:\\.\\d+)?$/.test(text)) return '';
                if (/@/.test(text)) return '';
                if (badText.test(text)) return '';
                if (text.length > 90) return '';
                return text;
            };
            const extractFromReceiveInfo = (label) => {
                const scopes = Array.from(document.querySelectorAll('.receive-info'))
                    .filter((el) => visible(el) && /收货信息/.test(textOf(el)));
                for (const scope of scopes) {
                    const wrappers = Array.from(scope.querySelectorAll('.info-wrapper')).filter((el) => visible(el));
                    for (const wrapper of wrappers) {
                        const labelEl = Array.from(wrapper.querySelectorAll('.label,span,div,label'))
                            .find((el) => visible(el) && textOf(el) === label);
                        if (!labelEl) continue;
                        const valueEl = wrapper.querySelector('.value,.ak-width-100p,input,textarea');
                        const directValue = cleanValue(valueOf(valueEl), label);
                        if (directValue) return directValue;
                        const rowValue = cleanValue(textOf(wrapper), label);
                        if (rowValue) return rowValue;
                    }
                }
                return '';
            };
            const receiveInfoValue = extractFromReceiveInfo('收件人') || extractFromReceiveInfo('买家姓名');
            if (receiveInfoValue) return receiveInfoValue;
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
                .filter((item) => /收货信息/.test(item.text) && (/收件人/.test(item.text) || /买家姓名/.test(item.text)))
                .sort((a, b) => a.text.length - b.text.length)
                .map((item) => item.el);
            const scope = shippingRoots[0] || root;
            const extractFromRowText = (rowText, label) => {
                const text = String(rowText || '').replace(/\\s+/g, ' ').trim();
                const pattern = new RegExp(`${label}\\s*[:：]?\\s*(.+?)(?=\\s*(?:公司|电话|买家邮箱|收件地址|详细地址|门牌号|邮编|地址类型|买家姓名)\\s*[:：]?|$)`);
                const match = text.match(pattern);
                return match ? cleanValue(match[1], label) : '';
            };
            const extractByLabel = (label) => {
                const candidates = [];
                const addCandidate = (value, score) => {
                    const clean = cleanValue(value, label);
                    if (clean) candidates.push({ value: clean, score });
                };
                const labels = Array.from(scope.querySelectorAll('span,div,label,p,td,th'))
                    .filter((el) => visible(el) && textOf(el) === label);
                for (const labelEl of labels) {
                    let sibling = labelEl.nextElementSibling;
                    for (let index = 0; index < 5 && sibling; index += 1, sibling = sibling.nextElementSibling) {
                        if (visible(sibling)) addCandidate(valueOf(sibling), index + 1);
                    }
                    let node = labelEl.parentElement;
                    for (let depth = 0; depth < 8 && node && node !== document.body; depth += 1) {
                        const rowText = textOf(node);
                        if (rowText.includes(label) && rowText.length <= 700) {
                            const directChildren = Array.from(node.children || []).filter((el) => visible(el));
                            const labelIndex = directChildren.findIndex((item) => item === labelEl || item.contains(labelEl));
                            if (labelIndex >= 0) {
                                for (let index = labelIndex + 1; index < Math.min(directChildren.length, labelIndex + 7); index += 1) {
                                    addCandidate(valueOf(directChildren[index]), 20 + depth * 10 + index - labelIndex);
                                }
                            }
                            const fallback = extractFromRowText(rowText, label);
                            if (fallback) addCandidate(fallback, 80 + depth);
                        }
                        node = node.parentElement;
                    }
                    const rect = labelEl.getBoundingClientRect();
                    const labelCenterY = (rect.top + rect.bottom) / 2;
                    const nearbyNodes = Array.from(scope.querySelectorAll('span,div,label,p,td,th,input,textarea'))
                        .filter((el) => visible(el))
                        .map((el) => ({ el, rect: el.getBoundingClientRect(), text: valueOf(el) }))
                        .filter((item) => {
                            if (item.el === labelEl || item.el.contains(labelEl) || labelEl.contains(item.el)) return false;
                            const clean = cleanValue(item.text, label);
                            if (!clean) return false;
                            const itemCenterY = (item.rect.top + item.rect.bottom) / 2;
                            const sameLine = Math.abs(itemCenterY - labelCenterY) <= Math.max(10, rect.height * 0.8);
                            const toRight = item.rect.left >= rect.right - 4 && item.rect.left <= rect.left + 520;
                            return sameLine && toRight;
                        });
                    for (const item of nearbyNodes) {
                        const dx = Math.max(0, item.rect.left - rect.right);
                        const dy = Math.abs(((item.rect.top + item.rect.bottom) / 2) - labelCenterY);
                        addCandidate(item.text, 120 + dx + dy * 20 + String(item.text || '').length / 100);
                    }
                }
                candidates.sort((a, b) => a.score - b.score || a.value.length - b.value.length);
                return candidates.length ? candidates[0].value : '';
            };
            return extractByLabel('收件人') || extractByLabel('买家姓名') || null;
        }
        """
    )
    return _clean_detail_recipient_name(str(value or ""))

async def read_detail_product_quantity(page, asin: str | None) -> int | None:
    """从详情页商品信息 DOM 中读取当前 ASIN 所在商品行的 xN 数量。"""
    if not asin:
        return None
    value = await page.evaluate(
        """
        (asin) => {
            const targetAsin = String(asin || '').toUpperCase();
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const parseQuantity = (text) => {
                const raw = String(text || '').replace(/\\s+/g, ' ').trim();
                const direct = raw.match(/^(?:[xX×*]|Qty|QTY|数量)\\s*[:：]?\\s*(\\d{1,4})$/);
                if (direct) return Number(direct[1]);
                const embedded = raw.match(/(?:^|[\\s(（])(?:[xX×*]|Qty|QTY|数量)\\s*[:：]?\\s*(\\d{1,4})(?=$|[\\s)）])/);
                if (embedded) return Number(embedded[1]);
                // 商品数量只认商品行里的 x1 / Qty 1 / 数量 1。
                // “共5/共4”是更多商品信息右侧的附件数量，不能当成商品购买数量，
                // 否则汽车磁贴 2 个装会被误乘成 10 个、8 个。
                return null;
            };
            const quantityFromElement = (el) => {
                const nodes = [el, ...Array.from(el.querySelectorAll('span,div,p,td,th,b,strong,button,label,i,em,[class*="tag"],[class*="Tag"],[class*="badge"],[class*="Badge"]'))]
                    .filter((node) => visible(node))
                    .map((node) => textOf(node))
                    .filter(Boolean)
                    .sort((a, b) => a.length - b.length);
                for (const text of nodes) {
                    const quantity = parseQuantity(text);
                    if (quantity) return quantity;
                }
                return null;
            };
            const quantityNodeSelector = 'span,div,p,td,th,b,strong,button,label,i,em,[class*="tag"],[class*="Tag"],[class*="badge"],[class*="Badge"]';
            const addWithSiblings = (set, el) => {
                if (!el || el === document.body || el === document.documentElement) return;
                set.add(el);
                const parent = el.parentElement;
                if (!parent) return;
                for (const sibling of Array.from(parent.children || [])) {
                    set.add(sibling);
                }
            };
            const addRowScope = (set, el) => {
                if (!el || el === document.body || el === document.documentElement) return;
                set.add(el);
                for (const child of Array.from(el.children || [])) {
                    set.add(child);
                }
            };
            const detailRoots = Array.from(document.querySelectorAll('.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const text = textOf(el);
                    return /系统单号/.test(text) && /商品信息/.test(text) && text.toUpperCase().includes(targetAsin);
                })
                .map((el) => ({ el, text: textOf(el) }))
                .sort((a, b) => a.text.length - b.text.length)
                .map((item) => item.el);
            const root = detailRoots[0] || document.body;
            const rowSelector = 'tr,[role="row"],.vxe-body--row,.el-table__row,.ant-table-row';
            const hasProductRowSignal = (text) => /ASIN|SKU|MSKU|商品ID|商品信息|订单信息|交易信息|更多商品信息/.test(text);
            const isReasonableScope = (text, maxLength = 80000) =>
                text.toUpperCase().includes(targetAsin) && text.length < maxLength;
            const findQuantityBySharedProductScope = () => {
                const quantityNodes = Array.from(root.querySelectorAll(quantityNodeSelector))
                    .filter((el) => visible(el))
                    .map((el) => ({ el, text: textOf(el), quantity: parseQuantity(textOf(el)) }))
                    .filter((item) => item.quantity);
                const matches = [];
                for (const item of quantityNodes) {
                    let node = item.el;
                    for (let depth = 0; depth < 30 && node && node !== document.body; depth += 1) {
                        const text = textOf(node);
                        if (isReasonableScope(text)) {
                            // 商品数量是同一商品 DOM 容器内的 “×N” 小标签。
                            // 反向从数量标签找包含当前 ASIN 的最小容器，比按坐标找更稳，
                            // 也不会把“共N”附件数量当成购买数量。
                            matches.push({
                                quantity: item.quantity,
                                textLength: text.length,
                                hasSignal: hasProductRowSignal(text) ? 0 : 1,
                                depth,
                            });
                            break;
                        }
                        node = node.parentElement;
                    }
                }
                matches.sort((a, b) => a.hasSignal - b.hasSignal || a.textLength - b.textLength || a.depth - b.depth);
                return matches[0]?.quantity || null;
            };
            const sharedQuantity = findQuantityBySharedProductScope();
            if (sharedQuantity) return sharedQuantity;
            const scopes = new Set();
            const asinNodes = Array.from(root.querySelectorAll('span,div,p,td,th,b,strong,a'))
                .filter((el) => visible(el) && textOf(el).toUpperCase().includes(targetAsin));
            for (const asinNode of asinNodes) {
                const row = asinNode.closest(rowSelector);
                if (row && isReasonableScope(textOf(row))) addRowScope(scopes, row);
                else addWithSiblings(scopes, asinNode);
                let node = asinNode;
                for (let depth = 0; depth < 25 && node && node !== document.body; depth += 1) {
                    const text = textOf(node);
                    if (isReasonableScope(text)) {
                        if (node.matches?.(rowSelector)) addRowScope(scopes, node);
                        else if (depth <= 8 || hasProductRowSignal(text)) {
                            // ASIN 通常在商品信息左侧，x1 在同一商品卡片的订单信息区域。
                            // 这里向上扩到真实商品卡片，而不是停在 ASIN 附近的 li/小 div，
                            // 同时仍只在包含当前 ASIN 的范围内读取，避免串到其他商品。
                            addWithSiblings(scopes, node);
                            addRowScope(scopes, node);
                        }
                        else scopes.add(node);
                    }
                    node = node.parentElement;
                }
            }
            const rows = Array.from(root.querySelectorAll(rowSelector))
                .filter((el) => {
                    if (!visible(el)) return false;
                    const text = textOf(el);
                    return isReasonableScope(text);
                })
                .map((el) => ({ el, text: textOf(el) }))
                .sort((a, b) => a.text.length - b.text.length);
            for (const row of rows) {
                addRowScope(scopes, row.el);
            }
            const sortedScopes = Array.from(scopes)
                .filter((el) => visible(el))
                .map((el) => ({ el, text: textOf(el) }))
                .filter((item) => item.text.length < 80000)
                .sort((a, b) => a.text.length - b.text.length);
            for (const scope of sortedScopes) {
                const quantity = quantityFromElement(scope.el) || parseQuantity(scope.text);
                if (quantity) return quantity;
            }
            return null;
        }
        """,
        asin,
    )
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed and parsed > 0 else None

async def collect_order_folder_dom_context(page, asin: str | None) -> dict[str, object]:
    """读取文件夹生成所需的 DOM 上下文，不解释业务规则。"""
    recipient_name = await read_detail_recipient_name(page)
    quantity = await read_detail_product_quantity(page, asin)
    return {
        "recipient_name": recipient_name,
        "tent_quantity": quantity,
        "quantity_fallback": quantity is None,
    }

async def find_contact_from_system_orders(page, system_order_nos: list[str]) -> tuple[str | None, ContactInfo | None, list[str]]:
    """在多个系统订单中查找可用联系方式。"""
    fallback_texts: list[str] = []
    partial_system_order_no: str | None = None
    partial_contact: ContactInfo | None = None
    partial_texts: list[str] = []
    for system_order_no in system_order_nos:
        contact, texts = await extract_contact_from_system_order(page, system_order_no)
        if not fallback_texts:
            fallback_texts = texts
        if not missing_contact_fields(contact):
            return system_order_no, contact, texts
        # 完整联系方式优先；如果全都不完整，保留第一个至少有电话或邮箱的候选，交给 CMD 人工确认。
        if (contact.phone or contact.email) and partial_contact is None:
            partial_system_order_no = system_order_no
            partial_contact = contact
            partial_texts = texts
    if partial_contact is not None:
        return partial_system_order_no, partial_contact, partial_texts
    return None, None, fallback_texts
