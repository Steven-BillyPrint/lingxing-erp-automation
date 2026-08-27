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
        "logistics": "Standard Shipping",
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


def test_shipment_tag_requires_an_exact_label_match():
    assert row_has_shipment_tag("非自动标发", "自动标发") is False
    assert row_has_shipment_tag("自动标发已取消", "自动标发") is False
    assert row_has_shipment_tag("拆分订单 | 自动标发 | 客户已确认", "自动标发") is True
    assert row_has_shipment_tag("Ready To Ship | Reviewed", "Ready To Ship") is True


def test_report_builds_candidate_from_tagged_row():
    report = build_shipment_scan_report(
        [
            _row(
                sales_platform_code="10001",
                sales_platform_name="Amazon",
                has_main_image=True,
            )
        ],
        "自动标发",
        queue_path="queue.sqlite3",
    )

    assert report.scanned_row_count == 1
    assert report.tagged_row_count == 1
    assert report.valid_logistics_row_count == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].logistics_no == "ALS01781406025"
    assert report.candidates[0].system_order_no == "103710434633847501"
    assert report.candidates[0].customer_shipping_service == "standard"
    assert report.candidates[0].sales_platform_code == "10001"
    assert report.candidates[0].sales_platform_name == "Amazon"
    assert report.candidates[0].has_main_image is True


def test_shipment_identity_does_not_require_customization_rules() -> None:
    report = build_shipment_scan_report(
        [_row(asin_text="B0H36GPHVH")],
        "自动标发",
    )

    assert report.candidates[0].product_type == "pop_up_displays"


def test_shipment_identity_displays_tent_when_multiple_product_types_exist() -> None:
    report = build_shipment_scan_report(
        [_row(asin_text="B0CRRGTPFH B0FX9W3MJL")],
        "自动标发",
    )

    assert report.candidates[0].product_type == "tent"


def test_repeated_split_rows_promote_tent_over_an_earlier_product_type() -> None:
    report = build_shipment_scan_report(
        [
            _row(
                system_order_no="103720821042180608",
                asin_text="B0DBG9JWYS",
            ),
            _row(
                system_order_no="103720260088221441",
                asin_text="B0CRRGTPFH",
            ),
        ],
        "自动标发",
    )

    assert len(report.candidates) == 1
    assert report.candidates[0].product_type == "tent"


def test_report_accepts_independent_site_wc_platform_order():
    report = build_shipment_scan_report([_row(platform_order_no="wc39877")], "自动标发")

    assert report.valid_logistics_row_count == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].platform_order_no == "wc39877"
    assert report.manual_reviews == []


def test_report_only_uses_first_valid_logistics_and_records_review():
    report = build_shipment_scan_report(
        [_row(customer_remark="ALS01781406025，另一个 ALS01789020252")],
        "自动标发",
    )

    assert len(report.candidates) == 1
    assert report.candidates[0].logistics_no == "ALS01781406025"
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].logistics_numbers == ["ALS01781406025", "ALS01789020252"]
    assert report.manual_reviews[0].selected_logistics_no == "ALS01781406025"


def test_report_uses_first_current_als_and_merges_repeated_split_rows():
    remark = (
        "新单ALS01825902784；ALS01824309596作废。"
        "ALS01823850227高申报作废。"
    )
    report = build_shipment_scan_report(
        [
            _row(system_order_no="103720821042180608", customer_remark=remark),
            _row(system_order_no="103720260088221441", customer_remark=remark),
        ],
        "自动标发",
    )

    assert [item.logistics_no for item in report.candidates] == ["ALS01825902784"]
    assert report.candidates[0].system_order_no == "103720821042180608"
    assert report.manual_reviews[0].selected_logistics_no == "ALS01825902784"
    assert any("ALS01824309596" in warning for warning in report.candidates[0].warnings)


def test_tagged_row_without_valid_logistics_goes_to_manual_review():
    report = build_shipment_scan_report([_row(customer_remark="取消 ALS01781406025")], "自动标发")

    assert report.candidates == []
    assert report.tagged_row_count == 1
    assert report.valid_logistics_row_count == 0
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].reason == "missing_valid_logistics"


def test_tagged_row_with_logistics_but_missing_platform_is_not_enqueued():
    report = build_shipment_scan_report([_row(platform_order_no="")], "自动标发")

    assert report.valid_logistics_row_count == 1
    assert report.candidates == []
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].reason == "missing_platform_order_no"


def test_tagged_row_without_customer_shipping_service_defaults_to_standard():
    report = build_shipment_scan_report([_row(logistics="")], "自动标发")

    assert report.valid_logistics_row_count == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].customer_shipping_service == "standard"
    assert report.manual_review_count == 0


def test_tagged_row_with_unknown_customer_shipping_service_is_not_enqueued():
    report = build_shipment_scan_report(
        [_row(customer_shipping_service="UPS-全程")],
        "自动标发",
    )

    assert report.valid_logistics_row_count == 1
    assert report.candidates == []
    assert report.manual_review_count == 1
    assert report.manual_reviews[0].reason == "unknown_customer_shipping_service"


def test_duplicate_queue_result_keeps_existing_stage_states_and_error():
    report = build_shipment_scan_report([_row()], "自动标发")
    result = QueueInsertResult(
        inserted=False,
        candidate=report.candidates[0],
        existing={
            "system_order_no": "103710434633847501",
            "platform_order_no": "112-1165824-9982644",
            "identity_state": "ACTIVE",
            "logistics_state": "READY",
            "erp_state": "RETRYABLE",
            "last_error": "上一轮 ERP 标发失败",
        },
    )

    apply_queue_results(report, [result])

    assert report.duplicate_skipped[0].existing_logistics_state == "READY"
    assert report.duplicate_skipped[0].existing_erp_state == "RETRYABLE"
    assert report.duplicate_skipped[0].existing_last_error == "上一轮 ERP 标发失败"
    assert report.refreshed_count == 1
    assert report.conflict_count == 0


def test_duplicate_queue_result_counts_immediate_reprocessing():
    report = build_shipment_scan_report([_row()], "自动标发")
    result = QueueInsertResult(
        inserted=False,
        candidate=report.candidates[0],
        existing={"system_order_no": "103710434633847501", "platform_order_no": "112-1165824-9982644"},
        immediate_logistics=True,
        immediate_erp=True,
    )

    apply_queue_results(report, [result])

    assert report.refreshed_count == 1
    assert report.immediate_logistics_count == 1
    assert report.immediate_erp_count == 1
    assert report.duplicate_skipped[0].immediate_logistics is True
    assert report.duplicate_skipped[0].immediate_erp is True
