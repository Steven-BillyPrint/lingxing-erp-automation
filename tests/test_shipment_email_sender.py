import pytest

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
    assert preview["message_id"] == "<stable@shipment-automation.local>"
    assert preview["body_lines"] == [
        "ALS01781406025: 1Z9253126709651051",
        "ALS01789020252: 874000000000",
    ]
    with pytest.raises(RuntimeError, match="真实邮件发送尚未启用"):
        send_customer_email(batch)
