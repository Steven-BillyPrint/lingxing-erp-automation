from shipment_automation.email_sender import build_customer_email_preview, send_customer_email
from shipment_automation.models import EmailBatchPreview


def test_email_sender_only_builds_local_preview():
    batch = EmailBatchPreview(
        id=1,
        platform_order_no="112-1165824-9982644",
        sequence_no=1,
        state="PENDING",
        recipient_email="buyer@example.com",
        message_id="<stable@shipment-automation.local>",
        logistics_numbers=["ALS01781406025", "ALS01789020252"],
        tracking_numbers=["1Z9253126709651051", "874000000000"],
    )

    preview = build_customer_email_preview(batch)

    assert preview["send_enabled"] is False
    assert preview["delivery_mode"] == "preview_only"
    assert preview["delivery_status"] == "draft_ready"
    assert preview["send_attempted"] is False
    assert preview["message_id"] == "<stable@shipment-automation.local>"
    assert preview["body_lines"] == [
        "ALS01781406025: 1Z9253126709651051",
        "ALS01789020252: 874000000000",
    ]
    send_result = send_customer_email(batch)

    assert send_result["send_enabled"] is False
    assert send_result["send_attempted"] is False
    assert send_result["delivery_status"] == "not_sent_preview_only"
    assert "流程可继续" in send_result["message"]


def test_email_preview_without_recipient_is_a_non_crashing_draft():
    batch = EmailBatchPreview(
        id=2,
        platform_order_no="112-0000000-0000000",
        sequence_no=1,
        state="BLOCKED",
        recipient_email=None,
        message_id="<missing@shipment-automation.local>",
        logistics_numbers=["ALS00000000000"],
        tracking_numbers=[None],
    )

    preview = build_customer_email_preview(batch)
    send_result = send_customer_email(batch)

    assert preview["delivery_status"] == "draft_needs_recipient"
    assert "补充收件邮箱" in preview["message"]
    assert send_result["delivery_status"] == "not_sent_preview_only"
    assert send_result["send_attempted"] is False
