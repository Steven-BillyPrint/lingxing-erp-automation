from erp_automation.contracts.controller import ControlResult, TaskSubmissionReceipt
from erp_automation.contracts.operation_feedback import operation_rejection

import pytest


@pytest.mark.parametrize("details", [{}, {"non_modal": True}])
@pytest.mark.parametrize("reason", [
    "国际物流单号与承运商不匹配：YANWEN / INVALID123，请审核后选择处理方式。",
    "当前任务正在被其他实例处理。",
    "当前订单已完成，不允许修改。",
    "无权执行此操作。",
    "共享状态版本已变化，请刷新后重试。",
])
def test_definitive_server_rejection_always_has_a_notice(reason, details):
    notice = operation_rejection(ControlResult(False, reason, details=details))
    assert notice.title == "操作被拒绝"
    assert notice.message == reason


def test_maintenance_refusal_is_short_and_actionable():
    notice = operation_rejection(ControlResult(
        False,
        "后台仍有等待或运行中的任务；为避免 SQLite 状态丢失，请等待任务结束后再执行迁移或状态维护。",
    ))
    assert notice.message == "后台仍有任务在等待或运行，请在任务结束后重试。"


@pytest.mark.parametrize("accepted,details", [
    (True, {}),
    (False, {"submission_outcome_unknown": True, "non_modal": True}),
    (False, {"automatic_retry": True}),
    (False, {"rejection_notice_shown": True}),
])
def test_success_pending_reads_and_already_shown_dialogs_do_not_warn_again(accepted, details):
    assert operation_rejection(ControlResult(accepted, "结果", details=details)) is None


@pytest.mark.parametrize("details", [
    {"skipped_reasons": {"ORDER-2": "任务已被其他人领取"}},
    {"rejected_orders": (("ORDER-2", "任务已被其他人领取"),)},
    {"submission_receipts": (
        TaskSubmissionReceipt("s1", "ORDER-1", "r1", ControlResult(True, "已排队")),
        TaskSubmissionReceipt("s2", "ORDER-2", "r2", ControlResult(False, "任务已被其他人领取")),
    )},
])
def test_partial_batch_rejection_is_not_hidden_by_success_or_pending(details):
    notice = operation_rejection(ControlResult(True, "部分已完成", details={
        **details, "non_modal": True, "submission_outcome_unknown": True,
    }))
    assert notice.title == "部分操作被拒绝"
    assert "ORDER-2：任务已被其他人领取" in notice.message


def test_empty_or_long_server_reasons_remain_readable():
    assert "未提供具体原因" in operation_rejection(ControlResult(False, "")).message
    notice = operation_rejection(ControlResult(False, "状态已变化。" * 100))
    assert len(notice.message) <= 180


def test_many_refused_items_are_summarized_without_losing_first_reason():
    notice = operation_rejection(ControlResult(True, "已修改一条", details={
        "skipped_reasons": {f"ORDER-{i}": "缺少必要信息" for i in range(10)},
    }))
    assert "10 条操作未执行" in notice.message
    assert "ORDER-0：缺少必要信息" in notice.message
    assert "另有 7 条" in notice.message
