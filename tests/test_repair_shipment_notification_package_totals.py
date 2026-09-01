from __future__ import annotations

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

from shipment_automation.notification_domain import (
    EMAIL_TEMPLATE_VERSION,
    NOTIFICATION_AWAITING_REVIEW,
    stable_package_label,
)


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "repair_shipment_notification_package_totals.py"
)
SPEC = importlib.util.spec_from_file_location("package_total_repair_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


def _approved_preview():
    orders = {}
    for platform, expected in repair.EXPECTED_ORDERS.items():
        orders[platform] = {
            "outbound_state": "OUTBOUNDED",
            "known_customer_package_total": expected.package_total,
            "package_complete": expected.package_complete,
            "package_missing": expected.package_missing,
            "unknown_status_count": 0,
            "conflicting_status_count": 0,
            "packages": [
                {"tracking_number": tracking}
                for tracking in expected.tracking_numbers
            ],
        }
    for platform in repair.EXCLUDED_ORDERS:
        orders[platform] = {
            "outbound_state": "WAITING",
            "known_customer_package_total": 1,
            "package_complete": 0,
            "package_missing": 1,
            "unknown_status_count": 0,
            "conflicting_status_count": 0,
            "packages": [],
        }
    return {"orders": orders}


def test_repair_manifest_contains_only_42_partial_amazon_orders() -> None:
    repair._validate_manifest()

    assert len(repair.EXPECTED_ORDERS) == 42
    assert set(repair.SKIPPED_INDEPENDENT_ORDERS) == {
        "wc40268",
        "wc40309",
        "wc40398",
        "wc40591",
    }
    assert not (set(repair.EXPECTED_ORDERS) & set(repair.SKIPPED_INDEPENDENT_ORDERS))
    assert Counter(
        expected.package_total for expected in repair.EXPECTED_ORDERS.values()
    ) == Counter({2: 9, 3: 19, 4: 14})
    assert all(
        expected.package_missing > 0
        for expected in repair.EXPECTED_ORDERS.values()
    )


def test_preview_gate_rejects_an_order_that_is_now_fully_tracked() -> None:
    preview = _approved_preview()
    repair._validate_api_preview(preview)

    platform = "111-9677801-8945001"
    expected = repair.EXPECTED_ORDERS[platform]
    preview["orders"][platform].update(
        {
            "package_complete": expected.package_total,
            "package_missing": 0,
            "packages": [
                *preview["orders"][platform]["packages"],
                {"tracking_number": "NEWLY-COMPLETED-3"},
                {"tracking_number": "NEWLY-COMPLETED-4"},
            ],
        }
    )

    with pytest.raises(RuntimeError, match="differs from approved manifest"):
        repair._validate_api_preview(preview)


def test_generated_draft_gate_requires_one_placeholder_per_missing_package() -> None:
    platform = "111-9677801-8945001"
    expected = repair.EXPECTED_ORDERS[platform]
    progress = "Shipment progress: 2 of 4 packages have shipped."
    placeholders = "\n".join(
        f"Package {stable_package_label(index)}: Available soon."
        for index in (3, 4)
    )
    notification = {
        "id": 10,
        "revision": 2,
        "platform_order_no": platform,
        "channel": "EMAIL",
        "state": NOTIFICATION_AWAITING_REVIEW,
        "template_version": EMAIL_TEMPLATE_VERSION,
        "package_total": 4,
        "package_complete": 2,
        "package_missing": 2,
        "provider_message_id": "",
        "sent_at": "",
        "body": f"{progress}\n{placeholders}",
        "body_html": f"{progress}<br>{placeholders.replace(chr(10), '<br>')}",
        "items": [
            {
                "customer_visible": 1,
                "is_complete": 1,
                "final_tracking_no": tracking,
            }
            for tracking in expected.tracking_numbers
        ],
    }

    assert repair._notification_matches_expected(notification, expected) == []

    notification["package_complete"] = 4
    notification["package_missing"] = 0
    notification["body"] = "Shipment progress: 4 of 4 packages have shipped."
    problems = repair._notification_matches_expected(notification, expected)
    assert {"package_complete", "package_missing", "progress_text", "available_soon"}.issubset(
        problems
    )
