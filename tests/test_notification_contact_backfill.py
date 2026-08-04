from __future__ import annotations

import json

from erp_automation.application.notification_contact_backfill import (
    backfill_missing_notification_contacts,
    resolve_customization_json_contact,
)
from erp_automation.persistence import CustomWorkflowStore
from shipment_automation.notification_domain import (
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    CONTACT_SOURCE_WMS,
    EMAIL_PRESENCE_PROVIDED,
    OrderContact,
)
from shipment_automation.notification_store import ShipmentNotificationStore


PLATFORM = "111-4131086-3773056"


def _workflow(store: CustomWorkflowStore, platform: str = PLATFORM) -> None:
    store.mutate_legacy_record(
        platform,
        lambda current: {
            **current,
            "workflow_status": "completed",
            "last_seen_at": "2026-07-07T04:00:00Z",
            "contact_writeback_complete": True,
            "contact_completed_at": "2026-07-07T05:00:00Z",
            "folder_complete": True,
            "folder_completed_at": "2026-07-07T05:05:00Z",
        },
        event_type="test_workflow",
    )


def _payload(platform: str, *, email: str, phone: str) -> dict:
    return {
        "orderId": platform,
        "orderItemId": "item-1",
        "asin": "B000TEST01",
        "quantity": 1,
        "version3.0": {
            "customizationInfo": {
                "surfaces": [
                    {
                        "areas": [
                            {
                                "customizationType": "TextPrinting",
                                "label": (
                                    "Please provide an email address to confirm "
                                    "customization design and details or for emergencies."
                                ),
                                "text": email,
                            },
                            {
                                "customizationType": "TextPrinting",
                                "label": (
                                    "Please provide a texting number to confirm "
                                    "customization design and details or for emergencies."
                                ),
                                "text": phone,
                            },
                        ]
                    }
                ]
            }
        },
    }


def _order_folder(tmp_path, platform: str = PLATFORM):
    folder = tmp_path / "orders" / "2026" / "7月" / "0707" / f"{platform}+Customer"
    folder.mkdir(parents=True)
    return folder


def test_backfill_reads_unique_exact_order_json_and_preserves_wms_name(tmp_path) -> None:
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    _workflow(workflow_store)
    folder = _order_folder(tmp_path)
    (folder / "custom.json").write_text(
        json.dumps(
            _payload(PLATFORM, email="buyer@example.com", phone="4155552671")
        ),
        encoding="utf-8",
    )

    notification_store = ShipmentNotificationStore(tmp_path / "queue.sqlite3")
    notification_store.upsert_contact(
        OrderContact(
            platform_order_no=PLATFORM,
            recipient_name="WMS Customer",
            email="legacy@example.com",
            email_presence=EMAIL_PRESENCE_PROVIDED,
            source=CONTACT_SOURCE_WMS,
            recipient_name_source=CONTACT_SOURCE_WMS,
        )
    )
    target = {
        "platform_order_no": PLATFORM,
        "system_order_nos": ("103700000000000001",),
    }

    report = backfill_missing_notification_contacts(
        [target],
        notification_store=notification_store,
        workflow_store=workflow_store,
        folder_root=tmp_path / "orders",
    )

    assert report["contact_backfill_update_count"] == 1
    contact = notification_store.get_contact(PLATFORM)
    assert contact is not None
    assert contact.recipient_name == "WMS Customer"
    assert contact.recipient_name_source == CONTACT_SOURCE_WMS
    assert contact.email == "buyer@example.com"
    assert contact.phone_raw == "4155552671"
    assert contact.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
    assert contact.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
    assert contact.system_order_nos == ("103700000000000001",)

    # Repeated scans revalidate the actual matching JSON instead of trusting
    # a writeback/source flag, while the stored values remain idempotent.
    repeated = backfill_missing_notification_contacts(
        [target],
        notification_store=notification_store,
        workflow_store=workflow_store,
        folder_root=tmp_path / "orders",
    )
    assert repeated["contact_backfill_candidate_count"] == 1
    assert repeated["contact_backfill_update_count"] == 0


def test_backfill_rejects_mismatched_order_id(tmp_path) -> None:
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    _workflow(workflow_store)
    folder = _order_folder(tmp_path)
    (folder / "other.json").write_text(
        json.dumps(
            _payload(
                "111-0000000-0000000",
                email="wrong@example.com",
                phone="4155552671",
            )
        ),
        encoding="utf-8",
    )

    result = resolve_customization_json_contact(
        workflow_store,
        tmp_path / "orders",
        PLATFORM,
    )

    assert result.status == "order_mismatch"
    assert not result.authoritative


def test_backfill_without_workflow_uses_target_completion_month(tmp_path) -> None:
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    folder = _order_folder(tmp_path)
    (folder / "custom.json").write_text(
        json.dumps(
            _payload(
                PLATFORM,
                email="buyer@example.com",
                phone="4155552671",
            )
        ),
        encoding="utf-8",
    )
    notification_store = ShipmentNotificationStore(tmp_path / "queue.sqlite3")

    report = backfill_missing_notification_contacts(
        [
            {
                "platform_order_no": PLATFORM,
                "system_order_nos": ("103700000000000001",),
                "erp_completed_at": "2026-07-21T00:00:00Z",
            }
        ],
        notification_store=notification_store,
        workflow_store=workflow_store,
        folder_root=tmp_path / "orders",
    )

    assert report["contact_backfill_update_count"] == 1
    assert report["contact_backfill_resolved_count"] == 1
    contact = notification_store.get_contact(PLATFORM)
    assert contact is not None
    assert contact.email == "buyer@example.com"


def test_backfill_rejects_multiple_different_candidates(tmp_path) -> None:
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    _workflow(workflow_store)
    folder = _order_folder(tmp_path)
    for index, email in enumerate(("first@example.com", "second@example.com"), start=1):
        payload = _payload(PLATFORM, email=email, phone=f"415555267{index}")
        payload["orderItemId"] = f"item-{index}"
        (folder / f"custom-{index}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    result = resolve_customization_json_contact(
        workflow_store,
        tmp_path / "orders",
        PLATFORM,
    )

    assert result.status == "ambiguous"
    assert not result.authoritative
