import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest
import shipment_automation.queue_store as queue_store_module

from shipment_automation.alibaba_logistics import tracking_number_mismatch_reason
from shipment_automation.models import (
    EMAIL_BLOCKED,
    EMAIL_PENDING,
    EMAIL_RETRYABLE,
    EMAIL_SENT,
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_NONE,
    ERP_CHECKPOINT_OUTBOUNDED,
    ERP_COMPLETION_MANUAL_DETECTED,
    ERP_BLOCKED,
    ERP_DONE,
    ERP_PENDING,
    ERP_RETRYABLE,
    ERP_WAITING,
    IDENTITY_ACTIVE,
    IDENTITY_CANCELLED,
    IDENTITY_CONFLICT,
    IDENTITY_MANUALLY_CANCELLED,
    IDENTITY_PAUSED_TAG_REMOVED,
    IDENTITY_SUPERSEDED,
    LOGISTICS_BLOCKED,
    LOGISTICS_CANCELLED,
    LOGISTICS_PENDING,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LogisticsDetail,
    SALES_CHANNEL_INDEPENDENT_SITE,
    SALES_CHANNEL_MARKETPLACE,
    ShipmentCandidate,
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
)
from shipment_automation.queue_store import SCHEMA_VERSION, ShipmentWorkflowStore, utc_now


def _candidate(
    logistics_no: str = "ALS01781406025",
    system_order_no: str = "103710434633847501",
    platform_order_no: str = "112-1165824-9982644",
    receiver_email: str | None = "buyer@example.com",
) -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no=system_order_no,
        platform_order_no=platform_order_no,
        logistics_no=logistics_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {logistics_no}",
        status_text="待审核发货",
        receiver_email=receiver_email,
        product_type="tent",
    )


def _ready_detail(logistics_no: str) -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=logistics_no,
        status_text="运输中",
        service_type="快递门到门",
        service_line="UPS-Saver",
        carrier="UPS",
        international_tracking_no="1Z9253126709651051",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
        package_count=1,
    )


def _make_ready(store: ShipmentWorkflowStore, logistics_no: str) -> None:
    assert store.complete_logistics_attempt(
        logistics_no,
        _ready_detail(logistics_no),
        state=LOGISTICS_READY,
        last_error=None,
    )


def test_queue_index_and_one_complete_page_stay_under_one_second(tmp_path) -> None:
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    for index in range(481):
        store.upsert_candidate(
            _candidate(
                logistics_no=f"ALS-PERF-{index:04d}",
                system_order_no=f"SYS-PERF-{index:04d}",
                platform_order_no=f"ORDER-PERF-{index:04d}",
            )
        )

    started_at = time.perf_counter()
    index_rows = store.list_queue_index_rows()
    first_page = store.list_jobs_by_logistics_nos(
        [row["logistics_no"] for row in index_rows[:50]]
    )
    elapsed_seconds = time.perf_counter() - started_at

    assert len(index_rows) == 481
    assert len(first_page) == 50
    assert elapsed_seconds < 1.0


def test_repeat_scan_updates_scan_time_without_faking_business_or_query_time(
    tmp_path,
    monkeypatch,
):
    timestamps = iter(
        (
            "2026-08-10T01:00:00Z",
            "2026-08-10T01:00:00Z",
            "2026-08-10T02:00:00Z",
            "2026-08-10T02:00:00Z",
            "2026-08-10T03:00:00Z",
            "2026-08-10T03:00:00Z",
            "2026-08-10T04:00:00Z",
            "2026-08-10T04:00:00Z",
        )
    )
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.initialize()
    monkeypatch.setattr(queue_store_module, "utc_now", lambda: next(timestamps))
    candidate = _candidate()

    store.upsert_candidate(candidate)
    discovered = store.get_by_logistics_no(candidate.logistics_no)
    assert discovered["last_scanned_at"] == "2026-08-10T01:00:00Z"
    assert discovered["identity_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert discovered["logistics_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert discovered["erp_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert discovered["logistics_last_checked_at"] is None

    store.upsert_candidate(candidate)
    rescanned = store.get_by_logistics_no(candidate.logistics_no)
    assert rescanned["last_scanned_at"] == "2026-08-10T02:00:00Z"
    assert rescanned["identity_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert rescanned["logistics_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert rescanned["erp_state_changed_at"] == "2026-08-10T01:00:00Z"
    assert rescanned["logistics_last_checked_at"] is None

    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _ready_detail(candidate.logistics_no),
        state=LOGISTICS_READY,
        last_error=None,
    )
    queried = store.get_by_logistics_no(candidate.logistics_no)
    assert queried["logistics_state_changed_at"] == "2026-08-10T03:00:00Z"
    assert queried["erp_state_changed_at"] == "2026-08-10T03:00:00Z"
    assert queried["logistics_last_checked_at"] == "2026-08-10T03:00:00Z"

    store.upsert_candidate(candidate)
    rescanned_ready = store.get_by_logistics_no(candidate.logistics_no)
    assert rescanned_ready["last_scanned_at"] == "2026-08-10T04:00:00Z"
    assert rescanned_ready["logistics_state_changed_at"] == "2026-08-10T03:00:00Z"
    assert rescanned_ready["erp_state_changed_at"] == "2026-08-10T03:00:00Z"
    assert rescanned_ready["logistics_last_checked_at"] == "2026-08-10T03:00:00Z"


def _force_legacy_program_block(
    store: ShipmentWorkflowStore,
    logistics_no: str,
) -> None:
    """Simulate a BLOCKED row written before program errors became retryable."""

    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_logistics
            SET state = ?, next_attempt_at = NULL
            WHERE job_id = (
                SELECT id FROM shipment_jobs WHERE logistics_no = ?
            )
            """,
            (LOGISTICS_BLOCKED, logistics_no),
        )


def _create_v1_database(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE shipment_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system_order_no TEXT NOT NULL,
            platform_order_no TEXT NOT NULL,
            als_no TEXT NOT NULL,
            shipment_tag_name TEXT NOT NULL,
            tag_text TEXT,
            sku_text TEXT,
            customer_remark TEXT,
            status_text TEXT,
            receiver_email TEXT,
            carrier TEXT,
            international_tracking_no TEXT,
            logistics_order_no TEXT,
            actual_total TEXT,
            chargeable_weight_kg TEXT,
            package_count INTEGER,
            queue_status TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processed_at TEXT,
            email_sent_at TEXT
        )
        """
    )
    for row in rows:
        values = {
            "system_order_no": "103710434633847501",
            "platform_order_no": "112-1165824-9982644",
            "als_no": row["logistics_no"],
            "shipment_tag_name": "自动标发",
            "tag_text": "自动标发",
            "sku_text": "10x10",
            "customer_remark": row["logistics_no"],
            "status_text": "待审核发货",
            "receiver_email": "buyer@example.com",
            "carrier": row.get("carrier"),
            "international_tracking_no": row.get("tracking"),
            "logistics_order_no": row["logistics_no"],
            "actual_total": row.get("amount"),
            "chargeable_weight_kg": row.get("weight"),
            "package_count": 1,
            "queue_status": row["status"],
            "last_error": row.get("error"),
            "created_at": "2026-07-08 16:14:44",
            "updated_at": "2026-07-09 18:15:51",
            "processed_at": row.get("processed_at"),
            "email_sent_at": row.get("email_sent_at"),
        }
        columns = list(values)
        conn.execute(
            f"INSERT INTO shipment_queue ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
    conn.commit()
    conn.close()


def test_v13_migration_requeries_only_unset_ups_tasks_missing_service_line(
    tmp_path,
):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    first = _candidate()
    second = _candidate(
        logistics_no="ALS01781406026",
        system_order_no="103710434633847502",
    )
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    _make_ready(store, second.logistics_no)
    with sqlite3.connect(path) as conn:
        second_job_id = conn.execute(
            "SELECT id FROM shipment_jobs WHERE logistics_no = ?",
            (second.logistics_no,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE shipment_erp SET checkpoint = ? WHERE job_id = ?",
            (ERP_CHECKPOINT_CHANNEL_SET, second_job_id),
        )
        conn.execute("ALTER TABLE shipment_logistics DROP COLUMN service_line")
        conn.execute("PRAGMA user_version = 12")
        conn.commit()

    migrated = ShipmentWorkflowStore(path)
    migrated.initialize()

    first_row = migrated.get_by_logistics_no(first.logistics_no)
    second_row = migrated.get_by_logistics_no(second.logistics_no)
    assert first_row["logistics_state"] == LOGISTICS_RETRYABLE
    assert first_row["erp_state"] == ERP_WAITING
    assert "重新查询阿里服务线路" in first_row["logistics_last_error"]
    assert second_row["logistics_state"] == LOGISTICS_READY
    assert second_row["erp_checkpoint"] == ERP_CHECKPOINT_CHANNEL_SET
    assert list(tmp_path.glob("shipment_queue.pre_v13_*.sqlite3"))


def test_candidate_upsert_uses_only_logistics_no_identity(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")

    first = store.upsert_candidate(_candidate())
    duplicate = store.upsert_candidate(_candidate())

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.conflict is False
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_no"] == "ALS01781406025"
    assert row["identity_state"] == IDENTITY_ACTIVE
    assert row["logistics_state"] == LOGISTICS_PENDING
    assert row["erp_state"] != ERP_PENDING


@pytest.mark.parametrize(
    ("stage", "expected_logistics", "expected_erp", "expected_checkpoint"),
    [
        ("logistics", LOGISTICS_RETRYABLE, ERP_WAITING, ERP_CHECKPOINT_NONE),
        ("set_channel", LOGISTICS_READY, ERP_RETRYABLE, ERP_CHECKPOINT_NONE),
        ("audit", LOGISTICS_READY, ERP_RETRYABLE, ERP_CHECKPOINT_CHANNEL_SET),
        ("tracking", LOGISTICS_READY, ERP_RETRYABLE, ERP_CHECKPOINT_AUDITED),
        ("outbound", LOGISTICS_READY, ERP_RETRYABLE, ERP_CHECKPOINT_LOGISTICS_SAVED),
    ],
)
def test_completed_shipment_can_be_reopened_from_any_business_stage(
    tmp_path,
    stage,
    expected_logistics,
    expected_erp,
    expected_checkpoint,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    assert store.mark_erp_outbounded(candidate.logistics_no)
    before = store.get_by_logistics_no(candidate.logistics_no)
    assert before["erp_state"] == ERP_DONE
    assert before["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED

    summary = store.reopen_shipments_from_stage(
        [candidate.logistics_no, candidate.logistics_no],
        stage,
        reason="ERP 已人工退回，按核验阶段续作",
    )

    assert summary.requested_count == 1
    assert summary.changed_logistics_nos == (candidate.logistics_no,)
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == expected_logistics
    assert row["erp_state"] == expected_erp
    assert row["erp_checkpoint"] == expected_checkpoint
    assert row["completion_source"] is None
    assert row["outbounded_at"] is None
    assert row["lease_owner"] is None
    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == "SHIPMENT_STAGE_MANUALLY_REOPENED"
    assert event.details["target_stage"] == stage
    assert event.details["source"] == "desktop_user"
    assert event.details["previous"]["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert event.details["previous"]["completion_source"]


def test_completed_shipment_can_be_moved_to_manual_review_with_evidence_preserved(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    assert store.mark_erp_outbounded(candidate.logistics_no)

    summary = store.move_completed_to_manual_review_many(
        [candidate.logistics_no],
        reason="临时异常，必须重新核对",
    )

    assert summary.changed_logistics_nos == (candidate.logistics_no,)
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["erp_state"] == ERP_BLOCKED
    assert row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert row["completion_source"]
    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == "MANUAL_COMPLETION_REVIEW_OPENED"
    assert event.details["preserved_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert event.details["completion_source"]


def test_erp_completion_does_not_create_email_preview_when_feature_is_disabled(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)

    assert store.mark_erp_outbounded(candidate.logistics_no)
    assert store.list_email_batches(platform_order_no=candidate.platform_order_no) == []


def test_independent_site_order_disables_customer_email(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")

    result = store.upsert_candidate(_candidate(platform_order_no="wc39877"))

    assert result.inserted is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["sales_channel"] == SALES_CHANNEL_INDEPENDENT_SITE
    assert row["customer_email_required"] == 0


def test_marketplace_order_keeps_customer_email_required(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")

    store.upsert_candidate(_candidate(platform_order_no="112-1165824-9982644"))

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["sales_channel"] == SALES_CHANNEL_MARKETPLACE
    assert row["customer_email_required"] == 1


def test_same_logistics_no_on_different_order_is_frozen_as_conflict(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())

    result = store.upsert_candidate(
        _candidate(system_order_no="103710639045926988", platform_order_no="111-8854282-5961022")
    )

    assert result.conflict is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["identity_state"] == IDENTITY_CONFLICT
    assert row["system_order_no"] == "103710434633847501"
    assert store.history("ALS01781406025")[-1].event_type == "LOGISTICS_NUMBER_CONFLICT"


def test_repeat_scan_refreshes_source_without_resetting_stage(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store, "ALS01781406025")
    refreshed = _candidate()
    refreshed.sku_text = "updated sku"

    store.upsert_candidate(refreshed)

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["sku_text"] == "updated sku"
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["erp_state"] == ERP_PENDING


def test_new_als_for_same_platform_and_system_updates_queue_in_place(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    original = _candidate(logistics_no="ALS01823850227")
    store.upsert_candidate(original)
    store.complete_logistics_attempt(
        original.logistics_no,
        LogisticsDetail(logistics_no=original.logistics_no, status_text="订单终止"),
        state=LOGISTICS_WAITING,
        last_error="缺少国际物流服务商或国际物流单号，下次继续查询。",
    )

    # Opening an existing queue upgrades the old WAITING representation to a
    # terminal logistics cancellation without cancelling the ERP identity.
    store = ShipmentWorkflowStore(path)
    closed = store.get_by_logistics_no(original.logistics_no)
    assert closed["identity_state"] == IDENTITY_ACTIVE
    assert closed["logistics_state"] == LOGISTICS_CANCELLED
    assert closed["logistics_next_attempt_at"] is None
    assert store.list_cancelled_logistics_refresh_targets() == [
        {
            "system_order_no": original.system_order_no,
            "platform_order_no": original.platform_order_no,
            "logistics_no": original.logistics_no,
            "shipment_tag_name": original.shipment_tag_name,
            "customer_shipping_service": None,
        }
    ]

    replacement = _candidate(logistics_no="ALS01825902784")
    result = store.upsert_candidate(replacement, run_id="replacement-scan")

    assert result.inserted is False
    assert result.immediate_logistics is True
    assert store.get_by_logistics_no(original.logistics_no) is None
    current = store.get_by_logistics_no(replacement.logistics_no)
    assert current["logistics_state"] == LOGISTICS_PENDING
    assert current["logistics_last_error"] is None
    assert len(store.list_all_jobs()) == 1
    assert store.history(replacement.logistics_no)[-1].event_type == "PLATFORM_LOGISTICS_NUMBER_REPLACED"


def test_initialize_hides_legacy_duplicate_business_identity(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    first = _candidate(logistics_no="ALS01823850227")
    store.upsert_candidate(first)
    with store.connect() as conn:
        now = utc_now()
        conn.execute(
            """
            INSERT INTO shipment_jobs (
                logistics_no, system_order_no, platform_order_no,
                shipment_tag_name, identity_state, first_seen_at,
                last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, '自动标发', 'ACTIVE', ?, ?, ?, ?)
            """,
            (
                "ALS01825902784",
                first.system_order_no,
                first.platform_order_no,
                now, now, now, now,
            ),
        )
        job_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO shipment_logistics (job_id, state, updated_at) VALUES (?, ?, ?)",
            (job_id, LOGISTICS_READY, now),
        )
        conn.execute(
            "INSERT INTO shipment_erp (job_id, state, checkpoint, updated_at) VALUES (?, ?, ?, ?)",
            (job_id, ERP_DONE, ERP_CHECKPOINT_OUTBOUNDED, now),
        )
        conn.commit()
    repaired = ShipmentWorkflowStore(path)

    assert [row["logistics_no"] for row in repaired.list_all_jobs()] == ["ALS01825902784"]
    assert repaired.get_by_logistics_no("ALS01823850227")["identity_state"] == IDENTITY_SUPERSEDED


def test_manual_cancellation_is_persistent_and_can_include_completed_job(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)

    summary = store.mark_manually_cancelled_many(
        [candidate.logistics_no], reason="临时情况，不再处理"
    )

    assert summary.changed_logistics_nos == (candidate.logistics_no,)
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_MANUALLY_CANCELLED
    store.upsert_candidate(candidate, allow_tag_restore=True)
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_MANUALLY_CANCELLED
    restored = store.restore_manually_cancelled_many(
        [candidate.logistics_no], reason="确认恢复"
    )
    assert restored.changed_count == 1
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_ACTIVE


def test_repeat_scan_makes_waiting_logistics_immediately_claimable(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    store.complete_logistics_attempt(
        "ALS01781406025",
        LogisticsDetail(logistics_no="ALS01781406025", status_text="待揽收"),
        state=LOGISTICS_WAITING,
        last_error="阿里物流状态未就绪：待揽收",
    )
    assert store.list_logistics_check_candidates() == []

    result = store.upsert_candidate(_candidate())

    assert result.immediate_logistics is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_WAITING
    assert row["logistics_next_attempt_at"] is None
    assert [item["logistics_no"] for item in store.claim_logistics_jobs("worker-2")] == ["ALS01781406025"]


def test_repeat_scan_makes_ready_erp_immediately_claimable(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store, "ALS01781406025")
    store.finish_erp_attempt(
        "ALS01781406025",
        owner=None,
        state=ERP_RETRYABLE,
        last_error="上一轮 ERP 暂时失败",
    )
    assert store.claim_erp_jobs("worker-1") == []

    result = store.upsert_candidate(_candidate())

    assert result.immediate_erp is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["erp_state"] == ERP_RETRYABLE
    assert row["erp_next_attempt_at"] is None
    assert [item["logistics_no"] for item in store.claim_erp_jobs("worker-2")] == ["ALS01781406025"]


def test_repeat_scan_does_not_reprocess_done_or_cancelled_jobs(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store, "ALS01781406025")
    store.mark_erp_outbounded("ALS01781406025")

    done_result = store.upsert_candidate(_candidate())

    assert done_result.immediate_logistics is False
    assert done_result.immediate_erp is False
    assert store.claim_logistics_jobs("worker-1") == []
    assert store.claim_erp_jobs("worker-1") == []

    other = _candidate(
        logistics_no="ALS01789020252",
        system_order_no="103710434633847502",
        platform_order_no="112-1165824-9982645",
    )
    store.upsert_candidate(other)
    store.cancel(other.logistics_no, "测试取消")
    cancelled_result = store.upsert_candidate(other)

    assert cancelled_result.existing["identity_state"] == IDENTITY_CANCELLED
    assert cancelled_result.immediate_logistics is False
    assert cancelled_result.immediate_erp is False


@pytest.mark.parametrize(
    "technical_error",
    [
        "等待阿里国际站物流详情页加载或登录完成超时。",
        "阿里物流详情读取失败：'NoneType' object has no attribute 'new_page'",
    ],
)
def test_programmatic_block_request_is_coerced_and_repeat_scan_is_immediate(
    tmp_path,
    technical_error,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    store.complete_logistics_attempt(
        "ALS01781406025",
        LogisticsDetail(
            logistics_no="ALS01781406025",
            page_error=technical_error,
        ),
        state=LOGISTICS_BLOCKED,
        last_error=technical_error,
    )
    before_scan = store.get_by_logistics_no("ALS01781406025")
    assert before_scan["logistics_state"] == LOGISTICS_RETRYABLE
    assert before_scan["logistics_next_attempt_at"]

    result = store.upsert_candidate(_candidate())

    assert result.immediate_logistics is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["logistics_next_attempt_at"] is None


def test_repeat_scan_refreshes_retryable_unknown_carrier_and_replaces_stale_facts(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(
        logistics_no="ALS01851930430",
        system_order_no="103724776794940694",
        platform_order_no="wc39952",
    )
    store.upsert_candidate(candidate)
    stale_error = (
        "承运商为 Unknown，且无法根据运单号 "
        "WNBAA0486972500YQ 唯一判断真实承运商。"
    )
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="Unknown",
            international_tracking_no="WNBAA0486972500YQ",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=stale_error,
    )

    result = store.upsert_candidate(candidate, run_id="repeat-scan-after-alibaba-update")

    assert result.immediate_logistics is True
    retryable = store.get_by_logistics_no(candidate.logistics_no)
    assert retryable["logistics_state"] == LOGISTICS_RETRYABLE
    assert retryable["logistics_next_attempt_at"] is None
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            service_type="快递含到门",
            service_line="无忧全球普货专线",
            carrier="USPS",
            international_tracking_no="9235990374018502989276",
            actual_total="CNY 1.00",
            chargeable_weight_kg="1.000",
            package_count=1,
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    refreshed = store.get_by_logistics_no(candidate.logistics_no)
    assert refreshed["logistics_state"] == LOGISTICS_READY
    assert refreshed["carrier"] == "USPS"
    assert refreshed["international_tracking_no"] == "9235990374018502989276"
    assert refreshed["logistics_last_error"] is None


def test_repeat_scan_preserves_explicit_order_issue_tracking_stop(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    reason = tracking_number_mismatch_reason("USPS", "WNBAA0486972500YQ")
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            carrier="USPS",
            international_tracking_no="WNBAA0486972500YQ",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=reason,
    )
    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    result = store.upsert_candidate(candidate, run_id="repeat-scan-human-stop")

    assert result.immediate_logistics is False
    blocked = store.get_by_logistics_no(candidate.logistics_no)
    assert blocked["logistics_state"] == LOGISTICS_BLOCKED
    assert blocked["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE


def test_legacy_program_blocks_are_requeued_but_explicit_human_stop_is_preserved(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    automated = _candidate("ALS-AUTO", "SYS-AUTO", "111-AUTO")
    human = _candidate("ALS-HUMAN", "SYS-HUMAN", "111-HUMAN")
    store.insert_candidates([automated, human])

    store.complete_logistics_attempt(
        automated.logistics_no,
        LogisticsDetail(
            logistics_no=automated.logistics_no,
            page_error="阿里物流详情页面异常：无数据",
        ),
        state=LOGISTICS_BLOCKED,
        last_error="阿里物流详情页面异常：无数据",
    )
    _force_legacy_program_block(store, automated.logistics_no)

    human_error = tracking_number_mismatch_reason("FedEx", "1Z9253126709651051")
    store.complete_logistics_attempt(
        human.logistics_no,
        LogisticsDetail(
            logistics_no=human.logistics_no,
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=human_error,
    )
    assert store.set_tracking_mismatch_review(
        human.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    changed = store.requeue_automated_logistics_blocks(run_id="startup-migration")

    assert changed == (automated.logistics_no,)
    restored = store.get_by_logistics_no(automated.logistics_no)
    locked = store.get_by_logistics_no(human.logistics_no)
    assert restored["logistics_state"] == LOGISTICS_RETRYABLE
    assert restored["logistics_next_attempt_at"]
    assert locked["logistics_state"] == LOGISTICS_BLOCKED
    assert locked["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE
    assert store.history(automated.logistics_no)[-1].event_type == (
        "AUTOMATED_BLOCK_REQUEUED"
    )


def test_repeat_scan_does_not_revoke_live_logistics_lease(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    claimed = store.claim_logistics_jobs("logistics-worker")[0]

    result = store.upsert_candidate(candidate, run_id="repeat-while-logistics-running")

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.immediate_logistics is False
    assert row["lease_owner"] == "logistics-worker"
    assert row["lease_stage"] == "logistics"
    assert row["version"] == claimed["version"]
    assert store.claim_logistics_jobs("second-logistics-worker") == []
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _ready_detail(candidate.logistics_no),
        state=LOGISTICS_READY,
        last_error=None,
        owner="logistics-worker",
        expected_version=claimed["version"],
    ) is True


def test_repeat_scan_fails_closed_when_owned_lease_has_no_expiry(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    claimed = store.claim_logistics_jobs("legacy-logistics-worker")[0]
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET lease_until = NULL WHERE id = ?",
            (claimed["job_id"],),
        )

    result = store.upsert_candidate(candidate, run_id="repeat-with-unknown-lease-expiry")

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.immediate_logistics is False
    assert row["lease_owner"] == "legacy-logistics-worker"
    assert row["version"] == claimed["version"]


def test_repeat_scan_does_not_revoke_live_erp_lease(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    claimed = store.claim_erp_jobs("erp-worker")[0]

    result = store.upsert_candidate(candidate, run_id="repeat-while-erp-running")

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.immediate_erp is False
    assert row["lease_owner"] == "erp-worker"
    assert row["lease_stage"] == "erp"
    assert row["version"] == claimed["version"]
    assert store.claim_erp_jobs("second-erp-worker") == []
    assert store.finish_erp_attempt(
        candidate.logistics_no,
        owner="erp-worker",
        state=ERP_RETRYABLE,
        last_error="temporary ERP failure",
        expected_version=claimed["version"],
    ) is True


def test_repeat_scan_reclaims_expired_erp_lease(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    claimed = store.claim_erp_jobs("expired-erp-worker", lease_seconds=-1)[0]

    result = store.upsert_candidate(candidate, run_id="repeat-after-lease-expired")

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.immediate_erp is True
    assert row["lease_owner"] is None
    assert row["version"] > claimed["version"]
    assert [item["logistics_no"] for item in store.claim_erp_jobs("replacement-worker")] == [
        candidate.logistics_no
    ]


def test_complete_tag_snapshot_pauses_job_revokes_lease_and_rejects_stale_worker(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    claimed = store.claim_logistics_jobs("worker-before-pause")[0]

    result = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
        run_id="scan-pause",
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.paused_count == 1
    assert result.paused_logistics_numbers == (candidate.logistics_no,)
    assert row["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
    assert row["identity_status_text"] == "标签已移除/自动暂停"
    assert row["lease_owner"] is None
    assert row["version"] > claimed["version"]
    assert store.claim_logistics_jobs("worker-after-pause") == []
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _ready_detail(candidate.logistics_no),
        state=LOGISTICS_READY,
        last_error=None,
        owner="worker-before-pause",
        expected_version=claimed["version"],
    ) is False
    assert store.history(candidate.logistics_no)[-1].event_type == "TAG_REMOVED_AUTO_PAUSE"
    assert store.list_attention()[0]["identity_status_text"] == "标签已移除/自动暂停"


def test_incomplete_or_unknown_tag_snapshot_never_changes_identity_state(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)

    incomplete = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=False,
    )
    unknown = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: None},
        snapshot_complete=True,
    )

    assert incomplete.snapshot_complete is False
    assert incomplete.paused_count == 0
    assert unknown.paused_count == 0
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_ACTIVE


def test_tag_restore_resumes_only_auto_paused_job_and_retries_immediately(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(logistics_no=candidate.logistics_no, status_text="待揽收"),
        state=LOGISTICS_WAITING,
        last_error="not ready",
    )
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )

    result = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: True},
        snapshot_complete=True,
        run_id="scan-resume",
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert result.resumed_count == 1
    assert result.immediate_logistics_count == 1
    assert result.immediate_erp_count == 0
    assert row["identity_state"] == IDENTITY_ACTIVE
    assert row["logistics_state"] == LOGISTICS_WAITING
    assert row["logistics_next_attempt_at"] is None
    assert [item["logistics_no"] for item in store.claim_logistics_jobs("restored-worker")] == [
        candidate.logistics_no
    ]
    assert store.history(candidate.logistics_no)[-1].event_type == "TAG_RESTORED_AUTO_RESUME"


def test_reseen_candidate_does_not_restore_tag_pause_until_complete_snapshot(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.finish_erp_attempt(
        candidate.logistics_no,
        owner=None,
        state=ERP_RETRYABLE,
        last_error="temporary ERP failure",
    )
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )

    result = store.upsert_candidate(candidate, run_id="candidate-restored")

    assert result.auto_resumed is False
    assert result.immediate_erp is False
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
    assert row["erp_next_attempt_at"] is not None

    reconciliation = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: True},
        snapshot_complete=True,
        run_id="complete-snapshot-restored",
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert reconciliation.resumed_count == 1
    assert reconciliation.immediate_erp_count == 1
    assert row["identity_state"] == IDENTITY_ACTIVE
    assert row["erp_next_attempt_at"] is None
    event_types = [event.event_type for event in store.history(candidate.logistics_no)]
    assert "TAG_RESTORED_AUTO_RESUME" in event_types


def test_complete_tag_reconciliation_restores_current_run_cancel_only(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    done = _candidate()
    cancelled = _candidate(
        logistics_no="ALS-CANCELLED",
        system_order_no="SYS-CANCELLED",
        platform_order_no="ORDER-CANCELLED",
    )
    conflict = _candidate(
        logistics_no="ALS-CONFLICT",
        system_order_no="SYS-CONFLICT",
        platform_order_no="ORDER-CONFLICT",
    )
    store.upsert_candidate(done)
    _make_ready(store, done.logistics_no)
    store.mark_erp_outbounded(done.logistics_no)
    store.upsert_candidate(cancelled)
    store.cancel(cancelled.logistics_no, "manual cancellation")
    store.upsert_candidate(conflict)
    store.upsert_candidate(
        _candidate(
            logistics_no=conflict.logistics_no,
            system_order_no="SYS-OTHER",
            platform_order_no="ORDER-OTHER",
        )
    )

    result = store.reconcile_shipment_tag_snapshot(
        {
            done.system_order_no: False,
            cancelled.system_order_no: True,
            conflict.system_order_no: True,
        },
        snapshot_complete=True,
    )

    assert result.paused_count == 0
    assert result.resumed_count == 1
    assert store.get_by_logistics_no(done.logistics_no)["identity_state"] == IDENTITY_ACTIVE
    assert store.get_by_logistics_no(cancelled.logistics_no)["identity_state"] == IDENTITY_ACTIVE
    assert store.get_by_logistics_no(conflict.logistics_no)["identity_state"] == IDENTITY_CONFLICT
    assert store.history(cancelled.logistics_no)[-1].event_type == "JOB_AUTO_RESTORED_ON_RESCAN"


def test_current_run_cancel_restores_only_after_complete_tagged_rescan(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )

    assert store.restore_cancelled(candidate.logistics_no, reason="must not bypass tag") is False
    assert store.cancel(candidate.logistics_no, "operator cancelled paused job") is True
    assert store.cancel(candidate.logistics_no, "duplicate cancellation") is False
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_CANCELLED
    assert store.restore_cancelled(candidate.logistics_no, reason="tag is still absent") is False
    incomplete = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: True},
        snapshot_complete=False,
    )
    assert incomplete.resumed_count == 0
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_CANCELLED
    resumed = store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: True},
        snapshot_complete=True,
    )
    assert resumed.resumed_count == 1
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_ACTIVE
    assert store.history(candidate.logistics_no)[-1].event_type == "JOB_AUTO_RESTORED_ON_RESCAN"
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
    assert store.restore_cancelled(candidate.logistics_no, reason="tag was removed again") is False
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: True},
        snapshot_complete=True,
    )
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_ACTIVE


def test_manual_add_does_not_restore_job_while_shipment_tag_is_absent(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )

    result = store.add_manual_candidate(
        system_order_no=candidate.system_order_no,
        platform_order_no=candidate.platform_order_no,
        logistics_no=candidate.logistics_no,
        reason="operator refreshed source data",
    )

    assert result.auto_resumed is False
    assert store.get_by_logistics_no(candidate.logistics_no)["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED


def test_logistics_and_erp_errors_are_isolated(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store, "ALS01781406025")

    assert store.finish_erp_attempt(
        "ALS01781406025",
        owner=None,
        state=ERP_RETRYABLE,
        last_error="ERP row not found",
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["service_line"] == "UPS-Saver"
    assert row["erp_state"] == ERP_RETRYABLE
    assert store.list_logistics_check_candidates() == []
    candidates = store.list_erp_mark_candidates()
    assert [item.logistics_no for item in candidates] == ["ALS01781406025"]
    assert candidates[0].service_line == "UPS-Saver"


def test_claim_lease_prevents_two_workers_from_claiming_same_job(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())

    first = store.claim_logistics_jobs("worker-1")
    second = store.claim_logistics_jobs("worker-2")

    assert [row["logistics_no"] for row in first] == ["ALS01781406025"]
    assert second == []


def test_erp_candidate_and_claim_filter_select_exact_logistics_no(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate(logistics_no="ALS-FIRST", platform_order_no="ORDER-FIRST")
    second = _candidate(
        logistics_no="ALS-SECOND",
        system_order_no="103710434633847502",
        platform_order_no="ORDER-SECOND",
    )
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    _make_ready(store, second.logistics_no)

    listed = store.list_erp_mark_candidates(limit=1, logistics_no=second.logistics_no)
    claimed = store.claimed_erp_items(
        "selected-worker",
        limit=1,
        logistics_no=second.logistics_no,
    )

    assert [item.logistics_no for item in listed] == [second.logistics_no]
    assert [item.logistics_no for item in claimed] == [second.logistics_no]
    assert store.get_by_logistics_no(first.logistics_no)["lease_owner"] is None
    assert store.get_by_logistics_no(second.logistics_no)["lease_owner"] == "selected-worker"


def test_erp_write_intent_is_durably_audited_under_the_active_lease(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.claimed_erp_items("erp-worker")
    details = {
        "attempt_id": "attempt-review-1",
        "operation": "review_orders",
        "payload_hash": "a" * 64,
        "system_order_no": candidate.system_order_no,
        "baseline_active_wms_rows": 0,
    }

    assert store.record_erp_write_audit(
        candidate.logistics_no,
        owner="erp-worker",
        event_type="ERP_WRITE_INTENT_RECORDED",
        details=details,
        run_id="review-run",
    )
    assert not store.record_erp_write_audit(
        candidate.logistics_no,
        owner="other-worker",
        event_type="ERP_WRITE_RESULT_AMBIGUOUS",
        details=details,
    )

    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == "ERP_WRITE_INTENT_RECORDED"
    assert event.details == details
    assert event.run_id == "review-run"
    assert store.get_pending_erp_review_intent(candidate.logistics_no) == details

    assert store.record_erp_write_audit(
        candidate.logistics_no,
        owner="erp-worker",
        event_type="ERP_WRITE_ACKNOWLEDGED",
        details=details,
    )
    assert store.get_pending_erp_review_intent(candidate.logistics_no) == details

    assert store.record_erp_write_audit(
        candidate.logistics_no,
        owner="erp-worker",
        event_type="ERP_WRITE_REJECTED",
        details=details,
    )
    assert store.get_pending_erp_review_intent(candidate.logistics_no) is None


def test_wms_outbound_selection_is_persisted_and_cannot_change_after_checkpoint(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    claimed = store.claimed_erp_items("erp-worker")[0]
    candidates = [
        {
            "wo_number": "WO-A",
            "order_number": candidate.system_order_no,
            "platform_order_no": [candidate.platform_order_no],
            "status": "pending",
        },
        {
            "wo_number": "WO-B",
            "order_number": candidate.system_order_no,
            "platform_order_no": [candidate.platform_order_no],
            "status": "pending",
        },
    ]

    assert store.record_wms_outbound_selection_required(
        candidate.logistics_no,
        owner="erp-worker",
        expected_version=claimed.version,
        candidates=candidates,
    )
    assert store.get_by_logistics_no(candidate.logistics_no)[
        "wms_selection_required"
    ] == 1

    selected_version = store.record_wms_outbound_selection(
        candidate.logistics_no,
        owner="erp-worker",
        expected_version=claimed.version,
        selected_wo_number="WO-B",
        candidates=candidates,
        actor="operator",
    )
    assert selected_version is not None
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["selected_wms_wo_number"] == "WO-B"
    assert row["selected_wms_selected_by"] == "operator"
    assert row["selected_wms_candidates_hash"]
    assert row["wms_selection_required"] == 0

    checkpoint_version = store.record_erp_checkpoint(
        candidate.logistics_no,
        owner="erp-worker",
        expected_version=selected_version,
        checkpoint=ERP_CHECKPOINT_CHANNEL_SET,
    )
    assert checkpoint_version is not None
    with pytest.raises(ValueError, match="禁止更换销售出库单"):
        store.record_wms_outbound_selection(
            candidate.logistics_no,
            owner="erp-worker",
            expected_version=checkpoint_version,
            selected_wo_number="WO-A",
            candidates=candidates,
            actor="operator",
        )


def test_legacy_multiple_outbound_event_remains_selectable_after_error_is_overwritten(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)

    first = store.claimed_erp_items("first-worker")[0]
    assert store.finish_erp_attempt(
        candidate.logistics_no,
        owner="first-worker",
        state=ERP_BLOCKED,
        last_error="同一系统单号对应多个销售出库单，禁止猜测要修改哪一条。",
        expected_version=first.version,
    )
    assert store.retry_stage(candidate.logistics_no, "erp", reason="retry selection")
    second = store.claimed_erp_items("second-worker")[0]
    assert store.finish_erp_attempt(
        candidate.logistics_no,
        owner="second-worker",
        state=ERP_BLOCKED,
        last_error="写入前无法检查既有销售出库单：用户未选择销售出库单。",
        expected_version=second.version,
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["selected_wms_wo_number"] is None
    assert row["wms_selection_required"] == 1


def test_version_guard_rejects_stale_logistics_completion(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    claimed = store.claim_logistics_jobs("worker-1")[0]

    assert store.complete_logistics_attempt(
        claimed["logistics_no"],
        _ready_detail(claimed["logistics_no"]),
        state=LOGISTICS_READY,
        last_error=None,
        owner="worker-1",
        expected_version=claimed["version"] - 1,
    ) is False
    assert store.get_by_logistics_no(claimed["logistics_no"])["logistics_state"] == LOGISTICS_PENDING


def _make_invalid_fedex_ready(store: ShipmentWorkflowStore, logistics_no: str) -> None:
    assert store.complete_logistics_attempt(
        logistics_no,
        LogisticsDetail(
            logistics_no=logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )


@pytest.mark.parametrize(
    ("checkpoint", "expected_checkpoint"),
    [
        (ERP_CHECKPOINT_AUDITED, ERP_CHECKPOINT_AUDITED),
        (ERP_CHECKPOINT_LOGISTICS_SAVED, ERP_CHECKPOINT_AUDITED),
    ],
)
def test_invalid_historical_ready_tracking_is_retryable_and_rolls_back_safely(
    tmp_path, checkpoint, expected_checkpoint
):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = 'RUNNING', checkpoint = ?, logistics_payload_hash = 'bad-hash',
                logistics_confirmed_at = '2026-07-13T00:00:00Z',
                logistics_saved_at = '2026-07-13T00:01:00Z',
                freight_amount = '123.45', chargeable_weight_g = '4500'
            """,
            (checkpoint,),
        )

    retryable = store.block_invalid_tracking_records(run_id="erp-run-1")

    assert [item["logistics_no"] for item in retryable] == [candidate.logistics_no]
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["erp_state"] == "WAITING"
    assert row["erp_checkpoint"] == expected_checkpoint
    if checkpoint == ERP_CHECKPOINT_LOGISTICS_SAVED:
        assert row["logistics_payload_hash"] is None
        assert row["logistics_saved_at"] is None
        assert row["freight_amount"] is None
        assert row["chargeable_weight_g"] is None
    assert store.history(candidate.logistics_no)[-1].event_type == "TRACKING_NUMBER_RETRYABLE"


def test_manual_tracking_confirmation_is_exact_pair_scoped_and_clears_after_change(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    assert store.confirm_tracking_override(candidate.logistics_no)
    confirmed = store.get_by_logistics_no(candidate.logistics_no)
    assert confirmed["logistics_state"] == LOGISTICS_READY
    assert confirmed["erp_state"] == ERP_PENDING
    assert store.list_erp_mark_candidates()[0].tracking_manually_verified is True
    assert store.history(candidate.logistics_no)[-1].event_type == "TRACKING_NUMBER_MANUALLY_CONFIRMED"

    changed_detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="1Z9253126709651099",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        changed_detail,
        state=LOGISTICS_BLOCKED,
        last_error="国际物流单号与承运商不匹配",
    )
    changed = store.get_by_logistics_no(candidate.logistics_no)
    assert changed["tracking_override_at"] is None
    assert changed["tracking_override_no"] is None


def test_manual_tracking_pair_can_correct_carrier_and_make_exact_pair_ready(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    assert store.confirm_tracking_pair(
        candidate.logistics_no,
        carrier="USPS",
        tracking_no="9400100000000000000000",
        reason="人工核对 USPS 官网轨迹",
    )

    confirmed = store.get_by_logistics_no(candidate.logistics_no)
    assert confirmed["carrier"] == "USPS"
    assert confirmed["international_tracking_no"] == "9400100000000000000000"
    assert confirmed["logistics_state"] == LOGISTICS_READY
    assert confirmed["erp_state"] == ERP_PENDING
    assert store.list_erp_mark_candidates()[0].tracking_manually_verified is True
    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == "TRACKING_PAIR_MANUALLY_CONFIRMED"
    assert event.details["old_pair"]["carrier"] == "FedEx"


def test_amazon_main_image_forbidden_channel_blocks_until_valid_manual_pair(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.sales_platform_code = "10001"
    candidate.sales_platform_name = "Amazon"
    candidate.has_main_image = True
    store.upsert_candidate(candidate)

    detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="SpeedX",
        international_tracking_no="SPX123456789012",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        detail,
        state=LOGISTICS_READY,
        last_error=None,
    )

    blocked = store.get_by_logistics_no(candidate.logistics_no)
    assert blocked["logistics_state"] == LOGISTICS_READY
    assert blocked["erp_state"] == ERP_BLOCKED
    assert blocked["policy_block_code"] == "amazon_main_image_forbidden_channel"
    assert "校验通过后才可执行出库" in blocked["erp_last_error"]
    assert store.list_ready_to_mark() == []
    assert not store.retry_stage(candidate.logistics_no, "erp")
    assert not store.confirm_tracking_override(candidate.logistics_no)

    assert store.retry_stage(candidate.logistics_no, "logistics")
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="USPS",
            international_tracking_no="9400100000000000000000",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    still_blocked = store.get_by_logistics_no(candidate.logistics_no)
    assert still_blocked["erp_state"] == ERP_BLOCKED
    assert still_blocked["policy_block_code"] == "amazon_main_image_forbidden_channel"
    assert store.list_ready_to_mark() == []

    with pytest.raises(ValueError, match="仍未解除限制"):
        store.confirm_tracking_pair(
            candidate.logistics_no,
            carrier="SpeedX",
            tracking_no="SPX123456789012",
            reason="人工核对后仍选择禁用渠道",
        )
    with pytest.raises(ValueError, match="不匹配"):
        store.confirm_tracking_pair(
            candidate.logistics_no,
            carrier="USPS",
            tracking_no="SPX123456789012",
            reason="错误组合不得放行",
        )

    assert store.confirm_tracking_pair(
        candidate.logistics_no,
        carrier="USPS",
        tracking_no="9400100000000000000000",
        reason="人工向物流客服核实并改为正确 USPS 单号",
    )
    corrected = store.get_by_logistics_no(candidate.logistics_no)
    assert corrected["erp_state"] == ERP_PENDING
    assert corrected["policy_block_code"] is None
    assert corrected["carrier"] == "USPS"
    assert corrected["international_tracking_no"] == "9400100000000000000000"
    assert [item.logistics_no for item in store.list_ready_to_mark()] == [
        candidate.logistics_no
    ]


@pytest.mark.parametrize(
    ("sales_platform_code", "has_main_image"),
    (("10002", True), ("10001", False)),
)
def test_forbidden_carrier_does_not_apply_without_both_amazon_and_main_image(
    tmp_path,
    sales_platform_code,
    has_main_image,
):
    store = ShipmentWorkflowStore(tmp_path / f"queue-{sales_platform_code}-{has_main_image}.sqlite3")
    candidate = _candidate(platform_order_no="wc39715" if sales_platform_code != "10001" else "112-1165824-9982644")
    candidate.sales_platform_code = sales_platform_code
    candidate.has_main_image = has_main_image
    store.upsert_candidate(candidate)
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="SpeedX",
            international_tracking_no="SPX123456789012",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    ready = store.get_by_logistics_no(candidate.logistics_no)
    assert ready["erp_state"] == ERP_PENDING
    assert ready["policy_block_code"] is None


def test_erp_claim_backfills_historical_main_image_and_blocks_before_lease(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = _candidate()
    candidate.sales_platform_code = "10001"
    store.upsert_candidate(candidate)
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="SpeedX",
            international_tracking_no="SPX123456789012",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    assert store.get_by_logistics_no(candidate.logistics_no)["erp_state"] == ERP_PENDING

    now = utc_now()
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO shipment_order_product_snapshots (
                platform_order_no, system_order_no, item_key, has_main_image,
                active, first_seen_at, last_seen_at, updated_at
            ) VALUES (?, ?, ?, 1, 1, ?, ?, ?)
            """,
            (
                candidate.platform_order_no,
                candidate.system_order_no,
                "historical-main-image",
                now,
                now,
                now,
            ),
        )

    assert store.claim_erp_jobs("erp-worker") == []
    blocked = store.get_by_logistics_no(candidate.logistics_no)
    assert blocked["has_main_image"] == 1
    assert blocked["erp_state"] == ERP_BLOCKED
    assert blocked["lease_owner"] is None


def test_tracking_mismatch_retries_automatically_and_remains_available_for_review(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["tracking_mismatch_action"] is None
    assert row["logistics_next_attempt_at"]
    assert store.list_logistics_check_candidates() == []
    assert [item["logistics_no"] for item in store.list_pending_tracking_mismatch_reviews()] == [
        candidate.logistics_no
    ]


@pytest.mark.parametrize(
    ("carrier", "tracking_no", "normalized_carrier"),
    [
        ("YWE", "YWNJC010158019848", "YANWEN"),
        ("USPS", "420630849235990416420600935898", "USPS"),
    ],
)
def test_newly_supported_tracking_family_is_requeued_for_fresh_page_confirmation(
    tmp_path,
    carrier,
    tracking_no,
    normalized_carrier,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier=carrier,
            international_tracking_no=tracking_no,
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason(carrier, tracking_no),
    )
    _force_legacy_program_block(store, candidate.logistics_no)

    changed = store.requeue_tracking_mismatches_resolved_by_current_rules(
        run_id="tracking-rule-upgrade",
    )

    assert changed == (candidate.logistics_no,)
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["logistics_last_error"] is None
    assert row["logistics_next_attempt_at"]
    assert [item["logistics_no"] for item in store.list_logistics_check_candidates()] == [
        candidate.logistics_no
    ]
    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == "TRACKING_RULE_MATCH_REQUEUED"
    assert event.run_id == "tracking-rule-upgrade"
    assert event.details == {
        "carrier": normalized_carrier,
        "tracking_no": tracking_no,
    }
    assert store.requeue_tracking_mismatches_resolved_by_current_rules() == ()


def test_explicit_order_issue_is_not_overridden_by_tracking_rule_upgrade(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    carrier = "YWE"
    tracking_no = "YWNJC010158019848"
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier=carrier,
            international_tracking_no=tracking_no,
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason(carrier, tracking_no),
    )
    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    assert store.requeue_tracking_mismatches_resolved_by_current_rules() == ()
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_BLOCKED
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE


@pytest.mark.parametrize("tracking_no", ["ALS01781406025", "JYCP00000093286", "订单异常联系人"])
def test_obvious_legacy_parser_artifact_is_requeued_once(tmp_path, tracking_no):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no=tracking_no,
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        detail,
        state=LOGISTICS_BLOCKED,
        last_error=(
            f"国际物流单号与承运商不匹配：FEDEX / {tracking_no}，"
            "请审核后选择处理方式。"
        ),
    )
    _force_legacy_program_block(store, candidate.logistics_no)
    if tracking_no.startswith("JYCP"):
        assert store.list_pending_tracking_mismatch_reviews() == []

    changed = store.requeue_obvious_tracking_parser_artifacts(run_id="repair-1")

    assert changed == (candidate.logistics_no,)
    row = store.get_by_logistics_no(candidate.logistics_no)
    expected_state = LOGISTICS_WAITING if tracking_no.startswith("JYCP") else LOGISTICS_RETRYABLE
    assert row["logistics_state"] == expected_state
    assert row["international_tracking_no"] is None
    assert row["logistics_next_attempt_at"]
    assert [item["logistics_no"] for item in store.list_logistics_check_candidates()] == [
        candidate.logistics_no
    ]
    event = store.history(candidate.logistics_no)[-1]
    assert event.event_type == (
        "LOGISTICS_INTERMEDIARY_TRACKING_REQUEUED"
        if tracking_no.startswith("JYCP")
        else "LOGISTICS_PARSER_ARTIFACT_REQUEUED"
    )
    assert event.run_id == "repair-1"
    assert event.details["artifact_class"] in {"placeholder", "intermediary", "ui_text"}
    assert store.requeue_obvious_tracking_parser_artifacts(run_id="repair-2") == ()
    assert [
        item.event_type
        for item in store.history(candidate.logistics_no)
    ].count(event.event_type) == 1


def test_jycp_auto_recheck_choice_is_migrated_to_waiting_without_manual_review(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    tracking_no = "JYCP00000093286"
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no=tracking_no,
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason("FedEx", tracking_no),
    )
    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_AUTO_RECHECK,
    )
    _force_legacy_program_block(store, candidate.logistics_no)

    assert store.requeue_obvious_tracking_parser_artifacts(run_id="repair-auto") == (
        candidate.logistics_no,
    )
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_WAITING
    assert row["international_tracking_no"] is None
    assert row["tracking_mismatch_action"] is None
    assert row["logistics_last_error"] == (
        "阿里页面的国际物流单号仍为 JYCP00000093286，等待真实尾程单号。"
    )


def test_jycp_explicit_order_issue_choice_is_not_automatically_migrated(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    tracking_no = "JYCP00000093286"
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no=tracking_no,
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason("FedEx", tracking_no),
    )
    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    assert store.requeue_obvious_tracking_parser_artifacts(run_id="repair-manual") == ()
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_BLOCKED
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE


def test_real_mismatch_and_manually_reviewed_rows_are_not_parser_repaired(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    mismatch = _candidate("ALS01781406025", "SYS-MISMATCH", "111-MISMATCH")
    reviewed = _candidate("ALS01781406026", "SYS-REVIEWED", "112-REVIEWED")
    store.insert_candidates([mismatch, reviewed])
    for candidate in (mismatch, reviewed):
        detail = LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        )
        assert store.complete_logistics_attempt(
            candidate.logistics_no,
            detail,
            state=LOGISTICS_BLOCKED,
            last_error=(
                "国际物流单号与承运商不匹配：FEDEX / 1Z9253126709651051，"
                "请审核后选择处理方式。"
            ),
        )
    assert store.set_tracking_mismatch_review(
        reviewed.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    assert store.requeue_obvious_tracking_parser_artifacts() == ()
    assert store.get_by_logistics_no(mismatch.logistics_no)["logistics_state"] == LOGISTICS_RETRYABLE
    assert store.get_by_logistics_no(reviewed.logistics_no)["tracking_mismatch_action"] == (
        TRACKING_REVIEW_ORDER_ISSUE
    )


def test_auto_recheck_review_retries_mismatch_until_valid_tracking_arrives(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_AUTO_RECHECK,
    )
    reviewed = store.get_by_logistics_no(candidate.logistics_no)
    assert reviewed["tracking_mismatch_action"] == TRACKING_REVIEW_AUTO_RECHECK
    assert reviewed["tracking_mismatch_reviewed_at"]
    assert [item["logistics_no"] for item in store.list_logistics_check_candidates()] == [
        candidate.logistics_no
    ]

    mismatch_detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="1Z9253126709651051",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        mismatch_detail,
        state=LOGISTICS_BLOCKED,
        last_error="国际物流单号与承运商不匹配：FEDEX / 1Z9253126709651051，请审核后选择处理方式。",
    )
    waiting = store.get_by_logistics_no(candidate.logistics_no)
    assert waiting["tracking_mismatch_action"] == TRACKING_REVIEW_AUTO_RECHECK
    assert waiting["logistics_next_attempt_at"]
    assert store.list_logistics_check_candidates() == []
    assert store.list_pending_tracking_mismatch_reviews() == []

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE shipment_logistics SET next_attempt_at = '2000-01-01T00:00:00Z'"
        )
    valid_detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="874084304695",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        valid_detail,
        state=LOGISTICS_READY,
        last_error=None,
    )
    ready = store.get_by_logistics_no(candidate.logistics_no)
    assert ready["logistics_state"] == LOGISTICS_READY
    assert ready["erp_state"] == ERP_PENDING
    assert ready["tracking_mismatch_action"] is None
    assert ready["tracking_mismatch_reviewed_at"] is None


def test_order_issue_review_stays_blocked_without_automatic_query(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    assert store.set_tracking_mismatch_review(
        candidate.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE
    assert row["logistics_next_attempt_at"] is None
    assert store.list_logistics_check_candidates() == []
    assert store.claim_logistics_jobs("worker-1") == []
    assert store.list_pending_tracking_mismatch_reviews() == []


def test_auto_recheck_keeps_retrying_after_a_different_program_error(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()
    store.set_tracking_mismatch_review(candidate.logistics_no, TRACKING_REVIEW_AUTO_RECHECK)

    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(logistics_no=candidate.logistics_no, page_error="物流详情无权限"),
        state=LOGISTICS_BLOCKED,
        last_error="物流详情无权限",
    )

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_AUTO_RECHECK
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["logistics_next_attempt_at"]
    assert store.list_logistics_check_candidates() == []


def test_invalid_tracking_does_not_roll_back_completed_erp_job(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)

    assert store.block_invalid_tracking_records() == []
    assert store.get_by_logistics_no(candidate.logistics_no)["erp_state"] == ERP_DONE


def test_invalid_tracking_safety_scan_revokes_an_existing_erp_lease(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    claimed = store.claim_erp_jobs("old-worker")
    assert claimed[0]["lease_owner"] == "old-worker"

    retryable = store.block_invalid_tracking_records(run_id="new-worker-preflight")

    assert [item["logistics_no"] for item in retryable] == [candidate.logistics_no]
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["lease_owner"] is None


def test_v1_migration_splits_stage_states_and_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    _create_v1_database(
        path,
        [
            {"logistics_no": "ALS01781406025", "status": "NOT_READY"},
            {
                "logistics_no": "ALS01789020252",
                "status": "ERROR",
                "carrier": "UPS",
                "tracking": "1Z9253126709651051",
                "amount": "CNY 123.45",
                "weight": "4.500",
                "error": "上一轮 ERP 标发失败",
            },
            {
                "logistics_no": "ALS01789020253",
                "status": "MANUAL_REVIEW",
                "error": "Page.wait_for_timeout: Target page, context or browser has been closed",
            },
            {
                "logistics_no": "ALS01789020254",
                "status": "ERP_MARKED",
                "carrier": "UPS",
                "tracking": "1Z204E380338943508",
                "amount": "CNY 99.00",
                "weight": "3.000",
                "processed_at": "2026-07-09 18:00:00",
            },
        ],
    )

    store = ShipmentWorkflowStore(path)
    store.initialize()

    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_WAITING
    erp_error = store.get_by_logistics_no("ALS01789020252")
    assert erp_error["logistics_state"] == LOGISTICS_READY
    assert erp_error["erp_state"] == ERP_RETRYABLE
    browser_error = store.get_by_logistics_no("ALS01789020253")
    assert browser_error["logistics_state"] == LOGISTICS_RETRYABLE
    marked = store.get_by_logistics_no("ALS01789020254")
    assert marked["erp_state"] == ERP_DONE
    assert marked["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert list(tmp_path.glob("shipment_queue.pre_v2_*.sqlite3"))
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "shipment_queue_v1" in table_names
        assert "shipment_jobs" in table_names
        new_columns = {
            row[1]
            for table in ("shipment_jobs", "shipment_logistics", "shipment_erp")
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        assert "als_no" not in new_columns
        assert "sales_channel" in new_columns
        assert "customer_email_required" in new_columns
        assert "tracking_mismatch_action" in new_columns
        assert "tracking_mismatch_reviewed_at" in new_columns


def test_v5_database_migrates_tracking_review_fields_and_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE shipment_logistics DROP COLUMN tracking_mismatch_action")
        conn.execute("ALTER TABLE shipment_logistics DROP COLUMN tracking_mismatch_reviewed_at")
        conn.execute("PRAGMA user_version = 5")

    migrated = ShipmentWorkflowStore(path)
    migrated.initialize()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shipment_logistics)")}
        assert "tracking_mismatch_action" in columns
        assert "tracking_mismatch_reviewed_at" in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v6_*.sqlite3"))


def test_v6_database_migrates_product_type_and_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN product_type")
        conn.execute("PRAGMA user_version = 6")

    migrated = ShipmentWorkflowStore(path)
    migrated.initialize()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")}
        assert "product_type" in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v7_*.sqlite3"))


def test_product_type_is_persisted_and_batch_cancel_is_atomic(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate("ALS01781406025", "SYS-1", "111-AAA")
    second = _candidate("ALS01781406026", "SYS-2", "112-BBB")
    second.product_type = "x_stands"
    store.insert_candidates([first, second])

    assert store.get_by_logistics_no(first.logistics_no)["product_type"] == "tent"
    assert store.get_by_logistics_no(second.logistics_no)["product_type"] == "x_stands"
    assert store.cancel_many(
        [first.logistics_no, second.logistics_no, first.logistics_no],
        "批量取消测试",
    ) == 2
    assert store.get_by_logistics_no(first.logistics_no)["identity_state"] == IDENTITY_CANCELLED
    assert store.get_by_logistics_no(second.logistics_no)["identity_state"] == IDENTITY_CANCELLED
    assert store.history(first.logistics_no)[-1].details["source"] == "desktop_batch"


def test_customer_shipping_service_is_persisted_and_due_notice_is_not_an_error(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.customer_shipping_service = "Expedited Shipping"
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = '2020-01-01T00:00:00Z' WHERE logistics_no = ?",
            (candidate.logistics_no,),
        )
        conn.commit()

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["customer_shipping_service"] == "expedited"
    assert row["shipping_attention_notice"]
    assert row["logistics_overdue_at"] == "2020-01-02T09:30:00Z"
    assert row["last_error"] is None
    assert [item["logistics_no"] for item in store.list_attention()] == [
        candidate.logistics_no
    ]

    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _ready_detail(candidate.logistics_no),
        state=LOGISTICS_WAITING,
        last_error=None,
    )
    resolved = store.get_by_logistics_no(candidate.logistics_no)
    assert resolved["shipping_attention_notice"] is None
    assert resolved["logistics_overdue_at"] == "2020-01-02T09:30:00Z"
    assert store.list_attention() == []


def test_overdue_history_is_captured_before_logistics_resolution_without_ui_refresh(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.customer_shipping_service = "Expedited"
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = ? WHERE logistics_no = ?",
            ("2020-01-01T00:00:00Z", candidate.logistics_no),
        )
        conn.commit()

    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _ready_detail(candidate.logistics_no),
        state=LOGISTICS_READY,
        last_error=None,
    )

    resolved = store.get_by_logistics_no(candidate.logistics_no)
    assert resolved["logistics_state"] == LOGISTICS_READY
    assert resolved["shipping_attention_notice"] is None
    assert resolved["logistics_overdue_at"] == "2020-01-02T09:30:00Z"


def test_overdue_history_reconciliation_uses_inclusive_1730_boundary(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.customer_shipping_service = "Expedited"
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = ? WHERE logistics_no = ?",
            ("2026-08-20T16:00:00Z", candidate.logistics_no),
        )
        conn.commit()

    def stored_overdue_at():
        with store.connect() as conn:
            row = conn.execute(
                "SELECT logistics_overdue_at FROM shipment_jobs WHERE logistics_no = ?",
                (candidate.logistics_no,),
            ).fetchone()
        return row["logistics_overdue_at"]

    assert store.reconcile_logistics_overdue_history(
        now=datetime(2026, 8, 22, 9, 29, 59, tzinfo=timezone.utc)
    ) == 0
    assert stored_overdue_at() is None
    assert store.reconcile_logistics_overdue_history(
        now=datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)
    ) == 1
    assert stored_overdue_at() == "2026-08-22T09:30:00Z"


def test_customer_shipping_service_scan_issue_is_visible_without_als_and_resolves(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    identity = {
        "system_order_no": "103000000000009901",
        "platform_order_no": "112-0000000-0009901",
        "shipment_tag_name": "自动标发",
        "tag_text": "自动标发",
        "source_status_text": "待审核",
    }

    created = store.reconcile_customer_shipping_service_scan_issues(
        [
            {
                **identity,
                "error_message": "领星订单列表未返回客选物流字段。",
            }
        ],
        snapshot_complete=True,
        run_id="scan-service-error-001",
    )

    assert created == {
        "observed_count": 1,
        "created_count": 1,
        "refreshed_count": 0,
        "resolved_count": 0,
    }
    rows = store.list_all_jobs()
    assert len(rows) == 1
    assert rows[0]["system_order_no"] == identity["system_order_no"]
    assert rows[0]["platform_order_no"] == identity["platform_order_no"]
    assert rows[0]["logistics_no"] == ""
    assert rows[0]["identity_state"] == "SCAN_ERROR"
    assert rows[0]["scan_issue_code"] == (
        "customer_shipping_service_unavailable"
    )
    assert rows[0]["last_error"] == "领星订单列表未返回客选物流字段。"

    refreshed = store.reconcile_customer_shipping_service_scan_issues(
        [
            {
                **identity,
                "error_message": "领星订单列表未返回客选物流字段。",
            }
        ],
        snapshot_complete=True,
    )
    assert refreshed["created_count"] == 0
    assert refreshed["refreshed_count"] == 1
    assert len(store.list_all_jobs()) == 1

    resolved = store.reconcile_customer_shipping_service_scan_issues(
        [{**identity, "error_message": ""}],
        snapshot_complete=True,
    )
    assert resolved["resolved_count"] == 1
    assert store.list_active_scan_issues() == []
    assert store.list_all_jobs() == []


def test_scan_issue_manual_state_is_audited_and_blocks_candidate_until_restored(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    identity = {
        "system_order_no": "103000000000009972",
        "platform_order_no": "39972",
        "shipment_tag_name": "自动标发",
        "tag_text": "自动标发",
        "source_status_text": "待审核",
    }
    store.reconcile_customer_shipping_service_scan_issues(
        [{**identity, "error_message": "订单列表未返回可识别的客选物流。"}],
        snapshot_complete=True,
        run_id="scan-39972",
    )
    issue = store.list_active_scan_issues()[0]
    issue_key = issue["scan_issue_key"]

    changed = store.change_scan_issue_statuses(
        [issue_key],
        "manual_review",
        reason="等待业务人员确认客选物流",
        run_id="manual-review-39972",
    )
    assert changed.changed_logistics_nos == (issue_key,)
    managed = store.list_active_scan_issues()[0]
    assert managed["scan_issue_state"] == "MANUAL_REVIEW"
    assert managed["scan_issue_reason"] == "等待业务人员确认客选物流"

    # Even after the source field is repaired, an explicit human state is
    # retained and prevents the next scan from silently creating a queue job.
    store.reconcile_customer_shipping_service_scan_issues(
        [{**identity, "error_message": ""}],
        snapshot_complete=True,
    )
    candidate = _candidate(
        logistics_no="ALS039972",
        system_order_no=identity["system_order_no"],
        platform_order_no=identity["platform_order_no"],
    )
    blocked = store.upsert_candidate(candidate, run_id="candidate-39972")
    assert not blocked.inserted
    assert blocked.existing["identity_state"] == "MANUAL_REVIEW"
    assert store.get_by_logistics_no(candidate.logistics_no) is None
    assert store.list_active_scan_issues()[0]["scan_issue_state"] == "MANUAL_REVIEW"

    restored = store.change_scan_issue_statuses(
        [issue_key],
        "restore_scan_issue",
        reason="客选物流已修复，恢复自动处理",
        run_id="restore-39972",
    )
    assert restored.changed_logistics_nos == (issue_key,)
    assert store.list_active_scan_issues() == []
    assert store.upsert_candidate(candidate, run_id="candidate-restored-39972").inserted

    events = store.list_scan_issue_events(issue_key)
    assert [event["action"] for event in events] == [
        "manual_review",
        "candidate_blocked_by_manual_state",
        "restore_scan_issue",
    ]
    assert events[0]["reason"] == "等待业务人员确认客选物流"
    assert events[-1]["new_state"] == "ACTIVE"


def test_scan_issue_blocks_existing_queue_claim_until_source_and_manual_state_clear(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(
        logistics_no="ALS-EXISTING-39972",
        system_order_no="103000000000019972",
        platform_order_no="39972",
    )
    assert store.upsert_candidate(candidate).inserted
    identity = {
        "system_order_no": candidate.system_order_no,
        "platform_order_no": candidate.platform_order_no,
        "shipment_tag_name": "自动标发",
        "tag_text": "自动标发",
        "source_status_text": "待审核",
    }
    store.reconcile_customer_shipping_service_scan_issues(
        [{**identity, "error_message": "订单列表未返回可识别的客选物流。"}],
        snapshot_complete=True,
    )
    assert store.claim_logistics_jobs("worker-before-fix") == []

    issue_key = store.list_active_scan_issues()[0]["scan_issue_key"]
    store.change_scan_issue_statuses(
        [issue_key],
        "manual_review",
        reason="继续阻止自动处理",
    )
    store.reconcile_customer_shipping_service_scan_issues(
        [{**identity, "error_message": ""}],
        snapshot_complete=True,
    )
    assert store.claim_logistics_jobs("worker-after-source-fix") == []

    store.change_scan_issue_statuses(
        [issue_key],
        "restore_scan_issue",
        reason="解除人工阻止",
    )
    claimed = store.claim_logistics_jobs("worker-after-restore")
    assert [row["logistics_no"] for row in claimed] == [candidate.logistics_no]


def test_v21_scan_issue_management_migration_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    store.initialize()
    with store.connect() as conn:
        conn.execute("PRAGMA user_version = 20")
        conn.commit()

    upgraded = ShipmentWorkflowStore(path)
    upgraded.initialize()

    with upgraded.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_scan_issues)")
        }
        assert {
            "management_state",
            "management_reason",
            "management_updated_at",
        }.issubset(columns)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'shipment_scan_issue_events'"
        ).fetchone()
    assert list(tmp_path.glob("shipment_queue.pre_v21_*.sqlite3"))


def test_v22_amazon_main_image_policy_migration_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    store.initialize()
    with store.connect() as conn:
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN sales_platform_code")
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN sales_platform_name")
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN has_main_image")
        conn.execute("ALTER TABLE shipment_erp DROP COLUMN policy_block_code")
        conn.execute("PRAGMA user_version = 21")
        conn.commit()

    upgraded = ShipmentWorkflowStore(path)
    upgraded.initialize()

    with upgraded.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        job_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")
        }
        erp_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_erp)")
        }
        assert {
            "sales_platform_code",
            "sales_platform_name",
            "has_main_image",
        }.issubset(job_columns)
        assert "policy_block_code" in erp_columns
    assert list(tmp_path.glob("shipment_queue.pre_v22_*.sqlite3"))


def test_polluted_customer_shipping_service_is_repaired_from_explicit_detail(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    polluted = _candidate()
    polluted.customer_shipping_service = "UPS-全程"
    store.upsert_candidate(polluted)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = '2020-01-01T00:00:00Z' "
            "WHERE logistics_no = ?",
            (polluted.logistics_no,),
        )
        conn.commit()

    assert store.customer_shipping_service_backfill_counts() == {
        "contaminated_job_count": 1,
        "target_count": 1,
        "detail_target_count": 1,
    }
    assert store.customer_shipping_service_backfill_targets() == [
        {
            "job_id": 1,
            "system_order_no": polluted.system_order_no,
            "platform_order_no": polluted.platform_order_no,
            "logistics_no": polluted.logistics_no,
            "expected_old_value": "UPS-全程",
        }
    ]

    result = store.apply_customer_shipping_service_backfill(
        [
            {
                "system_order_no": polluted.system_order_no,
                "platform_order_no": polluted.platform_order_no,
                "logistics_no": polluted.logistics_no,
                "expected_old_value": "UPS-全程",
                "customer_shipping_service": "Expedited",
                "authoritative_field": "buyer_choose_express",
            }
        ],
        run_id="service-repair-001",
    )

    assert result == {
        "target_count": 1,
        "resolved_target_count": 1,
        "unresolved_target_count": 0,
        "updated_job_count": 1,
        "already_resolved_target_count": 0,
        "cas_mismatch_target_count": 0,
    }
    repaired = store.get_by_logistics_no(polluted.logistics_no)
    assert repaired["customer_shipping_service"] == "expedited"
    assert repaired["shipping_attention_notice"]
    assert store.customer_shipping_service_backfill_counts() == {
        "contaminated_job_count": 0,
        "target_count": 0,
        "detail_target_count": 0,
    }
    event = store.history(polluted.logistics_no)[-1]
    assert event.event_type == "CUSTOMER_SHIPPING_SERVICE_REPAIRED"
    assert event.old_state == "UPS-全程"
    assert event.new_state == "expedited"
    assert event.details["authoritative_field"] == "buyer_choose_express"


def test_customer_shipping_service_backfill_never_guesses_from_polluted_route(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    polluted = _candidate()
    polluted.customer_shipping_service = "Fedex-专线尾程"
    store.upsert_candidate(polluted)

    result = store.apply_customer_shipping_service_backfill(
        [
            {
                "system_order_no": polluted.system_order_no,
                "platform_order_no": polluted.platform_order_no,
                "logistics_no": polluted.logistics_no,
                "expected_old_value": "Fedex-专线尾程",
                "customer_shipping_service": "",
                "error": "订单详情未返回明确的客选物流字段。",
            }
        ]
    )

    assert result["unresolved_target_count"] == 1
    assert result["updated_job_count"] == 0
    assert (
        store.get_by_logistics_no(polluted.logistics_no)[
            "customer_shipping_service"
        ]
        == "Fedex-专线尾程"
    )


def test_customer_shipping_service_backfill_uses_exact_value_cas(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    polluted = _candidate()
    polluted.customer_shipping_service = "UPS-全程"
    store.upsert_candidate(polluted)

    result = store.apply_customer_shipping_service_backfill(
        [
            {
                "system_order_no": polluted.system_order_no,
                "platform_order_no": polluted.platform_order_no,
                "logistics_no": polluted.logistics_no,
                "expected_old_value": "UPS-专线尾程",
                "customer_shipping_service": "Standard",
                "authoritative_field": "buyer_choose_express",
            }
        ]
    )

    assert result["cas_mismatch_target_count"] == 1
    assert result["updated_job_count"] == 0
    assert (
        store.get_by_logistics_no(polluted.logistics_no)[
            "customer_shipping_service"
        ]
        == "UPS-全程"
    )


def test_v16_database_migrates_customer_shipping_service_and_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN customer_shipping_service")
        conn.execute("PRAGMA user_version = 16")

    ShipmentWorkflowStore(path).initialize()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")}
        assert "customer_shipping_service" in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v17_*.sqlite3"))


def test_v17_database_migrates_product_identity_checkpoint_and_creates_backup(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "ALTER TABLE shipment_jobs DROP COLUMN product_identity_catalog_version"
        )
        conn.execute(
            "ALTER TABLE shipment_jobs DROP COLUMN product_identity_checked_at"
        )
        conn.execute("PRAGMA user_version = 17")

    ShipmentWorkflowStore(path).initialize()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")}
        assert "product_identity_catalog_version" in columns
        assert "product_identity_checked_at" in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v18_*.sqlite3"))


def test_v18_database_migrates_product_identity_retry_state_and_creates_backup(
    tmp_path,
):
    path = tmp_path / "shipment_queue.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN product_identity_retry_count")
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN product_identity_next_retry_at")
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN product_identity_last_error")
        conn.execute(
            "ALTER TABLE shipment_order_product_snapshots "
            "DROP COLUMN marketplace_product_id"
        )
        conn.execute("PRAGMA user_version = 18")

    ShipmentWorkflowStore(path).initialize()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")}
        assert {
            "product_identity_retry_count",
            "product_identity_next_retry_at",
            "product_identity_last_error",
        }.issubset(columns)
        product_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(shipment_order_product_snapshots)"
            )
        }
        assert "marketplace_product_id" in product_columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v19_*.sqlite3"))


def test_v19_database_migrates_and_backfills_permanent_overdue_history(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = _candidate()
    candidate.customer_shipping_service = "Standard"
    store.upsert_candidate(candidate)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = ? WHERE logistics_no = ?",
            ("2026-08-01T00:00:00Z", candidate.logistics_no),
        )
        conn.execute(
            "UPDATE shipment_logistics SET state = ?, carrier_raw = ?, "
            "carrier_normalized = ?, international_tracking_no = ?, "
            "state_changed_at = ?, last_checked_at = ?, updated_at = ? WHERE job_id = 1",
            (
                LOGISTICS_READY,
                "UPS",
                "UPS",
                "1Z204E380338943508",
                "2026-08-04T10:00:00Z",
                "2026-08-04T10:00:00Z",
                "2026-08-04T10:00:00Z",
            ),
        )
        conn.execute(
            "UPDATE shipment_events SET created_at = ? "
            "WHERE job_id = 1 AND event_type = 'LOGISTICS_ATTEMPT_COMPLETED'",
            ("2026-08-04T10:00:00Z",),
        )
        conn.execute("ALTER TABLE shipment_jobs DROP COLUMN logistics_overdue_at")
        conn.execute("PRAGMA user_version = 19")

    migrated = ShipmentWorkflowStore(path)
    migrated.initialize()

    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(shipment_jobs)")
        }
        overdue_at = conn.execute(
            "SELECT logistics_overdue_at FROM shipment_jobs WHERE id = 1"
        ).fetchone()[0]
        assert "logistics_overdue_at" in columns
        assert overdue_at == "2026-08-04T09:30:00Z"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert list(tmp_path.glob("shipment_queue.pre_v20_*.sqlite3"))


def test_product_identity_backfill_preserves_shipment_workflow_states(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.product_type = ""
    store.upsert_candidate(candidate)
    before = store.get_by_logistics_no(candidate.logistics_no)

    result = store.apply_product_identity_backfill(
        [
            {
                "system_order_no": candidate.system_order_no,
                "platform_order_no": candidate.platform_order_no,
                "product_types": ("tent", "tablecloths"),
                "observed_asins": ("B0CRRGTPFH", "B0FX9W3MJL"),
            }
        ],
        catalog_version="test-catalog-v1",
        run_id="product-type-test",
    )
    after = store.get_by_logistics_no(candidate.logistics_no)

    assert result == {
        "target_count": 1,
        "checked_job_count": 1,
        "resolved_job_count": 1,
        "unresolved_job_count": 0,
        "failed_target_count": 0,
        "retry_scheduled_job_count": 0,
    }
    assert after["product_type"] == "tent"
    assert after["product_identity_catalog_version"] == "test-catalog-v1"
    assert after["product_identity_checked_at"]
    assert json.loads(after["product_identity_evidence_json"])[
        "observed_asins"
    ] == ["B0CRRGTPFH", "B0FX9W3MJL"]
    for field in (
        "identity_state",
        "logistics_state",
        "erp_state",
        "erp_checkpoint",
        "identity_state_changed_at",
        "logistics_state_changed_at",
        "erp_state_changed_at",
    ):
        assert after[field] == before[field]
    assert store.history(candidate.logistics_no)[-1].event_type == (
        "PRODUCT_IDENTITY_BACKFILLED"
    )


def test_completed_exact_sku_prepass_and_platform_sibling_backfill(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform = "112-0703089-1217824"
    old = _candidate("ALS-OLD", "SYS-OLD", platform)
    current = _candidate("ALS-CURRENT", "SYS-CURRENT", platform)
    pending = _candidate("ALS-PENDING", "SYS-PENDING", platform)
    existing = _candidate("ALS-EXISTING", "SYS-EXISTING", platform)
    for candidate in (old, current, pending):
        candidate.product_type = ""
        candidate.sku_text = "Car-Magnet-12x18in-2pcs"
    existing.product_type = "tablecloths"
    existing.sku_text = "Car-Magnet-12x18in-2pcs"
    store.insert_candidates([old, current, pending, existing])
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_erp SET state = 'DONE' WHERE job_id IN ("
            "SELECT id FROM shipment_jobs WHERE system_order_no IN (?, ?)"
            ")",
            (old.system_order_no, current.system_order_no),
        )
        conn.execute(
            "UPDATE shipment_erp SET state = 'DONE' WHERE job_id = ("
            "SELECT id FROM shipment_jobs WHERE system_order_no = ?"
            ")",
            (existing.system_order_no,),
        )
        conn.commit()

    sku_targets = store.list_completed_sku_product_identity_jobs()

    assert {
        (row["system_order_no"], tuple(row["product_types"]))
        for row in sku_targets
    } == {
        (old.system_order_no, ("car_magnet",)),
        (current.system_order_no, ("car_magnet",)),
    }

    result = store.apply_product_identity_backfill(
        [
            {
                "system_order_no": current.system_order_no,
                "platform_order_no": platform,
                "product_types": ("car_magnet",),
                "observed_skus": ("Car-Magnet-12x18in-2pcs",),
                "match_platform_siblings": True,
                "completed_only": True,
                "evidence_scope": "notification_full_scan_exact_skus",
            }
        ],
        catalog_version="sku-catalog-v1",
    )

    assert result["resolved_job_count"] == 2
    assert store.get_by_logistics_no(old.logistics_no)["product_type"] == (
        "car_magnet"
    )
    assert store.get_by_logistics_no(current.logistics_no)["product_type"] == (
        "car_magnet"
    )
    assert store.get_by_logistics_no(pending.logistics_no)["product_type"] == ""
    assert store.get_by_logistics_no(existing.logistics_no)["product_type"] == (
        "tablecloths"
    )
    assert store.history(old.logistics_no)[-1].details["observed_skus"] == [
        "Car-Magnet-12x18in-2pcs"
    ]


def test_unknown_product_identity_is_rechecked_only_after_catalog_changes(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.product_type = ""
    store.upsert_candidate(candidate)

    targets = store.list_missing_product_type_jobs(
        catalog_version="test-catalog-v1",
        limit=25,
    )
    assert [(item["system_order_no"], item["platform_order_no"]) for item in targets] == [
        (candidate.system_order_no, candidate.platform_order_no)
    ]

    result = store.apply_product_identity_backfill(
        [
            {
                "system_order_no": candidate.system_order_no,
                "platform_order_no": candidate.platform_order_no,
                "product_types": (),
                "observed_asins": ("B0UNKNOWN00",),
            }
        ],
        catalog_version="test-catalog-v1",
    )
    assert result["unresolved_job_count"] == 1
    assert not store.get_by_logistics_no(candidate.logistics_no)["product_type"]
    assert store.list_missing_product_type_jobs(
        catalog_version="test-catalog-v1",
        limit=25,
    ) == []
    assert len(
        store.list_missing_product_type_jobs(
            catalog_version="test-catalog-v2",
            limit=25,
        )
    ) == 1


def test_product_identity_row_prefers_persisted_asin_evidence_over_later_retry(
    tmp_path,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.product_type = ""
    store.upsert_candidate(candidate)
    store.apply_product_identity_backfill(
        [
            {
                "system_order_no": candidate.system_order_no,
                "platform_order_no": candidate.platform_order_no,
                "product_types": (),
                "observed_asins": ("B0UNKNOWN00",),
                "evidence_scope": "exact_detail",
            }
        ],
        catalog_version="test-catalog-v1",
    )
    store.apply_product_identity_backfill(
        [
            {
                "system_order_no": candidate.system_order_no,
                "platform_order_no": candidate.platform_order_no,
                "error": "详情查询失败。",
                "evidence_scope": "sibling_aggregate",
            }
        ],
        catalog_version="test-catalog-v2",
    )

    row = store.get_by_logistics_no(candidate.logistics_no)

    assert json.loads(row["product_identity_evidence_json"])[
        "observed_asins"
    ] == ["B0UNKNOWN00"]


def test_failed_product_detail_is_deferred_without_blocking_later_targets(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate("ALS-FIRST", "SYS-FIRST", "111-0000000-0000001")
    second = _candidate("ALS-SECOND", "SYS-SECOND", "111-0000000-0000002")
    first.product_type = ""
    second.product_type = ""
    store.insert_candidates([first, second])

    result = store.apply_product_identity_backfill(
        [
            {
                "system_order_no": first.system_order_no,
                "platform_order_no": first.platform_order_no,
                "error": "详情查询失败。",
                "evidence_scope": "sibling_aggregate",
                "evidence_system_order_nos": ("SYS-FIRST", "SYS-SIBLING"),
            }
        ],
        catalog_version="test-catalog-v1",
    )

    row = store.get_by_logistics_no(first.logistics_no)
    assert result["failed_target_count"] == 1
    assert result["retry_scheduled_job_count"] == 1
    assert row["product_type"] == ""
    assert not row["product_identity_catalog_version"]
    assert row["product_identity_retry_count"] == 1
    assert row["product_identity_next_retry_at"]
    assert row["product_identity_last_error"] == "详情查询失败。"
    targets = store.list_missing_product_type_jobs(
        catalog_version="test-catalog-v1",
        limit=25,
    )
    assert [item["system_order_no"] for item in targets] == ["SYS-SECOND"]
    assert store.product_identity_backfill_counts(
        catalog_version="test-catalog-v1"
    ) == {
        "total_target_count": 2,
        "due_target_count": 1,
        "deferred_target_count": 1,
    }
    event = store.history(first.logistics_no)[-1]
    assert event.event_type == "PRODUCT_IDENTITY_RETRY_SCHEDULED"
    assert event.details["evidence_system_order_nos"] == [
        "SYS-FIRST",
        "SYS-SIBLING",
    ]

    assert store.release_deferred_product_identity_retries(
        run_id="manual-audit"
    ) == 1
    released = store.get_by_logistics_no(first.logistics_no)
    assert not released["product_identity_next_retry_at"]
    assert {
        item["system_order_no"]
        for item in store.list_missing_product_type_jobs(
            catalog_version="test-catalog-v1",
            limit=25,
        )
    } == {"SYS-FIRST", "SYS-SECOND"}
    release_event = store.history(first.logistics_no)[-1]
    assert release_event.event_type == "PRODUCT_IDENTITY_RETRY_RELEASED"
    assert release_event.run_id == "manual-audit"


def test_current_run_cancel_keeps_stable_queue_position(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate("ALS-FIRST", "SYS-FIRST", "111-FIRST")
    second = _candidate("ALS-SECOND", "SYS-SECOND", "111-SECOND")
    store.insert_candidates([first, second])

    before = [row["logistics_no"] for row in store.list_all_jobs()]
    assert store.cancel(first.logistics_no, "cancel current run")
    after = [row["logistics_no"] for row in store.list_all_jobs()]

    assert before == [first.logistics_no, second.logistics_no]
    assert after == before


def test_completed_erp_job_cannot_be_cancelled_for_current_run(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)

    assert store.cancel(candidate.logistics_no, "must not cancel completed") is False
    assert store.cancel_many([candidate.logistics_no], "must not cancel completed") == 0
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["identity_state"] == IDENTITY_ACTIVE
    assert row["erp_state"] == ERP_DONE


def test_v1_table_is_read_only_after_migration(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    _create_v1_database(path, [{"logistics_no": "ALS01781406025", "status": "NEW"}])
    ShipmentWorkflowStore(path).initialize()

    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE shipment_queue_v1 SET queue_status = 'ERROR'")


def test_v1_migration_failure_rolls_back_schema_changes(monkeypatch, tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    _create_v1_database(path, [{"logistics_no": "ALS01781406025", "status": "NEW"}])
    store = ShipmentWorkflowStore(path)

    def fail_migration(_conn):
        raise RuntimeError("forced migration failure")

    monkeypatch.setattr(store, "_migrate_v1", fail_migration)
    with pytest.raises(RuntimeError, match="forced migration failure"):
        store.initialize()

    with sqlite3.connect(path) as conn:
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "shipment_queue" in table_names
        assert "shipment_queue_v1" not in table_names
        assert "shipment_jobs" not in table_names


def test_waiting_logistics_is_not_due_until_explicit_retry(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(logistics_no=candidate.logistics_no, status_text="待揽收"),
        state=LOGISTICS_WAITING,
        last_error="物流状态未就绪",
    )

    assert store.list_logistics_check_candidates() == []
    assert store.retry_stage(candidate.logistics_no, "logistics")
    assert [row["logistics_no"] for row in store.list_logistics_check_candidates()] == [candidate.logistics_no]


def test_email_batch_waits_for_all_known_packages_and_supplement_contains_history(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    first = _candidate("ALS01781406025", platform_order_no=platform_order_no)
    second = _candidate("ALS01789020252", system_order_no="103710639045926988", platform_order_no=platform_order_no)
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    _make_ready(store, second.logistics_no)

    store.mark_erp_outbounded(first.logistics_no, email_preview_enabled=True)
    assert store.list_email_batches() == []
    store.mark_erp_outbounded(second.logistics_no, email_preview_enabled=True)
    batches = store.list_email_batches()
    assert len(batches) == 1
    assert batches[0].logistics_numbers == [first.logistics_no, second.logistics_no]
    first_message_id = batches[0].message_id
    assert store.mark_email_batch_sent(batches[0].id)

    third = _candidate("ALS01789020253", system_order_no="103710639045926989", platform_order_no=platform_order_no)
    store.upsert_candidate(third)
    _make_ready(store, third.logistics_no)
    store.mark_erp_outbounded(third.logistics_no, email_preview_enabled=True)

    batches = store.list_email_batches()
    assert len(batches) == 2
    assert batches[0].state == EMAIL_SENT
    assert batches[1].logistics_numbers == [first.logistics_no, second.logistics_no, third.logistics_no]
    assert batches[1].message_id != first_message_id


def test_paused_unfinished_package_blocks_initial_email_preview(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    first = _candidate("ALS-FIRST", platform_order_no=platform_order_no)
    second = _candidate(
        "ALS-PAUSED",
        system_order_no="103710639045926988",
        platform_order_no=platform_order_no,
    )
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    store.reconcile_shipment_tag_snapshot(
        {second.system_order_no: False},
        snapshot_complete=True,
    )
    _make_ready(store, first.logistics_no)

    store.mark_erp_outbounded(first.logistics_no, email_preview_enabled=True)

    assert store.get_by_logistics_no(second.logistics_no)["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
    assert store.list_email_batches(platform_order_no=platform_order_no) == []


@pytest.mark.parametrize("identity_change", ["pause", "cancel"])
def test_existing_pending_email_is_blocked_by_known_unfinished_package(
    tmp_path,
    identity_change,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    first = _candidate("ALS-FIRST", platform_order_no=platform_order_no)
    store.upsert_candidate(first)
    _make_ready(store, first.logistics_no)
    store.mark_erp_outbounded(first.logistics_no, email_preview_enabled=True)
    pending = store.list_email_batches(platform_order_no=platform_order_no)[0]
    assert pending.state == EMAIL_PENDING

    second = _candidate(
        "ALS-UNFINISHED",
        system_order_no="103710639045926988",
        platform_order_no=platform_order_no,
    )
    store.upsert_candidate(second)
    if identity_change == "pause":
        store.reconcile_shipment_tag_snapshot(
            {second.system_order_no: False},
            snapshot_complete=True,
        )
    else:
        assert store.cancel(second.logistics_no, "operator cancelled unfinished package")

    changed_count = store.prepare_email_batches_with_count(platform_order_no=platform_order_no)
    blocked = store.list_email_batches(platform_order_no=platform_order_no)[0]

    assert changed_count == 1
    assert blocked.id == pending.id
    assert blocked.state == EMAIL_BLOCKED
    assert "未完成的已知非冲突包裹" in str(blocked.last_error)
    assert blocked.logistics_numbers == [first.logistics_no, second.logistics_no]
    assert store.prepare_email_batches_with_count(platform_order_no=platform_order_no) == 0


def test_conflict_package_blocks_existing_pending_email_until_manually_resolved(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    first = _candidate("ALS-FIRST", platform_order_no=platform_order_no)
    store.upsert_candidate(first)
    _make_ready(store, first.logistics_no)
    store.mark_erp_outbounded(first.logistics_no, email_preview_enabled=True)
    pending = store.list_email_batches(platform_order_no=platform_order_no)[0]

    conflict = _candidate(
        "ALS-CONFLICT",
        system_order_no="103710639045926988",
        platform_order_no=platform_order_no,
    )
    store.upsert_candidate(conflict)
    store.upsert_candidate(
        _candidate(
            "ALS-CONFLICT",
            system_order_no="103710639045926999",
            platform_order_no="113-0000000-0000000",
        )
    )

    changed_count = store.prepare_email_batches_with_count(platform_order_no=platform_order_no)

    assert store.get_by_logistics_no(conflict.logistics_no)["identity_state"] == IDENTITY_CONFLICT
    blocked = store.list_email_batches(platform_order_no=platform_order_no)[0]
    assert changed_count == 1
    assert blocked.id == pending.id
    assert blocked.state == EMAIL_BLOCKED
    assert "订单归属冲突" in str(blocked.last_error)
    assert blocked.logistics_numbers == [first.logistics_no, conflict.logistics_no]


@pytest.mark.parametrize("blocker", ["unfinished", "paused", "conflict"])
def test_manual_email_retry_rechecks_platform_safety_instead_of_forcing_pending(
    tmp_path,
    blocker,
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    first = _candidate("ALS-FIRST", platform_order_no=platform_order_no)
    store.upsert_candidate(first)
    _make_ready(store, first.logistics_no)
    store.mark_erp_outbounded(first.logistics_no, email_preview_enabled=True)
    assert store.list_email_batches(platform_order_no=platform_order_no)[0].state == EMAIL_PENDING

    second = _candidate(
        "ALS-UNSAFE",
        system_order_no="103710639045926988",
        platform_order_no=platform_order_no,
    )
    store.upsert_candidate(second)
    if blocker == "paused":
        store.reconcile_shipment_tag_snapshot(
            {second.system_order_no: False},
            snapshot_complete=True,
        )
    elif blocker == "conflict":
        store.upsert_candidate(
            _candidate(
                second.logistics_no,
                system_order_no="103710639045926999",
                platform_order_no="113-0000000-0000000",
            )
        )

    assert store.retry_email_for_logistics_no(second.logistics_no, reason="operator retry")
    blocked = store.list_email_batches(platform_order_no=platform_order_no)[0]

    assert blocked.state == EMAIL_BLOCKED
    if blocker == "conflict":
        assert "订单归属冲突" in str(blocked.last_error)
    else:
        assert "未完成的已知非冲突包裹" in str(blocked.last_error)
    assert not store.retry_email_batch(blocked.id, reason="repeat unsafe retry")
    assert store.list_email_batches(platform_order_no=platform_order_no)[0].state == EMAIL_BLOCKED


def test_manual_email_retry_only_unblocks_missing_recipient_after_it_is_added(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    blocked = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert blocked.state == EMAIL_BLOCKED

    assert not store.retry_email_for_logistics_no(candidate.logistics_no, reason="still missing")
    assert store.list_email_batches(platform_order_no=candidate.platform_order_no)[0].state == EMAIL_BLOCKED

    store.upsert_candidate(_candidate(receiver_email=" Buyer@Example.COM "))
    assert store.retry_email_for_logistics_no(candidate.logistics_no, reason="recipient supplied")
    pending = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert pending.id == blocked.id
    assert pending.state == EMAIL_PENDING
    assert pending.recipient_email == "buyer@example.com"
    assert pending.last_error is None


def test_manual_email_retry_wakes_safe_retryable_batch_but_never_changes_sent_batch(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    batch = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_email_batches SET state = ?, last_error = ? WHERE id = ?",
            (EMAIL_RETRYABLE, "temporary local preview error", batch.id),
        )

    assert store.retry_email_batch(batch.id, reason="safe retry")
    retried = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert retried.state == EMAIL_PENDING
    assert retried.last_error is None

    assert store.mark_email_batch_sent(retried.id)
    assert not store.retry_email_for_logistics_no(candidate.logistics_no, reason="must stay sent")
    assert not store.retry_email_batch(retried.id, reason="must stay sent")
    sent = store.list_email_batches(platform_order_no=candidate.platform_order_no)
    assert len(sent) == 1
    assert sent[0].state == EMAIL_SENT


def test_email_message_id_is_stable_when_preparation_repeats(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    first = store.list_email_batches()[0]

    changed_count = store.prepare_email_batches_with_count(platform_order_no=candidate.platform_order_no)
    second = store.list_email_batches()[0]

    assert changed_count == 0
    assert first.message_id == second.message_id
    assert first.sequence_no == second.sequence_no == 1


def test_blocked_email_batch_becomes_pending_when_receiver_email_is_added(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    blocked = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert blocked.state == EMAIL_BLOCKED
    assert blocked.recipient_email is None

    refreshed = _candidate(receiver_email=" Buyer@Example.COM ")
    store.upsert_candidate(refreshed)
    changed_count = store.prepare_email_batches_with_count(platform_order_no=candidate.platform_order_no)

    pending = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert changed_count == 1
    assert pending.id == blocked.id
    assert pending.sequence_no == blocked.sequence_no == 1
    assert pending.state == EMAIL_PENDING
    assert pending.recipient_email == "buyer@example.com"
    assert pending.last_error is None
    assert pending.message_id != blocked.message_id


def test_completed_missing_email_can_be_safely_backfilled_from_order_detail(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)

    assert store.missing_receiver_email_targets() == [
        {
            "system_order_no": candidate.system_order_no,
            "platform_order_no": candidate.platform_order_no,
        }
    ]
    assert store.backfill_receiver_email(
        system_order_no=candidate.system_order_no,
        platform_order_no=candidate.platform_order_no,
        receiver_email="not-an-email",
    ) is False
    assert store.backfill_receiver_email(
        system_order_no=candidate.system_order_no,
        platform_order_no=candidate.platform_order_no,
        receiver_email=" Buyer@Example.COM ",
        run_id="shipment-scan-1",
    ) is True
    assert store.backfill_receiver_email(
        system_order_no=candidate.system_order_no,
        platform_order_no=candidate.platform_order_no,
        receiver_email="other@example.com",
    ) is False

    assert store.prepare_email_batches_with_count(
        platform_order_no=candidate.platform_order_no
    ) == 1
    batch = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert batch.state == EMAIL_PENDING
    assert batch.recipient_email == "buyer@example.com"
    event = [
        item
        for item in store.history(candidate.logistics_no)
        if item.event_type == "RECEIVER_EMAIL_BACKFILLED"
    ][0]
    assert event.details == {"source": "lingxing_order_detail"}
    assert "buyer@example.com" not in repr(event)


def test_legacy_sent_email_hash_remains_idempotent_after_hash_upgrade(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    batch = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    assert store.mark_email_batch_sent(batch.id)
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.logistics_no, j.receiver_email,
                   l.carrier_normalized, l.carrier_raw, l.international_tracking_no
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            WHERE j.platform_order_no = ?
            ORDER BY j.id
            """,
            (candidate.platform_order_no,),
        ).fetchall()
        conn.execute(
            "UPDATE shipment_email_batches SET content_hash = ? WHERE id = ?",
            (store._email_legacy_content_hash(rows), batch.id),
        )

    store.prepare_email_batches(platform_order_no=candidate.platform_order_no)

    batches = store.list_email_batches(platform_order_no=candidate.platform_order_no)
    assert len(batches) == 1
    assert batches[0].id == batch.id
    assert batches[0].state == EMAIL_SENT


def test_legacy_sent_email_without_recipient_does_not_create_upgrade_duplicate(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)
    batch = store.list_email_batches(platform_order_no=candidate.platform_order_no)[0]
    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.logistics_no, j.receiver_email,
                   l.carrier_normalized, l.carrier_raw, l.international_tracking_no
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            WHERE j.platform_order_no = ?
            ORDER BY j.id
            """,
            (candidate.platform_order_no,),
        ).fetchall()
        conn.execute(
            """
            UPDATE shipment_email_batches
            SET state = ?, content_hash = ?, last_error = NULL
            WHERE id = ?
            """,
            (EMAIL_SENT, store._email_legacy_content_hash(rows), batch.id),
        )

    changed_count = store.prepare_email_batches_with_count(
        platform_order_no=candidate.platform_order_no
    )

    batches = store.list_email_batches(platform_order_no=candidate.platform_order_no)
    assert changed_count == 0
    assert len(batches) == 1
    assert batches[0].id == batch.id
    assert batches[0].state == EMAIL_SENT


def test_independent_site_order_does_not_create_email_batch_after_erp_done(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(platform_order_no="wc39877")
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)

    assert store.get_by_logistics_no(candidate.logistics_no)["customer_email_required"] == 0
    assert store.prepare_email_batches(platform_order_no=candidate.platform_order_no) == []
    assert store.list_email_batches(platform_order_no=candidate.platform_order_no) == []


def test_email_blocked_job_is_included_in_attention_list_after_erp_done(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no, email_preview_enabled=True)

    rows = store.list_attention()

    assert [row["logistics_no"] for row in rows] == [candidate.logistics_no]
    assert rows[0]["erp_state"] == ERP_DONE
    assert rows[0]["email_state"] == EMAIL_BLOCKED
    assert rows[0]["last_error"] == "邮件预览未生成：缺少收件邮箱（不影响 ERP 标发）。"


def test_complete_missing_pending_orders_closes_only_absent_active_jobs(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    present = _candidate()
    missing = _candidate(
        "ALS01792557166",
        system_order_no="103717510103539424",
        platform_order_no="114-9238856-6341844",
    )
    store.upsert_candidate(present)
    store.upsert_candidate(missing)
    with sqlite3.connect(tmp_path / "shipment_queue.sqlite3") as conn:
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = ?, last_error = ?
            WHERE job_id = (SELECT id FROM shipment_jobs WHERE logistics_no = ?)
            """,
            (ERP_BLOCKED, "没有找到菜单项：设置仓库物流", missing.logistics_no),
        )
    started_at = utc_now()

    completed = store.complete_missing_pending_orders(
        {present.system_order_no},
        discovered_before=started_at,
        run_id="scan-1",
    )

    assert [item.logistics_no for item in completed] == [missing.logistics_no]
    present_row = store.get_by_logistics_no(present.logistics_no)
    missing_row = store.get_by_logistics_no(missing.logistics_no)
    assert present_row["erp_state"] != ERP_DONE
    assert missing_row["erp_state"] == ERP_DONE
    assert missing_row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert missing_row["completion_source"] == ERP_COMPLETION_MANUAL_DETECTED
    assert missing_row["externally_completed_at"]
    assert [row["logistics_no"] for row in store.list_logistics_check_candidates()] == [present.logistics_no]
    assert store.list_erp_mark_candidates() == []
    assert missing.logistics_no not in [item.logistics_no for item in store.list_logistics_skipped_records()]
    assert store.prepare_email_batches(platform_order_no=missing.platform_order_no) == []
    event = store.history(missing.logistics_no)[-1]
    assert event.event_type == "MANUAL_COMPLETION_DETECTED"
    assert event.details["previous_error"] == "没有找到菜单项：设置仓库物流"


def test_v2_missing_order_errors_migrate_to_manual_done(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = _candidate(
        "ALS01792557166",
        system_order_no="103717510103539424",
        platform_order_no="114-9238856-6341844",
    )
    store.upsert_candidate(candidate)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE shipment_erp SET state = ?, last_error = ?",
            (
                ERP_BLOCKED,
                "没有找到菜单项：设置仓库物流",
            ),
        )
        conn.execute("ALTER TABLE shipment_erp DROP COLUMN externally_completed_at")
        conn.execute("ALTER TABLE shipment_erp DROP COLUMN completion_source")
        conn.execute("PRAGMA user_version = 2")

    migrated = ShipmentWorkflowStore(path)
    row = migrated.get_by_logistics_no(candidate.logistics_no)

    assert row["erp_state"] == ERP_DONE
    assert row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert row["completion_source"] == ERP_COMPLETION_MANUAL_DETECTED
    assert row["erp_last_error"] is None
    assert list(tmp_path.glob("shipment_queue.pre_v3_*.sqlite3"))
    event = migrated.history(candidate.logistics_no)[-1]
    assert event.event_type == "MANUAL_COMPLETION_DETECTED"
    assert event.details["previous_error"] == "没有找到菜单项：设置仓库物流"


def test_manual_completed_package_is_excluded_from_mixed_email_batch(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    platform_order_no = "112-1165824-9982644"
    automated = _candidate(platform_order_no=platform_order_no)
    manual = _candidate(
        "ALS01792557166",
        system_order_no="103717510103539424",
        platform_order_no=platform_order_no,
    )
    store.upsert_candidate(automated)
    store.upsert_candidate(manual)
    started_at = utc_now()
    store.complete_missing_pending_orders(
        {automated.system_order_no},
        discovered_before=started_at,
        run_id="scan-2",
    )
    _make_ready(store, automated.logistics_no)
    store.mark_erp_outbounded(automated.logistics_no, email_preview_enabled=True)

    batches = store.list_email_batches(platform_order_no=platform_order_no)
    assert len(batches) == 1
    assert batches[0].logistics_numbers == [automated.logistics_no]


def test_desktop_can_add_or_refresh_a_manual_candidate_with_history(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")

    inserted = store.add_manual_candidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="API 扫描未出现，人工核对后加入。",
    )
    refreshed = store.add_manual_candidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        reason="再次核对订单身份。",
    )

    assert inserted.inserted is True
    assert refreshed.inserted is False
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["identity_state"] == IDENTITY_ACTIVE
    assert row["logistics_state"] == LOGISTICS_PENDING
    assert row["erp_state"] == ERP_WAITING
    assert [event.event_type for event in store.history("ALS01781406025")] == [
        "CANDIDATE_DISCOVERED",
        "MANUAL_CANDIDATE_ADDED",
        "CANDIDATE_RESEEN_IMMEDIATE",
        "MANUAL_CANDIDATE_REFRESHED",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("system_order_no", "not-a-system-order"),
        ("platform_order_no", "not-a-platform-order"),
        ("logistics_no", "含空格的物流号"),
        ("reason", ""),
    ],
)
def test_manual_candidate_rejects_invalid_identity_or_missing_reason(tmp_path, field, value):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    values = {
        "system_order_no": "103710434633847501",
        "platform_order_no": "112-1165824-9982644",
        "logistics_no": "ALS01781406025",
        "reason": "人工核对。",
    }
    values[field] = value

    with pytest.raises(ValueError):
        store.add_manual_candidate(**values)

    assert store.list_all_jobs() == []


def test_desktop_status_changes_are_audited_and_manual_done_can_be_undone(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)

    assert store.cancel(candidate.logistics_no, "暂不处理") is True
    assert store.restore_cancelled(candidate.logistics_no, reason="确认恢复") is True
    assert store.mark_manually_completed(
        candidate.logistics_no,
        reason="已在领星人工完成并核对",
    ) is True
    completed = store.get_by_logistics_no(candidate.logistics_no)
    assert completed["identity_state"] == IDENTITY_ACTIVE
    assert completed["erp_state"] == ERP_DONE
    assert completed["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert completed["completion_source"] == ERP_COMPLETION_MANUAL_DETECTED

    assert store.undo_manual_completion(
        candidate.logistics_no,
        reason="发现人工完成记录录入错误",
    ) is True
    restored = store.get_by_logistics_no(candidate.logistics_no)
    assert restored["erp_state"] == ERP_WAITING
    assert restored["erp_checkpoint"] == "NONE"
    assert restored["completion_source"] is None
    event_types = [event.event_type for event in store.history(candidate.logistics_no)]
    assert event_types[-4:] == [
        "JOB_CANCELLED",
        "JOB_RESTORED",
        "MANUAL_STATUS_SET_DONE",
        "MANUAL_STATUS_UNDONE",
    ]


def test_manual_completion_cannot_undo_reconciliation_completion(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    started_at = utc_now()
    store.complete_missing_pending_orders(set(), discovered_before=started_at)

    assert store.undo_manual_completion(candidate.logistics_no, reason="不应允许") is False
