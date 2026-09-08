from __future__ import annotations

import sqlite3

import pytest

from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.queue_store import ShipmentWorkflowStore


@pytest.mark.parametrize("existing_database", [False, True])
def test_notification_history_lookup_has_bounded_work_across_pages(
    tmp_path, monkeypatch, existing_database: bool,
) -> None:
    path = tmp_path / "notification-history.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    store = ShipmentNotificationStore(path)
    store.initialize()
    order_count = 300
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO shipment_notifications (platform_order_no, revision, state, "
            "template_version, content_hash, idempotency_key, body_html, "
            "created_at, updated_at, state_changed_at) "
            "VALUES (?, ?, ?, 'v1', 'content', ?, ?, ?, ?, ?)",
            [
                (
                    f"ORDER-{order:04d}", revision,
                    "DELIVERED"
                    if revision == 3 or (order % 2 == 0 and revision == 1)
                    else "CANCELLED",
                    f"notification-{order}-{revision}", "<p>archived email</p>" * 300,
                    "2026-09-08T01:00:00Z", "2026-09-08T01:00:00Z",
                    "2026-09-08T01:00:00Z",
                )
                for order in range(order_count)
                for revision in (1, 2, 3)
            ],
        )
        if existing_database:
            # An already deployed database gains the optimization on normal
            # initialization, without recreating notification history.
            conn.execute("DROP INDEX idx_shipment_notifications_prior_delivery")
        conn.commit()
    store = ShipmentNotificationStore(path)
    store.initialize()
    original_connect = store.connect
    instruction_blocks = 0

    def progress() -> int:
        nonlocal instruction_blocks
        instruction_blocks += 1
        # A deterministic VM-work budget catches the quadratic history scan
        # regardless of CPU speed, cache warmth, or the SQLite planner version.
        return int(instruction_blocks > 500)

    def counted_connect() -> sqlite3.Connection:
        conn = original_connect()
        conn.execute("PRAGMA query_only = ON")
        conn.set_progress_handler(progress, 1000)
        return conn

    monkeypatch.setattr(store, "connect", counted_connect)
    page_ids: list[int] = []
    for page in (1, 2):
        instruction_blocks = 0
        result = store.list_notification_page(
            page=page, page_size=50, outbound_eligible_only=False,
        )
        assert result["total"] == order_count
        assert len(result["items"]) == 50
        assert all(
            item["is_supplemental_revision"]
            == (int(item["platform_order_no"].split("-")[-1]) % 2 == 0)
            for item in result["items"]
        )
        assert all("body_html" not in item for item in result["items"])
        page_ids.extend(item["id"] for item in result["items"])
    assert len(set(page_ids)) == 100
    instruction_blocks = 0
    completed = store.list_notification_page(
        status="DELIVERED", outbound_eligible_only=False,
    )
    assert completed["total"] == order_count
    assert set(completed["statuses"]) == {"DELIVERED"}
