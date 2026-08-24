from __future__ import annotations

from datetime import datetime

import pytest

from lingxing_automation.cli import build_parser
from lingxing_automation.constants import DEFAULT_PAYMENT_WINDOW_HOURS
from lingxing_automation.parsers.dates import classify_recent_payment_window
from lingxing_automation.flows.contact_sync import compact_batch_result_log
from erp_automation.application.email_policy import email_preview_enabled
from erp_automation.configuration.settings import with_configuration_defaults
from erp_automation.ui.models import Capability, CapabilityMode, CapabilityPolicy, DesktopSettings
from shipment_automation.config import SHIPMENT_TAG_NAME


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


@pytest.mark.parametrize("weight_kg", [3, 4, 5])
def test_high_value_split_weight_threshold_accepts_only_settings_options(
    weight_kg: int,
) -> None:
    normalized = with_configuration_defaults(
        {"automation.high_value_split_weight_kg": weight_kg}
    )

    assert normalized["automation.high_value_split_weight_kg"] == weight_kg
    assert not DesktopSettings(high_value_split_weight_kg=weight_kg).validate()


def test_invalid_high_value_split_weight_threshold_falls_back_to_3kg() -> None:
    normalized = with_configuration_defaults(
        {"automation.high_value_split_weight_kg": 6}
    )

    assert normalized["automation.high_value_split_weight_kg"] == 3
    assert "高金额订单拆单估重阈值必须选择 3、4 或 5kg。" in DesktopSettings(
        high_value_split_weight_kg=6
    ).validate()


def test_shipment_scan_tag_defaults_to_mark_ship_and_is_user_configurable() -> None:
    normalized = with_configuration_defaults({})
    customized = with_configuration_defaults(
        {"automation.shipment_tag_name": "待客户标发"}
    )

    assert SHIPMENT_TAG_NAME == "标发"
    assert normalized["automation.shipment_tag_name"] == "标发"
    assert customized["automation.shipment_tag_name"] == "待客户标发"
    assert DesktopSettings().shipment_tag_name == "标发"
    assert "自动标发扫描标签不能为空。" in DesktopSettings(
        shipment_tag_name="  "
    ).validate()


def test_custom_batch_logs_keep_contact_and_address_diagnostics() -> None:
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
                "recipient_name": "Alice Buyer",
                "recipient_name_source": "amazon_orders_api",
                "lingxing_recipient_name_raw": "-",
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

    assert "buyer@example.com" in serialized
    assert "15551234567" in serialized
    assert "123 Main Street" in serialized
    assert "Alice Buyer" in serialized
    assert "amazon_orders_api" in serialized
    assert "<redacted-email>" not in serialized
    assert "<redacted-address>" not in serialized


def test_desktop_business_log_redaction_is_fixed_off() -> None:
    normalized = with_configuration_defaults({"logs.redact_sensitive": True})

    assert normalized["logs.redact_sensitive"] is False
    assert "业务日志固定保留原始诊断内容，不能开启脱敏。" in DesktopSettings(
        redact_sensitive_logs=True
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
    assert normalized["lingxing.erp_mark.routes"]["ONTRAC"] == {
        "warehouse_id": 7979,
        "logistics_type_id": 64302,
        "channel_name": "手动 > OnTrac",
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
