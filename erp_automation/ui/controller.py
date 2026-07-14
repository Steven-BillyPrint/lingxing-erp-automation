from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol, runtime_checkable
from uuid import uuid4

from .models import (
    Capability,
    CapabilityMode,
    DesktopSettings,
    DesktopSnapshot,
    LogEntry,
    LogLevel,
    MigrationInfo,
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


@runtime_checkable
class BackgroundTaskController(Protocol):
    """Boundary between the desktop shell and a real background worker."""

    def snapshot(self) -> DesktopSnapshot: ...

    def submit_task(self, command: TaskCommand) -> ControlResult: ...

    def cancel_task(self, task_id: str) -> ControlResult: ...

    def retry_task(self, task_id: str) -> ControlResult: ...

    def update_capability_mode(
        self,
        capability: Capability,
        mode: CapabilityMode,
    ) -> ControlResult: ...

    def set_emergency_stop_writes(self, enabled: bool) -> ControlResult: ...

    def save_settings(self, settings: DesktopSettings) -> ControlResult: ...

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

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
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

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult: ...


class InMemoryBackgroundTaskController:
    """Safe desktop-shell controller used until a production worker is wired in.

    It deliberately queues nothing outside this process and starts with ERP writes
    stopped. This makes the UI runnable without coupling it to Playwright.
    """

    def __init__(self, initial: DesktopSnapshot | None = None) -> None:
        self._state = deepcopy(initial) if initial is not None else DesktopSnapshot()
        self._lock = RLock()
        if not self._state.logs:
            self._append_log(LogLevel.WARNING, "desktop", self._state.backend_message)

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            return deepcopy(self._state)

    def submit_task(self, command: TaskCommand) -> ControlResult:
        with self._lock:
            mode = self._state.policy.effective_mode_for(command.capability)
            if mode is CapabilityMode.DISABLED:
                message = f"“{command.capability.label}”当前已禁用，任务未提交。"
                self._append_log(LogLevel.WARNING, command.area.value, message)
                return ControlResult(False, message)

            task_id = uuid4().hex
            task = TaskRecord(
                task_id=task_id,
                name=command.name,
                area=command.area,
                capability=command.capability,
                order_no=command.order_no,
                payload=dict(command.payload),
                message=f"已进入骨架队列；执行模式：{mode.label}。",
            )
            self._state.tasks.insert(0, task)
            message = f"任务“{command.name}”已进入桌面骨架队列。"
            self._append_log(LogLevel.INFO, command.area.value, message)
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

    def retry_task(self, task_id: str) -> ControlResult:
        with self._lock:
            match = self._find_task(task_id)
            if match is None:
                return ControlResult(False, f"找不到任务：{task_id}")
            index, task = match
            if task.status not in {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}:
                return ControlResult(False, "只有失败、阻止或取消的任务可以重试。", task_id)
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

    def save_settings(self, settings: DesktopSettings) -> ControlResult:
        errors = settings.validate()
        if errors:
            return ControlResult(False, " ".join(errors))
        with self._lock:
            self._state.settings = settings
            self._append_log(LogLevel.INFO, "settings", "桌面配置已保存到控制器。")
            return ControlResult(True, "配置已保存。")

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
    ) -> ControlResult:
        del package_path, passphrase, overwrite
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

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        del platform_order_no, stage, reason
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

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult:
        del logistics_no, reason
        return ControlResult(False, "当前内存控制器没有连接自动标发队列。")

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

    def _append_log(self, level: LogLevel, source: str, message: str) -> None:
        self._state.logs.insert(0, LogEntry(level=level, source=source, message=message))
        del self._state.logs[1000:]
