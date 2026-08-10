from __future__ import annotations

from datetime import datetime

from lingxing_automation.cli import build_parser
from lingxing_automation.constants import DEFAULT_PAYMENT_WINDOW_HOURS
from lingxing_automation.parsers.dates import classify_recent_payment_window
from lingxing_automation.flows.contact_sync import compact_batch_result_log
from erp_automation.application.email_policy import email_preview_enabled
from erp_automation.configuration.settings import with_configuration_defaults
from erp_automation.ui.models import Capability, CapabilityMode, CapabilityPolicy, DesktopSettings


def test_payment_window_defaults_to_96_hours_in_parser_and_classifier():
    args = build_parser().parse_args([])
    now = datetime(2026, 7, 14, 12, 0, 0)

    assert DEFAULT_PAYMENT_WINDOW_HOURS == 96.0
    assert args.batch_payment_hours == 96.0
    assert classify_recent_payment_window("付款时间 2026-07-10 13:00:00", now=now) == "recent"
    assert classify_recent_payment_window("付款时间 2026-07-10 11:59:59", now=now) == "old"


def test_explicit_payment_window_parameter_remains_compatible():
    args = build_parser().parse_args(["--batch-payment-hours", "24"])
    now = datetime(2026, 7, 14, 12, 0, 0)

    assert args.batch_payment_hours == 24.0
    assert classify_recent_payment_window(
        "付款时间 2026-07-12 12:00:00",
        now=now,
        hours=args.batch_payment_hours,
    ) == "old"


def test_desktop_payment_window_is_fixed_to_96_hours() -> None:
    normalized = with_configuration_defaults({"automation.payment_window_hours": 24})

    assert normalized["automation.payment_window_hours"] == 96
    assert not DesktopSettings(payment_window_hours=96).validate()
    assert "付款时间窗口固定为 96 小时。" in DesktopSettings(
        payment_window_hours=24
    ).validate()


def test_custom_batch_logs_redact_contact_and_address_pii() -> None:
    payload = {
        "status": "completed",
        "message": "邮箱 buyer@example.com 电话: +1-555-123-4567；备用 15551234567",
        "items": [
            {
                "platform_order_no": "111-1111111-1111111",
                "system_order_no": "103700000000000001",
                "status": "needs_manual_save",
                "phone": "+1-555-123-4567",
                "email": "buyer@example.com",
                "source_excerpt": "Buyer: Alice, buyer@example.com",
                "shipping_address_text": "Alice, 123 Main Street, Seattle WA",
                "update_messages": ["email=buyer@example.com phone=15551234567"],
                "extracted_contacts": [
                    {
                        "system_order_no": "103700000000000001",
                        "phone": "15551234567",
                        "email": "buyer@example.com",
                    }
                ],
            }
        ],
    }

    serialized = str(compact_batch_result_log(payload))

    assert "buyer@example.com" not in serialized
    assert "15551234567" not in serialized
    assert "123 Main Street" not in serialized
    assert "<redacted-email>" in serialized
    assert "<redacted-address>" in serialized


def test_desktop_log_redaction_policy_cannot_be_disabled() -> None:
    normalized = with_configuration_defaults({"logs.redact_sensitive": False})

    assert normalized["logs.redact_sensitive"] is True
    assert "日志敏感信息脱敏为固定安全策略，不能关闭。" in DesktopSettings(
        redact_sensitive_logs=False
    ).validate()


def test_desktop_lingxing_endpoint_is_pinned_to_official_https_host() -> None:
    normalized = with_configuration_defaults(
        {"lingxing.api_base_url": "http://untrusted.invalid"}
    )

    assert normalized["lingxing.api_base_url"] == "https://openapi.lingxing.com"
    assert "领星 API 地址固定为官方 HTTPS 域名。" in DesktopSettings(
        lingxing_api_base_url="http://untrusted.invalid"
    ).validate()


def test_verified_routes_are_added_without_overwriting_existing_erp_routes() -> None:
    normalized = with_configuration_defaults(
        {
            "lingxing.erp_mark.routes": {
                "USPS": {
                    "warehouse_id": 7979,
                    "logistics_type_id": 40173,
                }
            }
        }
    )

    assert normalized["lingxing.erp_mark.routes"]["USPS"]["logistics_type_id"] == 40173
    assert normalized["lingxing.erp_mark.routes"]["WANB"] == {
        "warehouse_id": 7979,
        "logistics_type_id": 63287,
        "channel_name": "手动 > 万邦速达",
    }
    assert normalized["lingxing.erp_mark.routes"]["CANADAPOST"] == {
        "warehouse_id": 7979,
        "logistics_type_id": 42492,
        "channel_name": "手动 > 加拿大邮政",
    }
    assert normalized["lingxing.erp_mark.routes"]["ARAMEX"] == {
        "warehouse_id": 7979,
        "logistics_type_id": 63924,
        "channel_name": "手动 > ARAMEX",
    }


def test_email_feature_is_fixed_disabled_until_mail_delivery_is_integrated() -> None:
    normalized = with_configuration_defaults(
        {
            "capabilities.email_preview": "api_first",
            "email.mode": "preview_only",
        }
    )
    policy = CapabilityPolicy(
        modes={Capability.EMAIL_PREVIEW: CapabilityMode.API_FIRST},
        emergency_stop_writes=False,
    )

    assert normalized["capabilities.email_preview"] == "disabled"
    assert normalized["email.mode"] == "disabled"
    assert email_preview_enabled(normalized) is False
    assert email_preview_enabled(
        {"capabilities.email_preview": "api_first", "email.mode": "preview_only"}
    ) is False
    assert policy.configured_mode_for(Capability.EMAIL_PREVIEW) is CapabilityMode.DISABLED
    policy.set_mode(Capability.EMAIL_PREVIEW, CapabilityMode.API_FIRST)
    assert policy.effective_mode_for(Capability.EMAIL_PREVIEW) is CapabilityMode.DISABLED
