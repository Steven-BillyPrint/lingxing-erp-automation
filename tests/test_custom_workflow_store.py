from __future__ import annotations

import json
import sqlite3

import pytest

from erp_automation.persistence import (
    CustomWorkflowStore,
    StageRetryReviewResolution,
    WorkflowPauseKind,
    WorkflowStageState,
)
from lingxing_automation.storage.dedupe import load_processed_platform_orders


def _legacy_payload():
    return {
        "version": 3,
        "updated_at": "2026-07-14 10:00:00",
        "orders": {
            "111-1111111-1111111": {
                "platform_order_no": "111-1111111-1111111",
                "system_order_no": "103700000000000001",
                "product_type": "tent",
                "contact_writeback_complete": True,
                "folder_complete": True,
                "sku_adjustment_required": True,
                "sku_adjustment_complete": True,
                "package_split_required": True,
                "package_split_complete": True,
                "package_split_system_order_nos": ["103700000000000002"],
                "instruction_remark_required": False,
                "instruction_remark_complete": True,
                "instruction_remark_target_system_order_no": "103700000000000002",
                "workflow_status": "completed",
            },
            "112-2222222-2222222": {
                "platform_order_no": "112-2222222-2222222",
                "system_order_no": "103700000000000003",
                "contact_writeback_complete": True,
                "folder_complete": True,
                "workflow_status": "completed",
            },
        },
    }


def test_manual_cancellation_preserves_stages_and_reopen_restores_queue(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "111-1111111-1111111"
    before = store.get_workflow(order_no)

    summary = store.mark_workflows_cancelled(
        [order_no], reason="订单临时取消", actor="desktop_user"
    )

    cancelled = store.get_workflow(order_no)
    assert summary.changed_order_count == 1
    assert cancelled["workflow_status"] == "cancelled"
    assert cancelled["stages"] == before["stages"]
    assert order_no in store.processed_platform_orders()
    assert order_no not in {
        item["platform_order_no"] for item in store.list_active_scanned_workflows()
    }

    reopened = store.reopen_workflows_from_stage(
        [order_no], "sku", reason="恢复处理", actor="desktop_user"
    )
    assert reopened.changed_order_count == 1
    assert store.get_workflow(order_no)["workflow_status"] == "sku_adjustment_pending"


def test_import_is_transactional_idempotent_and_preserves_missing_stage_semantics(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload(), ensure_ascii=False), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")

    first = store.import_legacy_json(source)
    second = store.import_legacy_json(source)

    assert first.source_count == first.imported_count == 2
    assert first.backup_path and first.backup_path.exists()
    assert second.skipped is True
    assert store.processed_platform_orders() == {
        "111-1111111-1111111",
        "112-2222222-2222222",
    }
    legacy = store.get_workflow("112-2222222-2222222")
    assert legacy is not None
    stages = {item["stage"]: item for item in legacy["stages"]}
    assert stages["sku"]["required"] is None
    assert stages["sku"]["state"] == "NOT_APPLICABLE"


def test_workflow_summaries_include_stage_errors_without_detail_queries(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    store.set_stage_state(
        "111-1111111-1111111",
        "sku",
        WorkflowStageState.PENDING,
        reason="等待重试",
        result_status="failed",
        last_error="SKU API 明确拒绝",
    )

    rows = store.list_workflow_summaries(limit=2000)

    row = next(item for item in rows if item["platform_order_no"] == "111-1111111-1111111")
    assert row["original_system_order_no"] == "103700000000000001"
    assert row["workflow_status"] == "sku_adjustment_pending"
    assert row["last_error"] == "SKU API 明确拒绝"


def test_warehouse_result_detail_is_listed_and_pending_state_clears_only_warehouse_result(
    tmp_path,
):
    order_no = "111-6425622-6410611"
    original_system_order = "103725267407372040"
    split_system_orders = [
        "103725301788217856",
        "103725301788217857",
    ]
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    plan_input = {
        "platform_order_no": order_no,
        "system_order_no": original_system_order,
        "destination": {
            "raw_text": "United States, OH",
            "postal_code": None,
        },
    }
    store.mutate_legacy_record(
        order_no,
        lambda _current: {
            "platform_order_no": order_no,
            "system_order_no": original_system_order,
            "product_type": "tent",
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": True,
            "package_split_required": True,
            "package_split_complete": True,
            "package_split_system_order_nos": split_system_orders,
            "instruction_remark_required": False,
            "instruction_remark_complete": True,
            "warehouse_logistics_required": True,
            "warehouse_logistics_complete": True,
            "warehouse_logistics_status": "not_required",
            "warehouse_logistics_result_detail": "无需修改：旧的错误跳过结果",
            "warehouse_logistics_decisions": [{"status": "skip"}],
            "warehouse_logistics_write_results": [],
            "warehouse_logistics_plan_input": plan_input,
            "warehouse_logistics_postal_code": None,
            "processed_at": "2026-07-23 12:00:00",
        },
        event_type="test_initialized",
        actor="test",
    )

    summary = next(
        row
        for row in store.list_workflow_summaries()
        if row["platform_order_no"] == order_no
    )
    assert summary["result_detail"] == "无需修改：旧的错误跳过结果"

    store.set_stage_state(
        order_no,
        "warehouse_logistics",
        WorkflowStageState.PENDING,
        reason="修复后重新测试",
        actor="test",
    )

    workflow = store.get_workflow(order_no)
    stages = {row["stage"]: row for row in workflow["stages"]}
    assert workflow["workflow_status"] == "warehouse_logistics_pending"
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["folder"]["state"] == "COMPLETED"
    assert stages["sku"]["state"] == "COMPLETED"
    assert stages["package_split"]["state"] == "COMPLETED"
    assert stages["instruction_remark"]["state"] == "COMPLETED"
    assert stages["warehouse_logistics"]["state"] == "PENDING"
    assert stages["warehouse_logistics"]["last_error"] is None
    assert {
        row["system_order_no"] for row in workflow["system_orders"]
    } == {original_system_order, *split_system_orders}

    reopened_record = store.get_legacy_record(order_no)
    assert reopened_record["warehouse_logistics_plan_input"] == plan_input
    assert "warehouse_logistics_result_detail" not in reopened_record
    assert reopened_record["warehouse_logistics_complete"] is False
    assert "warehouse_logistics_decisions" not in reopened_record
    assert "warehouse_logistics_write_results" not in reopened_record
    assert reopened_record.get("processed_at") is None
    reopened_summary = next(
        row
        for row in store.list_workflow_summaries()
        if row["platform_order_no"] == order_no
    )
    assert reopened_summary["result_detail"] == ""


def test_workflow_summaries_expose_retry_review_lock_for_quick_selection(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    store.set_stage_state(
        "111-1111111-1111111",
        "sku",
        WorkflowStageState.PENDING,
        reason="准备复核",
    )
    store.record_workflow_paused(
        "111-1111111-1111111",
        "sku",
        reason="写入结果不明确",
        result_status="unknown",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )

    rows = store.list_workflow_summaries(limit=2000)

    row = next(item for item in rows if item["platform_order_no"] == "111-1111111-1111111")
    assert row["retry_confirmation_required"] == 1


def test_identity_backfill_fills_only_missing_metadata_and_preserves_stages(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload(), ensure_ascii=False), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "112-2222222-2222222"
    before = store.get_workflow(order_no)
    assert before is not None
    assert before["product_type"] is None
    before_stages = [dict(stage) for stage in before["stages"]]

    assert store.backfill_workflow_identity(
        order_no,
        system_order_no="999999999999999999",
        product_type="x_stands",
    )

    after = store.get_workflow(order_no)
    assert after is not None
    assert after["original_system_order_no"] == "103700000000000003"
    assert after["product_type"] == "x_stands"
    assert after["workflow_status"] == before["workflow_status"]
    assert after["stages"] == before_stages
    assert json.loads(after["source_record_json"])["product_type"] == "x_stands"
    event = store.history(order_no)[-1]
    assert event["event_type"] == "workflow_metadata_backfilled"
    assert json.loads(event["details_json"])["fields"] == ["product_type"]

    assert not store.backfill_workflow_identity(
        order_no,
        system_order_no="888888888888888888",
        product_type="tent",
    )
    unchanged = store.get_workflow(order_no)
    assert unchanged is not None
    assert unchanged["original_system_order_no"] == "103700000000000003"
    assert unchanged["product_type"] == "x_stands"


def test_buyer_cancel_marks_active_workflow_not_required_and_preserves_completed_stages(
    tmp_path,
):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "114-9578255-9785802"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103722237001371149",
            "product_type": "car_magnet",
            "contact_writeback_complete": True,
            "workflow_status": "folder_pending",
        },
        event_type="test_candidate",
        actor="test",
    )

    summary = store.mark_workflows_not_required(
        [order_no, "111-0000000-0000999"],
        reason="API 显示买家申请取消",
        actor="api_scanner",
    )

    assert summary.requested_count == 2
    assert summary.changed_order_count == 1
    assert summary.missing_count == 1
    assert order_no in store.processed_platform_orders()
    workflow = store.get_workflow(order_no)
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert workflow["not_required_reason"] == "buyer_cancel_requested"
    assert workflow["buyer_cancel_clear_streak"] == 0
    stages = {row["stage"]: row for row in workflow["stages"]}
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["folder"]["state"] == "NOT_REQUIRED"
    assert stages["folder"]["result_status"] == "buyer_cancel_requested"
    assert all(
        stages[stage]["state"] in {"COMPLETED", "NOT_REQUIRED", "NOT_APPLICABLE"}
        for stage in stages
    )

    repeated = store.mark_workflows_not_required(
        [order_no],
        reason="重复扫描仍显示买家申请取消",
        actor="api_scanner",
    )
    assert repeated.changed_order_count == 0
    assert repeated.already_terminal_count == 1


def _buyer_cancelled_workflow(store, order_no: str) -> dict:
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000777",
            "product_type": "tent",
            "last_seen_at": "2026-07-20T08:00:00Z",
            "contact_writeback_complete": True,
            "folder_complete": False,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "package_split_required": False,
            "package_split_complete": False,
            "workflow_status": "folder_pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.mark_workflows_not_required(
        [order_no],
        reason="领星订单状态显示买家申请取消。",
        actor="api_scanner",
    )
    workflow = store.get_workflow(order_no)
    assert workflow is not None
    return workflow


def test_buyer_cancel_clear_requires_two_distinct_complete_scans_and_reopens_only_cancelled_stages(
    tmp_path,
):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "701-4689510-2891447"
    before = _buyer_cancelled_workflow(store, order_no)
    before_stages = {row["stage"]: row for row in before["stages"]}
    assert before_stages["contact"]["state"] == "COMPLETED"
    assert before_stages["folder"]["result_status"] == "buyer_cancel_requested"
    assert before_stages["sku"]["result_status"] == "buyer_cancel_requested"
    assert before_stages["package_split"]["state"] == "NOT_REQUIRED"
    assert before_stages["package_split"]["result_status"] is None
    assert before_stages["instruction_remark"]["state"] == "NOT_APPLICABLE"

    first = store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-scan-001",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )
    duplicate = store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-scan-001",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )

    assert first.clear_observed_order_nos == (order_no,)
    assert first.reactivated_count == 0
    assert duplicate.clear_observed_count == 0
    assert duplicate.reactivated_count == 0
    waiting = store.get_workflow(order_no)
    assert waiting is not None
    assert waiting["workflow_status"] == "not_required"
    assert waiting["buyer_cancel_clear_streak"] == 1
    assert waiting["buyer_cancel_clear_last_scan_id"] == "complete-scan-001"

    second = store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-scan-002",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )

    assert second.reactivated_order_nos == (order_no,)
    restored = store.get_workflow(order_no)
    assert restored is not None
    assert restored["workflow_status"] == "folder_pending"
    assert restored["processed_at"] is None
    assert restored["not_required_reason"] is None
    assert restored["buyer_cancel_clear_streak"] == 0
    restored_stages = {row["stage"]: row for row in restored["stages"]}
    assert restored_stages["contact"]["state"] == "COMPLETED"
    assert restored_stages["folder"]["state"] == "PENDING"
    assert restored_stages["folder"]["required"] == 1
    assert restored_stages["sku"]["state"] == "PENDING"
    assert restored_stages["sku"]["required"] == 1
    assert restored_stages["package_split"]["state"] == "NOT_REQUIRED"
    assert restored_stages["package_split"]["required"] == 0
    assert restored_stages["instruction_remark"]["state"] == "NOT_APPLICABLE"
    assert order_no not in store.processed_platform_orders()
    event_types = [event["event_type"] for event in store.history(order_no)]
    assert event_types.count("buyer_cancel_clear_observed") == 1
    assert event_types.count("stage_auto_reopened") == 2
    assert event_types[-1] == "workflow_auto_reactivated"


@pytest.mark.parametrize(
    ("snapshots_complete", "eligible", "cancelled"),
    [
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_buyer_cancel_clear_confirmation_resets_when_scan_is_unsafe(
    tmp_path,
    snapshots_complete,
    eligible,
    cancelled,
):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "701-4689510-2891447"
    _buyer_cancelled_workflow(store, order_no)
    store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-scan-001",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )

    reset = store.reconcile_buyer_cancel_reactivation(
        scan_id="scan-002",
        eligible_order_nos=[order_no] if eligible else [],
        currently_cancelled_order_nos=[order_no] if cancelled else [],
        snapshots_complete=snapshots_complete,
    )

    assert reset.reset_order_nos == (order_no,)
    workflow = store.get_workflow(order_no)
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert workflow["buyer_cancel_clear_streak"] == 0
    assert workflow["buyer_cancel_clear_last_scan_id"] is None
    next_complete = store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-scan-003",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )
    assert next_complete.clear_observed_count == 1
    assert next_complete.reactivated_count == 0


def test_existing_sqlite_schema_is_migrated_and_buyer_cancel_reason_is_backfilled(
    tmp_path,
):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE custom_order_workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_order_no TEXT NOT NULL UNIQUE,
                original_system_order_no TEXT,
                product_type TEXT,
                workflow_status TEXT NOT NULL,
                ignored INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT,
                processed_at TEXT,
                source_record_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE custom_order_stages (
                workflow_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                required INTEGER,
                state TEXT NOT NULL,
                result_status TEXT,
                completed_at TEXT,
                last_error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (workflow_id, stage)
            );
            INSERT INTO custom_order_workflows(
                id, platform_order_no, workflow_status, processed_at,
                created_at, updated_at
            ) VALUES (
                1, '701-4689510-2891447', 'not_required',
                '2026-07-20T08:00:00Z', '2026-07-20T08:00:00Z',
                '2026-07-20T08:00:00Z'
            );
            INSERT INTO custom_order_stages(
                workflow_id, stage, required, state, result_status
            ) VALUES (
                1, 'folder', 0, 'NOT_REQUIRED', 'buyer_cancel_requested'
            );
            """
        )

    store = CustomWorkflowStore(database)
    store.initialize()

    workflow = store.get_workflow("701-4689510-2891447")
    assert workflow is not None
    assert workflow["not_required_reason"] == "buyer_cancel_requested"
    assert workflow["buyer_cancel_clear_streak"] == 0
    assert workflow["buyer_cancel_clear_last_scan_id"] is None
    with store.connect() as conn:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(custom_order_workflows)")
        }
    assert {
        "not_required_reason",
        "buyer_cancel_clear_streak",
        "buyer_cancel_clear_last_scan_id",
        "buyer_cancel_clear_last_seen_at",
    } <= columns


def test_missing_candidate_folder_reconciliation_completes_clean_and_preserves_blocked(tmp_path):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    found_order = "111-0000000-0000101"
    absent_order = "112-0000000-0000102"
    seen_at = "2026-07-15T08:00:00Z"

    store.mutate_legacy_record(
        found_order,
        lambda _old: {
            "platform_order_no": found_order,
            "system_order_no": "103700000000000101",
            "last_seen_at": seen_at,
            "contact_writeback_complete": True,
            "folder_complete": False,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "workflow_status": "folder_pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.mutate_legacy_record(
        absent_order,
        lambda _old: {
            "platform_order_no": absent_order,
            "system_order_no": "103700000000000102",
            "last_seen_at": seen_at,
            "contact_writeback_complete": True,
            "folder_complete": True,
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "workflow_status": "sku_adjustment_pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.set_stage_state(
        absent_order,
        "sku",
        WorkflowStageState.BLOCKED,
        reason="人工暂停等待检查",
        actor="desktop_user",
    )
    protected_active = {
        row["platform_order_no"]: row for row in store.list_active_scanned_workflows()
    }
    assert protected_active[absent_order]["folder_reconciliation_protection_codes"] == (
        "manual_blocked",
    )
    absent_before = store.get_workflow(absent_order)
    absent_history_before = store.history(absent_order)

    summary = store.reconcile_missing_candidate_folders(
        {found_order: True, absent_order: False},
        reason="下一轮完整快照中不再是候选，核对订单文件夹",
    )

    assert summary.requested_count == 2
    assert summary.completed_count == 1
    assert summary.pending_count == 0
    assert summary.changed_order_count == 1
    assert summary.error_preserved_count == 1
    found = store.get_workflow(found_order)
    assert found is not None
    assert found["workflow_status"] == "completed"
    found_stages = {row["stage"]: row for row in found["stages"]}
    assert found_stages["contact"]["state"] == "COMPLETED"
    assert found_stages["folder"]["state"] == "COMPLETED"
    assert found_stages["sku"]["state"] == "COMPLETED"
    assert found_stages["folder"]["result_status"] == "folder_reconciled"

    absent = store.get_workflow(absent_order)
    assert absent == absent_before
    assert store.history(absent_order) == absent_history_before
    absent_stages = {row["stage"]: row for row in absent["stages"]}
    assert absent_stages["sku"]["state"] == "BLOCKED"


@pytest.mark.parametrize("folder_exists", [False, True])
def test_missing_candidate_folder_reconciliation_preserves_existing_errors(
    tmp_path,
    folder_exists,
):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "113-0000000-0000104"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000104",
            "last_seen_at": "2026-07-15T08:00:00Z",
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="联系方式写回读回失败",
        actor="desktop_worker",
        result_status="readback_failed",
        last_error="用于验证必须原样保留的错误",
    )
    before = store.get_workflow(order_no)
    history_before = store.history(order_no)

    first = store.reconcile_missing_candidate_folders(
        {order_no: folder_exists},
        reason="下一轮候选消失",
    )
    second = store.reconcile_missing_candidate_folders(
        {order_no: folder_exists},
        reason="重复扫描仍然候选消失",
    )

    assert first.error_preserved_count == second.error_preserved_count == 1
    assert first.changed_order_count == second.changed_order_count == 0
    assert store.get_workflow(order_no) == before
    assert store.history(order_no) == history_before
    active = store.list_active_scanned_workflows()
    assert active[0]["folder_reconciliation_protected"] is True
    assert active[0]["folder_reconciliation_protection_codes"] == ("existing_error",)


def test_manual_reopen_clears_folder_reconciliation_protection(tmp_path):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "114-0000000-0000105"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000105",
            "last_seen_at": "2026-07-15T08:00:00Z",
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.record_workflow_paused(
        order_no,
        "contact",
        reason="API 写入结果不明确",
        result_status="manual_review",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE custom_order_stages
            SET last_error = NULL
            WHERE workflow_id = (
                SELECT id FROM custom_order_workflows WHERE platform_order_no = ?
            ) AND stage = 'contact'
            """,
            (order_no,),
        )
        conn.commit()
    protected = store.list_active_scanned_workflows()[0]
    assert protected["folder_reconciliation_protection_codes"] == (
        "retry_review_required",
    )

    store.reopen_from_stage(
        order_no,
        "contact",
        reason="人工核对后允许重新处理",
        actor="desktop_user",
    )
    reopened = store.list_active_scanned_workflows()[0]
    assert reopened["folder_reconciliation_protected"] is False

    summary = store.reconcile_missing_candidate_folders(
        {order_no: True},
        reason="人工清除错误后重新参与对账",
    )
    assert summary.completed_count == 1
    assert summary.error_preserved_count == 0
    assert store.get_workflow(order_no)["workflow_status"] == "completed"


def test_missing_candidate_folder_absence_is_idempotent_when_already_pending(tmp_path):
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    order_no = "113-0000000-0000103"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000103",
            "last_seen_at": "2026-07-15T08:00:00Z",
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )

    before_history_count = len(store.history(order_no))
    first = store.reconcile_missing_candidate_folders(
        {order_no: False},
        reason="文件夹不存在，继续待处理",
    )
    second = store.reconcile_missing_candidate_folders(
        {order_no: False},
        reason="文件夹仍不存在，继续待处理",
    )

    assert first.pending_count == second.pending_count == 1
    assert first.changed_order_count == second.changed_order_count == 0
    assert len(store.history(order_no)) == before_history_count
    assert [row["platform_order_no"] for row in store.list_active_scanned_workflows()] == [
        order_no
    ]


def test_reopen_cascades_and_records_reasoned_events(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)

    store.reopen_from_stage(
        "111-1111111-1111111",
        "sku",
        reason="重新核验帐篷配件",
    )

    workflow = store.get_workflow("111-1111111-1111111")
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["folder"]["state"] == "COMPLETED"
    assert stages["sku"]["state"] == "PENDING"
    assert stages["package_split"]["state"] == "PENDING"
    assert stages["instruction_remark"]["state"] == "PENDING"
    assert workflow["workflow_status"] == "sku_adjustment_pending"
    assert {event["reason"] for event in store.history("111-1111111-1111111")} == {
        "重新核验帐篷配件"
    }


def test_manual_state_change_requires_reason_and_export_remains_legacy_readable(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)

    with pytest.raises(ValueError, match="必须填写原因"):
        store.set_stage_state(
            "111-1111111-1111111",
            "sku",
            WorkflowStageState.PENDING,
            reason="",
        )

    store.set_stage_state(
        "111-1111111-1111111",
        "sku",
        WorkflowStageState.COMPLETED,
        reason="人工核验完成",
        result_status="manual",
    )
    exported = store.export_legacy_json(tmp_path / "compat.json")

    assert exported.exists()
    assert load_processed_platform_orders(exported) == {
        "111-1111111-1111111",
        "112-2222222-2222222",
    }


def test_ambiguous_write_stays_pending_until_review_is_resolved(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)

    store.set_stage_state(
        "111-1111111-1111111",
        "sku",
        WorkflowStageState.PENDING,
        reason="准备验证不明确写入",
    )
    pause = store.record_workflow_paused(
        "111-1111111-1111111",
        "sku",
        reason="请先读回 ERP 状态。",
        result_status="manual_review",
        pause_kind=WorkflowPauseKind.AMBIGUOUS_WRITE,
    )
    assert pause.stage == "sku"
    assert pause.retry_confirmation_required is True
    pending = store.get_workflow("111-1111111-1111111")
    pending_stages = {item["stage"]: item for item in pending["stages"]}
    assert pending["workflow_status"] == "sku_adjustment_pending"
    assert pending_stages["sku"]["state"] == "PENDING"
    assert pending_stages["sku"]["last_error"] == "请先读回 ERP 状态。"
    assert store.get_pending_retry_review("111-1111111-1111111")["stage"] == "sku"

    status = store.resolve_stage_retry_review(
        "111-1111111-1111111",
        "sku",
        StageRetryReviewResolution.RETRY,
        reason="已人工读回确认 API 未执行",
    )
    assert status == "sku_adjustment_pending"
    assert store.get_pending_retry_review("111-1111111-1111111") is None


def test_only_manual_actor_can_block_and_rejected_automation_is_audited(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "111-1111111-1111111"

    with pytest.raises(PermissionError, match="只能由用户手动设置"):
        store.set_stage_state(
            order_no,
            "sku",
            WorkflowStageState.BLOCKED,
            reason="自动任务错误地请求阻止",
            actor="desktop_worker",
        )

    workflow = store.get_workflow(order_no)
    sku = next(item for item in workflow["stages"] if item["stage"] == "sku")
    assert sku["state"] == "COMPLETED"
    assert store.history(order_no)[-1]["event_type"] == "automatic_block_rejected"


def test_historical_automatic_block_repair_preserves_manual_block_and_is_idempotent(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    automatic_order = "111-1111111-1111111"
    manual_order = "112-2222222-2222222"
    store.set_stage_state(
        manual_order,
        "contact",
        WorkflowStageState.BLOCKED,
        reason="用户明确阻止",
        actor="desktop_user",
    )
    with store.connect() as conn:
        workflow_id = conn.execute(
            "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
            (automatic_order,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE custom_order_stages
            SET state = 'BLOCKED', result_status = 'manual_review',
                last_error = 'API 写入结果 unknown'
            WHERE workflow_id = ? AND stage = 'sku'
            """,
            (workflow_id,),
        )
        store._insert_event(
            conn,
            workflow_id,
            stage="sku",
            event_type="stage_state_changed",
            old_state="PENDING",
            new_state="BLOCKED",
            actor="desktop_worker",
            reason="旧版自动阻止",
        )

    assert store.repair_automated_blocked_stages() == 1
    assert store.repair_automated_blocked_stages() == 0
    repaired = store.get_workflow(automatic_order)
    repaired_sku = next(item for item in repaired["stages"] if item["stage"] == "sku")
    assert repaired_sku["state"] == "PENDING"
    assert store.get_pending_retry_review(automatic_order)["stage"] == "sku"
    manual = store.get_workflow(manual_order)
    manual_contact = next(item for item in manual["stages"] if item["stage"] == "contact")
    assert manual_contact["state"] == "BLOCKED"


@pytest.mark.parametrize(
    ("stage", "expected_status"),
    [
        ("contact", "pending"),
        ("folder", "folder_pending"),
        ("sku", "sku_adjustment_pending"),
        ("package_split", "package_split_pending"),
        ("instruction_remark", "instruction_remark_pending"),
        ("warehouse_logistics", "warehouse_logistics_pending"),
    ],
)
def test_pause_status_tracks_each_current_stage_and_preserves_prior_completion(
    tmp_path, stage, expected_status
):
    order_no = "111-9999999-9999999"
    stage_order = [
        "contact",
        "folder",
        "sku",
        "package_split",
        "instruction_remark",
        "warehouse_logistics",
    ]
    current_index = stage_order.index(stage)
    record = {
        "platform_order_no": order_no,
        "contact_writeback_complete": current_index > 0,
        "folder_complete": current_index > 1,
        "sku_adjustment_required": True,
        "sku_adjustment_complete": current_index > 2,
        "package_split_required": True,
        "package_split_complete": current_index > 3,
        "instruction_remark_required": True,
        "instruction_remark_complete": current_index > 4,
        "warehouse_logistics_required": True,
        "warehouse_logistics_complete": False,
    }
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.mutate_legacy_record(
        order_no,
        lambda _current: record,
        event_type="test_initialized",
        actor="test",
    )

    store.record_workflow_paused(
        order_no,
        stage,
        reason=f"{stage} 阶段普通失败",
        result_status="failed",
        pause_kind=WorkflowPauseKind.RETRYABLE_FAILURE,
    )

    workflow = store.get_workflow(order_no)
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert workflow["workflow_status"] == expected_status
    assert stages[stage]["state"] == "PENDING"
    for prior_stage in stage_order[:current_index]:
        assert stages[prior_stage]["state"] == "COMPLETED"


def test_later_sku_pause_does_not_restore_unverified_contact_after_manual_reopen(
    tmp_path,
):
    """人工重开必须作废旧 written 标记，后续 SKU 结果不能把它恢复成完成。"""

    order_no = "113-9130699-3238645"
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.mutate_legacy_record(
        order_no,
        lambda _current: {
            "platform_order_no": order_no,
            "system_order_no": "103726971468103718",
            "product_type": "tent",
            "contact_writeback_complete": True,
            "contact_status": "written",
            "contact_completed_at": "2026-07-28 08:22:18",
            "folder_complete": True,
            "folder_completed_at": "2026-07-28 08:22:20",
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "workflow_status": "sku_adjustment_pending",
        },
        event_type="test_initialized",
        actor="test",
    )
    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="人工重新放回联系方式待处理",
        actor="desktop_user",
    )

    before = store.get_workflow(order_no)
    before_stages = {item["stage"]: item for item in before["stages"]}
    assert before["workflow_status"] == "pending"
    assert before_stages["contact"]["state"] == "PENDING"
    assert before_stages["sku"]["state"] == "PENDING"

    pause = store.record_workflow_paused(
        order_no,
        "sku",
        reason="联系方式和文件夹已完成，但用户取消 SKU 调整。",
        result_status="updated_folder_created_sku_failed",
        pause_kind=WorkflowPauseKind.USER_CANCELLED,
        actor="desktop_worker",
    )

    workflow = store.get_workflow(order_no)
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert pause.stage == "sku"
    assert pause.workflow_status == "pending"
    assert workflow["workflow_status"] == "pending"
    assert stages["contact"]["state"] == "PENDING"
    assert stages["contact"]["result_status"] is None
    assert stages["contact"]["completed_at"] is None
    assert stages["folder"]["state"] == "COMPLETED"
    assert stages["sku"]["state"] == "PENDING"
    source_record = json.loads(workflow["source_record_json"])
    assert "contact_writeback_complete" not in source_record
    assert "contact_status" not in source_record
    assert "contact_completed_at" not in source_record
    reconciliation = [
        event
        for event in store.history(order_no)
        if event["event_type"] == "stage_checkpoint_reconciled"
    ]
    assert reconciliation == []


def test_batch_manual_completion_preserves_terminal_semantics_and_audits(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "111-1111111-1111111"

    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="等待人工处理",
    )
    store.set_stage_state(
        order_no,
        "folder",
        WorkflowStageState.BLOCKED,
        reason="文件夹需人工核验",
        result_status="manual_review",
        last_error="历史阻塞错误",
    )
    store.set_stage_state(
        order_no,
        "package_split",
        WorkflowStageState.NOT_REQUIRED,
        reason="无需拆包",
    )
    store.set_stage_state(
        order_no,
        "instruction_remark",
        WorkflowStageState.NOT_APPLICABLE,
        reason="不适用说明书备注",
    )
    with store.connect() as conn:
        workflow_id = conn.execute(
            "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
            (order_no,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE custom_order_stages SET metadata_json = ?
            WHERE workflow_id = ? AND stage = 'contact'
            """,
            ('{"preserved":true}', workflow_id),
        )

    summary = store.mark_workflows_manually_completed(
        [order_no, "112-2222222-2222222", order_no],
        reason="人工线下处理完成",
        actor="desktop_user",
    )

    assert summary.requested_count == 2
    assert summary.completed_count == 1
    assert summary.already_completed_count == 1
    assert summary.changed_stage_count == 2
    workflow = store.get_workflow(order_no)
    assert workflow is not None
    assert workflow["workflow_status"] == "completed"
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert stages["contact"]["state"] == "COMPLETED"
    assert stages["contact"]["result_status"] == "manual"
    assert stages["contact"]["completed_at"]
    assert stages["contact"]["metadata_json"] == '{"preserved":true}'
    assert stages["folder"]["state"] == "COMPLETED"
    assert stages["folder"]["last_error"] is None
    assert stages["package_split"]["state"] == "NOT_REQUIRED"
    assert stages["package_split"]["required"] == 0
    assert stages["instruction_remark"]["state"] == "NOT_APPLICABLE"
    assert stages["instruction_remark"]["required"] is None
    batch_events = [
        event
        for event in store.history(order_no)
        if event["reason"] == "人工线下处理完成"
    ]
    assert [event["stage"] for event in batch_events[:-1]] == ["contact", "folder"]
    assert batch_events[-1]["event_type"] == "workflow_manually_completed"
    assert batch_events[-1]["old_state"] == "blocked"
    details = json.loads(batch_events[-1]["details_json"])
    assert details["changed_stages"] == ["contact", "folder"]
    assert details["preserved_stage_states"]["package_split"] == "NOT_REQUIRED"
    assert order_no in store.processed_platform_orders()


def test_batch_manual_completion_validates_before_writing_and_rolls_back(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "111-1111111-1111111"
    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="等待人工处理",
    )

    with pytest.raises(ValueError, match="必须填写原因"):
        store.mark_workflows_manually_completed([order_no], reason="")
    with pytest.raises(ValueError, match="找不到定制订单"):
        store.mark_workflows_manually_completed(
            [order_no, "missing-order"],
            reason="人工完成",
        )
    with store.connect() as conn:
        second_id = conn.execute(
            """
            SELECT id FROM custom_order_workflows
            WHERE platform_order_no = '112-2222222-2222222'
            """
        ).fetchone()[0]
        conn.execute(
            """
            DELETE FROM custom_order_stages
            WHERE workflow_id = ? AND stage = 'instruction_remark'
            """,
            (second_id,),
        )
    with pytest.raises(ValueError, match="阶段数据不完整"):
        store.mark_workflows_manually_completed(
            [order_no, "112-2222222-2222222"],
            reason="人工完成",
        )

    workflow = store.get_workflow(order_no)
    stages = {item["stage"]: item for item in workflow["stages"]}
    assert stages["contact"]["state"] == "PENDING"
    assert workflow["workflow_status"] == "pending"


def test_batch_stage_state_update_supports_every_state_dedupes_and_skips_noops(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_nos = ["111-1111111-1111111", "112-2222222-2222222"]

    expected_required = {
        WorkflowStageState.PENDING: 1,
        WorkflowStageState.COMPLETED: 1,
        WorkflowStageState.NOT_REQUIRED: 0,
        WorkflowStageState.NOT_APPLICABLE: None,
        WorkflowStageState.BLOCKED: 1,
    }
    for state in expected_required:
        summary = store.set_stage_states_for_workflows(
            [order_nos[0], order_nos[1], order_nos[0]],
            "contact",
            state,
            reason=f"批量修改为 {state}",
            actor="desktop_user",
        )
        assert summary.requested_count == 2
        assert summary.changed_order_count == 2
        assert summary.unchanged_order_count == 0
        assert summary.changed_stage_count == 2
        for order_no in order_nos:
            workflow = store.get_workflow(order_no)
            contact = next(item for item in workflow["stages"] if item["stage"] == "contact")
            assert contact["state"] == str(state)
            assert contact["required"] == expected_required[state]
            assert bool(contact["completed_at"]) == (state == WorkflowStageState.COMPLETED)
            assert contact["last_error"] is None

    history_count = len(store.history(order_nos[0]))
    unchanged = store.set_stage_states_for_workflows(
        order_nos,
        "contact",
        WorkflowStageState.BLOCKED,
        reason="重复设置阻塞",
    )
    assert unchanged.changed_order_count == 0
    assert unchanged.unchanged_order_count == 2
    assert len(store.history(order_nos[0])) == history_count
    latest_change = store.history(order_nos[0])[-1]
    assert json.loads(latest_change["details_json"])["source"] == "manual_batch_stage_update"


def test_batch_stage_state_update_validates_all_orders_before_writing(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    order_no = "111-1111111-1111111"

    with pytest.raises(ValueError, match="找不到定制订单"):
        store.set_stage_states_for_workflows(
            [order_no, "missing-order"],
            "contact",
            WorkflowStageState.PENDING,
            reason="整批应回滚",
        )
    workflow = store.get_workflow(order_no)
    contact = next(item for item in workflow["stages"] if item["stage"] == "contact")
    assert contact["state"] == "COMPLETED"

    with store.connect() as conn:
        second_id = conn.execute(
            "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
            ("112-2222222-2222222",),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM custom_order_stages WHERE workflow_id = ? AND stage = 'folder'",
            (second_id,),
        )
    with pytest.raises(ValueError, match="阶段数据不完整"):
        store.set_stage_states_for_workflows(
            [order_no, "112-2222222-2222222"],
            "contact",
            WorkflowStageState.PENDING,
            reason="阶段异常时回滚",
        )
    workflow = store.get_workflow(order_no)
    contact = next(item for item in workflow["stages"] if item["stage"] == "contact")
    assert contact["state"] == "COMPLETED"


def test_batch_reopen_is_atomic_cascades_and_preserves_not_applicable(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)
    first = "111-1111111-1111111"
    second = "112-2222222-2222222"

    summary = store.reopen_workflows_from_stage(
        [first, second, first],
        "sku",
        reason="批量重新核验",
        actor="desktop_user",
    )

    assert summary.requested_count == 2
    assert summary.changed_order_count == 1
    assert summary.unchanged_order_count == 1
    assert summary.changed_stage_count == 3
    first_workflow = store.get_workflow(first)
    first_stages = {item["stage"]: item for item in first_workflow["stages"]}
    assert first_stages["contact"]["state"] == "COMPLETED"
    assert first_stages["folder"]["state"] == "COMPLETED"
    assert first_stages["sku"]["state"] == "PENDING"
    assert first_stages["package_split"]["state"] == "PENDING"
    assert first_stages["instruction_remark"]["state"] == "PENDING"
    second_workflow = store.get_workflow(second)
    second_stages = {item["stage"]: item for item in second_workflow["stages"]}
    assert second_stages["sku"]["state"] == "NOT_APPLICABLE"
    reopen_events = [
        event for event in store.history(first) if event["event_type"] == "stage_reopened"
    ]
    assert [event["stage"] for event in reopen_events] == [
        "sku",
        "package_split",
        "instruction_remark",
    ]
    assert json.loads(reopen_events[0]["details_json"])["source"] == "manual_batch_reopen"

    before_states = {stage: item["state"] for stage, item in first_stages.items()}
    with pytest.raises(ValueError, match="找不到定制订单"):
        store.reopen_workflows_from_stage(
            [first, "missing-order"],
            "folder",
            reason="整批应回滚",
        )
    current = store.get_workflow(first)
    assert {item["stage"]: item["state"] for item in current["stages"]} == before_states


def test_upgrade_adds_warehouse_stage_without_reopening_historical_completion(tmp_path):
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(_legacy_payload(), ensure_ascii=False), encoding="utf-8")
    database = tmp_path / "workflow.sqlite3"
    store = CustomWorkflowStore(database)
    store.import_legacy_json(source)
    order_no = "111-1111111-1111111"

    with store.connect() as conn:
        workflow_id = conn.execute(
            "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
            (order_no,),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM custom_order_stages WHERE workflow_id = ? AND stage = 'warehouse_logistics'",
            (workflow_id,),
        )

    upgraded = CustomWorkflowStore(database)
    workflow = upgraded.get_workflow(order_no)
    stages = {row["stage"]: row for row in workflow["stages"]}

    assert stages["warehouse_logistics"]["state"] == "NOT_APPLICABLE"
    assert stages["warehouse_logistics"]["required"] is None
    assert workflow["workflow_status"] == "completed"
    assert order_no in upgraded.processed_platform_orders()
