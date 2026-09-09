from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import sqlite3

import pytest

from erp_automation.application.queue_queries import paginate_shipment_rows, shipment_row_from_mapping
from erp_automation.ui.controller import InMemoryBackgroundTaskController
from shipment_automation.models import ShipmentCandidate
from shipment_automation.queue_store import ShipmentWorkflowStore


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def seed_queue(path, count=180):
    store = ShipmentWorkflowStore(path)
    store.initialize()
    randomizer = random.Random(9817)
    locks, overlays = {}, {}
    states = [
        ("ACTIVE", "READY", "PENDING", "NONE", ""),
        ("ACTIVE", "READY", "RUNNING", "NONE", ""),
        ("ACTIVE", "READY", "RETRYABLE", "NONE", ""),
        ("ACTIVE", "READY", "BLOCKED", "NONE", ""),
        ("ACTIVE", "PENDING", "PENDING", "NONE", ""),
        ("ACTIVE", "WAITING", "PENDING", "NONE", ""),
        ("ACTIVE", "RETRYABLE", "PENDING", "NONE", ""),
        ("ACTIVE", "BLOCKED", "PENDING", "NONE", ""),
        ("ACTIVE", "CANCELLED", "PENDING", "NONE", ""),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", ""),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", "DETECTED"),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", "MANUAL_REVIEW"),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", "COMPLETED"),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", "CANCELLED"),
        ("ACTIVE", "READY", "DONE", "OUTBOUNDED", "WITHDRAWN"),
        ("CANCELLED", "READY", "PENDING", "NONE", ""),
        ("MANUALLY_CANCELLED", "READY", "PENDING", "NONE", ""),
        ("PAUSED_TAG_REMOVED", "READY", "PENDING", "NONE", ""),
        ("CONFLICT", "READY", "PENDING", "NONE", ""),
        ("SUPERSEDED", "READY", "PENDING", "NONE", ""),
        ("", "", "", "", ""),
        (" active ", " ready ", "pending", " audited ", ""),
    ]
    for index in range(count):
        platform = f"Straße_%_{index:04}" if index % 7 == 0 else f"ORDER-{index:04}"
        store.upsert_candidate(ShipmentCandidate(
            system_order_no=f"系统-{index:04}", platform_order_no=platform,
            logistics_no=f"ALS-{index:04}", shipment_tag_name="自动标发", sku_text=f"detail-{index}",
        ))
        if index % 11 == 0:
            locks[platform.casefold()] = {"reason": "外部结果待核验", "created_at": NOW.timestamp() - index, "source_area": "shipment"}
        if index % 9 == 0:
            overlays[f"ALS-{index:04}"] = randomizer.choice(["标发处理中", "重新标发处理中", "等待标发", "等待用户确认"])
    with store.connect() as conn:
        for index in range(count):
            identity, logistics, erp, checkpoint, remark = states[index % len(states)]
            stamp = randomizer.choice(["2026-09-01T01:00:00Z", "2026-09-01T09:00:00+08:00", "bad", "", "2026-09-01T01:00:00"])
            conn.execute("UPDATE shipment_jobs SET identity_state=?, product_type=?, first_seen_at=?, "
                         "updated_at=?, state_changed_at=?, lease_owner=?, lease_until=? WHERE id=?", (
                identity, randomizer.choice(["tent", "TENT | 帐篷", "x_stands||tent", " ", "Straße"]),
                "2026-09-01T00:00:00Z" if index % 5 == 0 else NOW.isoformat(), stamp, stamp,
                "worker" if index % 4 == 0 else "", randomizer.choice(["", "bad", "2999-01-01T00:00:00Z", "2025-01-01T00:00:00Z"])
                if index % 4 == 0 else "", index + 1,
            ))
            conn.execute("UPDATE shipment_logistics SET state=?, carrier_raw=?, international_tracking_no=?, "
                         "currency='CNY', fee_amount=?, chargeable_weight_kg=?, state_changed_at=? WHERE job_id=?", (
                logistics, "UPS" if index % 8 else "", "1Z9253126709651051" if index % 6 else "invalid",
                "10" if index % 13 else "", "1" if index % 17 else "", stamp, index + 1,
            ))
            conn.execute("UPDATE shipment_erp SET state=?, checkpoint=?, state_changed_at=?, outbounded_at=? WHERE job_id=?",
                         (erp, checkpoint, stamp, stamp if erp == "DONE" else "", index + 1))
            if remark:
                for revision in (1, 2):
                    conn.execute("""INSERT INTO shipment_re_mark_cycles (
                        job_id, revision_no, source_snapshot_hash, state, checkpoint, system_order_no,
                        platform_order_no, logistics_no, new_carrier, new_waybill_no, new_tracking_no,
                        new_freight, new_currency, new_fee_weight_g, detected_at, created_at, updated_at
                    ) SELECT id, ?, ?, ?, 'NONE', system_order_no, platform_order_no, logistics_no,
                             'UPS', 'waybill', 'tracking', '10', 'CNY', '1000', ?, ?, ?
                      FROM shipment_jobs WHERE id=?""",
                                 (revision, f"hash-{revision}", remark if revision == 2 else "COMPLETED", stamp, stamp, stamp, index + 1))
        for index in range(12):
            conn.execute("""INSERT INTO shipment_scan_issues (
                system_order_no, platform_order_no, issue_code, shipment_tag_name, error_message,
                first_seen_at, last_seen_at, updated_at, management_state, management_updated_at, resolved_at
            ) VALUES (?, 'ISSUE-ORDER', ?, '自动标发', 'synthetic error', ?, ?, ?, ?, ?, ?)""",
                         (f"issue-system-{index}", f"issue-{index}", NOW.isoformat(), NOW.isoformat(),
                          NOW.isoformat(), ["ACTIVE", "MANUAL_REVIEW", "MANUALLY_COMPLETED", "MANUALLY_CANCELLED"][index % 4],
                          "", NOW.isoformat() if index > 7 else None))
    return store, locks, overlays


@pytest.fixture(scope="module")
def queue(tmp_path_factory):
    return seed_queue(tmp_path_factory.mktemp("sql-queue") / "queue.sqlite3")


def assert_matches_reference(store, locks, overlays, **options):
    # This is the pre-change all-index algorithm, independent of the SQL adapter.
    raw = store.list_queue_index_rows()
    apply_locks = InMemoryBackgroundTaskController._shipment_rows_with_review_locks
    rows = apply_locks(tuple(shipment_row_from_mapping(row) for row in raw), locks)
    expected = paginate_shipment_rows(rows, active_statuses=overlays, now=NOW, **options)
    actual = ShipmentWorkflowStore(store.path, read_only=True).list_queue_page(
        review_locks=locks, active_statuses=overlays, now=NOW, **options,
    )
    jobs = {row["logistics_no"]: row for row in store.list_jobs_by_logistics_nos(
        [row.logistics_no for row in expected.items if row.logistics_no and not row.scan_issue_code],
    )}
    issues = {row.get("scan_issue_key"): row for row in raw if row.get("scan_issue_key")}
    expected_items = apply_locks(tuple(shipment_row_from_mapping(
        issues[row.scan_issue_key] if row.scan_issue_code else jobs[row.logistics_no],
    ) for row in expected.items), locks)
    actual_items = apply_locks(tuple(shipment_row_from_mapping(row) for row in actual["items"]), locks)
    assert actual_items == expected_items
    assert (actual["page"], actual["page_size"], actual["total"]) == (expected.page, expected.page_size, expected.total)
    assert actual["statuses"] == expected.facets.statuses
    assert actual["product_types"] == expected.facets.product_types
    return actual


@pytest.mark.parametrize("options", [
    {}, {"page": 3, "page_size": 7}, {"page": 999, "page_size": 13}, {"page": -3, "page_size": 0},
    {"page_size": 1000}, {"search_query": "STRASSE"}, {"search_query": "%_"},
    {"search_field": "system_order_no", "search_query": "系统-00"},
    {"search_field": "untrusted sql", "search_query": "ORDER"},
    {"search_query": "no results"}, {"product_types": ("tent",)},
    {"product_types": (" STRASSE ", "帐篷")}, {"status": "unknown"},
])
def test_sql_pages_equal_old_algorithm_for_filters_sort_and_details(queue, options):
    store, locks, overlays = queue
    assert_matches_reference(store, locks, overlays, **options)


def test_every_visible_status_and_every_page_matches_reference(queue):
    store, locks, overlays = queue
    all_rows = store.list_queue_page(review_locks=locks, active_statuses=overlays, now=NOW)
    for status in all_rows["statuses"]:
        first = assert_matches_reference(store, locks, overlays, status=status, page_size=7)
        for page in range(2, (first["total"] + 6) // 7 + 1):
            assert_matches_reference(store, locks, overlays, status=status, page_size=7, page=page)


def test_queue_page_does_not_use_unbounded_row_readers_or_materialize_other_details(queue, monkeypatch):
    writer, locks, overlays = queue
    reader = ShipmentWorkflowStore(writer.path, read_only=True)

    def forbidden(*_args, **_kwargs):
        pytest.fail("a page must not load the full index or issue list")

    for name in ("list_queue_index_rows", "list_all_jobs", "list_active_scan_issues"):
        monkeypatch.setattr(reader, name, forbidden)
    flattened = []
    original = reader._flatten

    def counted(row):
        flattened.append(row["id"])
        return original(row)

    monkeypatch.setattr(reader, "_flatten", counted)
    result = reader.list_queue_page(page_size=7, review_locks=locks, active_statuses=overlays, now=NOW)
    assert len(result["items"]) == 7
    assert len(flattened) <= 7


def test_empty_and_out_of_range_queue(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "empty.sqlite3")
    result = store.list_queue_page(page=9999, page_size=200, now=NOW)
    assert result["page"] == 1
    assert result["total"] == 0
    assert result["items"] == []
    assert result["statuses"] == result["product_types"] == ()


def test_hydration_total_and_revision_share_the_same_read_snapshot(tmp_path, monkeypatch):
    from shipment_automation import queue_page_query

    store, _, _ = seed_queue(tmp_path / "snapshot.sqlite3", count=1)
    before = store.queue_dataset_revision()
    original = queue_page_query.read_page_index

    def change_after_selection(*args, **kwargs):
        result = original(*args, **kwargs)
        with store.connect() as writer:
            writer.execute("UPDATE shipment_jobs SET sku_text='changed after selection'")
        return result

    monkeypatch.setattr(queue_page_query, "read_page_index", change_after_selection)
    result = ShipmentWorkflowStore(store.path, read_only=True).list_queue_page(now=NOW, search_query="Straße")
    assert result["items"][0]["sku_text"] == "detail-0"
    assert result["dataset_revision"] == before
    assert store.queue_dataset_revision() != before


def test_page_is_read_only_while_writer_has_uncommitted_changes(tmp_path):
    store, _, _ = seed_queue(tmp_path / "readonly.sqlite3", count=1)
    reader = ShipmentWorkflowStore(store.path, read_only=True)
    before = reader.queue_dataset_revision()
    with store.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE shipment_jobs SET sku_text='uncommitted'")
        result = reader.list_queue_page(now=NOW, search_query="Straße")
        assert result["items"][0]["sku_text"] == "detail-0"
        assert result["dataset_revision"] == before
        writer.rollback()
    assert reader.queue_dataset_revision() == before


def test_facets_cache_respects_data_dynamic_context_and_time_boundaries(tmp_path):
    store, _, _ = seed_queue(tmp_path / "facets.sqlite3", count=1)
    with store.connect() as conn:
        conn.execute("DELETE FROM shipment_scan_issues")
        conn.execute("UPDATE shipment_jobs SET identity_state='ACTIVE', first_seen_at=?, lease_owner='worker', lease_until=?",
                     (NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()))
        conn.execute("UPDATE shipment_logistics SET state='READY', carrier_raw='UPS', international_tracking_no='1Z', fee_amount='1', chargeable_weight_kg='1'")
    first = store.list_queue_page(now=NOW)
    assert first["statuses"] == ("标发处理中",)
    second = store.list_queue_page(now=NOW + timedelta(seconds=1), cached_facets=first["facets_cache"])
    assert second["statuses"] == ("可标发",)
    backwards = store.list_queue_page(now=NOW, cached_facets=second["facets_cache"])
    assert backwards["statuses"] == ("标发处理中",)
    changed_overlay = store.list_queue_page(now=NOW, active_statuses={"ALS-0000": "等待用户确认"}, cached_facets=first["facets_cache"])
    assert changed_overlay["statuses"] == ("等待用户确认",)
    lock = {"strasse_%_0000": {"reason": "review", "created_at": NOW.timestamp()}}
    changed_lock = store.list_queue_page(now=NOW, review_locks=lock, cached_facets=first["facets_cache"])
    assert changed_lock["statuses"] == ("标发需人工复核",)
    with store.connect() as conn:
        conn.execute("UPDATE shipment_jobs SET product_type='new type'")
    changed_data = store.list_queue_page(now=NOW, cached_facets=first["facets_cache"])
    assert changed_data["product_types"] == ("new type",)


def test_tracking_deadline_invalidates_cached_facets_without_database_write(tmp_path):
    store, _, _ = seed_queue(tmp_path / "deadline.sqlite3", count=1)
    with store.connect() as conn:
        conn.execute("DELETE FROM shipment_scan_issues")
        conn.execute("UPDATE shipment_jobs SET identity_state='ACTIVE', customer_shipping_service='expedited', "
                     "first_seen_at='2026-09-08T00:00:00Z', lease_owner='', lease_until=''")
        conn.execute("UPDATE shipment_logistics SET state='WAITING', carrier_raw='', international_tracking_no=''")
    before = datetime(2026, 9, 9, 9, 29, 59, tzinfo=timezone.utc)
    first = store.list_queue_page(now=before)
    second = store.list_queue_page(now=before + timedelta(seconds=1), cached_facets=first["facets_cache"])
    assert first["statuses"] == ("等待物流就绪",)
    assert second["statuses"] == ("物流逾期异常",)
    assert first["dataset_revision"] == second["dataset_revision"]
