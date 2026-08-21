from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from erp_automation.ui.models import DesktopSnapshot, ShipmentRow
from erp_automation.ui.qt import (
    _scheduled_scan_delay_ms,
    _format_status_timestamp,
    _notification_queue_sort_key,
    _notification_state_label,
    _notification_status_explanation,
    _notification_status_color,
    _notification_status_is_bold,
    _shipment_business_status,
    _shipment_checkpoint_label,
    _shipment_execution_eligibility,
    _shipment_status_explanation,
    _shipment_status_timestamp,
)


def test_scheduled_scan_delay_uses_server_due_time_and_leader_role() -> None:
    now = datetime.fromtimestamp(1000, timezone.utc)
    leader = DesktopSnapshot(
        is_scheduler_leader=True,
        scheduled_scan_due_at={"five_minute_timer": 1010.0},
    )
    follower = DesktopSnapshot(
        is_scheduler_leader=False,
        scheduled_scan_due_at={"five_minute_timer": 1010.0},
    )

    assert _scheduled_scan_delay_ms(
        leader,
        trigger="five_minute_timer",
        default_interval_ms=300_000,
        now=now,
    ) == 10_000
    assert _scheduled_scan_delay_ms(
        follower,
        trigger="five_minute_timer",
        default_interval_ms=300_000,
        now=now,
    ) is None
    paused = DesktopSnapshot(
        is_scheduler_leader=True,
        scheduled_scan_due_at={"five_minute_timer": 1010.0},
    )
    paused.policy.execution_paused = True
    assert _scheduled_scan_delay_ms(
        paused,
        trigger="five_minute_timer",
        default_interval_ms=300_000,
        now=now,
    ) is None
    assert _scheduled_scan_delay_ms(
        DesktopSnapshot(is_scheduler_leader=True),
        trigger="five_minute_timer",
        default_interval_ms=300_000,
        now=now,
    ) == 300_000
    assert _scheduled_scan_delay_ms(
        DesktopSnapshot(
            is_scheduler_leader=True,
            scheduled_scan_due_at={"five_minute_timer": 999.0},
        ),
        trigger="five_minute_timer",
        default_interval_ms=300_000,
        now=now,
    ) == 250


def _ready_row(**changes) -> ShipmentRow:
    values = {
        "platform_order_no": "111-0000000-0000001",
        "system_order_no": "SYS-001",
        "product_type": "tent",
        "logistics_no": "ALS-001",
        "international_tracking_no": "1Z999",
        "carrier": "UPS",
        "actual_total": "USD 25.00",
        "chargeable_weight_kg": "10.5",
        "identity_state": "ACTIVE",
        "logistics_state": "READY",
        "erp_state": "WAITING",
        "checkpoint": "NONE",
    }
    values.update(changes)
    return ShipmentRow(**values)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"logistics_state": "PENDING"}, "待查询物流"),
        ({"logistics_state": "WAITING"}, "等待物流就绪"),
        ({"logistics_state": "RETRYABLE"}, "查询失败待重试"),
        ({"logistics_state": "BLOCKED"}, "物流信息需复核"),
        ({"erp_state": "DONE", "checkpoint": "OUTBOUNDED"}, "已完成"),
        (
            {
                "erp_state": "DONE",
                "checkpoint": "OUTBOUNDED",
                "logistics_state": "RETRYABLE",
                "logistics_last_error": "浏览器关闭导致本轮查询失败。",
            },
            "已完成",
        ),
        ({"erp_state": "BLOCKED"}, "标发需人工复核"),
        ({"identity_state": "CANCELLED"}, "本轮已取消"),
        ({"identity_state": "MANUALLY_CANCELLED"}, "已取消"),
        ({"identity_state": "PAUSED_TAG_REMOVED"}, "标签已移除"),
        ({"identity_state": "CONFLICT"}, "订单信息冲突"),
        ({"checkpoint": "CHANNEL_SET", "erp_state": "RUNNING"}, "可继续标发"),
    ],
)
def test_shipment_business_status_is_chinese_and_business_facing(changes, expected):
    assert _shipment_business_status(_ready_row(**changes)) == expected


def test_ready_status_fails_closed_when_required_logistics_detail_is_missing():
    row = _ready_row(actual_total="")

    assert _shipment_business_status(row) == "物流信息需复核"
    assert _shipment_execution_eligibility(row) == (False, "物流信息需复核")


def test_customer_shipping_scan_issue_is_a_non_executable_queue_error():
    row = ShipmentRow(
        platform_order_no="112-0000000-0009901",
        system_order_no="103000000000009901",
        scan_issue_code="customer_shipping_service_unavailable",
        last_error="领星订单列表未返回客选物流字段。",
    )

    assert _shipment_business_status(row) == "扫描错误"
    assert _shipment_status_explanation(row, "扫描错误") == (
        "领星订单列表未返回客选物流字段。"
    )
    assert _shipment_execution_eligibility(row) == (
        False,
        "扫描错误记录不能执行标发",
    )


@pytest.mark.parametrize(
    ("scan_state", "expected"),
    [
        ("MANUAL_REVIEW", "标发需人工复核"),
        ("MANUALLY_COMPLETED", "已完成"),
        ("MANUALLY_CANCELLED", "已取消"),
    ],
)
def test_managed_scan_issue_changes_display_status_but_never_execution_eligibility(
    scan_state,
    expected,
):
    row = ShipmentRow(
        platform_order_no="39972",
        system_order_no="103000000000009972",
        scan_issue_key="scan-issue:72",
        scan_issue_code="customer_shipping_service_unavailable",
        scan_issue_state=scan_state,
        scan_issue_reason="人工处理原因",
        last_error="订单列表未返回可识别的客选物流。",
    )

    assert _shipment_business_status(row) == expected
    assert _shipment_execution_eligibility(row) == (
        False,
        "扫描错误记录不能执行标发",
    )
    explanation = _shipment_status_explanation(row, expected)
    assert "人工处理原因" in explanation
    assert "原扫描错误" in explanation


@pytest.mark.parametrize(
    ("service", "before", "due"),
    [
        ("expedited", "2026-08-13T09:29:59Z", "2026-08-13T09:30:00Z"),
        ("standard", "2026-08-15T09:29:59Z", "2026-08-15T09:30:00Z"),
    ],
)
def test_customer_shipping_deadline_uses_day_zero_and_china_1730_anchor(
    service,
    before,
    due,
):
    row = _ready_row(
        customer_shipping_service=service,
        first_seen_at="2026-08-11T16:30:00Z",
        logistics_state="WAITING",
        carrier="",
        international_tracking_no="",
    )

    assert _shipment_business_status(
        row,
        now=datetime.fromisoformat(before.replace("Z", "+00:00")),
    ) == "等待物流就绪"
    assert _shipment_business_status(
        row,
        now=datetime.fromisoformat(due.replace("Z", "+00:00")),
    ) == "物流逾期异常"


def test_validated_carrier_and_tracking_clear_non_blocking_due_notice():
    row = _ready_row(
        customer_shipping_service="expedited",
        first_seen_at="2026-08-01T00:00:00Z",
    )

    assert _shipment_business_status(
        row,
        now=datetime(2026, 8, 12, tzinfo=timezone.utc),
    ) == "可标发"


def test_retry_and_live_lease_are_not_submitted_early():
    now = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    expired = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")

    retry = _ready_row(erp_state="RETRYABLE", erp_next_attempt_at=future)
    assert _shipment_business_status(retry, now=now) == "标发失败可重试"
    assert _shipment_execution_eligibility(retry, now=now) == (
        False,
        "尚未到标发重试时间",
    )

    leased = _ready_row(
        erp_state="RUNNING",
        checkpoint="CHANNEL_SET",
        lease_owner="worker-1",
        lease_stage="erp",
        lease_until=future,
    )
    assert _shipment_business_status(leased, now=now) == "标发处理中"
    assert not _shipment_execution_eligibility(leased, now=now)[0]

    resumable = _ready_row(
        erp_state="RUNNING",
        checkpoint="CHANNEL_SET",
        lease_owner="worker-1",
        lease_stage="erp",
        lease_until=expired,
    )
    assert _shipment_business_status(resumable, now=now) == "可继续标发"
    assert _shipment_execution_eligibility(resumable, now=now)[0]


def test_active_desktop_task_prevents_duplicate_batch_submission():
    row = _ready_row()

    assert _shipment_execution_eligibility(
        row,
        active_logistics_nos={"ALS-001"},
    ) == (False, "已有等待或运行中的标发任务")
    assert _shipment_checkpoint_label("LOGISTICS_SAVED") == "已保存物流单号"


def test_completed_status_time_prefers_real_completion_evidence_and_uses_china_time():
    automated = _ready_row(
        erp_state="DONE",
        checkpoint="OUTBOUNDED",
        updated_at="2026-07-16T10:30:00Z",
        outbounded_at="2026-07-16T09:00:00Z",
        externally_completed_at="2026-07-16T08:00:00Z",
        completion_source="AUTOMATION",
    )
    manual = _ready_row(
        erp_state="DONE",
        checkpoint="OUTBOUNDED",
        updated_at="2026-07-16T10:30:00Z",
        outbounded_at="2026-07-16T09:00:00Z",
        externally_completed_at="2026-07-16T10:00:00Z",
        completion_source="MANUAL_DETECTED",
    )

    assert _shipment_status_timestamp(automated) == "2026-07-16T09:00:00Z"
    assert _shipment_status_timestamp(manual) == "2026-07-16T10:00:00Z"
    assert _format_status_timestamp(_shipment_status_timestamp(manual)) == "2026-07-16 18:00:00"

    legacy_without_completion_evidence = _ready_row(
        erp_state="DONE",
        checkpoint="OUTBOUNDED",
        updated_at="2026-07-16T12:00:00Z",
        erp_state_changed_at="2026-07-16T11:00:00Z",
    )
    assert (
        _shipment_status_timestamp(legacy_without_completion_evidence)
        == "2026-07-16T11:00:00Z"
    )


def test_non_completed_status_time_ignores_routine_scan_refresh():
    ready = _ready_row(
        updated_at="2026-08-10T02:00:00Z",
        last_scanned_at="2026-08-10T02:00:00Z",
        identity_state_changed_at="2026-08-09T00:00:00Z",
        logistics_state_changed_at="2026-08-10T01:00:00Z",
        logistics_last_checked_at="2026-08-10T01:00:00Z",
        erp_state_changed_at="2026-08-10T01:00:00Z",
    )
    waiting = _ready_row(
        logistics_state="WAITING",
        international_tracking_no="",
        updated_at="2026-08-10T04:00:00Z",
        last_scanned_at="2026-08-10T04:00:00Z",
        logistics_state_changed_at="2026-08-10T03:00:00Z",
        logistics_last_checked_at="2026-08-10T03:00:00Z",
        erp_state_changed_at="2026-08-09T01:00:00Z",
    )

    assert _shipment_status_timestamp(ready) == "2026-08-10T01:00:00Z"
    assert _shipment_status_timestamp(waiting) == "2026-08-10T03:00:00Z"


def test_all_stage_messages_share_one_chinese_status_explanation():
    row = _ready_row(
        erp_last_error="ERP 写入等待复核",
        logistics_last_error="物流单号尚未就绪",
        email_last_error="Missing receiver email.",
    )

    assert _shipment_status_explanation(row, "物流信息需复核") == (
        "ERP：ERP 写入等待复核；物流：物流单号尚未就绪；"
        "邮件：邮件预览未生成：缺少收件邮箱（不影响 ERP 标发）。"
    )


def test_unknown_historical_mail_error_is_never_exposed_in_english():
    row = _ready_row(email_last_error="SMTP timeout while rendering preview")
    explanation = _shipment_status_explanation(row, "物流信息需复核")

    assert explanation == "邮件：邮件预览处理异常，请打开详细日志检查（不影响 ERP 标发）。"
    assert "SMTP" not in explanation


@pytest.mark.parametrize(
    ("completion_source", "expected"),
    [
        ("AUTOMATION", "ERP 标发流程已完成。"),
        (
            "MANUAL_DETECTED",
            "已检测到领星订单在外部完成出库，自动标发任务已结案。",
        ),
    ],
)
def test_completed_explanation_suppresses_stale_stage_errors(
    completion_source,
    expected,
):
    row = _ready_row(
        erp_state="DONE",
        checkpoint="OUTBOUNDED",
        completion_source=completion_source,
        logistics_state="RETRYABLE",
        logistics_last_error="浏览器关闭导致本轮查询失败，下轮继续重试。",
        erp_last_error="历史 ERP 错误",
    )

    assert _shipment_status_explanation(row, "已完成") == expected


def test_manual_notification_completion_has_a_business_facing_label():
    assert _notification_state_label("MANUALLY_COMPLETED") == "人工完成"


def test_duplicate_suppression_has_a_business_facing_label():
    assert _notification_state_label("SUPPRESSED") == "已发送（自动去重）"


def test_cancelled_notification_has_a_business_facing_label():
    assert _notification_state_label("CANCELLED") == "已取消"


def test_provider_confirmed_notification_has_completed_label():
    assert _notification_state_label("DELIVERED") == "已完成"


def test_delivered_notification_with_missing_packages_has_partial_label_and_color():
    assert _notification_state_label("DELIVERED", 2) == "已发送，待补物流"
    assert _notification_status_color("DELIVERED", 2) == "#F58718"
    assert _notification_status_is_bold("DELIVERED", 2) is True
    assert _notification_state_label("DELIVERED", 0) == "已完成"
    assert _notification_status_color("DELIVERED", 0) == "#027A48"


def test_new_revision_after_a_delivery_is_labeled_as_supplemental_review():
    assert _notification_state_label("AWAITING_REVIEW", 2, True) == "待审核补发"
    assert _notification_state_label("AWAITING_REVIEW", 2, False) == "待审核"


def test_notification_queue_places_partial_between_unfinished_and_completed():
    notifications = [
        {
            "id": 1,
            "state": "DELIVERED",
            "package_missing": 0,
            "state_changed_at": "2026-07-20T12:00:00Z",
        },
        {
            "id": 2,
            "state": "DELIVERED",
            "package_missing": 2,
            "state_changed_at": "2026-07-20T11:00:00Z",
        },
        {
            "id": 3,
            "state": "AWAITING_REVIEW",
            "package_missing": 0,
            "state_changed_at": "2026-07-20T09:00:00Z",
        },
        {
            "id": 4,
            "state": "AWAITING_REVIEW",
            "package_missing": 1,
            "state_changed_at": "2026-07-20T10:00:00Z",
        },
        {
            "id": 5,
            "state": "DELIVERED",
            "package_missing": 1,
            "state_changed_at": "2026-07-20T12:00:00Z",
        },
        {
            "id": 6,
            "state": "MANUALLY_COMPLETED",
            "package_missing": 3,
            "state_changed_at": "2026-07-20T13:00:00Z",
        },
    ]

    ordered = sorted(notifications, key=_notification_queue_sort_key)

    assert [item["id"] for item in ordered] == [4, 3, 5, 2, 6, 1]
    assert _notification_state_label("MANUALLY_COMPLETED", 3) == "人工完成"


def test_waiting_contact_notification_has_a_business_facing_label():
    assert _notification_state_label("WAITING_CONTACT") == "待补联系方式"
    assert _notification_status_explanation(
        {
            "state": "WAITING_CONTACT",
            "last_error": "recipient_contact_unavailable",
        }
    ) == (
        "收件人没有可用的邮箱或电话；"
        "请补充至少一种有效联系方式，系统会在下次同步后重新生成待审核通知。"
    )


def test_sending_and_queued_notifications_sort_before_review_rows():
    notifications = [
        {
            "id": 1,
            "state": "AWAITING_REVIEW",
            "state_changed_at": "2026-08-13T12:00:00Z",
        },
        {
            "id": 2,
            "state": "SENDING",
            "state_changed_at": "2026-08-13T10:00:00Z",
        },
        {
            "id": 3,
            "state": "QUEUED",
            "state_changed_at": "2026-08-13T11:00:00Z",
        },
    ]

    ordered = sorted(notifications, key=_notification_queue_sort_key)

    assert [item["id"] for item in ordered] == [3, 2, 1]


def test_provider_accepted_notification_uses_unambiguous_send_service_label():
    assert _notification_state_label("ACCEPTED") == "发送服务已接收"
    assert _notification_status_explanation(
        {"state": "ACCEPTED", "provider_status": "ACCEPTED"}
    ) == "发送服务已接收，等待确认送达：ACCEPTED"


def test_unconfirmed_and_wc_suppressed_notifications_have_explicit_labels():
    assert _notification_state_label("DELIVERY_UNCONFIRMED") == "24小时未确认送达"
    assert _notification_status_explanation(
        {"state": "DELIVERY_UNCONFIRMED", "provider_status": "posting"}
    ) == "发送服务已接收，但 24 小时内未确认送达；这不代表发送失败，系统不会自动重发。"
    policy = "independent_site_customer_notification_disabled"
    assert _notification_state_label("SUPPRESSED", 0, False, policy) == (
        "独立站通知已禁用"
    )
    assert _notification_status_explanation(
        {"state": "SUPPRESSED", "last_error": policy}
    ) == "独立站订单已禁用客户通知；系统不会发送或重试。"


def test_product_block_has_a_specific_status_and_safe_explanation():
    error = "product_main_image_missing,product_title_missing"
    assert _notification_state_label("BLOCKED", 0, False, error) == "待补商品信息"
    assert _notification_status_explanation(
        {"state": "BLOCKED", "last_error": error}
    ) == "未找到带主图的商品；带主图商品缺少商品标题，暂不可审核发送。"

    sku_error = "product_sku_missing"
    assert _notification_state_label("BLOCKED", 0, False, sku_error) == "待补商品信息"
    assert _notification_status_explanation(
        {"state": "BLOCKED", "last_error": sku_error}
    ) == "未找到可用的商品 SKU，暂不可审核发送。"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            "outbound_ineligible:WAITING:waiting_for_all_customer_visible_packages_outbound",
            "尚无客户可见包裹被领星 WMS 明确确认为已出库，"
            "已保留扫描任务并等待下次同步。",
        ),
        (
            "outbound_ineligible:TERMINAL:terminal_wms_outbound_status",
            "订单或包裹已取消、截单或关闭，不可发送客户通知。",
        ),
        (
            "outbound_ineligible:UNKNOWN:previously_outbounded_package_unconfirmed",
            "之前已进入通知的包裹本次未能再次确认为已出库，"
            "原审核已失效，请重新同步并复核。",
        ),
    ],
)
def test_outbound_block_has_a_business_facing_explanation(error, expected):
    assert _notification_status_explanation(
        {"state": "BLOCKED", "last_error": error}
    ) == expected
