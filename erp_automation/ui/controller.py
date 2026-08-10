from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import uuid4

from .models import (
    Capability,
    CapabilityMode,
    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY,
    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY,
    DesktopSettings,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSnapshot,
    LogEntry,
    LogPage,
    LogLevel,
    MigrationInfo,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
    utc_now,
)


@dataclass(frozen=True)
class ControlResult:
    accepted: bool
    message: str
    task_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _notification_ids_from_payload(payload: Mapping[str, Any]) -> set[int]:
    raw_values = payload.get("notification_ids")
    if not isinstance(raw_values, Sequence) or isinstance(
        raw_values,
        (str, bytes),
    ):
        return set()
    notification_ids: set[int] = set()
    for value in raw_values:
        try:
            notification_id = int(value)
        except (TypeError, ValueError):
            continue
        if notification_id > 0:
            notification_ids.add(notification_id)
    return notification_ids


@runtime_checkable
class BackgroundTaskController(Protocol):
    """Boundary between the desktop shell and a real background worker."""

    def snapshot(self) -> DesktopSnapshot: ...

    def submit_task(self, command: TaskCommand) -> ControlResult: ...

    def cancel_task(self, task_id: str) -> ControlResult: ...

    def cancel_tasks(self, task_ids: Sequence[str]) -> ControlResult: ...

    def prepare_close(self) -> ControlResult: ...

    def retry_task(self, task_id: str) -> ControlResult: ...

    def pending_interactions(self) -> tuple[DesktopInteractionRequest, ...]: ...

    def respond_interaction(self, response: DesktopInteractionResponse) -> ControlResult: ...

    def update_capability_mode(
        self,
        capability: Capability,
        mode: CapabilityMode,
    ) -> ControlResult: ...

    def set_emergency_stop_writes(self, enabled: bool) -> ControlResult: ...

    def set_execution_paused(
        self,
        enabled: bool,
        reason: str = "",
    ) -> ControlResult: ...

    def save_settings(self, settings: DesktopSettings) -> ControlResult: ...

    def test_notification_provider(self, provider: str) -> ControlResult: ...

    def list_shipment_notifications(self) -> list[dict[str, Any]]: ...

    def refresh_shipment_notification_receipts(self) -> ControlResult: ...

    def approve_shipment_notification(self, notification_id: int) -> ControlResult: ...

    def approve_shipment_notifications(
        self, notification_ids: Sequence[int]
    ) -> ControlResult: ...

    def retry_shipment_notification(self, notification_id: int) -> ControlResult: ...

    def reject_shipment_notification(self, notification_id: int) -> ControlResult: ...

    def mark_shipment_notifications_manually_completed(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult: ...

    def cancel_shipment_notifications(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult: ...

    def resubmit_shipment_notification(
        self, notification_id: int, *, reason: str
    ) -> ControlResult: ...

    def edit_shipment_notification_contact(
        self, notification_id: int, *, email: str, phone: str
    ) -> ControlResult: ...

    def run_migrations(self, *, dry_run: bool) -> ControlResult: ...

    def export_portable_migration(
        self,
        destination: str,
        passphrase: str,
        *,
        include_state: bool,
    ) -> ControlResult: ...

    def import_portable_migration(
        self,
        package_path: str,
        passphrase: str,
        *,
        overwrite: bool,
        configuration_only: bool = False,
    ) -> ControlResult: ...

    def import_legacy_env(self, env_path: str) -> ControlResult: ...

    def set_custom_stage_state(
        self,
        platform_order_no: str,
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def set_custom_stage_states(
        self,
        platform_order_nos: Sequence[str],
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def complete_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult: ...

    def cancel_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult: ...

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def reopen_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def retry_shipment_stage(
        self,
        logistics_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def retry_shipment_stages(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def reopen_shipments_from_stage(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult: ...

    def cancel_shipments(
        self,
        logistics_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult: ...

    def add_shipment_order(
        self,
        *,
        system_order_no: str,
        platform_order_no: str,
        logistics_no: str,
        reason: str,
    ) -> ControlResult: ...

    def change_shipment_status(
        self,
        logistics_no: str,
        action: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def change_shipment_statuses(
        self,
        logistics_nos: Sequence[str],
        action: str,
        *,
        reason: str,
    ) -> ControlResult: ...

    def confirm_shipment_tracking_pair(
        self,
        logistics_no: str,
        *,
        carrier: str,
        tracking_no: str,
        reason: str,
    ) -> ControlResult: ...

    def full_log_text(self, task_id: str | None = None) -> tuple[str, str]: ...

    def scan_log_text(self, scan_kind: str) -> tuple[str, str]: ...

    def log_directory(self) -> str: ...

    def delete_logs_older_than(self, days: int) -> ControlResult: ...

    def list_log_entries(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        level: str = "",
        query: str = "",
    ) -> LogPage: ...


class InMemoryBackgroundTaskController:
    """Safe desktop-shell controller used until a production worker is wired in.

    It deliberately queues nothing outside this process and starts with ERP writes
    stopped. This makes the UI runnable without coupling it to Playwright.
    """

    _queue_label = "任务队列"

    def __init__(
        self,
        initial: DesktopSnapshot | None = None,
        *,
        log_initial_backend_message: bool = True,
    ) -> None:
        self._state = deepcopy(initial) if initial is not None else DesktopSnapshot()
        self._lock = RLock()
        if log_initial_backend_message and not self._state.logs:
            self._append_log(LogLevel.WARNING, "desktop", self._state.backend_message)

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            self._state.today_tasks = list(self._state.tasks)
            return deepcopy(self._state)

    def submit_task(self, command: TaskCommand) -> ControlResult:
        with self._lock:
            trigger = str(command.payload.get("trigger") or "")
            local_json_refresh = trigger == NOTIFICATION_CONTACT_REFRESH_TRIGGER
            if self._state.policy.execution_paused:
                message = "全部任务已暂停，解除暂停前不会接受新任务。"
                self._append_log(LogLevel.WARNING, command.area.value, message)
                return ControlResult(
                    False,
                    message,
                    details={"execution_paused": True},
                )
            mode = self._state.policy.effective_mode_for(command.capability)
            if mode is CapabilityMode.DISABLED and not local_json_refresh:
                message = f"“{command.capability.label}”当前已禁用，任务未提交。"
                self._append_log(LogLevel.WARNING, command.area.value, message)
                return ControlResult(False, message)

            notification_sync_triggers = {
                NOTIFICATION_REVIEW_RESCAN_TRIGGER,
                SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
            }
            if trigger in notification_sync_triggers:
                duplicate = next(
                    (
                        task
                        for task in self._state.tasks
                        if (
                            str(task.payload.get("trigger") or "")
                            in notification_sync_triggers
                            or (
                                task.area is TaskArea.SHIPMENT
                                and task.capability is Capability.LIST_ORDERS
                                and str(task.payload.get("trigger") or "")
                                == "three_hour_timer"
                            )
                        )
                        and not task.status.terminal
                    ),
                    None,
                )
                if duplicate is not None:
                    return ControlResult(
                        False,
                        "领星客户通知物流正在同步，请等待当前任务完成。",
                        duplicate.task_id,
                    )

            if trigger == NOTIFICATION_CONTACT_REFRESH_TRIGGER:
                duplicate = next(
                    (
                        task
                        for task in self._state.tasks
                        if str(task.payload.get("trigger") or "")
                        == NOTIFICATION_CONTACT_REFRESH_TRIGGER
                        and not task.status.terminal
                    ),
                    None,
                )
                if duplicate is not None:
                    return ControlResult(
                        False,
                        "正在从定制 JSON 读取联系方式，请等待当前任务完成。",
                        duplicate.task_id,
                    )

            if trigger in {
                SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                NOTIFICATION_CONTACT_REFRESH_TRIGGER,
            }:
                requested_notification_ids = _notification_ids_from_payload(
                    command.payload
                )
                duplicate = next(
                    (
                        (task, overlap)
                        for task in self._state.tasks
                        if not task.status.terminal
                        and str(task.payload.get("trigger") or "")
                        in {
                            SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                            NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                        }
                        and (
                            overlap := (
                                requested_notification_ids
                                & _notification_ids_from_payload(task.payload)
                            )
                        )
                    ),
                    None,
                )
                if duplicate is not None:
                    task, overlap = duplicate
                    operator = task.operator_name or task.operator_email
                    operator_text = (
                        f"（操作人：{operator}）"
                        if operator
                        else ""
                    )
                    message = (
                        f"其中 {len(overlap)} 条客户通知已进入其他客户端的处理队列"
                        f"{operator_text}，不能重复提交。"
                    )
                    return ControlResult(
                        False,
                        message,
                        task.task_id,
                        details={
                            "queue_conflict": True,
                            "conflict_notification_ids": tuple(sorted(overlap)),
                            "conflict_task_id": task.task_id,
                            "conflict_task_name": task.name,
                            "conflict_task_status": task.status.label,
                            "conflict_operator_name": task.operator_name,
                            "conflict_operator_email": task.operator_email,
                        },
                    )

            task_id = uuid4().hex
            task = TaskRecord(
                task_id=task_id,
                name=command.name,
                area=command.area,
                capability=command.capability,
                order_no=command.order_no,
                payload=dict(command.payload),
                operator_name=str(
                    command.payload.get(DESKTOP_OPERATOR_NAME_PAYLOAD_KEY) or ""
                ).strip(),
                operator_email=str(
                    command.payload.get(DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY) or ""
                ).strip(),
                message=(
                    f"已进入{self._queue_label}；执行模式：本地 JSON 只读。"
                    if local_json_refresh
                    else f"已进入{self._queue_label}；执行模式：{mode.label}。"
                ),
            )
            self._state.tasks.insert(0, task)
            message = f"任务“{command.name}”已进入{self._queue_label}。"
            self._append_log(LogLevel.INFO, command.area.value, message, task_id=task_id)
            return ControlResult(True, message, task_id)

    def cancel_task(self, task_id: str) -> ControlResult:
        with self._lock:
            match = self._find_task(task_id)
            if match is None:
                return ControlResult(False, f"找不到任务：{task_id}")
            index, task = match
            if task.status.terminal:
                return ControlResult(False, f"任务已处于终态：{task.status.label}")
            self._state.tasks[index] = replace(
                task,
                status=TaskStatus.CANCELLED,
                message="用户已取消。",
                updated_at=utc_now(),
            )
            self._append_log(LogLevel.WARNING, task.area.value, f"任务已取消：{task.name}")
            return ControlResult(True, "任务已取消。", task_id)

    def cancel_tasks(self, task_ids: Sequence[str]) -> ControlResult:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in task_ids))
        normalized = [value for value in normalized if value]
        if not normalized:
            return ControlResult(False, "请先勾选至少一个后台任务。")
        accepted = 0
        rejected: list[str] = []
        first_task_id: str | None = None
        for task_id in normalized:
            result = self.cancel_task(task_id)
            if result.accepted:
                accepted += 1
                first_task_id = first_task_id or task_id
            else:
                rejected.append(f"{task_id}：{result.message}")
        message = f"已取消 {accepted} 个后台任务"
        if rejected:
            message += f"；{len(rejected)} 个未取消。" + "\n" + "\n".join(rejected[:10])
        else:
            message += "。"
        return ControlResult(bool(accepted), message, first_task_id)

    def prepare_close(self) -> ControlResult:
        active = [task.task_id for task in self.snapshot().tasks if not task.status.terminal]
        result = self.cancel_tasks(active) if active else ControlResult(True, "没有活动任务。")
        return ControlResult(True, result.message)

    def retry_task(self, task_id: str) -> ControlResult:
        with self._lock:
            match = self._find_task(task_id)
            if match is None:
                return ControlResult(False, f"找不到任务：{task_id}")
            index, task = match
            if task.status not in {
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.PAUSED,
                TaskStatus.CANCELLED,
            }:
                return ControlResult(False, "只有失败、阻止、暂停或取消的任务可以重试。", task_id)
            mode = self._state.policy.effective_mode_for(task.capability)
            if mode is CapabilityMode.DISABLED:
                return ControlResult(False, f"“{task.capability.label}”当前已禁用。", task_id)
            self._state.tasks[index] = replace(
                task,
                status=TaskStatus.QUEUED,
                progress_percent=0,
                message=f"等待重试；执行模式：{mode.label}。",
                updated_at=utc_now(),
            )
            self._append_log(LogLevel.INFO, task.area.value, f"任务已重新排队：{task.name}")
            return ControlResult(True, "任务已重新排队。", task_id)

    def pending_interactions(self) -> tuple[DesktopInteractionRequest, ...]:
        return ()

    def respond_interaction(self, response: DesktopInteractionResponse) -> ControlResult:
        return ControlResult(False, f"找不到等待确认的请求：{response.request_id}")

    def update_capability_mode(
        self,
        capability: Capability,
        mode: CapabilityMode,
    ) -> ControlResult:
        with self._lock:
            coerced = CapabilityMode.coerce(mode)
            self._state.policy.set_mode(capability, coerced)
            message = f"“{capability.label}”已设置为“{coerced.label}”。"
            self._append_log(LogLevel.INFO, "capability", message)
            return ControlResult(True, message)

    def set_emergency_stop_writes(self, enabled: bool) -> ControlResult:
        with self._lock:
            self._state.policy.emergency_stop_writes = bool(enabled)
            message = "ERP 紧急停止写入已开启。" if enabled else "ERP 紧急停止写入已解除。"
            level = LogLevel.WARNING if enabled else LogLevel.INFO
            self._append_log(level, "safety", message)
            return ControlResult(True, message)

    def set_execution_paused(
        self,
        enabled: bool,
        reason: str = "",
    ) -> ControlResult:
        """Persist the global admission gate and pause every non-terminal task."""

        normalized_reason = str(reason or "").strip()[:500]
        with self._lock:
            active = [task for task in self._state.tasks if not task.status.terminal]
            if not enabled and active:
                return ControlResult(
                    False,
                    "仍有未结束任务，不能解除全局暂停。",
                    details={"execution_paused": True},
                )
            self._state.policy.execution_paused = bool(enabled)
            self._state.policy.execution_pause_reason = normalized_reason if enabled else ""
            if enabled:
                self._state.policy.emergency_stop_writes = True
                now = utc_now()
                for index, task in enumerate(self._state.tasks):
                    if task.status.terminal:
                        continue
                    self._state.tasks[index] = replace(
                        task,
                        status=TaskStatus.PAUSED,
                        message=normalized_reason or "已暂停全部任务。",
                        updated_at=now,
                    )
                message = f"已暂停 {len(active)} 个任务；新任务已禁止提交。"
                level = LogLevel.WARNING
            else:
                message = "已解除全部任务暂停；ERP 写入急停仍需单独解除。"
                level = LogLevel.INFO
            self._append_log(level, "safety", message)
            return ControlResult(
                True,
                message,
                details={
                    "execution_paused": bool(enabled),
                    "paused_tasks": len(active) if enabled else 0,
                },
            )

    def save_settings(self, settings: DesktopSettings) -> ControlResult:
        errors = settings.validate()
        if errors:
            return ControlResult(False, " ".join(errors))
        with self._lock:
            self._state.settings = settings
            self._append_log(LogLevel.INFO, "settings", "桌面配置已保存到控制器。")
            return ControlResult(True, "配置已保存。")

    def test_notification_provider(self, provider: str) -> ControlResult:
        del provider
        return ControlResult(False, "通知供应商连接测试需要持久化控制器。")

    def list_shipment_notifications(self) -> list[dict[str, Any]]:
        return []

    def refresh_shipment_notification_receipts(self) -> ControlResult:
        return ControlResult(False, "发送状态回查需要持久化控制器。")

    def approve_shipment_notification(self, notification_id: int) -> ControlResult:
        del notification_id
        return ControlResult(False, "通知发送需要持久化控制器。")

    def approve_shipment_notifications(
        self, notification_ids: Sequence[int]
    ) -> ControlResult:
        del notification_ids
        return ControlResult(False, "批量通知发送需要持久化控制器。")

    def retry_shipment_notification(self, notification_id: int) -> ControlResult:
        return self.approve_shipment_notification(notification_id)

    def reject_shipment_notification(self, notification_id: int) -> ControlResult:
        del notification_id
        return ControlResult(False, "通知审核需要持久化控制器。")

    def mark_shipment_notifications_manually_completed(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult:
        del notification_ids, reason
        return ControlResult(False, "人工完成通知需要持久化控制器。")

    def cancel_shipment_notifications(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult:
        del notification_ids, reason
        return ControlResult(False, "取消通知需要持久化控制器。")

    def resubmit_shipment_notification(
        self, notification_id: int, *, reason: str
    ) -> ControlResult:
        del reason
        return self.reject_shipment_notification(notification_id)

    def edit_shipment_notification_contact(
        self, notification_id: int, *, email: str, phone: str
    ) -> ControlResult:
        del email, phone
        return self.reject_shipment_notification(notification_id)

    def run_migrations(self, *, dry_run: bool) -> ControlResult:
        with self._lock:
            if dry_run:
                message = "迁移预检完成；桌面骨架未连接实际迁移器。"
            else:
                message = "迁移入口已调用；桌面骨架未执行任何数据库变更。"
            self._state.migration = replace(self._state.migration, last_result=message)
            self._append_log(LogLevel.INFO, "migration", message)
            return ControlResult(True, message)

    def export_portable_migration(
        self,
        destination: str,
        passphrase: str,
        *,
        include_state: bool,
    ) -> ControlResult:
        del destination, passphrase, include_state
        message = "当前内存控制器不会创建迁移包。"
        self._append_log(LogLevel.WARNING, "migration", message)
        return ControlResult(False, message)

    def import_portable_migration(
        self,
        package_path: str,
        passphrase: str,
        *,
        overwrite: bool,
        configuration_only: bool = False,
    ) -> ControlResult:
        del package_path, passphrase, overwrite, configuration_only
        message = "当前内存控制器不会导入迁移包。"
        self._append_log(LogLevel.WARNING, "migration", message)
        return ControlResult(False, message)

    def import_legacy_env(self, env_path: str) -> ControlResult:
        del env_path
        message = "当前内存控制器不会读取 .env。"
        self._append_log(LogLevel.WARNING, "configuration", message)
        return ControlResult(False, message)

    def set_custom_stage_state(
        self,
        platform_order_no: str,
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_no, stage, state, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def set_custom_stage_states(
        self,
        platform_order_nos: Sequence[str],
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_nos, stage, state, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def complete_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_nos, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def cancel_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_nos, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_no, stage, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def reopen_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_nos, stage, reason
        return ControlResult(False, "当前内存控制器没有连接状态数据库。")

    def retry_shipment_stage(
        self,
        logistics_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_no, stage, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def retry_shipment_stages(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_nos, stage, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def reopen_shipments_from_stage(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_nos, stage, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult:
        del logistics_no, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def cancel_shipments(
        self,
        logistics_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_nos, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def add_shipment_order(
        self,
        *,
        system_order_no: str,
        platform_order_no: str,
        logistics_no: str,
        reason: str,
    ) -> ControlResult:
        del system_order_no, platform_order_no, logistics_no, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def change_shipment_status(
        self,
        logistics_no: str,
        action: str,
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_no, action, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def change_shipment_statuses(
        self,
        logistics_nos: Sequence[str],
        action: str,
        *,
        reason: str,
    ) -> ControlResult:
        del logistics_nos, action, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def confirm_shipment_tracking_pair(
        self,
        logistics_no: str,
        *,
        carrier: str,
        tracking_no: str,
        reason: str,
    ) -> ControlResult:
        del logistics_no, carrier, tracking_no, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

    def full_log_text(self, task_id: str | None = None) -> tuple[str, str]:
        del task_id
        return "完整日志", "当前内存控制器没有持久化日志。"

    def scan_log_text(self, scan_kind: str) -> tuple[str, str]:
        del scan_kind
        return "扫描日志", "当前内存控制器没有持久化扫描日志。"

    def log_directory(self) -> str:
        return ""

    def delete_logs_older_than(self, days: int) -> ControlResult:
        del days
        return ControlResult(False, "当前内存控制器没有可清理的持久化日志。")

    def list_log_entries(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        level: str = "",
        query: str = "",
    ) -> LogPage:
        normalized_size = page_size if page_size in {50, 100, 200} else 100
        normalized_page = max(1, int(page))
        needle = str(query or "").strip().casefold()
        normalized_level = str(level or "").strip().upper()
        with self._lock:
            rows = [
                entry
                for entry in self._state.logs
                if (not normalized_level or entry.level.value == normalized_level)
                and (
                    not needle
                    or needle
                    in (
                        f"{entry.task_id or ''} {entry.operator_name} "
                        f"{entry.operator_email} {entry.source} {entry.message}"
                    ).casefold()
                )
            ]
        total = len(rows)
        page_count = max(1, (total + normalized_size - 1) // normalized_size)
        normalized_page = min(normalized_page, page_count)
        start = (normalized_page - 1) * normalized_size
        return LogPage(
            tuple(rows[start : start + normalized_size]),
            normalized_page,
            normalized_size,
            total,
        )

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        message: str = "",
        progress_percent: int | None = None,
    ) -> ControlResult:
        """Test/demo hook; a production adapter would receive this from Worker events."""

        with self._lock:
            match = self._find_task(task_id)
            if match is None:
                return ControlResult(False, f"找不到任务：{task_id}")
            index, task = match
            progress = task.progress_percent if progress_percent is None else progress_percent
            self._state.tasks[index] = replace(
                task,
                status=status,
                message=message or task.message,
                progress_percent=progress,
                updated_at=utc_now(),
            )
            return ControlResult(True, "任务状态已更新。", task_id)

    def _find_task(self, task_id: str) -> tuple[int, TaskRecord] | None:
        for index, task in enumerate(self._state.tasks):
            if task.task_id == task_id:
                return index, task
        return None

    def _append_log(
        self,
        level: LogLevel,
        source: str,
        message: str,
        *,
        task_id: str | None = None,
        operator_name: str = "",
        operator_email: str = "",
    ) -> None:
        if task_id and (not operator_name or not operator_email):
            match = self._find_task(task_id)
            if match is not None:
                operator_name = operator_name or match[1].operator_name
                operator_email = operator_email or match[1].operator_email
        self._state.logs.insert(
            0,
            LogEntry(
                level=level,
                source=source,
                message=message,
                task_id=task_id,
                operator_name=str(operator_name or "").strip(),
                operator_email=str(operator_email or "").strip(),
            ),
        )
        del self._state.logs[1000:]

    def record_operator_event(
        self,
        *,
        operator_name: str,
        operator_email: str,
        operation: str,
        resources: Sequence[str] = (),
        message: str = "",
        task_id: str | None = None,
        accepted: bool = True,
    ) -> None:
        resource_text = "、".join(str(item) for item in resources if str(item))
        result_text = "成功" if accepted else "未执行"
        detail = str(message or "").strip()
        summary = f"{operation}；结果：{result_text}"
        if resource_text:
            summary += f"；对象：{resource_text}"
        if detail:
            summary += f"；{detail}"
        with self._lock:
            self._append_log(
                LogLevel.INFO if accepted else LogLevel.WARNING,
                "operator_audit",
                summary,
                task_id=task_id,
                operator_name=operator_name,
                operator_email=operator_email,
            )
