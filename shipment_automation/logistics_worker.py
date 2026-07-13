from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from lingxing_automation.browser.session import launch_context

from .alibaba_session import wait_for_alibaba_logistics_detail
from .alibaba_logistics import (
    logistics_detail_url,
    logistics_readiness_decision,
    parse_json_payload,
    parse_logistics_detail_from_json_payloads,
    parse_logistics_detail_from_text,
)
from .config import DEFAULT_SHIPMENT_QUEUE_PATH, AlibabaLoginConfig, load_alibaba_login_config
from .models import (
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LogisticsDetail,
    LogisticsQueryResult,
    LogisticsWorkerReport,
    ReadyToMarkItem,
)
from .queue_store import ShipmentQueueStore


FetchLogisticsDetail = Callable[[str], Awaitable[LogisticsDetail]]

BROWSER_CLOSED_ERROR_MESSAGE = "浏览器关闭导致本轮查询失败，下轮继续重试。"
BROWSER_CLOSED_RETRY_MESSAGE = "浏览器在阿里物流查询中被关闭，已重启一次重试。"
BROWSER_CLOSED_ERROR_KEYWORDS = (
    "Target page, context or browser has been closed",
    "BrowserContext.new_page",
    "Page.wait_for_timeout",
    "browser has been closed",
    "context has been closed",
    "浏览器关闭导致本轮查询失败",
    "浏览器在阿里物流查询中被关闭",
)


class LogisticsBrowserClosedError(RuntimeError):
    """Raised when the automation browser/context is closed during logistics lookup."""


def is_browser_closed_error(error: object) -> bool:
    text = str(error or "").lower()
    return any(keyword.lower() in text for keyword in BROWSER_CLOSED_ERROR_KEYWORDS)


def compact_exception_message(error: object) -> str:
    text = str(error or "").strip()
    if is_browser_closed_error(error):
        if text.startswith(BROWSER_CLOSED_ERROR_MESSAGE) and "Browser logs:" not in text:
            return text[:500]
        return BROWSER_CLOSED_ERROR_MESSAGE
    if not text:
        return type(error).__name__
    text = text.split("\nBrowser logs:", 1)[0].split("\r\nBrowser logs:", 1)[0]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines[:3]).strip()
    return compact[:500] or type(error).__name__


async def run_logistics_worker(args: argparse.Namespace) -> dict[str, Any]:
    """Run one Alibaba logistics lookup pass."""

    queue_path = str(Path(getattr(args, "queue_path", DEFAULT_SHIPMENT_QUEUE_PATH)).resolve())
    update_queue = bool(getattr(args, "update_queue", False))
    dry_run = not update_queue
    store = ShipmentQueueStore(queue_path)
    login_config = AlibabaLoginConfig()
    if not getattr(args, "no_auto_login", False):
        login_config = load_alibaba_login_config(getattr(args, "env_path", ".env"))
    if not getattr(args, "from_queue", False):
        report = LogisticsWorkerReport(
            status="source_missing",
            message="请指定 --from-queue。本项目 CLI 不直接读取普通 Chrome 已打开标签页。",
            queue_path=queue_path,
            dry_run=dry_run,
            update_queue=update_queue,
            skipped_query_records=store.list_logistics_skipped_records(limit=50),
        )
        return logistics_report_to_dict(report)

    limit = int(getattr(args, "limit", 0) or 0)
    worker_id = f"logistics-{uuid.uuid4().hex}"
    rows = store.list_logistics_check_candidates(limit=limit)
    if not rows:
        report = LogisticsWorkerReport(
            status="completed",
            message="没有 NEW / NOT_READY / ERROR 物流记录需要查询。",
            queue_path=queue_path,
            dry_run=dry_run,
            update_queue=update_queue,
            ready_to_mark_items=store.list_ready_to_mark(),
            skipped_query_records=store.list_logistics_skipped_records(limit=50),
        )
        report.ready_count = len(report.ready_to_mark_items)
        return logistics_report_to_dict(report)

    playwright, context = await launch_context(args)
    browser_state: dict[str, Any] = {
        "playwright": playwright,
        "context": context,
        "restarted": False,
    }

    async def fetch_with_current_browser(logistics_no: str) -> LogisticsDetail:
        return await fetch_logistics_detail_from_page(
            browser_state["context"],
            logistics_no,
            login_config=login_config,
            auto_login=not getattr(args, "no_auto_login", False),
            login_timeout_sec=int(getattr(args, "login_timeout_sec", 300) or 300),
        )

    async def restart_browser_and_fetch(logistics_no: str) -> LogisticsDetail:
        if browser_state["restarted"]:
            return await fetch_with_current_browser(logistics_no)
        browser_state["restarted"] = True
        await _close_browser_state(browser_state)
        try:
            browser_state["playwright"], browser_state["context"] = await launch_context(args)
        except Exception as exc:
            raise LogisticsBrowserClosedError(
                f"{BROWSER_CLOSED_ERROR_MESSAGE} 重启浏览器失败：{compact_exception_message(exc)}"
            ) from exc
        return await fetch_with_current_browser(logistics_no)

    try:
        if update_queue:
            rows = store.claim_logistics_jobs(worker_id, limit=limit)
        report = await process_logistics_queue_once(
            store,
            fetch_detail=fetch_with_current_browser,
            retry_fetch_detail=restart_browser_and_fetch,
            limit=int(getattr(args, "limit", 0) or 0),
            update_queue=update_queue,
            dry_run=dry_run,
            preloaded_rows=rows,
            worker_id=worker_id if update_queue else None,
            run_id=uuid.uuid4().hex,
        )
        report.queue_path = queue_path
        return logistics_report_to_dict(report)
    finally:
        if getattr(args, "keep_browser_open", False):
            print("Browser will stay open for inspection.")
        else:
            await _close_browser_state(browser_state)


async def process_logistics_queue_once(
    store: ShipmentQueueStore,
    *,
    fetch_detail: FetchLogisticsDetail,
    retry_fetch_detail: FetchLogisticsDetail | None = None,
    limit: int = 0,
    update_queue: bool = False,
    dry_run: bool = True,
    preloaded_rows: list[dict[str, Any]] | None = None,
    worker_id: str | None = None,
    run_id: str | None = None,
) -> LogisticsWorkerReport:
    rows = preloaded_rows
    if rows is None:
        rows = store.list_logistics_check_candidates(limit=limit)
    rows = _dedupe_rows_by_logistics_no(rows)
    queried_logistics_numbers = {str(row.get("logistics_no") or "").strip() for row in rows if str(row.get("logistics_no") or "").strip()}
    report = LogisticsWorkerReport(
        status="completed",
        message="物流查询完成。",
        dry_run=dry_run,
        update_queue=update_queue,
        scanned_page_count=len(rows),
    )
    ready_this_run: list[ReadyToMarkItem] = []

    for row in rows:
        logistics_no = str(row.get("logistics_no") or "").strip()
        try:
            detail = await _fetch_detail_with_optional_retry(
                logistics_no,
                fetch_detail=fetch_detail,
                retry_fetch_detail=retry_fetch_detail,
                report=report,
            )
            if not detail.logistics_no:
                detail.logistics_no = logistics_no
            decision = logistics_readiness_decision(detail)
            logistics_state = decision.logistics_state
            last_error = None if decision.should_continue else decision.reason
            if update_queue:
                updated = store.complete_logistics_attempt(
                    logistics_no,
                    detail,
                    state=logistics_state,
                    last_error=last_error,
                    owner=worker_id,
                    expected_version=int(row.get("version") or 0) if worker_id else None,
                    run_id=run_id,
                )
                if not updated:
                    raise RuntimeError(f"物流任务租约或版本已变化：{logistics_no}")
            result = LogisticsQueryResult(
                system_order_no=str(row.get("system_order_no") or ""),
                platform_order_no=str(row.get("platform_order_no") or ""),
                logistics_no=logistics_no,
                status_text=detail.status_text,
                last_error=last_error,
                detail=detail,
                logistics_state=logistics_state,
            )
            report.query_results.append(result)
            if detail.status_text or detail.international_tracking_no or detail.page_error:
                report.parsed_count += 1
            if logistics_state == LOGISTICS_READY:
                ready_this_run.append(_ready_item_from_row_and_detail(row, detail))
            elif logistics_state == LOGISTICS_WAITING:
                report.waiting_count += 1
            elif logistics_state == LOGISTICS_BLOCKED:
                report.blocked_count += 1
            elif logistics_state == LOGISTICS_RETRYABLE:
                report.retryable_count += 1
        except Exception as exc:
            message = _logistics_query_error_message(exc)
            report.retryable_count += 1
            report.query_results.append(
                LogisticsQueryResult(
                    system_order_no=str(row.get("system_order_no") or ""),
                    platform_order_no=str(row.get("platform_order_no") or ""),
                    logistics_no=logistics_no,
                    last_error=message,
                    logistics_state=LOGISTICS_RETRYABLE,
                )
            )
            if update_queue:
                store.complete_logistics_attempt(
                    logistics_no,
                    LogisticsDetail(logistics_no=logistics_no, page_error=message),
                    state=LOGISTICS_RETRYABLE,
                    last_error=message,
                    owner=worker_id,
                    expected_version=int(row.get("version") or 0) if worker_id else None,
                    run_id=run_id,
                )

    if update_queue:
        report.ready_to_mark_items = store.list_ready_to_mark()
    else:
        report.ready_to_mark_items = _dedupe_ready_items(store.list_ready_to_mark() + ready_this_run)
    report.ready_count = len(report.ready_to_mark_items)
    report.skipped_query_records = [
        record for record in store.list_logistics_skipped_records(limit=50) if record.logistics_no not in queried_logistics_numbers
    ]
    return report


async def _fetch_detail_with_optional_retry(
    logistics_no: str,
    *,
    fetch_detail: FetchLogisticsDetail,
    retry_fetch_detail: FetchLogisticsDetail | None,
    report: LogisticsWorkerReport,
) -> LogisticsDetail:
    try:
        return _raise_browser_closed_page_error(await fetch_detail(logistics_no))
    except LogisticsBrowserClosedError:
        if retry_fetch_detail is None:
            raise
        detail = _raise_browser_closed_page_error(await retry_fetch_detail(logistics_no))
        if BROWSER_CLOSED_RETRY_MESSAGE not in report.warnings:
            report.warnings.append(BROWSER_CLOSED_RETRY_MESSAGE)
        return detail


def _raise_browser_closed_page_error(detail: LogisticsDetail) -> LogisticsDetail:
    if detail.page_error and is_browser_closed_error(detail.page_error):
        raise LogisticsBrowserClosedError(compact_exception_message(detail.page_error))
    return detail


def _logistics_query_error_message(error: object) -> str:
    if isinstance(error, LogisticsBrowserClosedError) or is_browser_closed_error(error):
        text = compact_exception_message(error)
        return text if text != type(error).__name__ else BROWSER_CLOSED_ERROR_MESSAGE
    return f"阿里物流详情查询失败：{compact_exception_message(error)}"


async def fetch_logistics_detail_from_page(
    context,
    logistics_no: str,
    *,
    login_config: AlibabaLoginConfig | None = None,
    auto_login: bool = True,
    login_timeout_sec: int = 300,
) -> LogisticsDetail:
    url = logistics_detail_url(logistics_no)
    page = None
    json_payloads: list[Any] = []
    response_tasks: list[asyncio.Task] = []

    async def capture_response(response) -> None:
        try:
            resource_type = getattr(response.request, "resource_type", "")
            content_type = response.headers.get("content-type", "")
            if resource_type not in {"xhr", "fetch"} and "json" not in content_type:
                return
            text = await response.text()
            payload = parse_json_payload(text)
            if payload is not None:
                json_payloads.append(payload)
        except Exception:
            return

    try:
        page = await context.new_page()
        page.on("response", lambda response: response_tasks.append(asyncio.create_task(capture_response(response))))
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await wait_for_alibaba_logistics_detail(
            page,
            url,
            login_config=login_config,
            auto_login=auto_login,
            timeout_sec=login_timeout_sec,
        )
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

        json_detail = parse_logistics_detail_from_json_payloads(
            json_payloads,
            source_url=url,
            fallback_logistics_no=logistics_no,
        )
        body_text = await page.locator("body").inner_text(timeout=8000)
        text_detail = parse_logistics_detail_from_text(body_text, source_url=url, fallback_logistics_no=logistics_no)
        if json_detail and not json_detail.page_error and (
            json_detail.status_text or json_detail.international_tracking_no
        ):
            return json_detail
        return text_detail
    except Exception as exc:
        if is_browser_closed_error(exc):
            raise LogisticsBrowserClosedError(compact_exception_message(exc)) from exc
        return LogisticsDetail(logistics_no=logistics_no, source_url=url, page_error=f"阿里物流详情读取失败：{compact_exception_message(exc)}")
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def _close_browser_state(browser_state: dict[str, Any]) -> None:
    context = browser_state.get("context")
    playwright = browser_state.get("playwright")
    browser_state["context"] = None
    browser_state["playwright"] = None
    if context is not None:
        try:
            await context.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            pass


def logistics_report_to_dict(report: LogisticsWorkerReport) -> dict[str, Any]:
    return asdict(report)


def _dedupe_rows_by_logistics_no(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        logistics_no = str(row.get("logistics_no") or "").strip()
        if not logistics_no or logistics_no in seen:
            continue
        seen.add(logistics_no)
        unique.append(row)
    return unique


def _dedupe_ready_items(items: list[ReadyToMarkItem]) -> list[ReadyToMarkItem]:
    seen: set[str] = set()
    unique: list[ReadyToMarkItem] = []
    for item in items:
        if item.logistics_no in seen:
            continue
        seen.add(item.logistics_no)
        unique.append(item)
    return unique


def _ready_item_from_row_and_detail(row: dict[str, Any], detail: LogisticsDetail) -> ReadyToMarkItem:
    return ReadyToMarkItem(
        system_order_no=str(row.get("system_order_no") or ""),
        platform_order_no=str(row.get("platform_order_no") or ""),
        logistics_no=detail.logistics_no or str(row.get("logistics_no") or ""),
        carrier=detail.carrier,
        international_tracking_no=detail.international_tracking_no,
        actual_total=detail.actual_total,
        chargeable_weight_kg=detail.chargeable_weight_kg,
    )
