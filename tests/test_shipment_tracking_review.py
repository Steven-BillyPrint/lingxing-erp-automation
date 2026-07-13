import pytest

from shipment_automation.alibaba_logistics import tracking_number_mismatch_reason
from shipment_automation.models import (
    ERP_PENDING,
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentWorkflowStore
from shipment_automation.tracking_review import review_pending_tracking_mismatches


def _add_mismatch(store: ShipmentWorkflowStore, suffix: str = "1") -> str:
    logistics_no = f"ALS0179855136{suffix}"
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no=f"10371493386976720{suffix}",
            platform_order_no=f"114-1416477-454345{suffix}",
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
            international_tracking_no=f"JYCP0000009328{suffix}",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason("FedEx", f"JYCP0000009328{suffix}"),
    )
    return logistics_no


@pytest.mark.parametrize(
    ("answer", "expected_action", "expected_state"),
    [
        ("1", TRACKING_REVIEW_AUTO_RECHECK, LOGISTICS_BLOCKED),
        ("2", TRACKING_REVIEW_ORDER_ISSUE, LOGISTICS_BLOCKED),
        ("3", None, LOGISTICS_READY),
    ],
)
def test_first_tracking_mismatch_review_persists_each_choice(
    tmp_path, answer, expected_action, expected_state
):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    logistics_no = _add_mismatch(store)
    output: list[str] = []

    summary = review_pending_tracking_mismatches(
        store,
        input_func=lambda _prompt: answer,
        output_func=output.append,
    )

    row = store.get_by_logistics_no(logistics_no)
    assert summary.reviewed_count == 1
    assert row["tracking_mismatch_action"] == expected_action
    assert row["logistics_state"] == expected_state
    if answer == "1":
        assert row["logistics_next_attempt_at"]
        assert summary.auto_recheck_count == 1
    elif answer == "2":
        assert row["logistics_next_attempt_at"] is None
        assert summary.order_issue_count == 1
    else:
        assert row["erp_state"] == ERP_PENDING
        assert row["tracking_override_no"] == "JYCP00000093281"
        assert summary.confirmed_count == 1
    assert "1. 中间商单号" in "\n".join(output)
    assert "2. 订单有问题" in "\n".join(output)
    assert "3. 确认当前单号" in "\n".join(output)


def test_reviewed_mismatch_is_not_prompted_again(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    _add_mismatch(store)
    review_pending_tracking_mismatches(store, input_func=lambda _prompt: "1", output_func=lambda _text: None)

    prompts = []
    summary = review_pending_tracking_mismatches(
        store,
        input_func=lambda prompt: prompts.append(prompt) or "2",
        output_func=lambda _text: None,
    )

    assert prompts == []
    assert summary.reviewed_count == 0


def test_invalid_input_and_eof_defer_multiple_reviews_without_stopping_batch(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _add_mismatch(store, "1")
    second = _add_mismatch(store, "2")
    responses = iter(["invalid"])

    def input_func(_prompt):
        try:
            return next(responses)
        except StopIteration as exc:
            raise EOFError from exc

    summary = review_pending_tracking_mismatches(
        store,
        input_func=input_func,
        output_func=lambda _text: None,
    )

    assert summary.deferred_count == 2
    assert store.get_by_logistics_no(first)["tracking_mismatch_action"] is None
    assert store.get_by_logistics_no(second)["tracking_mismatch_action"] is None
