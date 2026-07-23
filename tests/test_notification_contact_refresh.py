from __future__ import annotations

import asyncio
import json
import sqlite3

from erp_automation.application.notification_contact_refresh import (
    refresh_shipment_notification_contacts,
)
from erp_automation.persistence import CustomWorkflowStore
from shipment_automation.notification_domain import (
    CHANNEL_EMAIL,
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_WAITING_CONTACT,
    NotificationConfiguration,
    OrderProductSnapshot,
    PACKAGE_MANUAL,
    PackageSnapshot,
)
from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.queue_store import ShipmentWorkflowStore


PLATFORM_ORDER_NO = "112-1234567-1234567"


def _configuration() -> NotificationConfiguration:
    return NotificationConfiguration(
        alimail_application_name="mail-app",
        alimail_app_id="app-id",
        alimail_app_secret="secret",
        clicksend_username="username",
        clicksend_api_key="key",
    )


def _store(tmp_path, *, system_count: int = 2) -> ShipmentNotificationStore:
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    now = "2026-07-21T00:00:00Z"
    system_order_nos = tuple(f"1000{index}" for index in range(1, system_count + 1))
    with sqlite3.connect(path) as conn:
        for index, system_order_no in enumerate(system_order_nos, start=1):
            conn.execute(
                """
                INSERT INTO shipment_jobs (
                    logistics_no, system_order_no, platform_order_no,
                    shipment_tag_name, identity_state, first_seen_at,
                    last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'tag', 'ACTIVE', ?, ?, ?, ?)
                """,
                (
                    f"ALS{index:011d}",
                    system_order_no,
                    PLATFORM_ORDER_NO,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO shipment_erp (job_id, state, checkpoint, updated_at) "
                "VALUES (?, 'DONE', 'OUTBOUNDED', ?)",
                (job_id, now),
            )
        conn.commit()

    store = ShipmentNotificationStore(path)
    store.upsert_wms_recipient_name(
        PLATFORM_ORDER_NO,
        "Customer",
        system_order_nos=system_order_nos,
    )
    store.replace_product_scan(
        PLATFORM_ORDER_NO,
        [
            OrderProductSnapshot(
                platform_order_no=PLATFORM_ORDER_NO,
                system_order_no=system_order_no,
                item_key=f"ITEM-{index}",
                source_sequence=index,
                local_sku=f"PRODUCT-{index}",
                raw_title="Test Product" if index == 1 else "",
                display_title="Test Product" if index == 1 else "",
                has_main_image=index == 1,
            )
            for index, system_order_no in enumerate(system_order_nos, start=1)
        ],
        system_order_nos,
    )
    store.replace_package_scan(
        PLATFORM_ORDER_NO,
        [
            PackageSnapshot(
                package_key="10001:WO-1",
                platform_order_no=PLATFORM_ORDER_NO,
                system_order_no=system_order_nos[0],
                shipment_type=PACKAGE_MANUAL,
                carrier_raw="UPS",
                carrier="UPS",
                waybill_no="1Z9999999999999999",
                tracking_no="ALS00000000001",
                final_tracking_no="1Z9999999999999999",
                stable_sequence=1,
                stable_label="a",
            )
        ],
    )
    waiting = store.prepare_notification(PLATFORM_ORDER_NO, _configuration())
    assert waiting is not None
    assert waiting["state"] == NOTIFICATION_WAITING_CONTACT
    return store


def _customization_payload(*, email: str, phone: str, order_id: str = PLATFORM_ORDER_NO):
    return {
        "orderId": order_id,
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


def _json_source(tmp_path, *payloads: dict):
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    workflow_store.mutate_legacy_record(
        PLATFORM_ORDER_NO,
        lambda current: {
            **current,
            "workflow_status": "completed",
            "last_seen_at": "2026-07-21T04:00:00Z",
            "contact_writeback_complete": True,
            "contact_completed_at": "2026-07-21T05:00:00Z",
            "folder_complete": True,
            "folder_completed_at": "2026-07-21T05:05:00Z",
        },
        event_type="test_workflow",
    )
    folder_root = tmp_path / "orders"
    folder = folder_root / "2026" / "7月" / "0721" / f"{PLATFORM_ORDER_NO}+Customer"
    folder.mkdir(parents=True)
    for index, payload in enumerate(payloads, start=1):
        (folder / f"custom-{index}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    return workflow_store, folder_root


def _refresh(store, waiting, workflow_store, folder_root, *, staging_root=None):
    return asyncio.run(
        refresh_shipment_notification_contacts(
            store,
            _configuration(),
            [waiting["id"]],
            workflow_store=workflow_store,
            folder_root=folder_root,
            staging_root=staging_root,
        )
    )


def test_manual_refresh_reads_matching_customization_json_and_creates_review(tmp_path) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    workflow_store, folder_root = _json_source(
        tmp_path,
        _customization_payload(email="customer@example.com", phone="4155552671"),
    )

    summary = _refresh(store, waiting, workflow_store, folder_root)

    assert summary.requested_count == 1
    assert summary.refreshed_count == 1
    assert summary.json_resolved_count == 1
    assert summary.detail_request_count == 0
    assert summary.new_review_count == 1
    contact = store.get_contact(PLATFORM_ORDER_NO)
    assert contact is not None
    assert contact.email == "customer@example.com"
    assert contact.phone_raw == "4155552671"
    assert contact.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
    assert contact.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
    latest = store.list_notifications()[0]
    assert latest["state"] == NOTIFICATION_AWAITING_REVIEW
    assert latest["channel"] == CHANNEL_EMAIL


def test_ambiguous_json_preserves_existing_contact(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_customization_contact(
        PLATFORM_ORDER_NO,
        email="existing@example.com",
        phone="4155552671",
    )
    current = store.prepare_notification(PLATFORM_ORDER_NO, _configuration())
    assert current is not None
    first = _customization_payload(email="first@example.com", phone="4155552671")
    second = _customization_payload(email="second@example.com", phone="4155552672")
    second["orderItemId"] = "item-2"
    workflow_store, folder_root = _json_source(tmp_path, first, second)

    summary = _refresh(store, current, workflow_store, folder_root)

    assert summary.conflict_count == 1
    assert summary.refreshed_count == 0
    assert store.get_contact(PLATFORM_ORDER_NO).email == "existing@example.com"
    assert store.list_notifications()[0]["id"] == current["id"]


def test_explicitly_empty_json_is_authoritative_and_clears_legacy_destination(tmp_path) -> None:
    store = _store(tmp_path)
    store.upsert_customization_contact(
        PLATFORM_ORDER_NO,
        email="old@example.com",
        phone="4155552671",
    )
    current = store.prepare_notification(PLATFORM_ORDER_NO, _configuration())
    workflow_store, folder_root = _json_source(
        tmp_path,
        _customization_payload(email="", phone=""),
    )

    summary = _refresh(store, current, workflow_store, folder_root)

    assert summary.refreshed_count == 1
    assert summary.json_empty_count == 1
    assert summary.no_usable_count == 1
    contact = store.get_contact(PLATFORM_ORDER_NO)
    assert contact.email == ""
    assert contact.phone_raw == ""
    assert store.list_notifications()[0]["state"] == NOTIFICATION_WAITING_CONTACT


def test_missing_json_is_reported_without_changing_contact(tmp_path) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")

    summary = _refresh(store, waiting, workflow_store, tmp_path / "orders")

    assert summary.no_usable_count == 1
    assert summary.json_missing_count == 1
    assert summary.refreshed_count == 0


def test_missing_workflow_uses_notification_month_to_find_final_json(tmp_path) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    folder_root = tmp_path / "orders"
    folder = (
        folder_root
        / "2026"
        / "7月"
        / "0715"
        / f"加急{PLATFORM_ORDER_NO}+Customer"
        / "CustomizedInfo"
    )
    folder.mkdir(parents=True)
    (folder / "custom.json").write_text(
        json.dumps(
            _customization_payload(
                email="customer@example.com",
                phone="4155552671",
            )
        ),
        encoding="utf-8",
    )

    summary = _refresh(store, waiting, workflow_store, folder_root)

    assert summary.refreshed_count == 1
    assert summary.json_resolved_count == 1
    contact = store.get_contact(PLATFORM_ORDER_NO)
    assert contact.email == "customer@example.com"
    assert contact.phone_raw == "4155552671"


def test_missing_workflow_can_read_order_specific_zip_staging(tmp_path) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    workflow_store = CustomWorkflowStore(tmp_path / "custom.sqlite3")
    staging_root = tmp_path / "logs" / "custom_zip_staging"
    folder = staging_root / PLATFORM_ORDER_NO / "CustomizedInfo"
    folder.mkdir(parents=True)
    (folder / "custom.json").write_text(
        json.dumps(
            _customization_payload(
                email="staging@example.com",
                phone="4155552672",
            )
        ),
        encoding="utf-8",
    )

    summary = _refresh(
        store,
        waiting,
        workflow_store,
        tmp_path / "missing-orders",
        staging_root=staging_root,
    )

    assert summary.refreshed_count == 1
    assert summary.json_resolved_count == 1
    contact = store.get_contact(PLATFORM_ORDER_NO)
    assert contact.email == "staging@example.com"
    assert contact.phone_raw == "4155552672"


def test_sent_notification_is_not_refreshable_and_does_not_read_json(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE shipment_notifications SET state = 'DELIVERED' WHERE id = ?",
            (waiting["id"],),
        )
        conn.commit()
    calls = {"count": 0}

    def unexpected(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("JSON resolver must not run for a sent notification")

    monkeypatch.setattr(
        "erp_automation.application.notification_contact_refresh.resolve_customization_json_contact",
        unexpected,
    )
    summary = asyncio.run(
        refresh_shipment_notification_contacts(
            store,
            _configuration(),
            [waiting["id"]],
            workflow_store=CustomWorkflowStore(tmp_path / "custom.sqlite3"),
            folder_root=tmp_path / "orders",
        )
    )

    assert summary.failed_count == 1
    assert calls["count"] == 0


def test_local_update_failure_is_reported_without_raising_from_batch(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    waiting = store.list_notifications()[0]
    workflow_store, folder_root = _json_source(
        tmp_path,
        _customization_payload(email="customer@example.com", phone="4155552671"),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("local failure")

    monkeypatch.setattr(store, "upsert_customization_contact", fail)
    summary = _refresh(store, waiting, workflow_store, folder_root)

    assert summary.requested_count == 1
    assert summary.failed_count == 1
    assert summary.results[0].status == "local_update_failed"
