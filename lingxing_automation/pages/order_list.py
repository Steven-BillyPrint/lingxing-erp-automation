from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from ..constants import PLATFORM_ORDER_RE, SYSTEM_ORDER_RE
from ..models import BatchOrderItem
from ..parsers.dates import classify_recent_payment_window, latest_payment_text
from ..products.catalog import PRODUCT_TYPE_TENT, extract_asins, match_supported_product
from .diagnostics import save_page_diagnostics


BUYER_CANCEL_REQUEST_TEXT = "买家申请取消"


SPLIT_ORDER_TEXT_RE = re.compile(r"(拆分订单|已拆分|拆分单)")


def _int_or_none(value: object) -> int | None:
    """把文本安全转换为整数，无法转换时返回空值。"""
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed or None


def _matched_product_debug_from_asins(asins: list[str]) -> tuple[object | None, list[str]]:
    """根据识别到的 ASIN 生成产品匹配调试信息。"""
    product_match = match_supported_product(asins)
    matched_asins = [asin for asin in asins if match_supported_product(asin)]
    return product_match, matched_asins


def _unknown_asins_from_matches(all_asins: list[str], matched_asins: list[str]) -> list[str]:
    """返回已识别但未命中当前支持商品表的 ASIN，保留首次出现顺序。"""

    matched = set(matched_asins)
    unknown: list[str] = []
    seen: set[str] = set()
    for asin in all_asins:
        if asin in matched or asin in seen:
            continue
        seen.add(asin)
        unknown.append(asin)
    return unknown


def _row_has_buyer_cancel_request(row: dict[str, object]) -> bool:
    status_text = str(row.get("status_text", "") or "")
    return BUYER_CANCEL_REQUEST_TEXT in status_text


def _record_unknown_asins(
    debug: dict | None,
    unknown_asins: list[str],
    *,
    platform_order_no: str,
    system_order_no: str,
    sku: str,
    payment_time: str | None,
    source_page: object = None,
    source_scroll_top: object = None,
) -> None:
    """在扫描 debug 中按本轮全局去重记录未知 ASIN 的首次出现位置。"""

    if debug is None or not unknown_asins:
        return
    entries = debug.setdefault("unknown_asins", [])
    existing = {
        str(entry.get("asin") or "").upper()
        for entry in entries
        if isinstance(entry, dict)
    }
    for asin in unknown_asins:
        normalized = str(asin or "").strip().upper()
        if not normalized or normalized in existing:
            continue
        entries.append(
            {
                "asin": normalized,
                "platform_order_no": platform_order_no,
                "system_order_no": system_order_no,
                "sku": sku,
                "payment_time": payment_time,
                "source_page": _int_or_none(source_page),
                "source_scroll_top": int(source_scroll_top or 0),
            }
        )
        existing.add(normalized)


def _row_supported_product_debug(row: dict[str, object]) -> dict[str, object]:
    """记录列表行命中的产品类型，方便区分“未识别”和“有标签被跳过”。

    标签非空的订单会在预扫描阶段直接跳过，不进入候选聚合；
    因此这里必须在跳过前先识别 ASIN，否则日志里会看起来像没有遍历到喷绘订单。
    """
    asin_source = "\n".join(
        str(row.get(key, ""))
        for key in ("asin", "asin_text", "row_text", "sku")
        if str(row.get(key, "")).strip()
    )
    all_asins = extract_asins(asin_source)
    product_match, matched_asins = _matched_product_debug_from_asins(all_asins)
    unknown_asins = _unknown_asins_from_matches(all_asins, matched_asins)
    return {
        "all_asins": all_asins,
        "unknown_asins": unknown_asins,
        "matched_tent_asins": matched_asins,
        "matched_product_asins": matched_asins,
        "matched_asin": getattr(product_match, "asin", "") if product_match else "",
        "parent_asin": getattr(product_match, "parent_asin", "") if product_match else "",
        "product_type": getattr(product_match, "product_type", "") if product_match else "",
    }


def _mark_group_skip(debug: dict | None, reason: str, items: list[dict[str, object]], extra: dict | None = None) -> None:
    """把平台单号聚合后的跳过原因写入扫描日志，方便复盘为什么没命中。"""
    if debug is None or not items:
        return
    platform_order_no = str(items[0].get("platform_order_no", ""))
    system_order_nos = {str(item.get("system_order_no", "")) for item in items}
    for scan_row in debug.get("scan_rows", []):
        if scan_row.get("platform_order_no") == platform_order_no and scan_row.get("system_order_no") in system_order_nos:
            scan_row["skip_reason"] = reason
            if extra:
                scan_row.update({key: value for key, value in extra.items() if key not in {"row_excerpt"}})
    skip_counts = debug.setdefault("skip_counts", {})
    skip_counts[reason] = int(skip_counts.get(reason, 0)) + 1
    preview = debug.setdefault("skip_preview", [])
    if len(preview) < 30:
        preview.append(
            {
                "reason": reason,
                "platform_order_no": platform_order_no,
                "system_order_nos": sorted(system_order_nos),
                "asin_text": " ".join(str(item.get("asin_text", "")) for item in items)[:240],
                "paid_at_text": latest_payment_text("\n".join(str(item.get("paid_at_text", "")) for item in items)),
                "row_excerpt": "\n".join(str(item.get("row_text", "")) for item in items)[:240],
                **(extra or {}),
            }
        )


def build_batch_candidates_from_rows(
    raw_items: list[dict[str, object]],
    processed_platform_orders: set[str],
    limit: int = 0,
    payment_window_hours: float = 24,
    debug: dict | None = None,
    ignore_tags: bool = False,
    ignore_processed: bool = False,
    ignore_payment_window: bool = False,
    force_retry_order_no: str | None = None,
) -> list[BatchOrderItem]:
    """按平台单号聚合后筛选候选订单。

    业务规则：
    - 拆分订单不处理：同平台单号出现多个系统单号，或行文本带拆分标记。
    - 未拆分多商品可以处理：聚合后的 ASIN 只要包含任一已支持商品父/子 ASIN 就命中。
    """
    groups: dict[str, list[dict[str, object]]] = {}
    for item in raw_items:
        platform_order_no = str(item.get("platform_order_no", ""))
        system_order_no = str(item.get("system_order_no", ""))
        if not PLATFORM_ORDER_RE.fullmatch(platform_order_no) or not SYSTEM_ORDER_RE.fullmatch(system_order_no):
            _mark_group_skip(debug, "invalid_or_truncated_order_no", [item])
            continue
        groups.setdefault(platform_order_no, []).append(item)

    candidates: list[BatchOrderItem] = []
    group_logs: list[dict[str, object]] = []
    for platform_order_no, items in groups.items():
        force_retry_candidate = bool(force_retry_order_no and platform_order_no == force_retry_order_no)
        system_order_nos = sorted({str(item.get("system_order_no", "")) for item in items if item.get("system_order_no")})
        combined_row_text = "\n".join(str(item.get("row_text", "")) for item in items)
        combined_asin_text = "\n".join(
            f"{item.get('asin_text', '')}\n{item.get('row_text', '')}" for item in items
        )
        combined_sku = " | ".join(dict.fromkeys(str(item.get("sku", "")).strip() for item in items if str(item.get("sku", "")).strip()))
        # 提取客选物流（聚合去重）
        combined_logistics = " | ".join(dict.fromkeys(str(item.get("logistics", "")).strip() for item in items if str(item.get("logistics", "")).strip()))
        # 标签列是人工/系统处理状态标记；只要有内容就视为已处理过，不再进入本轮待修改列表。
        combined_tag_text = " | ".join(dict.fromkeys(str(item.get("tag_text", "")).strip() for item in items if str(item.get("tag_text", "")).strip()))
        combined_status_text = " | ".join(dict.fromkeys(str(item.get("status_text", "")).strip() for item in items if str(item.get("status_text", "")).strip()))
        buyer_cancel_requested = any(_row_has_buyer_cancel_request(item) for item in items)
        payment_text = "\n".join(
            f"付款时间 {item.get('paid_at_text')}" if item.get("paid_at_text") else str(item.get("row_text", ""))
            for item in items
        )
        all_asins = extract_asins(combined_asin_text)
        product_match, matched_asins = _matched_product_debug_from_asins(all_asins)
        unknown_asins = _unknown_asins_from_matches(all_asins, matched_asins)
        split_order = len(system_order_nos) > 1 or bool(SPLIT_ORDER_TEXT_RE.search(combined_row_text))
        payment_status = classify_recent_payment_window(payment_text, hours=payment_window_hours)
        skip_reason = ""
        paid_at_text = latest_payment_text(payment_text)
        primary_item = items[0]
        primary_system_order_no = system_order_nos[0] if system_order_nos else str(primary_item.get("system_order_no", ""))
        _record_unknown_asins(
            debug,
            unknown_asins,
            platform_order_no=platform_order_no,
            system_order_no=primary_system_order_no,
            sku=combined_sku,
            payment_time=paid_at_text,
            source_page=primary_item.get("source_page"),
            source_scroll_top=primary_item.get("source_scroll_top"),
        )
        group_log: dict[str, object] = {
            "platform_order_no": platform_order_no,
            "system_order_nos": system_order_nos,
            "system_order_count": len(system_order_nos),
            "asins": all_asins,
            "unknown_asins": unknown_asins,
            "matched_tent_asins": matched_asins,
            "matched_product_asins": matched_asins,
            "matched_asin": product_match.asin if product_match else "",
            "parent_asin": product_match.parent_asin if product_match else "",
            "product_type": product_match.product_type if product_match else "",
            "logistics": combined_logistics,
            "status_text": combined_status_text,
            "tag_text": combined_tag_text,
            "buyer_cancel_requested": buyer_cancel_requested,
            "is_split_order": split_order,
            "payment_status": payment_status,
            "paid_at_text": paid_at_text,
            "hit": False,
        }

        # 安全重测会复用批量链路，但允许真实重跑已打标签/已完成/历史订单；
        # 普通批量巡检仍保持严格跳过，避免重复修改生产订单。
        if buyer_cancel_requested:
            skip_reason = "buyer_cancel_requested"
        elif combined_tag_text and not ignore_tags:
            skip_reason = "has_tag"
        elif platform_order_no in processed_platform_orders and not ignore_processed:
            skip_reason = "already_processed_or_duplicate"
        elif split_order:
            skip_reason = "split_order"
        elif not product_match and not force_retry_candidate:
            skip_reason = "not_tent_asin"
        elif payment_status != "recent" and not ignore_payment_window:
            skip_reason = f"payment_{payment_status}"

        if skip_reason:
            group_log["skip_reason"] = skip_reason
            _mark_group_skip(
                debug,
                skip_reason,
                items,
                {
                    "is_split_order": split_order,
                    "matched_tent_asins": matched_asins,
                    "matched_product_asins": matched_asins,
                    "unknown_asins": unknown_asins,
                    "matched_asin": product_match.asin if product_match else "",
                    "parent_asin": product_match.parent_asin if product_match else "",
                    "product_type": product_match.product_type if product_match else "",
                    "status_text": combined_status_text,
                    "tag_text": combined_tag_text,
                    "buyer_cancel_requested": buyer_cancel_requested,
                },
            )
            group_logs.append(group_log)
            continue

        primary = next(
            (
                item
                for item in items
                if product_match and product_match.asin in f"{item.get('asin_text', '')} {item.get('row_text', '')}".upper()
            ),
            items[0],
        )
        candidate = BatchOrderItem(
            system_order_no=str(primary.get("system_order_no", "")),
            platform_order_no=platform_order_no,
            row_text=combined_row_text,
            paid_at_text=paid_at_text,
            asin=product_match.asin if product_match else (all_asins[0] if all_asins else None),
            sku=combined_sku or None,
            logistics=combined_logistics or None,
            tag_text=combined_tag_text or None,
            parent_asin=product_match.parent_asin if product_match else None,
            product_type=product_match.product_type if product_match else PRODUCT_TYPE_TENT,
            source_page=_int_or_none(primary.get("source_page")),
            source_scroll_top=int(primary.get("source_scroll_top") or 0),
            matched_asins=matched_asins,
            all_asins=all_asins,
        )
        candidates.append(candidate)
        group_log["hit"] = True
        group_log["parent_asin"] = candidate.parent_asin
        group_log["matched_asin"] = candidate.asin
        group_log["product_type"] = candidate.product_type
        if force_retry_candidate and not product_match:
            group_log["forced_retry_candidate"] = True
            group_log["skip_reason"] = ""
        group_logs.append(group_log)
        if debug is not None:
            for scan_row in debug.get("scan_rows", []):
                if scan_row.get("platform_order_no") == platform_order_no:
                    scan_row["hit"] = True
                    scan_row["parent_asin"] = candidate.parent_asin
                    scan_row["matched_asin"] = candidate.asin
                    scan_row["matched_tent_asins"] = matched_asins
                    scan_row["matched_product_asins"] = matched_asins
                    scan_row["unknown_asins"] = unknown_asins
                    scan_row["product_type"] = candidate.product_type
                    scan_row["logistics"] = candidate.logistics
                    scan_row["tag_text"] = candidate.tag_text
                    scan_row["skip_reason"] = ""
        if limit and len(candidates) >= limit:
            break

    if debug is not None:
        debug["platform_groups"] = group_logs
    return candidates


ORDER_TABLE_PROBE_JS = r"""
({ action, sourcePage, sourceScrollTop, scrollMode }) => {
    const systemRe = /\b\d{15,24}\b/g;
    const platformRe = /\b\d{3}-\d{7}-\d{7}\b/g;
    const asinRe = /\bB0[A-Z0-9]{8}\b/gi;
    const systemTestRe = /\b\d{15,24}\b/;
    const platformTestRe = /\b\d{3}-\d{7}-\d{7}\b/;
    const asinTestRe = /\bB0[A-Z0-9]{8}\b/i;
    const dateRe = /\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?/;
    const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\s+/g, ' ').trim();
    const visibleBox = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 &&
            style.visibility !== 'hidden' && style.display !== 'none';
    };
    const cssPath = (el) => {
        if (!el || el.nodeType !== 1) return '';
        const parts = [];
        let node = el;
        for (let depth = 0; depth < 7 && node && node.nodeType === 1 && node !== document.body; depth += 1) {
            const tag = node.tagName.toLowerCase();
            const id = node.id ? `#${CSS.escape(node.id)}` : '';
            const cls = String(node.className || '')
                .split(/\s+/)
                .filter(Boolean)
                .slice(0, 3)
                .map((name) => `.${CSS.escape(name)}`)
                .join('');
            const parent = node.parentElement;
            let nth = '';
            if (parent && !id) {
                const siblings = Array.from(parent.children).filter((item) => item.tagName === node.tagName);
                if (siblings.length > 1) nth = `:nth-of-type(${siblings.indexOf(node) + 1})`;
            }
            parts.unshift(`${tag}${id}${cls}${nth}`);
            if (id) break;
            node = parent;
        }
        return parts.join(' > ');
    };
    const uniqueByElement = (items) => items.filter((item, index, all) => all.findIndex((other) => other.el === item.el) === index);
    const cleanHeader = (text) => text.replace(/[↕↑↓⇅]+/g, '').replace(/\s+/g, ' ').trim();
    const isHeaderText = (text) => /系统单号|平台单号|平台订单号|商品|SKU|状态|标签|剩余发货|客服备注|付款时间|客选物流|ASIN\s*\/\s*商品ID|ASIN|商品ID|店铺|站点|操作/.test(text);
    const headerNodesFor = (root) => {
        const selectors = [
            'th',
            '[role="columnheader"]',
            '.vxe-header--column',
            '.vxe-table--header-wrapper .vxe-cell',
            '.vxe-table--header-wrapper span',
            '.el-table__header-wrapper th',
            '.el-table__header-wrapper .cell',
            '.el-table__header-wrapper span',
            '.ant-table-thead th',
            '.ant-table-thead span',
        ].join(',');
        const nodes = uniqueByElement(Array.from(root.querySelectorAll(selectors)).map((el) => ({ el }))).map(({ el }) => {
            const cell = el.closest('th,[role="columnheader"],.vxe-header--column,.el-table__cell') || el;
            const rect = cell.getBoundingClientRect();
            const text = cleanHeader(textOf(cell) || textOf(el));
            return { el: cell, text, rect };
        });
        const headers = nodes
            .filter((item) => item.text && item.text.length <= 120 && isHeaderText(item.text) && item.rect.width > 0 && item.rect.height > 0)
            .filter((item, index, all) => all.findIndex((other) =>
                other.el === item.el ||
                (other.text === item.text && Math.abs(other.rect.left - item.rect.left) < 3 && Math.abs(other.rect.top - item.rect.top) < 3)
            ) === index)
            .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
        return headers;
    };
    const rowLike = (el) => {
        if (!el || el === document.body) return false;
        const className = String(el.className || '');
        const role = el.getAttribute?.('role');
        const tag = el.tagName.toLowerCase();
        return tag === 'tr' || role === 'row' || /row|vxe-body--row|el-table__row/i.test(className);
    };
    const rowNodesFor = (root) => {
        const nodes = uniqueByElement(Array.from(root.querySelectorAll('tbody tr,[role="row"],.vxe-body--row,.el-table__row')).map((el) => ({ el })))
            .map(({ el }) => ({ el, rect: el.getBoundingClientRect(), text: textOf(el) }))
            .filter((item) =>
                item.text &&
                item.rect.width > 0 &&
                item.rect.height > 0 &&
                !/^系统单号\s+平台单号/.test(item.text) &&
                (platformTestRe.test(item.text) || systemTestRe.test(item.text) || asinTestRe.test(item.text) || dateRe.test(item.text))
            );
        platformRe.lastIndex = 0;
        systemRe.lastIndex = 0;
        asinRe.lastIndex = 0;
        return nodes.sort((a, b) => a.rect.top - b.rect.top);
    };
    const scrollablesFor = (root) => Array.from(root.querySelectorAll('*'))
        .concat([root])
        .filter((el) => {
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 80 && rect.height > 20 &&
                style.visibility !== 'hidden' && style.display !== 'none' &&
                (el.scrollHeight > el.clientHeight + 8 || el.scrollWidth > el.clientWidth + 8);
        })
        .map((el) => ({
            el,
            selector: cssPath(el),
            scrollHeight: Math.round(el.scrollHeight),
            clientHeight: Math.round(el.clientHeight),
            scrollTop: Math.round(el.scrollTop),
            scrollWidth: Math.round(el.scrollWidth),
            clientWidth: Math.round(el.clientWidth),
            scrollLeft: Math.round(el.scrollLeft),
            vertical: el.scrollHeight > el.clientHeight + 8,
            horizontal: el.scrollWidth > el.clientWidth + 8,
        }))
        .sort((a, b) => (b.scrollHeight - b.clientHeight + b.scrollWidth - b.clientWidth) - (a.scrollHeight - a.clientHeight + a.scrollWidth - a.clientWidth));
    const columnIndexes = (headers) => {
        const texts = headers.map((item) => item.text);
        const find = (pattern) => texts.findIndex((text) => pattern.test(text));
        return {
            system: find(/系统单号/),
            platform: find(/平台单号|平台订单号/),
            sku: find(/^SKU$|SKU/),
            status: find(/状态/),
            tag: find(/标签/),
            customerRemark: find(/客服备注/),
            payment: find(/付款时间|付款/),
            logistics: find(/客选物流/),
            asin: find(/ASIN\s*\/\s*商品ID|ASIN|商品ID/),
        };
    };
    const paymentHeaderFor = (root) => {
        const headers = headerNodesFor(root);
        const header = headers.find((item) => /付款时间|付款/.test(item.text)) || null;
        return { headers, header };
    };
    const sortStateFromText = (value) => {
        const text = String(value || '').toLowerCase();
        if (!text) return '';
        if (/(descending|desc|降序|倒序|↓|▼)/i.test(text)) return 'desc';
        if (/(ascending|asc|升序|正序|↑|▲)/i.test(text)) return 'asc';
        return '';
    };
    const sortStateFromClass = (value) => {
        const classText = String(value || '').toLowerCase();
        if (!classText) return '';
        const tokens = classText.split(/\s+/).filter(Boolean);
        if (tokens.some((token) =>
            /^(descending|desc|is-desc|sort-desc|sort--desc|col--desc|is--desc|order-desc|descend|is-descending)$/.test(token) ||
            /(^|[-_])(descending|desc)([-_]|$)/.test(token)
        )) return 'desc';
        if (tokens.some((token) =>
            /^(ascending|asc|is-asc|sort-asc|sort--asc|col--asc|is--asc|order-asc|ascend|is-ascending)$/.test(token) ||
            /(^|[-_])(ascending|asc)([-_]|$)/.test(token)
        )) return 'asc';
        return '';
    };
    const activeIconSortState = (headerEl) => {
        const selectors = [
            '.active',
            '.is-active',
            '.sort--active',
            '.is--active',
            '[aria-pressed="true"]',
            '[aria-selected="true"]',
            '[data-active="true"]',
            '.ant-table-column-sorter-up.active',
            '.ant-table-column-sorter-down.active',
            '.vxe-sort--asc-btn.sort--active',
            '.vxe-sort--desc-btn.sort--active',
            '[class*="sort"][class*="active"]',
        ].join(',');
        const activeNodes = Array.from(headerEl.querySelectorAll(selectors)).filter(visibleBox);
        for (const node of activeNodes) {
            const descriptor = [
                node.getAttribute?.('aria-label'),
                node.getAttribute?.('title'),
                node.getAttribute?.('data-sort'),
                node.getAttribute?.('data-order'),
                node.getAttribute?.('data-value'),
                node.className,
                textOf(node),
            ].join(' ');
            const state = sortStateFromText(descriptor) || sortStateFromClass(descriptor);
            if (state) {
                return { state, selector: cssPath(node), className: String(node.className || ''), text: textOf(node) };
            }
        }
        return null;
    };
    const sortStateForHeader = (header) => {
        if (!header) return { ok: false, state: 'missing', reason: '没有找到付款时间表头。' };
        const evidence = [];
        let node = header.el;
        for (let depth = 0; depth < 8 && node && node !== document.body; depth += 1) {
            const descriptor = [
                node.getAttribute?.('aria-sort'),
                node.getAttribute?.('data-sort'),
                node.getAttribute?.('data-order'),
                node.getAttribute?.('sort-order'),
                node.getAttribute?.('aria-label'),
                node.getAttribute?.('title'),
                node.className,
            ].join(' ');
            const attrState = sortStateFromText(descriptor) || sortStateFromClass(descriptor);
            evidence.push({
                depth,
                selector: cssPath(node),
                tag: node.tagName.toLowerCase(),
                className: String(node.className || '').slice(0, 200),
                ariaSort: node.getAttribute?.('aria-sort') || '',
                dataSort: node.getAttribute?.('data-sort') || '',
                dataOrder: node.getAttribute?.('data-order') || '',
                state: attrState || '',
            });
            if (attrState) {
                return {
                    ok: true,
                    state: attrState,
                    source: 'header_attribute_or_class',
                    headerText: header.text,
                    headerSelector: cssPath(header.el),
                    evidence,
                };
            }
            node = node.parentElement;
        }
        const icon = activeIconSortState(header.el);
        if (icon?.state) {
            return {
                ok: true,
                state: icon.state,
                source: 'active_sort_icon',
                headerText: header.text,
                headerSelector: cssPath(header.el),
                activeIcon: icon,
                evidence,
            };
        }
        const rawHeaderText = textOf(header.el);
        const symbolState = /付款时间[^↑↓▲▼]*[↓▼]/.test(rawHeaderText) ? 'desc' :
            (/付款时间[^↑↓▲▼]*[↑▲]/.test(rawHeaderText) ? 'asc' : '');
        if (symbolState) {
            return {
                ok: true,
                state: symbolState,
                source: 'header_symbol',
                headerText: header.text,
                headerSelector: cssPath(header.el),
                rawHeaderText,
                evidence,
            };
        }
        return {
            ok: true,
            state: 'none',
            source: 'no_active_sort_state',
            headerText: header.text,
            headerSelector: cssPath(header.el),
            rawHeaderText,
            evidence,
        };
    };
    const findPaymentDescClickTarget = (headerEl) => {
        const candidates = Array.from(headerEl.querySelectorAll([
            '.sort-caret.descending',
            '.vxe-sort--desc-btn',
            '.ant-table-column-sorter-down',
            '[aria-label*="降序"]',
            '[title*="降序"]',
            '[class*="desc"]',
            '[class*="down"]',
        ].join(','))).filter(visibleBox);
        return candidates
            .map((el) => {
                const descriptor = [
                    el.getAttribute?.('aria-label'),
                    el.getAttribute?.('title'),
                    el.className,
                    textOf(el),
                ].join(' ');
                let score = 0;
                if (/sort-caret/.test(String(el.className || ''))) score += 20;
                if (/desc|descending|down|降序/i.test(descriptor)) score += 80;
                if (/active|is-active|sort--active/i.test(descriptor)) score += 10;
                const rect = el.getBoundingClientRect();
                return { el, score, top: rect.top, left: rect.left };
            })
            .sort((a, b) => b.score - a.score || a.top - b.top || a.left - b.left)[0]?.el || headerEl;
    };
    const seedSet = new Set();
    const addSeed = (el) => {
        if (!el || el === document.body || el === document.documentElement) return;
        const rect = el.getBoundingClientRect();
        if (rect.width < 300 || rect.height < 80) return;
        seedSet.add(el);
    };
    Array.from(document.querySelectorAll('.vxe-table,.vxe-grid,.el-table,.ant-table,[role="grid"],table,[class*="table"],[class*="Table"]')).forEach(addSeed);
    Array.from(document.querySelectorAll('th,[role="columnheader"],.vxe-header--column,.el-table__header-wrapper span,.el-table__header-wrapper th,span,div'))
        .filter((el) => isHeaderText(cleanHeader(textOf(el))))
        .forEach((el) => {
            let node = el;
            for (let i = 0; i < 10 && node && node !== document.body; i += 1) {
                const text = textOf(node);
                if (/系统单号/.test(text) && /平台单号|平台订单号/.test(text)) addSeed(node);
                node = node.parentElement;
            }
        });
    Array.from(document.querySelectorAll('tr,[role="row"],.vxe-body--row,.el-table__row,a,td,div'))
        .filter((el) => {
            const text = textOf(el);
            return platformTestRe.test(text) || (asinTestRe.test(text) && dateRe.test(text));
        })
        .slice(0, 80)
        .forEach((el) => {
            platformRe.lastIndex = 0;
            asinRe.lastIndex = 0;
            let node = el;
            for (let i = 0; i < 10 && node && node !== document.body; i += 1) {
                if (rowLike(node)) addSeed(node.parentElement);
                const text = textOf(node);
                if (/系统单号/.test(text) && /平台单号|平台订单号/.test(text)) addSeed(node);
                node = node.parentElement;
            }
        });
    const candidates = Array.from(seedSet)
        .map((root, index) => {
            const rect = root.getBoundingClientRect();
            const headers = headerNodesFor(root);
            const rows = rowNodesFor(root);
            const indexes = columnIndexes(headers);
            const scrollables = scrollablesFor(root).slice(0, 8).map(({ el, ...rest }) => rest);
            const headerTexts = headers.map((item) => item.text);
            const rowTexts = rows.slice(0, 5).map((item) => item.text.slice(0, 500));
            const joinedRows = rowTexts.join(' ');
            let score = 0;
            if (indexes.platform >= 0) score += 40;
            if (indexes.payment >= 0) score += 40;
            if (indexes.asin >= 0) score += 40;
            if (indexes.tag >= 0) score += 20;
            if (indexes.system >= 0) score += 20;
            if (platformTestRe.test(joinedRows)) score += 25;
            if (asinTestRe.test(joinedRows)) score += 25;
            if (dateRe.test(joinedRows)) score += 25;
            if (rows.length) score += Math.min(30, rows.length);
            if (scrollables.some((item) => item.vertical)) score += 8;
            if (scrollables.some((item) => item.horizontal)) score += 8;
            if (/vxe|el-table|ant-table|table/i.test(String(root.className || '') + root.tagName)) score += 8;
            platformRe.lastIndex = 0;
            asinRe.lastIndex = 0;
            return {
                index,
                root,
                score,
                selector: cssPath(root),
                tag: root.tagName.toLowerCase(),
                id: root.id || '',
                className: String(root.className || '').slice(0, 240),
                rect: {
                    left: Math.round(rect.left),
                    top: Math.round(rect.top),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
                headers: headerTexts,
                column_indexes: indexes,
                first_rows: rowTexts,
                row_count_visible: rows.length,
                scrollables,
                is_scrollable: scrollables.some((item) => item.vertical || item.horizontal),
            };
        })
        .filter((item) => item.headers.length || item.first_rows.length)
        .sort((a, b) => b.score - a.score || b.row_count_visible - a.row_count_visible);
    const selected = candidates[0] || null;
    const publicCandidates = candidates.slice(0, 20).map(({ root, ...item }) => item);
    const publicSelected = selected ? publicCandidates.find((item) => item.index === selected.index) || null : null;
    const selectedRoot = selected?.root || null;

    const findHorizontalScrollable = (root) => scrollablesFor(root)
        .filter((item) => item.horizontal)
        .map((item) => item.el)
        .sort((a, b) => (b.scrollWidth - b.clientWidth) - (a.scrollWidth - a.clientWidth))[0] || null;
    const findVerticalScrollable = (root) => scrollablesFor(root)
        .filter((item) => item.vertical)
        .map((item) => item.el)
        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0] || null;

    if (action === 'probe') {
        return { selected: publicSelected, candidates: publicCandidates };
    }
    if (!selectedRoot) {
        return { ok: false, reason: '没有探测到真实订单表格。', selected: null, candidates: publicCandidates };
    }
    if (action === 'scroll_horizontal') {
        const el = findHorizontalScrollable(selectedRoot);
        if (!el) return { ok: false, changed: false, reason: '真实订单表格没有横向滚动容器。', selected: publicSelected };
        const max = Math.max(0, el.scrollWidth - el.clientWidth);
        const old = el.scrollLeft;
        let next = old;
        if (scrollMode === 'start') next = 0;
        else if (scrollMode === 'end') next = max;
        else next = Math.min(max, old + Math.max(420, Math.round(el.clientWidth * 0.35)));
        el.scrollLeft = next;
        el.dispatchEvent(new Event('scroll', { bubbles: true }));
        return { ok: true, changed: Math.abs(next - old) > 4, scrollLeft: Math.round(next), maxScrollLeft: Math.round(max), selected: publicSelected };
    }
    if (action === 'reset_vertical') {
        const el = findVerticalScrollable(selectedRoot);
        if (!el) return { ok: false, reason: '真实订单表格没有纵向滚动容器。', selected: publicSelected };
        el.scrollTop = 0;
        el.dispatchEvent(new Event('scroll', { bubbles: true }));
        return {
            ok: true,
            scrollTop: Math.round(el.scrollTop),
            maxScrollTop: Math.round(el.scrollHeight - el.clientHeight),
            clientHeight: Math.round(el.clientHeight),
            selected: publicSelected,
        };
    }
    if (action === 'scroll_vertical') {
        const el = findVerticalScrollable(selectedRoot);
        if (!el) return { ok: false, changed: false, end: true, reason: '真实订单表格没有纵向滚动容器。', selected: publicSelected };
        const max = Math.max(0, el.scrollHeight - el.clientHeight);
        const old = el.scrollTop;
        // 半屏滚动并保留重叠区域，兼容虚拟表格 DOM 复用，避免跨屏漏行。
        const step = Math.max(220, Math.round(el.clientHeight * 0.55));
        const next = Math.min(max, old + step);
        el.scrollTop = next;
        el.dispatchEvent(new Event('scroll', { bubbles: true }));
        return {
            ok: true,
            changed: Math.abs(next - old) > 4,
            end: max - next <= 4,
            scrollTop: Math.round(next),
            maxScrollTop: Math.round(max),
            clientHeight: Math.round(el.clientHeight),
            selected: publicSelected,
        };
    }
    if (action === 'payment_sort_state') {
        const { headers, header } = paymentHeaderFor(selectedRoot);
        const state = sortStateForHeader(header);
        return { ...state, headers: headers.map((item) => item.text), selected: publicSelected };
    }
    if (action === 'click_payment_desc') {
        const { headers, header } = paymentHeaderFor(selectedRoot);
        if (!header) return { ok: false, reason: '真实订单表格中没有付款时间表头。', selected: publicSelected };
        const beforeState = sortStateForHeader(header);
        if (beforeState.state === 'desc') {
            return { ok: true, changed: false, headerText: header.text, sortStateBefore: beforeState, headers: headers.map((item) => item.text), selected: publicSelected };
        }
        const target = findPaymentDescClickTarget(header.el);
        target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
        target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
        target.click();
        return {
            ok: true,
            changed: true,
            headerText: header.text,
            targetSelector: cssPath(target),
            targetClassName: String(target.className || ''),
            sortStateBefore: beforeState,
            headers: headers.map((item) => item.text),
            selected: publicSelected,
        };
    }
    if (action === 'collect_rows') {
        const headers = headerNodesFor(selectedRoot);
        const indexes = columnIndexes(headers);
        const columnValue = (cells, header) => {
            if (!header) return '';
            const left = header.rect.left;
            const right = header.rect.right;
            const center = (left + right) / 2;
            const scored = cells.map((cell) => {
                const overlap = Math.max(0, Math.min(right, cell.rect.right) - Math.max(left, cell.rect.left));
                const cellCenter = (cell.rect.left + cell.rect.right) / 2;
                const centerInside = cell.rect.left <= center && cell.rect.right >= center ? 1 : 0;
                const distance = Math.abs(cellCenter - center);
                return { ...cell, score: overlap * 10 + centerInside * 1000 - distance };
            }).sort((a, b) => b.score - a.score);
            return scored[0]?.score > -320 ? scored[0].text : '';
        };
        const columnValueStrict = (cells, header) => {
            if (!header) return '';
            // 标签列为空时，不能把右侧“剩余发货/客服备注”等邻近列误读成标签；
            // 因此这里要求单元格必须和表头有真实重叠，专门用于标签列。
            const left = header.rect.left;
            const right = header.rect.right;
            const center = (left + right) / 2;
            const scored = cells.map((cell) => {
                const overlap = Math.max(0, Math.min(right, cell.rect.right) - Math.max(left, cell.rect.left));
                const cellCenter = (cell.rect.left + cell.rect.right) / 2;
                const centerInside = cell.rect.left <= center && cell.rect.right >= center ? 1 : 0;
                const distance = Math.abs(cellCenter - center);
                return { ...cell, overlap, centerInside, score: overlap * 10 + centerInside * 1000 - distance };
            })
                .filter((cell) => cell.overlap > 2 || cell.centerInside)
                .sort((a, b) => b.score - a.score);
            return scored[0]?.text || '';
        };
        const rowidFor = (el) => {
            let node = el;
            for (let depth = 0; depth < 10 && node && node !== document.body; depth += 1) {
                const value = node.getAttribute?.('rowid') ||
                    node.getAttribute?.('data-rowid') ||
                    node.getAttribute?.('data-row-id') ||
                    '';
                if (value) return String(value).trim();
                node = node.parentElement;
            }
            return '';
        };
        const cellLike = (el) => {
            const self = el.matches?.('td,[role="gridcell"],.vxe-body--column,.el-table__cell');
            return self ? el : (el.closest?.('td,[role="gridcell"],.vxe-body--column,.el-table__cell') || el);
        };
        const rowCells = (row) => {
            const nodes = Array.from(row.querySelectorAll('td,[role="gridcell"],.vxe-body--column,.el-table__cell'));
            return nodes.map(cellLike)
                .filter((cell, index, all) => all.indexOf(cell) === index)
                .map((cell) => ({ text: textOf(cell), rect: cell.getBoundingClientRect(), rowid: rowidFor(cell) || rowidFor(row) }))
                .filter((cell) => cell.text && cell.rect.width > 0 && cell.rect.height > 0);
        };
        const headerBy = (pattern) => headers.find((item) => pattern.test(item.text)) || null;
        const headerMap = {
            system: headerBy(/系统单号/),
            platform: headerBy(/平台单号|平台订单号/),
            sku: headerBy(/^SKU$|SKU/),
            status: headerBy(/状态/),
            tag: headerBy(/标签/),
            customerRemark: headerBy(/客服备注/),
            payment: headerBy(/付款时间|付款/),
            logistics: headerBy(/客选物流/),
            asin: headerBy(/ASIN\s*\/\s*商品ID|ASIN|商品ID/),
        };
        const headerRects = headers.map((item) => item.rect).filter((rect) => rect.width > 0 && rect.height > 0);
        const headerBottom = headerRects.length ? Math.max(...headerRects.map((rect) => rect.bottom)) : 300;
        const tableLeft = headerRects.length ? Math.min(...headerRects.map((rect) => rect.left)) - 24 : 0;
        const tableRight = headerRects.length ? Math.max(...headerRects.map((rect) => rect.right)) + 80 : window.innerWidth;
        const visibleCell = (el) => {
            if (!visibleBox(el)) return false;
            const rect = el.getBoundingClientRect();
            return rect.bottom > headerBottom - 2 &&
                rect.top < window.innerHeight - 20 &&
                rect.right >= tableLeft &&
                rect.left <= tableRight &&
                rect.height >= 8 &&
                rect.width >= 8;
        };
        const pushGroupCell = (groups, cell, source) => {
            const rect = cell.rect;
            const centerY = (rect.top + rect.bottom) / 2;
            let group = cell.rowid ? groups.find((item) => item.rowid && item.rowid === cell.rowid) : null;
            if (!group) {
                group = groups.find((item) => Math.abs(item.centerY - centerY) <= 18 || Math.abs(item.top - rect.top) <= 10);
            }
            if (!group) {
                group = { centerY, top: rect.top, rowid: cell.rowid || '', texts: [], cells: [], sources: new Set() };
                groups.push(group);
            }
            if (cell.rowid && !group.rowid) group.rowid = cell.rowid;
            group.centerY = Math.min(group.centerY, centerY);
            group.top = Math.min(group.top, rect.top);
            group.sources.add(source);
            group.cells.push(cell);
            if (cell.text) group.texts.push(cell.text);
        };
        const groupedRows = [];
        const cellSelector = [
            'td',
            '[role="gridcell"]',
            '.vxe-body--column',
            '.vxe-table--body-wrapper .vxe-cell',
            '.vxe-table--fixed-left-wrapper .vxe-cell',
            '.vxe-table--fixed-right-wrapper .vxe-cell',
            '.el-table__body-wrapper .el-table__cell',
            '.el-table__fixed-body-wrapper .el-table__cell',
            '.ant-table-tbody td',
        ].join(',');
        const bodyCells = uniqueByElement(Array.from(document.querySelectorAll(cellSelector)).map((el) => ({ el: cellLike(el) })))
            .map(({ el }) => ({ text: textOf(el), rect: el.getBoundingClientRect(), rowid: rowidFor(el), el }))
            .filter((cell) => visibleCell(cell.el) && cell.text && !isHeaderText(cleanHeader(cell.text)));
        for (const cell of bodyCells) {
            pushGroupCell(groupedRows, cell, 'cell');
        }
        const bodyRows = rowNodesFor(document.body).filter((row) =>
            row.rect.bottom > headerBottom - 2 &&
            row.rect.top < window.innerHeight - 20 &&
            row.rect.right >= tableLeft &&
            row.rect.left <= tableRight
        );
        for (const row of bodyRows) {
            const cells = rowCells(row.el).filter((cell) =>
                cell.rect.bottom > headerBottom - 2 &&
                cell.rect.top < window.innerHeight - 20 &&
                cell.rect.right >= tableLeft &&
                cell.rect.left <= tableRight
            );
            if (!cells.length && row.text) {
                cells.push({ text: row.text, rect: row.rect, rowid: rowidFor(row.el) });
            }
            for (const cell of cells) {
                pushGroupCell(groupedRows, cell, 'row');
            }
        }
        const rows = groupedRows
            .sort((a, b) => a.top - b.top)
            .map((group, rowIndex) => {
            const cells = group.cells
                .filter((cell, index, all) =>
                    all.findIndex((other) =>
                        other.text === cell.text &&
                        Math.abs(other.rect.left - cell.rect.left) < 2 &&
                        Math.abs(other.rect.top - cell.rect.top) < 2
                    ) === index
                )
                .sort((a, b) => a.rect.left - b.rect.left);
            const fullRowText = Array.from(new Set([...group.texts, ...cells.map((cell) => cell.text)].filter(Boolean))).join(' ');
            let systemText = columnValue(cells, headerMap.system);
            let platformText = columnValue(cells, headerMap.platform);
            let skuText = columnValue(cells, headerMap.sku);
            let statusText = columnValue(cells, headerMap.status);
            let tagText = columnValueStrict(cells, headerMap.tag);
            let customerRemarkText = columnValue(cells, headerMap.customerRemark);
            let paymentText = columnValue(cells, headerMap.payment);
            let logisticsText = columnValue(cells, headerMap.logistics);
            let asinText = columnValue(cells, headerMap.asin);
            if (!systemText) systemText = (fullRowText.match(systemRe) || [''])[0] || '';
            if (!platformText) platformText = (fullRowText.match(platformRe) || [''])[0] || '';
            if (!paymentText) paymentText = (fullRowText.match(dateRe) || [''])[0] || '';
            if (!asinText) asinText = Array.from(fullRowText.matchAll(asinRe)).map((match) => match[0].toUpperCase()).join(' ');
            const systems = Array.from((systemText || fullRowText).matchAll(systemRe)).map((match) => match[0]);
            const platforms = Array.from((platformText || fullRowText).matchAll(platformRe)).map((match) => match[0]);
            const asins = Array.from((asinText || fullRowText).matchAll(asinRe)).map((match) => match[0].toUpperCase());
            const text = [
                fullRowText,
                systemText ? `系统单号 ${systemText}` : '',
                platformText ? `平台单号 ${platformText}` : '',
                statusText ? `状态 ${statusText}` : '',
                tagText ? `标签 ${tagText}` : '',
                customerRemarkText ? `客服备注 ${customerRemarkText}` : '',
                paymentText ? `付款时间 ${paymentText}` : '',
                logisticsText ? `客选物流 ${logisticsText}` : '',
                asinText ? `ASIN/商品ID ${asinText}` : '',
                skuText ? `SKU ${skuText}` : '',
            ].join(' ').replace(/\s+/g, ' ').trim();
            return {
                row_index: rowIndex + 1,
                rowid: group.rowid || systems[0] || '',
                system_order_no: systems[0] || '',
                platform_order_no: platforms[0] || '',
                row_text: text,
                asin_text: asinText || '',
                asin: asins[0] || '',
                sku: skuText || '',
                status_text: statusText || '',
                tag_text: tagText || '',
                customer_remark: customerRemarkText || '',
                paid_at_text: paymentText || '',
                logistics: logisticsText || '',
                source_page: sourcePage || 1,
                source_scroll_top: sourceScrollTop || 0,
                row_sources: Array.from(group.sources),
                column_headers: {
                    system: headerMap.system?.text || '',
                    platform: headerMap.platform?.text || '',
                    sku: headerMap.sku?.text || '',
                    status: headerMap.status?.text || '',
                    tag: headerMap.tag?.text || '',
                    customer_remark: headerMap.customerRemark?.text || '',
                    payment: headerMap.payment?.text || '',
                    logistics: headerMap.logistics?.text || '',
                    asin: headerMap.asin?.text || '',
                },
            };
        }).filter((row) => row.system_order_no && row.platform_order_no);
        return {
            ok: true,
            selected: publicSelected,
            headers: headers.map((item) => item.text),
            column_indexes: indexes,
            rows,
        };
    }
    return { ok: false, reason: `未知表格动作：${action}`, selected: publicSelected, candidates: publicCandidates };
}
"""


async def order_table_action(page, action: str, **kwargs):
    """定位订单表格中的操作区，供详情打开等动作复用。"""
    payload = {"action": action, **kwargs}
    return await page.evaluate(ORDER_TABLE_PROBE_JS, payload)


async def ensure_order_view_mode(page, debug_dir: str | None = None) -> None:
    """确保订单列表处于自动化流程需要的视图模式。"""
    async def order_view_state() -> dict[str, object]:
        """通过表头文本判断当前视图，避免依赖按钮坐标或窗口宽度。"""
        return dict(
            await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        return (el.offsetWidth > 0 || el.offsetHeight > 0) &&
                            style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const headers = Array.from(document.querySelectorAll([
                        '.vxe-table--header-wrapper th',
                        '.vxe-table--header-wrapper .vxe-header--column',
                        '.vxe-table--header-wrapper span',
                        '.vxe-table--header-wrapper div',
                        '.el-table__header-wrapper th',
                        '.el-table__header-wrapper .el-table__cell',
                        '.ant-table-thead th',
                        'thead th',
                        'th',
                        '[role="columnheader"]',
                    ].join(',')))
                        .filter((el) => visible(el))
                        .map((el) => textOf(el))
                        .filter((text, index, all) => text && text.length <= 120 && all.indexOf(text) === index);
                    const hasOrderHeaders =
                        headers.some((text) => /系统单号|绯荤粺鍗曞彿/.test(text)) &&
                        headers.some((text) => /平台单号|平台订单号|骞冲彴鍗曞彿|骞冲彴璁㈠崟鍙/.test(text));
                    const hasProductHeaders =
                        headers.some((text) => /^图片$|图片|鍥剧墖/.test(text)) &&
                        headers.some((text) => /^商品信息$|商品信息|鍟嗗搧淇℃伅/.test(text)) &&
                        headers.some((text) => /^订单信息$|订单信息|璁㈠崟淇℃伅/.test(text));
                    if (hasOrderHeaders) return { state: 'order', headers: headers.slice(0, 40) };
                    if (hasProductHeaders) return { state: 'product', headers: headers.slice(0, 40) };
                    return { state: 'unknown', headers: headers.slice(0, 40) };
                }
                """
            )
        )

    async def click_order_switch_by_dom() -> dict[str, object]:
        """只在“商品/订单/物流追踪看板”按钮组里点击订单，避免误点首页或页签关闭。"""
        return dict(
            await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        return (el.offsetWidth > 0 || el.offsetHeight > 0) &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.opacity !== '0';
                    };
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const exactOrder = (text) => /^(订单|璁㈠崟)$/.test(text);
                    const hasGoods = (text) => /(商品|鍟嗗搧)/.test(text);
                    const hasOrder = (text) => /(订单|璁㈠崟)/.test(text);
                    const hasBoard = (text) => /(物流追踪看板|缺货看板|鐗╂祦杩借釜鐪嬫澘|缂鸿揣鐪嬫澘)/.test(text);
                    const interactiveSelector = 'button,[role="button"],label,a,.el-button,.ant-btn,.el-radio-button,.el-radio-button__inner,.ant-radio-button-wrapper';
                    const nodes = Array.from(document.querySelectorAll('button,[role="button"],label,a,span,div'));
                    const candidates = [];
                    for (const node of nodes) {
                        if (!visible(node) || !exactOrder(textOf(node))) continue;
                        let group = null;
                        let parent = node.parentElement;
                        for (let depth = 0; depth < 7 && parent && parent !== document.body; depth += 1) {
                            const groupText = textOf(parent);
                            if (hasGoods(groupText) && hasOrder(groupText) && hasBoard(groupText)) {
                                group = parent;
                                break;
                            }
                            parent = parent.parentElement;
                        }
                        if (!group) continue;
                        const target = node.closest(interactiveSelector) || node;
                        candidates.push({
                            target,
                            groupText: textOf(group).slice(0, 160),
                            targetText: textOf(target).slice(0, 80),
                            className: String(target.className || '').slice(0, 160),
                        });
                    }
                    const picked = candidates.sort((a, b) => a.groupText.length - b.groupText.length)[0];
                    if (!picked) {
                        const visibleSwitchTexts = nodes
                            .filter((el) => visible(el))
                            .map((el) => textOf(el))
                            .filter((text) => /商品|订单|物流追踪看板|缺货看板|鍟嗗搧|璁㈠崟|鐗╂祦杩借釜鐪嬫澘|缂鸿揣鐪嬫澘/.test(text))
                            .slice(0, 40);
                        return {
                            ok: false,
                            reason: '没有找到商品/订单/物流追踪看板这一组里的订单按钮。',
                            visibleSwitchTexts,
                        };
                    }
                    picked.target.click();
                    return {
                        ok: true,
                        groupText: picked.groupText,
                        targetText: picked.targetText,
                        className: picked.className,
                    };
                }
                """
            )
        )

    target_debug_dir = debug_dir or "debug/logs"
    state: dict[str, object] = {"state": "unknown", "headers": []}
    probe: dict[str, object] = {}

    # 优先使用表格探测器确认是否已经是订单模式。
    for _ in range(12):
        try:
            probe = await order_table_action(page, "probe")
            selected = probe.get("selected") or {}
            indexes = selected.get("column_indexes") or {}
            headers = selected.get("headers") or []
            if int(indexes.get("system", -1)) >= 0 and int(indexes.get("platform", -1)) >= 0:
                return
            has_system = any("系统单号" in str(text) or "绯荤粺鍗曞彿" in str(text) for text in headers)
            has_platform = any("平台单号" in str(text) or "骞冲彴鍗曞彿" in str(text) for text in headers)
            if has_system and has_platform:
                return
        except Exception:
            pass
        await page.wait_for_timeout(350)

    for _ in range(20):
        state = await order_view_state()
        if state.get("state") == "order":
            return
        if state.get("state") == "product":
            break
        await page.wait_for_timeout(500)

    if state.get("state") != "product":
        message = (
            "没有确认当前是商品模式，也没有识别到订单模式表头，已停止切换以避免误点。"
            f" 当前表头：{state.get('headers', [])}"
        )
        artifacts = await save_page_diagnostics(
            page,
            target_debug_dir,
            "order_mode_unknown",
            message,
            {"state": state, "probe": probe},
        )
        raise RuntimeError(f"{message} 诊断文件：{artifacts.get('diagnostic_file')}")

    last_click: dict[str, object] | None = None
    for attempt in range(4):
        last_click = await click_order_switch_by_dom()
        if last_click.get("ok"):
            for _ in range(12 + attempt * 2):
                await page.wait_for_timeout(500)
                state = await order_view_state()
                if state.get("state") == "order":
                    return
                try:
                    probe = await order_table_action(page, "probe")
                    selected = probe.get("selected") or {}
                    indexes = selected.get("column_indexes") or {}
                    if int(indexes.get("system", -1)) >= 0 and int(indexes.get("platform", -1)) >= 0:
                        return
                except Exception:
                    pass
        await page.wait_for_timeout(700)

    state = await order_view_state()
    message = (
        "没有成功切换到订单模式：请确认右上角“商品 / 订单 / 物流追踪看板”中“订单”按钮可点击。"
        f" 点击信息：{last_click}；当前表头：{state.get('headers', [])}"
    )
    artifacts = await save_page_diagnostics(
        page,
        target_debug_dir,
        "order_mode_switch_failed",
        message,
        {"state": state, "last_click": last_click, "probe": probe},
    )
    raise RuntimeError(f"{message} 诊断文件：{artifacts.get('diagnostic_file')}")


async def ensure_batch_key_columns_visible(page, debug: dict | None = None) -> None:
    """确保批量巡检所需的关键列在订单表格中可见。"""
    probe = await order_table_action(page, "probe")
    if debug is not None:
        debug["table_probe"] = probe
        debug["selected_table"] = probe.get("selected")
        debug["table_candidates"] = probe.get("candidates", [])

    def has_required_columns(selected: dict | None) -> bool:
        """判断当前订单表格是否包含批量流程所需列。"""
        if not selected:
            return False
        indexes = selected.get("column_indexes") or {}
        return (
            int(indexes.get("platform", -1)) >= 0
            and int(indexes.get("payment", -1)) >= 0
            and int(indexes.get("asin", -1)) >= 0
            and int(indexes.get("tag", -1)) >= 0
        )

    if has_required_columns(probe.get("selected")):
        return

    horizontal_steps: list[dict] = []
    for mode in ["start", "step", "step", "step", "end"]:
        state = await order_table_action(page, "scroll_horizontal", scrollMode=mode)
        horizontal_steps.append({"mode": mode, **state})
        await page.wait_for_timeout(450)
        probe = await order_table_action(page, "probe")
        if debug is not None:
            debug["table_probe"] = probe
            debug["selected_table"] = probe.get("selected")
            debug["table_candidates"] = probe.get("candidates", [])
            debug["horizontal_column_scan"] = horizontal_steps
        if has_required_columns(probe.get("selected")):
            return

    selected = probe.get("selected") or {}
    headers = selected.get("headers", [])
    indexes = selected.get("column_indexes", {})
    raise RuntimeError(
        "没有在真实订单表格中同时识别到“平台单号 / 标签 / 付款时间 / ASIN/商品ID”四列。"
        f" 当前选中表格表头：{headers}；列索引：{indexes}。"
    )


async def ensure_order_table_columns_visible(
    page,
    required_columns: list[str] | tuple[str, ...],
    debug: dict | None = None,
) -> None:
    """确保订单表格中指定的结构化列可见。"""
    probe = await order_table_action(page, "probe")
    if debug is not None:
        debug["order_table_column_probe"] = probe
        debug["order_table_selected_table"] = probe.get("selected")

    def has_required_columns(selected: dict | None) -> bool:
        if not selected:
            return False
        indexes = selected.get("column_indexes") or {}
        return all(int(indexes.get(column, -1)) >= 0 for column in required_columns)

    if has_required_columns(probe.get("selected")):
        return

    horizontal_steps: list[dict] = []
    for mode in ["start", "step", "step", "step", "end"]:
        state = await order_table_action(page, "scroll_horizontal", scrollMode=mode)
        horizontal_steps.append({"mode": mode, **state})
        await page.wait_for_timeout(450)
        probe = await order_table_action(page, "probe")
        if debug is not None:
            debug["order_table_column_probe"] = probe
            debug["order_table_selected_table"] = probe.get("selected")
            debug["order_table_horizontal_column_scan"] = horizontal_steps
        if has_required_columns(probe.get("selected")):
            return

    selected = probe.get("selected") or {}
    headers = selected.get("headers", [])
    indexes = selected.get("column_indexes", {})
    missing = [column for column in required_columns if int(indexes.get(column, -1)) < 0]
    raise RuntimeError(
        f"没有在真实订单表格中同时识别到所需列：{', '.join(required_columns)}；缺失：{missing}。"
        f" 当前选中表格表头：{headers}；列索引：{indexes}。"
    )

async def ensure_page_size_1000(page, debug: dict | None = None) -> None:
    """确保订单列表分页数量设置为 1000，减少翻页遗漏。"""
    state = await page.evaluate(
        """
        async () => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const clickNode = (el) => {
                el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                el.click();
            };
            const findPagerRoots = () => Array.from(document.querySelectorAll('.el-pagination,.vxe-pager,[class*="pagination"],[class*="Pagination"],div'))
                .filter((el) => {
                    const text = textOf(el);
                    const rect = el.getBoundingClientRect();
                    return rect.width > 100 && rect.height > 12 && /条\\s*\\/\\s*页|共\\s*\\d+\\s*条/.test(text);
                })
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return br.top - ar.top || (ar.width * ar.height) - (br.width * br.height);
                });

            window.scrollTo(0, document.body.scrollHeight);
            await sleep(250);
            let roots = findPagerRoots();
            let root = roots[0] || null;
            if (!root) {
                window.scrollTo(0, 0);
                await sleep(150);
                roots = findPagerRoots();
                root = roots[0] || null;
            }
            if (!root) return { ok: false, reason: '没有找到分页区域。' };
            root.scrollIntoView({ block: 'center', inline: 'nearest' });
            await sleep(250);
            const currentText = textOf(root);
            if (/1000\\s*条\\s*\\/\\s*页/.test(currentText)) {
                window.scrollTo(0, 0);
                return { ok: true, changed: false, currentText, dropdownOpened: false };
            }

            const triggers = Array.from(root.querySelectorAll('.el-select,.el-select__wrapper,.el-input,.el-input__inner,input,button,span,div'))
                .filter((el) => visible(el) && /\\d+\\s*条\\s*\\/\\s*页|条\\s*\\/\\s*页/.test(textOf(el) || el.value || ''))
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (ar.width * ar.height) - (br.width * br.height);
                });
            const trigger = triggers[0] || root.querySelector('.el-select,.el-input,input') || root;
            clickNode(trigger);
            await sleep(500);
            const triggerRect = trigger.getBoundingClientRect();
            return {
                ok: true,
                changed: false,
                currentText,
                dropdownOpened: true,
                triggerText: textOf(trigger),
                triggerRect: {
                    left: Math.round(triggerRect.left),
                    top: Math.round(triggerRect.top),
                    right: Math.round(triggerRect.right),
                    bottom: Math.round(triggerRect.bottom),
                },
            };
        }
        """
    )
    if not state.get("ok"):
        if debug is not None:
            debug["page_size_1000"] = state
        raise RuntimeError(state.get("reason") or "没有成功设置每页 1000 条。")
    if not state.get("dropdownOpened"):
        if debug is not None:
            debug["page_size_1000"] = state
        return

    selected = False
    option_selector = "li.el-select-dropdown__item, .el-select-dropdown__item, [role='option'], .vxe-select-option"
    try:
        option = page.locator(option_selector).filter(has_text="1000条/页")
        if await option.count():
            await option.last.click(timeout=5000, force=True)
            selected = True
    except Exception as exc:
        state["locator_select_error"] = str(exc)

    if not selected:
        selected = bool(
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
                    const option = Array.from(document.querySelectorAll('li.el-select-dropdown__item,.el-select-dropdown__item,[role="option"],.vxe-select-option,li'))
                        .map((el) => ({ el, text: textOf(el), rect: el.getBoundingClientRect(), className: String(el.className || '') }))
                        .filter((item) =>
                            visible(item.el) &&
                            /^1000\\s*条\\s*\\/\\s*页$/.test(item.text) &&
                            !/disabled|is-disabled/.test(item.className)
                        )
                        .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left)[0];
                    if (!option) return false;
                    option.el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                    option.el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                    option.el.click();
                    return true;
                }
                """
            )
        )
    await page.wait_for_timeout(1800)
    validation = await page.evaluate(
        """
        (selected) => {
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll('.el-pagination,.vxe-pager,[class*="pagination"],[class*="Pagination"],div'))
                .filter((el) => {
                    const text = textOf(el);
                    const rect = el.getBoundingClientRect();
                    return rect.width > 100 && rect.height > 12 && /条\\s*\\/\\s*页|共\\s*\\d+\\s*条/.test(text);
                })
                .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return br.top - ar.top || (ar.width * ar.height) - (br.width * br.height);
                });
            const root = roots[0] || null;
            const currentText = root ? textOf(root) : '';
            window.scrollTo(0, 0);
            return { ok: /1000\\s*条\\s*\\/\\s*页/.test(currentText), currentText, selected };
        }
        """,
        selected,
    )
    state["validation"] = validation
    if debug is not None:
        debug["page_size_1000"] = state
    if not validation.get("ok"):
        raise RuntimeError(
            f"分页下拉已打开但没有成功切换到 1000条/页；当前分页文本：{validation.get('currentText') or state.get('currentText') or '未知'}"
        )

async def ensure_payment_time_desc(page, debug: dict | None = None) -> None:
    """确保订单列表按付款时间倒序排列。"""
    attempts: list[dict[str, object]] = []
    # 这个上限只防止前端状态异常时无限循环；是否继续点击完全由表头 DOM 排序状态决定。
    for attempt in range(1, 7):
        before_state = await order_table_action(page, "payment_sort_state")
        attempt_state: dict[str, object] = {
            "attempt": attempt,
            "before_state": before_state,
        }
        attempts.append(attempt_state)
        if not before_state.get("ok"):
            if debug is not None:
                debug["payment_sort_attempts"] = attempts
                debug["payment_sort_desc"] = before_state
            raise RuntimeError(before_state.get("reason") or "没有读取到付款时间列的排序状态。")

        if before_state.get("state") == "desc":
            state = {
                **before_state,
                "ok": True,
                "verified": True,
                "clicked": any(bool(item.get("click_state")) for item in attempts),
                "attempt_count": attempt,
                "message": "已从付款时间表头 DOM 状态确认当前为降序。",
            }
            if debug is not None:
                debug["payment_sort_attempts"] = attempts
                debug["payment_sort_desc"] = state
            return

        click_state = await order_table_action(page, "click_payment_desc")
        attempt_state["click_state"] = click_state
        if not click_state.get("ok"):
            if debug is not None:
                debug["payment_sort_attempts"] = attempts
                debug["payment_sort_desc"] = click_state
            raise RuntimeError(click_state.get("reason") or "没有成功点击付款时间降序控件。")

        await page.wait_for_timeout(1200)
        after_state = await order_table_action(page, "payment_sort_state")
        attempt_state["after_state"] = after_state
        if after_state.get("ok") and after_state.get("state") == "desc":
            state = {
                **after_state,
                "ok": True,
                "verified": True,
                "clicked": True,
                "attempt_count": attempt,
                "message": "点击后已从付款时间表头 DOM 状态确认当前为降序。",
            }
            if debug is not None:
                debug["payment_sort_attempts"] = attempts
                debug["payment_sort_desc"] = state
            return

        # 等待前端刷新排序类名/aria-sort，避免在状态尚未落稳时连续点击。
        await page.wait_for_timeout(600)

    final_state = await order_table_action(page, "payment_sort_state")
    state = {
        **final_state,
        "ok": False,
        "verified": False,
        "reason": (
            "已按付款时间表头 DOM 排序状态尝试切换降序，但最终仍未读取到 desc 状态；"
            "不会用点击次数推断排序，已停止以避免误把降序切成升序。"
        ),
    }
    if debug is not None:
        debug["payment_sort_attempts"] = attempts
        debug["payment_sort_desc"] = state
    raise RuntimeError(state["reason"])

async def reset_order_table_vertical_scroll(page) -> dict:
    """将订单表格滚动条重置到顶部。"""
    return dict(await order_table_action(page, "reset_vertical"))

async def scroll_order_table_down(page) -> dict:
    """向下滚动订单表格以加载更多可见行。"""
    return dict(await order_table_action(page, "scroll_vertical"))

async def collect_visible_batch_order_rows(page, source_page: int, source_scroll_top: int) -> list[dict[str, str]]:
    """收集当前可见的批量订单行文本和结构化字段。"""
    result = await order_table_action(
        page,
        "collect_rows",
        sourcePage=source_page,
        sourceScrollTop=source_scroll_top,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("reason") or "没有成功读取订单表格行。")
    return list(result.get("rows") or [])


async def wait_for_visible_batch_order_rows(
    page,
    debug: dict | None = None,
    *,
    timeout_ms: int = 12000,
    poll_ms: int = 700,
) -> dict:
    """
    等待付款时间排序后订单行重新挂回 DOM。

    领星的 VXE 表格会先更新表头排序状态，再异步刷新 body 行；如果看到
    “付款时间已降序”就立刻扫描，偶发只能读到表头，导致本轮候选为 0。
    这里仍然只通过 DOM 状态判断，不依赖坐标或截图。
    """
    started_at = time.monotonic()
    attempts: list[dict[str, object]] = []
    last_result: dict[str, object] = {}
    attempt = 0
    while True:
        attempt += 1
        result = dict(
            await order_table_action(
                page,
                "collect_rows",
                sourcePage=1,
                sourceScrollTop=0,
            )
        )
        rows = list(result.get("rows") or [])
        selected = result.get("selected") if isinstance(result.get("selected"), dict) else {}
        last_result = result
        attempt_log = {
            "attempt": attempt,
            "ok": bool(result.get("ok")),
            "row_count": len(rows),
            "selected_row_count_visible": selected.get("row_count_visible") if isinstance(selected, dict) else None,
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        }
        if not result.get("ok"):
            attempt_log["reason"] = result.get("reason") or ""
        attempts.append(attempt_log)
        if rows:
            if debug is not None:
                debug["wait_for_visible_rows"] = {
                    "ok": True,
                    "attempts": attempts,
                }
            return result
        if (time.monotonic() - started_at) * 1000 >= timeout_ms:
            if debug is not None:
                debug["wait_for_visible_rows"] = {
                    "ok": False,
                    "attempts": attempts,
                    "last_selected": result.get("selected"),
                }
                debug.setdefault("warnings", []).append(
                    "付款时间排序后等待订单表格行重新渲染超时；本轮可能只读取到表头，未读取到订单行。"
                )
            return last_result
        await page.wait_for_timeout(poll_ms)


async def click_next_batch_page(page) -> bool:
    """点击批量订单列表下一页并等待页面更新。"""
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
                const roots = Array.from(document.querySelectorAll('.el-pagination,.vxe-pager,[class*="pagination"],[class*="Pagination"],div'))
                    .filter((el) => visible(el) && /条\\s*\\/\\s*页|共\\s*\\d+\\s*条/.test(textOf(el)))
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return br.top - ar.top || (ar.width * ar.height) - (br.width * br.height);
                    });
                const root = roots[0] || null;
                if (!root) return false;
                const isDisabled = (el) => {
                    let node = el;
                    for (let i = 0; i < 5 && node && node !== root.parentElement; i += 1) {
                        const className = String(node.className || '');
                        if (node.disabled || node.getAttribute?.('aria-disabled') === 'true') return true;
                        if (/disabled|is-disabled/.test(className)) return true;
                        node = node.parentElement;
                    }
                    return false;
                };
                const next = Array.from(root.querySelectorAll('.btn-next,[aria-label*="next" i],[class*="next"],button,i,span'))
                    .map((el) => {
                        const target = el.closest?.('.btn-next,button,[role="button"],[class*="next"]') || el;
                        const className = `${String(el.className || '')} ${String(target.className || '')}`;
                        return { el: target, className, disabled: isDisabled(target) };
                    })
                    .filter((item, index, all) =>
                        visible(item.el) &&
                        all.findIndex((other) => other.el === item.el) === index &&
                        /btn-next|next|arrow-right|right/i.test(item.className) &&
                        !item.disabled
                    )
                    .sort((a, b) => {
                        const aButton = /btn-next|button/i.test(a.className) ? 0 : 1;
                        const bButton = /btn-next|button/i.test(b.className) ? 0 : 1;
                        return aButton - bButton;
                    })[0];
                if (!next) return false;
                next.el.click();
                return true;
            }
            """
        )
    )

async def collect_batch_order_candidates(
    page,
    processed_platform_orders: set[str],
    limit: int = 0,
    payment_window_hours: float = 24,
    debug: dict | None = None,
) -> list[BatchOrderItem]:
    """遍历订单列表并收集符合批量巡检条件的候选订单。"""
    threshold_dt = datetime.now() - timedelta(hours=payment_window_hours)
    if debug is not None:
        debug["scan_started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        debug["payment_window_hours"] = payment_window_hours
        debug["recent_threshold"] = threshold_dt.strftime("%Y-%m-%d %H:%M:%S")
        debug.setdefault("skip_counts", {})
        debug.setdefault("skip_preview", [])
        debug.setdefault("visited_pages", [])
        debug.setdefault("scroll_steps", [])
        debug.setdefault("scan_rows", [])
        debug.setdefault("warnings", [])

    await ensure_page_size_1000(page, debug)
    await ensure_batch_key_columns_visible(page, debug)
    await ensure_payment_time_desc(page, debug)
    await wait_for_visible_batch_order_rows(page, debug)

    raw_items: list[dict[str, object]] = []
    seen_raw_rows: set[str] = set()
    seen_scan_rows: set[str] = set()
    page_number = 1
    max_pages = 50
    stop_all_pages = False

    while page_number <= max_pages and not stop_all_pages:
        reset_state = await reset_order_table_vertical_scroll(page)
        current_scroll_top = int(reset_state.get("scrollTop") or 0)
        page_debug = {
            "page": page_number,
            "reset": reset_state,
            "screens": [],
        }
        if debug is not None:
            debug["visited_pages"].append(page_debug)

        reached_end = False
        consecutive_no_new_screens = 0
        for screen_index in range(120):
            visible_rows = await collect_visible_batch_order_rows(page, page_number, current_scroll_top)
            if debug is not None:
                snapshot = await order_table_action(
                    page,
                    "collect_rows",
                    sourcePage=page_number,
                    sourceScrollTop=current_scroll_top,
                )
                if snapshot.get("ok"):
                    debug["detected_headers"] = snapshot.get("headers") or []
                    debug["column_indexes"] = snapshot.get("column_indexes") or {}
                    current_visible_rows = debug.setdefault("current_visible_rows", [])
                    if len(current_visible_rows) < 80:
                        current_visible_rows.extend((snapshot.get("rows") or [])[: max(0, 80 - len(current_visible_rows))])
            new_count = 0
            old_payment_seen = False
            for row in visible_rows:
                paid_at_text = str(row.get("paid_at_text", ""))
                payment_text = f"付款时间 {paid_at_text}" if paid_at_text else str(row.get("row_text", ""))
                payment_status = classify_recent_payment_window(payment_text, hours=payment_window_hours)
                platform_order_no = str(row.get("platform_order_no", ""))
                system_order_no = str(row.get("system_order_no", ""))
                tag_text = str(row.get("tag_text", "")).strip()
                row_key = f"{system_order_no}:{platform_order_no}"
                already_processed = platform_order_no in processed_platform_orders
                product_debug = _row_supported_product_debug(row)
                _record_unknown_asins(
                    debug,
                    list(product_debug.get("unknown_asins") or []),
                    platform_order_no=platform_order_no,
                    system_order_no=system_order_no,
                    sku=str(row.get("sku") or ""),
                    payment_time=paid_at_text,
                    source_page=row.get("source_page"),
                    source_scroll_top=row.get("source_scroll_top"),
                )

                # 全量扫描日志只按订单去重，方便排查是否真正遍历到阈值附近。
                if debug is not None and row_key and row_key not in seen_scan_rows:
                    seen_scan_rows.add(row_key)
                    debug["scan_rows"].append(
                        {
                            "row": len(debug["scan_rows"]) + 1,
                            "page": page_number,
                            "screen": screen_index + 1,
                            "source_scroll_top": current_scroll_top,
                            "system_order_no": system_order_no,
                            "platform_order_id": platform_order_no,
                            "platform_order_no": platform_order_no,
                            "payment_time": paid_at_text,
                            "payment_status": payment_status,
                            "asin": row.get("asin") or "",
                            "asin_text": row.get("asin_text") or "",
                            "sku": row.get("sku") or "",
                            "status_text": row.get("status_text") or "",
                            "buyer_cancel_requested": _row_has_buyer_cancel_request(row),
                            "tag_text": tag_text,
                            "has_tag": bool(tag_text),
                            "is_processed": already_processed,
                            "hit": False,
                            "skip_reason": "",
                            **product_debug,
                        }
                    )
                if payment_status == "old":
                    old_payment_seen = True
                    if debug is not None:
                        debug["stopped_due_to_old_payment"] = {
                            "page": page_number,
                            "screen": screen_index + 1,
                            "scroll_top": current_scroll_top,
                            "paid_at_text": paid_at_text,
                            "platform_order_no": platform_order_no,
                            "system_order_no": system_order_no,
                        }
                    continue
                if _row_has_buyer_cancel_request(row):
                    if debug is not None:
                        skip_counts = debug.setdefault("skip_counts", {})
                        skip_counts["buyer_cancel_requested_pre_scan"] = int(skip_counts.get("buyer_cancel_requested_pre_scan", 0)) + 1
                        for scan_row in debug.get("scan_rows", []):
                            if (
                                scan_row.get("platform_order_no") == platform_order_no
                                and scan_row.get("system_order_no") == system_order_no
                            ):
                                scan_row["skip_reason"] = "buyer_cancel_requested_pre_scan"
                                scan_row["status_text"] = row.get("status_text") or ""
                                scan_row["buyer_cancel_requested"] = True
                                scan_row.update(product_debug)
                                break
                    continue
                if tag_text:
                    if debug is not None:
                        skip_counts = debug.setdefault("skip_counts", {})
                        skip_counts["has_tag_pre_scan"] = int(skip_counts.get("has_tag_pre_scan", 0)) + 1
                        for scan_row in debug.get("scan_rows", []):
                            if (
                                scan_row.get("platform_order_no") == platform_order_no
                                and scan_row.get("system_order_no") == system_order_no
                            ):
                                scan_row["skip_reason"] = "has_tag_pre_scan"
                                scan_row["tag_text"] = tag_text
                                scan_row.update(product_debug)
                                break
                    continue
                if already_processed:
                    if debug is not None:
                        skip_counts = debug.setdefault("skip_counts", {})
                        skip_counts["already_processed_pre_scan"] = int(skip_counts.get("already_processed_pre_scan", 0)) + 1
                        for scan_row in debug.get("scan_rows", []):
                            if (
                                scan_row.get("platform_order_no") == platform_order_no
                                and scan_row.get("system_order_no") == system_order_no
                            ):
                                scan_row["skip_reason"] = "already_processed_pre_scan"
                                scan_row.update(product_debug)
                                break
                    continue
                if row_key in seen_raw_rows:
                    continue
                seen_raw_rows.add(row_key)
                raw_items.append(row)
                new_count += 1

            screen_debug = {
                "screen": screen_index + 1,
                "scroll_top": current_scroll_top,
                "visible_rows": len(visible_rows),
                "new_rows": new_count,
                "old_payment_seen": old_payment_seen,
            }
            page_debug["screens"].append(screen_debug)
            if debug is not None:
                debug["scroll_steps"].append({"page": page_number, **screen_debug})

            if new_count:
                consecutive_no_new_screens = 0
            else:
                consecutive_no_new_screens += 1

            if old_payment_seen:
                screen_debug["stop_reason"] = f"付款时间早于最近 {payment_window_hours:g} 小时，降序遍历提前停止。"
                reached_end = True
                stop_all_pages = True
                break

            if reached_end:
                break

            scroll_state = await scroll_order_table_down(page)
            screen_debug["scroll_after"] = scroll_state
            if not scroll_state.get("ok"):
                screen_debug["stop_reason"] = scroll_state.get("reason") or "表格纵向滚动失败。"
                break
            if not scroll_state.get("changed"):
                screen_debug["stop_reason"] = "表格纵向滚动条已到底或连续滚动未产生新位置。"
                if consecutive_no_new_screens >= 3 or scroll_state.get("end"):
                    break
                await page.wait_for_timeout(700)
                continue
            if consecutive_no_new_screens >= 4 and scroll_state.get("end"):
                screen_debug["stop_reason"] = "连续多屏没有新增订单且已到底。"
                break
            current_scroll_top = int(scroll_state.get("scrollTop") or current_scroll_top)
            reached_end = bool(scroll_state.get("end"))
            await page.wait_for_timeout(450)

        if stop_all_pages or not await click_next_batch_page(page):
            break
        page_number += 1
        await page.wait_for_timeout(1800)

    if debug is not None:
        if not debug.get("stopped_due_to_old_payment"):
            debug["warnings"].append(
                "本轮没有读取到早于最近一天阈值的付款时间；请检查订单是否确实都在窗口内，或查看滚动步骤是否提前到底。"
            )
        debug["raw_item_count"] = len(raw_items)
        debug["unique_raw_item_count"] = len(seen_raw_rows)
        debug["raw_item_preview"] = [
            {
                "system_order_no": item.get("system_order_no", ""),
                "platform_order_no": item.get("platform_order_no", ""),
                "asin_text": item.get("asin_text", ""),
                "sku": item.get("sku", ""),
                "status_text": item.get("status_text", ""),
                "tag_text": item.get("tag_text", ""),
                "paid_at_text": item.get("paid_at_text", ""),
                "source_page": item.get("source_page"),
                "source_scroll_top": item.get("source_scroll_top"),
                "headers": item.get("column_headers", {}),
            }
            for item in raw_items[:20]
        ]
        debug.setdefault("skip_counts", {})
        debug.setdefault("skip_preview", [])

    candidates = build_batch_candidates_from_rows(
        raw_items,
        processed_platform_orders,
        limit=limit,
        payment_window_hours=payment_window_hours,
        debug=debug,
    )
    if debug is not None:
        candidates_by_platform = {item.platform_order_no: item for item in candidates}
        for scan_row in debug.get("scan_rows", []):
            platform_order_no = str(scan_row.get("platform_order_no") or "")
            matched = candidates_by_platform.get(platform_order_no)
            if matched:
                scan_row["hit"] = True
                scan_row["parent_asin"] = matched.parent_asin
                scan_row["matched_asin"] = matched.asin
                scan_row["matched_tent_asins"] = matched.matched_asins
                scan_row["matched_product_asins"] = matched.matched_asins
                scan_row["product_type"] = matched.product_type
                scan_row["skip_reason"] = ""
        debug["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        debug["scan_summary"] = {
            "read_total_unique_rows": len(debug.get("scan_rows", [])),
            "raw_recent_unprocessed_rows": len(raw_items),
            "unique_raw_item_count": len(seen_raw_rows),
            "candidate_count": len(candidates),
            "covered_recent_threshold": bool(debug.get("stopped_due_to_old_payment")),
            "warning_count": len(debug.get("warnings", [])),
        }
        debug["candidate_count"] = len(candidates)
        debug["needs_update_platform_orders"] = [item.platform_order_no for item in candidates]
        debug["orders_to_update"] = [
            {
                "platform_order_id": item.platform_order_no,
                "platform_order_no": item.platform_order_no,
                "system_order_no": item.system_order_no,
                "payment_time": item.paid_at_text,
                "asin_or_product_id": item.asin,
                "parent_asin": item.parent_asin,
                "matched_tent_asins": item.matched_asins,
                "all_asins": item.all_asins,
                "sku": item.sku,
                "source_page": item.source_page,
                "source_scroll_top": item.source_scroll_top,
            }
            for item in candidates
        ]
    return candidates

async def find_system_order_for_order_no(page, order_no: str, search_kind: str) -> str | None:
    """根据平台单号查找对应的系统单号。"""
    if search_kind == "system" and SYSTEM_ORDER_RE.fullmatch(order_no.strip()):
        visible = await page.evaluate(
            """
            (orderNo) => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && rect.top > 150 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll('a,span,td,div'))
                    .some((el) => visible(el) && (el.innerText || el.textContent || '').includes(orderNo));
            }
            """,
            order_no,
        )
        return order_no if visible else None

    return await page.evaluate(
        """
        ({ orderNo }) => {
            const systemRe = /\\b\\d{15,24}\\b/;
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && rect.top > 150 &&
                    rect.top < window.innerHeight - 20 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const normalized = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const rowLike = (el) => {
                const className = String(el.className || '');
                const role = el.getAttribute && el.getAttribute('role');
                const tag = el.tagName.toLowerCase();
                return tag === 'tr' || role === 'row' || /row|vxe-body--row|el-table__row/i.test(className);
            };
            const findRowText = (el) => {
                let node = el;
                let fallback = '';
                for (let i = 0; i < 12 && node && node !== document.body; i += 1) {
                    const text = normalized(node);
                    if (text.includes(orderNo) && systemRe.test(text)) {
                        fallback = text;
                        if (rowLike(node) || text.length < 2500) return text;
                    }
                    node = node.parentElement;
                }
                return fallback;
            };
            const nodes = Array.from(document.querySelectorAll('a,span,td,div'))
                .filter((el) => visible(el) && normalized(el).includes(orderNo));
            for (const node of nodes) {
                const rowText = findRowText(node);
                const match = rowText.match(systemRe);
                if (match) return match[0];
            }
            return null;
        }
        """,
        {"orderNo": order_no},
    )

async def find_system_orders_for_order_no(page, order_no: str, search_kind: str) -> list[str]:
    """根据平台单号查找所有可见的系统单号。"""
    if search_kind == "system" and SYSTEM_ORDER_RE.fullmatch(order_no.strip()):
        system_order_no = await find_system_order_for_order_no(page, order_no, search_kind)
        return [system_order_no] if system_order_no else []

    return await page.evaluate(
        """
        ({ orderNo }) => {
            const systemRe = /^\\d{15,24}$/;
            const systemSearchRe = /\\b\\d{15,24}\\b/g;
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && rect.top > 150 &&
                    rect.top < window.innerHeight - 20 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const rowLike = (el) => {
                const className = String(el.className || '');
                const role = el.getAttribute && el.getAttribute('role');
                const tag = el.tagName.toLowerCase();
                return tag === 'tr' || role === 'row' || /row|vxe-body--row|el-table__row/i.test(className);
            };
            const findRow = (el) => {
                let node = el;
                let fallback = null;
                for (let i = 0; i < 14 && node && node !== document.body; i += 1) {
                    const text = textOf(node);
                    if (text.includes(orderNo) && /\\b\\d{15,24}\\b/.test(text)) {
                        fallback = node;
                        if (rowLike(node)) return node;
                    }
                    node = node.parentElement;
                }
                return fallback;
            };
            const scoreRow = (row, top) => {
                const text = textOf(row);
                const hasImage = Array.from(row.querySelectorAll('img'))
                    .some((img) => {
                        const src = img.getAttribute('src') || '';
                        const rect = img.getBoundingClientRect();
                        return src && rect.width > 8 && rect.height > 8;
                    });
                let score = 10000 - Math.round(top || 0);
                if (hasImage) score += 8000;
                if (/Custom|Config|Package|Canopy|Tent|定制|更多商品信息/i.test(text)) score += 2000;
                if (/无图/.test(text)) score -= 1000;
                return score;
            };
            const candidates = new Map();
            const push = (value, score) => {
                if (!systemRe.test(value)) return;
                const old = candidates.get(value);
                if (!old || score > old.score) candidates.set(value, { value, score });
            };
            const platformNodes = Array.from(document.querySelectorAll('a,span,td,div'))
                .filter((el) => visible(el) && textOf(el).includes(orderNo));
            for (const node of platformNodes) {
                const row = findRow(node);
                if (!row) continue;
                const rowRect = row.getBoundingClientRect();
                const baseScore = scoreRow(row, rowRect.top);
                const directNodes = Array.from(row.querySelectorAll('a,span,td,div'))
                    .filter((el) => visible(el));
                for (const item of directNodes) {
                    const text = textOf(item);
                    const exact = text.match(/^\\d{15,24}$/);
                    if (exact) {
                        const tagBonus = item.tagName.toLowerCase() === 'a' ? 500 : 0;
                        push(exact[0], baseScore + tagBonus);
                    }
                }
                const rowText = textOf(row);
                for (const match of rowText.matchAll(systemSearchRe)) {
                    push(match[0], baseScore);
                }
            }
            return Array.from(candidates.values())
                .sort((a, b) => b.score - a.score)
                .map((item) => item.value);
        }
        """,
        {"orderNo": order_no},
    )

async def wait_for_order_in_list(page, order_no: str, search_kind: str, timeout_sec: int) -> str | None:
    """等待目标订单出现在订单列表中。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        system_order_no = await find_system_order_for_order_no(page, order_no, search_kind)
        if system_order_no:
            return system_order_no
        await page.wait_for_timeout(1000)
    return None

async def wait_for_orders_in_list(page, order_no: str, search_kind: str, timeout_sec: int) -> list[str]:
    """等待多个目标订单出现在订单列表中。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        system_order_nos = await find_system_orders_for_order_no(page, order_no, search_kind)
        if system_order_nos:
            return system_order_nos
        await page.wait_for_timeout(1000)
    return []

async def find_visible_system_order_no(page, preferred: str | None = None) -> str | None:
    """从当前可见列表中查找目标平台单号对应的系统单号。"""
    if preferred and SYSTEM_ORDER_RE.fullmatch(preferred):
        return preferred
    return await page.evaluate(
        """
        (preferred) => {
            const re = /\\b\\d{15,24}\\b/;
            const visible = (el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && rect.top > 150 && rect.top < window.innerHeight - 20 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const nodes = Array.from(document.querySelectorAll('a,td,span,div'));
            const matches = [];
            for (const el of nodes) {
                if (!visible(el)) continue;
                const text = (el.innerText || el.textContent || '').trim();
                if (preferred && text.includes(preferred)) return preferred;
                const match = text.match(re);
                if (match) {
                    const rect = el.getBoundingClientRect();
                    const tagScore = el.tagName.toLowerCase() === 'a' ? 10 : 0;
                    matches.push({ value: match[0], score: tagScore - rect.top / 10000 });
                }
            }
            matches.sort((a, b) => b.score - a.score);
            return matches.length ? matches[0].value : null;
        }
        """,
        preferred,
    )
