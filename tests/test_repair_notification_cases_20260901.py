from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "repair_notification_cases_20260901.py"
)
SPEC = importlib.util.spec_from_file_location("notification_case_repair", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


def test_repair_manifest_contains_exactly_the_three_approved_orders() -> None:
    assert repair.APPROVED_ORDERS == (
        "114-9608129-2148247",
        "111-1829451-7385063",
        "112-7217878-8825061",
    )
    assert repair.DHL_RAW_CARRIER == "3PL-DHL"
    assert repair.DHL_TRACKING_NO == "7723922905"


def test_dhl_repair_detail_is_ready_only_for_the_approved_pair() -> None:
    row = {
        "logistics_no": "ALS01930800000",
        "alibaba_status": "运输中",
        "service_type": "快递门到门",
        "service_line": "DHL Express",
        "carrier_raw": "3PL-DHL",
        "international_tracking_no": "7723922905",
        "actual_total": "CNY 123.45",
        "chargeable_weight_kg": "4.500",
        "package_count": 1,
        "source_url": "https://example.invalid/logistics",
    }

    detail = repair._dhl_detail(row)

    assert detail.carrier == "3PL-DHL"
    assert detail.international_tracking_no == "7723922905"

    row["international_tracking_no"] = "INVALID"
    with pytest.raises(RuntimeError, match="not ready"):
        repair._dhl_detail(row)


def test_corrected_draft_requires_both_package_c_and_d() -> None:
    notification = {
        "platform_order_no": repair.DRAFT_REPAIR_ORDER,
        "state": "AWAITING_REVIEW",
        "channel": "EMAIL",
        "template_version": "shipment-email-v10",
        "package_complete": 2,
        "package_total": 4,
        "package_missing": 2,
        "provider_message_id": "",
        "sent_at": "",
        "body": (
            "Shipment progress: 2 of 4 packages have shipped.\n"
            "Package c: Available soon.\nPackage d: Available soon."
        ),
        "body_html": (
            "Shipment progress: 2 of 4 packages have shipped.<br>"
            "Package c: Available soon.<br>Package d: Available soon."
        ),
    }

    repair._validate_corrected_draft(notification)

    notification["body"] = notification["body"].replace(
        "Package d: Available soon.",
        "",
    )
    with pytest.raises(RuntimeError, match="two pending packages"):
        repair._validate_corrected_draft(notification)


def test_already_reconciled_state_still_requires_provider_evidence() -> None:
    notification = {
        "state": "DELIVERED",
        "provider_status": "success",
        "provider_message_id": "sent-copy-123",
        "sent_at": "2026-09-01T09:00:00+00:00",
    }

    receipt = asyncio.run(repair._verify_provider_success(notification, object()))

    assert receipt == {
        "send_status": "success",
        "message_id": "sent-copy-123",
        "already_reconciled": "1",
    }

    notification["provider_message_id"] = ""
    with pytest.raises(RuntimeError, match="immutable provider evidence"):
        asyncio.run(repair._verify_provider_success(notification, object()))
