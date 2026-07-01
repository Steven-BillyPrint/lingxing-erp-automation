from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from ..models import BatchOrderItem, CustomZipDownloadResult

CUSTOM_ZIP_DOWNLOADED = "custom_zip_downloaded"
CUSTOM_ZIP_PREVIEW = "custom_zip_preview"
CUSTOM_ZIP_SKIPPED_NO_FOLDER = "custom_zip_skipped_no_folder"
CUSTOM_ZIP_NOT_FOUND = "custom_zip_not_found"
CUSTOM_ZIP_TRIGGER_NOT_FOUND = "custom_zip_trigger_not_found"
CUSTOM_ZIP_DOWNLOAD_ERROR = "custom_zip_download_error"
CUSTOM_ZIP_DISABLED = "custom_zip_disabled"

WINDOWS_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
LIST_SUFFIX_RE = re.compile(r"\s+共\s*\d+\s*$")
ZIP_NAME_RE = re.compile(r"\.zip(?:$|[\s)）\]}])", re.IGNORECASE)
NON_ZIP_MEDIA_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|pdf|ai|psd|svg|eps)(?:$|[\s)）\]}])", re.IGNORECASE)

ROW_ATTR = "data-lx-custom-zip-row-id"
TRIGGER_ATTR = "data-lx-custom-zip-trigger-id"
ENTRY_ATTR = "data-lx-custom-zip-entry-id"


def _base_result(status: str, item: BatchOrderItem, **kwargs: Any) -> CustomZipDownloadResult:
    return CustomZipDownloadResult(
        status=status,
        platform_order_no=item.platform_order_no,
        asin=item.asin,
        sku=item.sku,
        **kwargs,
    )


def _short_error(exc: Exception, max_length: int = 500) -> str:
    text = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
    return text[:max_length]


def sanitize_zip_filename(filename: str | None) -> str:
    """清洗 zip 文件名，避免保存到订单文件夹时触发 Windows 非法字符。"""
    raw = str(filename or "").strip()
    if not raw:
        raw = "customization_images.zip"
    cleaned = WINDOWS_INVALID_FILENAME_CHARS_RE.sub("_", raw.replace("/", "_").replace("\\", "_"))
    cleaned = re.sub(r"_+", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        cleaned = "customization_images.zip"
    if not cleaned.lower().endswith(".zip"):
        cleaned = f"{cleaned}.zip"
    return cleaned


def normalize_item_match_text(value: str | None) -> str | None:
    """清理列表采集附带的数量文本，避免 SKU 匹配被“共1”污染。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    text = LIST_SUFFIX_RE.sub("", text).strip()
    return text or None


def unique_zip_target_path(target_folder: str | Path, filename: str | None) -> Path:
    """生成订单文件夹内不覆盖已有文件的 zip 保存路径。"""
    folder = Path(target_folder)
    base_name = sanitize_zip_filename(filename)
    candidate = folder / base_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix or ".zip"
    index = 2
    while True:
        numbered = folder / f"{stem} ({index}){suffix}"
        if not numbered.exists():
            return numbered
        index += 1


def build_item_match_payload(item: BatchOrderItem) -> dict[str, str | None]:
    """构造 DOM 商品行匹配字段。"""
    return {
        "platform_order_no": item.platform_order_no,
        "system_order_no": item.system_order_no,
        "asin": normalize_item_match_text(item.asin),
        "sku": normalize_item_match_text(item.sku),
        "msku": normalize_item_match_text(getattr(item, "msku", None)),
    }


def _entry_sort_key(entry: dict[str, Any]) -> tuple[float, int]:
    top = entry.get("top")
    index = entry.get("index")
    try:
        top_value = float(top)
    except (TypeError, ValueError):
        top_value = 0.0
    try:
        index_value = int(index)
    except (TypeError, ValueError):
        index_value = 0
    return top_value, index_value


def choose_zip_entry_from_popover_entries(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    从附件浮层条目中选择要下载的 zip。
    """
    visible_entries = [entry for entry in entries if str(entry.get("text") or "").strip()]
    if not visible_entries:
        return None
    explicit_zip = [entry for entry in visible_entries if ZIP_NAME_RE.search(str(entry.get("text") or ""))]
    if explicit_zip:
        return max(explicit_zip, key=_entry_sort_key)
    non_media = [
        entry
        for entry in visible_entries
        if not NON_ZIP_MEDIA_RE.search(str(entry.get("text") or ""))
    ]
    return max(non_media or visible_entries, key=_entry_sort_key)


def zip_candidate_names(candidates: list[dict[str, Any]]) -> list[str]:
    """生成日志中的候选名称。"""
    names: list[str] = []
    for candidate in candidates:
        label = candidate.get("text") or candidate.get("filename") or candidate.get("entry_id")
        if label:
            names.append(str(label))
    return names


def _candidate_entries_for_log(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """日志只保留 DOM 定位信息，不保存页面下载链接或临时对象。"""
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        entries.append(
            {
                "entry_id": candidate.get("entry_id"),
                "text": candidate.get("text"),
                "top": candidate.get("top"),
                "index": candidate.get("index"),
                "is_explicit_zip": bool(candidate.get("is_explicit_zip")),
            }
        )
    return entries


async def _locate_product_row_and_trigger(page, item: BatchOrderItem) -> dict[str, Any]:
    payload = build_item_match_payload(item)
    return await page.evaluate(
        """
        ({ payload, rowAttr, triggerAttr }) => {
            const marker = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const targetAsin = String(payload.asin || '').toUpperCase();
            const targetSku = String(payload.sku || payload.msku || '').toLowerCase();
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const hasTarget = (text) => {
                const raw = String(text || '');
                const upper = raw.toUpperCase();
                const lower = raw.toLowerCase();
                return Boolean((targetAsin && upper.includes(targetAsin)) || (targetSku && lower.includes(targetSku)));
            };
            const detailRoots = Array.from(document.querySelectorAll(
                '.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,main,section,article,div'
            ))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const text = textOf(el);
                    return /系统单号/.test(text) && /商品信息/.test(text) && hasTarget(text);
                })
                .map((el) => ({ el, text: textOf(el) }))
                .filter((item) => item.text.length >= 80 && item.text.length < 50000)
                .sort((a, b) => a.text.length - b.text.length)
                .map((item) => item.el);
            const root = detailRoots[0] || document.body;
            const rowSelector = 'tr,[role="row"],.vxe-body--row,.el-table__row,.ant-table-row,li';
            const scopes = [];
            const addScope = (el, reason) => {
                if (!el || el === document.body || el === document.documentElement || !visible(el)) return;
                const text = textOf(el);
                if (!hasTarget(text) || text.length > 12000) return;
                if (scopes.some((item) => item.el === el)) return;
                scopes.push({ el, text, reason });
            };
            const seedNodes = Array.from(root.querySelectorAll('span,div,p,td,th,b,strong,a'))
                .filter((el) => visible(el) && hasTarget(textOf(el)));
            for (const seed of seedNodes) {
                addScope(seed.closest(rowSelector), 'closest-row');
                let node = seed;
                for (let depth = 0; depth < 9 && node && node !== root.parentElement; depth += 1) {
                    addScope(node, `ancestor-${depth}`);
                    node = node.parentElement;
                }
            }
            for (const row of Array.from(root.querySelectorAll(rowSelector))) {
                addScope(row, 'row-scan');
            }
            const triggerNodes = (scope) => Array.from(scope.querySelectorAll('a,span,div,p,td,th,b,strong,button'))
                .filter((el) => {
                    if (!visible(el)) return false;
                    const text = textOf(el);
                    const className = String(el.className || '');
                    return /^共\\s*\\d+$/.test(text) || (/共\\s*\\d+/.test(text) && /tags-more|ak-tags|more|attachment|link/i.test(className));
                });
            const scoredScopes = scopes
                .map((scope) => {
                    const triggers = triggerNodes(scope.el);
                    const text = scope.text;
                    const score =
                        (triggers.length ? 200 : 0) +
                        (/更多商品信息/.test(text) ? 80 : 0) +
                        (/商品ID|ASIN/.test(text) ? 30 : 0) +
                        (/订单信息|交易信息|其他信息/.test(text) ? 20 : 0) -
                        Math.min(text.length / 100, 80);
                    return { ...scope, triggers, score };
                })
                .filter((scope) => scope.triggers.length)
                .sort((a, b) => b.score - a.score || a.text.length - b.text.length);
            const chosenScope = scoredScopes[0];
            if (!chosenScope) {
                return {
                    ok: false,
                    error: '无法定位当前订单商品行的附件入口。',
                    diagnostics: {
                        root_found: Boolean(detailRoots.length),
                        seed_node_count: seedNodes.length,
                        scope_count: scopes.length,
                        target_asin: targetAsin,
                        target_sku: targetSku,
                    },
                };
            }
            const scopeRect = chosenScope.el.getBoundingClientRect();
            const chosenTrigger = chosenScope.triggers
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = textOf(el);
                    const className = String(el.className || '');
                    const score =
                        (/^共\\s*\\d+$/.test(text) ? 80 : 0) +
                        (/tags-more|ak-tags|more/i.test(className) ? 60 : 0) +
                        (rect.left > scopeRect.left + scopeRect.width * 0.55 ? 30 : 0) +
                        rect.left / 10000;
                    return { el, rect, text, score };
                })
                .sort((a, b) => b.score - a.score)[0];
            const rowId = `lx-custom-zip-row-${marker}`;
            const triggerId = `lx-custom-zip-trigger-${marker}`;
            chosenScope.el.setAttribute(rowAttr, rowId);
            chosenTrigger.el.setAttribute(triggerAttr, triggerId);
            chosenTrigger.el.scrollIntoView({ block: 'center', inline: 'center' });
            return {
                ok: true,
                row_id: rowId,
                trigger_id: triggerId,
                trigger_text: chosenTrigger.text,
                product_row_match: `dom:${chosenScope.reason}`,
                diagnostics: {
                    root_found: Boolean(detailRoots.length),
                    seed_node_count: seedNodes.length,
                    scope_count: scopes.length,
                    trigger_count: chosenScope.triggers.length,
                    target_asin: targetAsin,
                    target_sku: targetSku,
                    row_text_preview: chosenScope.text.slice(0, 240),
                },
            };
        }
        """,
        {"payload": payload, "rowAttr": ROW_ATTR, "triggerAttr": TRIGGER_ATTR},
    )


async def _dismiss_attachment_popovers(page) -> None:
    try:
        await page.keyboard.press("Escape")
        await page.mouse.move(8, 8)
        await page.wait_for_timeout(120)
    except Exception:
        pass


async def _read_popover_entries(page, trigger_rect: dict[str, float] | None = None) -> list[dict[str, Any]]:
    entries = await page.evaluate(
        """
        ({ entryAttr, triggerRect }) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none' &&
                    rect.bottom >= 0 && rect.top <= window.innerHeight &&
                    rect.right >= 0 && rect.left <= window.innerWidth;
            };
            const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const nearTrigger = (rect) => {
                if (!triggerRect) return true;
                const triggerCenterY = triggerRect.top + triggerRect.height / 2;
                const rootCenterY = rect.top + rect.height / 2;
                return Math.abs(rootCenterY - triggerCenterY) <= 260 ||
                    (rect.top <= triggerCenterY + 80 && rect.bottom >= triggerCenterY - 80);
            };
            const explicitRoots = Array.from(document.querySelectorAll(
                '.el-tooltip__popper,.el-popper,.vxe-table--tooltip-wrapper,.ak-tooltip,.tooltip,.popover,[role="tooltip"]'
            ));
            const floatingRoots = Array.from(document.querySelectorAll('body > div, body > span'))
                .filter((el) => {
                    if (!visible(el)) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const zIndex = Number.parseInt(style.zIndex || '0', 10) || 0;
                    const text = textOf(el);
                    return text.length >= 2 && text.length < 9000 &&
                        (zIndex >= 100 || style.position === 'fixed' || style.position === 'absolute') &&
                        (/共\\s*\\d+|\\.zip|\\.png|\\.jpe?g|\\.pdf|Customize|Custom|B0[A-Z0-9]{8}|[a-f0-9]{6,}-/i.test(text) || rect.width >= 120);
                });
            const roots = [];
            for (const root of [...explicitRoots, ...floatingRoots]) {
                if (!visible(root) || roots.includes(root)) continue;
                const rect = root.getBoundingClientRect();
                if (!nearTrigger(rect)) continue;
                roots.push(root);
            }
            const rawEntries = [];
            const seen = new Set();
            roots.forEach((root, rootIndex) => {
                const nodes = Array.from(root.querySelectorAll('a,span,div,p,li,button'))
                    .filter((el) => visible(el))
                    .map((el) => {
                        const text = textOf(el);
                        const rect = el.getBoundingClientRect();
                        return { el, text, rect };
                    })
                    .filter((item) =>
                        item.text &&
                        item.text.length <= 260 &&
                        !/^共\\s*\\d+$/.test(item.text) &&
                        !/^更多$/.test(item.text)
                    )
                    .filter((item) => {
                        const childTexts = Array.from(item.el.children || [])
                            .filter((child) => visible(child))
                            .map((child) => textOf(child))
                            .filter(Boolean);
                        return !childTexts.some((childText) => childText && childText !== item.text && item.text.includes(childText) && item.text.length > childText.length + 8);
                    });
                nodes.forEach((item, nodeIndex) => {
                    const text = item.text;
                    if (!/\\.zip|\\.png|\\.jpe?g|\\.pdf|Customize|Custom|B0[A-Z0-9]{8}|[a-f0-9]{6,}-/i.test(text)) return;
                    const key = `${rootIndex}:${Math.round(item.rect.top)}:${text}`;
                    if (seen.has(key)) return;
                    seen.add(key);
                    const clickable = item.el.closest('a,button,[role="button"],.el-link,.ak-link') || item.el;
                    const entryId = `lx-custom-zip-entry-${Date.now()}-${rootIndex}-${nodeIndex}-${Math.random().toString(36).slice(2)}`;
                    clickable.setAttribute(entryAttr, entryId);
                    rawEntries.push({
                        entry_id: entryId,
                        text,
                        top: item.rect.top,
                        left: item.rect.left,
                        index: rawEntries.length,
                        root_index: rootIndex,
                        is_explicit_zip: /\\.zip(?:$|[\\s)）\\]}])/i.test(text),
                    });
                });
            });
            rawEntries.sort((a, b) => a.top - b.top || a.left - b.left || a.index - b.index);
            return rawEntries.map((entry, index) => ({ ...entry, index }));
        }
        """,
        {"entryAttr": ENTRY_ATTR, "triggerRect": trigger_rect},
    )
    return entries if isinstance(entries, list) else []


async def _wait_for_zip_entries(
    page,
    timeout_ms: int = 1400,
    trigger_rect: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    deadline = time.monotonic() + timeout_ms / 1000
    latest_entries: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        latest_entries = await _read_popover_entries(page, trigger_rect)
        chosen = choose_zip_entry_from_popover_entries(latest_entries)
        if chosen:
            return latest_entries, chosen
        await page.wait_for_timeout(120)
    return latest_entries, choose_zip_entry_from_popover_entries(latest_entries)


async def _open_attachment_popover(page, trigger_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    trigger = page.locator(f'[{TRIGGER_ATTR}="{trigger_id}"]').first
    await _dismiss_attachment_popovers(page)
    await trigger.scroll_into_view_if_needed(timeout=1200)
    trigger_box = await trigger.bounding_box()
    trigger_rect = (
        {
            "top": float(trigger_box["y"]),
            "bottom": float(trigger_box["y"] + trigger_box["height"]),
            "height": float(trigger_box["height"]),
        }
        if trigger_box
        else None
    )
    methods: list[tuple[str, Any]] = [
        ("hover", lambda: trigger.hover(timeout=1500)),
        ("force_hover", lambda: trigger.hover(timeout=1500, force=True)),
        ("click", lambda: trigger.click(timeout=1500)),
        ("force_click", lambda: trigger.click(timeout=1500, force=True)),
    ]
    last_entries: list[dict[str, Any]] = []
    for method_name, action in methods:
        try:
            await action()
        except Exception:
            pass
        entries, chosen = await _wait_for_zip_entries(page, trigger_rect=trigger_rect)
        last_entries = entries or last_entries
        if chosen:
            return entries, chosen, method_name
    try:
        await page.evaluate(
            """
            ({ triggerAttr, triggerId }) => {
                const trigger = document.querySelector(`[${triggerAttr}="${triggerId}"]`);
                if (!trigger) return;
                trigger.scrollIntoView({ block: 'center', inline: 'center' });
                for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
                    trigger.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                }
            }
            """,
            {"triggerAttr": TRIGGER_ATTR, "triggerId": trigger_id},
        )
    except Exception:
        pass
    entries, chosen = await _wait_for_zip_entries(page, trigger_rect=trigger_rect)
    return entries or last_entries, chosen, "dispatch_mouse_events"


async def _prepare_dom_zip_candidate(page, item: BatchOrderItem) -> CustomZipDownloadResult:
    locate = await _locate_product_row_and_trigger(page, item)
    diagnostics = dict(locate.get("diagnostics") or {})
    if not locate.get("ok"):
        return _base_result(
            CUSTOM_ZIP_TRIGGER_NOT_FOUND,
            item,
            diagnostics=diagnostics,
            open_method="dom",
            error=str(locate.get("error") or "无法定位当前订单商品行的附件入口。"),
        )
    entries, chosen, method = await _open_attachment_popover(page, str(locate.get("trigger_id") or ""))
    diagnostics.update(
        {
            "dom_open_method": method,
            "attachment_entry_count": len(entries),
            "chosen_zip_text": chosen.get("text") if chosen else None,
        }
    )
    if not chosen:
        return _base_result(
            CUSTOM_ZIP_NOT_FOUND,
            item,
            trigger_text=str(locate.get("trigger_text") or ""),
            zip_candidates=zip_candidate_names(entries),
            zip_candidate_entries=_candidate_entries_for_log(entries),
            diagnostics=diagnostics,
            product_row_match=locate.get("product_row_match"),
            open_method=method,
            error="已打开当前商品附件浮层，但未找到底部 zip 候选。",
        )
    return _base_result(
        CUSTOM_ZIP_PREVIEW,
        item,
        trigger_text=str(locate.get("trigger_text") or ""),
        zip_candidates=zip_candidate_names(entries),
        zip_candidate_entries=_candidate_entries_for_log([chosen]),
        diagnostics=diagnostics,
        product_row_match=locate.get("product_row_match"),
        open_method=method,
    )


async def prepare_order_custom_zip(
    page,
    item: BatchOrderItem,
    *,
    enabled: bool = True,
    prepared_before_writeback: bool = True,
    **_: Any,
) -> CustomZipDownloadResult:
    """预读领星商品行附件浮层里的 zip 候选；此阶段不创建文件。"""
    if not enabled:
        return _base_result(CUSTOM_ZIP_DISABLED, item, prepared_before_writeback=prepared_before_writeback)
    if page is None:
        return _base_result(
            CUSTOM_ZIP_TRIGGER_NOT_FOUND,
            item,
            prepared_before_writeback=prepared_before_writeback,
            error="缺少 Playwright page，无法读取领星 DOM 附件节点。",
        )
    result = await _prepare_dom_zip_candidate(page, item)
    result.prepared_before_writeback = prepared_before_writeback
    return result


def _fallback_zip_filename(item: BatchOrderItem) -> str:
    asin = normalize_item_match_text(item.asin) or "custom"
    return f"{item.platform_order_no}_{asin}_Customized.zip"


async def _click_entry_and_wait_for_download(page, entry_id: str):
    entry = page.locator(f'[{ENTRY_ATTR}="{entry_id}"]').first
    try:
        async with page.expect_download(timeout=15000) as download_info:
            await entry.click(timeout=2500)
        return await download_info.value
    except Exception:
        async with page.expect_download(timeout=15000) as download_info:
            await entry.click(timeout=2500, force=True)
        return await download_info.value


async def _download_dom_zip(page, item: BatchOrderItem, target_folder: Path) -> CustomZipDownloadResult:
    preview = await _prepare_dom_zip_candidate(page, item)
    if preview.status != CUSTOM_ZIP_PREVIEW or not preview.zip_candidate_entries:
        return preview
    chosen = preview.zip_candidate_entries[0]
    entry_id = str(chosen.get("entry_id") or "")
    if not entry_id:
        return _base_result(
            CUSTOM_ZIP_DOWNLOAD_ERROR,
            item,
            zip_candidates=list(preview.zip_candidates),
            zip_candidate_entries=list(preview.zip_candidate_entries),
            diagnostics=preview.diagnostics,
            product_row_match=preview.product_row_match,
            open_method=preview.open_method,
            error="已找到 zip 候选，但 DOM 条目缺少可点击标记。",
        )
    try:
        download = await _click_entry_and_wait_for_download(page, entry_id)
        suggested = getattr(download, "suggested_filename", None) or str(chosen.get("text") or "")
        target_path = unique_zip_target_path(target_folder, suggested or _fallback_zip_filename(item))
        await download.save_as(str(target_path))
        return _base_result(
            CUSTOM_ZIP_DOWNLOADED,
            item,
            zip_filename=target_path.name,
            zip_path=str(target_path),
            trigger_text=preview.trigger_text,
            zip_candidates=list(preview.zip_candidates),
            zip_candidate_entries=list(preview.zip_candidate_entries),
            diagnostics=preview.diagnostics,
            product_row_match=preview.product_row_match,
            open_method=preview.open_method,
            prepared_before_writeback=preview.prepared_before_writeback,
        )
    except Exception as exc:
        return _base_result(
            CUSTOM_ZIP_DOWNLOAD_ERROR,
            item,
            trigger_text=preview.trigger_text,
            zip_candidates=list(preview.zip_candidates),
            zip_candidate_entries=list(preview.zip_candidate_entries),
            diagnostics=preview.diagnostics,
            product_row_match=preview.product_row_match,
            open_method=preview.open_method,
            prepared_before_writeback=preview.prepared_before_writeback,
            error=_short_error(exc),
        )


async def download_order_custom_zip(
    page,
    item: BatchOrderItem,
    target_folder: str | Path | None,
    *,
    enabled: bool = True,
    download: bool = True,
    prepared_result: CustomZipDownloadResult | None = None,
    **_: Any,
) -> CustomZipDownloadResult:
    """通过领星订单详情页 DOM 下载当前商品行附件浮层底部的定制化 zip。"""
    if not enabled:
        return _base_result(CUSTOM_ZIP_DISABLED, item)
    if download and not target_folder:
        return _base_result(CUSTOM_ZIP_SKIPPED_NO_FOLDER, item, error="订单文件夹未创建成功，跳过定制化 zip 下载。")
    if download and target_folder and not Path(target_folder).exists():
        return _base_result(CUSTOM_ZIP_SKIPPED_NO_FOLDER, item, error="订单文件夹不存在，跳过定制化 zip 下载。")
    if not download:
        if prepared_result is not None:
            return prepared_result
        return await prepare_order_custom_zip(page=page, item=item, enabled=enabled)
    if page is None:
        return _base_result(CUSTOM_ZIP_TRIGGER_NOT_FOUND, item, error="缺少 Playwright page，无法下载领星 DOM 附件。")

    # prepared_result 只作为“写回前已确认有 zip 候选”的预读日志；真正下载时仍重新定位
    # 当前商品行，避免保存到错误订单文件夹，也避免复用已消失的浮层 DOM 节点。
    result = await _download_dom_zip(page, item, Path(target_folder))
    if prepared_result and result.status == CUSTOM_ZIP_DOWNLOADED:
        result.prepared_before_writeback = prepared_result.prepared_before_writeback
    return result
