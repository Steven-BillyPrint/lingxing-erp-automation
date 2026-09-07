import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from erp_automation.contracts.controller import ControlResult
from erp_automation.coordination.remote_controller import (
    CoordinationReadTimeout, RemoteBackgroundTaskController,
)
from erp_automation.ui.models import DesktopSnapshot, TaskArea, TaskCommand, Capability


def _client():
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
    client.instance_id = "desktop-one"
    client._pending_request_ids = set()
    client._request_reconciliation_pool = ThreadPoolExecutor(max_workers=1)
    return client


def _wait_for_notice(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        notices = client.take_operation_rejections()
        if notices:
            return notices
        time.sleep(0.01)
    pytest.fail("A reconciled server refusal was not delivered to the GUI queue")


@pytest.mark.parametrize("accepted,details", [
    (False, {"non_modal": True}),
    (True, {"skipped_reasons": {"ALS-2": "当前任务仍在执行"}}),
])
def test_state_mutation_reconciles_late_refusal_or_partial_refusal_once(accepted, details):
    client = _client()
    rpc_calls = []
    def request(_method, path, **kwargs):
        if path == "/v1/rpc":
            rpc_calls.append(kwargs["json"]["request_id"])
            raise CoordinationReadTimeout("服务器尚未返回")
        assert path == "/v1/requests/status"
        return {"state": "SUCCEEDED", "response": {
            "revision": 2, "result_type": "control_result",
            "result": {"accepted": accepted, "message": "当前任务仍在执行", "details": details},
        }}
    client._request = request
    try:
        initial = client.change_shipment_statuses(["ALS-1", "ALS-2"], "mark_manual_done", reason="核对完成")
        assert initial.details["submission_outcome_unknown"] is True
        notices = _wait_for_notice(client)
        assert len(notices) == 1
        assert notices[0].message == "当前任务仍在执行"
        assert notices[0].accepted is accepted
        assert client.take_operation_rejections() == ()
        assert len(rpc_calls) == 1
    finally:
        client._closed = True
        client._request_reconciliation_pool.shutdown(wait=True)


def test_late_batch_refusal_keeps_each_order_reason():
    client = _client()
    commands = (
        TaskCommand("扫描一", TaskArea.SHIPMENT, Capability.LIST_ORDERS, order_no="ORDER-1"),
        TaskCommand("扫描二", TaskArea.SHIPMENT, Capability.LIST_ORDERS, order_no="ORDER-2"),
    )
    client._request = lambda *_args, **_kwargs: {
        "state": "SUCCEEDED", "response": {
            "revision": 2, "result_type": "control_results", "result": [
                {"accepted": True, "message": "已排队"},
                {"accepted": False, "message": "另一实例已经领取此任务"},
            ],
        },
    }
    try:
        client._schedule_request_reconciliation("request-1", "submit_tasks", submitted_commands=commands)
        notices = _wait_for_notice(client)
        assert notices[0].accepted is True
        assert notices[0].details["rejected_orders"] == (("ORDER-2", "另一实例已经领取此任务"),)
    finally:
        client._closed = True
        client._request_reconciliation_pool.shutdown(wait=True)


def test_repeated_failed_journal_does_not_redisplay_consumed_notice():
    client = _client()
    refusal = ControlResult(False, "服务器处理失败：共享状态保存失败。", details={
        "server_execution_failed": True,
    })
    try:
        client._record_operation_rejection("request-1", refusal)
        assert client.take_operation_rejections() == (refusal,)
        # A failed submission journal keeps its duplicate-submission guard and
        # may be polled again after the user has already dismissed its notice.
        client._record_operation_rejection("request-1", refusal)
        assert client.take_operation_rejections() == ()
        client._record_operation_rejection("request-2", refusal)
        assert client.take_operation_rejections() == (refusal,)
    finally:
        client._closed = True
        client._request_reconciliation_pool.shutdown(wait=True)
