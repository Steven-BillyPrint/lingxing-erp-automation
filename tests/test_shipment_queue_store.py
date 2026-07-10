from shipment_automation.models import (
    LogisticsDetail,
    QUEUE_STATUS_EMAIL_SENT,
    QUEUE_STATUS_ERP_MARKED,
    QUEUE_STATUS_ERROR,
    QUEUE_STATUS_MANUAL_REVIEW,
    QUEUE_STATUS_NOT_READY,
    QUEUE_STATUS_READY_TO_MARK,
    ShipmentCandidate,
)
from shipment_automation.logistics_worker import BROWSER_CLOSED_ERROR_KEYWORDS, BROWSER_CLOSED_ERROR_MESSAGE
from shipment_automation.queue_store import ShipmentQueueStore


def _candidate(als_no: str = "ALS01781406025", system_order_no: str = "103710434633847501"):
    return ShipmentCandidate(
        system_order_no=system_order_no,
        platform_order_no="112-1165824-9982644",
        als_no=als_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {als_no}",
        status_text="待审核发货",
    )


def test_queue_inserts_candidate(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")

    result = store.insert_candidate(_candidate())

    assert result.inserted is True
    assert store.get_by_als("ALS01781406025")["system_order_no"] == "103710434633847501"


def test_queue_dedupes_by_als_only(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate(system_order_no="103710434633847501")
    duplicate = _candidate(system_order_no="103710639045926988")

    assert store.insert_candidate(first).inserted is True
    result = store.insert_candidate(duplicate)

    assert result.inserted is False
    assert result.existing["system_order_no"] == "103710434633847501"


def test_queue_allows_different_als_for_same_system_order(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")

    assert store.insert_candidate(_candidate("ALS01781406025")).inserted is True
    assert store.insert_candidate(_candidate("ALS01789020252")).inserted is True

    assert store.get_by_als("ALS01781406025") is not None
    assert store.get_by_als("ALS01789020252") is not None


def test_queue_lists_new_and_not_ready_for_logistics_check(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    new_candidate = _candidate("ALS01781406025")
    not_ready_candidate = _candidate("ALS01789020252")
    not_ready_candidate.queue_status = QUEUE_STATUS_NOT_READY
    error_candidate = _candidate("ALS01789020254")
    error_candidate.queue_status = QUEUE_STATUS_ERROR
    ready_candidate = _candidate("ALS01789020253")
    ready_candidate.queue_status = QUEUE_STATUS_READY_TO_MARK

    store.insert_candidate(new_candidate)
    store.insert_candidate(not_ready_candidate)
    store.insert_candidate(error_candidate)
    store.insert_candidate(ready_candidate)

    rows = store.list_logistics_check_candidates()

    assert [row["als_no"] for row in rows] == ["ALS01781406025", "ALS01789020252", "ALS01789020254"]


def test_queue_updates_ready_to_mark_logistics_fields(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    updated = store.update_logistics_by_als(
        "ALS01781406025",
        LogisticsDetail(
            als_no="ALS01781406025",
            logistics_order_no="ALS01781406025",
            carrier="UPS",
            international_tracking_no="1Z999",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
            package_count=1,
        ),
        queue_status=QUEUE_STATUS_READY_TO_MARK,
        last_error=None,
    )

    row = store.get_by_als("ALS01781406025")
    assert updated is True
    assert row["queue_status"] == QUEUE_STATUS_READY_TO_MARK
    assert row["carrier"] == "UPS"
    assert row["international_tracking_no"] == "1Z999"
    assert row["actual_total"] == "CNY 123.45"
    assert row["chargeable_weight_kg"] == "4.500"


def test_queue_ready_to_mark_excludes_marked_and_emailed(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    ready = _candidate("ALS01781406025")
    ready.queue_status = QUEUE_STATUS_READY_TO_MARK
    marked = _candidate("ALS01789020252")
    marked.queue_status = QUEUE_STATUS_ERP_MARKED
    emailed = _candidate("ALS01789020253")
    emailed.queue_status = QUEUE_STATUS_EMAIL_SENT

    for candidate in [ready, marked, emailed]:
        store.insert_candidate(candidate)

    items = store.list_ready_to_mark()

    assert [item.als_no for item in items] == ["ALS01781406025"]


def test_queue_lists_erp_mark_retryable_error_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    ready = _candidate("ALS01781406025")
    ready.queue_status = QUEUE_STATUS_READY_TO_MARK
    retryable_error = _candidate("ALS01789020252")
    retryable_error.queue_status = QUEUE_STATUS_ERROR
    missing_logistics_error = _candidate("ALS01789020253")
    missing_logistics_error.queue_status = QUEUE_STATUS_ERROR

    for candidate in [ready, retryable_error, missing_logistics_error]:
        store.insert_candidate(candidate)

    store.update_logistics_by_als(
        "ALS01789020252",
        LogisticsDetail(
            als_no="ALS01789020252",
            logistics_order_no="ALS01789020252",
            carrier="UPS",
            international_tracking_no="1Z999",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
            package_count=1,
        ),
        queue_status=QUEUE_STATUS_ERROR,
        last_error="上一轮 ERP 标发失败",
    )

    items = store.list_erp_mark_candidates()

    assert [item.als_no for item in items] == ["ALS01781406025", "ALS01789020252"]


def test_queue_updates_erp_mark_status(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    ready = _candidate("ALS01781406025")
    ready.queue_status = QUEUE_STATUS_READY_TO_MARK
    store.insert_candidate(ready)

    updated = store.update_erp_mark_by_als("ALS01781406025", queue_status=QUEUE_STATUS_ERP_MARKED)

    row = store.get_by_als("ALS01781406025")
    assert updated is True
    assert row["queue_status"] == QUEUE_STATUS_ERP_MARKED
    assert row["last_error"] is None
    assert row["processed_at"]


def test_queue_lists_logistics_skipped_manual_review_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    manual_review_candidate = _candidate("ALS01781406025")
    manual_review_candidate.queue_status = QUEUE_STATUS_MANUAL_REVIEW
    manual_review_candidate.last_error = "需要人工复核"
    error_candidate = _candidate("ALS01789020253")
    error_candidate.queue_status = QUEUE_STATUS_ERROR
    not_ready_candidate = _candidate("ALS01789020252")
    not_ready_candidate.queue_status = QUEUE_STATUS_NOT_READY

    store.insert_candidate(manual_review_candidate)
    store.insert_candidate(error_candidate)
    store.insert_candidate(not_ready_candidate)

    records = store.list_logistics_skipped_records()

    assert [record.als_no for record in records] == ["ALS01781406025"]
    assert records[0].queue_status == QUEUE_STATUS_MANUAL_REVIEW
    assert records[0].last_error == "需要人工复核"


def test_queue_resets_browser_closed_manual_review_to_error(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    browser_closed = _candidate("ALS01781406025")
    browser_closed.queue_status = QUEUE_STATUS_MANUAL_REVIEW
    browser_closed.last_error = "阿里物流详情读取失败：Page.wait_for_timeout: Target page, context or browser has been closed"
    real_manual_review = _candidate("ALS01789020252")
    real_manual_review.queue_status = QUEUE_STATUS_MANUAL_REVIEW
    real_manual_review.last_error = "需要人工复核"

    store.insert_candidate(browser_closed)
    store.insert_candidate(real_manual_review)

    updated = store.reset_manual_review_errors_to_error(
        keywords=BROWSER_CLOSED_ERROR_KEYWORDS,
        last_error=BROWSER_CLOSED_ERROR_MESSAGE,
    )

    assert updated == 1
    assert store.get_by_als("ALS01781406025")["queue_status"] == QUEUE_STATUS_ERROR
    assert store.get_by_als("ALS01781406025")["last_error"] == BROWSER_CLOSED_ERROR_MESSAGE
    assert store.get_by_als("ALS01789020252")["queue_status"] == QUEUE_STATUS_MANUAL_REVIEW
