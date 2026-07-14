from __future__ import annotations

import json

import pytest

from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState
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


def test_ambiguous_write_blocks_workflow_until_reasoned_reopen(tmp_path):
    source = tmp_path / "processed_platform_orders.json"
    source.write_text(json.dumps(_legacy_payload()), encoding="utf-8")
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)

    store.set_stage_state(
        "111-1111111-1111111",
        "sku",
        WorkflowStageState.BLOCKED,
        reason="API 写入结果不明确，禁止自动重试。",
        actor="desktop_worker",
        result_status="manual_review",
        last_error="请先读回 ERP 状态。",
    )

    blocked = store.get_workflow("111-1111111-1111111")
    blocked_stages = {item["stage"]: item for item in blocked["stages"]}
    assert blocked["workflow_status"] == "blocked"
    assert blocked_stages["sku"]["state"] == "BLOCKED"
    assert blocked_stages["sku"]["last_error"] == "请先读回 ERP 状态。"

    store.reopen_from_stage(
        "111-1111111-1111111",
        "sku",
        reason="已人工读回确认 API 未执行",
    )
    reopened = store.get_workflow("111-1111111-1111111")
    assert reopened["workflow_status"] == "sku_adjustment_pending"
