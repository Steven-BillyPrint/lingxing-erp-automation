import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox
from shiboken6 import isValid

from erp_automation.ui.controller import ControlResult, InMemoryBackgroundTaskController
from erp_automation.ui.models import DesktopSnapshot, ShipmentRow
from erp_automation.ui.qt import (
    DesktopMainWindow, _ConfirmedShipmentTrackingDialog, _run_control_result_responsive,
    _control_operation_result,
)
from shipment_automation.alibaba_logistics import REAL_OVERSEAS_CARRIER_DISPLAY_NAMES


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch):
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, title, message: notices.append((title, message)))
    controller = InMemoryBackgroundTaskController()
    window = DesktopMainWindow(controller)
    window._timer.stop()
    window._custom_scan_timer.stop()
    window._shipment_scan_timer.stop()
    yield window, controller, notices
    window.close()
    window.deleteLater()
    app.processEvents()


def test_main_window_shows_legacy_non_modal_server_refusal(window):
    view, _controller, notices = window
    view._show_result(ControlResult(False, "当前状态不允许修改。", details={"non_modal": True}))
    assert notices == [("操作被拒绝", "当前状态不允许修改。")]


def test_unexpected_operation_error_retains_reason_without_leaking_credentials(window):
    view, _controller, notices = window
    def fail():
        raise ValueError("服务器拒绝：状态版本已变化； token=private-test-value")
    result = _control_operation_result(fail)
    view._show_result(result)
    assert notices[0][0] == "操作未完成"
    assert "状态版本已变化" in notices[0][1]
    assert "private-test-value" not in notices[0][1]


def test_manual_tracking_dialog_includes_every_supported_carrier(app):
    row = ShipmentRow(platform_order_no="ORDER-1", system_order_no="SYS-1", logistics_no="ALS-1")
    dialog = _ConfirmedShipmentTrackingDialog(row)
    assert {dialog.carrier_combo.itemData(i) for i in range(dialog.carrier_combo.count())} == set(REAL_OVERSEAS_CARRIER_DISPLAY_NAMES.values())
    assert "Wanb Express" in dialog.CARRIERS
    assert "OnTrac" in dialog.CARRIERS
    dialog.deleteLater()


def test_main_window_shows_partial_status_refusal(window):
    view, _controller, notices = window
    view._show_result(ControlResult(True, "已修改一条，跳过一条", details={
        "skipped_reasons": {"ORDER-2": "正在被其他实例处理"},
    }))
    assert notices[0][0] == "部分操作被拒绝"
    assert "ORDER-2：正在被其他实例处理" in notices[0][1]


def test_actual_tracking_edit_delivers_server_refusal_to_main_window(app, window, monkeypatch):
    view, controller, notices = window
    controller.control_calls_run_in_background = True
    controller.confirm_shipment_tracking_pair = lambda *_args, **_kwargs: ControlResult(
        False, "后台仍有等待或运行中的任务；为避免 SQLite 状态丢失，请等待任务结束。",
    )
    row = ShipmentRow(
        platform_order_no="ORDER-1", system_order_no="SYS-1", logistics_no="ALS-1",
        carrier="null", international_tracking_no="YWE00001506996989",
    )
    monkeypatch.setattr(view.shipment_page, "_checked_shipment_rows", lambda: [row])
    monkeypatch.setattr(_ConfirmedShipmentTrackingDialog, "exec", lambda _: _ConfirmedShipmentTrackingDialog.DialogCode.Accepted)
    monkeypatch.setattr(_ConfirmedShipmentTrackingDialog, "values", lambda _: ("Yanwen", row.international_tracking_no))
    view.shipment_page._edit_selected_tracking_pair()
    deadline = time.monotonic() + 3
    while getattr(view.shipment_page, "_responsive_control_threads", ()) and time.monotonic() < deadline:
        QTest.qWait(10)
    assert notices == [("操作被拒绝", "后台仍有任务在等待或运行，请在任务结束后重试。")]
    assert not getattr(view.shipment_page, "_responsive_control_threads", ())


def test_late_refusal_is_shown_even_when_snapshot_object_did_not_change(window):
    view, controller, notices = window
    snapshot = DesktopSnapshot()
    view._apply_snapshot(snapshot)
    pending = [ControlResult(False, "人工完成被拒绝：当前任务仍在执行。")]
    def take():
        result = tuple(pending)
        pending.clear()
        return result
    controller.take_operation_rejections = take
    view._apply_snapshot(snapshot)
    view._apply_snapshot(snapshot)
    assert notices == [("操作被拒绝", "人工完成被拒绝：当前任务仍在执行。")]


def test_fast_async_results_keep_workers_alive_until_rejection_dialog_returns(app):
    owner = QLabel()
    received = []
    controller = SimpleNamespace(control_calls_run_in_background=True)
    for index in range(32):
        def finish(result, index=index):
            threads = tuple(owner._responsive_control_threads)
            assert threads
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            assert all(isValid(thread) for thread in threads)
            received.append((index, result.message))
        _run_control_result_responsive(owner, controller, lambda: ControlResult(False, "版本冲突"), finish)
    # Queue all finished signals before returning control to the GUI.
    for thread in tuple(owner._responsive_control_threads):
        assert thread.wait(2000)
    deadline = time.monotonic() + 5
    while owner._responsive_control_threads and time.monotonic() < deadline:
        QTest.qWait(10)
    assert len(received) == 32
    assert not owner._responsive_control_threads
    owner.deleteLater()
