from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from erp_automation.contracts.controller import ControlResult, TaskSubmissionReceipt
from erp_automation.contracts.models import (
    CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    Capability, DesktopSnapshot, TaskArea, TaskCommand, TaskRecord, TaskStatus,
)
from erp_automation.coordination.codec import (
    decode_snapshot, redact_snapshot_settings, to_jsonable,
)
from erp_automation.coordination.remote_controller import (
    CoordinationReadTimeout, RemoteBackgroundTaskController,
)
from erp_automation.ui.custom_order_submission import CustomOrderSubmission


def _command(order="ORDER", submission_id="attempt-one"):
    return TaskCommand(
        "处理定制订单", TaskArea.CUSTOMIZATION, Capability.UPDATE_CONTACT,
        order_no=order, payload={CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY: submission_id},
    )


def _client(status_response):
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._metadata_lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._local_pause_requested = False
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client._closed = False
    client.instance_id = "test-instance"
    client._browser_host = SimpleNamespace(ensure_started=lambda: None)
    client.browser_endpoint = "http://127.0.0.1:9222"
    client._browser_cleanup_task_ids = set()
    operations = []
    client._request_reconciliation_pool = SimpleNamespace(submit=operations.append)
    client._request_reconciliation_stop = SimpleNamespace(wait=lambda _delay: False)
    calls = []

    def request(_method, path, **kwargs):
        calls.append((path, kwargs["json"]))
        if path == "/v1/rpc":
            raise CoordinationReadTimeout("response not confirmed")
        assert path == "/v1/requests/status"
        return status_response

    client._request = request
    return client, operations, calls


def _response(*results):
    return {"state": "SUCCEEDED", "response": {
        "revision": 7, "result_type": "control_results",
        "result": [to_jsonable(result) for result in results],
    }}


def test_timeout_receipts_keep_per_order_outcomes_and_original_request_id():
    client, operations, calls = _client(_response(
        ControlResult(True, "已入队", "task-a"),
        ControlResult(False, "明确拒绝，未入队"),
    ))
    commands = (_command("A", "attempt-a"), _command("B", "attempt-b"))
    first = client.submit_tasks(commands)
    assert all(result.details["submission_outcome_unknown"] for result in first)
    request_id = first[0].details["request_id"]
    # Polling can finish before Qt handles the original timeout result.
    operations.pop()()
    assert client.revision == 7
    assert client.take_task_submission_receipts(("unrelated",)) == ()
    receipts = client.take_task_submission_receipts(("attempt-a", "attempt-b"))
    assert [(r.order_no, r.result.accepted, r.result.task_id) for r in receipts] == [
        ("A", True, "task-a"), ("B", False, None),
    ]
    assert all(receipt.request_id == request_id for receipt in receipts)
    assert client.take_task_submission_receipts(("attempt-a", "attempt-b")) == ()
    assert [path for path, _payload in calls] == ["/v1/rpc", "/v1/requests/status"]
    assert calls[-1][1]["request_id"] == request_id


@pytest.mark.parametrize("status_response", [
    {"state": "NOT_FOUND"}, {"state": "RUNNING"},
    {"state": "FAILED", "error": "exception after partial processing"},
    {"state": "SUCCEEDED", "response": None},
    _response(),  # A truncated batch result must not be treated as a rejection.
    {"state": "SUCCEEDED", "response": {
        "result_type": "control_results", "result": [{"message": "invalid"}],
    }},
])
def test_unresolved_or_malformed_receipt_never_claims_not_queued(status_response):
    client, operations, calls = _client(status_response)
    result = client.submit_tasks((_command(),))[0]
    operations.pop()()
    receipt, = client.take_task_submission_receipts(("attempt-one",))
    assert receipt.request_id == result.details["request_id"]
    assert receipt.result.details["submission_outcome_unknown"] is True
    assert receipt.result.details["reconciliation_exhausted"] is True
    assert sum(path == "/v1/rpc" for path, _payload in calls) == 1


def test_malformed_initial_batch_response_is_reconciled_without_resubmission():
    client, operations, calls = _client(_response(ControlResult(True, "已入队", "task-one")))
    original_request = client._request

    def request(method, path, **kwargs):
        if path == "/v1/rpc":
            calls.append((path, kwargs["json"]))
            return {"result_type": "control_results", "result": []}
        return original_request(method, path, **kwargs)

    client._request = request
    first = client.submit_tasks((_command(),))[0]
    assert first.details["submission_outcome_unknown"] is True
    operations.pop()()
    receipt, = client.take_task_submission_receipts(("attempt-one",))
    assert receipt.result.task_id == "task-one"
    assert sum(path == "/v1/rpc" for path, _payload in calls) == 1


def test_direct_receipt_includes_correlation_and_preserves_task_cleanup():
    client, operations, _calls = _client({})
    client._request = lambda *_args, **_kwargs: {
        "revision": 8, "result_type": "control_result",
        "result": to_jsonable(ControlResult(True, "已入队", "task-one")),
    }
    result = client.submit_task(_command())
    receipt, = client.take_task_submission_receipts(("attempt-one",))
    assert result.details["request_id"] == receipt.request_id
    assert receipt.result.task_id == result.task_id == "task-one"
    assert "task-one" in client._browser_cleanup_task_ids
    assert operations == []


def test_snapshot_preserves_public_submission_identity_without_authorization():
    command = _command()
    task = TaskRecord(
        "task-one", command.name, command.area, command.capability,
        order_no=command.order_no, status=TaskStatus.BLOCKED,
        payload={**command.payload, DESKTOP_CONFIRMATION_PAYLOAD_KEY: {"secret": "private"}},
    )
    decoded = decode_snapshot(to_jsonable(redact_snapshot_settings(DesktopSnapshot(
        tasks=[task], today_tasks=[task],
    ))))
    for received in (*decoded.tasks, *decoded.today_tasks):
        assert received.payload[CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY] == "attempt-one"
        assert DESKTOP_CONFIRMATION_PAYLOAD_KEY not in received.payload


def test_non_submission_request_keeps_its_explicit_failure_message():
    client, operations, _calls = _client({"state": "FAILED", "error": "保存配置失败"})
    client._schedule_request_reconciliation("request-settings", "save_settings")
    operations.pop()()
    assert client._take_reconciled_request_message() == "后台确认请求失败：保存配置失败"
    assert client.take_task_submission_receipts(("attempt-one",)) == ()


def test_receipt_and_snapshot_ordering_does_not_reopen_finished_attempt():
    pending = CustomOrderSubmission("attempt-one", "ORDER", 0)
    accepted = TaskSubmissionReceipt(
        "attempt-one", "ORDER", "request-one", ControlResult(True, "已入队", "task-one"),
    )
    assert pending.apply_receipt(accepted)
    late_timeout = replace(accepted, result=ControlResult(False, "超时", details={
        "submission_outcome_unknown": True,
    }))
    assert not pending.apply_receipt(late_timeout)
    assert pending.awaiting_task and not pending.awaiting_ack
    task = TaskRecord(
        "task-one", "处理定制订单", TaskArea.CUSTOMIZATION, Capability.UPDATE_CONTACT,
        order_no="ORDER", status=TaskStatus.BLOCKED,
    )
    assert pending.observe_task(task)
    assert not pending.apply_receipt(late_timeout)
    assert not pending.awaiting_ack and not pending.awaiting_task
    assert not pending.observe_task(replace(task, status=TaskStatus.QUEUED))
    assert pending.task.status is TaskStatus.BLOCKED
    assert not CustomOrderSubmission("attempt-two", "ORDER", 0).apply_receipt(accepted)
