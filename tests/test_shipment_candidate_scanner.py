from shipment_automation.candidate_scanner import (
    apply_queue_results,
    build_shipment_scan_report,
    row_has_shipment_tag,
)
from shipment_automation.queue_store import QueueInsertResult


def _row(**overrides):
    row = {
        "system_order_no": "103710434633847501",
        "platform_order_no": "112-1165824-9982644",
        "tag_text": "拆分订单 已安排制作 自动标发",
        "customer_remark": "重发邮件 ALS01781406025",
        "sku": "10x10-Canopy 共1",
        "status_text": "待审核发货",
        "rowid": "103710434633847501",
    }
    row.update(overrides)
    return row


def test_empty_shipment_tag_does_not_match():
    assert row_has_shipment_tag("自动标发", "") is False


def test_tag_text_contains_shipment_tag_matches():
    assert row_has_shipment_tag("拆分订单 已安排制作 自动标发", "自动标发") is True
    assert row_has_shipment_tag("拆分订单 已安排制作 客户已确认", "自动标发") is False


def test_report_builds_candidate_from_tagged_row():
    report = build_shipment_scan_report([_row()], "自动标发", queue_path="queue.sqlite3")

    assert report.scanned_row_count == 1
    assert report.tagged_row_count == 1
    assert report.valid_als_row_count == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].als_no == "ALS01781406025"
    assert report.candidates[0].system_order_no == "103710434633847501"


def test_report_only_uses_first_valid_als_and_records_review():
    report = build_shipment_scan_report(
        [_row(customer_remark="ALS01781406025，另一个 ALS01789020252")],
        "自动标发",
    )

    assert len(report.candidates) == 1
    assert report.candidates[0].als_no == "ALS01781406025"
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].als_numbers == ["ALS01781406025", "ALS01789020252"]
    assert report.manual_reviews[0].selected_als_no == "ALS01781406025"


def test_tagged_row_without_valid_als_goes_to_manual_review():
    report = build_shipment_scan_report([_row(customer_remark="取消 ALS01781406025")], "自动标发")

    assert report.candidates == []
    assert report.tagged_row_count == 1
    assert report.valid_als_row_count == 0
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].reason == "missing_valid_als"


def test_duplicate_queue_result_keeps_existing_status_and_error():
    report = build_shipment_scan_report([_row()], "自动标发")
    result = QueueInsertResult(
        inserted=False,
        candidate=report.candidates[0],
        existing={
            "system_order_no": "103710434633847501",
            "platform_order_no": "112-1165824-9982644",
            "queue_status": "ERROR",
            "last_error": "上一轮 ERP 标发失败",
        },
    )

    apply_queue_results(report, [result])

    assert report.duplicate_skipped[0].existing_queue_status == "ERROR"
    assert report.duplicate_skipped[0].existing_last_error == "上一轮 ERP 标发失败"
