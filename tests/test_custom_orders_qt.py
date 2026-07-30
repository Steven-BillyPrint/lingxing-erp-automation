from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QStyleOptionViewItem,
)

from erp_automation.ui.controller import ControlResult, InMemoryBackgroundTaskController
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    CustomOrderRow,
    SERVER_CONFIGURED_SECRET,
    DesktopSnapshot,
    DesktopSettings,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)
from erp_automation.ui.qt import (
    AlibabaOrderPage,
    CustomOrdersPage,
    DashboardPage,
    DesktopMainWindow,
    LogsPage,
    SettingsPage,
    ShipmentPage,
    ShipmentNotificationPage,
    StateManagementPage,
    _COMPLETE_ALL_STATE,
    _ConfirmedShipmentTrackingDialog,
    _ModernComboBox,
    _ModernSpinBox,
    _ShipmentStatusDialog,
    _interaction_stage_label,
)


class RecordingController(InMemoryBackgroundTaskController):
    def __init__(
        self,
        result: ControlResult | None = None,
        task_results: dict[str, ControlResult] | None = None,
    ) -> None:
        super().__init__()
        self.result = result or ControlResult(True, "完成")
        self.task_results = task_results or {}
        self.submitted_commands: list[TaskCommand] = []
        self.completion_calls: list[tuple[list[str], str]] = []
        self.stage_calls: list[tuple[list[str], str, str, str]] = []
        self.reopen_calls: list[tuple[list[str], str, str]] = []
        self.cancel_task_calls: list[list[str]] = []
        self.cancel_shipment_calls: list[tuple[list[str], str]] = []
        self.retry_shipment_calls: list[tuple[list[str], str, str]] = []
        self.reopen_shipment_calls: list[tuple[list[str], str, str]] = []
        self.change_shipment_calls: list[tuple[list[str], str, str]] = []
        self.confirm_shipment_calls: list[tuple[str, str, str, str]] = []
        self.notification_rows: list[dict[str, object]] = []

    def list_shipment_notifications(self) -> list[dict[str, object]]:
        return list(self.notification_rows)

    def submit_task(self, command: TaskCommand) -> ControlResult:
        self.submitted_commands.append(command)
        return self.task_results.get(
            str(command.order_no or ""),
            ControlResult(True, "已排队", f"task-{command.order_no}"),
        )

    def set_custom_stage_states(
        self,
        platform_order_nos,
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult:
        self.stage_calls.append((list(platform_order_nos), stage, state, reason))
        return self.result

    def complete_custom_workflows(
        self,
        platform_order_nos,
        *,
        reason: str,
    ) -> ControlResult:
        self.completion_calls.append((list(platform_order_nos), reason))
        return self.result

    def reopen_custom_workflows(
        self,
        platform_order_nos,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        self.reopen_calls.append((list(platform_order_nos), stage, reason))
        return self.result

    def cancel_tasks(self, task_ids) -> ControlResult:
        self.cancel_task_calls.append(list(task_ids))
        return self.result

    def cancel_shipments(self, logistics_nos, *, reason: str) -> ControlResult:
        self.cancel_shipment_calls.append((list(logistics_nos), reason))
        return self.result

    def retry_shipment_stages(self, logistics_nos, stage: str, *, reason: str) -> ControlResult:
        values = list(logistics_nos)
        self.retry_shipment_calls.append((values, stage, reason))
        return ControlResult(
            True,
            "已重试",
            details={"changed_logistics_nos": tuple(values)},
        )

    def reopen_shipments_from_stage(
        self,
        logistics_nos,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        values = list(logistics_nos)
        self.reopen_shipment_calls.append((values, stage, reason))
        return ControlResult(
            True,
            "已重开",
            details={"changed_logistics_nos": tuple(values)},
        )

    def change_shipment_statuses(
        self,
        logistics_nos,
        action: str,
        *,
        reason: str,
    ) -> ControlResult:
        values = list(logistics_nos)
        self.change_shipment_calls.append((values, action, reason))
        return ControlResult(
            True,
            "已修改",
            details={"changed_logistics_nos": tuple(values)},
        )

    def confirm_shipment_tracking_pair(
        self,
        logistics_no: str,
        *,
        carrier: str,
        tracking_no: str,
        reason: str,
    ) -> ControlResult:
        self.confirm_shipment_calls.append(
            (logistics_no, carrier, tracking_no, reason)
        )
        return ControlResult(True, "已确认", details={"logistics_no": logistics_no})


class SlowRemoteLikeController(RecordingController):
    snapshot_runs_in_background = True

    def __init__(self) -> None:
        super().__init__()
        self.snapshot_started = threading.Event()
        self.release_snapshot = threading.Event()

    def snapshot(self) -> DesktopSnapshot:
        self.snapshot_started.set()
        self.release_snapshot.wait(timeout=2)
        return super().snapshot()


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_alibaba_order_page_uses_two_independent_stages(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    results: list[ControlResult] = []
    page = AlibabaOrderPage(controller, results.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    try:
        page.system_order_edit.setText("SYS-100")
        page._prepare()

        assert controller.submitted_commands[-1].capability is (
            Capability.ALIBABA_ORDER_PREPARE
        )
        assert controller.submitted_commands[-1].order_no == "SYS-100"

        page.expedited_checkbox.setChecked(True)
        assert page.signature_checkbox.isChecked() is False
        assert page.signature_checkbox.isEnabled() is True
        page.expedited_checkbox.setChecked(False)
        assert page.signature_checkbox.isChecked() is False
        assert page.signature_checkbox.isEnabled() is True
        page.signature_checkbox.setChecked(True)
        page.expedited_checkbox.setChecked(True)
        page.expedited_checkbox.setChecked(False)
        assert page.signature_checkbox.isChecked() is True
        page.expedited_checkbox.setChecked(True)
        page.signature_checkbox.setChecked(False)
        page.heavy_checkbox.setChecked(True)
        page._fill_draft()

        command = controller.submitted_commands[-1]
        assert command.capability is Capability.ALIBABA_ORDER_DRAFT
        assert command.payload["expedited"] is True
        assert command.payload["signature_requested"] is False
        assert command.payload["heavy_or_frame"] is True
        assert "ddp_declaration_price_override" not in command.payload
        confirmation = DesktopWriteConfirmation.from_payload(command.payload)
        assert confirmation.action is DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT
        assert confirmation.system_order_no == "SYS-100"
    finally:
        page.deleteLater()


def test_settings_page_marks_server_secrets_and_only_keeps_portable_actions(
    app,
) -> None:
    controller = RecordingController()
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                lingxing_app_id="visible-app-id",
                lingxing_app_secret=SERVER_CONFIGURED_SECRET,
                amazon_refresh_token=SERVER_CONFIGURED_SECRET,
            ),
            configured_secret_lengths={
                "lingxing_app_secret": 7,
                "amazon_refresh_token": 19,
            },
        )
    )

    assert page.app_id.text() == "visible-app-id"
    assert page.app_secret.text() == ""
    assert page.app_secret.placeholderText() == "●" * 7
    assert page.app_secret.echoMode() == page.app_secret.EchoMode.Password
    assert bool(page.app_secret.property("server_secret_configured")) is True
    assert page.app_secret.property("server_secret_length") == 7
    assert page.amazon_refresh_token.text() == ""
    assert page.amazon_refresh_token.placeholderText() == "●" * 19
    assert (
        page.amazon_refresh_token.echoMode()
        == page.amazon_refresh_token.EchoMode.Password
    )
    assert (
        bool(
            page.amazon_refresh_token.property(
                "server_secret_configured"
            )
        )
        is True
    )
    assert page._secret_value(page.app_secret) == ""
    page.app_secret.setText("replacement-secret")
    page._sensitive_text_edited(
        page.app_secret,
        "replacement-secret",
    )
    assert page.app_secret.text() == "replacement-secret"
    assert page.app_secret.placeholderText() == ""
    assert page._secret_value(page.app_secret) == "replacement-secret"
    button_texts = {
        button.text() for button in page.findChildren(QPushButton)
    }
    assert "导出设置与授权" in button_texts
    assert "导入设置与授权" in button_texts
    assert "导入旧 .env" not in button_texts
    assert "状态迁移预检" not in button_texts
    assert "JSON 迁入 SQLite" not in button_texts
    assert not hasattr(page, "migration_status")


def test_settings_page_uses_exact_length_for_every_server_secret(app) -> None:
    secret_fields = {
        "lingxing_app_secret": ("app_secret", 5),
        "lingxing_password": ("lingxing_password", 8),
        "alibaba_password": ("alibaba_password", 11),
        "amazon_lwa_client_secret": ("amazon_client_secret", 14),
        "amazon_refresh_token": ("amazon_refresh_token", 17),
        "alimail_app_secret": ("alimail_app_secret", 20),
        "clicksend_username": ("clicksend_username", 23),
        "clicksend_api_key": ("clicksend_api_key", 26),
    }
    settings = DesktopSettings(
        **{
            field_name: SERVER_CONFIGURED_SECRET
            for field_name in secret_fields
        }
    )
    page = SettingsPage(RecordingController(), lambda _result: None)

    page.update_snapshot(
        DesktopSnapshot(
            settings=settings,
            configured_secret_lengths={
                field_name: length
                for field_name, (_editor_name, length) in secret_fields.items()
            },
        )
    )

    for field_name, (editor_name, length) in secret_fields.items():
        editor = getattr(page, editor_name)
        assert editor.text() == "", field_name
        assert editor.placeholderText() == "●" * length, field_name
        assert editor.property("server_secret_length") == length, field_name
        assert page._secret_value(editor) == "", field_name


def test_settings_page_safely_handles_legacy_secret_marker_without_length(app) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)

    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                lingxing_app_secret=SERVER_CONFIGURED_SECRET,
            )
        )
    )

    assert page.app_secret.text() == ""
    assert page.app_secret.placeholderText() == "已配置（请更新服务端）"
    assert page._secret_value(page.app_secret) == ""


def _snapshot(*order_nos: str) -> DesktopSnapshot:
    return DesktopSnapshot(
        custom_orders=[
            CustomOrderRow(
                platform_order_no=order_no,
                system_order_no=f"system-{index}",
                product_type="tent",
                workflow_stage="pending",
                status_text="pending",
            )
            for index, order_no in enumerate(order_nos, start=1)
        ]
    )


def _status_snapshot(*rows: tuple[str, str]) -> DesktopSnapshot:
    return DesktopSnapshot(
        custom_orders=[
            CustomOrderRow(
                platform_order_no=order_no,
                system_order_no=f"system-{index}",
                product_type="tent",
                workflow_stage=status,
                status_text=status,
            )
            for index, (order_no, status) in enumerate(rows, start=1)
        ]
    )


def _click_check_cell(table, row: int, horizontal_position: str) -> None:
    item = table.item(row, 0)
    rect = table.visualItemRect(item)
    x = {
        "left": rect.left() + 2,
        "center": rect.center().x(),
        "right": rect.right() - 2,
    }[horizontal_position]
    QTest.mouseClick(
        table.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        QPoint(x, rect.center().y()),
    )


def test_main_window_initial_refresh_has_interaction_guard(app):
    window = DesktopMainWindow(RecordingController())
    try:
        assert window._active_interaction_id is None
        window.refresh()
    finally:
        window.close()


def test_task_tables_show_the_verified_operator_account(app):
    task = TaskRecord(
        task_id="task-account-audit",
        name="处理定制订单",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        operator_name="Steven",
        operator_email="steven@billyprint.com",
    )
    snapshot = DesktopSnapshot(tasks=[task], today_tasks=[task])
    dashboard = DashboardPage()
    state = StateManagementPage(
        RecordingController(),
        lambda _result: None,
    )
    try:
        dashboard.update_snapshot(snapshot)
        state.update_snapshot(snapshot)

        assert dashboard.tasks.horizontalHeaderItem(4).text() == "操作账号"
        assert dashboard.tasks.item(0, 4).text() == (
            "Steven（steven@billyprint.com）"
        )
        assert state.tasks.horizontalHeaderItem(4).text() == "操作账号"
        assert state.tasks.item(0, 4).text() == (
            "Steven（steven@billyprint.com）"
        )
    finally:
        dashboard.deleteLater()
        state.deleteLater()


def test_expired_login_prompts_before_opening_browser(app, monkeypatch):
    class AuthenticationController(RecordingController):
        authentication_required = True

        def __init__(self) -> None:
            super().__init__()
            self.reauthentication_calls = 0

        def reauthenticate(self) -> ControlResult:
            self.reauthentication_calls += 1
            self.authentication_required = False
            return ControlResult(
                True,
                "企业邮箱登录已恢复，请重新执行刚才的操作。",
                details={"reauthenticated": True},
            )

    controller = AuthenticationController()
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "exec",
        lambda _message: QMessageBox.StandardButton.Open,
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message, *_args: notices.append((title, message)),
    )
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()

        window._show_result(
            ControlResult(
                False,
                "企业邮箱登录已过期。",
                details={"authentication_required": True},
            )
        )

        deadline = time.monotonic() + 2
        while (
            controller.reauthentication_calls == 0
            or window._authentication_thread is not None
        ) and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)

        assert controller.reauthentication_calls == 1
        assert notices[-1][0] == "企业邮箱登录已恢复"
    finally:
        thread = window._authentication_thread
        if thread is not None:
            thread.wait(2000)
            app.processEvents()
        window.close()


def test_remote_main_window_does_not_block_ui_during_snapshot(app):
    controller = SlowRemoteLikeController()
    started = time.perf_counter()
    window = DesktopMainWindow(controller)
    elapsed = time.perf_counter() - started
    try:
        window._timer.stop()

        assert elapsed < 0.5
        assert controller.snapshot_started.wait(timeout=1)
        assert window._latest_snapshot is None
        window.navigation.setCurrentRow(1)
        assert window.navigation.currentRow() == 1

        controller.release_snapshot.set()
        deadline = time.monotonic() + 2
        while window._latest_snapshot is None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
        assert window._latest_snapshot is not None
    finally:
        controller.release_snapshot.set()
        thread = window._snapshot_thread
        if thread is not None:
            thread.wait(2000)
            app.processEvents()
        window.close()


def test_main_window_shows_one_prominent_notice_for_newly_completed_shipment(
    app,
    monkeypatch,
):
    controller = RecordingController()
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message, *_args: notices.append((title, message)),
    )
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        controller._state.tasks = [
            TaskRecord(
                task_id="shipment-complete-1",
                name="执行自动标发",
                area=TaskArea.SHIPMENT,
                capability=Capability.OUTBOUND_ORDER,
                status=TaskStatus.SUCCEEDED,
                order_no="112-1165824-9982644",
                payload={
                    "system_order_no": "103710434633847501",
                    "logistics_no": "ALS01781406025",
                },
            )
        ]

        window.refresh()
        window.refresh()

        assert len(notices) == 1
        assert notices[0][0] == "自动标发完成"
        assert "112-1165824-9982644" in notices[0][1]
        assert "103710434633847501" in notices[0][1]
        assert "ALS01781406025" in notices[0][1]
    finally:
        window.close()


def test_main_window_aggregates_mixed_shipment_batch_into_one_notice(app, monkeypatch):
    controller = RecordingController()
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message, *_args: notices.append((title, message)),
    )
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        batch_id = "batch-aggregate"
        window._register_shipment_batch(batch_id, ("task-success", "task-failed"))
        controller._state.tasks = [
            TaskRecord(
                task_id="task-success",
                name="执行自动标发",
                area=TaskArea.SHIPMENT,
                capability=Capability.OUTBOUND_ORDER,
                status=TaskStatus.RUNNING,
                order_no="111-SUCCESS",
                payload={
                    "system_order_no": "SYS-SUCCESS",
                    "logistics_no": "ALS-SUCCESS",
                    "shipment_batch_id": batch_id,
                    "shipment_batch_position": 1,
                },
            ),
            TaskRecord(
                task_id="task-failed",
                name="执行自动标发",
                area=TaskArea.SHIPMENT,
                capability=Capability.OUTBOUND_ORDER,
                status=TaskStatus.QUEUED,
                order_no="112-FAILED",
                payload={
                    "system_order_no": "SYS-FAILED",
                    "logistics_no": "ALS-FAILED",
                    "shipment_batch_id": batch_id,
                    "shipment_batch_position": 2,
                },
            ),
        ]
        window.refresh()
        assert notices == []

        controller._state.tasks = [
            TaskRecord(
                **{
                    **controller._state.tasks[0].__dict__,
                    "status": TaskStatus.SUCCEEDED,
                    "message": "完成",
                }
            ),
            TaskRecord(
                **{
                    **controller._state.tasks[1].__dict__,
                    "status": TaskStatus.FAILED,
                    "message": "接口明确失败",
                }
            ),
        ]
        window.refresh()
        window.refresh()

        assert len(notices) == 1
        assert notices[0][0] == "自动标发完成"
        assert "成功 1" in notices[0][1]
        assert "失败 1" in notices[0][1]
        assert "111-SUCCESS" in notices[0][1]
        assert "112-FAILED" in notices[0][1]
        assert "接口明确失败" in notices[0][1]
    finally:
        window.close()


def test_shipment_table_click_selection_is_one_cell_not_whole_row(app):
    page = ShipmentPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="112-1165824-9982644",
                    system_order_no="103710434633847501",
                    logistics_no="ALS01781406025",
                )
            ]
        )
    )

    page.table.setCurrentCell(0, 2)

    assert page.table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems
    assert [(index.row(), index.column()) for index in page.table.selectedIndexes()] == [(0, 2)]
    page.deleteLater()


def test_entire_check_cell_toggles_exactly_once_in_all_checkable_tables(app):
    custom_page = CustomOrdersPage(RecordingController(), lambda _result: None)
    custom_page.resize(1000, 600)
    custom_page.show()
    custom_page.update_snapshot(_snapshot("111-CHECK"))
    app.processEvents()
    for position in ("left", "center", "right"):
        _click_check_cell(custom_page.table, 0, position)
        app.processEvents()
        assert custom_page.table.item(0, 0).checkState() == Qt.CheckState.Checked
        assert custom_page._checked_order_nos == {"111-CHECK"}
        _click_check_cell(custom_page.table, 0, position)
        app.processEvents()
        assert custom_page.table.item(0, 0).checkState() == Qt.CheckState.Unchecked
        assert custom_page._checked_order_nos == set()

    shipment_page = ShipmentPage(RecordingController(), lambda _result: None)
    shipment_page.resize(1000, 600)
    shipment_page.show()
    shipment_page.update_snapshot(
        DesktopSnapshot(
            shipments=[ShipmentRow("112-CHECK", logistics_no="ALS-CHECK")]
        )
    )
    app.processEvents()
    _click_check_cell(shipment_page.table, 0, "right")
    app.processEvents()
    assert shipment_page.table.item(0, 0).checkState() == Qt.CheckState.Checked
    assert shipment_page._checked_logistics_nos == {"ALS-CHECK"}

    state_page = StateManagementPage(RecordingController(), lambda _result: None)
    state_page.resize(1000, 700)
    state_page.show()
    state_page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "active-check",
                    "scan",
                    TaskArea.CUSTOMIZATION,
                    Capability.LIST_ORDERS,
                ),
                TaskRecord(
                    "finished-check",
                    "done",
                    TaskArea.SHIPMENT,
                    Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                ),
            ]
        )
    )
    app.processEvents()
    _click_check_cell(state_page.tasks, 0, "left")
    _click_check_cell(state_page.tasks, 1, "right")
    app.processEvents()
    assert state_page.tasks.item(0, 0).checkState() == Qt.CheckState.Checked
    assert state_page._checked_task_ids == {"active-check"}
    assert state_page.tasks.item(1, 0).checkState() == Qt.CheckState.Unchecked

    custom_page.close()
    shipment_page.close()
    state_page.close()
    custom_page.deleteLater()
    shipment_page.deleteLater()
    state_page.deleteLater()


def test_capability_combo_does_not_select_its_table_cell(app):
    page = StateManagementPage(RecordingController(), lambda _result: None)
    page.resize(1000, 700)
    page.show()
    page.update_snapshot(DesktopSnapshot())
    app.processEvents()
    combo = page.capabilities.cellWidget(0, 2)

    QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
    app.processEvents()

    assert isinstance(combo, _ModernComboBox)
    assert page.capabilities.selectionMode() == QAbstractItemView.SelectionMode.NoSelection
    assert page.capabilities.selectedIndexes() == []
    combo.hidePopup()
    page.close()
    page.deleteLater()


def test_main_window_waits_then_closes_automatically_after_safe_drain(app, monkeypatch):
    controller = RecordingController()
    state = {"ready": False, "closed": False, "notices": 0}

    def prepare_close():
        return ControlResult(
            state["ready"],
            "可以关闭" if state["ready"] else "正在等待已确认写入安全完成",
        )

    controller.prepare_close = prepare_close
    controller.close = lambda: state.__setitem__("closed", True)
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *_args: state.__setitem__("notices", state["notices"] + 1),
    )
    window = DesktopMainWindow(controller)
    window._timer.stop()
    window._custom_scan_timer.stop()
    window._shipment_scan_timer.stop()
    window.show()
    app.processEvents()

    window.close()

    assert window._close_pending is True
    assert window.isVisible()
    assert state["notices"] == 1

    state["ready"] = True
    window.refresh()
    app.processEvents()
    app.processEvents()

    assert state["closed"] is True
    assert not window.isVisible()


def test_main_window_schedules_custom_and_shipment_scans_with_clear_scope(app):
    controller = RecordingController()
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        assert window._custom_scan_timer.interval() == 5 * 60 * 1000
        assert window._shipment_scan_timer.interval() == 3 * 60 * 60 * 1000
        assert "每 5 分钟" in window.custom_orders_page.scan_schedule_label.text()
        assert "无自定义标签" in window.custom_orders_page.scan_schedule_label.text()
        assert "无错误订单存在文件夹则完成、不存在则待处理" in (
            window.custom_orders_page.scan_schedule_label.text()
        )
        assert "报错、待复核或人工阻止订单保留原状态" in (
            window.custom_orders_page.scan_schedule_label.text()
        )
        assert "每 3 小时" in window.shipment_page.scan_schedule_label.text()
        assert "扫描领星待审核订单" in window.shipment_page.scan_schedule_label.text()
        assert "本机可见 Chrome" in window.shipment_page.scan_schedule_label.text()
        assert "没有在线客户端时物流记录保持待查询" in (
            window.shipment_page.scan_schedule_label.text()
        )

        window._run_automatic_custom_scan()
        window._run_automatic_shipment_scan()

        assert [(command.area, command.payload["trigger"]) for command in controller.submitted_commands] == [
            (TaskArea.CUSTOMIZATION, "five_minute_timer"),
            (TaskArea.SHIPMENT, "three_hour_timer"),
        ]
    finally:
        window.close()


def test_shipment_page_scan_registers_local_visible_logistics_followup(app):
    controller = RecordingController()
    registered: list[str] = []
    page = ShipmentPage(
        controller,
        lambda _result: None,
        scan_handler=registered.append,
    )

    page._scan()

    command = controller.submitted_commands[-1]
    assert command.area is TaskArea.SHIPMENT
    assert command.capability is Capability.LIST_ORDERS
    assert command.payload["trigger"] == "manual_button"
    assert command.payload["local_visible_logistics_followup"] is True
    assert registered == ["task-None"]


def test_completed_shipment_scan_starts_local_visible_logistics_task(app):
    controller = RecordingController()
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        window._run_automatic_shipment_scan()

        assert window._pending_local_logistics_scan_ids == {"task-None"}
        running = DesktopSnapshot(
            tasks=[
                TaskRecord(
                    task_id="task-None",
                    name="自动标发三小时自动扫描",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                    status=TaskStatus.RUNNING,
                )
            ]
        )
        window._apply_snapshot(running)
        assert len(controller.submitted_commands) == 1

        completed = DesktopSnapshot(
            tasks=[
                TaskRecord(
                    task_id="task-None",
                    name="自动标发三小时自动扫描",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                )
            ]
        )
        window._apply_snapshot(completed)
        for _ in range(100):
            app.processEvents()
            thread = window._local_logistics_followup_thread
            if thread is None:
                break
            QTest.qWait(10)

        assert len(controller.submitted_commands) == 3
        logistics_followup = controller.submitted_commands[-2]
        assert logistics_followup.area is TaskArea.SHIPMENT
        assert logistics_followup.capability is Capability.ALIBABA_LOGISTICS
        assert logistics_followup.payload == {
            "trigger": "after_shipment_scan",
            "source_scan_task_id": "task-None",
        }
        notification_followup = controller.submitted_commands[-1]
        assert notification_followup.area is TaskArea.SHIPMENT
        assert notification_followup.capability is Capability.LIST_ORDERS
        assert notification_followup.payload == {
            "trigger": "shipment_notification_compensation",
            "source_scan_task_id": "task-None",
        }
        assert window._pending_local_logistics_scan_ids == set()
    finally:
        window.close()


def test_completed_scan_from_another_or_offline_client_stays_pending(app):
    controller = RecordingController()
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        snapshot = DesktopSnapshot(
            tasks=[
                TaskRecord(
                    task_id="unowned-scan",
                    name="其它客户端扫描",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                )
            ]
        )

        window._apply_snapshot(snapshot)
        app.processEvents()

        assert controller.submitted_commands == []
        assert window._pending_local_logistics_scan_ids == set()
    finally:
        window.close()


def test_scan_log_buttons_open_separate_queue_directories(app, tmp_path, monkeypatch):
    custom_logs = tmp_path / "custom_order_scan"
    shipment_logs = tmp_path / "shipment_scan"
    custom_logs.mkdir()
    shipment_logs.mkdir()
    controller = RecordingController()
    controller.log_directory = lambda: str(tmp_path)
    opened = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    window = DesktopMainWindow(controller)
    try:
        button_texts = {
            button.text() for button in window.findChildren(QPushButton)
        }
        assert "打开定制订单扫描日志" in button_texts
        assert "打开自动标发扫描日志" in button_texts

        window.custom_orders_page._open_scan_logs()
        window.shipment_page._open_scan_logs()

        assert [Path(value) for value in opened] == [custom_logs, shipment_logs]
    finally:
        window.close()


def test_contact_capability_menu_only_offers_browser_or_disabled(app):
    page = StateManagementPage(RecordingController(), lambda _result: None)
    page.update_snapshot(DesktopSnapshot())
    row = list(Capability).index(Capability.UPDATE_CONTACT)
    combo = page.capabilities.cellWidget(row, 2)

    assert isinstance(combo, QComboBox)
    assert [combo.itemData(index) for index in range(combo.count())] == [
        CapabilityMode.BROWSER.value,
        CapabilityMode.DISABLED.value,
    ]
    assert combo.currentData() == CapabilityMode.BROWSER.value
    page.deleteLater()


def test_combo_boxes_use_modern_chevron_and_spacious_popup_items(app):
    controller = RecordingController()
    window = DesktopMainWindow(controller)
    try:
        combo = window.custom_orders_page.status_filter_combo
        option = QStyleOptionViewItem()
        item = combo.model().index(0, 0)

        assert type(combo).__name__ == "_ModernComboBox"
        assert combo.itemDelegate().sizeHint(option, item).height() >= 36
        assert "QComboBox::drop-down" in window.styleSheet()
        assert "QComboBox QAbstractItemView" in window.styleSheet()
        assert "QComboBox::down-arrow" in window.styleSheet()
    finally:
        window.close()


def test_spin_boxes_use_the_same_modern_chevron_treatment(app):
    window = DesktopMainWindow(RecordingController())
    try:
        assert isinstance(window.settings_page.api_timeout, _ModernSpinBox)
        assert isinstance(window.settings_page.payment_window, _ModernSpinBox)
        assert isinstance(window.settings_page.log_retention, _ModernSpinBox)
        assert "QSpinBox::up-button" in window.styleSheet()
        assert "QSpinBox::up-arrow" in window.styleSheet()
    finally:
        window.close()


def test_emergency_stop_has_one_global_page_entry_and_state_banner(app):
    controller = RecordingController()
    controller.set_emergency_stop_writes(False)
    window = DesktopMainWindow(controller)
    try:
        buttons = [
            button
            for button in window.findChildren(QPushButton)
            if "紧急停止" in button.text()
        ]
        assert buttons == [window.global_emergency_button]
        assert not hasattr(window.custom_orders_page, "emergency_stop_button")
        assert not hasattr(window.shipment_page, "emergency_stop_button")
        assert not hasattr(window.state_page, "emergency_stop")
        assert window.emergency_banner.isHidden() is True

        controller.set_emergency_stop_writes(True)
        window.refresh()
        window.state_page.update_snapshot(controller.snapshot())

        assert window.global_emergency_button.text() == "解除急停"
        assert window.safety_panel.property("emergencyActive") is True
        assert window.global_emergency_state.text() == "●  已紧急停止"
        assert window.local_connection_state.text() == "只读功能仍可使用"
        assert window.emergency_banner.isHidden() is False
        assert "已紧急停止" in window.state_page.emergency_state.text()
    finally:
        window.close()


def test_logs_page_offers_one_and_three_month_cleanup_choices(app):
    page = LogsPage(RecordingController())
    try:
        assert [
            page.cleanup_age_combo.itemData(index)
            for index in range(page.cleanup_age_combo.count())
        ] == [30, 90]
        assert page.cleanup_button.text() == "清理旧日志"
    finally:
        page.deleteLater()


def test_custom_order_checks_are_tristate_and_survive_refresh(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    snapshot = _snapshot("111-1", "112-2")
    page.update_snapshot(snapshot)

    assert page.table.columnCount() == 8
    assert page.table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert page.stage_state_combo.itemText(page.stage_state_combo.count() - 2) == "全部完成"
    assert page.stage_state_combo.itemData(page.stage_state_combo.count() - 2) == _COMPLETE_ALL_STATE
    assert page.stage_state_combo.itemText(page.stage_state_combo.count() - 1) == "取消订单"
    assert "勾选订单全部完成" not in {
        button.text() for button in page.findChildren(QPushButton)
    }
    assert "处理勾选订单" in {
        button.text() for button in page.findChildren(QPushButton)
    }
    assert "处理选中订单" not in {
        button.text() for button in page.findChildren(QPushButton)
    }
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert page._checked_order_nos == {"111-1"}
    assert page._check_header.check_state == Qt.CheckState.PartiallyChecked

    page.update_snapshot(snapshot)
    assert page.table.item(0, 0).checkState() == Qt.CheckState.Checked
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    assert page._checked_order_nos == {"111-1", "112-2"}
    assert page._check_header.check_state == Qt.CheckState.Checked

    page.update_snapshot(_snapshot("112-2"))
    assert page._checked_order_nos == {"112-2"}
    page._check_header.check_state_changed.emit(Qt.CheckState.Unchecked.value)
    assert page._checked_order_nos == set()
    assert page._check_header.check_state == Qt.CheckState.Unchecked
    page.deleteLater()


def test_custom_completed_filter_shows_most_recent_status_first_with_china_time(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no="111-OLD",
                    workflow_stage="completed",
                    status_text="completed",
                    status_updated_at="2026-07-16T01:00:00Z",
                ),
                CustomOrderRow(
                    platform_order_no="111-NEW",
                    workflow_stage="completed",
                    status_text="completed",
                    status_updated_at="2026-07-16T03:30:00Z",
                ),
            ]
        )
    )
    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("completed")
    )

    assert [row.platform_order_no for row in page._rows] == ["111-NEW", "111-OLD"]
    assert page.table.item(0, 6).text() == "2026-07-16 11:30:00"
    assert page.table.item(1, 6).text() == "2026-07-16 09:00:00"
    page.deleteLater()


def test_missing_product_type_is_explicit_and_interaction_stages_are_chinese(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no="114-5019404-8703446",
                    system_order_no="103715030356611759",
                    product_type="",
                    workflow_stage="completed",
                    status_text="completed",
                )
            ]
        )
    )

    assert page.table.item(0, 3).text() == "未记录"
    assert _interaction_stage_label("folder_creation") == "创建订单文件夹"
    assert _interaction_stage_label("contact_writeback") == "联系方式修改审核"
    assert _interaction_stage_label("retry_review:folder") == "重试前人工复核：订单文件夹"
    assert _interaction_stage_label("future_stage") == "future_stage"
    page.deleteLater()


def test_process_uses_checked_rows_and_ignores_blue_selection(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.setCurrentCell(0, 1)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    confirmation_text: list[str] = []

    def confirm(*args):
        confirmation_text.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == ["112-2"]
    command = controller.submitted_commands[0]
    confirmation = DesktopWriteConfirmation.from_payload(command.payload)
    confirmation.require_matches(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        "112-2",
        system_order_no="system-2",
    )
    assert "1 张勾选订单" in confirmation_text[0]
    assert "112-2" in confirmation_text[0]
    assert "111-1" not in confirmation_text[0]
    assert page._checked_order_nos == set()
    assert results[-1].accepted
    page.deleteLater()


def test_process_requires_checks_even_when_blue_row_is_selected(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1"))
    page.table.setCurrentCell(0, 1)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("没有勾选时不应显示确认弹窗"),
    )

    page._process_checked_orders()

    assert controller.submitted_commands == []
    assert not results[-1].accepted
    assert "勾选至少一张" in results[-1].message
    page.deleteLater()


def test_process_batch_preserves_visible_order_and_summarizes_preview(app, monkeypatch):
    order_nos = [f"order-{index:02d}" for index in range(12)]
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot(*order_nos))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    confirmation_text: list[str] = []

    def confirm(*args):
        confirmation_text.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == order_nos
    for index, command in enumerate(controller.submitted_commands, start=1):
        confirmation = DesktopWriteConfirmation.from_payload(command.payload)
        confirmation.require_matches(
            DesktopWriteAction.PROCESS_CUSTOM_ORDER,
            command.order_no or "",
            system_order_no=f"system-{index}",
        )
    assert "12 张勾选订单" in confirmation_text[0]
    assert "order-09" in confirmation_text[0]
    assert "order-10" not in confirmation_text[0]
    assert "另有 2 张订单" in confirmation_text[0]
    assert page._checked_order_nos == set()
    assert results[-1].accepted
    page.deleteLater()


def test_process_batch_cancel_keeps_all_checks(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    page._process_checked_orders()

    assert controller.submitted_commands == []
    assert page._checked_order_nos == {"111-1", "112-2"}
    page.deleteLater()


def test_process_batch_clears_only_accepted_checks(app, monkeypatch):
    controller = RecordingController(
        task_results={"112-2": ControlResult(False, "该订单已完成")}
    )
    results: list[ControlResult] = []
    warnings: list[str] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: warnings.append(str(_args[2])),
    )

    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == [
        "111-1",
        "112-2",
    ]
    assert page._checked_order_nos == {"112-2"}
    assert results[-1].accepted
    assert "1 张未排队" in results[-1].message
    assert "112-2" in warnings[0]
    assert "该订单已完成" in warnings[0]
    page.deleteLater()


def test_process_batch_keeps_checks_when_all_submissions_fail(app, monkeypatch):
    controller = RecordingController(
        task_results={
            "111-1": ControlResult(False, "重复排队"),
            "112-2": ControlResult(False, "存在已阻止阶段"),
        }
    )
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._process_checked_orders()

    assert page._checked_order_nos == {"111-1", "112-2"}
    assert not results[-1].accepted
    assert "111-1：重复排队" in results[-1].message
    assert "112-2：存在已阻止阶段" in results[-1].message
    page.deleteLater()


def test_process_batch_stops_after_first_local_browser_failure(app, monkeypatch):
    controller = RecordingController(
        task_results={
            "111-1": ControlResult(
                False,
                "本机专用 Chrome 未就绪。",
                details={
                    "local_browser_unavailable": True,
                    "retry_suppressed": True,
                },
            )
        }
    )
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2", "113-3"))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == [
        "111-1"
    ]
    assert page._checked_order_nos == {"111-1", "112-2", "113-3"}
    assert results[-1].accepted is False
    assert results[-1].details["local_browser_batch_aborted"] is True
    assert len(results[-1].details["rejected_orders"]) == 3
    assert "停止本批次的重复启动" in results[-1].message
    assert "3 张订单保持勾选" in results[-1].message
    page.deleteLater()


def test_remote_process_batch_submits_without_blocking_qt_thread(app, monkeypatch):
    class SlowSubmissionController(RecordingController):
        snapshot_runs_in_background = True

        def __init__(self) -> None:
            super().__init__()
            self.submission_started = threading.Event()
            self.release_submission = threading.Event()

        def submit_task(self, command: TaskCommand) -> ControlResult:
            self.submission_started.set()
            assert self.release_submission.wait(2)
            return super().submit_task(command)

    controller = SlowSubmissionController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1"))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    started_at = time.monotonic()
    page._process_checked_orders()

    assert time.monotonic() - started_at < 0.2
    assert not page.process_button.isEnabled()
    assert controller.submission_started.wait(1)
    controller.release_submission.set()
    deadline = time.monotonic() + 2
    while not results and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert results[-1].accepted
    assert page.process_button.isEnabled()
    assert page._checked_order_nos == set()
    page.deleteLater()


def test_batch_stage_update_uses_only_checked_rows_and_clears_on_success(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.setCurrentCell(0, 1)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page.stage_state_combo.setCurrentIndex(page.stage_state_combo.findData("NOT_REQUIRED"))
    monkeypatch.setattr(page, "_reason", lambda _title: "无需联系方式")
    confirmation_text: list[str] = []

    def confirm(*args):
        confirmation_text.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page._update_stage_state()

    assert controller.stage_calls == [
        (["112-2"], "contact", "NOT_REQUIRED", "无需联系方式")
    ]
    assert controller.completion_calls == []
    assert "112-2" in confirmation_text[0]
    assert "111-1" not in confirmation_text[0]
    assert "不会请求领星 ERP" in confirmation_text[0]
    assert page._checked_order_nos == set()
    assert page.table.item(1, 0).checkState() == Qt.CheckState.Unchecked
    assert results[-1].accepted
    page.deleteLater()


def test_all_complete_is_in_state_menu_and_ignores_stage(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page.stage_combo.setCurrentIndex(page.stage_combo.findData("sku"))
    page.stage_state_combo.setCurrentIndex(
        page.stage_state_combo.findData(_COMPLETE_ALL_STATE)
    )
    monkeypatch.setattr(page, "_reason", lambda _title: "人工线下完成")
    confirmation_text: list[str] = []

    def confirm(*args):
        confirmation_text.append(str(args[2]))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    page._update_stage_state()

    assert controller.completion_calls == [(["112-2"], "人工线下完成")]
    assert controller.stage_calls == []
    assert "当前阶段选择将被忽略" in confirmation_text[0]
    assert page.stage_combo.isEnabled()
    assert page._checked_order_nos == set()
    page.deleteLater()


def test_all_complete_keeps_checks_when_cancelled_or_failed(app, monkeypatch):
    controller = RecordingController(ControlResult(False, "事务失败"))
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1"))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.stage_state_combo.setCurrentIndex(
        page.stage_state_combo.findData(_COMPLETE_ALL_STATE)
    )
    monkeypatch.setattr(page, "_reason", lambda _title: "人工线下完成")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )
    page._update_stage_state()
    assert controller.completion_calls == []
    assert page._checked_order_nos == {"111-1"}

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    page._update_stage_state()
    assert controller.completion_calls == [(["111-1"], "人工线下完成")]
    assert page._checked_order_nos == {"111-1"}
    assert page.table.item(0, 0).checkState() == Qt.CheckState.Checked
    page.deleteLater()


def test_stage_update_falls_back_to_blue_selected_row_without_confirmation(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.setCurrentCell(0, 1)
    page.stage_combo.setCurrentIndex(page.stage_combo.findData("folder"))
    page.stage_state_combo.setCurrentIndex(page.stage_state_combo.findData("BLOCKED"))
    monkeypatch.setattr(page, "_reason", lambda _title: "人工阻止")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("普通单行状态修改不应二次确认"),
    )

    page._update_stage_state()

    assert controller.stage_calls == [(["111-1"], "folder", "BLOCKED", "人工阻止")]
    page.deleteLater()


def test_reopen_prefers_checked_rows_and_falls_back_to_blue_row(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.setCurrentCell(0, 1)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page.stage_combo.setCurrentIndex(page.stage_combo.findData("sku"))
    monkeypatch.setattr(page, "_reason", lambda _title: "重新核验")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._reopen_stage()

    assert controller.reopen_calls == [(["112-2"], "sku", "重新核验")]
    assert page._checked_order_nos == set()

    page._reopen_stage()
    assert controller.reopen_calls[-1] == (["111-1"], "sku", "重新核验")
    page.deleteLater()


def test_statuses_are_sorted_and_displayed_in_chinese_with_exact_filters(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        _status_snapshot(
            ("completed-1", "completed"),
            ("pending-1", "pending"),
            ("unknown-1", "future_status"),
            ("pending-2", "pending"),
            ("blocked-1", "blocked"),
            ("sku-1", "sku_adjustment_pending"),
        )
    )

    assert [row.platform_order_no for row in page._rows] == [
        "pending-1",
        "pending-2",
        "blocked-1",
        "sku-1",
        "completed-1",
        "unknown-1",
    ]
    assert page.table.item(0, 4).text() == "联系方式待处理"
    assert page.table.item(0, 5).text() == "联系方式待处理"
    assert page.table.item(2, 5).text() == "已阻止"
    assert page.table.item(3, 5).text() == "SKU 调整待处理"
    assert page.status_filter_combo.itemText(
        page.status_filter_combo.findData("completed")
    ) == "已完成"
    assert page.status_filter_combo.itemText(
        page.status_filter_combo.findData("future_status")
    ) == "future_status"

    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("sku_adjustment_pending")
    )
    assert page.status_filter_combo.currentData() == "sku_adjustment_pending"
    assert [row.platform_order_no for row in page._rows] == ["sku-1"]
    assert page.table.item(0, 5).text() == "SKU 调整待处理"
    page.deleteLater()


def test_status_filter_survives_refresh_and_keeps_empty_exact_state(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(_status_snapshot(("pending-1", "pending")))
    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("package_split_pending")
    )
    assert page.table.rowCount() == 0

    page.update_snapshot(
        _status_snapshot(
            ("pending-1", "pending"),
            ("split-1", "package_split_pending"),
        )
    )

    assert page.status_filter_combo.currentData() == "package_split_pending"
    assert [row.platform_order_no for row in page._rows] == ["split-1"]
    assert page.table.item(0, 5).text() == "拆包待处理"
    page.deleteLater()


def test_custom_queue_searches_platform_system_and_product_type(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow("111-AAA", "SYS-001", "tent", "pending", "pending"),
                CustomOrderRow("112-BBB", "SYS-002", "x_stands", "completed", "completed"),
            ]
        )
    )

    page.search_edit.setText("112-b")
    assert [row.platform_order_no for row in page._rows] == ["112-BBB"]
    page.search_field_combo.setCurrentIndex(
        page.search_field_combo.findData("system_order_no")
    )
    page.search_edit.setText("001")
    assert [row.system_order_no for row in page._rows] == ["SYS-001"]
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.search_field_combo.setCurrentIndex(
        page.search_field_combo.findData("product_type")
    )
    page.search_edit.setText("x_stands")
    assert [row.product_type for row in page._rows] == ["x_stands"]
    assert page._checked_order_nos == set()
    page.deleteLater()


def test_shipment_queue_search_and_checked_batch_cancel(app, monkeypatch):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow("111-AAA", "SYS-001", "tent", "ALS-1"),
                ShipmentRow("112-BBB", "SYS-002", "x_stands", "ALS-2"),
            ]
        )
    )

    assert page.table.columnCount() == 10
    assert page.search_field_combo.findData("product_type") == -1
    page.search_edit.setText("111-a")
    assert [row.logistics_no for row in page._rows] == ["ALS-1"]
    page.search_field_combo.setCurrentIndex(
        page.search_field_combo.findData("system_order_no")
    )
    page.search_edit.setText("002")
    assert [row.logistics_no for row in page._rows] == ["ALS-2"]
    assert page.table.item(0, 3).text() == "ALS-2"
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(page, "_reason", lambda _title: "批量取消测试")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._cancel_checked()

    assert controller.cancel_shipment_calls == [(["ALS-2"], "批量取消测试")]
    assert page._checked_logistics_nos == set()
    page.deleteLater()


def test_shipment_completed_filter_uses_completion_time_newest_first(app):
    page = ShipmentPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-OLD",
                    logistics_no="ALS-OLD",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    international_tracking_no="1Z-OLD",
                    carrier="UPS",
                    actual_total="USD 10.00",
                    chargeable_weight_kg="1.0",
                    erp_state="DONE",
                    checkpoint="OUTBOUNDED",
                    updated_at="2026-07-16T05:00:00Z",
                    outbounded_at="2026-07-16T01:00:00Z",
                    completion_source="AUTOMATION",
                ),
                ShipmentRow(
                    platform_order_no="111-MANUAL",
                    logistics_no="ALS-MANUAL",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    international_tracking_no="1Z-MANUAL",
                    carrier="UPS",
                    actual_total="USD 20.00",
                    chargeable_weight_kg="2.0",
                    erp_state="DONE",
                    checkpoint="OUTBOUNDED",
                    updated_at="2026-07-16T06:00:00Z",
                    externally_completed_at="2026-07-16T04:00:00Z",
                    completion_source="MANUAL_DETECTED",
                ),
                ShipmentRow(
                    platform_order_no="111-NEW",
                    logistics_no="ALS-NEW",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    international_tracking_no="1Z-NEW",
                    carrier="UPS",
                    actual_total="USD 30.00",
                    chargeable_weight_kg="3.0",
                    erp_state="DONE",
                    checkpoint="OUTBOUNDED",
                    updated_at="2026-07-16T07:00:00Z",
                    outbounded_at="2026-07-16T03:00:00Z",
                    completion_source="AUTOMATION",
                ),
            ]
        )
    )
    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("已完成")
    )

    assert [row.logistics_no for row in page._rows] == [
        "ALS-MANUAL",
        "ALS-NEW",
        "ALS-OLD",
    ]
    assert page.table.item(0, 8).text() == "2026-07-16 12:00:00"
    assert page.table.item(1, 8).text() == "2026-07-16 11:00:00"
    page.deleteLater()


def test_shipment_status_dialog_uses_modern_combo_and_lists_every_reopen_stage(app):
    dialog = _ShipmentStatusDialog(2)
    try:
        assert isinstance(dialog.action_combo, _ModernComboBox)
        actions = {
            str(dialog.action_combo.itemData(index))
            for index in range(dialog.action_combo.count())
        }
        assert {
            "reopen:logistics",
            "reopen:set_channel",
            "reopen:audit",
            "reopen:tracking",
            "reopen:outbound",
            "manual_review",
            "mark_manual_done",
            "restore_cancelled",
            "cancel",
        }.issubset(actions)
    finally:
        dialog.deleteLater()


def test_shipment_status_and_retry_ignore_blue_row_and_use_checks(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-DONE",
                    system_order_no="SYS-DONE",
                    logistics_no="ALS-DONE",
                    international_tracking_no="1Z999",
                    carrier="UPS",
                    actual_total="USD 20.00",
                    chargeable_weight_kg="10",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    erp_state="DONE",
                    checkpoint="OUTBOUNDED",
                ),
                ShipmentRow(
                    platform_order_no="112-OTHER",
                    system_order_no="SYS-OTHER",
                    logistics_no="ALS-OTHER",
                ),
            ]
        )
    )
    completed_index = next(
        index for index, row in enumerate(page._rows) if row.logistics_no == "ALS-DONE"
    )
    page.table.setCurrentCell(completed_index, 1)

    page._retry_selected_stage()
    assert controller.retry_shipment_calls == []
    assert results[-1].accepted is False

    page.table.item(completed_index, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(_ShipmentStatusDialog, "exec", lambda _dialog: 1)
    monkeypatch.setattr(
        _ShipmentStatusDialog,
        "selected_action",
        lambda _dialog: "reopen:tracking",
    )
    monkeypatch.setattr(
        _ShipmentStatusDialog,
        "selected_label",
        lambda _dialog: "从填写运单信息重新开始",
    )
    monkeypatch.setattr(page, "_reason", lambda _title: "ERP 已人工退回")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._change_selected_status()

    assert controller.reopen_shipment_calls == [
        (["ALS-DONE"], "tracking", "ERP 已人工退回")
    ]
    assert page._checked_logistics_nos == set()
    assert controller.change_shipment_calls == []
    page.deleteLater()


def test_shipment_batch_execution_uses_only_checked_actionable_rows(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)
    ready = ShipmentRow(
        platform_order_no="111-READY",
        system_order_no="SYS-READY",
        product_type="tent",
        logistics_no="ALS-READY",
        international_tracking_no="1Z999",
        carrier="UPS",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="READY",
        erp_state="WAITING",
        checkpoint="NONE",
    )
    waiting = ShipmentRow(
        platform_order_no="112-WAITING",
        system_order_no="SYS-WAITING",
        product_type="tent",
        logistics_no="ALS-WAITING",
        identity_state="ACTIVE",
        logistics_state="WAITING",
        erp_state="WAITING",
    )
    page.update_snapshot(DesktopSnapshot(shipments=[waiting, ready]))
    assert [row.platform_order_no for row in page._rows] == ["111-READY", "112-WAITING"]
    assert page.table.horizontalHeaderItem(6).text() == "处理状态"
    assert page.table.item(0, 6).text() == "可标发"
    assert page.table.item(1, 6).text() == "等待物流就绪"
    page.table.setCurrentCell(1, 1)
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("执行勾选标发不应再显示批量确认弹窗"),
    )

    page._execute_selected()

    assert len(controller.submitted_commands) == 1
    command = controller.submitted_commands[0]
    assert command.order_no == "111-READY"
    assert command.payload["system_order_no"] == "SYS-READY"
    assert command.payload["logistics_no"] == "ALS-READY"
    assert command.payload["shipment_batch_id"]
    assert command.payload["shipment_batch_position"] == 1
    confirmation = DesktopWriteConfirmation.from_payload(command.payload)
    assert confirmation.order_no == "111-READY"
    assert confirmation.system_order_no == "SYS-READY"
    assert confirmation.logistics_no == "ALS-READY"
    assert confirmation.source == "qt_checked_action"
    assert page._checked_logistics_nos == {"ALS-WAITING"}
    assert results[-1].accepted is True
    assert "跳过并保留勾选" in results[-1].message
    page.deleteLater()


def test_confirmed_shipment_uses_new_pair_and_requests_notification(app, monkeypatch):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    row = ShipmentRow(
        platform_order_no="111-CONFIRM",
        system_order_no="SYS-CONFIRM",
        logistics_no="ALS-CONFIRM",
        international_tracking_no="YW-OLD",
        carrier="Yanwen",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="BLOCKED",
        erp_state="WAITING",
        checkpoint="NONE",
    )
    page.update_snapshot(DesktopSnapshot(shipments=[row]))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        _ConfirmedShipmentTrackingDialog,
        "exec",
        lambda _dialog: _ConfirmedShipmentTrackingDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        _ConfirmedShipmentTrackingDialog,
        "values",
        lambda _dialog: ("USPS", "9400111899223856928499"),
    )
    captured: dict[str, object] = {}

    def capture_submission(rows, **kwargs):
        captured["rows"] = tuple(rows)
        captured["kwargs"] = kwargs

    monkeypatch.setattr(page, "_submit_shipment_rows", capture_submission)

    page._confirm_and_execute()

    assert controller.confirm_shipment_calls == [
        (
            "ALS-CONFIRM",
            "USPS",
            "9400111899223856928499",
            "桌面用户人工核对承运商和运单号，并确认标发及发送客户通知",
        )
    ]
    confirmed = captured["rows"][0]
    assert confirmed.carrier == "USPS"
    assert confirmed.international_tracking_no == "9400111899223856928499"
    assert confirmed.logistics_state == "READY"
    assert confirmed.erp_state == "PENDING"
    assert captured["kwargs"]["auto_send_customer_notification"] is True
    page.deleteLater()


def test_remote_shipment_batch_submits_without_blocking_qt_thread(app):
    class SlowShipmentController(RecordingController):
        snapshot_runs_in_background = True

        def __init__(self) -> None:
            super().__init__()
            self.submission_started = threading.Event()
            self.release_submission = threading.Event()

        def submit_task(self, command: TaskCommand) -> ControlResult:
            self.submission_started.set()
            assert self.release_submission.wait(2)
            return super().submit_task(command)

    controller = SlowShipmentController()
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-READY",
                    system_order_no="SYS-READY",
                    product_type="tent",
                    logistics_no="ALS-READY",
                    international_tracking_no="1Z999",
                    carrier="UPS",
                    actual_total="USD 20.00",
                    chargeable_weight_kg="10",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    erp_state="WAITING",
                    checkpoint="NONE",
                )
            ]
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

    started_at = time.monotonic()
    page._execute_selected()

    assert time.monotonic() - started_at < 0.2
    assert not page.execute_button.isEnabled()
    assert controller.submission_started.wait(1)
    controller.release_submission.set()
    deadline = time.monotonic() + 2
    while not results and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert results[-1].accepted
    assert page.execute_button.isEnabled()
    assert page._checked_logistics_nos == set()
    page.deleteLater()


def test_shipment_status_filter_clears_hidden_checks(app):
    page = ShipmentPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-PENDING",
                    logistics_no="ALS-PENDING",
                    identity_state="ACTIVE",
                    logistics_state="PENDING",
                ),
                ShipmentRow(
                    platform_order_no="112-DONE",
                    logistics_no="ALS-DONE",
                    international_tracking_no="1Z999",
                    carrier="UPS",
                    actual_total="USD 20.00",
                    chargeable_weight_kg="10",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    erp_state="DONE",
                    checkpoint="OUTBOUNDED",
                ),
            ]
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)

    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("待查询物流")
    )

    assert [row.platform_order_no for row in page._rows] == ["111-PENDING"]
    assert page._checked_logistics_nos == {"ALS-PENDING"}
    page.deleteLater()


def test_shipment_quick_select_checks_only_visible_executable_orders(app):
    results: list[ControlResult] = []
    page = ShipmentPage(RecordingController(), results.append)
    ready = ShipmentRow(
        platform_order_no="111-READY",
        system_order_no="SYS-READY",
        logistics_no="ALS-READY",
        international_tracking_no="1Z999",
        carrier="UPS",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="READY",
        erp_state="WAITING",
        checkpoint="NONE",
    )
    active = ShipmentRow(
        **{
            **ready.__dict__,
            "platform_order_no": "112-ACTIVE",
            "system_order_no": "SYS-ACTIVE",
            "logistics_no": "ALS-ACTIVE",
        }
    )
    waiting = ShipmentRow(
        platform_order_no="113-WAITING",
        logistics_no="ALS-WAITING",
        identity_state="ACTIVE",
        logistics_state="WAITING",
        erp_state="WAITING",
    )
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[ready, active, waiting],
            tasks=[
                TaskRecord(
                    "active-task",
                    "执行自动标发",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                    status=TaskStatus.RUNNING,
                    order_no="112-ACTIVE",
                    payload={"logistics_no": "ALS-ACTIVE"},
                )
            ],
        )
    )
    page._checked_logistics_nos = {"ALS-WAITING"}

    page._select_visible_ready_shipments()

    assert page._checked_logistics_nos == {"ALS-READY"}
    assert page.quick_select_button.text() == "一键勾选可标发（1）"
    assert page.search_edit.minimumWidth() == 240
    assert page.search_edit.maximumWidth() == 380
    assert results[-1].accepted is True
    page.deleteLater()


def test_state_page_cancels_only_checked_active_tasks(app):
    controller = RecordingController()
    page = StateManagementPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord("task-1", "scan", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS),
                TaskRecord("task-2", "write", TaskArea.SHIPMENT, Capability.OUTBOUND_ORDER),
                TaskRecord(
                    "task-email",
                    "发送客户通知",
                    TaskArea.SHIPMENT,
                    Capability.SEND_NOTIFICATION,
                    payload={"trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER},
                ),
                TaskRecord(
                    "task-3",
                    "done",
                    TaskArea.SHIPMENT,
                    Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                ),
            ]
        )
    )

    page.tasks.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.tasks.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page.tasks.item(2, 0).setCheckState(Qt.CheckState.Checked)
    page._cancel_checked()

    assert controller.cancel_task_calls == [["task-1", "task-2", "task-email"]]
    assert not bool(page.tasks.item(3, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
    page.deleteLater()


def test_every_known_status_filter_uses_raw_value_and_chinese_label(app):
    expected_labels = {
        "pending": "联系方式待处理",
        "folder_pending": "订单文件夹待处理",
        "blocked": "已阻止",
        "sku_adjustment_pending": "SKU 调整待处理",
        "package_split_pending": "拆包待处理",
        "instruction_remark_pending": "说明书备注待处理",
        "warehouse_logistics_pending": "仓库物流待处理",
        "not_required": "不需要（买家申请取消）",
        "completed": "已完成",
        "已忽略": "已忽略",
    }
    rows = [
        CustomOrderRow(
            platform_order_no=f"order-{index}",
            workflow_stage="completed" if status == "已忽略" else status,
            status_text=status,
        )
        for index, status in enumerate(expected_labels, start=1)
    ]
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(DesktopSnapshot(custom_orders=rows))

    for index, (status, label) in enumerate(expected_labels.items(), start=1):
        combo_index = page.status_filter_combo.findData(status)
        assert combo_index >= 0
        assert page.status_filter_combo.itemText(combo_index) == label
        page.status_filter_combo.setCurrentIndex(combo_index)
        assert [row.platform_order_no for row in page._rows] == [f"order-{index}"]
        assert page.table.item(0, 5).text() == label

    page.deleteLater()


def test_filter_clears_hidden_checks_and_batch_scope_is_visible_only(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(
        _status_snapshot(
            ("completed-1", "completed"),
            ("pending-1", "pending"),
            ("pending-2", "pending"),
        )
    )
    completed_index = next(
        index for index, row in enumerate(page._rows) if row.platform_order_no == "completed-1"
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(completed_index, 0).setCheckState(Qt.CheckState.Checked)
    assert page._checked_order_nos == {"pending-1", "completed-1"}

    page.status_filter_combo.setCurrentIndex(page.status_filter_combo.findData("pending"))
    assert page._checked_order_nos == {"pending-1"}
    assert [row.platform_order_no for row in page._rows] == ["pending-1", "pending-2"]
    assert page._check_header.check_state == Qt.CheckState.PartiallyChecked

    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    assert page._checked_order_nos == {"pending-1", "pending-2"}
    page.stage_state_combo.setCurrentIndex(page.stage_state_combo.findData("BLOCKED"))
    monkeypatch.setattr(page, "_reason", lambda _title: "仅处理当前筛选结果")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    page._update_stage_state()

    assert controller.stage_calls == [
        (["pending-1", "pending-2"], "contact", "BLOCKED", "仅处理当前筛选结果")
    ]
    assert "completed-1" not in controller.stage_calls[0][0]
    page.deleteLater()


def test_custom_quick_select_excludes_errors_reviews_blocked_and_active(app):
    results: list[ControlResult] = []
    page = CustomOrdersPage(RecordingController(), results.append)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no="pending-clean",
                    workflow_stage="pending",
                    status_text="pending",
                ),
                CustomOrderRow(
                    platform_order_no="folder-error",
                    workflow_stage="folder_pending",
                    status_text="folder_pending",
                    last_error="文件夹创建失败",
                ),
                CustomOrderRow(
                    platform_order_no="sku-review",
                    workflow_stage="sku_adjustment_pending",
                    status_text="sku_adjustment_pending",
                    retry_confirmation_required=True,
                ),
                CustomOrderRow(
                    platform_order_no="blocked",
                    workflow_stage="blocked",
                    status_text="blocked",
                ),
                CustomOrderRow(
                    platform_order_no="pending-active",
                    workflow_stage="package_split_pending",
                    status_text="package_split_pending",
                ),
            ],
            tasks=[
                TaskRecord(
                    "custom-active",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.RUNNING,
                    order_no="pending-active",
                )
            ],
        )
    )
    page._checked_order_nos = {"folder-error", "blocked"}

    page._select_visible_pending_orders()

    assert page._checked_order_nos == {"pending-clean"}
    assert page.quick_select_button.text() == "一键勾选待处理（1）"
    assert page.search_edit.minimumWidth() == 240
    assert page.search_edit.maximumWidth() == 380
    assert results[-1].accepted is True
    page.deleteLater()


def test_unchanged_custom_snapshot_does_not_rebuild_table(app, monkeypatch):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    render_calls = 0
    original = page._render_rows

    def counted_render(*, selected_order_no=""):
        nonlocal render_calls
        render_calls += 1
        return original(selected_order_no=selected_order_no)

    monkeypatch.setattr(page, "_render_rows", counted_render)
    snapshot = _status_snapshot(
        ("pending-1", "pending"),
        ("completed-1", "completed"),
    )

    page.update_snapshot(snapshot)
    page.update_snapshot(snapshot)

    assert render_calls == 1
    page.deleteLater()


def test_notification_contact_refresh_uses_checked_rows_then_selected_fallback(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 11,
            "platform_order_no": "111-CONTACT",
            "state": "WAITING_CONTACT",
            "package_total": 2,
            "package_complete": 1,
            "package_missing": 1,
            "items": [],
        },
        {
            "id": 12,
            "platform_order_no": "112-CONTACT",
            "state": "BLOCKED",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page._refresh_contacts()

    checked_command = controller.submitted_commands[-1]
    assert checked_command.capability is Capability.GET_ORDER_DETAIL
    assert checked_command.payload["trigger"] == NOTIFICATION_CONTACT_REFRESH_TRIGGER
    assert set(checked_command.payload["notification_ids"]) == {11, 12}

    page._checked_notification_ids.clear()
    page.table.setCurrentCell(1, 2)
    page._refresh_contacts()

    selected_id = int(page.table.item(1, 0).data(Qt.ItemDataRole.UserRole))
    assert controller.submitted_commands[-1].payload["notification_ids"] == [selected_id]
    page.deleteLater()


def test_notification_review_lists_each_pending_wms_system_order(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 13,
            "platform_order_no": "112-PENDING",
            "recipient_name": "Pending Customer",
            "state": "AWAITING_REVIEW",
            "package_total": 2,
            "package_complete": 1,
            "package_missing": 1,
            "items": [
                {
                    "stable_sequence": 1,
                    "display_label": "a",
                    "system_order_no": "10001",
                    "shipment_type": "MANUAL",
                    "carrier_normalized": "Yanwen",
                    "waybill_no": "TRACK-1",
                    "tracking_no": "",
                    "final_tracking_no": "TRACK-1",
                    "customer_visible": 1,
                    "visibility_reason": "",
                    "is_complete": 1,
                },
                {
                    "stable_sequence": 2,
                    "display_label": "",
                    "system_order_no": "20001",
                    "shipment_type": "UNKNOWN",
                    "carrier_normalized": "",
                    "waybill_no": "",
                    "tracking_no": "",
                    "final_tracking_no": "",
                    "customer_visible": 1,
                    "visibility_reason": "pending_wms",
                    "is_complete": 0,
                },
            ],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.package_table.rowCount() == 2
    assert page.package_table.item(1, 1).text() == "待补"
    assert page.package_table.item(1, 2).text() == "20001"
    assert page.package_table.item(1, 8).text() == "待补物流"
    page.deleteLater()


def test_notification_table_selects_one_cell_and_copies_current_value(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 21,
            "platform_order_no": "701-COPY-ORDER",
            "state": "FAILED",
            "last_error": (
                "状态核验超时：供应商已接收通知，但没有返回终态。"
                "这不等于发送失败，请先刷新发送状态。"
            ),
            "provider_status": "Queued / 200",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.table.columnCount() == 9
    assert (
        page.table.selectionBehavior()
        == QAbstractItemView.SelectionBehavior.SelectItems
    )
    page.table.setCurrentCell(0, 1)
    page.table.setFocus()
    QTest.keyClick(page.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == "701-COPY-ORDER"
    assert page.table.item(0, 7).text() == "状态核验失败"
    assert "状态核验超时" in page.table.item(0, 8).text()
    page.deleteLater()


def test_notification_review_page_filters_without_reloading_and_has_one_status_action(
    app,
):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 31,
            "platform_order_no": "701-ALICE",
            "recipient_name": "Alice",
            "recipient_email": "alice@example.com",
            "recipient_phone": "+1-555-0101",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
        {
            "id": 32,
            "platform_order_no": "702-BOB",
            "recipient_name": "Bob",
            "recipient_email": "bob@example.cn",
            "recipient_phone": "+86-13800000000",
            "state": "CANCELLED",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.table.rowCount() == 2
    email_index = page.search_field_combo.findData("recipient_email")
    page.search_field_combo.setCurrentIndex(email_index)
    page.search_edit.setText("EXAMPLE.CN")

    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "702-BOB"
    assert page._notifications == controller.notification_rows
    labels = {button.text() for button in page.findChildren(QPushButton)}
    assert "修改状态" in labels
    assert "勾选设为人工完成" not in labels
    assert "勾选设为已取消" not in labels
    assert not hasattr(page, "content")
    page.deleteLater()


def test_notification_status_action_dispatches_the_selected_target(app, monkeypatch):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 41,
            "platform_order_no": "703-STATUS",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    dispatched: list[list[int]] = []
    monkeypatch.setattr(
        QInputDialog,
        "getItem",
        lambda *_args, **_kwargs: ("已取消", True),
    )
    monkeypatch.setattr(
        page,
        "_cancel_notifications",
        lambda notifications=None: dispatched.append(
            [int(item["id"]) for item in list(notifications or [])]
        ),
    )

    page._change_status()

    assert dispatched == [[41]]
    page.deleteLater()


def test_single_notification_approval_uses_the_full_preview_dialog(app, monkeypatch):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 51,
            "platform_order_no": "704-PREVIEW",
            "recipient_name": "Preview Customer",
            "recipient_email": "preview@example.com",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "subject": "Shipment update",
            "body": "Full email body",
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    previewed: list[list[int]] = []
    monkeypatch.setattr(
        page,
        "_confirm_batch_review",
        lambda notifications: previewed.append(
            [int(item["id"]) for item in notifications]
        )
        or False,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: pytest.fail(
            "Single-item approval must not use the old summary-only dialog."
        ),
    )

    page._approve()

    assert previewed == [[51]]
    assert page._batch_send_thread is None
    page.deleteLater()


def test_notification_approval_submits_visible_cancellable_background_task(
    app,
    monkeypatch,
):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 61,
            "platform_order_no": "705-BACKGROUND",
            "recipient_name": "Background Customer",
            "recipient_email": "background@example.com",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "subject": "Shipment update",
            "body": "Full email body",
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    monkeypatch.setattr(page, "_confirm_batch_review", lambda _items: True)

    page._approve()

    command = controller.submitted_commands[-1]
    assert command.capability is Capability.SEND_NOTIFICATION
    assert command.payload["trigger"] == SHIPMENT_NOTIFICATION_SEND_TRIGGER
    assert command.payload["notification_ids"] == [61]
    assert page._notification_send_task_id == "task-None"

    page.deleteLater()


def test_main_window_refresh_updates_only_visible_page(app, monkeypatch):
    window = DesktopMainWindow(RecordingController())
    window._timer.stop()
    calls = {index: 0 for index in range(len(window._page_widgets))}

    for index, page in enumerate(window._page_widgets):
        monkeypatch.setattr(
            page,
            "update_snapshot",
            lambda _snapshot, current=index: calls.__setitem__(current, calls[current] + 1),
        )

    window.navigation.setCurrentRow(1)
    calls = {index: 0 for index in calls}
    window.refresh()

    assert calls[1] == 1
    assert sum(calls.values()) == 1
    window.deleteLater()
