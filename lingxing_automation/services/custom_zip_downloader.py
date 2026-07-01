from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from ..models import CustomZipFile, OrderCustomZipBundle
from .custom_attachment_downloader import (
    ENTRY_ATTR,
    TRIGGER_ATTR,
    _click_entry_and_wait_for_download,
    _dismiss_attachment_popovers,
    _open_attachment_popover,
    sanitize_zip_filename,
    unique_zip_target_path,
    zip_candidate_names,
)

CUSTOM_ZIP_DISABLED = "custom_zip_disabled"
CUSTOM_ZIP_DOWNLOADED = "custom_zip_downloaded"
CUSTOM_ZIP_ROW_NOT_FOUND = "custom_zip_row_not_found"
CUSTOM_ZIP_TRIGGER_NOT_FOUND = "custom_zip_trigger_not_found"
CUSTOM_ZIP_NOT_FOUND = "custom_zip_not_found"
CUSTOM_ZIP_DOWNLOAD_ERROR = "custom_zip_download_error"

DUPLICATE_DOWNLOAD_SUFFIX_RE = re.compile(r"\s+\(\d+\)(?=\.zip$)", re.IGNORECASE)
ZIP_FILENAME_ASIN_RE = re.compile(r"^(B0[A-Z0-9]{8})(?:_|$)", re.IGNORECASE)


def _canonical_zip_download_name(filename: str | None) -> str:
    """规范化下载得到的 zip 文件名，便于去重和匹配。"""
    cleaned = sanitize_zip_filename(filename)
    return DUPLICATE_DOWNLOAD_SUFFIX_RE.sub("", cleaned).lower()


def _asin_from_zip_filename(filename: str | None) -> str | None:
    """从 zip 文件名中提取 ASIN。"""
    match = ZIP_FILENAME_ASIN_RE.search(str(filename or ""))
    return match.group(1).upper() if match else None


def _order_item_id_from_zip_path(zip_path: Path) -> str | None:
    """从 zip 文件路径中提取订单行 ID。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            json_names = sorted(name for name in archive.namelist() if name.lower().endswith(".json"))
            for name in json_names:
                try:
                    with archive.open(name) as handle:
                        data = json.loads(handle.read().decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                order_item_id = str(data.get("orderItemId") or data.get("OrderItemId") or "").strip()
                if order_item_id:
                    return order_item_id
    except (OSError, zipfile.BadZipFile):
        return None
    return None


def _expected_order_items_covered(zip_files: list[CustomZipFile], expected_order_item_ids: set[str] | None) -> bool:
    """判断已下载文件是否覆盖所有预期订单行。"""
    if not expected_order_item_ids:
        return False
    downloaded = {str(item.order_item_id or "") for item in zip_files if item.order_item_id}
    return expected_order_item_ids <= downloaded


def _downloaded_order_item_ids(zip_files: list[CustomZipFile]) -> set[str]:
    """处理已下载 订单行 ID 集合相关逻辑，并返回后续流程所需结果。"""
    return {str(item.order_item_id) for item in zip_files if item.order_item_id}


def _missing_expected_order_item_ids(
    zip_files: list[CustomZipFile],
    expected_order_item_ids: set[str],
) -> list[str]:
    """计算仍未下载到 zip 的预期订单行 ID。"""
    return sorted(expected_order_item_ids - _downloaded_order_item_ids(zip_files))


def _warn_failed_custom_zip_targets(warnings: list[str], failed_files: list[CustomZipFile]) -> None:
    # 预期订单项缺失时，失败入口只是辅助诊断；不能让不可见的重复按钮盖住真正缺哪个 JSON。
    """汇总失败的 zip 下载目标并生成告警信息。"""
    for item in failed_files:
        error = (item.error or item.status or "").replace("\n", " ").strip()
        warnings.append(f"custom_zip_target_failed:{item.row_index}:{item.status}:{error[:200]}")


def _existing_staging_zip_files(
    staging_dir: Path,
    platform_order_no: str,
    *,
    expected_order_item_ids: set[str] | None = None,
) -> list[CustomZipFile]:
    """读取暂存目录中已经存在的 zip 文件。"""
    latest_by_identity: dict[str, Path] = {}
    order_item_id_by_identity: dict[str, str | None] = {}
    for path in staging_dir.glob("*.zip"):
        if not path.is_file():
            continue
        order_item_id = _order_item_id_from_zip_path(path)
        key = f"order_item:{order_item_id}" if order_item_id else f"name:{_canonical_zip_download_name(path.name)}"
        previous = latest_by_identity.get(key)
        if previous is None or path.stat().st_mtime >= previous.stat().st_mtime:
            latest_by_identity[key] = path
            order_item_id_by_identity[key] = order_item_id
    chosen_items = sorted(latest_by_identity.items(), key=lambda item: item[1].name.lower())
    if expected_order_item_ids:
        chosen_items = [
            item
            for item in chosen_items
            if not order_item_id_by_identity.get(item[0]) or order_item_id_by_identity[item[0]] in expected_order_item_ids
        ]
    return [
        CustomZipFile(
            row_index=index + 1,
            asin=_asin_from_zip_filename(path.name),
            sku=None,
            msku=None,
            platform_order_no=platform_order_no,
            trigger_text="existing_staging_zip",
            zip_filename=path.name,
            zip_path=str(path),
            zip_candidates=[path.name],
            order_item_id=order_item_id_by_identity.get(key),
            status=CUSTOM_ZIP_DOWNLOADED,
        )
        for index, (key, path) in enumerate(chosen_items)
    ]


def _target_position_key(target: dict[str, Any]) -> str | None:
    """生成页面附件入口的位置排序键。"""
    try:
        top = float(target.get("trigger_top"))
        left = float(target.get("trigger_left"))
    except (TypeError, ValueError):
        return None
    return f"pos:{round(top / 2)}:{round(left / 2)}"


def _filter_interactable_zip_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤掉被弹窗遮挡或不可操作的重复附件入口，避免同一商品行重复下载 zip。"""

    deduped_by_key: dict[str, dict[str, Any]] = {}
    key_order: list[str] = []
    for index, target in enumerate(targets):
        key = _target_position_key(target) or str(target.get("trigger_id") or f"target:{index}")
        if key not in deduped_by_key:
            key_order.append(key)
        previous = deduped_by_key.get(key)
        if previous and previous.get("trigger_is_interactable") is not False and target.get("trigger_is_interactable") is False:
            continue
        # Keep off-screen rows too. _open_attachment_popover scrolls the marked trigger
        # into view before opening it, so discovery must not discard lower product rows.
        deduped_by_key[key] = target
    deduped = [deduped_by_key[key] for key in key_order]
    return [{**target, "row_index": index + 1} for index, target in enumerate(deduped)]


async def _find_product_zip_targets(page, system_order_no: str) -> list[dict[str, Any]]:
    """查找商品行中可交互的定制化 zip 下载入口。"""
    return await page.evaluate(
        """
        ({ systemOrderNo, triggerAttr }) => {
            const marker = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
            };
            const inViewport = (rect) =>
                rect.bottom >= 0 && rect.top <= window.innerHeight &&
                rect.right >= 0 && rect.left <= window.innerWidth;
            const isTopLayer = (el) => {
                if (!visible(el)) return false;
                const rect = el.getBoundingClientRect();
                if (!inViewport(rect)) return false;
                const points = [
                    [rect.left + rect.width / 2, rect.top + rect.height / 2],
                    [rect.left + Math.max(1, rect.width * 0.2), rect.top + rect.height / 2],
                    [rect.right - Math.max(1, rect.width * 0.2), rect.top + rect.height / 2],
                ];
                return points.some(([rawX, rawY]) => {
                    const x = Math.min(window.innerWidth - 1, Math.max(0, rawX));
                    const y = Math.min(window.innerHeight - 1, Math.max(0, rawY));
                    const top = document.elementFromPoint(x, y);
                    return Boolean(top && (top === el || el.contains(top) || top.contains(el)));
                });
            };
            const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
            const isAttachmentCountText = (text) => /^\\u5171\\s*\\d+$/.test(String(text || ''));
            const extractAsin = (text) => {
                const match = String(text || '').toUpperCase().match(/B0[A-Z0-9]{8}/);
                return match ? match[0] : '';
            };
            const extractValueAfter = (text, label) => {
                const pattern = new RegExp(`${label}\\s+(.+?)(?=\\s+(?:商品ID|ASIN|MSKU|参考号|品名|SKU|订单信息|交易信息|其他信息|更多商品信息)\\b|$)`, 'i');
                const match = String(text || '').match(pattern);
                return match ? match[1].replace(/\\s+共\\s*\\d+\\s*$/, '').trim() : '';
            };
            const modalSelector = '.el-dialog__wrapper,.el-dialog,.vxe-modal--wrapper,.vxe-modal--box,.ant-modal,.ant-drawer,.el-drawer,.order-detail-dialog,[role="dialog"],[aria-modal="true"]';
            const detailRoots = Array.from(document.querySelectorAll(
                `${modalSelector},main,section,article,div`
            ))
                .filter((el) => {
                    if (el === document.body || el === document.documentElement || !visible(el)) return false;
                    const text = textOf(el);
                    return text.includes(systemOrderNo) && /商品信息/.test(text) && /B0[A-Z0-9]{8}/i.test(text);
                })
                .map((el) => ({ el, text: textOf(el), isModal: el.matches(modalSelector), topLayer: isTopLayer(el) }))
                .filter((item) => item.text.length >= 80 && item.text.length < 100000)
                .sort((a, b) => {
                    const score = (item) =>
                        (item.isModal ? 200000 : 0) +
                        (item.topLayer ? 100000 : 0) -
                        item.text.length / 100;
                    return score(b) - score(a) || a.text.length - b.text.length;
                })
                .map((item) => item.el);
            const root = detailRoots[0] || document.body;
            const rowSelector = 'tr,[role="row"],.vxe-body--row,.el-table__row,.ant-table-row,li';
            const asinNodes = Array.from(root.querySelectorAll('span,div,p,td,th,b,strong,a'))
                .filter((el) => visible(el) && isTopLayer(el) && /^B0[A-Z0-9]{8}$/i.test(textOf(el)));
            const triggerNodesForScope = (scope, requireTopLayer = true) =>
                Array.from(scope.querySelectorAll('a,span,div,p,td,th,b,strong,button'))
                    .filter((node) => visible(node) && (!requireTopLayer || isTopLayer(node)) && isAttachmentCountText(textOf(node)))
                    .map((node) => ({
                        node,
                        text: textOf(node),
                        rect: node.getBoundingClientRect(),
                        topLayer: isTopLayer(node),
                        className: String(node.className || ''),
                    }));
            const candidates = [];
            const addCandidate = (el, asinNode, reason) => {
                if (!el || el === document.body || el === document.documentElement || !visible(el)) return;
                const text = textOf(el);
                const asin = extractAsin(textOf(asinNode)) || extractAsin(text);
                if (!asin || text.length < 120 || text.length > 20000) return;
                if (!/商品ID|ASIN|SKU|MSKU|订单信息|交易信息|其他信息|更多商品信息/.test(text)) return;
                const triggers = triggerNodesForScope(el, true);
                if (!triggers.length) return;
                const rect = el.getBoundingClientRect();
                const asinRect = asinNode.getBoundingClientRect();
                candidates.push({ el, text, asin, rect, asinTop: asinRect.top, reason, triggers });
            };
            for (const row of Array.from(root.querySelectorAll(rowSelector))) {
                if (!visible(row)) continue;
                const rowText = textOf(row);
                const rowAsin = extractAsin(rowText);
                if (!rowAsin || rowText.length < 80 || rowText.length > 20000) continue;
                if (!/ASIN|SKU|MSKU|鍟嗗搧ID/.test(rowText)) continue;
                const triggers = triggerNodesForScope(row, false);
                if (!triggers.length) continue;
                const rect = row.getBoundingClientRect();
                candidates.push({
                    el: row,
                    text: rowText,
                    asin: rowAsin,
                    rect,
                    asinTop: rect.top,
                    reason: 'product-table-row',
                    triggers,
                });
            }
            for (const asinNode of asinNodes) {
                addCandidate(asinNode.closest(rowSelector), asinNode, 'closest-row');
                let node = asinNode;
                for (let depth = 0; depth < 18 && node && node !== document.body; depth += 1) {
                    addCandidate(node, asinNode, `ancestor-${depth}`);
                    node = node.parentElement;
                }
            }
            const triggerSeedNodes = Array.from(root.querySelectorAll('a,span,div,p,td,th,b,strong,button'))
                .filter((el) => visible(el) && isAttachmentCountText(textOf(el)));
            const overlapsY = (a, b, tolerance = 22) =>
                Math.max(a.top, b.top) <= Math.min(a.bottom, b.bottom) + tolerance;
            for (const asinNode of asinNodes) {
                const row = asinNode.closest(rowSelector);
                const rowRect = (row || asinNode).getBoundingClientRect();
                const asinRect = asinNode.getBoundingClientRect();
                const rowText = textOf(row || asinNode);
                const rowAsin = extractAsin(textOf(asinNode)) || extractAsin(rowText);
                if (!rowAsin || !row || !visible(row)) continue;
                const rowTriggers = triggerSeedNodes
                    .map((node) => ({
                        node,
                        text: textOf(node),
                        rect: node.getBoundingClientRect(),
                        topLayer: isTopLayer(node),
                        className: String(node.className || ''),
                    }))
                    .filter((trigger) =>
                        overlapsY(rowRect, trigger.rect) ||
                        Math.abs((trigger.rect.top + trigger.rect.bottom) / 2 - (asinRect.top + asinRect.bottom) / 2) <= 40
                    )
                    .sort((a, b) =>
                        Math.abs((a.rect.top + a.rect.bottom) / 2 - (asinRect.top + asinRect.bottom) / 2) -
                        Math.abs((b.rect.top + b.rect.bottom) / 2 - (asinRect.top + asinRect.bottom) / 2)
                    );
                if (!rowTriggers.length) continue;
                candidates.push({
                    el: row,
                    text: rowText,
                    asin: rowAsin,
                    rect: rowRect,
                    asinTop: asinRect.top,
                    reason: 'vertical-trigger-match',
                    triggers: [rowTriggers[0]],
                });
            }
            for (const triggerNode of triggerSeedNodes) {
                addCandidate(triggerNode.closest(rowSelector), triggerNode, 'trigger-closest-row');
                let node = triggerNode;
                for (let depth = 0; depth < 14 && node && node !== document.body; depth += 1) {
                    addCandidate(node, triggerNode, `trigger-ancestor-${depth}`);
                    node = node.parentElement;
                }
            }
            candidates.sort((a, b) => {
                const closestTriggerDistance = (item) => Math.min(
                    ...item.triggers.map((trigger) => Math.abs(trigger.rect.top - item.asinTop))
                );
                const rowHeightPenalty = (item) => Math.max(0, item.rect.height - 320) / 2;
                const score = (item) =>
                    (item.reason === 'product-table-row' ? 9000 : 0) +
                    (item.reason === 'closest-row' ? 5000 : 0) +
                    (item.reason === 'vertical-trigger-match' ? 4800 : 0) +
                    (/更多商品信息/.test(item.text) ? 1000 : 0) +
                    (/订单信息/.test(item.text) ? 200 : 0) +
                    (/交易信息/.test(item.text) ? 100 : 0) -
                    Math.abs(item.rect.top - item.asinTop) -
                    closestTriggerDistance(item) * 3 -
                    rowHeightPenalty(item) -
                    item.text.length / 100;
                return score(b) - score(a);
            });
            const chosenRows = [];
            const seen = new Set();
            for (const row of candidates) {
                const keyTop = row.reason === 'product-table-row' ? row.rect.top : row.asinTop;
                const key = `${row.asin}:${Math.round(keyTop / 8)}`;
                if (seen.has(key)) continue;
                seen.add(key);
                chosenRows.push(row);
            }
            chosenRows.sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
            const output = [];
            const seenTriggerNodes = new WeakSet();
            for (const row of chosenRows) {
                const trigger = row.triggers
                    .map((item) => ({
                        ...item,
                        score:
                            (/product-collapse|el-popover__reference/.test(String(item.className || '')) ? 2000 : 0) +
                            (item.rect.left > row.rect.left + row.rect.width * 0.55 ? 1000 : 0) -
                            Math.abs(item.rect.top - row.asinTop) * 10 +
                            item.rect.left / 10000,
                    }))
                    .sort((a, b) => b.score - a.score)[0];
                if (!trigger || seenTriggerNodes.has(trigger.node)) continue;
                seenTriggerNodes.add(trigger.node);
                const triggerId = `lx-custom-json-zip-trigger-${marker}-${output.length}`;
                trigger.node.setAttribute(triggerAttr, triggerId);
                output.push({
                    row_index: output.length + 1,
                    asin: row.asin,
                    sku: extractValueAfter(row.text, 'SKU'),
                    msku: extractValueAfter(row.text, 'MSKU'),
                    trigger_id: triggerId,
                    trigger_text: trigger.text,
                    trigger_is_interactable: Boolean(trigger.topLayer),
                    trigger_top: trigger.rect.top,
                    trigger_left: trigger.rect.left,
                    row_text_preview: row.text.slice(0, 500),
                });
            }
            return output;
        }
        """,
        {"systemOrderNo": system_order_no, "triggerAttr": TRIGGER_ATTR},
    )


async def download_order_custom_zip_bundle(
    page,
    *,
    platform_order_no: str,
    system_order_no: str,
    staging_root: str | Path = "logs/custom_zip_staging",
    enabled: bool = True,
    expected_zip_count: int | None = None,
    expected_order_item_ids: set[str] | None = None,
) -> OrderCustomZipBundle:
    """逐商品行下载定制化 zip 到 staging。

    ERP 浮窗文字只用于找到 zip 下载入口，不再作为定制化业务数据源；
    业务数据统一从 zip 内 JSON 读取，避免 Notes、换行和浮窗截断导致解析错误。
    """

    if not enabled:
        return OrderCustomZipBundle(platform_order_no=platform_order_no, status=CUSTOM_ZIP_DISABLED, error="已通过 CLI 禁用定制 zip 下载。")
    staging_dir = Path(staging_root) / platform_order_no
    staging_dir.mkdir(parents=True, exist_ok=True)
    zip_files: list[CustomZipFile] = []
    failed_files: list[CustomZipFile] = []
    warnings: list[str] = []
    downloaded_filenames: set[str] = set()
    downloaded_order_item_ids: set[str] = set()
    if expected_zip_count is not None:
        zip_files = _existing_staging_zip_files(
            staging_dir,
            platform_order_no,
            expected_order_item_ids=expected_order_item_ids,
        )
        downloaded_filenames = {_canonical_zip_download_name(item.zip_filename) for item in zip_files}
        downloaded_order_item_ids = {str(item.order_item_id) for item in zip_files if item.order_item_id}
        if zip_files:
            warnings.extend(f"existing_custom_zip_reused:{item.zip_filename}" for item in zip_files)
        if _expected_order_items_covered(zip_files, expected_order_item_ids) or (
            expected_order_item_ids is None and len(zip_files) >= expected_zip_count
        ):
            return OrderCustomZipBundle(
                platform_order_no=platform_order_no,
                zip_files=zip_files[:expected_zip_count],
                status="ok",
                warnings=warnings,
            )
    try:
        targets = await _find_product_zip_targets(page, system_order_no)
    except Exception as exc:
        return OrderCustomZipBundle(platform_order_no=platform_order_no, status=CUSTOM_ZIP_ROW_NOT_FOUND, error=str(exc)[:800])
    targets = _filter_interactable_zip_targets(targets)
    if not targets:
        return OrderCustomZipBundle(platform_order_no=platform_order_no, status=CUSTOM_ZIP_ROW_NOT_FOUND, error="没有定位到带 zip 附件入口的商品行。")

    seen_trigger_ids: set[str] = set()
    for target in targets:
        if _expected_order_items_covered(zip_files, expected_order_item_ids):
            break
        if expected_order_item_ids is None and expected_zip_count is not None and len(zip_files) >= expected_zip_count:
            break
        trigger_id = str(target.get("trigger_id") or "")
        if trigger_id and trigger_id in seen_trigger_ids:
            continue
        if trigger_id:
            seen_trigger_ids.add(trigger_id)
        row_index = int(target.get("row_index") or len(zip_files) + 1)
        base = {
            "row_index": row_index,
            "asin": str(target.get("asin") or "") or None,
            "sku": str(target.get("sku") or "") or None,
            "msku": str(target.get("msku") or "") or None,
            "platform_order_no": platform_order_no,
            "trigger_text": str(target.get("trigger_text") or "") or None,
        }
        try:
            entries, chosen, _method = await _open_attachment_popover(page, trigger_id)
            candidates = zip_candidate_names(entries)
            if not chosen:
                failed_files.append(
                    CustomZipFile(
                        **base,
                        zip_filename="",
                        zip_path="",
                        zip_candidates=candidates,
                        status=CUSTOM_ZIP_NOT_FOUND,
                        error="当前商品行附件浮层中没有找到 zip。",
                    )
                )
                continue
            entry_id = str(chosen.get("entry_id") or "")
            if not entry_id:
                failed_files.append(
                    CustomZipFile(
                        **base,
                        zip_filename="",
                        zip_path="",
                        zip_candidates=candidates,
                        status=CUSTOM_ZIP_TRIGGER_NOT_FOUND,
                        error="zip 候选缺少可点击 DOM 标记。",
                    )
                )
                continue
            download = await _click_entry_and_wait_for_download(page, entry_id)
            suggested = getattr(download, "suggested_filename", None) or str(chosen.get("text") or "")
            filename = sanitize_zip_filename(suggested)
            canonical_filename = _canonical_zip_download_name(filename)
            if expected_order_item_ids is None and canonical_filename in downloaded_filenames:
                warnings.append(f"duplicate_custom_zip_skipped:{filename}")
                await _dismiss_attachment_popovers(page)
                continue
            target_path = unique_zip_target_path(staging_dir, filename)
            await download.save_as(str(target_path))
            await _dismiss_attachment_popovers(page)
            order_item_id = _order_item_id_from_zip_path(target_path)
            if expected_order_item_ids is not None and order_item_id and order_item_id in downloaded_order_item_ids:
                warnings.append(f"duplicate_custom_zip_order_item_skipped:{order_item_id}:{target_path.name}")
                continue
            downloaded_filenames.add(canonical_filename)
            if order_item_id:
                downloaded_order_item_ids.add(order_item_id)
            zip_files.append(
                CustomZipFile(
                    **base,
                    zip_filename=target_path.name,
                    zip_path=str(target_path),
                    zip_candidates=candidates,
                    order_item_id=order_item_id,
                    status=CUSTOM_ZIP_DOWNLOADED,
                )
            )
        except Exception as exc:
            failed_files.append(
                CustomZipFile(
                    **base,
                    zip_filename="",
                    zip_path="",
                    status=CUSTOM_ZIP_DOWNLOAD_ERROR,
                    error=str(exc).splitlines()[0][:800] if str(exc) else exc.__class__.__name__,
                )
            )
    if expected_order_item_ids is not None:
        if _expected_order_items_covered(zip_files, expected_order_item_ids):
            return OrderCustomZipBundle(
                platform_order_no=platform_order_no,
                zip_files=zip_files,
                status="ok",
                warnings=warnings,
            )
        missing = _missing_expected_order_item_ids(zip_files, expected_order_item_ids)
        if missing:
            _warn_failed_custom_zip_targets(warnings, failed_files)
            return OrderCustomZipBundle(
                platform_order_no=platform_order_no,
                zip_files=[*zip_files, *failed_files],
                status=CUSTOM_ZIP_NOT_FOUND,
                error=f"定制 zip 缺少 Amazon OrderItemId：{', '.join(missing)}",
                warnings=warnings,
            )
        failed = failed_files[0] if failed_files else None
        return OrderCustomZipBundle(
            platform_order_no=platform_order_no,
            zip_files=[*zip_files, *failed_files],
            status=failed.status if failed else CUSTOM_ZIP_NOT_FOUND,
            error=failed.error if failed else "定制 zip 未覆盖预期 Amazon OrderItemId。",
            warnings=warnings,
        )
    if expected_zip_count is not None and len(zip_files) >= expected_zip_count:
        return OrderCustomZipBundle(
            platform_order_no=platform_order_no,
            zip_files=zip_files,
            status="ok",
            warnings=warnings,
        )
    if expected_zip_count is not None and len(zip_files) < expected_zip_count:
        failed = failed_files[0] if failed_files else None
        return OrderCustomZipBundle(
            platform_order_no=platform_order_no,
            zip_files=[*zip_files, *failed_files],
            status=failed.status if failed else CUSTOM_ZIP_NOT_FOUND,
            error=failed.error if failed else f"定制 zip 数量不足：需要 {expected_zip_count} 个，实际下载 {len(zip_files)} 个。",
            warnings=warnings,
        )
    zip_files.extend(failed_files)
    failed = next((item for item in zip_files if item.status != CUSTOM_ZIP_DOWNLOADED), None)
    return OrderCustomZipBundle(
        platform_order_no=platform_order_no,
        zip_files=zip_files,
        status=failed.status if failed else "ok",
        error=failed.error if failed else None,
        warnings=warnings,
    )
