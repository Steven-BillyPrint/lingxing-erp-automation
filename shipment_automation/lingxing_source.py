from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

from lingxing_automation.browser.session import get_first_page, launch_context, wait_for_order_page
from lingxing_automation.config import load_login_config
from lingxing_automation.constants import ORDER_MANAGEMENT_URL
from lingxing_automation.models import LoginConfig
from lingxing_automation.pages.order_detail_navigation import close_order_detail_dialog
from lingxing_automation.pages.order_list import (
    click_next_batch_page,
    collect_visible_batch_order_rows,
    ensure_order_table_columns_visible,
    ensure_order_view_mode,
    ensure_page_size_1000,
    reset_order_table_vertical_scroll,
    scroll_order_table_down,
    set_order_table_vertical_scroll,
    wait_for_visible_batch_order_rows,
)
from lingxing_automation.pages.order_table_actions import (
    read_order_table_total_count,
    reset_order_filters,
    switch_order_tab,
)

from .candidate_scanner import (
    _compact_report_for_log,
    apply_queue_results,
    build_shipment_scan_report,
    normalized_shipment_tag,
    report_to_dict,
)
from .config import DEFAULT_SHIPMENT_QUEUE_PATH, SHIPMENT_TAG_NAME
from .models import ShipmentScanReport
from .queue_store import ShipmentQueueStore, utc_now


SHIPMENT_REQUIRED_ORDER_COLUMNS = ("platform", "tag", "customerRemark")
SHIPMENT_REQUIRED_ROW_FIELDS = ("system", "platform", "tag", "customer_remark")


def is_complete_pending_snapshot(
    *,
    limit: int,
    row_count: int,
    total_before: int | None,
    total_after: int | None,
    incomplete_field_count: int = 0,
) -> bool:
    return bool(
        not limit
        and total_before is not None
        and total_after == total_before
        and row_count == total_before
        and incomplete_field_count == 0
    )


def merge_collected_order_rows(
    rows_by_key: dict[str, dict[str, Any]],
    incoming_rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """Merge VXE fragments and repeated virtual-scroll snapshots by rowid."""

    inserted = 0
    enriched = 0
    for incoming in incoming_rows:
        key = _erp_order_row_key(incoming)
        if not key:
            continue
        existing = rows_by_key.get(key)
        if existing is None:
            rows_by_key[key] = dict(incoming)
            inserted += 1
            continue
        merged = _merge_order_row(existing, incoming)
        if merged != existing:
            rows_by_key[key] = merged
            enriched += 1
    return inserted, enriched


def missing_required_row_fields(row: dict[str, Any]) -> list[str]:
    presence = row.get("field_presence")
    if not isinstance(presence, dict):
        return []
    return [field for field in SHIPMENT_REQUIRED_ROW_FIELDS if not bool(presence.get(field))]


def build_recovery_scroll_positions(scroll_state: dict[str, Any], *, reverse: bool = False) -> list[int]:
    maximum = max(0, int(scroll_state.get("maxScrollTop") or 0))
    client_height = max(1, int(scroll_state.get("clientHeight") or 1))
    step = max(120, round(client_height * 0.30))
    positions = list(range(0, maximum + 1, step))
    if not positions or positions[-1] != maximum:
        positions.append(maximum)
    return list(reversed(positions)) if reverse else positions


def _merge_order_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    text_fields = (
        "system_order_no",
        "platform_order_no",
        "row_text",
        "asin_text",
        "asin",
        "sku",
        "status_text",
        "tag_text",
        "customer_remark",
        "paid_at_text",
        "logistics",
    )
    for field in text_fields:
        old_value = str(merged.get(field) or "")
        new_value = str(incoming.get(field) or "")
        if len(new_value) > len(old_value):
            merged[field] = incoming.get(field)
    if not str(merged.get("rowid") or "").strip() and incoming.get("rowid"):
        merged["rowid"] = incoming["rowid"]
    old_presence = merged.get("field_presence") if isinstance(merged.get("field_presence"), dict) else {}
    new_presence = incoming.get("field_presence") if isinstance(incoming.get("field_presence"), dict) else {}
    merged["field_presence"] = {
        field: bool(old_presence.get(field) or new_presence.get(field))
        for field in set(old_presence) | set(new_presence) | set(SHIPMENT_REQUIRED_ROW_FIELDS)
    }
    merged["row_sources"] = sorted(
        set(merged.get("row_sources") or []) | set(incoming.get("row_sources") or [])
    )
    return merged


async def _collect_stable_visible_rows(page, page_number: int, scroll_top: int) -> list[dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    previous_keys: tuple[str, ...] | None = None
    stable_count = 0
    for _ in range(4):
        visible_rows = await collect_visible_batch_order_rows(page, page_number, scroll_top)
        merge_collected_order_rows(snapshots, visible_rows)
        current_keys = tuple(sorted(_erp_order_row_key(row) for row in visible_rows if _erp_order_row_key(row)))
        if current_keys and current_keys == previous_keys:
            stable_count += 1
            if stable_count >= 1:
                break
        else:
            stable_count = 0
        previous_keys = current_keys
        await page.wait_for_timeout(250)
    return list(snapshots.values())


async def _recover_current_page(
    page,
    *,
    page_number: int,
    rows_by_key: dict[str, dict[str, Any]],
    scroll_state: dict[str, Any],
    reverse: bool,
    debug: dict[str, Any] | None,
) -> None:
    pass_debug = {
        "page": page_number,
        "direction": "bottom_to_top" if reverse else "top_to_bottom",
        "rows_before": len(rows_by_key),
        "positions": [],
    }
    if debug is not None:
        debug.setdefault("recovery_passes", []).append(pass_debug)
    for position in build_recovery_scroll_positions(scroll_state, reverse=reverse):
        state = await set_order_table_vertical_scroll(page, position)
        if not state.get("ok"):
            pass_debug["error"] = state.get("reason") or "补扫滚动失败。"
            break
        await page.wait_for_timeout(250)
        visible_rows = await _collect_stable_visible_rows(page, page_number, int(state.get("scrollTop") or position))
        inserted, enriched = merge_collected_order_rows(rows_by_key, visible_rows)
        pass_debug["positions"].append(
            {
                "scroll_top": int(state.get("scrollTop") or position),
                "visible_rows": len(visible_rows),
                "new_rows": inserted,
                "enriched_rows": enriched,
            }
        )
    pass_debug["rows_after"] = len(rows_by_key)


async def collect_lingxing_shipment_rows(
    page,
    *,
    limit: int = 0,
    debug: dict[str, Any] | None = None,
    _snapshot_attempt: int = 1,
) -> list[dict[str, Any]]:
    """Collect ERP order rows needed by shipment automation from Lingxing."""

    if debug is not None:
        debug.setdefault("visited_pages", [])
        debug.setdefault("scroll_steps", [])
        debug.setdefault("warnings", [])

    await ensure_page_size_1000(page, debug)
    await ensure_order_table_columns_visible(page, SHIPMENT_REQUIRED_ORDER_COLUMNS, debug)
    expected_total = await read_order_table_total_count(page)
    if expected_total != 0:
        await wait_for_visible_batch_order_rows(page, debug)

    rows_by_key: dict[str, dict[str, Any]] = {}
    page_number = 1
    max_pages = 50
    stop = False
    while page_number <= max_pages and not stop:
        reset_state = await reset_order_table_vertical_scroll(page)
        current_scroll_top = int(reset_state.get("scrollTop") or 0)
        page_debug = {
            "page": page_number,
            "snapshot_attempt": _snapshot_attempt,
            "reset": reset_state,
            "screens": [],
        }
        if debug is not None:
            debug["visited_pages"].append(page_debug)

        consecutive_no_new_screens = 0
        for screen_index in range(120):
            visible_rows = await collect_visible_batch_order_rows(page, page_number, current_scroll_top)
            new_count, enriched_count = merge_collected_order_rows(rows_by_key, visible_rows)
            if limit and len(rows_by_key) >= limit:
                stop = True
            screen_debug = {
                "screen": screen_index + 1,
                "scroll_top": current_scroll_top,
                "visible_rows": len(visible_rows),
                "new_rows": new_count,
                "enriched_rows": enriched_count,
            }
            page_debug["screens"].append(screen_debug)
            if debug is not None:
                debug["scroll_steps"].append({"page": page_number, **screen_debug})
            if stop:
                break
            consecutive_no_new_screens = 0 if new_count else consecutive_no_new_screens + 1
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
            current_scroll_top = int(scroll_state.get("scrollTop") or current_scroll_top)
            await page.wait_for_timeout(450)

        needs_recovery = len(rows_by_key) < (expected_total or 0) or any(
            missing_required_row_fields(row) for row in rows_by_key.values()
        )
        if not stop and not limit and expected_total is not None and needs_recovery:
            await _recover_current_page(
                page,
                page_number=page_number,
                rows_by_key=rows_by_key,
                scroll_state=reset_state,
                reverse=False,
                debug=debug,
            )
            if len(rows_by_key) < expected_total or any(
                missing_required_row_fields(row) for row in rows_by_key.values()
            ):
                await _recover_current_page(
                    page,
                    page_number=page_number,
                    rows_by_key=rows_by_key,
                    scroll_state=reset_state,
                    reverse=True,
                    debug=debug,
                )

        if stop or not await click_next_batch_page(page):
            break
        page_number += 1
        await page.wait_for_timeout(1800)

    final_total = await read_order_table_total_count(page)
    if (
        not limit
        and _snapshot_attempt == 1
        and expected_total is not None
        and final_total is not None
        and final_total != expected_total
    ):
        if debug is not None:
            debug.setdefault("snapshot_restarts", []).append(
                {"attempt": 1, "total_before": expected_total, "total_after": final_total}
            )
        return await collect_lingxing_shipment_rows(
            page,
            limit=limit,
            debug=debug,
            _snapshot_attempt=2,
        )
    rows = list(rows_by_key.values())
    if limit:
        rows = rows[:limit]
    incomplete_rows = [
        {
            "rowid": row.get("rowid") or row.get("system_order_no"),
            "missing_fields": missing_required_row_fields(row),
        }
        for row in rows
        if missing_required_row_fields(row)
    ]
    scan_complete = is_complete_pending_snapshot(
        limit=limit,
        row_count=len(rows),
        total_before=expected_total,
        total_after=final_total,
        incomplete_field_count=len(incomplete_rows),
    )
    if debug is not None:
        debug["row_count"] = len(rows)
        debug["table_total_count"] = final_total
        debug["table_total_count_before_scan"] = expected_total
        debug["scan_complete"] = scan_complete
        debug["incomplete_field_rows"] = incomplete_rows
        debug["incomplete_field_count"] = len(incomplete_rows)
        if not scan_complete:
            debug.setdefault("warnings", []).append(
                "待审核扫描不完整，本轮不会将缺失的历史队列订单标记为人工完成。"
            )
        debug["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rows


async def run_shipment_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Run phase-one shipment candidate scan from the Lingxing ERP order page."""

    tag_name = normalized_shipment_tag(getattr(args, "shipment_tag", None) or SHIPMENT_TAG_NAME)
    queue_path = str(Path(getattr(args, "queue_path", DEFAULT_SHIPMENT_QUEUE_PATH)).resolve())
    dry_run = bool(getattr(args, "dry_run", True))
    scan_limit = int(getattr(args, "scan_limit", 0) or 0)
    scan_started_at = utc_now()
    scan_run_id = uuid.uuid4().hex
    if not tag_name:
        report = ShipmentScanReport(
            status="config_missing",
            message="未配置专属发货标签。",
            dry_run=dry_run,
            queue_path=queue_path,
        )
        return report_to_dict(report)

    log_dir = Path(getattr(args, "log_dir", "logs")).resolve()
    login_config = LoginConfig()
    if not getattr(args, "no_auto_login", False):
        login_config = load_login_config(getattr(args, "env_path", ".env"))

    playwright, context = await launch_context(args)
    page = await get_first_page(context)
    try:
        await page.goto(ORDER_MANAGEMENT_URL, wait_until="domcontentloaded")
        await wait_for_order_page(
            page,
            int(getattr(args, "login_timeout_sec", 300)),
            login_config,
            auto_login=not getattr(args, "no_auto_login", False),
            debug_dir=getattr(args, "debug_log_dir", "debug/logs"),
        )
        await close_order_detail_dialog(page)
        await ensure_order_view_mode(page, debug_dir=getattr(args, "debug_log_dir", "debug/logs"))
        await switch_order_tab(page, "待审核")
        await reset_order_filters(page)
        debug: dict[str, Any] = {
            "shipment_tag_name": tag_name,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "run_id": scan_run_id,
            "scan_limit": scan_limit,
        }
        rows = await collect_lingxing_shipment_rows(
            page,
            limit=scan_limit,
            debug=debug,
        )
        report = build_shipment_scan_report(rows, tag_name, dry_run=dry_run, queue_path=queue_path)
        report.table_total_count = debug.get("table_total_count")
        report.scan_complete = bool(debug.get("scan_complete"))
        report.incomplete_field_count = int(debug.get("incomplete_field_count") or 0)
        if not report.scan_complete:
            report.status = "incomplete"
            report.message = "待审核扫描不完整，已禁止人工完成判定；已有队列仍可继续处理。"
        store = ShipmentQueueStore(queue_path)
        queue_results = [store.upsert_candidate(candidate, run_id=scan_run_id) for candidate in report.candidates]
        apply_queue_results(report, queue_results)
        if report.scan_complete:
            visible_system_orders = {
                str(row.get("system_order_no") or row.get("rowid") or "").strip()
                for row in rows
                if str(row.get("system_order_no") or row.get("rowid") or "").strip()
            }
            report.manual_completed = store.complete_missing_pending_orders(
                visible_system_orders,
                discovered_before=scan_started_at,
                run_id=scan_run_id,
            )
            report.manual_completed_count = len(report.manual_completed)
        debug["report"] = _compact_report_for_log(report)
        report.scan_log_file = write_shipment_scan_log(log_dir, debug)
        return report_to_dict(report)
    finally:
        if getattr(args, "keep_browser_open", False):
            print("Browser will stay open for inspection.")
        else:
            await context.close()
        await playwright.stop()


def write_shipment_scan_log(log_dir: Path, payload: dict[str, Any]) -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"shipment_scan_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _erp_order_row_key(row: dict[str, Any]) -> str:
    rowid = str(row.get("rowid") or "").strip()
    if rowid:
        return f"rowid:{rowid}"
    system_order_no = str(row.get("system_order_no") or "").strip()
    platform_order_no = str(row.get("platform_order_no") or "").strip()
    if not system_order_no:
        return ""
    return f"system:{system_order_no}:{platform_order_no}"

