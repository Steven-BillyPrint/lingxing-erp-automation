from __future__ import annotations

import argparse
import json
import time
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
    wait_for_visible_batch_order_rows,
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
from .queue_store import ShipmentQueueStore


SHIPMENT_REQUIRED_ORDER_COLUMNS = ("platform", "tag", "customerRemark")


async def collect_lingxing_shipment_rows(
    page,
    *,
    limit: int = 0,
    debug: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect ERP order rows needed by shipment automation from Lingxing."""

    if debug is not None:
        debug.setdefault("visited_pages", [])
        debug.setdefault("scroll_steps", [])
        debug.setdefault("warnings", [])

    await ensure_page_size_1000(page, debug)
    await ensure_order_table_columns_visible(page, SHIPMENT_REQUIRED_ORDER_COLUMNS, debug)
    await wait_for_visible_batch_order_rows(page, debug)

    rows: list[dict[str, Any]] = []
    seen_rows: set[str] = set()
    page_number = 1
    max_pages = 50
    stop = False
    while page_number <= max_pages and not stop:
        reset_state = await reset_order_table_vertical_scroll(page)
        current_scroll_top = int(reset_state.get("scrollTop") or 0)
        page_debug = {"page": page_number, "reset": reset_state, "screens": []}
        if debug is not None:
            debug["visited_pages"].append(page_debug)

        consecutive_no_new_screens = 0
        for screen_index in range(120):
            visible_rows = await collect_visible_batch_order_rows(page, page_number, current_scroll_top)
            new_count = 0
            for row in visible_rows:
                key = _erp_order_row_key(row)
                if not key or key in seen_rows:
                    continue
                seen_rows.add(key)
                rows.append(row)
                new_count += 1
                if limit and len(rows) >= limit:
                    stop = True
                    break
            screen_debug = {
                "screen": screen_index + 1,
                "scroll_top": current_scroll_top,
                "visible_rows": len(visible_rows),
                "new_rows": new_count,
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

        if stop or not await click_next_batch_page(page):
            break
        page_number += 1
        await page.wait_for_timeout(1800)

    if debug is not None:
        debug["row_count"] = len(rows)
        debug["scan_finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return rows


async def run_shipment_scan(args: argparse.Namespace) -> dict[str, Any]:
    """Run phase-one shipment candidate scan from the Lingxing ERP order page."""

    tag_name = normalized_shipment_tag(getattr(args, "shipment_tag", None) or SHIPMENT_TAG_NAME)
    queue_path = str(Path(getattr(args, "queue_path", DEFAULT_SHIPMENT_QUEUE_PATH)).resolve())
    dry_run = bool(getattr(args, "dry_run", True))
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
        debug: dict[str, Any] = {
            "shipment_tag_name": tag_name,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rows = await collect_lingxing_shipment_rows(
            page,
            limit=int(getattr(args, "scan_limit", 0) or 0),
            debug=debug,
        )
        report = build_shipment_scan_report(rows, tag_name, dry_run=dry_run, queue_path=queue_path)
        store = ShipmentQueueStore(queue_path)
        apply_queue_results(report, store.insert_candidates(report.candidates))
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
    return f"{row.get('system_order_no') or ''}:{row.get('platform_order_no') or ''}"

