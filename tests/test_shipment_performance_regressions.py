from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shipment_automation import queue_store as queue_module
from shipment_automation.logistics_worker import process_logistics_queue_once
from shipment_automation.models import LogisticsDetail, LOGISTICS_WAITING, ShipmentCandidate
from shipment_automation.queue_store import ShipmentWorkflowStore


def candidate(index: int) -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no=f"SYS-{index}", platform_order_no=f"ORDER-{index}",
        logistics_no=f"ALS{index:011}", shipment_tag_name="标发",
    )


def test_search_and_summary_are_read_only_even_while_writer_holds_transaction(tmp_path):
    path = tmp_path / "queue.sqlite3"
    writer = ShipmentWorkflowStore(path)
    writer.upsert_candidate(candidate(1))
    reader = ShipmentWorkflowStore(path, read_only=True)
    before = reader.queue_dataset_revision()
    with writer.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE shipment_jobs SET tag_text = 'uncommitted'")
        assert len(reader.list_queue_index_rows()) == 1
        assert len(reader.list_jobs_by_logistics_nos((candidate(1).logistics_no,))) == 1
        assert reader.count_all_jobs()[0] == 1
        assert reader.queue_dataset_revision() == before
        with reader.connect() as read:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                read.execute("UPDATE shipment_jobs SET tag_text = 'forbidden'")
        conn.rollback()
    assert reader.queue_dataset_revision() == before


def test_fresh_store_at_current_schema_never_runs_historical_repairs(tmp_path, monkeypatch):
    path = tmp_path / "queue.sqlite3"
    ShipmentWorkflowStore(path).upsert_candidate(candidate(1))
    def forbidden(*_args, **_kwargs):
        pytest.fail("reading a current queue must not run migrations")
    monkeypatch.setattr(ShipmentWorkflowStore, "_migrate_to_v4", forbidden)
    monkeypatch.setattr(ShipmentWorkflowStore, "_reconcile_logistics_overdue_conn", forbidden)
    assert ShipmentWorkflowStore(path).count_all_jobs()[0] == 1
    assert ShipmentWorkflowStore(path).list_queue_index_rows()[0]["system_order_no"] == "SYS-1"


def test_queue_revision_ignores_unrelated_writes_but_tracks_same_second_changes(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    store.upsert_candidate(candidate(1))
    before = store.queue_dataset_revision()
    with store.connect() as conn:
        conn.execute("CREATE TABLE unrelated_events (value TEXT)")
        conn.execute("INSERT INTO unrelated_events VALUES ('notification')")
        conn.commit()
    assert store.queue_dataset_revision() == before
    with store.connect() as conn:
        conn.execute("UPDATE shipment_logistics SET carrier_raw = 'FedEx'")
        conn.commit()
    changed = store.queue_dataset_revision()
    assert changed != before
    with store.connect() as conn:
        conn.execute("UPDATE shipment_logistics SET carrier_raw = 'UPS'")
        conn.commit()
    assert store.queue_dataset_revision() != changed


def test_run_cannot_reclaim_its_failure_after_retry_deadline_or_expand_its_due_set(tmp_path, monkeypatch):
    store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    original = candidate(1)
    store.upsert_candidate(original)
    allowed = (original.logistics_no,)
    claimed = store.claim_logistics_jobs("worker", eligible_logistics_nos=allowed, attempted_in_run_id="run-1")
    assert len(claimed) == 1
    store.complete_logistics_attempt(
        original.logistics_no, LogisticsDetail(logistics_no=original.logistics_no),
        state=LOGISTICS_WAITING, owner="worker", expected_version=claimed[0]["version"], run_id="run-1",
        last_error="not ready",
    )
    store.upsert_candidate(candidate(2))
    later = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(queue_module, "utc_now", lambda: later)
    assert store.claim_logistics_jobs("worker", eligible_logistics_nos=allowed, attempted_in_run_id="run-1") == []
    next_run = store.claim_logistics_jobs("worker", eligible_logistics_nos=allowed, attempted_in_run_id="run-2")
    assert [row["logistics_no"] for row in next_run] == [original.logistics_no]


def test_consecutive_page_failures_stop_without_consuming_unread_orders(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    for index in range(6):
        store.upsert_candidate(candidate(index))
    calls = []
    async def unavailable(logistics_no):
        calls.append(logistics_no)
        return LogisticsDetail(logistics_no=logistics_no, page_error="页面加载超时")
    report = asyncio.run(process_logistics_queue_once(
        store, fetch_detail=unavailable, update_queue=True, dry_run=False,
    ))
    assert report.status == "failed"
    assert len(calls) == report.scanned_page_count == 3
    assert report.aborted_count == 3
    assert [store.get_by_logistics_no(candidate(i).logistics_no)["logistics_attempt_count"] for i in range(6)] == [1, 1, 1, 0, 0, 0]


def completed_store(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    store.upsert_candidate(candidate(1))
    store.complete_logistics_attempt(
        candidate(1).logistics_no,
        LogisticsDetail(logistics_no=candidate(1).logistics_no, carrier="UPS",
                        international_tracking_no="1Z9253126709651051", actual_total="CNY 20",
                        chargeable_weight_kg="1", status_text="运输中"),
        state="READY", last_error=None,
    )
    assert store.mark_erp_outbounded(candidate(1).logistics_no, email_preview_enabled=False)
    return store


def test_ineligible_history_is_cached_and_new_evidence_invalidates_it(tmp_path):
    store = completed_store(tmp_path)
    store.update_completed_refresh_evidence(system_order_no="SYS-1", platform_order_no="ORDER-1", sales_platform_code="EBAY")
    assert store.list_completed_refresh_targets(evidence_only=True) == []
    store.upsert_candidate(replace(candidate(1), sales_platform_code="AMAZON"))
    assert len(store.list_completed_refresh_targets(evidence_only=True)) == 1
    store.defer_completed_refresh(system_order_no="SYS-1", platform_order_no="ORDER-1", logistics_no=candidate(1).logistics_no, reason="API unavailable")
    assert store.list_completed_refresh_targets(evidence_only=True) == []
    assert store.list_completed_refresh_targets(eligible_only=True) == []
    with store.connect() as conn:
        conn.execute("UPDATE shipment_jobs SET version = version + 1")
        conn.commit()
    assert len(store.list_completed_refresh_targets(evidence_only=True)) == 1


def test_history_read_concurrency_is_bounded_and_reuses_exact_system_order(monkeypatch):
    from erp_automation.application import desktop_services as module
    targets = [dict(system_order_no=f"SYS-{i // 2}", platform_order_no=f"ORDER-{i}", logistics_no=f"ALS-{i}") for i in range(6)]
    calls = []
    active = peak = 0

    async def get_detail(system_order_no):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(system_order_no)
        await asyncio.sleep(0.01)
        active -= 1
        return SimpleNamespace(payload={})

    def forbidden(**_kwargs):
        pytest.fail("ineligible order must not request WMS")

    monkeypatch.setattr(module, "completed_re_mark_evidence_from_payload", lambda *_a, **_kw: {})
    monkeypatch.setattr(module, "completed_re_mark_eligibility", lambda **_kw: SimpleNamespace(eligible=False))
    queue = SimpleNamespace(list_completed_refresh_targets=lambda **_kw: targets,
                            update_completed_refresh_evidence=lambda **_kw: 1,
                            defer_completed_refresh=forbidden)
    gateway = SimpleNamespace(get_order_detail=get_detail, list_wms_orders=forbidden)
    metrics = asyncio.run(module.DesktopApiServices._refresh_completed_shipment_eligibility_evidence(gateway, queue, run_id="history"))
    assert 1 <= peak <= 2
    assert sorted(calls) == ["SYS-0", "SYS-1", "SYS-2"]
    assert metrics["ineligible_count"] == 6
    assert metrics["wms_request_count"] == 0


@pytest.mark.parametrize("error_kind", ["page", "account"])
def test_history_technical_failure_stops_batch_and_is_not_unchanged(tmp_path, error_kind):
    from shipment_automation.alibaba_session import AlibabaAccountUnverifiedError
    from shipment_automation.logistics_worker import _process_completed_refresh_rows
    from shipment_automation.models import LogisticsWorkerReport
    report = LogisticsWorkerReport(status="completed")
    calls = []
    async def failure(logistics_no):
        calls.append(logistics_no)
        if error_kind == "account":
            raise AlibabaAccountUnverifiedError("login expired")
        return LogisticsDetail(logistics_no=logistics_no, page_error="timeout")
    asyncio.run(_process_completed_refresh_rows(
        ShipmentWorkflowStore(tmp_path / "queue.sqlite3"),
        [dict(logistics_no=f"ALS-{i}") for i in range(6)],
        fetch_detail=failure, retry_fetch_detail=None, report=report,
        update_queue=False, dry_run=True, run_id="history", progress_callback=None,
    ))
    assert len(calls) == (1 if error_kind == "account" else 3)
    assert report.status == ("identity_unverified" if error_kind == "account" else "failed")
    assert report.completed_refresh_unchanged_count == 0
    assert report.aborted_count == (6 if error_kind == "account" else 3)


def test_sql_search_preserves_contains_unicode_split_orders_and_facets(tmp_path):
    from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController
    controller = PersistentBackgroundTaskController(tmp_path, recover_interrupted_task_journal=False)
    store = ShipmentWorkflowStore(controller._shipment_state_path())
    try:
        for i, number in enumerate(("STRASSE-%_12", "Straße-%_123", "UNRELATED")):
            store.upsert_candidate(replace(candidate(i), platform_order_no=number, product_type="tent" if i < 2 else "poster"))
        result = controller.list_shipment_page(search_query="strasse-%_12", page_size=1)
        assert result.total == 2 and len(result.items) == 1
        assert set(result.facets.product_types) == {"tent", "poster"}
        second = controller.list_shipment_page(search_query="STRASSE-%_12", page_size=1, page=2)
        assert result.items[0].logistics_no != second.items[0].logistics_no
        assert controller.list_shipment_page(search_field="system_order_no", search_query="SYS-2").total == 1
        assert controller.list_shipment_page(search_query="missing").total == 0
        assert controller.list_shipment_page(search_field="bad SQL", search_query="STRASSE-%_12").total == 2
    finally:
        controller.close()


@pytest.mark.parametrize("mode, elapsed, error", [("page", 30, RuntimeError), ("login", 300, RuntimeError), ("transition", 90, RuntimeError)])
def test_page_and_login_wait_budgets_are_separate(monkeypatch, mode, elapsed, error):
    from shipment_automation import alibaba_session as session
    from shipment_automation.alibaba_session import AlibabaAccountUnverifiedError
    ticks = [0.0]
    class Page:
        url = "https://scm.alibaba.com/luyou/express/detail.htm?id=1"
        async def wait_for_timeout(self, milliseconds):
            ticks[0] += milliseconds / 1000
    async def body(_page):
        return "loading"
    async def login(_page, _body):
        return mode == "login" or mode == "transition" and ticks[0] < 60
    monkeypatch.setattr(session.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(session, "_safe_body_text", body)
    monkeypatch.setattr(session, "is_alibaba_login_page", login)
    with pytest.raises(AlibabaAccountUnverifiedError if mode == "login" else error):
        asyncio.run(session.wait_for_alibaba_logistics_detail(Page(), Page.url, login_config=None, auto_login=False))
    assert ticks[0] == elapsed


def test_detail_identity_allows_tracking_parameters_but_rejects_other_order():
    from shipment_automation.alibaba_session import _is_logistics_detail_url
    expected = "https://scm.alibaba.com/luyou/express/detail.htm?id=123"
    assert _is_logistics_detail_url(expected + "&spm=redirect#detail", expected)
    assert not _is_logistics_detail_url(expected.replace("id=123", "id=456"), expected)
    assert not _is_logistics_detail_url(expected + "&id=456", expected)
    assert not _is_logistics_detail_url(expected.replace("scm.alibaba.com", "example.com"), expected)


@pytest.mark.parametrize("navigation_seconds, mode, total_seconds", [
    (24, "page", 30),
    (29.8, "page", 30),
    (30, "page", 30),
    (30, "login", 330),
    (30, "transition", 120),
    (24, "batch", 90),
])
def test_navigation_does_not_add_another_page_wait_budget(tmp_path, monkeypatch, navigation_seconds, mode, total_seconds):
    from shipment_automation import alibaba_session as session
    from shipment_automation import logistics_worker as worker
    ticks = [0.0]

    class Page:
        url = ""
        def on(self, *_args):
            pass
        def remove_listener(self, *_args):
            pass
        async def goto(self, url, *, wait_until, timeout):
            assert wait_until == "domcontentloaded" and timeout == 30_000
            self.url = url
            ticks[0] += navigation_seconds
        async def wait_for_timeout(self, milliseconds):
            ticks[0] += milliseconds / 1000

    async def no_warmup(_page):
        pass
    async def body(_page):
        return "loading"
    async def login(_page, _body):
        return mode == "login" or mode == "transition" and ticks[0] < 90

    monkeypatch.setattr(session.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(session, "_safe_body_text", body)
    monkeypatch.setattr(session, "is_alibaba_login_page", login)
    monkeypatch.setattr(worker, "_warm_up_alibaba_page_if_needed", no_warmup)
    if mode == "batch":
        store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
        for index in range(6):
            store.upsert_candidate(candidate(index))
        async def fetch(logistics_no):
            return await worker.fetch_logistics_detail_from_page(SimpleNamespace(), logistics_no, page=Page(), auto_login=False)
        report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fetch, update_queue=True, dry_run=False))
        assert report.status == "failed"
        assert report.scanned_page_count == report.aborted_count == 3
        assert [store.get_by_logistics_no(candidate(i).logistics_no)["logistics_attempt_count"] for i in range(6)] == [1, 1, 1, 0, 0, 0]
    else:
        query = worker.fetch_logistics_detail_from_page(SimpleNamespace(), "ALS123", page=Page(), auto_login=False)
        if mode == "login":
            with pytest.raises(session.AlibabaAccountUnverifiedError):
                asyncio.run(query)
        else:
            assert "加载超时" in asyncio.run(query).page_error
    assert ticks[0] == pytest.approx(total_seconds, abs=0.002)
