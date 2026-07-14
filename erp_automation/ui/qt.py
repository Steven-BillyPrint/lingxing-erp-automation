from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from .controller import BackgroundTaskController, ControlResult
from .models import (
    Capability,
    CapabilityMode,
    CustomOrderRow,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopSnapshot,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LogEntry,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)
from .qt_compat import PYSIDE6_AVAILABLE, require_pyside6


if PYSIDE6_AVAILABLE:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QScrollArea,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    ResultHandler = Callable[[ControlResult], None]


    def _format_time(value) -> str:
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


    def _readonly_item(text: object, *, user_data: object | None = None) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text or ""))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        return item


    def _prepare_table(table: QTableWidget) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)


    class _MetricCard(QFrame):
        def __init__(self, title: str, color: str) -> None:
            super().__init__()
            self.setObjectName("metricCard")
            self.setStyleSheet(
                "QFrame#metricCard { background: white; border: 1px solid #dfe4ea; "
                "border-radius: 8px; }"
            )
            layout = QVBoxLayout(self)
            title_label = QLabel(title)
            title_label.setStyleSheet("color: #667085;")
            self.value_label = QLabel("0")
            font = QFont()
            font.setPointSize(22)
            font.setBold(True)
            self.value_label.setFont(font)
            self.value_label.setStyleSheet(f"color: {color};")
            layout.addWidget(title_label)
            layout.addWidget(self.value_label)

        def set_value(self, value: int) -> None:
            self.value_label.setText(str(value))


    class DashboardPage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            layout = QVBoxLayout(self)
            title = QLabel("仪表盘")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            self.backend_message = QLabel()
            self.backend_message.setWordWrap(True)
            self.backend_message.setStyleSheet(
                "background: #fff8e1; color: #7a5b00; padding: 10px; border-radius: 5px;"
            )
            layout.addWidget(self.backend_message)

            cards = QGridLayout()
            self.queued_card = _MetricCard("等待中", "#3867d6")
            self.running_card = _MetricCard("运行中", "#8854d0")
            self.succeeded_card = _MetricCard("已完成", "#20bf6b")
            self.attention_card = _MetricCard("需要关注", "#eb3b5a")
            cards.addWidget(self.queued_card, 0, 0)
            cards.addWidget(self.running_card, 0, 1)
            cards.addWidget(self.succeeded_card, 0, 2)
            cards.addWidget(self.attention_card, 0, 3)
            layout.addLayout(cards)

            layout.addWidget(QLabel("最近任务"))
            self.tasks = QTableWidget(0, 6)
            self.tasks.setHorizontalHeaderLabels(
                ["时间", "业务", "任务", "订单号", "状态", "说明"]
            )
            _prepare_table(self.tasks)
            self.tasks.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.tasks.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            layout.addWidget(self.tasks, 1)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            metrics = snapshot.dashboard
            self.queued_card.set_value(metrics.queued)
            self.running_card.set_value(metrics.running)
            self.succeeded_card.set_value(metrics.succeeded)
            self.attention_card.set_value(metrics.attention)
            self.backend_message.setText(snapshot.backend_message)
            rows = snapshot.tasks[:20]
            self.tasks.setRowCount(len(rows))
            for row_index, task in enumerate(rows):
                values = (
                    _format_time(task.updated_at),
                    task.area.label,
                    task.name,
                    task.order_no or "-",
                    task.status.label,
                    task.message,
                )
                for column, value in enumerate(values):
                    self.tasks.setItem(row_index, column, _readonly_item(value))


    class CustomOrdersPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._rows: list[CustomOrderRow] = []
            layout = QVBoxLayout(self)
            title = QLabel("定制订单")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            actions = QHBoxLayout()
            scan_button = QPushButton("扫描候选")
            scan_button.clicked.connect(self._scan)
            process_button = QPushButton("处理选中订单")
            process_button.clicked.connect(self._process_selected)
            self.stage_combo = QComboBox()
            for value, label in (
                ("contact", "联系方式"),
                ("folder", "订单文件夹"),
                ("sku", "SKU 调整"),
                ("package_split", "拆包"),
                ("instruction_remark", "说明书备注"),
            ):
                self.stage_combo.addItem(label, value)
            self.stage_state_combo = QComboBox()
            for value, label in (
                ("PENDING", "待处理"),
                ("COMPLETED", "已完成"),
                ("NOT_REQUIRED", "不需要"),
                ("NOT_APPLICABLE", "不适用"),
                ("BLOCKED", "已阻止"),
            ):
                self.stage_state_combo.addItem(label, value)
            update_state_button = QPushButton("修改阶段状态")
            update_state_button.clicked.connect(self._update_stage_state)
            reopen_button = QPushButton("从此阶段重开")
            reopen_button.clicked.connect(self._reopen_stage)
            actions.addWidget(scan_button)
            actions.addWidget(process_button)
            actions.addWidget(self.stage_combo)
            actions.addWidget(self.stage_state_combo)
            actions.addWidget(update_state_button)
            actions.addWidget(reopen_button)
            actions.addStretch(1)
            layout.addLayout(actions)

            self.table = QTableWidget(0, 6)
            self.table.setHorizontalHeaderLabels(
                ["平台单号", "系统单号", "商品类型", "工作流阶段", "状态", "最后错误"]
            )
            _prepare_table(self.table)
            self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            layout.addWidget(self.table, 1)

        def _scan(self) -> None:
            result = self._controller.submit_task(
                TaskCommand(
                    name="扫描定制订单候选",
                    area=TaskArea.CUSTOMIZATION,
                    capability=Capability.LIST_ORDERS,
                )
            )
            self._result_handler(result)

        def _process_selected(self) -> None:
            index = self.table.currentRow()
            if index < 0 or index >= len(self._rows):
                self._result_handler(ControlResult(False, "请先选择一条定制订单。"))
                return
            row = self._rows[index]
            answer = QMessageBox.question(
                self,
                "确认处理定制订单",
                "即将处理以下订单：\n"
                f"平台单号：{row.platform_order_no}\n"
                f"系统单号：{row.system_order_no or '-'}\n\n"
                "流程可能写入电话、买家邮箱、商品、拆包及客服备注，并创建订单文件夹。"
                "官方 API 不支持的邮箱/完整地址步骤仍会打开网页。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirmation = DesktopWriteConfirmation.create(
                DesktopWriteAction.PROCESS_CUSTOM_ORDER,
                row.platform_order_no,
                system_order_no=row.system_order_no,
            )
            result = self._controller.submit_task(
                TaskCommand(
                    name="处理定制订单",
                    area=TaskArea.CUSTOMIZATION,
                    capability=Capability.UPDATE_CONTACT,
                    order_no=row.platform_order_no,
                    payload={
                        "system_order_no": row.system_order_no,
                        DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                    },
                )
            )
            self._result_handler(result)

        def _selected_order(self) -> CustomOrderRow | None:
            index = self.table.currentRow()
            return self._rows[index] if 0 <= index < len(self._rows) else None

        def _reason(self, title: str) -> str | None:
            reason, accepted = QInputDialog.getText(
                self,
                title,
                "请输入修改原因（会写入审计历史）：",
            )
            value = reason.strip()
            if not accepted:
                return None
            if not value:
                self._result_handler(ControlResult(False, "修改原因不能为空。"))
                return None
            return value

        def _update_stage_state(self) -> None:
            row = self._selected_order()
            if row is None:
                self._result_handler(ControlResult(False, "请先选择一条定制订单。"))
                return
            reason = self._reason("修改订单阶段状态")
            if reason is None:
                return
            self._result_handler(
                self._controller.set_custom_stage_state(
                    row.platform_order_no,
                    str(self.stage_combo.currentData()),
                    str(self.stage_state_combo.currentData()),
                    reason=reason,
                )
            )

        def _reopen_stage(self) -> None:
            row = self._selected_order()
            if row is None:
                self._result_handler(ControlResult(False, "请先选择一条定制订单。"))
                return
            reason = self._reason("重新打开订单工作流")
            if reason is None:
                return
            self._result_handler(
                self._controller.reopen_custom_workflow(
                    row.platform_order_no,
                    str(self.stage_combo.currentData()),
                    reason=reason,
                )
            )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._rows = list(snapshot.custom_orders)
            self.table.setRowCount(len(self._rows))
            for row_index, row in enumerate(self._rows):
                values = (
                    row.platform_order_no,
                    row.system_order_no,
                    row.product_type,
                    row.workflow_stage,
                    row.status_text,
                    row.last_error,
                )
                for column, value in enumerate(values):
                    self.table.setItem(row_index, column, _readonly_item(value))


    class ShipmentPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._rows: list[ShipmentRow] = []
            layout = QVBoxLayout(self)
            title = QLabel("自动标发")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            actions = QHBoxLayout()
            scan_button = QPushButton("扫描候选")
            scan_button.clicked.connect(self._scan)
            logistics_button = QPushButton("查询国际物流")
            logistics_button.clicked.connect(self._query_logistics)
            execute_button = QPushButton("执行选中标发")
            execute_button.clicked.connect(self._execute_selected)
            self.retry_stage_combo = QComboBox()
            self.retry_stage_combo.addItem("重试物流", "logistics")
            self.retry_stage_combo.addItem("重试 ERP", "erp")
            self.retry_stage_combo.addItem("重建邮件预览", "email")
            retry_button = QPushButton("重试选中阶段")
            retry_button.clicked.connect(self._retry_selected_stage)
            cancel_button = QPushButton("取消选中任务")
            cancel_button.clicked.connect(self._cancel_selected)
            actions.addWidget(scan_button)
            actions.addWidget(logistics_button)
            actions.addWidget(execute_button)
            actions.addWidget(self.retry_stage_combo)
            actions.addWidget(retry_button)
            actions.addWidget(cancel_button)
            actions.addStretch(1)
            layout.addLayout(actions)

            self.table = QTableWidget(0, 7)
            self.table.setHorizontalHeaderLabels(
                ["平台单号", "系统单号", "物流单号", "物流状态", "ERP 状态", "检查点", "最后错误"]
            )
            _prepare_table(self.table)
            self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
            layout.addWidget(self.table, 1)

        def _scan(self) -> None:
            result = self._controller.submit_task(
                TaskCommand(
                    name="扫描自动标发候选",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.LIST_ORDERS,
                )
            )
            self._result_handler(result)

        def _execute_selected(self) -> None:
            row = self._selected_row()
            if row is None:
                self._result_handler(ControlResult(False, "请先选择一条待标发订单。"))
                return
            answer = QMessageBox.question(
                self,
                "确认执行 ERP 标发",
                "即将对以下订单执行仓库/物流设置、审核、跟踪号写入和出库：\n"
                f"平台单号：{row.platform_order_no}\n"
                f"系统单号：{row.system_order_no or '-'}\n"
                f"物流单号：{row.logistics_no or '-'}\n\n"
                "该操作会写入 ERP，结果不明确时将停止并转人工处理。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirmation = DesktopWriteConfirmation.create(
                DesktopWriteAction.EXECUTE_ERP_MARK,
                row.platform_order_no,
                system_order_no=row.system_order_no,
                logistics_no=row.logistics_no,
            )
            result = self._controller.submit_task(
                TaskCommand(
                    name="执行自动标发",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.OUTBOUND_ORDER,
                    order_no=row.platform_order_no,
                    payload={
                        "system_order_no": row.system_order_no,
                        "logistics_no": row.logistics_no,
                        DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                    },
                )
            )
            self._result_handler(result)

        def _query_logistics(self) -> None:
            self._result_handler(
                self._controller.submit_task(
                    TaskCommand(
                        name="查询阿里国际物流",
                        area=TaskArea.SHIPMENT,
                        capability=Capability.ALIBABA_LOGISTICS,
                    )
                )
            )

        def _selected_row(self) -> ShipmentRow | None:
            index = self.table.currentRow()
            return self._rows[index] if 0 <= index < len(self._rows) else None

        def _reason(self, title: str) -> str | None:
            reason, accepted = QInputDialog.getText(self, title, "请输入原因（会保留在事件历史）：")
            if not accepted:
                return None
            value = reason.strip()
            if not value:
                self._result_handler(ControlResult(False, "原因不能为空。"))
                return None
            return value

        def _retry_selected_stage(self) -> None:
            row = self._selected_row()
            if row is None:
                self._result_handler(ControlResult(False, "请先选择一条自动标发任务。"))
                return
            reason = self._reason("重试自动标发阶段")
            if reason is None:
                return
            self._result_handler(
                self._controller.retry_shipment_stage(
                    row.logistics_no,
                    str(self.retry_stage_combo.currentData()),
                    reason=reason,
                )
            )

        def _cancel_selected(self) -> None:
            row = self._selected_row()
            if row is None:
                self._result_handler(ControlResult(False, "请先选择一条自动标发任务。"))
                return
            reason = self._reason("取消自动标发任务")
            if reason is None:
                return
            answer = QMessageBox.question(
                self,
                "确认取消",
                "取消后仍会保留历史，可在状态管理中重新处理。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._result_handler(
                    self._controller.cancel_shipment(row.logistics_no, reason=reason)
                )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._rows = list(snapshot.shipments)
            self.table.setRowCount(len(self._rows))
            for row_index, row in enumerate(self._rows):
                values = (
                    row.platform_order_no,
                    row.system_order_no,
                    row.logistics_no,
                    row.logistics_state,
                    row.erp_state,
                    row.checkpoint,
                    row.last_error,
                )
                for column, value in enumerate(values):
                    self.table.setItem(row_index, column, _readonly_item(value))


    class StateManagementPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._tasks: list[TaskRecord] = []
            layout = QVBoxLayout(self)
            title = QLabel("状态管理")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            self.emergency_stop = QCheckBox("紧急停止所有 ERP 写入")
            self.emergency_stop.setStyleSheet("font-weight: bold; color: #c0392b;")
            self.emergency_stop.toggled.connect(self._toggle_emergency_stop)
            layout.addWidget(self.emergency_stop)

            splitter = QSplitter(Qt.Orientation.Vertical)
            capability_panel = QWidget()
            capability_layout = QVBoxLayout(capability_panel)
            capability_layout.setContentsMargins(0, 0, 0, 0)
            capability_layout.addWidget(QLabel("能力执行模式"))
            self.capabilities = QTableWidget(0, 4)
            self.capabilities.setHorizontalHeaderLabels(["能力", "类型", "配置模式", "实际模式"])
            _prepare_table(self.capabilities)
            self.capabilities.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            capability_layout.addWidget(self.capabilities)
            splitter.addWidget(capability_panel)

            task_panel = QWidget()
            task_layout = QVBoxLayout(task_panel)
            task_layout.setContentsMargins(0, 0, 0, 0)
            action_row = QHBoxLayout()
            action_row.addWidget(QLabel("后台任务"))
            action_row.addStretch(1)
            retry_button = QPushButton("重试")
            retry_button.clicked.connect(self._retry_selected)
            cancel_button = QPushButton("取消")
            cancel_button.clicked.connect(self._cancel_selected)
            action_row.addWidget(retry_button)
            action_row.addWidget(cancel_button)
            task_layout.addLayout(action_row)
            self.tasks = QTableWidget(0, 6)
            self.tasks.setHorizontalHeaderLabels(["任务 ID", "业务", "任务", "状态", "进度", "说明"])
            _prepare_table(self.tasks)
            self.tasks.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.tasks.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            task_layout.addWidget(self.tasks)
            splitter.addWidget(task_panel)
            splitter.setSizes([330, 330])
            layout.addWidget(splitter, 1)

        def _toggle_emergency_stop(self, enabled: bool) -> None:
            self._result_handler(self._controller.set_emergency_stop_writes(enabled))

        def _change_mode(self, capability: Capability, mode_value: str) -> None:
            self._result_handler(
                self._controller.update_capability_mode(capability, CapabilityMode(mode_value))
            )

        def _selected_task_id(self) -> str | None:
            row = self.tasks.currentRow()
            if row < 0:
                return None
            item = self.tasks.item(row, 0)
            return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

        def _retry_selected(self) -> None:
            task_id = self._selected_task_id()
            result = (
                self._controller.retry_task(task_id)
                if task_id
                else ControlResult(False, "请先选择一个任务。")
            )
            self._result_handler(result)

        def _cancel_selected(self) -> None:
            task_id = self._selected_task_id()
            result = (
                self._controller.cancel_task(task_id)
                if task_id
                else ControlResult(False, "请先选择一个任务。")
            )
            self._result_handler(result)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self.emergency_stop.blockSignals(True)
            self.emergency_stop.setChecked(snapshot.policy.emergency_stop_writes)
            self.emergency_stop.blockSignals(False)

            capabilities = list(Capability)
            self.capabilities.setRowCount(len(capabilities))
            for row, capability in enumerate(capabilities):
                self.capabilities.setItem(row, 0, _readonly_item(capability.label))
                self.capabilities.setItem(row, 1, _readonly_item("写入" if capability.is_write else "只读"))
                combo = QComboBox()
                if capability is Capability.ALIBABA_LOGISTICS:
                    allowed_modes = (CapabilityMode.BROWSER, CapabilityMode.DISABLED)
                elif capability is Capability.EMAIL_PREVIEW:
                    combo.addItem("本地预览（不发送）", CapabilityMode.API_FIRST.value)
                    combo.addItem(CapabilityMode.DISABLED.label, CapabilityMode.DISABLED.value)
                    allowed_modes = ()
                else:
                    # Every capability covered by the official OpenAPI is
                    # API-only in the new application.  The old browser code is
                    # retained solely in the frozen rollback branch.
                    allowed_modes = (CapabilityMode.API_FIRST, CapabilityMode.DISABLED)
                for mode in allowed_modes:
                    combo.addItem(mode.label, mode.value)
                configured = snapshot.policy.configured_mode_for(capability)
                configured_index = combo.findData(configured.value)
                if configured_index < 0:
                    configured_index = 0
                combo.setCurrentIndex(configured_index)
                combo.currentIndexChanged.connect(
                    lambda _index, cap=capability, box=combo: self._change_mode(cap, box.currentData())
                )
                self.capabilities.setCellWidget(row, 2, combo)
                effective = snapshot.policy.effective_mode_for(capability)
                effective_item = _readonly_item(effective.label)
                if effective is CapabilityMode.DISABLED:
                    effective_item.setForeground(QColor("#c0392b"))
                self.capabilities.setItem(row, 3, effective_item)

            self._tasks = list(snapshot.tasks)
            self.tasks.setRowCount(len(self._tasks))
            for row, task in enumerate(self._tasks):
                short_id = task.task_id[:10]
                values = (
                    short_id,
                    task.area.label,
                    task.name,
                    task.status.label,
                    f"{task.progress_percent}%",
                    task.message,
                )
                for column, value in enumerate(values):
                    user_data = task.task_id if column == 0 else None
                    self.tasks.setItem(row, column, _readonly_item(value, user_data=user_data))


    class SettingsPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._dirty = False
            layout = QVBoxLayout(self)
            title = QLabel("设置")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            body = QWidget()
            body_layout = QVBoxLayout(body)

            def section(label: str) -> QFormLayout:
                frame = QFrame()
                frame.setStyleSheet(
                    "background: white; border: 1px solid #dfe4ea; border-radius: 6px;"
                )
                section_layout = QVBoxLayout(frame)
                heading = QLabel(label)
                heading.setStyleSheet("font-size: 16px; font-weight: bold; border: 0;")
                section_layout.addWidget(heading)
                form_layout = QFormLayout()
                section_layout.addLayout(form_layout)
                body_layout.addWidget(frame)
                return form_layout

            account_form = section("账号与 API（Windows 当前用户加密保存）")
            self.app_id = QLineEdit()
            self.app_secret = QLineEdit()
            self.api_base_url = QLineEdit()
            self.api_base_url.setReadOnly(True)
            self.api_base_url.setToolTip("固定为领星官方 HTTPS OpenAPI，防止凭据发送到错误主机。")
            self.lingxing_account = QLineEdit()
            self.lingxing_password = QLineEdit()
            self.lingxing_remember = QCheckBox("记住领星网页登录状态")
            self.erp_mark_routes = QPlainTextEdit()
            self.erp_mark_routes.setMinimumHeight(130)
            self.erp_mark_routes.setPlaceholderText(
                '{\n  "UPS": {"warehouse_id": 1, "logistics_type_id": 2, '
                '"freight_currency_code": "USD"}\n}'
            )
            self.erp_outbound_strategy = QComboBox()
            self.erp_outbound_strategy.addItem("分阶段审核并出库（推荐）", "staged")
            self.erp_outbound_strategy.addItem("快速出库", "fast_outbound")
            self.alibaba_account = QLineEdit()
            self.alibaba_password = QLineEdit()
            self.alibaba_auto_login = QCheckBox("允许自动登录阿里国际站")
            self.amazon_client_id = QLineEdit()
            self.amazon_client_secret = QLineEdit()
            self.amazon_refresh_token = QLineEdit()
            self.amazon_sandbox = QCheckBox("使用 Amazon SP-API 沙箱")
            for editor in (
                self.app_secret,
                self.lingxing_password,
                self.alibaba_password,
                self.amazon_client_secret,
                self.amazon_refresh_token,
            ):
                editor.setEchoMode(QLineEdit.EchoMode.Password)
            account_form.addRow("领星 AppID", self.app_id)
            account_form.addRow("领星 AppSecret", self.app_secret)
            account_form.addRow("领星 OpenAPI 地址", self.api_base_url)
            account_form.addRow("领星网页账号", self.lingxing_account)
            account_form.addRow("领星网页密码", self.lingxing_password)
            account_form.addRow("领星网页登录", self.lingxing_remember)
            account_form.addRow("ERP 仓库/物流 ID 映射", self.erp_mark_routes)
            account_form.addRow("ERP 出库策略", self.erp_outbound_strategy)
            account_form.addRow("阿里国际站账号", self.alibaba_account)
            account_form.addRow("阿里国际站密码", self.alibaba_password)
            account_form.addRow("阿里网页登录", self.alibaba_auto_login)
            account_form.addRow("Amazon LWA Client ID", self.amazon_client_id)
            account_form.addRow("Amazon LWA Client Secret", self.amazon_client_secret)
            account_form.addRow("Amazon Refresh Token", self.amazon_refresh_token)
            account_form.addRow("Amazon 环境", self.amazon_sandbox)

            path_form = section("路径与运行策略")
            self.folder_root = QLineEdit()
            self.custom_state_path = QLineEdit()
            self.queue_path = QLineEdit()
            self.custom_state_path.setReadOnly(True)
            self.queue_path.setReadOnly(True)
            self.custom_state_path.setToolTip("固定路径可确保状态迁移和跨电脑导入只覆盖业务数据库。")
            self.queue_path.setToolTip("固定路径可确保状态迁移和跨电脑导入只覆盖业务数据库。")
            self.browser_profile = QLineEdit()
            self.log_dir = QLineEdit()
            self.log_dir.setReadOnly(True)
            self.log_dir.setToolTip("固定为程序目录下的 logs，避免日志清理误删其他目录。")
            self.api_timeout = QSpinBox()
            self.api_timeout.setRange(1, 600)
            self.payment_window = QSpinBox()
            self.payment_window.setRange(96, 96)
            self.payment_window.setToolTip("业务规则固定扫描最近 96 小时付款订单。")
            self.log_retention = QSpinBox()
            self.log_retention.setRange(90, 90)
            self.browser_fallback = QCheckBox("API 明确不可用时允许网页补位")
            self.browser_fallback.setText("仅官方无 API 的功能使用网页（固定策略）")
            self.browser_fallback.setChecked(True)
            self.browser_fallback.setEnabled(False)
            self.redact_logs = QCheckBox("日志隐藏令牌、邮箱和电话等敏感内容")
            self.redact_logs.setEnabled(False)
            self.redact_logs.setToolTip("固定开启；日志页面无需额外权限，因此不能关闭敏感信息脱敏。")
            path_form.addRow("订单文件夹根目录", self.folder_root)
            path_form.addRow("定制订单状态数据库", self.custom_state_path)
            path_form.addRow("自动标发队列数据库", self.queue_path)
            path_form.addRow("浏览器 Profile", self.browser_profile)
            path_form.addRow("日志目录", self.log_dir)
            path_form.addRow("API 超时（秒）", self.api_timeout)
            path_form.addRow("付款扫描窗口（小时）", self.payment_window)
            path_form.addRow("日志保留（天）", self.log_retention)
            path_form.addRow("网页补位", self.browser_fallback)
            path_form.addRow("日志脱敏", self.redact_logs)
            path_form.addRow("邮件", QLabel("仅生成本地预览，不连接邮箱、不真实发送"))

            editors = (
                self.app_id,
                self.app_secret,
                self.api_base_url,
                self.lingxing_account,
                self.lingxing_password,
                self.alibaba_account,
                self.alibaba_password,
                self.amazon_client_id,
                self.amazon_client_secret,
                self.amazon_refresh_token,
                self.folder_root,
                self.custom_state_path,
                self.queue_path,
                self.browser_profile,
                self.log_dir,
            )
            for editor in editors:
                editor.textEdited.connect(self._mark_dirty)
            self.erp_mark_routes.textChanged.connect(self._mark_dirty)
            self.erp_outbound_strategy.currentIndexChanged.connect(self._mark_dirty)
            for widget in (self.api_timeout, self.payment_window):
                widget.valueChanged.connect(self._mark_dirty)
            for widget in (
                self.lingxing_remember,
                self.alibaba_auto_login,
                self.amazon_sandbox,
                self.browser_fallback,
                self.redact_logs,
            ):
                widget.toggled.connect(self._mark_dirty)

            actions = QHBoxLayout()
            save_button = QPushButton("保存加密配置")
            save_button.clicked.connect(self._save)
            test_api_button = QPushButton("测试领星 API")
            test_api_button.clicked.connect(self._test_api)
            import_env_button = QPushButton("导入旧 .env")
            import_env_button.clicked.connect(self._import_env)
            migration_check = QPushButton("状态迁移预检")
            migration_check.clicked.connect(lambda: self._run_migration(True))
            migration_execute = QPushButton("JSON 迁入 SQLite")
            migration_execute.clicked.connect(lambda: self._run_migration(False))
            export_button = QPushButton("导出到新电脑")
            export_button.clicked.connect(self._export_portable)
            import_button = QPushButton("从迁移包导入")
            import_button.clicked.connect(self._import_portable)
            for button in (
                save_button,
                test_api_button,
                import_env_button,
                migration_check,
                migration_execute,
                export_button,
                import_button,
            ):
                actions.addWidget(button)
            actions.addStretch(1)
            body_layout.addLayout(actions)

            self.migration_status = QLabel()
            self.migration_status.setWordWrap(True)
            self.migration_status.setStyleSheet("background: #f5f7fa; padding: 8px;")
            body_layout.addWidget(self.migration_status)
            body_layout.addStretch(1)
            scroll.setWidget(body)
            layout.addWidget(scroll, 1)

        def _mark_dirty(self, *_args) -> None:
            self._dirty = True

        def _save(self) -> None:
            settings = DesktopSettings(
                lingxing_app_id=self.app_id.text().strip(),
                lingxing_app_secret=self.app_secret.text(),
                lingxing_api_base_url=self.api_base_url.text().strip(),
                lingxing_account=self.lingxing_account.text().strip(),
                lingxing_password=self.lingxing_password.text(),
                lingxing_remember_login=self.lingxing_remember.isChecked(),
                erp_mark_routes_json=self.erp_mark_routes.toPlainText().strip() or "{}",
                erp_mark_outbound_strategy=str(self.erp_outbound_strategy.currentData()),
                alibaba_account=self.alibaba_account.text().strip(),
                alibaba_password=self.alibaba_password.text(),
                alibaba_auto_login=self.alibaba_auto_login.isChecked(),
                amazon_lwa_client_id=self.amazon_client_id.text().strip(),
                amazon_lwa_client_secret=self.amazon_client_secret.text(),
                amazon_refresh_token=self.amazon_refresh_token.text(),
                amazon_sp_api_sandbox=self.amazon_sandbox.isChecked(),
                folder_root=self.folder_root.text().strip(),
                custom_state_path=self.custom_state_path.text().strip(),
                queue_path=self.queue_path.text().strip(),
                browser_profile=self.browser_profile.text().strip(),
                log_dir=self.log_dir.text().strip(),
                api_timeout_seconds=self.api_timeout.value(),
                payment_window_hours=self.payment_window.value(),
                log_retention_days=90,
                browser_fallback_enabled=True,
                redact_sensitive_logs=self.redact_logs.isChecked(),
            )
            result = self._controller.save_settings(settings)
            if result.accepted:
                self._dirty = False
            self._result_handler(result)

        def _test_api(self) -> None:
            if self._dirty:
                self._result_handler(ControlResult(False, "请先保存加密配置，再测试领星 API。"))
                return
            self._result_handler(
                self._controller.submit_task(
                    TaskCommand(
                        name="测试领星 OpenAPI 连接",
                        area=TaskArea.MAINTENANCE,
                        capability=Capability.LIST_ORDERS,
                    )
                )
            )

        def _run_migration(self, dry_run: bool) -> None:
            if not dry_run:
                answer = QMessageBox.question(
                    self,
                    "确认执行迁移",
                    "旧 JSON 会先备份，再通过 SQLite 事务导入。是否继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._result_handler(self._controller.run_migrations(dry_run=dry_run))

        def _import_env(self) -> None:
            source, _selected_filter = QFileDialog.getOpenFileName(
                self, "选择旧 .env", ".env", "环境配置 (.env);;所有文件 (*)"
            )
            if source:
                self._result_handler(self._controller.import_legacy_env(source))

        def _ask_passphrase(self, *, confirm: bool) -> str | None:
            first, accepted = QInputDialog.getText(
                self,
                "迁移包密码",
                "输入迁移包密码（至少 12 个字符）：",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return None
            if len(first) < 12:
                self._result_handler(ControlResult(False, "迁移包密码至少需要 12 个字符。"))
                return None
            if confirm:
                second, accepted = QInputDialog.getText(
                    self,
                    "确认迁移包密码",
                    "再次输入密码：",
                    QLineEdit.EchoMode.Password,
                )
                if not accepted:
                    return None
                if first != second:
                    self._result_handler(ControlResult(False, "两次输入的迁移包密码不一致。"))
                    return None
            return first

        def _export_portable(self) -> None:
            destination, _selected_filter = QFileDialog.getSaveFileName(
                self,
                "导出跨电脑迁移包",
                "erp-automation-migration.erp-migrate",
                "ERP 迁移包 (*.erp-migrate)",
            )
            if not destination:
                return
            choice = QMessageBox.question(
                self,
                "选择迁移内容",
                "是否同时迁移 SQLite 业务状态和规则？\n选择“否”只迁移加密配置。",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                return
            passphrase = self._ask_passphrase(confirm=True)
            if passphrase is None:
                return
            self._result_handler(
                self._controller.export_portable_migration(
                    destination,
                    passphrase,
                    include_state=choice == QMessageBox.StandardButton.Yes,
                )
            )

        def _import_portable(self) -> None:
            source, _selected_filter = QFileDialog.getOpenFileName(
                self,
                "选择跨电脑迁移包",
                "",
                "ERP 迁移包 (*.erp-migrate);;所有文件 (*)",
            )
            if not source:
                return
            passphrase = self._ask_passphrase(confirm=False)
            if passphrase is None:
                return
            answer = QMessageBox.question(
                self,
                "确认导入",
                "导入会先为被替换的配置和状态创建 .bak。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._result_handler(
                self._controller.import_portable_migration(
                    source,
                    passphrase,
                    overwrite=True,
                )
            )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            if not self._dirty:
                settings = snapshot.settings
                widgets = (
                    (self.app_id, settings.lingxing_app_id),
                    (self.app_secret, settings.lingxing_app_secret),
                    (self.api_base_url, settings.lingxing_api_base_url),
                    (self.lingxing_account, settings.lingxing_account),
                    (self.lingxing_password, settings.lingxing_password),
                    (self.alibaba_account, settings.alibaba_account),
                    (self.alibaba_password, settings.alibaba_password),
                    (self.amazon_client_id, settings.amazon_lwa_client_id),
                    (self.amazon_client_secret, settings.amazon_lwa_client_secret),
                    (self.amazon_refresh_token, settings.amazon_refresh_token),
                    (self.folder_root, settings.folder_root),
                    (self.custom_state_path, settings.custom_state_path),
                    (self.queue_path, settings.queue_path),
                    (self.browser_profile, settings.browser_profile),
                    (self.log_dir, settings.log_dir),
                )
                for widget, value in widgets:
                    widget.setText(value)
                self.api_timeout.setValue(settings.api_timeout_seconds)
                self.erp_mark_routes.setPlainText(settings.erp_mark_routes_json)
                strategy_index = self.erp_outbound_strategy.findData(
                    settings.erp_mark_outbound_strategy
                )
                self.erp_outbound_strategy.setCurrentIndex(max(0, strategy_index))
                self.payment_window.setValue(settings.payment_window_hours)
                self.log_retention.setValue(90)
                self.lingxing_remember.setChecked(settings.lingxing_remember_login)
                self.alibaba_auto_login.setChecked(settings.alibaba_auto_login)
                self.amazon_sandbox.setChecked(settings.amazon_sp_api_sandbox)
                self.browser_fallback.setChecked(True)
                self.redact_logs.setChecked(settings.redact_sensitive_logs)
                self._dirty = False
            migration = snapshot.migration
            pending = "、".join(migration.pending_migrations) if migration.pending_migrations else "无"
            self.migration_status.setText(
                f"当前 schema：{migration.current_schema_version}　"
                f"目标 schema：{migration.target_schema_version}\n"
                f"待执行迁移：{pending}\n{migration.last_result}"
            )


    class LogsPage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._logs: list[LogEntry] = []
            layout = QVBoxLayout(self)
            title = QLabel("日志")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            filters = QHBoxLayout()
            self.level_filter = QComboBox()
            self.level_filter.addItem("全部级别", "")
            for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
                self.level_filter.addItem(level, level)
            self.search = QLineEdit()
            self.search.setPlaceholderText("搜索来源或消息")
            self.level_filter.currentIndexChanged.connect(self._apply_filters)
            self.search.textChanged.connect(self._apply_filters)
            filters.addWidget(self.level_filter)
            filters.addWidget(self.search, 1)
            layout.addLayout(filters)

            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(["时间", "级别", "来源", "消息"])
            _prepare_table(self.table)
            self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            layout.addWidget(self.table, 1)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._logs = list(snapshot.logs)
            self._apply_filters()

        def _apply_filters(self, *_args) -> None:
            level = str(self.level_filter.currentData() or "")
            query = self.search.text().strip().casefold()
            rows = [
                entry
                for entry in self._logs
                if (not level or entry.level.value == level)
                and (not query or query in f"{entry.source} {entry.message}".casefold())
            ]
            self.table.setRowCount(len(rows))
            for row, entry in enumerate(rows):
                values = (_format_time(entry.created_at), entry.level.value, entry.source, entry.message)
                for column, value in enumerate(values):
                    self.table.setItem(row, column, _readonly_item(value))


    class DesktopMainWindow(QMainWindow):
        def __init__(self, controller: BackgroundTaskController) -> None:
            super().__init__()
            self._controller = controller
            self.setWindowTitle("ERP 自动化控制台")
            self.resize(1280, 820)

            root = QWidget()
            root_layout = QHBoxLayout(root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            self.navigation = QListWidget()
            self.navigation.setFixedWidth(170)
            self.navigation.setStyleSheet(
                "QListWidget { background: #202939; color: #e5e7eb; border: 0; padding-top: 12px; }"
                "QListWidget::item { padding: 12px 14px; }"
                "QListWidget::item:selected { background: #3867d6; color: white; }"
            )
            self.pages = QStackedWidget()
            root_layout.addWidget(self.navigation)
            root_layout.addWidget(self.pages, 1)
            self.setCentralWidget(root)

            self.dashboard_page = DashboardPage()
            self.custom_orders_page = CustomOrdersPage(controller, self._show_result)
            self.shipment_page = ShipmentPage(controller, self._show_result)
            self.state_page = StateManagementPage(controller, self._show_result)
            self.settings_page = SettingsPage(controller, self._show_result)
            self.logs_page = LogsPage()
            pages = (
                ("仪表盘", self.dashboard_page),
                ("定制订单", self.custom_orders_page),
                ("自动标发", self.shipment_page),
                ("状态管理", self.state_page),
                ("设置", self.settings_page),
                ("日志", self.logs_page),
            )
            for label, page in pages:
                self.navigation.addItem(QListWidgetItem(label))
                self.pages.addWidget(page)
            self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
            self.navigation.setCurrentRow(0)

            self.setStyleSheet(
                "QMainWindow, QWidget { background: #f5f7fa; color: #273142; }"
                "QLabel#pageTitle { font-size: 22px; font-weight: bold; margin-bottom: 6px; }"
                "QPushButton { padding: 6px 12px; }"
                "QTableWidget { background: white; border: 1px solid #dfe4ea; }"
                "QHeaderView::section { background: #edf1f7; padding: 7px; border: 0; }"
            )
            self.statusBar().showMessage("桌面控制台已启动。")

            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(2000)
            self.refresh()

        def refresh(self) -> None:
            snapshot = self._controller.snapshot()
            self.dashboard_page.update_snapshot(snapshot)
            self.custom_orders_page.update_snapshot(snapshot)
            self.shipment_page.update_snapshot(snapshot)
            self.state_page.update_snapshot(snapshot)
            self.settings_page.update_snapshot(snapshot)
            self.logs_page.update_snapshot(snapshot)

        def _show_result(self, result: ControlResult) -> None:
            self.statusBar().showMessage(result.message, 8000)
            if not result.accepted:
                QMessageBox.warning(self, "操作未执行", result.message)
            self.refresh()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt callback name
            active = [
                task
                for task in self._controller.snapshot().tasks
                if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
            ]
            if active:
                QMessageBox.warning(
                    self,
                    "后台任务尚未结束",
                    f"仍有 {len(active)} 个等待或运行中的任务。请先等待任务完成，"
                    "或在状态管理页安全取消尚未开始的任务；窗口暂不关闭。",
                )
                event.ignore()
                return
            self._timer.stop()
            close_controller = getattr(self._controller, "close", None)
            if callable(close_controller):
                close_controller()
            event.accept()


    def run_desktop(
        controller: BackgroundTaskController,
        *,
        argv: Sequence[str] | None = None,
    ) -> int:
        require_pyside6()
        application = QApplication.instance()
        owns_application = application is None
        if application is None:
            qt_argv = list(argv) if argv is not None else list(sys.argv)
            if not qt_argv:
                qt_argv = ["erp-automation"]
            application = QApplication(qt_argv)
            # Pin a Windows CJK-capable UI font.  Qt's off-screen backend and
            # some freshly installed Windows accounts do not always select a
            # Chinese fallback for Segoe UI, which otherwise renders labels as
            # empty squares even though the source text is valid UTF-8.
            application.setFont(QFont("Microsoft YaHei UI", 9))
            application.setApplicationName("ERP 自动化控制台")
            application.setOrganizationName("ERP Automation")
        window = DesktopMainWindow(controller)
        window.show()
        # Keep a strong reference when embedded in an already-running Qt host.
        setattr(application, "_erp_automation_window", window)
        return application.exec() if owns_application else 0


else:

    class _QtUnavailable:
        def __init__(self, *_args, **_kwargs) -> None:
            require_pyside6()


    DashboardPage = _QtUnavailable
    CustomOrdersPage = _QtUnavailable
    ShipmentPage = _QtUnavailable
    StateManagementPage = _QtUnavailable
    SettingsPage = _QtUnavailable
    LogsPage = _QtUnavailable
    DesktopMainWindow = _QtUnavailable

    def run_desktop(
        controller: BackgroundTaskController,
        *,
        argv: Sequence[str] | None = None,
    ) -> int:
        del controller, argv
        require_pyside6()
        return 2


__all__ = [
    "DashboardPage",
    "CustomOrdersPage",
    "DesktopMainWindow",
    "LogsPage",
    "SettingsPage",
    "ShipmentPage",
    "StateManagementPage",
    "run_desktop",
]
