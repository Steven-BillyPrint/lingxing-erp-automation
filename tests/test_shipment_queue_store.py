import sqlite3

import pytest

from shipment_automation.models import (
    EMAIL_BLOCKED,
    EMAIL_SENT,
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_OUTBOUNDED,
    ERP_COMPLETION_MANUAL_DETECTED,
    ERP_BLOCKED,
    ERP_DONE,
    ERP_PENDING,
    ERP_RETRYABLE,
    IDENTITY_ACTIVE,
    IDENTITY_CANCELLED,
    IDENTITY_CONFLICT,
    LOGISTICS_BLOCKED,
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
    )


def _ready_detail(logistics_no: str) -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=logistics_no,
        status_text="运输中",
        service_type="快递门到门",
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

    other = _candidate(logistics_no="ALS01789020252")
    store.upsert_candidate(other)
    store.cancel(other.logistics_no, "测试取消")
    cancelled_result = store.upsert_candidate(other)

    assert cancelled_result.existing["identity_state"] == IDENTITY_CANCELLED
    assert cancelled_result.immediate_logistics is False
    assert cancelled_result.immediate_erp is False


def test_repeat_scan_converts_technical_blocked_logistics_to_retryable(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    store.complete_logistics_attempt(
        "ALS01781406025",
        LogisticsDetail(
            logistics_no="ALS01781406025",
            page_error="等待阿里国际站物流详情页加载或登录完成超时。",
        ),
        state=LOGISTICS_BLOCKED,
        last_error="等待阿里国际站物流详情页加载或登录完成超时。",
    )

    result = store.upsert_candidate(_candidate())

    assert result.immediate_logistics is True
    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["logistics_next_attempt_at"] is None


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
    assert row["erp_state"] == ERP_RETRYABLE
    assert store.list_logistics_check_candidates() == []
    assert [item.logistics_no for item in store.list_erp_mark_candidates()] == ["ALS01781406025"]


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
            international_tracking_no="JYCP00000093286",
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
def test_invalid_historical_ready_tracking_is_blocked_and_rolls_back_safely(
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

    blocked = store.block_invalid_tracking_records(run_id="erp-run-1")

    assert [item["logistics_no"] for item in blocked] == [candidate.logistics_no]
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_BLOCKED
    assert row["erp_state"] == "WAITING"
    assert row["erp_checkpoint"] == expected_checkpoint
    if checkpoint == ERP_CHECKPOINT_LOGISTICS_SAVED:
        assert row["logistics_payload_hash"] is None
        assert row["logistics_saved_at"] is None
        assert row["freight_amount"] is None
        assert row["chargeable_weight_g"] is None
    assert store.history(candidate.logistics_no)[-1].event_type == "TRACKING_NUMBER_BLOCKED"


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
        international_tracking_no="JYCP00000099999",
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


def test_tracking_mismatch_waits_for_first_review(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_invalid_fedex_ready(store, candidate.logistics_no)
    store.block_invalid_tracking_records()

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_BLOCKED
    assert row["tracking_mismatch_action"] is None
    assert row["logistics_next_attempt_at"] is None
    assert store.list_logistics_check_candidates() == []
    assert [item["logistics_no"] for item in store.list_pending_tracking_mismatch_reviews()] == [
        candidate.logistics_no
    ]


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
        international_tracking_no="JYCP00000099999",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        mismatch_detail,
        state=LOGISTICS_BLOCKED,
        last_error="国际物流单号与承运商不匹配：FEDEX / JYCP00000099999，请审核后选择处理方式。",
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


def test_auto_recheck_does_not_retry_a_different_blocked_reason(tmp_path):
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
    assert row["logistics_next_attempt_at"] is None
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

    blocked = store.block_invalid_tracking_records(run_id="new-worker-preflight")

    assert [item["logistics_no"] for item in blocked] == [candidate.logistics_no]
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["logistics_state"] == LOGISTICS_BLOCKED
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

    store.mark_erp_outbounded(first.logistics_no)
    assert store.list_email_batches() == []
    store.mark_erp_outbounded(second.logistics_no)
    batches = store.list_email_batches()
    assert len(batches) == 1
    assert batches[0].logistics_numbers == [first.logistics_no, second.logistics_no]
    first_message_id = batches[0].message_id
    assert store.mark_email_batch_sent(batches[0].id)

    third = _candidate("ALS01789020253", system_order_no="103710639045926989", platform_order_no=platform_order_no)
    store.upsert_candidate(third)
    _make_ready(store, third.logistics_no)
    store.mark_erp_outbounded(third.logistics_no)

    batches = store.list_email_batches()
    assert len(batches) == 2
    assert batches[0].state == EMAIL_SENT
    assert batches[1].logistics_numbers == [first.logistics_no, second.logistics_no, third.logistics_no]
    assert batches[1].message_id != first_message_id


def test_email_message_id_is_stable_when_preparation_repeats(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)
    first = store.list_email_batches()[0]

    store.prepare_email_batches(platform_order_no=candidate.platform_order_no)
    second = store.list_email_batches()[0]

    assert first.message_id == second.message_id
    assert first.sequence_no == second.sequence_no == 1


def test_independent_site_order_does_not_create_email_batch_after_erp_done(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(platform_order_no="wc39877")
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)

    assert store.get_by_logistics_no(candidate.logistics_no)["customer_email_required"] == 0
    assert store.prepare_email_batches(platform_order_no=candidate.platform_order_no) == []
    assert store.list_email_batches(platform_order_no=candidate.platform_order_no) == []


def test_email_blocked_job_is_included_in_attention_list_after_erp_done(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(receiver_email=None)
    store.upsert_candidate(candidate)
    _make_ready(store, candidate.logistics_no)
    store.mark_erp_outbounded(candidate.logistics_no)

    rows = store.list_attention()

    assert [row["logistics_no"] for row in rows] == [candidate.logistics_no]
    assert rows[0]["erp_state"] == ERP_DONE
    assert rows[0]["email_state"] == EMAIL_BLOCKED
    assert rows[0]["last_error"] == "Missing receiver email."


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
    store.mark_erp_outbounded(automated.logistics_no)

    batches = store.list_email_batches(platform_order_no=platform_order_no)
    assert len(batches) == 1
    assert batches[0].logistics_numbers == [automated.logistics_no]
