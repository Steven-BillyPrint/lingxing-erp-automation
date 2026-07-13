import asyncio
from types import SimpleNamespace

from shipment_automation import logistics_worker as worker_module
from shipment_automation.logistics_worker import (
    BROWSER_CLOSED_ERROR_MESSAGE,
    BROWSER_CLOSED_RETRY_MESSAGE,
    LogisticsBrowserClosedError,
    compact_exception_message,
    is_browser_closed_error,
    process_logistics_queue_once,
    run_logistics_worker,
)
from shipment_automation.models import (
    LOGISTICS_BLOCKED,
    LOGISTICS_PENDING,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentQueueStore


def _candidate(logistics_no: str = "ALS01781406025") -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no=logistics_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {logistics_no}",
        status_text="待审核发货",
    )


def _ready_detail(logistics_no: str = "ALS01781406025") -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=logistics_no,
        status_text="运输中",
        carrier="UPS",
        international_tracking_no="1Z999",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
        package_count=1,
    )


def _non_tail_carrier_detail(logistics_no: str = "ALS01781406025") -> LogisticsDetail:
    detail = _ready_detail(logistics_no)
    detail.carrier = "YHA"
    return detail


def test_logistics_worker_detects_browser_closed_errors():
    assert is_browser_closed_error("BrowserContext.new_page: Target page, context or browser has been closed")
    assert is_browser_closed_error("Page.wait_for_timeout: Target page, context or browser has been closed")
    assert compact_exception_message(
        "BrowserContext.new_page: Target page, context or browser has been closed\nBrowser logs:\n<launching> chrome"
    ) == BROWSER_CLOSED_ERROR_MESSAGE


def test_logistics_worker_dry_run_does_not_update_queue(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=False))

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_PENDING
    assert row["carrier"] is None
    assert report.ready_count == 1
    assert report.ready_to_mark_items[0].logistics_no == "ALS01781406025"


def test_logistics_worker_update_queue_writes_ready_fields(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["carrier"] == "UPS"
    assert row["international_tracking_no"] == "1Z999"
    assert report.ready_count == 1
    assert report.skipped_query_records == []


def test_logistics_worker_restarts_once_after_browser_closed(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(("first", logistics_no))
        raise LogisticsBrowserClosedError("BrowserContext.new_page: Target page, context or browser has been closed")

    async def fake_retry_fetch(logistics_no):
        calls.append(("retry", logistics_no))
        return _ready_detail(logistics_no)

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            retry_fetch_detail=fake_retry_fetch,
            update_queue=True,
            dry_run=False,
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert calls == [("first", "ALS01781406025"), ("retry", "ALS01781406025")]
    assert row["logistics_state"] == LOGISTICS_READY
    assert report.retryable_count == 0
    assert BROWSER_CLOSED_RETRY_MESSAGE in report.warnings


def test_logistics_worker_browser_closed_after_retry_stays_error(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        raise LogisticsBrowserClosedError("BrowserContext.new_page: Target page, context or browser has been closed")

    async def fake_retry_fetch(logistics_no):
        raise LogisticsBrowserClosedError(
            "Page.wait_for_timeout: Target page, context or browser has been closed\nBrowser logs:\n<launching> chrome"
        )

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            retry_fetch_detail=fake_retry_fetch,
            update_queue=True,
            dry_run=False,
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["last_error"] == BROWSER_CLOSED_ERROR_MESSAGE
    assert "Browser logs:" not in row["last_error"]
    assert report.retryable_count == 1
    assert report.blocked_count == 0


def test_logistics_worker_dedupes_same_logistics_number_in_output(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    row = store.get_by_logistics_no("ALS01781406025")
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            preloaded_rows=[row, row],
        )
    )

    assert calls == ["ALS01781406025"]
    assert report.scanned_page_count == 1
    assert [item.logistics_no for item in report.ready_to_mark_items] == ["ALS01781406025"]


def test_logistics_worker_non_real_carrier_not_ready_to_mark(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _non_tail_carrier_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=False))

    assert report.ready_to_mark_items == []
    assert report.waiting_count == 1
    assert report.query_results[0].logistics_state == LOGISTICS_WAITING
    assert "不是真实海外尾程承运商" in report.query_results[0].last_error


def test_logistics_worker_update_queue_records_non_real_carrier_reason(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _non_tail_carrier_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))
    row = store.get_by_logistics_no("ALS01781406025")

    assert report.ready_to_mark_items == []
    assert row["logistics_state"] == LOGISTICS_WAITING
    assert row["carrier"] == "YHA"
    assert "不是真实海外尾程承运商" in row["last_error"]


def test_logistics_worker_retries_error_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    error_candidate = _candidate("ALS01781406025")
    query_candidate = _candidate("ALS01789020252")
    store.insert_candidate(error_candidate)
    store.insert_candidate(query_candidate)
    store.complete_logistics_attempt(
        error_candidate.logistics_no,
        LogisticsDetail(logistics_no=error_candidate.logistics_no, page_error="上一轮查询失败"),
        state=LOGISTICS_RETRYABLE,
        last_error="上一轮查询失败",
    )
    store.retry_stage(error_candidate.logistics_no, "logistics")
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))

    assert calls == ["ALS01781406025", "ALS01789020252"]
    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_READY
    assert report.skipped_query_records == []


def test_logistics_worker_reports_skipped_manual_review_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    manual_candidate = _candidate("ALS01781406025")
    query_candidate = _candidate("ALS01789020252")
    store.insert_candidate(manual_candidate)
    store.insert_candidate(query_candidate)
    store.complete_logistics_attempt(
        manual_candidate.logistics_no,
        LogisticsDetail(logistics_no=manual_candidate.logistics_no, page_error="需要人工复核"),
        state=LOGISTICS_BLOCKED,
        last_error="需要人工复核",
    )

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch))

    assert [item.logistics_no for item in report.skipped_query_records] == ["ALS01781406025"]
    assert report.skipped_query_records[0].last_error == "需要人工复核"


def test_run_logistics_worker_queries_retryable_browser_error(monkeypatch, tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    manual_candidate = _candidate("ALS01781406025")
    store.insert_candidate(manual_candidate)
    store.complete_logistics_attempt(
        manual_candidate.logistics_no,
        LogisticsDetail(logistics_no=manual_candidate.logistics_no, page_error=BROWSER_CLOSED_ERROR_MESSAGE),
        state=LOGISTICS_RETRYABLE,
        last_error=BROWSER_CLOSED_ERROR_MESSAGE,
    )
    store.retry_stage(manual_candidate.logistics_no, "logistics")
    calls = []

    class FakeContext:
        async def close(self):
            calls.append("context.close")

    class FakePlaywright:
        async def stop(self):
            calls.append("playwright.stop")

    async def fake_launch_context(_args):
        calls.append("launch")
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(worker_module, "fetch_logistics_detail_from_page", fake_fetch_detail)

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                env_path=".env",
                limit=20,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert "ALS01781406025" in calls
    assert row["logistics_state"] == LOGISTICS_READY
    assert result["ready_count"] == 1
