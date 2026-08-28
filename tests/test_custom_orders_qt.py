from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from erp_automation.client_version import CLIENT_VERSION

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyleOptionViewItem,
    QTableWidget,
)

import erp_automation.ui.qt as qt_module
from erp_automation.coordination.remote_controller import (
    CoordinationClientUpdateRequired,
)
from erp_automation.ui.controller import ControlResult, InMemoryBackgroundTaskController
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    CustomOrderRow,
    DatasetSummary,
    SERVER_CONFIGURED_SECRET,
    SENSITIVE_SETTINGS_FIELDS,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSnapshot,
    DesktopSettings,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LINGXING_BROWSER_LOGIN_TRIGGER,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
    notification_confirmation_order_no,
)


def test_custom_queue_uses_server_page_contract_when_capability_is_advertised(
    app,
) -> None:
    rows = [
        CustomOrderRow(
            f"ORDER-{index:03d}",
            product_type="tent",
            workflow_stage="pending",
            status_text="pending",
        )
        for index in range(120)
    ]
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            custom_orders=rows,
            custom_orders_summary=DatasetSummary(120, "custom-rev"),
            server_features=(
                "custom_order_pagination_v1",
                "snapshot_summary_v1",
            ),
        )
    )
    page = CustomOrdersPage(controller, lambda _result: None)

    page.update_snapshot(controller.snapshot())

    assert len(page._rows) == 50
    assert page._page_count == 3
    assert page.pagination_bar.total_label.text() == "共 120 条"
    page._show_page(3)
    assert page._page == 3
    assert len(page._rows) == 20


def test_shipment_queue_uses_server_page_contract_when_capability_is_advertised(
    app,
) -> None:
    rows = [
        ShipmentRow(
            f"ORDER-{index:03d}",
            logistics_no=f"ALS-{index:03d}",
            identity_state="ACTIVE",
            logistics_state="WAITING",
            erp_state="PENDING",
        )
        for index in range(75)
    ]
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            shipments=rows,
            shipments_summary=DatasetSummary(75, "shipment-rev"),
            server_features=(
                "shipment_pagination_v1",
                "snapshot_summary_v1",
            ),
        )
    )
    page = ShipmentPage(controller, lambda _result: None)

    page.update_snapshot(controller.snapshot())

    assert len(page._rows) == 50
    assert page._page_count == 2
    page._show_page(2)
    assert page._page == 2
    assert len(page._rows) == 25


def test_custom_queue_retries_after_first_server_page_failure(app) -> None:
    class FlakyController(InMemoryBackgroundTaskController):
        calls = 0

        def list_custom_order_page(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary queue failure")
            return super().list_custom_order_page(**kwargs)

    snapshot = DesktopSnapshot(
        custom_orders=[
            CustomOrderRow(
                "ORDER-RETRY",
                product_type="tent",
                workflow_stage="pending",
                status_text="pending",
            )
        ],
        custom_orders_summary=DatasetSummary(1, "custom-retry-rev"),
        server_features=("custom_order_pagination_v1", "snapshot_summary_v1"),
    )
    controller = FlakyController(snapshot)
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)

    page.update_snapshot(controller.snapshot())

    assert controller.calls == 1
    assert page._server_page_state == "error"
    assert page._server_page_retry_timer.isActive() is True
    assert results[-1].details["automatic_retry"] is True

    page.ensure_loaded()

    assert controller.calls == 2
    assert page._server_page_state == "success"
    assert page._server_page_retry_timer.isActive() is False
    assert [row.platform_order_no for row in page._rows] == ["ORDER-RETRY"]


def test_shipment_queue_retries_after_first_server_page_failure(app) -> None:
    class FlakyController(InMemoryBackgroundTaskController):
        calls = 0

        def list_shipment_page(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary queue failure")
            return super().list_shipment_page(**kwargs)

    snapshot = DesktopSnapshot(
        shipments=[
            ShipmentRow(
                "ORDER-RETRY",
                logistics_no="ALS-RETRY",
                identity_state="ACTIVE",
                logistics_state="WAITING",
                erp_state="PENDING",
            )
        ],
        shipments_summary=DatasetSummary(1, "shipment-retry-rev"),
        server_features=("shipment_pagination_v1", "snapshot_summary_v1"),
    )
    controller = FlakyController(snapshot)
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)

    page.update_snapshot(controller.snapshot())

    assert controller.calls == 1
    assert page._server_page_state == "error"
    assert page._server_page_retry_timer.isActive() is True
    assert results[-1].details["automatic_retry"] is True

    page.ensure_loaded()

    assert controller.calls == 2
    assert page._server_page_state == "success"
    assert page._server_page_retry_timer.isActive() is False
    assert [row.logistics_no for row in page._rows] == ["ALS-RETRY"]


def test_custom_queue_does_not_treat_summary_mismatch_as_a_real_empty_queue(
    app,
) -> None:
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            custom_orders=[],
            custom_orders_summary=DatasetSummary(2_908, "custom-server-rev"),
            server_features=("custom_order_pagination_v1", "snapshot_summary_v1"),
        )
    )
    page = CustomOrdersPage(controller, lambda _result: None)

    page.update_snapshot(controller.snapshot())

    assert page._server_page_state == "error"
    assert page._server_page_retry_timer.isActive() is True
    assert "自动重试" in page.server_page_state_label.text()


def test_shipment_queue_does_not_treat_summary_mismatch_as_a_real_empty_queue(
    app,
) -> None:
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            shipments=[],
            shipments_summary=DatasetSummary(468, "shipment-server-rev"),
            server_features=("shipment_pagination_v1", "snapshot_summary_v1"),
        )
    )
    page = ShipmentPage(controller, lambda _result: None)

    page.update_snapshot(controller.snapshot())

    assert page._server_page_state == "error"
    assert page._server_page_retry_timer.isActive() is True
    assert "自动重试" in page.server_page_state_label.text()
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
    _NotificationPackageLogisticsDialog,
    _NotificationStatusDialog,
    _ShipmentStatusDialog,
    _ControlResultThread,
    _interaction_stage_label,
)


@pytest.mark.parametrize("page_type", [CustomOrdersPage, ShipmentPage])
def test_queue_first_load_uses_top_spinner_then_navigation_uses_pager_spinner(
    app,
    page_type,
) -> None:
    page = page_type(
        InMemoryBackgroundTaskController(),
        lambda _result: None,
    )

    page._set_server_page_state("loading", "正在读取不应显示")

    assert page.server_page_spinner.isHidden() is False
    assert page.server_page_state_label.isHidden() is True
    assert page.server_page_state_container.isHidden() is False

    page._set_server_page_state("success")
    page._set_server_page_state("loading", "后续读取不应显示")

    assert page.server_page_spinner.isHidden() is True
    assert page.server_page_state_label.isHidden() is True
    assert page.server_page_state_container.isHidden() is True
    assert page.pagination_bar.loading_spinner.isHidden() is False
    assert page.table.isEnabled() is False

    page._set_server_page_state("error", "读取失败，请重试")

    assert page.server_page_spinner.isHidden() is True
    assert page.server_page_state_label.isHidden() is False
    assert page.server_page_state_label.text() == "读取失败，请重试"
    assert page.server_page_retry_button.isHidden() is False
    assert page.pagination_bar.loading_spinner.isHidden() is True
    assert page.table.isEnabled() is True


@pytest.mark.parametrize("page_type", [CustomOrdersPage, ShipmentPage])
def test_production_scale_queue_first_page_is_painted_within_one_second(
    app,
    page_type,
) -> None:
    class BackgroundQueueController(InMemoryBackgroundTaskController):
        snapshot_runs_in_background = True

    if page_type is CustomOrdersPage:
        rows = [
            CustomOrderRow(
                f"ORDER-{index:04d}",
                product_type="tent",
                workflow_stage="pending",
                status_text="pending",
            )
            for index in range(2_909)
        ]
        revision = "custom-production-scale"
        controller = BackgroundQueueController(
            DesktopSnapshot(
                custom_orders=rows,
                custom_orders_summary=DatasetSummary(len(rows), revision),
            )
        )
        summary = DesktopSnapshot(
            custom_orders_summary=DatasetSummary(len(rows), revision),
            server_features=(
                "custom_order_pagination_v1",
                "snapshot_summary_v1",
            ),
        )
    else:
        rows = [
            ShipmentRow(
                f"ORDER-{index:04d}",
                logistics_no=f"ALS-{index:04d}",
                identity_state="ACTIVE",
                logistics_state="WAITING",
                erp_state="PENDING",
            )
            for index in range(481)
        ]
        revision = "shipment-production-scale"
        controller = BackgroundQueueController(
            DesktopSnapshot(
                shipments=rows,
                shipments_summary=DatasetSummary(len(rows), revision),
            )
        )
        summary = DesktopSnapshot(
            shipments_summary=DatasetSummary(len(rows), revision),
            server_features=(
                "shipment_pagination_v1",
                "snapshot_summary_v1",
            ),
        )

    page = page_type(controller, lambda _result: None)
    page.show()
    started_at = time.perf_counter()
    try:
        page.update_snapshot(summary)
        assert page.server_page_spinner.isHidden() is False
        assert page.server_page_state_label.isHidden() is True

        deadline = started_at + 1.0
        while (
            page._server_page_state != "success"
            and time.perf_counter() < deadline
        ):
            QTest.qWait(5)
        elapsed = time.perf_counter() - started_at

        assert page._server_page_state == "success"
        assert elapsed < 1.0
        assert page.table.rowCount() == 50
        assert page.server_page_state_container.isHidden() is True
    finally:
        cleanup_deadline = time.monotonic() + 2
        while (
            page._server_page_loader.has_running_requests
            and time.monotonic() < cleanup_deadline
        ):
            QTest.qWait(5)
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("page_type", [CustomOrdersPage, ShipmentPage])
def test_server_queue_navigation_is_latest_wins_and_reuses_adjacent_prefetch(
    app,
    page_type,
) -> None:
    page_two_started = threading.Event()
    release_page_two = threading.Event()

    custom_rows = [
        CustomOrderRow(
            f"CUSTOM-{index:03d}",
            product_type="tent",
            workflow_stage="pending",
            status_text="pending",
        )
        for index in range(120)
    ]
    shipment_rows = [
        ShipmentRow(
            f"SHIP-{index:03d}",
            logistics_no=f"ALS-{index:03d}",
            identity_state="ACTIVE",
            logistics_state="WAITING",
            erp_state="PENDING",
        )
        for index in range(120)
    ]

    class ControlledController(InMemoryBackgroundTaskController):
        snapshot_runs_in_background = True

        def __init__(self) -> None:
            super().__init__(
                DesktopSnapshot(
                    custom_orders=custom_rows,
                    shipments=shipment_rows,
                    custom_orders_summary=DatasetSummary(120, "custom-latest"),
                    shipments_summary=DatasetSummary(120, "shipment-latest"),
                )
            )
            self.calls: list[int] = []

        def _wait_for_page_two(self, page: int) -> None:
            self.calls.append(page)
            if page == 2 and not release_page_two.is_set():
                page_two_started.set()
                assert release_page_two.wait(2)

        def list_custom_order_page(self, **kwargs):
            self._wait_for_page_two(int(kwargs.get("page") or 1))
            return super().list_custom_order_page(**kwargs)

        def list_shipment_page(self, **kwargs):
            self._wait_for_page_two(int(kwargs.get("page") or 1))
            return super().list_shipment_page(**kwargs)

    controller = ControlledController()
    revision = "custom-latest" if page_type is CustomOrdersPage else "shipment-latest"
    feature = (
        "custom_order_pagination_v1"
        if page_type is CustomOrdersPage
        else "shipment_pagination_v1"
    )
    summary = DesktopSnapshot(
        custom_orders_summary=DatasetSummary(120, revision),
        shipments_summary=DatasetSummary(120, revision),
        server_features=(feature, "snapshot_summary_v1"),
    )
    page = page_type(controller, lambda _result: None)
    page.show()
    try:
        page.update_snapshot(summary)
        deadline = time.monotonic() + 2
        while page._server_page_state != "success" and time.monotonic() < deadline:
            QTest.qWait(5)
        assert page._page == 1
        assert page_two_started.wait(1)
        assert controller.calls.count(2) == 1

        page._show_page(2)
        assert page._page == 1
        assert page.pagination_bar.loading_target_page == 2
        assert page.pagination_bar.loading_spinner.isHidden() is False
        assert page.table.isEnabled() is False

        page._show_page(3)
        deadline = time.monotonic() + 2
        while page._page != 3 and time.monotonic() < deadline:
            QTest.qWait(5)
        assert page._page == 3
        assert page.pagination_bar.loading_spinner.isHidden() is True
        assert page.table.isEnabled() is True
        assert controller.calls.count(3) == 1

        release_page_two.set()
        deadline = time.monotonic() + 2
        while page._server_page_loader.has_running_requests and time.monotonic() < deadline:
            QTest.qWait(5)
        assert page._page == 3
        assert controller.calls.count(2) == 1
    finally:
        release_page_two.set()
        deadline = time.monotonic() + 2
        while page._server_page_loader.has_running_requests and time.monotonic() < deadline:
            QTest.qWait(5)
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("page_type", [CustomOrdersPage, ShipmentPage])
def test_server_queue_page_failure_keeps_previous_page_and_manual_retry_recovers(
    app,
    page_type,
) -> None:
    allow_page_two = False
    custom_rows = [
        CustomOrderRow(
            f"CUSTOM-{index:03d}",
            workflow_stage="pending",
            status_text="pending",
        )
        for index in range(75)
    ]
    shipment_rows = [
        ShipmentRow(
            f"SHIP-{index:03d}",
            logistics_no=f"ALS-{index:03d}",
            identity_state="ACTIVE",
            logistics_state="WAITING",
            erp_state="PENDING",
        )
        for index in range(75)
    ]

    class RecoveringController(InMemoryBackgroundTaskController):
        snapshot_runs_in_background = True

        def __init__(self) -> None:
            super().__init__(
                DesktopSnapshot(
                    custom_orders=custom_rows,
                    shipments=shipment_rows,
                    custom_orders_summary=DatasetSummary(75, "custom-recovery"),
                    shipments_summary=DatasetSummary(75, "shipment-recovery"),
                )
            )

        @staticmethod
        def _fail_page_two(page: int) -> None:
            if page == 2 and not allow_page_two:
                raise RuntimeError("temporary page-two failure")

        def list_custom_order_page(self, **kwargs):
            self._fail_page_two(int(kwargs.get("page") or 1))
            return super().list_custom_order_page(**kwargs)

        def list_shipment_page(self, **kwargs):
            self._fail_page_two(int(kwargs.get("page") or 1))
            return super().list_shipment_page(**kwargs)

    controller = RecoveringController()
    revision = "custom-recovery" if page_type is CustomOrdersPage else "shipment-recovery"
    feature = (
        "custom_order_pagination_v1"
        if page_type is CustomOrdersPage
        else "shipment_pagination_v1"
    )
    summary = DesktopSnapshot(
        custom_orders_summary=DatasetSummary(75, revision),
        shipments_summary=DatasetSummary(75, revision),
        server_features=(feature, "snapshot_summary_v1"),
    )
    page = page_type(controller, lambda _result: None)
    page.show()
    try:
        page.update_snapshot(summary)
        deadline = time.monotonic() + 2
        while (
            page._server_page_state != "success"
            or page._server_page_loader.has_running_requests
        ) and time.monotonic() < deadline:
            QTest.qWait(5)
        original_rows = tuple(page._rows)

        page._show_page(2)
        deadline = time.monotonic() + 2
        while page._server_page_state != "error" and time.monotonic() < deadline:
            QTest.qWait(5)
        assert page._page == 1
        assert tuple(page._rows) == original_rows
        assert page.pagination_bar.page == 1
        assert page.pagination_bar.loading_spinner.isHidden() is True
        assert page.table.isEnabled() is True
        assert page.server_page_retry_button.isHidden() is False

        allow_page_two = True
        page.server_page_retry_button.click()
        deadline = time.monotonic() + 2
        while page._page != 2 and time.monotonic() < deadline:
            QTest.qWait(5)
        assert page._page == 2
        assert page.server_page_state_container.isHidden() is True
        assert page.table.isEnabled() is True
    finally:
        deadline = time.monotonic() + 2
        while page._server_page_loader.has_running_requests and time.monotonic() < deadline:
            QTest.qWait(5)
        page.close()
        page.deleteLater()


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
        self.notification_resubmit_calls: list[tuple[int, str]] = []
        self.notification_package_edit_calls: list[
            tuple[int, str, str, str, str]
        ] = []

    def list_shipment_notifications(self) -> list[dict[str, object]]:
        return list(self.notification_rows)

    def submit_task(self, command: TaskCommand) -> ControlResult:
        self.submitted_commands.append(command)
        return self.task_results.get(
            str(command.order_no or ""),
            ControlResult(True, "已排队", f"task-{command.order_no}"),
        )

    def resubmit_shipment_notification(
        self, notification_id: int, *, reason: str
    ) -> ControlResult:
        self.notification_resubmit_calls.append((notification_id, reason))
        return self.result

    def edit_shipment_notification_package(
        self,
        notification_id: int,
        *,
        package_key: str,
        carrier: str,
        tracking_no: str,
        reason: str,
    ) -> ControlResult:
        self.notification_package_edit_calls.append(
            (notification_id, package_key, carrier, tracking_no, reason)
        )
        return ControlResult(
            True,
            "已修改",
            details={"notification_id": notification_id + 1000},
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


def test_alibaba_order_page_displays_and_copies_ephemeral_quote_details(app) -> None:
    page = AlibabaOrderPage(RecordingController(), lambda _result: None)
    page.system_order_edit.setText("SYS-QUOTE-100")
    request = DesktopInteractionRequest(
        request_id="quote-details-1",
        task_id="task-quote-1",
        stage="alibaba_order:quote_details",
        title="阿里查价资料已准备",
        message="transient",
        display_data={
            "requested_order_no": "SYS-QUOTE-100",
            "system_order_no": "SYS-QUOTE-100",
            "platform_order_no": "113-1234567-1234567",
            "origin_country": "中国大陆",
            "origin_city": "佛山市",
            "destination_country_code": "CA",
            "destination_country_name": "Canada",
            "destination_postal_code": "N2R 1A6",
            "category": "vinyl_banner",
            "category_label": "喷绘类",
        },
    )

    assert page.quote_info_frame.isHidden() is True
    assert page.apply_quote_details(request) is True
    assert page.quote_info_frame.isHidden() is False
    assert page.quote_origin_label.text() == "中国大陆 / 佛山市"
    assert page.quote_destination_label.text() == "加拿大（CA）"
    assert page.quote_postal_label.text() == "N2R 1A6"
    assert page.quote_category_label.text() == "喷绘类"
    assert page.heavy_checkbox.isChecked() is False
    assert page.heavy_checkbox.isEnabled() is False
    assert "SYS-QUOTE-100" in page.quote_order_label.text()
    assert "113-1234567-1234567" in page.quote_order_label.text()

    page._copy_postal_code()

    assert QApplication.clipboard().text() == "N2R 1A6"
    assert page.copy_postal_button.text() == "已复制"

    page.system_order_edit.setText("SYS-QUOTE-NEW")

    assert page.quote_info_frame.isHidden() is True
    assert page.copy_postal_button.isEnabled() is False
    assert page.quote_postal_label.text() == "-"
    assert page.quote_category_label.text() == "-"
    assert page.heavy_checkbox.isEnabled() is True
    page.deleteLater()


def test_main_window_routes_quote_details_to_page_without_a_modal(app) -> None:
    request = DesktopInteractionRequest(
        request_id="quote-details-window",
        task_id="task-quote-window",
        stage="alibaba_order:quote_details",
        title="阿里查价资料已准备",
        message="transient",
        display_data={
            "requested_order_no": "SYS-WINDOW-100",
            "system_order_no": "SYS-WINDOW-100",
            "platform_order_no": "",
            "origin_country": "中国大陆",
            "origin_city": "佛山市",
            "destination_country_code": "US",
            "destination_country_name": "United States",
            "destination_postal_code": "90012",
        },
    )

    class QuoteInteractionController(RecordingController):
        def __init__(self):
            super().__init__()
            self.request: DesktopInteractionRequest | None = None
            self.responses: list[DesktopInteractionResponse] = []

        def pending_interactions(self):
            return (self.request,) if self.request is not None else ()

        def respond_interaction(self, response):
            self.responses.append(response)
            self.request = None
            return ControlResult(True, "已显示")

    controller = QuoteInteractionController()
    window = DesktopMainWindow(controller)
    try:
        window.alibaba_order_page.system_order_edit.setText("SYS-WINDOW-100")
        controller.request = request

        window._show_next_interaction()

        assert window.alibaba_order_page.quote_info_frame.isHidden() is False
        assert window.alibaba_order_page.quote_postal_label.text() == "90012"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not controller.responses:
            QTest.qWait(10)
        assert len(controller.responses) == 1
        assert controller.responses[0].request_id == request.request_id
        assert controller.responses[0].accepted is True
    finally:
        window.close()


def test_settings_page_forces_first_server_hydration_before_preserving_edits(
    app,
) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)
    page._dirty = True

    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                lingxing_app_id="server-app-id",
                shipment_tag_name="服务器标签",
            ),
            operator_email="yrq@billyprint.com",
        )
    )

    assert page._hydrated is True
    assert page._dirty is False
    assert page.app_id.text() == "server-app-id"
    assert page.shipment_tag_name.text() == "服务器标签"
    assert page.settings_load_state_label.isHidden() is True

    page._mark_dirty()
    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                lingxing_app_id="new-server-app-id",
                shipment_tag_name="新服务器标签",
            ),
            operator_email="yrq@billyprint.com",
        )
    )

    assert page.app_id.text() == "server-app-id"
    assert page.shipment_tag_name.text() == "服务器标签"


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
                alibaba_logistics_query_account="query@example.com",
                alibaba_logistics_query_password=SERVER_CONFIGURED_SECRET,
                shipment_tag_name="客户待标发",
            ),
            configured_secret_lengths={
                "lingxing_app_secret": 7,
                "amazon_refresh_token": 19,
                "alibaba_logistics_query_password": 21,
            },
        )
    )

    assert page.app_id.text() == "visible-app-id"
    assert page.app_secret.text() == ""
    assert page.alibaba_logistics_query_account.text() == "query@example.com"
    assert page.alibaba_logistics_query_password.text() == ""
    assert page.shipment_tag_name.text() == "客户待标发"
    assert bool(
        page.alibaba_logistics_query_password.property(
            "server_secret_configured"
        )
    )
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
    assert "登录领星账号" in button_texts
    assert "导入旧 .env" not in button_texts
    assert "状态迁移预检" not in button_texts
    assert "JSON 迁入 SQLite" not in button_texts
    assert not hasattr(page, "migration_status")


def test_settings_page_submits_current_host_lingxing_login_task(app) -> None:
    controller = RecordingController()
    results: list[ControlResult] = []
    page = SettingsPage(controller, results.append)

    page.lingxing_login_button.click()

    assert len(controller.submitted_commands) == 1
    command = controller.submitted_commands[0]
    assert command.area is TaskArea.MAINTENANCE
    assert command.capability is Capability.LIST_ORDERS
    assert command.payload["trigger"] == LINGXING_BROWSER_LOGIN_TRIGGER
    assert results[-1].accepted is True
    page.deleteLater()


def test_settings_page_requires_saved_lingxing_credentials_before_login(app) -> None:
    controller = RecordingController()
    results: list[ControlResult] = []
    page = SettingsPage(controller, results.append)
    page._mark_dirty()

    page.lingxing_login_button.click()

    assert controller.submitted_commands == []
    assert results[-1].accepted is False
    assert "先保存" in results[-1].message
    page.deleteLater()


def test_settings_import_accepts_access_only_profile_on_another_host(
    app,
    monkeypatch,
    tmp_path,
) -> None:
    from erp_automation.coordination.access_profile import (
        export_client_access_profile,
    )
    from erp_automation.coordination.client_bootstrap import (
        SERVER_HOST,
        SERVER_USER,
    )

    source_root = tmp_path / "source-access"
    source_root.mkdir()
    source_root.joinpath("server-tunnel-ed25519").write_bytes(
        b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
        b"dGVzdC1wcml2YXRlLWtleQ==\n"
        b"-----END OPENSSH PRIVATE KEY-----\n"
    )
    source_root.joinpath("known_hosts").write_bytes(
        b"example.test ssh-ed25519 dGVzdC1ob3N0LWtleQ==\n"
    )
    source_root.joinpath("coordination-token").write_text(
        "portable-client-token-" + ("x" * 32),
        encoding="utf-8",
    )
    passphrase = "correct horse battery staple"
    profile_path = tmp_path / "access-only.erp-client"
    export_client_access_profile(
        profile_path,
        passphrase,
        state_root=source_root,
        server_host=SERVER_HOST,
        server_user=SERVER_USER,
    )
    local_appdata = tmp_path / "other-host-local-appdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setattr(
        qt_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(profile_path), ""),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    results: list[ControlResult] = []
    page = SettingsPage(RecordingController(), results.append)
    monkeypatch.setattr(
        page,
        "_ask_passphrase",
        lambda *, confirm: passphrase,
    )

    page._import_portable()

    assert results[-1].accepted is True
    assert "不含设置备份" in results[-1].message
    target_root = local_appdata / "LingxingERP"
    assert target_root.joinpath("server-tunnel-ed25519").is_file()
    assert target_root.joinpath("known_hosts").is_file()
    assert target_root.joinpath("coordination-token").is_file()
    page.deleteLater()


def test_settings_dirty_snapshot_is_not_consumed_before_it_can_be_applied(
    app,
) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)
    initial = DesktopSnapshot(
        settings=DesktopSettings(lingxing_app_id=""),
        configuration_fingerprint="a" * 64,
    )
    imported = DesktopSnapshot(
        settings=DesktopSettings(lingxing_app_id="imported-app"),
        configuration_fingerprint="b" * 64,
    )
    page.update_snapshot(initial)
    initial_signature = page._last_signature
    page._mark_dirty()

    page.update_snapshot(imported)

    assert page.app_id.text() == ""
    assert page._last_signature == initial_signature
    page._dirty = False
    page.update_snapshot(imported)
    assert page.app_id.text() == "imported-app"
    assert page._last_signature != initial_signature
    page.deleteLater()


def test_settings_import_forces_verified_readback_and_refreshes_dirty_page(
    app,
    monkeypatch,
    tmp_path,
) -> None:
    fingerprint = "c" * 64

    class ImportController(RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.imported = False

        def import_portable_migration(
            self,
            package_path: str,
            passphrase: str,
            *,
            overwrite: bool,
            configuration_only: bool = False,
        ) -> ControlResult:
            assert Path(package_path).read_bytes() == b"encrypted-package"
            assert passphrase == "portable configuration password"
            assert overwrite is True
            assert configuration_only is True
            self.imported = True
            return ControlResult(
                True,
                "imported",
                details={
                    "target_operator_email": "alice@billyprint.com",
                    "configuration_fingerprint": fingerprint,
                    "configured_non_sensitive_field_count": 2,
                    "configured_secret_field_count": 1,
                },
            )

        def snapshot(self) -> DesktopSnapshot:
            if not self.imported:
                return DesktopSnapshot(
                    operator_email="alice@billyprint.com",
                    configuration_fingerprint="a" * 64,
                )
            return DesktopSnapshot(
                settings=DesktopSettings(
                    lingxing_app_id="imported-app",
                    lingxing_app_secret=SERVER_CONFIGURED_SECRET,
                ),
                configured_secret_lengths={"lingxing_app_secret": 17},
                operator_email="alice@billyprint.com",
                configuration_fingerprint=fingerprint,
                configured_non_sensitive_field_count=2,
                configured_secret_field_count=1,
                configuration_is_default=False,
            )

    package = tmp_path / "settings.erp-migrate"
    package.write_bytes(b"encrypted-package")
    controller = ImportController()
    results: list[ControlResult] = []
    page = SettingsPage(controller, results.append)
    page.update_snapshot(controller.snapshot())
    page._mark_dirty()
    monkeypatch.setattr(
        qt_module.QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(package), ""),
    )
    monkeypatch.setattr(
        page,
        "_ask_passphrase",
        lambda *, confirm: "portable configuration password",
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page._import_portable()

    assert results[-1].accepted is True
    assert results[-1].details["configuration_readback_verified"] is True
    assert "alice@billyprint.com" in results[-1].message
    assert page._dirty is False
    assert page.app_id.text() == "imported-app"
    assert page.app_secret.text() == ""
    assert page.app_secret.placeholderText() == "●" * 17
    page.deleteLater()


def test_settings_export_rejects_unsaved_page_without_opening_picker(
    app,
    monkeypatch,
) -> None:
    results: list[ControlResult] = []
    page = SettingsPage(RecordingController(), results.append)
    page.update_snapshot(
        DesktopSnapshot(
            configuration_fingerprint="a" * 64,
            configuration_is_default=False,
        )
    )
    page._mark_dirty()
    monkeypatch.setattr(
        qt_module.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: pytest.fail(
            "Unsaved settings must be saved before choosing an export path."
        ),
    )

    page._export_portable()

    assert results[-1].accepted is False
    assert "未保存" in results[-1].message
    page.deleteLater()


def test_settings_export_warns_before_exporting_default_configuration(
    app,
    monkeypatch,
) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            configuration_fingerprint="a" * 64,
            configuration_is_default=True,
        )
    )
    questions: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, message, *_args: (
            questions.append(message) or QMessageBox.StandardButton.No
        ),
    )
    monkeypatch.setattr(
        qt_module.QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: pytest.fail(
            "Cancelling the default-config warning must stop export."
        ),
    )

    page._export_portable()

    assert len(questions) == 1
    assert "默认设置" in questions[0]
    page.deleteLater()


def test_settings_page_saves_the_configurable_shipment_scan_tag(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(settings=DesktopSettings(shipment_tag_name="客户待标发"))
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    page.shipment_tag_name.setText("新的标发标签")
    page._save()

    assert controller.snapshot().settings.shipment_tag_name == "新的标发标签"
    page.deleteLater()


def test_settings_page_saves_high_value_split_weight_threshold(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(settings=DesktopSettings(high_value_split_weight_kg=4))
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    assert page.high_value_split_weight.currentData() == 4
    page.high_value_split_weight.setCurrentIndex(
        page.high_value_split_weight.findData(5)
    )
    page._save()

    assert controller.snapshot().settings.high_value_split_weight_kg == 5
    page.deleteLater()


def test_settings_page_saves_independent_execution_review_switches(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                custom_order_review_enabled=True,
                shipment_review_enabled=False,
            )
        )
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    assert page.custom_order_review_enabled.isChecked() is True
    assert page.shipment_review_enabled.isChecked() is False
    page.custom_order_review_enabled.setChecked(False)
    page.shipment_review_enabled.setChecked(True)
    page._save()

    saved = controller.snapshot().settings
    assert saved.custom_order_review_enabled is False
    assert saved.shipment_review_enabled is True
    page.deleteLater()


def test_settings_page_uses_exact_length_for_every_server_secret(app) -> None:
    secret_fields = {
        "lingxing_app_secret": ("app_secret", 5),
        "lingxing_password": ("lingxing_password", 8),
        "alibaba_password": ("alibaba_password", 11),
        "alibaba_logistics_query_password": (
            "alibaba_logistics_query_password",
            12,
        ),
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


def test_every_masked_setting_has_native_password_mode_and_eye_action(app) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)

    assert set(page._sensitive_field_names.values()) == set(
        SENSITIVE_SETTINGS_FIELDS
    )
    assert set(page._sensitive_visibility_actions) == set(
        page._sensitive_editors
    )
    for editor in page._sensitive_editors:
        assert editor.echoMode() == editor.EchoMode.Password
        action = page._sensitive_visibility_actions[editor]
        assert action.text() == "显示密码"
        assert action.icon().isNull() is False
    page.deleteLater()


def test_masked_setting_reveals_on_demand_and_keeps_untouched_save_semantics(
    app,
) -> None:
    secret = "Saved-P@ss_42"
    controller = RecordingController()
    controller.save_settings(DesktopSettings(lingxing_app_secret=secret))
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                lingxing_app_secret=SERVER_CONFIGURED_SECRET,
            ),
            configured_secret_lengths={"lingxing_app_secret": len(secret)},
        )
    )

    page.show()
    QTest.mouseClick(page.app_secret, Qt.MouseButton.LeftButton)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and page.app_secret.text() != secret:
        app.processEvents()
        QTest.qWait(10)

    assert page.app_secret.text() == secret
    assert page.app_secret.echoMode() == page.app_secret.EchoMode.Password
    assert bool(page.app_secret.property("secret_materialized")) is True
    assert bool(page.app_secret.property("server_secret_configured")) is True
    assert page._secret_value(page.app_secret) == ""

    page._sensitive_visibility_actions[page.app_secret].trigger()
    assert page.app_secret.echoMode() == page.app_secret.EchoMode.Normal
    page._sensitive_visibility_actions[page.app_secret].trigger()
    assert page.app_secret.echoMode() == page.app_secret.EchoMode.Password
    page.hide()
    page.deleteLater()


def test_masked_setting_supports_native_paste_backspace_delete_and_select_all(
    app,
) -> None:
    page = SettingsPage(RecordingController(), lambda _result: None)
    editor = page.alibaba_password
    pasted = "P@ss&Case_42!xYz"
    page.show()
    editor.setFocus()
    QApplication.clipboard().setText(pasted)

    QTest.keyClick(
        editor,
        Qt.Key.Key_V,
        Qt.KeyboardModifier.ControlModifier,
    )
    assert editor.text() == pasted

    editor.setCursorPosition(len(editor.text()))
    QTest.keyClick(editor, Qt.Key.Key_Backspace)
    assert editor.text() == pasted[:-1]

    editor.setCursorPosition(0)
    QTest.keyClick(editor, Qt.Key.Key_Delete)
    assert editor.text() == pasted[1:-1]

    QTest.keyClick(
        editor,
        Qt.Key.Key_A,
        Qt.KeyboardModifier.ControlModifier,
    )
    assert editor.selectedText() == pasted[1:-1]
    assert page._secret_value(editor) == pasted[1:-1]
    page.hide()
    page.deleteLater()


def test_leaving_settings_clears_only_materialized_server_secret(app) -> None:
    secret = "temporary-server-secret"
    controller = RecordingController()
    controller.save_settings(DesktopSettings(amazon_refresh_token=secret))
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            settings=DesktopSettings(
                amazon_refresh_token=SERVER_CONFIGURED_SECRET,
            ),
            configured_secret_lengths={"amazon_refresh_token": len(secret)},
        )
    )
    page.show()
    page._sensitive_visibility_actions[page.amazon_refresh_token].trigger()
    assert page.amazon_refresh_token.text() == secret

    page.hide()
    QApplication.processEvents()

    assert page.amazon_refresh_token.text() == ""
    assert page.amazon_refresh_token.placeholderText() == "●" * len(secret)
    assert (
        page.amazon_refresh_token.echoMode()
        == page.amazon_refresh_token.EchoMode.Password
    )
    assert page._secret_value(page.amazon_refresh_token) == ""
    page.deleteLater()


def test_successful_save_immediately_clears_edited_secret_from_widget(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    page = SettingsPage(controller, lambda _result: None)
    page.update_snapshot(controller.snapshot())
    secret = "newly-saved-P@ss_84"
    page.alibaba_password.setText(secret)
    page._sensitive_text_edited(page.alibaba_password, secret)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)

    page._save()

    assert controller.snapshot().settings.alibaba_password == secret
    assert page.alibaba_password.text() == ""
    assert page.alibaba_password.placeholderText() == "●" * len(secret)
    assert bool(
        page.alibaba_password.property("server_secret_configured")
    ) is True
    assert page.alibaba_password.echoMode() == page.alibaba_password.EchoMode.Password
    page.deleteLater()


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
    page.deleteLater()


def test_settings_save_shows_explicit_success_modal(app, monkeypatch) -> None:
    controller = RecordingController()
    results: list[ControlResult] = []
    page = SettingsPage(controller, results.append)
    monkeypatch.setattr(
        controller,
        "save_settings",
        lambda _settings: ControlResult(True, "配置已安全保存。"),
    )
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, message: messages.append((title, message)),
    )

    page._save()

    assert messages == [("保存成功", "配置已安全保存。")]
    assert results[-1].accepted is True
    assert page._dirty is False
    page.deleteLater()


def test_settings_save_shows_explicit_failure_modal_once(app, monkeypatch) -> None:
    controller = RecordingController()
    results: list[ControlResult] = []
    page = SettingsPage(controller, results.append)
    monkeypatch.setattr(
        controller,
        "save_settings",
        lambda _settings: ControlResult(False, "服务器拒绝保存。"),
    )
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, title, message: messages.append((title, message)),
    )

    page._save()

    assert messages == [("保存失败", "服务器拒绝保存。")]
    assert results[-1].accepted is False
    assert results[-1].details["non_modal"] is True
    page.deleteLater()


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


def test_main_window_separates_instance_pause_and_write_stop(app):
    controller = RecordingController()
    controller.instance_pause_supported = True
    window = DesktopMainWindow(controller)
    try:
        assert window.local_pause_button.text() == "暂停本机任务"
        assert "不会改变全局 ERP 写入急停" in window.local_pause_button.toolTip()

        local_pause = DesktopSnapshot()
        local_pause.policy.instance_execution_paused = True
        local_pause.policy.instance_execution_pause_state = "pausing"
        local_pause.policy.execution_paused = True
        local_pause.policy.instance_pause_target_count = 3
        local_pause.policy.instance_pause_stopped_count = 1
        local_pause.policy.instance_pause_stopping_count = 2
        local_pause.policy.emergency_stop_writes = False
        window._apply_snapshot(local_pause)
        assert window.local_pause_button.text() == "正在停止本机任务…"
        assert window.local_pause_button.isEnabled() is False
        assert "已停止 1/3" in window.execution_pause_banner.text()
        assert window.emergency_banner.isHidden() is True

        emergency = DesktopSnapshot()
        emergency.policy.emergency_stop_writes = True
        window._apply_snapshot(emergency)
        assert not hasattr(window, "global_recovery_panel")
        assert not hasattr(window, "clear_global_pause_button")
        assert not hasattr(window, "global_pause_button")
        assert window.global_emergency_button.text() == "解除急停"
        assert window.global_emergency_button.isEnabled() is True
        assert "后续 ERP 写入" in window.global_emergency_button.toolTip()
    finally:
        window.close()


def test_stage_review_dialog_keeps_main_window_pause_available(app, monkeypatch):
    request = DesktopInteractionRequest(
        request_id="shipment-stage-review-pause",
        task_id="shipment-task-pause",
        stage="erp_mark:stage_review:设置仓库物流",
        title="审核自动标发阶段：设置仓库物流",
        message="即将执行设置仓库物流。",
        approve_label="确认当前阶段",
        reject_label="拒绝并停止当前订单",
    )

    class InteractionPauseController(RecordingController):
        def __init__(self):
            super().__init__()
            self.request: DesktopInteractionRequest | None = None
            self.responses: list[DesktopInteractionResponse] = []
            self.pause_calls: list[tuple[bool, str]] = []

        def pending_interactions(self):
            return (self.request,) if self.request is not None else ()

        def respond_interaction(self, response):
            self.responses.append(response)
            self.request = None
            return ControlResult(True, "已响应")

        def set_execution_paused(self, enabled: bool, reason: str = ""):
            self.pause_calls.append((enabled, reason))
            self.request = None
            return ControlResult(
                True,
                "本机任务已暂停。",
                details={
                    "instance_execution_pause_state": "paused",
                    "target_count": 1,
                    "stopped_count": 1,
                },
            )

    confirmation_titles: list[str] = []

    def confirm_pause(_parent, title, _message, *_args):
        confirmation_titles.append(title)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "warning", confirm_pause)
    controller = InteractionPauseController()
    window = DesktopMainWindow(controller)
    window.show()
    try:
        controller.request = request
        window._show_next_interaction()
        QTest.qWait(20)

        dialog = window._active_interaction_dialog
        assert dialog is not None
        assert dialog.isVisible() is True
        assert dialog.isModal() is False
        assert dialog.windowModality() is Qt.WindowModality.NonModal
        assert window.isEnabled() is True
        assert dialog.findChild(QPushButton, "interactionLocalPauseButton") is not None

        window.local_pause_button.click()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (
            not controller.pause_calls
            or window._execution_pause_thread is not None
            or window._active_interaction_id is not None
        ):
            QTest.qWait(10)

        assert confirmation_titles == ["暂停本机任务"]
        assert controller.pause_calls == [(True, "用户从主界面暂停本机任务。")]
        assert controller.responses == []
        assert window._active_interaction_dialog is None
        assert window._active_interaction_id is None
    finally:
        window.close()


def test_non_modal_stage_review_still_submits_user_approval(app):
    request = DesktopInteractionRequest(
        request_id="shipment-stage-review-approve",
        task_id="shipment-task-approve",
        stage="erp_mark:stage_review:审核运单填写信息",
        title="审核自动标发阶段：审核运单填写信息",
        message="即将写入运单信息。",
    )

    class InteractionController(RecordingController):
        def __init__(self):
            super().__init__()
            self.request: DesktopInteractionRequest | None = request
            self.responses: list[DesktopInteractionResponse] = []

        def pending_interactions(self):
            return (self.request,) if self.request is not None else ()

        def respond_interaction(self, response):
            self.responses.append(response)
            self.request = None
            return ControlResult(True, "已响应")

    controller = InteractionController()
    window = DesktopMainWindow(controller)
    try:
        window._show_next_interaction()
        dialog = window._active_interaction_dialog
        assert dialog is not None

        dialog.accept()
        QTest.qWait(20)

        assert len(controller.responses) == 1
        assert controller.responses[0].request_id == request.request_id
        assert controller.responses[0].accepted is True
        assert window._active_interaction_id is None
        assert window._active_interaction_dialog is None
    finally:
        window.close()


def test_all_main_window_tables_allow_interactive_column_resizing(app):
    window = DesktopMainWindow(RecordingController())
    try:
        window.resize(1600, 900)
        window.show()
        QTest.qWait(30)
        tables = window.findChildren(QTableWidget)
        assert len(tables) == 8
        for table in tables:
            assert isinstance(
                getattr(table, "_adaptive_column_controller", None),
                qt_module._AdaptiveTableColumnController,
            )
            header = table.horizontalHeader()
            assert header.stretchLastSection() is False
            assert all(
                header.sectionResizeMode(column)
                == QHeaderView.ResizeMode.Interactive
                for column in range(table.columnCount())
            )

        table_pages = (
            window.dashboard_page,
            window.custom_orders_page,
            window.shipment_page,
            window.notification_page,
            window.state_page,
            window.logs_page,
        )
        for page in table_pages:
            window.pages.setCurrentWidget(page)
            QTest.qWait(20)
            page_tables = page.findChildren(QTableWidget)
            assert page_tables
            for table in page_tables:
                assert abs(
                    table.horizontalHeader().length() - table.viewport().width()
                ) <= 1
    finally:
        window.close()


def test_interactive_table_columns_adapt_to_viewport_and_preserve_user_ratio(app):
    table = QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(["选择", "短字段", "关键说明", "状态"])
    qt_module._prepare_table(table, full_cell_check_column=0)
    qt_module._set_table_default_widths(
        table,
        (40, 100, 240, 100),
    )
    table.resize(900, 300)
    table.show()
    QTest.qWait(20)

    header = table.horizontalHeader()
    assert abs(header.length() - table.viewport().width()) <= 1
    assert table.columnWidth(0) == 40
    assert table.columnWidth(2) > table.columnWidth(1)
    assert all(
        header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
        for column in range(table.columnCount())
    )

    table.setColumnWidth(1, 300)
    table.resize(1100, 300)
    QTest.qWait(20)
    assert abs(header.length() - table.viewport().width()) <= 1
    assert table.columnWidth(0) == 40
    assert table.columnWidth(1) > table.columnWidth(3)

    table.resize(720, 300)
    QTest.qWait(20)
    assert abs(header.length() - table.viewport().width()) <= 1
    assert table.columnWidth(0) == 40
    assert table.columnWidth(1) > table.columnWidth(3)
    table.close()
    table.deleteLater()


def test_local_test_window_has_persistent_visible_identity(app, monkeypatch):
    monkeypatch.setenv("ERP_AUTOMATION_LOCAL_TEST", "1")
    monkeypatch.setenv("ERP_AUTOMATION_LOCAL_TEST_SHARED_SERVER", "1")
    monkeypatch.setenv(
        "ERP_AUTOMATION_LOCAL_TEST_FORMAL_BASELINE_VERSION",
        "2026.08.06.1",
    )
    window = DesktopMainWindow(RecordingController())
    try:
        assert window.windowTitle() == (
            f"ERP 自动化控制台（本机测试 · 源码 {CLIENT_VERSION}）"
        )
        assert window.local_test_banner.isHidden() is False
        assert "工作分支客户端源码" in window.local_test_banner.text()
        assert f"源码目标 {CLIENT_VERSION}" in window.local_test_banner.text()
        assert "正式连接基线 2026.08.06.1" in window.local_test_banner.text()
        assert "订单来自正式业务环境" in window.local_test_banner.text()
        assert "任何写入都会影响真实数据" in window.local_test_banner.text()
        labels = [label.text() for label in window.findChildren(QLabel)]
        assert "ERP 自动化 · 本机测试" in labels
        assert f"源码 {CLIENT_VERSION} / 正式基线 2026.08.06.1" in labels
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


def test_all_status_description_tables_show_brief_text_and_full_tooltips(app):
    full_message = (
        "订单数据在后台复核期间发生变化，系统已经停止当前处理流程；"
        "请刷新数据并重新确认后再继续执行。"
    )
    expected_brief = qt_module._concise_status_text(full_message)
    task = TaskRecord(
        task_id="task-long-status",
        name="处理长状态说明",
        area=TaskArea.CUSTOMIZATION,
        capability=Capability.UPDATE_CONTACT,
        message=full_message,
    )
    dashboard = DashboardPage()
    state = StateManagementPage(RecordingController(), lambda _result: None)
    custom = CustomOrdersPage(RecordingController(), lambda _result: None)
    shipment = ShipmentPage(RecordingController(), lambda _result: None)
    try:
        dashboard.update_snapshot(DesktopSnapshot(today_tasks=[task]))
        state.update_snapshot(DesktopSnapshot(tasks=[task]))
        custom.update_snapshot(
            DesktopSnapshot(
                custom_orders=[
                    CustomOrderRow(
                        platform_order_no="111-LONG-STATUS",
                        workflow_stage="blocked",
                        status_text="blocked",
                        last_error=full_message,
                    )
                ]
            )
        )
        shipment.update_snapshot(
            DesktopSnapshot(
                shipments=[
                    ShipmentRow(
                        platform_order_no="112-LONG-STATUS",
                        logistics_no="ALS-LONG-STATUS",
                        scan_issue_code="invalid_row",
                        last_error=full_message,
                    )
                ]
            )
        )

        cases = (
            (dashboard.tasks, 6),
            (state.tasks, 7),
            (custom.table, 7),
            (shipment.table, 11),
        )
        for table, column in cases:
            item = table.item(0, column)
            assert item.text() == expected_brief
            assert len(item.text()) <= 26
            assert item.toolTip() == full_message
            assert "简短结论" in table.horizontalHeaderItem(column).toolTip()
    finally:
        dashboard.deleteLater()
        state.deleteLater()
        custom.deleteLater()
        shipment.deleteLater()


def test_non_table_status_surfaces_keep_full_text_wrapped(app):
    controller = RecordingController()
    pages = (
        DashboardPage(),
        CustomOrdersPage(controller, lambda _result: None),
        AlibabaOrderPage(controller, lambda _result: None),
        ShipmentPage(controller, lambda _result: None),
        StateManagementPage(controller, lambda _result: None),
        ShipmentNotificationPage(controller, lambda _result: None),
    )
    try:
        wrapped_labels = (
            pages[0].backend_message,
            pages[1].scan_schedule_label,
            pages[2].status_label,
            pages[3].scan_schedule_label,
            pages[4].emergency_state,
            pages[5].summary,
        )
        assert all(label.wordWrap() for label in wrapped_labels)
    finally:
        for page in pages:
            page.deleteLater()


def test_unchanged_long_task_message_cells_are_reused_on_progress_updates(app):
    message = "正在重新读取订单并核对服务器状态，请勿重复提交相同任务。"
    first = TaskRecord(
        task_id="task-status-cell-reuse",
        name="核对任务状态",
        area=TaskArea.MAINTENANCE,
        capability=Capability.LIST_ORDERS,
        message=message,
        progress_percent=25,
    )
    second = TaskRecord(
        task_id=first.task_id,
        name=first.name,
        area=first.area,
        capability=first.capability,
        message=message,
        progress_percent=50,
    )
    dashboard = DashboardPage()
    state = StateManagementPage(RecordingController(), lambda _result: None)
    try:
        dashboard.update_snapshot(DesktopSnapshot(today_tasks=[first]))
        state.update_snapshot(DesktopSnapshot(tasks=[first]))
        dashboard_message_item = dashboard.tasks.item(0, 6)
        state_message_item = state.tasks.item(0, 7)

        dashboard.update_snapshot(DesktopSnapshot(today_tasks=[second]))
        state.update_snapshot(DesktopSnapshot(tasks=[second]))

        assert dashboard.tasks.item(0, 6) is dashboard_message_item
        assert state.tasks.item(0, 7) is state_message_item
        assert dashboard_message_item.toolTip() == message
        assert state_message_item.toolTip() == message
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
    prompts: list[str] = []
    monkeypatch.setattr(
        qt_module,
        "confirm_cloudflare_access_login",
        lambda reason, **_kwargs: prompts.append(reason) or True,
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
        assert prompts == ["企业邮箱登录已过期。"]
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


def test_state_page_does_not_rebuild_capability_table_for_task_only_updates(app):
    page = StateManagementPage(RecordingController(), lambda _result: None)
    page.update_snapshot(DesktopSnapshot())
    original_combo = page.capabilities.cellWidget(0, 2)

    page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "task-progress-only",
                    "扫描订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.LIST_ORDERS,
                    progress_percent=25,
                )
            ]
        )
    )

    assert page.capabilities.cellWidget(0, 2) is original_combo
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


def test_running_old_client_installs_update_then_restarts_after_safe_close(
    app,
    tmp_path: Path,
) -> None:
    controller = RecordingController()
    required_versions: list[str] = []
    restart_paths: list[Path] = []
    application_path = tmp_path / "new-client" / "ERP自动化.exe"
    application_path.parent.mkdir()
    application_path.write_bytes(b"new")

    def install_update(required_version: str):
        required_versions.append(required_version)
        return SimpleNamespace(
            status="updated",
            latest_version=required_version,
            application_path=application_path,
        )

    window = DesktopMainWindow(
        controller,
        required_client_update_handler=install_update,
        runtime_restart_callback=restart_paths.append,
    )
    window._timer.stop()
    window._custom_scan_timer.stop()
    window._shipment_scan_timer.stop()
    window.show()
    app.processEvents()

    window._snapshot_failed(
        CoordinationClientUpdateRequired("2026.07.31.3")
    )
    deadline = time.monotonic() + 5
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert required_versions == ["2026.07.31.3"]
    assert restart_paths == [application_path]
    assert not window.isVisible()


def test_required_update_failure_is_not_repeated_by_every_snapshot(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    attempted_versions: list[str] = []
    dialogs: list[str] = []

    def fail_update(required_version: str):
        attempted_versions.append(required_version)
        raise RuntimeError("download unavailable")

    monkeypatch.setattr(
        qt_module,
        "show_packaged_client_error_dialog",
        lambda message, **_kwargs: dialogs.append(message),
        raising=False,
    )
    import erp_automation.ui.modern_dialogs as modern_dialogs

    monkeypatch.setattr(
        modern_dialogs,
        "show_packaged_client_error_dialog",
        lambda message, **_kwargs: dialogs.append(message),
    )
    window = DesktopMainWindow(
        controller,
        required_client_update_handler=fail_update,
    )
    window._timer.stop()
    window._custom_scan_timer.stop()
    window._shipment_scan_timer.stop()
    window.show()
    app.processEvents()
    try:
        required = CoordinationClientUpdateRequired("2026.07.31.3")
        window._snapshot_failed(required)
        deadline = time.monotonic() + 5
        while window._client_update_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

        window._snapshot_failed(required)
        app.processEvents()

        assert attempted_versions == ["2026.07.31.3"]
        assert len(dialogs) == 1
    finally:
        window.close()
        app.processEvents()


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
        assert "本机负责物流查询" in window.shipment_page.scan_schedule_label.text()
        assert "本机可见 Chrome" in window.shipment_page.scan_schedule_label.toolTip()
        assert "没有在线客户端时物流记录保持待查询" in (
            window.shipment_page.scan_schedule_label.toolTip()
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
                    payload={"local_visible_logistics_followup": True},
                )
            ]
        )
        window._apply_snapshot(running)
        assert window._api_wait_notice is not None
        assert len(controller.submitted_commands) == 1

        completed = DesktopSnapshot(
            tasks=[
                TaskRecord(
                    task_id="task-None",
                    name="自动标发三小时自动扫描",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                    payload={"local_visible_logistics_followup": True},
                )
            ]
        )
        window._apply_snapshot(completed)
        assert window._api_wait_notice is None
        for _ in range(100):
            app.processEvents()
            thread = window._local_logistics_followup_thread
            if thread is None:
                break
            QTest.qWait(10)

        assert len(controller.submitted_commands) == 2
        logistics_followup = controller.submitted_commands[-1]
        assert logistics_followup.area is TaskArea.SHIPMENT
        assert logistics_followup.capability is Capability.ALIBABA_LOGISTICS
        assert logistics_followup.payload == {
            "trigger": "after_shipment_scan",
            "source_scan_task_id": "task-None",
        }
        assert window._pending_local_logistics_scan_ids == set()
    finally:
        window.close()


def test_local_logistics_followup_marker_is_retained_until_submission_is_accepted(
    app,
) -> None:
    controller = RecordingController(
        task_results={"": ControlResult(False, "coordinator busy")}
    )
    window = DesktopMainWindow(controller)
    try:
        window._timer.stop()
        window._custom_scan_timer.stop()
        window._shipment_scan_timer.stop()
        window._pending_local_logistics_scan_ids.add("source-task")
        completed = DesktopSnapshot(
            tasks=[
                TaskRecord(
                    task_id="source-task",
                    name="shipment scan",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                    status=TaskStatus.SUCCEEDED,
                )
            ]
        )

        window._apply_snapshot(completed)
        for _ in range(100):
            app.processEvents()
            if window._local_logistics_followup_thread is None:
                break
            QTest.qWait(10)

        assert window._pending_local_logistics_scan_ids == {"source-task"}
        assert window._local_logistics_followup_retry_delay_ms == 2_000

        controller.task_results[""] = ControlResult(
            True,
            "accepted",
            "logistics-followup",
        )
        window._capture_local_logistics_followups(completed)
        for _ in range(100):
            app.processEvents()
            if window._local_logistics_followup_thread is None:
                break
            QTest.qWait(10)

        assert window._pending_local_logistics_scan_ids == set()
        assert window._local_logistics_followup_retry_delay_ms == 0
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


def test_server_hosted_log_buttons_fall_back_to_in_app_viewers(
    app,
    tmp_path,
    monkeypatch,
):
    controller = RecordingController()
    controller.log_directory = lambda: str(tmp_path / "server-only-logs")
    controller.scan_log_text = lambda kind: (
        f"{kind} title",
        f"{kind} content",
    )
    controller.full_log_text = lambda task_id=None: (
        "application title",
        "application content",
    )
    viewed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        qt_module,
        "_show_log_viewer",
        lambda _parent, title, content, **_kwargs: viewed.append((title, content)),
    )
    window = DesktopMainWindow(controller)
    try:
        window.custom_orders_page._open_scan_logs()
        window.shipment_page._open_scan_logs()
        window.logs_page._open_log_directory()

        assert viewed == [
            ("customization title", "customization content"),
            ("shipment title", "shipment content"),
            ("application title", "application content"),
        ]
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


def test_product_type_dropdown_delegate_reserves_a_visible_checkbox(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no="111-CHECKBOX",
                    product_type="tent",
                    workflow_stage="pending",
                    status_text="pending",
                )
            ]
        )
    )
    combo = page.product_type_filter_combo
    model = combo.model()
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 220, 36)
    option.widget = combo.view()

    all_index = model.index(0, 0)
    tent_index = model.index(1, 0)
    assert all_index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
    assert tent_index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked.value
    assert combo.itemDelegate()._checkbox_rect(option).width() > 0

    combo._toggle_index(tent_index)

    assert all_index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked.value
    assert tent_index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
    page.deleteLater()


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
    assert page.status_action_button.text() == "更多批量操作"
    status_menu = page.status_action_button.menu()
    assert status_menu is not None
    assert [action.text() for action in status_menu.actions()] == [
        "修改状态",
        "",
        "停止当前勾选任务",
    ]
    status_change_menu = page._status_change_menu
    assert [action.text() for action in status_change_menu.actions()[:2]] == [
        "全部完成",
        "取消订单",
    ]
    assert status_menu.actions()[-1].text() == "停止当前勾选任务"
    contact_menu = next(
        action.menu()
        for action in status_change_menu.actions()
        if action.text() == "联系方式"
    )
    assert contact_menu is not None
    assert "从此阶段重开" in {
        action.text() for action in contact_menu.actions()
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

    assert page.table.item(0, 3).text() == "未识别"
    assert _interaction_stage_label("folder_creation") == "创建订单文件夹"
    assert _interaction_stage_label("contact_writeback") == "联系方式修改审核"
    assert _interaction_stage_label("retry_review:folder") == "重试前人工复核：订单文件夹"
    assert _interaction_stage_label("erp_mark:stage_review:审核发货") == "自动标发：审核发货"
    assert _interaction_stage_label("erp_mark:stage_review:出库发货") == "自动标发：出库发货"
    assert _interaction_stage_label("future_stage") == "future_stage"
    page.deleteLater()


def test_process_uses_checked_rows_and_ignores_blue_selection(app, monkeypatch):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page.table.setCurrentCell(0, 1)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("常规定制订单执行不应显示审核弹窗"),
    )
    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == ["112-2"]
    command = controller.submitted_commands[0]
    confirmation = DesktopWriteConfirmation.from_payload(command.payload)
    confirmation.require_matches(
        DesktopWriteAction.PROCESS_CUSTOM_ORDER,
        "112-2",
        system_order_no="system-2",
    )
    assert confirmation.source == "qt_checked_action"
    assert page._checked_order_nos == set()
    assert results[-1].accepted
    page.deleteLater()


def test_custom_order_review_setting_controls_submission_confirmation(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=list(_snapshot("111-REVIEW").custom_orders),
            settings=DesktopSettings(custom_order_review_enabled=True),
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    prompts: list[tuple[str, str]] = []

    def reject(_parent, title, message, *_args):
        prompts.append((title, message))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", reject)
    page._process_checked_orders()

    assert controller.submitted_commands == []
    assert prompts[0][0] == "审核定制订单"
    assert "111-REVIEW" in prompts[0][1]
    assert page._checked_order_nos == {"111-REVIEW"}
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


def test_process_batch_preserves_visible_order_without_audit_popup(app, monkeypatch):
    order_nos = [f"order-{index:02d}" for index in range(12)]
    controller = RecordingController()
    results: list[ControlResult] = []
    page = CustomOrdersPage(controller, results.append)
    page.update_snapshot(_snapshot(*order_nos))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("批量定制订单执行不应显示审核弹窗"),
    )
    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == order_nos
    for index, command in enumerate(controller.submitted_commands, start=1):
        confirmation = DesktopWriteConfirmation.from_payload(command.payload)
        confirmation.require_matches(
            DesktopWriteAction.PROCESS_CUSTOM_ORDER,
            command.order_no or "",
            system_order_no=f"system-{index}",
        )
        assert confirmation.source == "qt_checked_action"
    assert page._checked_order_nos == set()
    assert results[-1].accepted
    page.deleteLater()


def test_custom_batch_immediately_moves_selected_rows_to_front(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-A", "112-B", "113-C"))
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(2, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    page._process_checked_orders()

    assert [row.platform_order_no for row in page._rows] == [
        "112-B",
        "113-C",
        "111-A",
    ]
    assert [page.table.item(index, 5).text() for index in range(3)] == [
        "等待处理",
        "等待处理",
        "联系方式待处理",
    ]
    assert "等待后台任务更新" in page.table.item(0, 7).text()

    tasks = [
        TaskRecord(
            f"task-{order_no}",
            "处理定制订单",
            TaskArea.CUSTOMIZATION,
            Capability.UPDATE_CONTACT,
            status=TaskStatus.RUNNING,
            progress_percent=40 + index,
            message=f"正在处理第 {index} 张",
            order_no=order_no,
        )
        for index, order_no in enumerate(("112-B", "113-C"), start=1)
    ]
    page.update_snapshot(
        DesktopSnapshot(custom_orders=_snapshot("111-A", "112-B", "113-C").custom_orders, tasks=tasks)
    )
    assert "41%" in page.table.item(0, 7).text()
    assert "正在处理第 1 张" in page.table.item(0, 7).text()
    page.deleteLater()


def test_custom_batch_updates_active_status_filter_and_counts(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-A", "112-B", "113-C"))
    page.status_filter_combo.setCurrentIndex(
        page.status_filter_combo.findData("pending")
    )
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    page._process_checked_orders()

    assert [row.platform_order_no for row in page._rows] == ["111-A", "113-C"]
    assert page.table.rowCount() == 2
    assert page.custom_selection_summary.text() == "显示 2 · 可处理 2 · 已选 0"
    assert page.quick_select_button.text() == "勾选待处理（2）"
    page.deleteLater()


def test_custom_running_stays_above_waiting_with_distinct_status_color(app):
    page = CustomOrdersPage(RecordingController(), lambda _result: None)
    rows = _snapshot("111-WAIT", "112-RUN", "113-IDLE").custom_orders
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=rows,
            tasks=[
                TaskRecord(
                    "task-wait",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.QUEUED,
                    order_no="111-WAIT",
                ),
                TaskRecord(
                    "task-run",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.RUNNING,
                    order_no="112-RUN",
                ),
            ],
        )
    )

    assert [row.platform_order_no for row in page._rows] == [
        "112-RUN",
        "111-WAIT",
        "113-IDLE",
    ]
    assert page.table.item(0, 5).text() == "正在处理"
    assert page.table.item(1, 5).text() == "等待处理"
    assert (
        page.table.item(0, 5).foreground().color().name()
        != page.table.item(1, 5).foreground().color().name()
    )
    page.deleteLater()


def test_process_batch_does_not_offer_obsolete_confirmation_cancel(app, monkeypatch):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(_snapshot("111-1", "112-2"))
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("常规执行不应再调用确认弹窗"),
    )

    page._process_checked_orders()

    assert [command.order_no for command in controller.submitted_commands] == [
        "111-1",
        "112-2",
    ]
    assert page._checked_order_nos == set()
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


def test_process_batch_timeout_stays_non_modal_until_snapshot_confirms(
    app,
    monkeypatch,
):
    unconfirmed = ControlResult(
        False,
        "批量提交请求已发送，但服务器尚未返回完整结果。",
        details={
            "submission_outcome_unknown": True,
            "non_modal": True,
            "retry_suppressed": True,
        },
    )
    controller = RecordingController(
        task_results={"111-1": unconfirmed, "112-2": unconfirmed}
    )
    results: list[ControlResult] = []
    warnings: list[str] = []
    page = CustomOrdersPage(controller, results.append)
    initial = _snapshot("111-1", "112-2")
    page.update_snapshot(initial)
    page._check_header.check_state_changed.emit(Qt.CheckState.Checked.value)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: warnings.append(str(_args[2])),
    )

    page._process_checked_orders()

    assert warnings == []
    assert results[-1].accepted is False
    assert results[-1].details["non_modal"] is True
    assert results[-1].details["submission_outcome_unknown"] is True
    assert "未排队" not in results[-1].message
    assert "等待服务器确认" in results[-1].message
    assert page._checked_order_nos == set()
    assert page._optimistic_waiting_order_nos == {"111-1", "112-2"}
    assert all(
        "等待服务器确认" in page.table.item(row_index, 7).text()
        for row_index in range(page.table.rowCount())
    )
    assert page._visible_pending_order_nos() == set()

    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page._process_checked_orders()

    assert len(controller.submitted_commands) == 2
    assert page._checked_order_nos == set()
    assert results[-1].details["non_modal"] is True
    assert "请勿重复提交" in results[-1].message

    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=initial.custom_orders,
            tasks=[
                TaskRecord(
                    f"task-{order_no}",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.QUEUED,
                    order_no=order_no,
                )
                for order_no in ("111-1", "112-2")
            ],
        )
    )

    assert page._optimistic_waiting_order_nos == set()
    assert page._active_order_nos == {"111-1", "112-2"}
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
    order_nos = [f"order-{index:04d}" for index in range(500)]
    page.update_snapshot(_snapshot(*order_nos))
    page._show_page(10)
    page.table.item(49, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    started_at = time.monotonic()
    page._process_checked_orders()

    assert time.monotonic() - started_at < 0.2
    assert not page.process_button.isEnabled()
    assert page.process_button.text() == "正在提交 1 张…"
    assert page._page == 1
    assert page._rows[0].platform_order_no == "order-0499"
    assert page.table.item(0, 5).text() == "等待处理"
    assert "正在提交本批订单" in page.table.item(0, 7).text()
    assert controller.submission_started.wait(1)
    controller.release_submission.set()
    deadline = time.monotonic() + 2
    while not results and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(10)

    assert results[-1].accepted
    assert not page.process_button.isEnabled()
    assert page._checked_order_nos == set()
    assert page._rows[0].platform_order_no == "order-0499"
    assert page.table.item(0, 5).text() == "等待处理"
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
        "sku-1",
        "blocked-1",
        "unknown-1",
        "completed-1",
    ]
    assert page.table.item(0, 4).text() == "联系方式待处理"
    assert page.table.item(0, 5).text() == "联系方式待处理"
    assert page.table.item(2, 5).text() == "SKU 调整待处理"
    assert page.table.item(3, 5).text() == "已阻止"
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
                ShipmentRow(
                    "111-AAA",
                    "SYS-001",
                    "tent",
                    "ALS-1",
                    logistics_overdue_at="2026-08-20T09:30:00Z",
                ),
                ShipmentRow("112-BBB", "SYS-002", "x_stands", "ALS-2"),
            ]
        )
    )

    assert page.table.columnCount() == 13
    assert page.table.horizontalHeaderItem(3).text() == "商品类型"
    assert page.table.horizontalHeaderItem(9).text() == "状态时间"
    assert page.table.horizontalHeaderItem(10).text() == "阿里查询时间"
    assert page.table.horizontalHeaderItem(11).text() == "状态说明"
    assert page.table.horizontalHeaderItem(12).text() == "逾期记录"
    header = page.table.horizontalHeader()
    assert header.stretchLastSection() is False
    assert all(
        header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
        for column in range(page.table.columnCount())
    )
    assert page.table.columnWidth(11) == 390
    assert page.table.columnWidth(12) == 76
    assert page.table.item(0, 12).text() == "逾期"
    assert page.table.item(0, 12).textAlignment() == int(
        Qt.AlignmentFlag.AlignCenter
    )
    assert page.table.item(0, 12).foreground().color().name() == "#b54708"
    assert page.table.item(1, 12).text() == "未曾逾期"
    assert page.table.item(1, 12).textAlignment() == int(
        Qt.AlignmentFlag.AlignCenter
    )
    assert page.table.item(1, 12).foreground().color().name() == "#047857"
    assert page.search_field_combo.findData("product_type") == -1
    page.search_edit.setText("111-a")
    assert [row.logistics_no for row in page._rows] == ["ALS-1"]
    page.search_field_combo.setCurrentIndex(
        page.search_field_combo.findData("system_order_no")
    )
    page.search_edit.setText("002")
    assert [row.logistics_no for row in page._rows] == ["ALS-2"]
    assert page.table.item(0, 3).text() == "x_stands"
    assert page.table.item(0, 4).text() == "ALS-2"
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(page, "_reason", lambda _title: "批量取消测试")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )

    page._stop_checked_tasks()

    assert controller.cancel_shipment_calls == [(["ALS-2"], "批量取消测试")]
    assert page._checked_logistics_nos == set()
    page.deleteLater()


def test_shipment_queue_scan_errors_are_independently_manageable_but_not_executable(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="112-ERROR-1",
                    system_order_no="SYS-ERROR-1",
                    scan_issue_key="scan-issue:41",
                    scan_issue_code="customer_shipping_service_unavailable",
                    last_error="领星订单列表未返回客选物流字段。",
                ),
                ShipmentRow(
                    platform_order_no="112-ERROR-2",
                    system_order_no="SYS-ERROR-2",
                    scan_issue_key="scan-issue:42",
                    scan_issue_code="customer_shipping_service_unavailable",
                    last_error="客选物流不是 Standard/Expedited。",
                ),
                ShipmentRow(
                    platform_order_no="112-READY",
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
                ),
            ],
            tasks=[
                TaskRecord(
                    "active-ready-task",
                    "执行自动标发",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                    status=TaskStatus.RUNNING,
                    order_no="112-READY",
                    payload={"logistics_no": "ALS-READY"},
                )
            ],
        )
    )

    assert page.table.rowCount() == 3
    assert [page.table.item(index, 7).text() for index in range(3)] == [
        "标发处理中",
        "扫描错误",
        "扫描错误",
    ]
    assert "未返回客选物流字段" in page.table.item(1, 11).text()
    assert "Standard/Expedited" in page.table.item(2, 11).text()
    assert all(
        bool(
            page.table.item(index, 0).flags()
            & Qt.ItemFlag.ItemIsUserCheckable
        )
        for index in range(1, 3)
    )
    assert page.table.item(1, 0).data(Qt.ItemDataRole.UserRole) == "scan-issue:41"
    assert page.table.item(2, 0).data(Qt.ItemDataRole.UserRole) == "scan-issue:42"
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert page._checked_scan_issue_keys == {"scan-issue:41"}
    assert page.change_status_action.isEnabled()
    assert not page.execute_button.isEnabled()
    assert not page.edit_tracking_action.isEnabled()
    assert not page.retry_logistics_action.isEnabled()

    monkeypatch.setattr(
        _ShipmentStatusDialog,
        "exec",
        lambda _self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        _ShipmentStatusDialog,
        "selected_action",
        lambda _self: "manual_cancel",
    )
    monkeypatch.setattr(
        _ShipmentStatusDialog,
        "selected_label",
        lambda _self: "人工取消订单（永久保留）",
    )
    monkeypatch.setattr(page, "_reason", lambda _title: "人工确认无需自动标发")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.Yes,
    )
    page._change_selected_status()

    assert controller.change_shipment_calls == [
        (["scan-issue:41"], "manual_cancel", "人工确认无需自动标发")
    ]
    assert page._checked_scan_issue_keys == set()
    assert page._ready_shipment_count == 0
    page.deleteLater()


def test_shipment_completed_row_ignores_stale_active_task_overlay(app):
    page = ShipmentPage(RecordingController(), lambda _result: None)
    completed = ShipmentRow(
        platform_order_no="113-7041730-7495465",
        system_order_no="103731121355462196",
        logistics_no="ALS01895319051",
        identity_state="ACTIVE",
        logistics_state="RETRYABLE",
        logistics_last_error="浏览器关闭导致本轮查询失败，下轮继续重试。",
        erp_state="DONE",
        checkpoint="OUTBOUNDED",
        completion_source="MANUAL_DETECTED",
    )
    stale_task = TaskRecord(
        "stale-completed-task",
        "执行自动标发",
        TaskArea.SHIPMENT,
        Capability.OUTBOUND_ORDER,
        status=TaskStatus.RUNNING,
        progress_percent=50,
        message="历史任务仍在列表中",
        order_no=completed.platform_order_no,
        payload={"logistics_no": completed.logistics_no},
    )

    page.update_snapshot(DesktopSnapshot(shipments=[completed], tasks=[stale_task]))

    assert page.table.item(0, 7).text() == "已完成"
    assert "外部完成出库" in page.table.item(0, 11).text()
    assert "浏览器关闭" not in page.table.item(0, 11).text()
    assert "历史任务" not in page.table.item(0, 11).text()
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
    assert page.table.item(0, 9).text() == "2026-07-16 12:00:00"
    assert page.table.item(1, 9).text() == "2026-07-16 11:00:00"
    page.deleteLater()


def test_shipment_page_replaces_scan_time_with_product_type(app):
    page = ShipmentPage(RecordingController(), lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-TIMES",
                    system_order_no="SYS-TIMES",
                    product_type="tent",
                    logistics_no="ALS-TIMES",
                    international_tracking_no="1Z999",
                    carrier="UPS",
                    actual_total="USD 20.00",
                    chargeable_weight_kg="10",
                    identity_state="ACTIVE",
                    logistics_state="READY",
                    erp_state="WAITING",
                    checkpoint="NONE",
                    updated_at="2026-08-10T04:00:00Z",
                    last_scanned_at="2026-08-10T04:00:00Z",
                    logistics_state_changed_at="2026-08-10T02:00:00Z",
                    logistics_last_checked_at="2026-08-10T03:00:00Z",
                    erp_state_changed_at="2026-08-10T02:00:00Z",
                )
            ]
        )
    )

    assert page.table.item(0, 3).text() == "tent"
    assert page.table.item(0, 9).text() == "2026-08-10 10:00:00"
    assert page.table.item(0, 10).text() == "2026-08-10 11:00:00"
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
        cancel_index = dialog.action_combo.findData("cancel")
        assert dialog.action_combo.itemText(cancel_index) == "停止当前勾选任务"
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
    assert page.table.horizontalHeaderItem(7).text() == "处理状态"
    assert page.table.item(0, 7).text() == "可标发"
    assert page.table.item(1, 7).text() == "等待物流就绪"
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


def test_shipment_batch_timeout_prevents_resubmit_until_snapshot_confirms(app):
    row = ShipmentRow(
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
    controller = RecordingController(
        task_results={
            "111-READY": ControlResult(
                False,
                "批量提交请求已发送，但服务器尚未返回完整结果。",
                details={
                    "submission_outcome_unknown": True,
                    "non_modal": True,
                    "retry_suppressed": True,
                },
            )
        }
    )
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)
    page.update_snapshot(DesktopSnapshot(shipments=[row]))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

    page._execute_selected()

    assert results[-1].accepted is False
    assert results[-1].details["submission_outcome_unknown"] is True
    assert "等待服务器确认" in results[-1].message
    assert page._checked_logistics_nos == set()
    assert page._unconfirmed_logistics_nos == {"ALS-READY"}
    assert page._active_logistics_nos == {"ALS-READY"}
    assert "等待服务器确认" in page.table.item(0, 11).text()
    assert page._visible_ready_logistics_nos() == set()

    page.update_snapshot(
        DesktopSnapshot(
            shipments=[row],
            tasks=[
                TaskRecord(
                    "task-111-ready",
                    "执行自动标发：111-READY",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                    status=TaskStatus.QUEUED,
                    order_no="111-READY",
                    payload={"logistics_no": "ALS-READY"},
                )
            ],
        )
    )

    assert page._unconfirmed_logistics_nos == set()
    assert page._active_logistics_nos == {"ALS-READY"}
    assert "等待服务器确认" not in page.table.item(0, 11).text()
    assert TaskStatus.QUEUED.label in page.table.item(0, 11).text()
    page.deleteLater()


def test_non_tent_shipment_executes_directly_when_review_is_disabled(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    row = ShipmentRow(
        platform_order_no="111-NON-TENT",
        system_order_no="SYS-NON-TENT",
        product_type="tablecloths",
        logistics_no="ALS-NON-TENT",
        international_tracking_no="1Z123",
        carrier="UPS",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="READY",
        erp_state="WAITING",
        checkpoint="NONE",
    )
    page.update_snapshot(DesktopSnapshot(shipments=[row]))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    prompts: list[tuple[str, str]] = []

    def approve(_parent, title, message, *_args):
        prompts.append((title, message))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", approve)

    page._execute_selected()

    assert prompts == []
    assert [command.order_no for command in controller.submitted_commands] == [
        "111-NON-TENT"
    ]
    page.deleteLater()


def test_shipment_review_setting_prompts_for_all_product_types_before_submission(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    row = ShipmentRow(
        platform_order_no="111-REVIEW-TENT",
        system_order_no="SYS-REVIEW-TENT",
        product_type="tent",
        logistics_no="ALS-REVIEW-TENT",
        international_tracking_no="1Z456",
        carrier="UPS",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="READY",
        erp_state="WAITING",
        checkpoint="NONE",
    )
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[row],
            settings=DesktopSettings(shipment_review_enabled=True),
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    prompts: list[tuple[str, str]] = []

    def approve(_parent, title, message, *_args):
        prompts.append((title, message))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", approve)
    page._execute_selected()

    assert len(prompts) == 1
    assert prompts[0][0] == "审核自动标发"
    assert "111-REVIEW-TENT" in prompts[0][1]
    assert "tent" in prompts[0][1]
    assert [command.order_no for command in controller.submitted_commands] == [
        "111-REVIEW-TENT"
    ]
    page.deleteLater()


def test_shipment_batch_immediately_marks_all_submitted_rows_processing_and_sorts_first(
    app,
):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)

    def ready(order_no: str, logistics_no: str) -> ShipmentRow:
        return ShipmentRow(
            platform_order_no=order_no,
            system_order_no=f"SYS-{order_no}",
            product_type="tent",
            logistics_no=logistics_no,
            international_tracking_no="1Z999",
            carrier="UPS",
            actual_total="USD 20.00",
            chargeable_weight_kg="10",
            identity_state="ACTIVE",
            logistics_state="READY",
            erp_state="WAITING",
            checkpoint="NONE",
        )

    first = ready("111-A", "ALS-A")
    second = ready("112-B", "ALS-B")
    waiting = ShipmentRow(
        platform_order_no="113-C",
        logistics_no="ALS-C",
        identity_state="ACTIVE",
        logistics_state="WAITING",
        erp_state="WAITING",
    )
    page.update_snapshot(DesktopSnapshot(shipments=[waiting, first, second]))
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)

    page._execute_selected()

    assert [row.logistics_no for row in page._rows] == ["ALS-A", "ALS-B", "ALS-C"]
    assert [page.table.item(index, 7).text() for index in range(3)] == [
        "等待标发",
        "等待标发",
        "等待物流就绪",
    ]
    assert "等待后台任务更新" in page.table.item(0, 11).text()

    task = TaskRecord(
        "task-111-A",
        "执行自动标发",
        TaskArea.SHIPMENT,
        Capability.OUTBOUND_ORDER,
        status=TaskStatus.RUNNING,
        progress_percent=65,
        message="正在填写物流信息",
        order_no="111-A",
        payload={"logistics_no": "ALS-A"},
    )
    page.update_snapshot(
        DesktopSnapshot(shipments=[waiting, first, second], tasks=[task])
    )
    assert page.table.item(0, 7).text() == "标发处理中"
    assert "65%" in page.table.item(0, 11).text()
    assert "正在填写物流信息" in page.table.item(0, 11).text()
    page.deleteLater()


def test_confirmed_shipment_uses_new_pair_without_sending_notification(app, monkeypatch):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    row = ShipmentRow(
        platform_order_no="111-CONFIRM",
        system_order_no="SYS-CONFIRM",
        product_type="tablecloths",
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
    review_prompts: list[tuple[str, str]] = []

    def approve_review(_parent, title, message, *_args):
        review_prompts.append((title, message))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", approve_review)
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
            "桌面用户人工核对承运商和运单号并放行，随后执行标发并同步客户通知草稿",
        )
    ]
    confirmed = captured["rows"][0]
    assert confirmed.carrier == "USPS"
    assert confirmed.international_tracking_no == "9400111899223856928499"
    assert confirmed.logistics_state == "READY"
    assert confirmed.erp_state == "PENDING"
    assert "auto_send_customer_notification" not in captured["kwargs"]
    assert review_prompts == []
    page.deleteLater()


def test_shipment_tracking_pair_has_no_prefix_gate_and_does_not_submit_erp(
    app,
    monkeypatch,
):
    controller = RecordingController()
    results: list[ControlResult] = []
    page = ShipmentPage(controller, results.append)
    row = ShipmentRow(
        platform_order_no="111-EDIT",
        system_order_no="SYS-EDIT",
        product_type="tent",
        logistics_no="ALS-EDIT",
        international_tracking_no="JYCP00000093286",
        carrier="FedEx",
        actual_total="USD 20.00",
        chargeable_weight_kg="10",
        identity_state="ACTIVE",
        logistics_state="WAITING",
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
        lambda _dialog: ("UPS", "1Z9253126709651051"),
    )

    page._edit_selected_tracking_pair()

    assert controller.confirm_shipment_calls == [
        (
            "ALS-EDIT",
            "UPS",
            "1Z9253126709651051",
            "桌面用户向物流客服核实后手动修改物流单号和承运商",
        )
    ]
    assert controller.submitted_commands == []
    assert results[-1].accepted
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
    assert not page.execute_button.isEnabled()
    assert page.execute_button.text() == "执行标发（0）"
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
    assert page.quick_select_button.text() == "勾选可标发（1）"
    assert page.search_edit.minimumWidth() == 180
    assert page.search_edit.maximumWidth() == 520
    assert page.ready_count_label.text() == "显示 3 · 可标发 1 · 已选 1"
    assert page.execute_button.text() == "执行标发（1）"
    assert page.execute_button.isEnabled()
    assert page.more_actions_button.isEnabled()
    assert results[-1].accepted is True
    page.deleteLater()


def test_shipment_retry_menu_routes_selected_stage(app, monkeypatch):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-RETRY",
                    logistics_no="ALS-RETRY",
                    identity_state="ACTIVE",
                    logistics_state="FAILED",
                    erp_state="FAILED",
                )
            ]
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(page, "_reason", lambda _title: "人工复核后重试")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    assert page.retry_erp_action.isEnabled()
    page.retry_erp_action.trigger()

    assert controller.retry_shipment_calls == [
        (["ALS-RETRY"], "erp", "人工复核后重试")
    ]
    assert page._checked_logistics_nos == set()
    assert not page.more_actions_button.isEnabled()
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
    assert page.quick_select_button.text() == "勾选待处理（1）"
    assert page.search_edit.minimumWidth() == 180
    assert page.search_edit.maximumWidth() == 520
    assert results[-1].accepted is True
    page.deleteLater()


def test_product_type_values_split_multi_type_storage() -> None:
    assert qt_module._product_type_label("tablecloths | tent") == "tent"
    assert qt_module._product_type_label("tablecloths | feather_flags") == (
        "tablecloths"
    )
    assert qt_module._product_type_label("") == "未识别"
    assert qt_module._shipment_product_type_label(
        ShipmentRow(
            platform_order_no="112-NO-ASIN",
            product_identity_status_text="同平台兄弟单已完整核验，无 ASIN",
        )
    ) == "无ASIN"
    assert qt_module._shipment_product_type_label(
        ShipmentRow(
            platform_order_no="112-RETRY",
            product_identity_status_text="兄弟单或详情读取失败，等待重试",
        )
    ) == "无ASIN"
    assert qt_module._shipment_product_type_label(
        ShipmentRow(
            platform_order_no="112-TENT",
            product_type="tent",
            product_identity_status_text="未复核",
        )
    ) == "tent"
    assert qt_module._product_type_values(
        SimpleNamespace(product_type="tent | tablecloths")
    ) == ("tent", "tablecloths")
    assert qt_module._product_type_values(
        {"product_types": ["tent | tablecloths", "tent"]}
    ) == ("tent", "tablecloths")


def test_product_type_multiselect_filters_and_quick_selects_all_three_queues(app):
    custom = CustomOrdersPage(RecordingController(), lambda _result: None)
    custom.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow("111-TENT", "SYS-T", "tent", "pending", "pending"),
                CustomOrderRow(
                    "112-STAND",
                    "SYS-X",
                    "x_stands",
                    "pending",
                    "pending",
                ),
            ]
        )
    )
    custom.product_type_filter_combo.set_selected_values(["tent", "x_stands"])
    assert custom.product_type_filter_combo.lineEdit().text() == "tent、x_stands"
    custom.product_type_filter_combo.set_selected_values(["x_stands"])
    assert [row.platform_order_no for row in custom._rows] == ["112-STAND"]
    custom._select_visible_pending_orders()
    assert custom._checked_order_nos == {"112-STAND"}
    custom.product_type_filter_combo.set_selected_values([])
    custom._select_visible_pending_orders()
    assert custom._checked_order_nos == {"111-TENT", "112-STAND"}

    def ready_shipment(order_no: str, product_type: str, logistics_no: str):
        return ShipmentRow(
            platform_order_no=order_no,
            system_order_no=f"SYS-{order_no}",
            product_type=product_type,
            logistics_no=logistics_no,
            international_tracking_no=f"TRACK-{logistics_no}",
            carrier="UPS",
            actual_total="USD 20.00",
            chargeable_weight_kg="1",
            identity_state="ACTIVE",
            logistics_state="READY",
            erp_state="WAITING",
            checkpoint="NONE",
        )

    shipment = ShipmentPage(RecordingController(), lambda _result: None)
    shipment.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ready_shipment("211-TENT", "tent", "ALS-T"),
                ready_shipment("212-STAND", "x_stands", "ALS-X"),
            ]
        )
    )
    shipment.product_type_filter_combo.set_selected_values(["tent"])
    assert [row.logistics_no for row in shipment._rows] == ["ALS-T"]
    shipment._select_visible_ready_shipments()
    assert shipment._checked_logistics_nos == {"ALS-T"}
    shipment.product_type_filter_combo.set_selected_values([])
    shipment._select_visible_ready_shipments()
    assert shipment._checked_logistics_nos == {"ALS-T", "ALS-X"}

    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 1,
            "platform_order_no": "311-TENT",
            "product_types": ["tent"],
            "product_type": "tent",
            "state": "AWAITING_REVIEW",
            "items": [],
        },
        {
            "id": 2,
            "platform_order_no": "312-STAND",
            "product_types": ["x_stands"],
            "product_type": "x_stands",
            "state": "AWAITING_REVIEW",
            "items": [],
        },
    ]
    notification = ShipmentNotificationPage(controller, lambda _result: None)
    notification._reload()
    notification.product_type_filter_combo.set_selected_values(["x_stands"])
    assert [
        item["platform_order_no"]
        for item in notification._visible_notifications
    ] == ["312-STAND"]
    notification._select_visible_awaiting_review()
    assert notification._checked_notification_ids == {2}
    notification.product_type_filter_combo.set_selected_values([])
    notification._select_visible_awaiting_review()
    assert notification._checked_notification_ids == {1, 2}
    assert notification.table.horizontalHeaderItem(2).text() == "商品类型"

    custom.deleteLater()
    shipment.deleteLater()
    notification.deleteLater()


def test_custom_and_shipment_queues_share_default_fifty_row_pagination(app):
    custom = CustomOrdersPage(RecordingController(), lambda _result: None)
    custom.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no=f"CUSTOM-{index:03d}",
                    workflow_stage="pending",
                    status_text="pending",
                )
                for index in range(105)
            ]
        )
    )

    assert custom.table.rowCount() == 50
    assert custom._page == 1
    assert custom._page_count == 3
    assert custom.custom_page_size_combo.currentData() == 50
    assert custom.pagination_bar.total_label.text() == "共 105 条"
    assert custom.custom_previous_page_button.accessibleName() == "上一页"
    assert custom.custom_next_page_button.accessibleName() == "下一页"
    custom.custom_next_page_button.click()
    assert custom._page == 2
    assert custom.table.rowCount() == 50
    custom.custom_page_size_combo.setCurrentIndex(
        custom.custom_page_size_combo.findData(20)
    )
    assert custom._page == 1
    assert custom._page_count == 6
    assert custom.table.rowCount() == 20
    custom.custom_jump_page_spin.setValue(6)
    custom.pagination_bar._request_jump()
    assert custom._page == 6
    assert custom.table.rowCount() == 5

    def ready_shipment(index: int) -> ShipmentRow:
        return ShipmentRow(
            platform_order_no=f"SHIP-{index:03d}",
            system_order_no=f"SYS-{index:03d}",
            product_type="tent",
            logistics_no=f"ALS-{index:03d}",
            international_tracking_no=f"TRACK-{index:03d}",
            carrier="UPS",
            actual_total="USD 20.00",
            chargeable_weight_kg="1",
            identity_state="ACTIVE",
            logistics_state="READY",
            erp_state="WAITING",
            checkpoint="NONE",
        )

    shipment = ShipmentPage(RecordingController(), lambda _result: None)
    shipment.update_snapshot(
        DesktopSnapshot(shipments=[ready_shipment(index) for index in range(51)])
    )

    assert shipment.table.rowCount() == 50
    assert shipment._page_count == 2
    assert shipment.shipment_page_size_combo.currentData() == 50
    assert shipment.pagination_bar.total_label.text() == "共 51 条"
    shipment.shipment_jump_page_spin.setValue(2)
    shipment.pagination_bar._request_jump()
    assert shipment._page == 2
    assert shipment.table.rowCount() == 1

    custom.deleteLater()
    shipment.deleteLater()


def test_notification_queue_pagination_changes_page_size_and_jumps(app):
    class PagedNotificationController(RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[int, int]] = []

        def list_shipment_notifications(self, **kwargs):
            page = int(kwargs.get("page") or 1)
            page_size = int(kwargs.get("page_size") or 50)
            self.calls.append((page, page_size))
            return {
                "items": [],
                "page": page,
                "page_size": page_size,
                "total": 135,
                "total_pages": (135 + page_size - 1) // page_size,
                "product_types": ["tent", "x_stands"],
            }

    controller = PagedNotificationController()
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert controller.calls[-1] == (1, 50)
    assert page.notification_page_size_combo.currentData() == 50
    assert page.notification_page_status.text() == "共 135 条"
    assert page.notification_previous_page_button.accessibleName() == "上一页"
    assert page.notification_next_page_button.accessibleName() == "下一页"
    page.notification_next_page_button.click()
    assert controller.calls[-1] == (2, 50)
    page.notification_page_size_combo.setCurrentIndex(
        page.notification_page_size_combo.findData(20)
    )
    assert controller.calls[-1] == (1, 20)
    page.notification_jump_page_spin.setValue(3)
    page.pagination_bar._request_jump()
    assert controller.calls[-1] == (3, 20)
    page.deleteLater()


def test_shipment_submission_from_later_page_moves_waiting_order_to_first_page(app):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    shipments = [
        ShipmentRow(
            platform_order_no=f"SHIP-{index:03d}",
            system_order_no=f"SYS-{index:03d}",
            product_type="tent",
            logistics_no=f"ALS-{index:03d}",
            international_tracking_no=f"TRACK-{index:03d}",
            carrier="UPS",
            actual_total="USD 20.00",
            chargeable_weight_kg="1",
            identity_state="ACTIVE",
            logistics_state="READY",
            erp_state="WAITING",
            checkpoint="NONE",
        )
        for index in range(101)
    ]
    page.update_snapshot(DesktopSnapshot(shipments=shipments))
    page._show_page(3)
    assert page._rows[0].logistics_no == "ALS-100"
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)

    page._execute_selected()

    assert page._page == 1
    assert page._rows[0].logistics_no == "ALS-100"
    assert page.table.item(0, 7).text() == "等待标发"
    page.deleteLater()


def test_notification_cached_next_page_renders_before_background_refresh(
    app,
    monkeypatch,
):
    class CachedPageController(RecordingController):
        def list_shipment_notifications(self, **kwargs):
            page = int(kwargs.get("page") or 1)
            return {
                "items": [
                    {
                        "id": page,
                        "platform_order_no": f"PAGE-{page}",
                        "state": "AWAITING_REVIEW",
                        "package_total": 1,
                        "package_complete": 1,
                        "package_missing": 0,
                        "preview_items": [],
                    }
                ],
                "page": page,
                "page_size": 50,
                "total": 100,
                "total_pages": 2,
                "product_types": [],
            }

    page = ShipmentNotificationPage(CachedPageController(), lambda _result: None)
    page._reload()
    page_two_query = page._notification_page_query(2)
    page_two_key = page._notification_page_cache_key(page_two_query)
    page._cache_notification_page(
        page_two_key,
        {
            "items": [
                {
                    "id": 2,
                    "platform_order_no": "PAGE-2",
                    "state": "AWAITING_REVIEW",
                    "package_total": 1,
                    "package_complete": 1,
                    "package_missing": 0,
                    "preview_items": [],
                }
            ],
            "page": 2,
            "page_size": 50,
            "total": 100,
            "total_pages": 2,
            "product_types": [],
        },
    )
    refreshes = 0

    def counted_reload():
        nonlocal refreshes
        refreshes += 1

    monkeypatch.setattr(page, "_reload", counted_reload)

    page._show_notification_page(2)

    assert page._notification_page == 2
    assert page.table.item(0, 1).text() == "PAGE-2"
    assert refreshes == 1
    page.deleteLater()


def test_stale_notification_page_response_does_not_replace_requested_page(app):
    class PageController(RecordingController):
        def list_shipment_notifications(self, **kwargs):
            return {
                "items": [],
                "page": int(kwargs.get("page") or 1),
                "page_size": 50,
                "total": 100,
                "total_pages": 2,
                "product_types": [],
            }

    page = ShipmentNotificationPage(PageController(), lambda _result: None)
    page._notification_page = 2
    stale_query = page._notification_page_query(1)
    stale_key = page._notification_page_cache_key(stale_query)

    page._apply_notification_reload_for_request(
        stale_key,
        {
            "items": [{"id": 91, "state": "AWAITING_REVIEW"}],
            "page": 1,
            "page_size": 50,
            "total": 100,
            "total_pages": 2,
            "product_types": [],
        },
    )

    assert page._notification_page == 2
    assert page._notifications == []
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


def test_notification_snapshot_does_not_reload_unchanged_data_every_second(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 9,
            "platform_order_no": "109-NO-RELOAD",
            "state": "AWAITING_REVIEW",
            "items": [],
        }
    ]
    calls = 0
    original = controller.list_shipment_notifications

    def counted_list():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(controller, "list_shipment_notifications", counted_list)
    page = ShipmentNotificationPage(controller, lambda _result: None)

    page.update_snapshot(DesktopSnapshot())
    page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "unrelated-progress",
                    "其他后台任务",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.RUNNING,
                    progress_percent=50,
                )
            ]
        )
    )

    assert calls == 1
    page.deleteLater()


def test_unchanged_notification_reload_does_not_rebuild_table(app, monkeypatch) -> None:
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 10,
            "platform_order_no": "110-NO-REBUILD",
            "state": "AWAITING_REVIEW",
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    renders = 0
    original = page._render_notifications

    def counted_render(*, selected_id, selected_column):
        nonlocal renders
        renders += 1
        return original(
            selected_id=selected_id,
            selected_column=selected_column,
        )

    monkeypatch.setattr(page, "_render_notifications", counted_render)

    page._reload()

    assert renders == 0
    page.deleteLater()


def test_remote_task_submission_does_not_block_the_qt_event_thread(app) -> None:
    started = threading.Event()
    release = threading.Event()

    class RemoteLikeController(RecordingController):
        snapshot_runs_in_background = True

        def submit_task(self, command: TaskCommand) -> ControlResult:
            started.set()
            release.wait(timeout=2)
            return super().submit_task(command)

    controller = RemoteLikeController()
    page = CustomOrdersPage(controller, lambda _result: None)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    before = time.perf_counter()

    page._scan()

    elapsed = time.perf_counter() - before
    assert elapsed < 0.2
    assert started.wait(timeout=1)
    release.set()
    deadline = time.time() + 2
    while getattr(page, "_responsive_control_threads", ()) and time.time() < deadline:
        QTest.qWait(20)
    timer.cancel()
    assert not getattr(page, "_responsive_control_threads", ())
    page.deleteLater()


def test_control_result_thread_publishes_only_after_worker_has_finished(app) -> None:
    owner = QLabel()
    threads: list[_ControlResultThread] = []
    callback_finished_states: list[bool] = []

    for _index in range(64):
        thread = _ControlResultThread(lambda: ControlResult(True, "完成"), owner)
        threads.append(thread)
        thread.result_ready.connect(
            lambda _result, current=thread: callback_finished_states.append(
                current.isFinished()
            )
        )
        thread.start()

    deadline = time.monotonic() + 5
    while len(callback_finished_states) < len(threads) and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)

    assert len(callback_finished_states) == len(threads)
    assert all(callback_finished_states)
    for thread in threads:
        assert thread.wait(1000)
    owner.deleteLater()


def test_main_window_waits_for_running_shipment_batch_before_close(app) -> None:
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
    window = DesktopMainWindow(controller)
    window._timer.stop()
    window._custom_scan_timer.stop()
    window._shipment_scan_timer.stop()
    startup_deadline = time.monotonic() + 2
    while window._snapshot_thread is not None and time.monotonic() < startup_deadline:
        app.processEvents()
        QTest.qWait(5)
    assert window._snapshot_thread is None

    window.show()
    window.shipment_page.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="111-SAFE-CLOSE",
                    system_order_no="SYS-SAFE-CLOSE",
                    product_type="tent",
                    logistics_no="ALS-SAFE-CLOSE",
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
    window.shipment_page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    window.shipment_page._execute_selected()
    assert controller.submission_started.wait(1)

    window.close()
    app.processEvents()
    close_was_deferred = window._close_pending
    window_remained_visible = window.isVisible()
    controller.release_submission.set()

    close_deadline = time.monotonic() + 3
    while window.isVisible() and time.monotonic() < close_deadline:
        app.processEvents()
        QTest.qWait(10)

    assert close_was_deferred is True
    assert window_remained_visible is True
    assert window.shipment_page._submission_thread is None
    assert not window.isVisible()
    window.deleteLater()


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

    assert page.edit_contact_action.text() == "修改联系方式"
    assert "自动扫描覆盖" in page.edit_contact_action.toolTip()

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
    assert page.package_table.columnCount() == 7
    assert page.package_table.item(0, 3).text() == "人工填写 · 运单号"
    assert page.package_table.item(0, 5).text() == "TRACK-1"
    assert "运单号：TRACK-1" in page.package_table.item(0, 5).toolTip()
    assert page.package_table.item(1, 1).text() == "待补"
    assert page.package_table.item(1, 2).text() == "20001"
    assert page.package_table.item(1, 6).text() == "待补物流"
    page.deleteLater()


def test_notification_package_preview_renders_without_detail_round_trip(app):
    class PreviewController(RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.detail_calls: list[tuple[int, ...]] = []

        def get_shipment_notification_details(self, notification_ids):
            self.detail_calls.append(tuple(notification_ids))
            return []

    controller = PreviewController()
    controller.notification_rows = [
        {
            "id": 130,
            "platform_order_no": "112-PREVIEW",
            "recipient_name": "Preview Customer",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "detail_loaded": False,
            "preview_items": [
                {
                    "stable_sequence": 1,
                    "display_label": "A",
                    "system_order_no": "10001",
                    "shipment_type": "SYSTEM_LABEL",
                    "carrier_normalized": "4PX",
                    "waybill_no": "JY001",
                    "tracking_no": "4PX001",
                    "final_tracking_no": "4PX001",
                    "customer_visible": 1,
                    "visibility_reason": "",
                    "is_complete": 1,
                }
            ],
        }
    ]

    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.package_table.rowCount() == 1
    assert page.package_table.item(0, 5).text() == "4PX001"
    assert page.summary.text() != "正在加载通知包裹与正文详情…"
    assert controller.detail_calls == []
    page.deleteLater()


def test_notification_review_marks_unresolved_tracking_source_for_review(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 14,
            "platform_order_no": "112-TRACKING-CONFLICT",
            "recipient_name": "Customer",
            "state": "BLOCKED",
            "package_total": 1,
            "package_complete": 0,
            "package_missing": 1,
            "items": [
                {
                    "stable_sequence": 1,
                    "display_label": "a",
                    "system_order_no": "10001",
                    "shipment_type": "UNKNOWN",
                    "carrier_normalized": "4PX",
                    "waybill_no": "internal-reference",
                    "tracking_no": "4PX306015515",
                    "final_tracking_no": "",
                    "customer_visible": 1,
                    "visibility_reason": "tracking_source_unresolved",
                    "is_complete": 0,
                }
            ],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.package_table.item(0, 3).text() == "来源待复核"
    assert page.package_table.item(0, 5).text() == "待复核"
    assert page.package_table.item(0, 6).text() == "需复核"
    assert "跟踪号：4PX306015515" in page.package_table.item(0, 5).toolTip()
    page.deleteLater()


def test_notification_more_actions_can_edit_any_selected_package_logistics(
    app,
    monkeypatch,
):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 15,
            "platform_order_no": "112-PACKAGE-EDIT",
            "recipient_name": "Customer",
            "state": "BLOCKED",
            "package_total": 2,
            "package_complete": 1,
            "package_missing": 1,
            "items": [],
            "editable_packages": [
                {
                    "package_key": "10001:WO-1",
                    "stable_sequence": 1,
                    "stable_label": "a",
                    "display_label": "a",
                    "system_order_no": "10001",
                    "carrier": "FedEx",
                    "final_tracking_no": "TRACK-OLD-1",
                    "customer_visible": 1,
                },
                {
                    "package_key": "10002:WO-2",
                    "stable_sequence": 2,
                    "stable_label": "b",
                    "display_label": "b",
                    "system_order_no": "10002",
                    "carrier": "FedEx",
                    "final_tracking_no": "TRACK-OLD-2",
                    "customer_visible": 1,
                },
            ],
        }
    ]
    results: list[ControlResult] = []
    page = ShipmentNotificationPage(controller, results.append)
    page._reload()
    monkeypatch.setattr(
        _NotificationPackageLogisticsDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        _NotificationPackageLogisticsDialog,
        "values",
        lambda _dialog: (
            "10002:WO-2",
            "USPS",
            "9334610990150195994324",
            "已在 USPS 官网核对轨迹",
        ),
    )

    page._edit_package_logistics()

    assert controller.notification_package_edit_calls == [
        (
            15,
            "10002:WO-2",
            "USPS",
            "9334610990150195994324",
            "已在 USPS 官网核对轨迹",
        )
    ]
    assert results[-1].accepted is True
    assert "修改包裹承运商和物流单号" in {
        action.text()
        for action in page.notification_more_actions_menu.actions()
    }
    page.deleteLater()


def test_notification_package_logistics_dialog_switches_between_order_packages(
    app,
):
    dialog = _NotificationPackageLogisticsDialog(
        "112-PACKAGE-DIALOG",
        [
            {
                "package_key": "10001:WO-1",
                "stable_label": "a",
                "system_order_no": "10001",
                "carrier": "FedEx",
                "final_tracking_no": "TRACK-ONE",
            },
            {
                "package_key": "10002:WO-2",
                "stable_label": "b",
                "system_order_no": "10002",
                "carrier": "USPS",
                "final_tracking_no": "TRACK-TWO",
            },
        ],
    )

    assert dialog.package_combo.count() == 2
    assert dialog.values()[:3] == (
        "10001:WO-1",
        "FedEx",
        "TRACK-ONE",
    )
    dialog.package_combo.setCurrentIndex(1)
    dialog.reason_edit.setText("人工核对")
    assert dialog.values() == (
        "10002:WO-2",
        "USPS",
        "TRACK-TWO",
        "人工核对",
    )
    dialog.deleteLater()


def test_notification_table_selects_one_cell_and_copies_current_value(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 21,
            "platform_order_no": "701-COPY-ORDER",
            "state": "FAILED",
            "last_error": (
                "状态核验超时：发送服务已接收通知，但没有返回终态。"
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

    assert page.table.columnCount() == 10
    assert (
        page.table.selectionBehavior()
        == QAbstractItemView.SelectionBehavior.SelectItems
    )
    page.table.setCurrentCell(0, 1)
    page.table.setFocus()
    QTest.keyClick(page.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)

    assert QApplication.clipboard().text() == "701-COPY-ORDER"
    assert page.table.item(0, 8).text() == "状态核验失败"
    assert "状态核验超时" in page.table.item(0, 9).text()
    page.deleteLater()


def test_notification_status_is_concise_in_table_and_full_in_selected_detail(app):
    controller = RecordingController()
    full_explanation = (
        "发送未开始：订单 112-2585733-5194611 的出库状态、物流信息或联系方式"
        "在审核后发生变化，系统已生成新的待审核版本；"
        "未调用邮件或短信服务，请审核新版本后再发送。"
    )
    controller.notification_rows = [
        {
            "id": 22,
            "platform_order_no": "112-2585733-5194611",
            "recipient_name": "Karen L. Stetins",
            "state": "AWAITING_REVIEW",
            "last_error": full_explanation,
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    status_item = page.table.item(0, 9)
    assert status_item.text() == "信息已变化，已生成新版本；请重新审核（未发送）"
    assert "112-2585733-5194611" not in status_item.text()
    assert status_item.toolTip() == full_explanation
    assert "简短结论" in page.table.horizontalHeaderItem(9).toolTip()
    assert f"状态说明：{full_explanation}" in page.summary.text()
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
    assert "更多批量操作" in labels
    assert "修改状态" in {
        action.text() for action in page.notification_more_actions_menu.actions()
    }
    assert "勾选设为人工完成" not in labels
    assert "勾选设为已取消" not in labels
    assert not hasattr(page, "content")
    page.deleteLater()


def test_notification_quick_select_excludes_manual_email_but_header_selects_all(
    app,
) -> None:
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 33,
            "platform_order_no": "701-REVIEW",
            "state": "AWAITING_REVIEW",
            "items": [],
        },
        {
            "id": 34,
            "platform_order_no": "702-MANUAL-EMAIL",
            "state": "MANUAL_EMAIL_REQUIRED",
            "items": [],
        },
        {
            "id": 35,
            "platform_order_no": "703-ACCEPTED",
            "state": "ACCEPTED",
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()

    assert page.quick_select_review_button.text() == "勾选待审核（1）"
    page._select_visible_awaiting_review()
    assert page._checked_notification_ids == {33}

    page._set_all_checked(Qt.CheckState.Checked.value)
    assert page._checked_notification_ids == {33, 34, 35}

    page.deleteLater()


def test_quick_select_updates_checkbox_cells_without_rebuilding_tables(
    app,
    monkeypatch,
) -> None:
    custom = CustomOrdersPage(RecordingController(), lambda _result: None)
    custom.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(
                    platform_order_no="111-CUSTOM",
                    workflow_stage="pending",
                    status_text="pending",
                )
            ]
        )
    )
    monkeypatch.setattr(
        custom,
        "_render_rows",
        lambda **_kwargs: pytest.fail("批量勾选不应重建定制订单表格"),
    )
    custom._select_visible_pending_orders()
    assert custom.table.item(0, 0).checkState() == Qt.CheckState.Checked

    shipment = ShipmentPage(RecordingController(), lambda _result: None)
    shipment.update_snapshot(
        DesktopSnapshot(
            shipments=[
                ShipmentRow(
                    platform_order_no="112-SHIPMENT",
                    system_order_no="SYS-QUICK-SELECT",
                    logistics_no="ALS-QUICK-SELECT",
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
    monkeypatch.setattr(
        shipment,
        "_render_rows",
        lambda **_kwargs: pytest.fail("批量勾选不应重建自动标发表格"),
    )
    shipment._select_visible_ready_shipments()
    assert shipment.table.item(0, 0).checkState() == Qt.CheckState.Checked

    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 36,
            "platform_order_no": "113-NOTIFICATION",
            "state": "AWAITING_REVIEW",
            "items": [],
        }
    ]
    notification = ShipmentNotificationPage(controller, lambda _result: None)
    notification._reload()
    monkeypatch.setattr(
        notification,
        "_render_notifications",
        lambda **_kwargs: pytest.fail("批量勾选不应重建客户通知表格"),
    )
    notification._select_visible_awaiting_review()
    assert notification.table.item(0, 0).checkState() == Qt.CheckState.Checked

    custom.deleteLater()
    shipment.deleteLater()
    notification.deleteLater()


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
        _NotificationStatusDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        _NotificationStatusDialog,
        "selected_value",
        lambda _dialog: "CANCELLED",
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


def test_notification_status_can_reopen_checked_rows_for_review(app, monkeypatch):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 51,
            "platform_order_no": "703-REOPEN-1",
            "state": "CANCELLED",
            "items": [],
        },
        {
            "id": 52,
            "platform_order_no": "703-REOPEN-2",
            "state": "RETRYABLE",
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    page._checked_notification_ids = {51, 52}
    dispatched: list[list[int]] = []
    monkeypatch.setattr(
        _NotificationStatusDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        _NotificationStatusDialog,
        "selected_value",
        lambda _dialog: "AWAITING_REVIEW",
    )
    monkeypatch.setattr(
        page,
        "_reopen_notifications_for_review",
        lambda notifications: dispatched.append(
            [int(item["id"]) for item in notifications]
        ),
    )

    page._change_status()

    assert [sorted(values) for values in dispatched] == [[51, 52]]
    page.deleteLater()


def test_notification_resubmit_supports_checked_batch(app, monkeypatch):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 61,
            "platform_order_no": "704-RESUBMIT-1",
            "state": "AWAITING_REVIEW",
            "items": [],
        },
        {
            "id": 62,
            "platform_order_no": "704-RESUBMIT-2",
            "state": "CANCELLED",
            "items": [],
        },
    ]
    results: list[ControlResult] = []
    page = ShipmentNotificationPage(controller, results.append)
    page._reload()
    page._checked_notification_ids = {61, 62}
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: ("修正内容后重新审核", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page._resubmit()

    assert controller.notification_resubmit_calls == [
        (61, "修正内容后重新审核"),
        (62, "修正内容后重新审核"),
    ]
    assert results[-1].accepted is True
    page.deleteLater()


def test_notification_retry_supports_checked_approved_content_batch(app):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 71,
            "platform_order_no": "705-RETRY-1",
            "state": "RETRYABLE",
            "items": [],
        },
        {
            "id": 72,
            "platform_order_no": "705-RETRY-2",
            "state": "RETRYABLE",
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    page._checked_notification_ids = {71, 72}

    page._retry()

    command = controller.submitted_commands[-1]
    assert command.payload["notification_ids"] == [71, 72]
    assert command.payload["retry"] is True
    assert command.payload["trigger"] == SHIPMENT_NOTIFICATION_SEND_TRIGGER
    assert page._checked_notification_ids == set()
    page.deleteLater()


def test_notification_manual_completion_accepts_provider_accepted_state(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    notification = {
        "id": 42,
        "platform_order_no": "704-ACCEPTED",
        "state": "ACCEPTED",
        "provider_message_id": "provider-42",
        "items": [],
    }
    controller.notification_rows = [notification]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    observed: list[tuple[list[int], str]] = []
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: ("人工标发并已核验", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        controller,
        "mark_shipment_notifications_manually_completed",
        lambda ids, *, reason: (
            observed.append((list(ids), reason))
            or ControlResult(True, "完成")
        ),
    )

    page._mark_manually_completed([notification])

    assert observed == [([42], "人工标发并已核验")]
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
    expected_order_no = notification_confirmation_order_no([61])
    assert command.order_no == expected_order_no
    confirmation = DesktopWriteConfirmation.from_payload(command.payload)
    assert confirmation.action is DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION
    assert confirmation.order_no == expected_order_no
    assert confirmation.source == "qt_message_box"
    assert page._notification_send_task_id == f"task-{expected_order_no}"
    assert page._checked_notification_ids == set()
    assert page._optimistic_send_notification_ids == {61}
    row = next(
        row
        for row in range(page.table.rowCount())
        if int(page.table.item(row, 0).data(Qt.ItemDataRole.UserRole)) == 61
    )
    assert page.table.item(row, 8).text() == "等待发送"

    page.deleteLater()


def test_notification_approval_from_later_page_returns_to_global_queue_front(
    app,
    monkeypatch,
):
    class PagedApprovalController(RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.page_calls: list[dict[str, object]] = []

        def list_shipment_notifications(self, **kwargs):
            self.page_calls.append(dict(kwargs))
            page = int(kwargs.get("page") or 1)
            notification_id = 61 if page == 1 else 62
            return {
                "items": [
                    {
                        "id": notification_id,
                        "platform_order_no": f"ORDER-{notification_id}",
                        "recipient_name": "Customer",
                        "recipient_email": "customer@example.com",
                        "state": "AWAITING_REVIEW",
                        "package_total": 1,
                        "package_complete": 1,
                        "package_missing": 0,
                        "subject": "Shipment update",
                        "body": "Full email body",
                        "items": [],
                    }
                ],
                "page": page,
                "page_size": 50,
                "total": 100,
                "total_pages": 2,
                "product_types": [],
            }

    controller = PagedApprovalController()
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page._reload()
    page._show_notification_page(2)
    monkeypatch.setattr(page, "_confirm_batch_review", lambda _items: True)

    page._approve()

    assert page._notification_page == 1
    assert controller.submitted_commands[-1].payload["notification_ids"] == [62]
    assert controller.page_calls[-1]["page"] == 1
    assert controller.page_calls[-1]["active_notification_ids"] == (62,)
    page.deleteLater()


def test_notification_approval_discards_stale_checked_rows_and_sends_review_rows(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 62,
            "platform_order_no": "705-STALE-SENT",
            "state": "DELIVERED",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
        {
            "id": 63,
            "platform_order_no": "705-READY",
            "recipient_name": "Ready Customer",
            "recipient_email": "ready@example.com",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "subject": "Shipment update",
            "body": "Body",
            "items": [],
        },
    ]
    results: list[ControlResult] = []
    page = ShipmentNotificationPage(controller, results.append)
    page._reload()
    page._checked_notification_ids.update({62, 63})
    monkeypatch.setattr(page, "_confirm_batch_review", lambda _items: True)

    page._approve()

    command = controller.submitted_commands[-1]
    assert command.payload["notification_ids"] == [63]
    assert page._checked_notification_ids == set()
    assert page._optimistic_send_notification_ids == {63}
    assert any("已自动排除 1 条" in result.message for result in results)
    page.deleteLater()


def test_notification_page_polls_server_rows_while_provider_receipt_is_pending(
    app,
    monkeypatch,
) -> None:
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 64,
            "platform_order_no": "705-ACCEPTED",
            "state": "ACCEPTED",
            "provider_message_id": "provider-64",
            "items": [],
        }
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    page.show()
    page._reload()
    reloads = 0
    original_reload = page._reload

    def counted_reload() -> None:
        nonlocal reloads
        reloads += 1
        original_reload()

    monkeypatch.setattr(page, "_reload", counted_reload)

    page._reload_pending_receipt_states()

    assert reloads == 1
    page.close()
    page.deleteLater()


def test_main_window_refresh_updates_only_visible_page(app, monkeypatch):
    window = DesktopMainWindow(RecordingController())
    window._timer.stop()
    calls = {index: 0 for index in range(len(window._page_widgets))}
    ensure_calls = 0

    for index, page in enumerate(window._page_widgets):
        monkeypatch.setattr(
            page,
            "update_snapshot",
            lambda _snapshot, current=index: calls.__setitem__(current, calls[current] + 1),
        )
    original_ensure_loaded = window.custom_orders_page.ensure_loaded

    def record_ensure_loaded() -> None:
        nonlocal ensure_calls
        ensure_calls += 1
        original_ensure_loaded()

    monkeypatch.setattr(
        window.custom_orders_page,
        "ensure_loaded",
        record_ensure_loaded,
    )

    window.navigation.setCurrentRow(1)
    assert ensure_calls == 1
    calls = {index: 0 for index in calls}
    window.refresh()

    assert calls[1] == 1
    assert sum(calls.values()) == 1
    window.deleteLater()


def test_api_wait_notice_only_tracks_shipment_scan_with_visible_followup(app):
    window = DesktopMainWindow(RecordingController())
    window._timer.stop()
    custom_scan = TaskRecord(
        "custom-scan",
        "扫描定制订单候选",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        status=TaskStatus.RUNNING,
        payload={"trigger": "five_minute_timer"},
    )
    window._sync_api_wait_notice(DesktopSnapshot(tasks=[custom_scan]))
    assert window._api_wait_notice is None

    notification_scan = TaskRecord(
        "notification-scan",
        "扫描订单并同步物流",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        status=TaskStatus.RUNNING,
        payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
    )
    window._sync_api_wait_notice(DesktopSnapshot(tasks=[notification_scan]))
    assert window._api_wait_notice is None

    scan = TaskRecord(
        "manual-scan",
        "扫描候选并查询物流",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        status=TaskStatus.RUNNING,
        payload={
            "trigger": "manual_button",
            "local_visible_logistics_followup": True,
        },
    )
    window._sync_api_wait_notice(DesktopSnapshot(tasks=[scan]))
    assert window._api_wait_notice is not None
    assert window._api_wait_notice.isModal() is False
    assert (
        window._api_wait_notice.standardButtons()
        == QMessageBox.StandardButton.NoButton
    )

    window._sync_api_wait_notice(DesktopSnapshot())
    assert window._api_wait_notice is None
    window.deleteLater()


def test_feature_pages_use_clear_stop_labels_and_remove_wms_retry_action(app):
    controller = RecordingController()
    pages = (
        CustomOrdersPage(controller, lambda _result: None),
        ShipmentPage(controller, lambda _result: None),
        ShipmentNotificationPage(controller, lambda _result: None),
        StateManagementPage(controller, lambda _result: None),
    )

    custom_action_labels = {
        action.text() for action in pages[0]._status_menu.actions()
    }
    notification_action_labels = {
        action.text()
        for action in pages[2].notification_more_actions_menu.actions()
    }
    for labels in (custom_action_labels, notification_action_labels):
        assert "停止当前勾选任务" in labels
        assert "停止本页所有任务" not in labels

    shipment_action_labels = {
        action.text() for action in pages[1].more_actions_menu.actions()
    }
    assert "停止当前勾选任务" in shipment_action_labels
    assert "停止本页所有任务" not in shipment_action_labels

    state_labels = {
        button.text() for button in pages[3].findChildren(QPushButton)
    }
    assert "停止当前勾选任务" in state_labels
    assert "停止本页所有任务" in state_labels

    alibaba_page = AlibabaOrderPage(controller, lambda _result: None)
    alibaba_labels = {
        button.text() for button in alibaba_page.findChildren(QPushButton)
    }
    assert "停止本页所有任务" not in alibaba_labels

    assert "人工核对物流并放行" in shipment_action_labels
    assert "确认标发" not in shipment_action_labels
    assert "选择销售出库单并重试" not in shipment_action_labels
    assert not hasattr(pages[1], "_select_wms_outbound_and_retry")

    for page in (*pages, alibaba_page):
        page.deleteLater()


def test_order_pages_use_separate_page_filter_and_batch_rows(app):
    controller = RecordingController()
    custom = CustomOrdersPage(controller, lambda _result: None)
    shipment = ShipmentPage(controller, lambda _result: None)
    notification = ShipmentNotificationPage(controller, lambda _result: None)

    for filter_widget in (
        custom.status_filter_combo,
        custom.product_type_filter_combo,
        custom.search_field_combo,
        custom.search_edit,
    ):
        assert custom._filter_row_layout.indexOf(filter_widget) >= 0
    for page_action in (custom.scan_button, custom.scan_logs_button):
        assert custom._page_action_row_layout.indexOf(page_action) >= 0
        assert custom._batch_action_row_layout.indexOf(page_action) == -1
    for batch_widget in (
        custom.custom_selection_summary,
        custom.quick_select_button,
        custom.status_action_button,
        custom.process_button,
    ):
        assert custom._batch_action_row_layout.indexOf(batch_widget) >= 0

    for filter_widget in (
        shipment.status_filter_combo,
        shipment.product_type_filter_combo,
        shipment.search_field_combo,
        shipment.search_edit,
    ):
        assert shipment._filter_row_layout.indexOf(filter_widget) >= 0
    assert shipment._filter_row_layout.indexOf(shipment.quick_select_button) == -1

    for page_action in (
        shipment.scan_button,
        shipment.logistics_button,
        shipment.scan_logs_button,
    ):
        assert shipment._page_action_row_layout.indexOf(page_action) >= 0
        assert shipment._batch_action_row_layout.indexOf(page_action) == -1

    for batch_widget in (
        shipment.ready_count_label,
        shipment.quick_select_button,
        shipment.more_actions_button,
        shipment.execute_button,
    ):
        assert shipment._batch_action_row_layout.indexOf(batch_widget) >= 0
    assert [
        action.text() for action in shipment.more_actions_menu.actions()
    ] == [
        "修改物流单号和承运商",
        "人工核对物流并放行",
        "修改状态",
        "重试阶段",
        "",
        "停止当前勾选任务",
    ]
    assert [action.text() for action in shipment.retry_actions_menu.actions()] == [
        "重试物流阶段",
        "核验 ERP 状态并安全继续",
    ]

    assert notification._filter_contact_row_layout.indexOf(
        notification.product_type_filter_combo
    ) >= 0
    assert not hasattr(notification, "contact_refresh_button")
    assert not hasattr(notification, "edit_contact_button")

    for page_action in (notification.receipt_button, notification.rescan_button):
        assert notification._page_action_row_layout.indexOf(page_action) >= 0
        assert notification._batch_action_row_layout.indexOf(page_action) == -1
    for batch_widget in (
        notification.notification_selection_summary,
        notification.quick_select_review_button,
        notification.notification_more_actions_button,
        notification.approve_button,
    ):
        assert notification._batch_action_row_layout.indexOf(batch_widget) >= 0
    assert [
        action.text()
        for action in notification.notification_more_actions_menu.actions()
    ] == [
        "从定制 JSON 获取联系方式",
        "修改联系方式",
        "修改包裹承运商和物流单号",
        "",
        "重新提交审核",
        "重试已批准内容",
        "修改状态",
        "",
        "停止当前勾选任务",
    ]

    custom.deleteLater()
    shipment.deleteLater()
    notification.deleteLater()


def test_order_queue_toolbars_use_compact_responsive_geometry(app):
    controller = RecordingController()
    custom = CustomOrdersPage(controller, lambda _result: None)
    shipment = ShipmentPage(controller, lambda _result: None)
    notification = ShipmentNotificationPage(controller, lambda _result: None)

    assert custom.status_filter_combo.minimumWidth() == 150
    assert custom.status_filter_combo.maximumWidth() == 220
    assert custom.search_field_combo.minimumWidth() == 128
    assert custom.search_field_combo.maximumWidth() == 160
    assert custom.search_edit.minimumWidth() == 180
    assert custom.search_edit.maximumWidth() == 520

    assert shipment.status_filter_combo.minimumWidth() == 150
    assert shipment.status_filter_combo.maximumWidth() == 220
    assert shipment.search_field_combo.minimumWidth() == 128
    assert shipment.search_field_combo.maximumWidth() == 160
    assert shipment.search_edit.minimumWidth() == 180
    assert shipment.search_edit.maximumWidth() == 520

    assert notification.product_type_filter_combo.minimumWidth() == 180
    assert notification.product_type_filter_combo.maximumWidth() == 300
    assert notification.search_field_combo.minimumWidth() == 128
    assert notification.search_field_combo.maximumWidth() == 160
    assert notification.search_edit.minimumWidth() == 180
    assert notification.search_edit.maximumWidth() == 520

    shipment_page_actions = (
        shipment.scan_button,
        shipment.logistics_button,
        shipment.scan_logs_button,
    )
    shipment_batch_widgets = (
        shipment.ready_count_label,
        shipment.quick_select_button,
        shipment.more_actions_button,
        shipment.execute_button,
    )
    custom_page_actions = (custom.scan_button, custom.scan_logs_button)
    custom_batch_widgets = (
        custom.custom_selection_summary,
        custom.quick_select_button,
        custom.status_action_button,
        custom.process_button,
    )
    notification_page_actions = (
        notification.receipt_button,
        notification.rescan_button,
    )
    notification_batch_widgets = (
        notification.notification_selection_summary,
        notification.quick_select_review_button,
        notification.notification_more_actions_button,
        notification.approve_button,
    )
    for button in (
        *custom_page_actions,
        *shipment_page_actions,
        *notification_page_actions,
    ):
        assert (
            button.sizePolicy().horizontalPolicy()
            == qt_module.QSizePolicy.Policy.Preferred
        )

    for page in (custom, shipment, notification):
        page.resize(1105, 760)
        page.show()
    app.processEvents()

    for widgets in (
        custom_page_actions,
        custom_batch_widgets,
        shipment_page_actions,
        shipment_batch_widgets,
        notification_page_actions,
        notification_batch_widgets,
    ):
        assert all(
            left.geometry().right() < right.geometry().left()
            for left, right in zip(widgets, widgets[1:])
        )

    product_filter_label = shipment._filter_row_layout.itemAtPosition(0, 2).widget()
    status_to_product_gap = (
        product_filter_label.geometry().left()
        - shipment.status_filter_combo.geometry().right()
        - 1
    )
    product_filter_gap = (
        shipment.product_type_filter_combo.geometry().left()
        - product_filter_label.geometry().right()
        - 1
    )
    assert 0 <= status_to_product_gap <= shipment._filter_row_layout.horizontalSpacing()
    assert 0 <= product_filter_gap <= shipment._filter_row_layout.horizontalSpacing()

    for page in (custom, shipment, notification):
        page.resize(874, 700)
    app.processEvents()
    for page in (custom, shipment, notification):
        assert page.width() == 874
        assert page.minimumSizeHint().width() <= 874
    for widgets in (
        custom_page_actions,
        custom_batch_widgets,
        shipment_page_actions,
        shipment_batch_widgets,
        notification_page_actions,
        notification_batch_widgets,
    ):
        assert all(
            left.geometry().right() < right.geometry().left()
            for left, right in zip(widgets, widgets[1:])
        )
        assert all(
            widget.width() >= widget.minimumSizeHint().width()
            for widget in widgets
        )

    notification_product_label = (
        notification._filter_contact_row_layout.itemAtPosition(0, 0).widget()
    )
    product_gap = (
        notification.product_type_filter_combo.geometry().left()
        - notification_product_label.geometry().right()
        - 1
    )
    row_spacing = notification._filter_contact_row_layout.horizontalSpacing()
    assert 0 <= product_gap <= row_spacing

    for page in (custom, shipment, notification):
        page.close()
        page.deleteLater()


def test_custom_page_stops_only_currently_checked_active_tasks_and_all_page_tasks(
    app,
    monkeypatch,
):
    controller = RecordingController()
    page = CustomOrdersPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow("111-CHECKED"),
                CustomOrderRow("112-OTHER"),
            ],
            tasks=[
                TaskRecord(
                    "custom-checked",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    order_no="111-CHECKED",
                ),
                TaskRecord(
                    "custom-other",
                    "处理定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    order_no="112-OTHER",
                ),
                TaskRecord(
                    "custom-scan",
                    "扫描定制订单",
                    TaskArea.CUSTOMIZATION,
                    Capability.LIST_ORDERS,
                ),
                TaskRecord(
                    "shipment-other-page",
                    "自动标发",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                ),
            ],
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page._stop_checked_tasks()
    page._stop_all_tasks()

    assert controller.cancel_task_calls == [
        ["custom-checked"],
        ["custom-checked", "custom-other", "custom-scan"],
    ]
    page.deleteLater()


def test_notification_stop_requires_whole_batch_and_all_stops_page_tasks(
    app,
    monkeypatch,
):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 71,
            "platform_order_no": "711-BATCH-A",
            "state": "SENDING",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
        {
            "id": 72,
            "platform_order_no": "712-BATCH-B",
            "state": "SENDING",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
    ]
    results: list[ControlResult] = []
    page = ShipmentNotificationPage(controller, results.append)
    page._reload()
    page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "notification-batch",
                    "发送客户通知",
                    TaskArea.SHIPMENT,
                    Capability.SEND_NOTIFICATION,
                    payload={
                        "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                        "notification_ids": [71, 72],
                    },
                ),
                TaskRecord(
                    "notification-rescan",
                    "同步客户通知物流",
                    TaskArea.SHIPMENT,
                    Capability.LIST_ORDERS,
                    payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
                ),
                TaskRecord(
                    "shipment-other-feature",
                    "执行自动标发",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                ),
            ]
        )
    )
    page.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    page._stop_checked_tasks()

    assert not controller.cancel_task_calls
    assert results[-1].accepted is False
    assert "同一批次" in results[-1].message

    page.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    page._stop_checked_tasks()
    page._stop_all_tasks()

    assert controller.cancel_task_calls == [
        ["notification-batch"],
        ["notification-batch", "notification-rescan"],
    ]
    page.deleteLater()


def test_notification_batch_state_is_visible_for_every_item_and_click_shows_conflict(
    app,
    monkeypatch,
):
    controller = RecordingController()
    controller.notification_rows = [
        {
            "id": 71,
            "platform_order_no": "711-BATCH-A",
            "state": "SENDING",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
        {
            "id": 72,
            "platform_order_no": "712-BATCH-B",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "items": [],
        },
        {
            "id": 73,
            "platform_order_no": "713-REVIEW-ONLY",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "state_changed_at": "2026-08-13T23:59:59Z",
            "items": [],
        },
    ]
    page = ShipmentNotificationPage(controller, lambda _result: None)
    task = TaskRecord(
        "notification-batch",
        "发送客户通知（2 条）",
        TaskArea.SHIPMENT,
        Capability.SEND_NOTIFICATION,
        status=TaskStatus.RUNNING,
        payload={
            "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
            "notification_ids": [71, 72],
        },
        operator_name="Alice",
        operator_email="alice@billyprint.com",
    )
    page.update_snapshot(DesktopSnapshot(tasks=[task]))

    states_by_order = {
        page.table.item(row, 1).text(): page.table.item(row, 8).text()
        for row in range(page.table.rowCount())
    }
    assert states_by_order == {
        "711-BATCH-A": "发送中",
        "712-BATCH-B": "等待发送",
        "713-REVIEW-ONLY": "待审核",
    }
    assert {
        page.table.item(row, 1).text() for row in range(2)
    } == {"711-BATCH-A", "712-BATCH-B"}
    assert page.table.item(2, 1).text() == "713-REVIEW-ONLY"
    queued_row = next(
        row
        for row in range(page.table.rowCount())
        if page.table.item(row, 1).text() == "712-BATCH-B"
    )
    assert "Alice" in page.table.item(queued_row, 9).text()

    shown: list[dict[str, object]] = []
    monkeypatch.setattr(
        qt_module,
        "show_queue_conflict_dialog",
        lambda **kwargs: shown.append(kwargs),
    )
    page._on_notification_clicked(queued_row, 0)
    assert shown == []
    page._on_notification_clicked(queued_row, 1)

    assert shown == [
        {
            "order_no": "712-BATCH-B",
            "task_name": "发送客户通知（2 条）",
            "task_status": "运行中",
            "operator_name": "Alice",
            "operator_email": "alice@billyprint.com",
            "parent": page,
        }
    ]
    page.deleteLater()


def test_notification_submit_conflict_uses_modern_shared_queue_dialog(
    app,
    monkeypatch,
):
    notification_id = 81
    order_no = notification_confirmation_order_no([notification_id])
    controller = RecordingController(
        task_results={
            order_no: ControlResult(
                False,
                "该通知已进入其他客户端的处理队列。",
                "task-other-client",
                details={
                    "queue_conflict": True,
                    "conflict_notification_ids": (notification_id,),
                    "conflict_task_name": "发送客户通知（3 条）",
                    "conflict_task_status": "等待中",
                    "conflict_operator_name": "Bob",
                    "conflict_operator_email": "bob@billyprint.com",
                },
            )
        }
    )
    controller.notification_rows = [
        {
            "id": notification_id,
            "platform_order_no": "811-CONFLICT",
            "recipient_name": "Conflict Customer",
            "recipient_email": "conflict@example.com",
            "state": "AWAITING_REVIEW",
            "package_total": 1,
            "package_complete": 1,
            "package_missing": 0,
            "subject": "Shipment update",
            "body": "Body",
            "items": [],
        }
    ]
    results: list[ControlResult] = []
    page = ShipmentNotificationPage(controller, results.append)
    page._reload()
    monkeypatch.setattr(page, "_confirm_batch_review", lambda _items: True)
    shown: list[dict[str, object]] = []
    monkeypatch.setattr(
        qt_module,
        "show_queue_conflict_dialog",
        lambda **kwargs: shown.append(kwargs),
    )

    page._approve()

    assert len(shown) == 1
    assert shown[0]["order_no"] == "811-CONFLICT"
    assert shown[0]["operator_name"] == "Bob"
    assert shown[0]["operator_email"] == "bob@billyprint.com"
    assert results[-1].details["non_modal"] is True
    page.deleteLater()


def test_notification_status_dialog_uses_the_modern_combo_arrow(app):
    dialog = _NotificationStatusDialog(2)

    assert isinstance(dialog.status_combo, _ModernComboBox)
    assert dialog.status_combo.itemText(0) == "人工完成"
    assert dialog.status_combo.itemText(1) == "已取消"
    assert dialog.status_combo.itemText(2) == "待审核（重新提交）"

    dialog.deleteLater()


def test_shipment_page_stops_only_its_active_background_tasks(app, monkeypatch):
    controller = RecordingController()
    page = ShipmentPage(controller, lambda _result: None)
    page.update_snapshot(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "shipment-outbound",
                    "执行自动标发",
                    TaskArea.SHIPMENT,
                    Capability.OUTBOUND_ORDER,
                    payload={"logistics_no": "ALS-STOP"},
                ),
                TaskRecord(
                    "shipment-logistics",
                    "查询阿里物流",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_LOGISTICS,
                ),
                TaskRecord(
                    "notification-rescan",
                    "同步客户通知物流",
                    TaskArea.SHIPMENT,
                    Capability.LIST_ORDERS,
                    payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
                ),
                TaskRecord(
                    "alibaba-draft",
                    "填写阿里物流草稿",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_ORDER_DRAFT,
                ),
            ]
        )
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    page._stop_all_tasks()

    assert controller.cancel_task_calls == [
        ["shipment-outbound", "shipment-logistics"]
    ]
    page.deleteLater()


def test_alibaba_and_state_pages_stop_every_active_page_task(app, monkeypatch):
    controller = RecordingController()
    snapshot = DesktopSnapshot(
        tasks=[
            TaskRecord(
                "alibaba-prepare",
                "准备阿里物流下单",
                TaskArea.SHIPMENT,
                Capability.ALIBABA_ORDER_PREPARE,
            ),
            TaskRecord(
                "alibaba-done",
                "填写阿里物流草稿",
                TaskArea.SHIPMENT,
                Capability.ALIBABA_ORDER_DRAFT,
                status=TaskStatus.SUCCEEDED,
            ),
            TaskRecord(
                "unrelated",
                "扫描定制订单",
                TaskArea.CUSTOMIZATION,
                Capability.LIST_ORDERS,
            ),
        ]
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    alibaba_page = AlibabaOrderPage(controller, lambda _result: None)
    alibaba_page.update_snapshot(snapshot)
    alibaba_page._stop_all_tasks()
    state_page = StateManagementPage(controller, lambda _result: None)
    state_page.update_snapshot(snapshot)
    state_page._cancel_all()

    assert controller.cancel_task_calls == [
        ["alibaba-prepare"],
        ["alibaba-prepare", "unrelated"],
    ]

    alibaba_page.deleteLater()
    state_page.deleteLater()
