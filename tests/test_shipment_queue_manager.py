from shipment_automation.models import (
    IDENTITY_PAUSED_TAG_REMOVED,
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_manager import _available_actions, _identity_status_text, run_interactive_queue_manager
from shipment_automation.queue_store import ShipmentWorkflowStore


def _blocked_mismatch_store(tmp_path) -> tuple[ShipmentWorkflowStore, str]:
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    logistics_no = "ALS01798551368"
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no="103714933869767207",
            platform_order_no="114-1416477-4543451",
            logistics_no=logistics_no,
            shipment_tag_name="自动标发",
        )
    )
    store.complete_logistics_attempt(
        logistics_no,
        LogisticsDetail(
            logistics_no=logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error="国际物流单号与承运商不匹配：FEDEX / 1Z9253126709651051，请在队列管理中人工确认。",
    )
    return store, logistics_no


def _scripted_input(responses: list[str]):
    answers = iter(responses)
    return lambda _prompt: next(answers)


def test_jycp_intermediary_number_does_not_offer_manual_tracking_review_actions():
    actions = _available_actions(
        {
            "identity_state": "ACTIVE",
            "logistics_state": LOGISTICS_BLOCKED,
            "logistics_no": "ALS01798551368",
            "carrier": "FedEx",
            "international_tracking_no": "JYCP00000093286",
            "logistics_last_error": (
                "国际物流单号与承运商不匹配：FEDEX / JYCP00000093286，请审核后选择处理方式。"
            ),
            "erp_state": "WAITING",
        }
    )

    action_names = {action for action, _label in actions}
    assert "auto-recheck-tracking" not in action_names
    assert "tracking-order-issue" not in action_names
    assert "confirm-tracking" not in action_names


def test_queue_manager_manually_confirms_only_current_tracking_pair(tmp_path):
    store, logistics_no = _blocked_mismatch_store(tmp_path)
    output: list[str] = []

    exit_code = run_interactive_queue_manager(
        store,
        input_func=_scripted_input(["1", "3", "y"]),
        output_func=output.append,
    )

    row = store.get_by_logistics_no(logistics_no)
    assert exit_code == 0
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["tracking_override_carrier"] == "FEDEX"
    assert row["tracking_override_no"] == "1Z9253126709651051"
    assert "确认当前单号并允许进入 ERP" in "\n".join(output)
    assert "该确认仅对以上承运商与单号组合有效" in "\n".join(output)


def test_queue_manager_non_y_confirmation_keeps_job_retryable(tmp_path):
    store, logistics_no = _blocked_mismatch_store(tmp_path)
    output: list[str] = []

    exit_code = run_interactive_queue_manager(
        store,
        input_func=_scripted_input(["1", "3", "n", "0", "0"]),
        output_func=output.append,
    )

    row = store.get_by_logistics_no(logistics_no)
    assert exit_code == 0
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["tracking_override_at"] is None
    assert "已取消，本次未修改队列" in "\n".join(output)


def test_queue_manager_can_enable_automatic_recheck(tmp_path):
    store, logistics_no = _blocked_mismatch_store(tmp_path)

    exit_code = run_interactive_queue_manager(
        store,
        input_func=_scripted_input(["1", "1", "y", "0"]),
        output_func=lambda _text: None,
    )

    row = store.get_by_logistics_no(logistics_no)
    assert exit_code == 0
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_AUTO_RECHECK
    assert row["logistics_next_attempt_at"]


def test_queue_manager_can_mark_order_issue(tmp_path):
    store, logistics_no = _blocked_mismatch_store(tmp_path)

    exit_code = run_interactive_queue_manager(
        store,
        input_func=_scripted_input(["1", "2", "y", "0"]),
        output_func=lambda _text: None,
    )

    row = store.get_by_logistics_no(logistics_no)
    assert exit_code == 0
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_ORDER_ISSUE
    assert row["logistics_next_attempt_at"] is None


def test_queue_manager_labels_auto_pause_and_only_allows_manual_cancel(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    candidate = ShipmentCandidate(
        system_order_no="SYS-PAUSED",
        platform_order_no="ORDER-PAUSED",
        logistics_no="ALS-PAUSED",
        shipment_tag_name="帐篷标发",
    )
    store.upsert_candidate(candidate)
    store.reconcile_shipment_tag_snapshot(
        {candidate.system_order_no: False},
        snapshot_complete=True,
    )
    item = store.get_by_logistics_no(candidate.logistics_no)

    assert item["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
    assert _identity_status_text(item) == "标签已移除/自动暂停"
    item["email_state"] = "PENDING"
    assert _available_actions(item) == [("cancel", "取消自动标发任务")]
