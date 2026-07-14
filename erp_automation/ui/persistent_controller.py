"""Persistent desktop controller backed by encrypted config and SQLite state."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from erp_automation.configuration import (
    ConfigurationDocument,
    EncryptedConfigurationStore,
    MigrationPathSpec,
    MigrationScope,
    PortableMigrationService,
    import_environment_values,
    parse_env_file,
    with_configuration_defaults,
)
from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState

from .controller import ControlResult, InMemoryBackgroundTaskController
from .models import (
    CustomOrderRow,
    Capability,
    CapabilityMode,
    DesktopSettings,
    DesktopSnapshot,
    DesktopWriteConfirmation,
    LogLevel,
    MigrationInfo,
    ShipmentRow,
    TaskCommand,
    TaskStatus,
)


CONFIG_PATH = Path("data/config.enc")
CUSTOM_STATE_PATH = Path("data/automation.sqlite3")
LEGACY_CUSTOM_STATE_PATH = Path("data/processed_platform_orders.json")
SHIPMENT_STATE_PATH = Path("data/shipment_queue.sqlite3")
_APPLICATION_PHONE_RE = re.compile(r"(?<!\d)\+?\d(?:[\s().-]*\d){6,20}(?!\d)")
_AMAZON_ORDER_RE = re.compile(r"\d{3}-\d{7}-\d{7}")


def _workspace_path(workspace: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def _redact_application_message(message: str, *, task_id: str | None) -> str:
    """Redact free text while retaining identifiers only in trusted audit contexts."""

    from erp_automation.operations.scan_audit import redact_audit_text

    # Remove secrets, contacts, e-mail addresses and URL queries before finding
    # safe identifier spans. This prevents an identifier embedded in sensitive
    # text from breaking the sensitive pattern and being restored afterwards.
    text = redact_audit_text(message, redact_phone=False)
    trusted_spans: list[tuple[int, int]] = []
    normalized_task_id = str(task_id or "").strip()
    if normalized_task_id:
        escaped_task_id = re.escape(normalized_task_id)
        trusted_patterns = (
            rf"(?:审计任务|任务)\s*ID\s*[:：]\s*{escaped_task_id}",
            rf"api_scan[\\/]\d{{4}}-\d{{2}}-\d{{2}}[\\/]{escaped_task_id}"
            rf"(?:\.attempt-\d+)?\.json",
        )
        for pattern in trusted_patterns:
            trusted_spans.extend(match.span() for match in re.finditer(pattern, text))

    order_number = _AMAZON_ORDER_RE.pattern
    trusted_order_patterns = (
        rf"(?:Amazon\s*)?(?:平台)?订单(?:号)?\s*[:：=#]?\s*{order_number}",
        rf"platform_order_no\s*[:=：]\s*{order_number}",
        rf"错误编号\s*[:：]\s*[0-9a-f]{{32}}",
    )
    for pattern in trusted_order_patterns:
        trusted_spans.extend(
            match.span() for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        )

    def redact_phone(match: re.Match[str]) -> str:
        start, end = match.span()
        if any(start >= safe_start and end <= safe_end for safe_start, safe_end in trusted_spans):
            return match.group(0)
        return "<redacted-phone>"

    return _APPLICATION_PHONE_RE.sub(redact_phone, text)


def _settings_from_values(values: dict[str, Any]) -> DesktopSettings:
    normalized = with_configuration_defaults(values)
    raw_routes = normalized.get("lingxing.erp_mark.routes", {})
    if isinstance(raw_routes, str):
        routes_json = raw_routes
    else:
        routes_json = json.dumps(raw_routes, ensure_ascii=False, indent=2, sort_keys=True)
    return DesktopSettings(
        lingxing_app_id=str(normalized.get("lingxing.app_id") or ""),
        lingxing_app_secret=str(normalized.get("lingxing.app_secret") or ""),
        lingxing_api_base_url=str(normalized.get("lingxing.api_base_url") or ""),
        lingxing_account=str(normalized.get("lingxing.account") or ""),
        lingxing_password=str(normalized.get("lingxing.password") or ""),
        lingxing_remember_login=bool(normalized.get("lingxing.remember_login")),
        erp_mark_routes_json=routes_json,
        erp_mark_outbound_strategy=str(
            normalized.get("lingxing.erp_mark.outbound_strategy") or "staged"
        ),
        alibaba_account=str(normalized.get("alibaba.account") or ""),
        alibaba_password=str(normalized.get("alibaba.password") or ""),
        alibaba_auto_login=bool(normalized.get("alibaba.auto_login")),
        amazon_lwa_client_id=str(normalized.get("amazon.lwa_client_id") or ""),
        amazon_lwa_client_secret=str(normalized.get("amazon.lwa_client_secret") or ""),
        amazon_refresh_token=str(normalized.get("amazon.refresh_token") or ""),
        amazon_sp_api_sandbox=bool(normalized.get("amazon.sp_api_sandbox")),
        folder_root=str(normalized.get("paths.folder_root") or ""),
        custom_state_path=str(normalized.get("paths.custom_state_db") or CUSTOM_STATE_PATH),
        queue_path=str(normalized.get("paths.shipment_queue_db") or SHIPMENT_STATE_PATH),
        browser_profile=str(normalized.get("paths.browser_profile") or "browser_profile"),
        log_dir=str(normalized.get("paths.log_dir") or "logs"),
        api_timeout_seconds=int(normalized.get("api.timeout_seconds") or 30),
        payment_window_hours=int(normalized.get("automation.payment_window_hours") or 96),
        log_retention_days=90,
        browser_fallback_enabled=bool(normalized.get("automation.browser_fallback_enabled")),
        redact_sensitive_logs=bool(normalized.get("logs.redact_sensitive")),
    )


def _settings_values(settings: DesktopSettings) -> dict[str, Any]:
    routes = json.loads(settings.erp_mark_routes_json or "{}")
    return {
        "lingxing.app_id": settings.lingxing_app_id.strip(),
        "lingxing.app_secret": settings.lingxing_app_secret,
        "lingxing.api_base_url": settings.lingxing_api_base_url.strip(),
        "lingxing.account": settings.lingxing_account.strip(),
        "lingxing.password": settings.lingxing_password,
        "lingxing.remember_login": settings.lingxing_remember_login,
        "lingxing.erp_mark.routes": routes,
        "lingxing.erp_mark.outbound_strategy": settings.erp_mark_outbound_strategy,
        "alibaba.account": settings.alibaba_account.strip(),
        "alibaba.password": settings.alibaba_password,
        "alibaba.auto_login": settings.alibaba_auto_login,
        "amazon.lwa_client_id": settings.amazon_lwa_client_id.strip(),
        "amazon.lwa_client_secret": settings.amazon_lwa_client_secret,
        "amazon.refresh_token": settings.amazon_refresh_token,
        "amazon.sp_api_sandbox": settings.amazon_sp_api_sandbox,
        "paths.folder_root": settings.folder_root.strip(),
        "paths.custom_state_db": settings.custom_state_path.strip(),
        "paths.shipment_queue_db": settings.queue_path.strip(),
        "paths.browser_profile": settings.browser_profile.strip(),
        "paths.log_dir": settings.log_dir.strip(),
        "api.timeout_seconds": settings.api_timeout_seconds,
        "automation.payment_window_hours": settings.payment_window_hours,
        "logs.retention_days": 90,
        "automation.browser_fallback_enabled": settings.browser_fallback_enabled,
        "logs.redact_sensitive": settings.redact_sensitive_logs,
        "email.mode": "preview_only",
    }


def _checkpoint_sqlite(path: Path) -> None:
    if not path.is_file():
        return
    with sqlite3.connect(path, timeout=15) as connection:
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA wal_checkpoint(FULL)")


class PersistentBackgroundTaskController(InMemoryBackgroundTaskController):
    """Own durable settings, migration and visible state-management operations."""

    _queue_label = "后台任务队列"

    def __init__(
        self,
        workspace: str | Path,
        *,
        config_store: EncryptedConfigurationStore | None = None,
        migration_service: PortableMigrationService | None = None,
        task_runner: Callable[[TaskCommand], Any] | None = None,
        initial: DesktopSnapshot | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._application_log_lock = threading.Lock()
        self.config_store = config_store or EncryptedConfigurationStore(self.workspace / CONFIG_PATH)
        self.migration_service = migration_service or PortableMigrationService()
        self._configuration_values: dict[str, Any] = {}
        self._custom_store: CustomWorkflowStore | None = None
        self._task_runner = task_runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erp-desktop-worker")
        self._futures: dict[str, Future[Any]] = {}
        super().__init__(initial, log_initial_backend_message=False)
        self._state.backend_message = "桌面程序已连接加密配置和 SQLite 状态库。"
        if not self._state.logs:
            self._append_log(LogLevel.INFO, "desktop", self._state.backend_message)
        self._load_configuration()
        self._refresh_persistent_rows()

    def _append_log(
        self,
        level: LogLevel,
        source: str,
        message: str,
        *,
        task_id: str | None = None,
    ) -> None:
        """Keep the concise UI row and append a durable, redacted JSONL event."""

        with self._lock:
            super()._append_log(level, source, message, task_id=task_id)
            entry = self._state.logs[0]
        try:
            from erp_automation.operations.scan_audit import redact_audit_text

            root = self.workspace / "logs"
            root.mkdir(parents=True, exist_ok=True)
            resolved_root = root.resolve()
            if not resolved_root.is_relative_to(self.workspace):
                return
            local_day = entry.created_at.astimezone().strftime("%Y-%m-%d")
            path = resolved_root / "app_events" / f"{local_day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_message = _redact_application_message(
                entry.message,
                task_id=entry.task_id,
            )

            payload = {
                "timestamp": entry.created_at.astimezone().isoformat(timespec="milliseconds"),
                "level": entry.level.value,
                "task_id": redact_audit_text(entry.task_id or "", redact_phone=False),
                "source": redact_audit_text(entry.source, redact_phone=False),
                "message": safe_message,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._application_log_lock:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
        except (OSError, TypeError, ValueError):
            # Logging must never recurse or break the business operation.
            return

    def log_directory(self) -> str:
        root = self.workspace / "logs"
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        return str(resolved if resolved.is_relative_to(self.workspace) else self.workspace)

    def full_log_text(self, task_id: str | None = None) -> tuple[str, str]:
        """Return one safe audit document or recent concise application events."""

        normalized_task_id = str(task_id or "").strip()
        if normalized_task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized_task_id):
            return "完整日志", "任务 ID 格式无效。"
        root = Path(self.log_directory())
        if normalized_task_id:
            candidates = sorted(
                root.glob(f"api_scan/*/{normalized_task_id}*.json"),
                key=lambda path: path.stat().st_mtime if path.is_file() else 0,
            )
            documents: list[tuple[Path, str]] = []
            for candidate in candidates:
                if candidate.name != f"{normalized_task_id}.json" and not candidate.name.startswith(
                    f"{normalized_task_id}.attempt-"
                ):
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    if not resolved.is_relative_to(root) or not resolved.is_file():
                        continue
                    documents.append((resolved, resolved.read_text(encoding="utf-8")))
                except (OSError, UnicodeError):
                    continue
            if documents:
                if len(documents) == 1:
                    path, content = documents[0]
                    return f"任务 {normalized_task_id} · {path}", content
                content = "\n\n".join(
                    f"===== 第 {index} 次记录 · {path.name} =====\n{text}"
                    for index, (path, text) in enumerate(documents, start=1)
                )
                return (
                    f"任务 {normalized_task_id} · 共 {len(documents)} 次扫描记录",
                    content,
                )

        rendered: list[str] = []
        event_files = sorted((root / "app_events").glob("*.jsonl"), reverse=True)
        for path in event_files[:90]:
            try:
                with self._application_log_lock:
                    lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in reversed(lines):
                try:
                    item = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if normalized_task_id and str(item.get("task_id") or "") != normalized_task_id:
                    continue
                rendered.append(
                    f"[{item.get('timestamp', '')}] {item.get('level', '')} "
                    f"[{item.get('task_id') or '-'}] {item.get('source', '')}: "
                    f"{item.get('message', '')}"
                )
                if len(rendered) >= 2000:
                    break
            if len(rendered) >= 2000:
                break
        if rendered:
            label = f"任务 {normalized_task_id} 的应用日志" if normalized_task_id else "最近应用日志"
            return label, "\n".join(reversed(rendered))
        if normalized_task_id:
            return f"任务 {normalized_task_id}", "没有找到该任务的完整审计日志。"
        return "完整日志", "目前还没有持久化日志。"

    def attach_task_runner(self, runner: Callable[[TaskCommand], Any]) -> None:
        """Attach the single-worker business executor after controller creation."""

        with self._lock:
            self._task_runner = runner
            self._state.backend_message = "桌面程序后台执行器已就绪；任务按顺序运行。"

    def configuration_values(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._configuration_values)

    def _has_active_tasks_locked(self) -> bool:
        return any(not future.done() for future in self._futures.values())

    def _maintenance_blocked_result_locked(self) -> ControlResult | None:
        if not self._has_active_tasks_locked():
            return None
        return ControlResult(
            False,
            "后台仍有等待或运行中的任务；为避免 SQLite 状态丢失，请等待任务结束后再执行迁移或状态维护。",
        )

    def _cancel_queued_write_tasks_locked(self) -> int:
        """Cancel writes that have not begun after the emergency stop is raised."""

        cancelled = 0
        for task_id, future in list(self._futures.items()):
            match = self._find_task(task_id)
            if match is None or not match[1].capability.is_write:
                continue
            if future.done() or future.running() or not future.cancel():
                continue
            message = "ERP 写入急停已开启；该排队写任务未执行。"
            self.set_task_status(
                task_id,
                TaskStatus.BLOCKED,
                message=message,
                progress_percent=100,
            )
            self._append_log(LogLevel.WARNING, match[1].area.value, message)
            self._futures.pop(task_id, None)
            cancelled += 1
        return cancelled

    def _custom_workflow_gate_message_locked(self, command: TaskCommand) -> str | None:
        if command.area.value != "customization" or not command.order_no:
            return None
        workflow = self._get_custom_store().get_workflow(command.order_no)
        if not workflow:
            return None
        if bool(workflow.get("ignored")):
            return "该订单已忽略；如确需重做，请先填写原因并从目标阶段重开。"
        status = str(workflow.get("workflow_status") or "").casefold()
        if status == "completed":
            return "该订单已完成；如确需重做，请先填写原因并从目标阶段重开。"
        blocked_stages = [
            str(stage.get("stage") or "")
            for stage in workflow.get("stages", [])
            if str(stage.get("state") or "") == str(WorkflowStageState.BLOCKED)
        ]
        if blocked_stages or status == "blocked":
            detail = "、".join(stage for stage in blocked_stages if stage) or "未知阶段"
            return f"该订单存在已阻止阶段：{detail}。请先人工读回并从对应阶段重开。"
        return None

    def submit_task(self, command: TaskCommand) -> ControlResult:
        with self._lock:
            if self._task_runner is None:
                return ControlResult(False, "后台执行器尚未就绪，任务未提交。")
            confirmation: DesktopWriteConfirmation | None = None
            if command.capability.is_write:
                try:
                    confirmation = DesktopWriteConfirmation.from_payload(command.payload)
                except ValueError as exc:
                    return ControlResult(False, str(exc))
            if command.area.value == "customization" and command.order_no:
                duplicate = next(
                    (
                        task
                        for task in self._state.tasks
                        if task.area.value == "customization"
                        and task.order_no == command.order_no
                        and task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}
                    ),
                    None,
                )
                if duplicate is not None:
                    return ControlResult(False, "该订单已有等待或运行中的任务，不能重复排队。")
            try:
                gate_message = self._custom_workflow_gate_message_locked(command)
            except Exception as exc:
                return ControlResult(False, f"读取订单工作流状态失败：{type(exc).__name__}。")
            if gate_message:
                return ControlResult(False, gate_message)

            result = super().submit_task(command)
            if not result.accepted or not result.task_id:
                return result
            if confirmation is not None:
                self._append_log(
                    LogLevel.INFO,
                    "safety",
                    "已记录桌面写入确认："
                    f"{confirmation.action.value} / {confirmation.order_no} / "
                    f"{confirmation.confirmation_id}",
                )
            execution_command = replace(command, execution_id=result.task_id)
            self._futures[result.task_id] = self._executor.submit(
                self._execute_task,
                result.task_id,
                execution_command,
            )
            return ControlResult(True, f"任务“{command.name}”已进入后台队列。", result.task_id)

    def _execute_task(self, task_id: str, command: TaskCommand) -> None:
        try:
            with self._lock:
                mode = self._state.policy.effective_mode_for(command.capability)
                gate_message = self._custom_workflow_gate_message_locked(command)
                if mode is CapabilityMode.DISABLED:
                    gate_message = f"“{command.capability.label}”已被急停或禁用；排队任务未执行。"
                if gate_message:
                    self.set_task_status(
                        task_id,
                        TaskStatus.BLOCKED,
                        message=gate_message,
                        progress_percent=100,
                    )
                    self._append_log(
                        LogLevel.WARNING,
                        command.area.value,
                        gate_message,
                        task_id=task_id,
                    )
                    return
                self.set_task_status(
                    task_id,
                    TaskStatus.RUNNING,
                    message="正在执行。",
                    progress_percent=10,
                )
            result = self._task_runner(command) if self._task_runner is not None else None
            if isinstance(result, Mapping):
                payload = dict(result)
                status = str(payload.get("status") or "completed")
                message = str(payload.get("message") or f"任务完成：{status}")
                failed = status in {"failed", "error", "config_missing", "incomplete"}
            else:
                payload = getattr(result, "payload", {}) if result is not None else {}
                message = str(getattr(result, "message", "任务已完成。"))
                failed = not bool(getattr(result, "succeeded", True))
            blocked = bool(getattr(result, "blocked", False)) or str(
                payload.get("status") or ""
            ).lower() in {"blocked", "manual_review", "unknown"}
            if blocked and command.order_no:
                payload.setdefault("platform_order_no", command.order_no)
            with self._lock:
                self._apply_task_payload(payload)
            self.set_task_status(
                task_id,
                TaskStatus.BLOCKED if blocked else (TaskStatus.FAILED if failed else TaskStatus.SUCCEEDED),
                message=message,
                progress_percent=100,
            )
            self._append_log(
                LogLevel.WARNING if blocked else (LogLevel.ERROR if failed else LogLevel.INFO),
                command.area.value,
                message,
                task_id=task_id,
            )
        except Exception as exc:
            message = f"后台任务失败：{type(exc).__name__}。请在日志中查看对应任务。"
            self.set_task_status(task_id, TaskStatus.FAILED, message=message, progress_percent=100)
            self._append_log(LogLevel.ERROR, command.area.value, message, task_id=task_id)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)
                self._refresh_persistent_rows()

    def _apply_task_payload(self, payload: Mapping[str, Any]) -> None:
        custom_rows = payload.get("custom_orders")
        if isinstance(custom_rows, list):
            self._state.custom_orders = [
                item
                if isinstance(item, CustomOrderRow)
                else CustomOrderRow(
                    platform_order_no=str(item.get("platform_order_no") or ""),
                    system_order_no=str(item.get("system_order_no") or ""),
                    product_type=str(item.get("product_type") or ""),
                    workflow_stage=str(item.get("workflow_stage") or "candidate"),
                    status_text=str(item.get("status_text") or "待处理"),
                    last_error=str(item.get("last_error") or ""),
                )
                for item in custom_rows
                if isinstance(item, (CustomOrderRow, Mapping))
            ]

        status = str(payload.get("status") or "").strip().lower()
        stage = str(payload.get("workflow_blocked_stage") or "").strip()
        platform_order_no = str(payload.get("platform_order_no") or "").strip()
        if (
            status in {"blocked", "manual_review", "unknown"}
            and stage
            and platform_order_no
            and not bool(payload.get("workflow_block_recorded"))
        ):
            reason = str(payload.get("message") or "写入结果无法确认，需人工读回复核。").strip()
            try:
                store = self._get_custom_store()
                if store.get_workflow(platform_order_no) is None:
                    store.mutate_legacy_record(
                        platform_order_no,
                        lambda current: {**current, "workflow_status": "pending"},
                        event_type="desktop_processing_blocked_initialized",
                        actor="desktop_worker",
                        reason=reason,
                    )
                store.set_stage_state(
                    platform_order_no,
                    stage,
                    WorkflowStageState.BLOCKED,
                    reason=reason,
                    actor="desktop_worker",
                    result_status=status,
                    last_error=reason,
                )
            except Exception as exc:
                self._append_log(
                    LogLevel.ERROR,
                    "custom_state",
                    f"人工复核状态持久化失败：{type(exc).__name__}。",
                )

    def cancel_task(self, task_id: str) -> ControlResult:
        with self._lock:
            future = self._futures.get(task_id)
            if future is not None and future.running():
                return ControlResult(False, "任务已经开始运行，不能安全强制终止；请等待当前原子步骤完成。", task_id)
            if future is not None and not future.cancel():
                return ControlResult(False, "任务当前无法安全取消。", task_id)
        return super().cancel_task(task_id)

    def retry_task(self, task_id: str) -> ControlResult:
        if self._task_runner is None:
            return ControlResult(False, "后台执行器尚未就绪。", task_id)
        with self._lock:
            match = self._find_task(task_id)
            if match is not None and match[1].capability.is_write:
                return ControlResult(
                    False,
                    "危险写入任务不能复用旧确认重试；请返回对应订单页面重新核对并确认。",
                    task_id,
                )
        result = super().retry_task(task_id)
        if not result.accepted:
            return result
        with self._lock:
            match = self._find_task(task_id)
            if match is None:
                return ControlResult(False, "找不到需要重试的任务。", task_id)
            _index, task = match
            command = TaskCommand(
                name=task.name,
                area=task.area,
                capability=task.capability,
                payload=dict(task.payload),
                order_no=task.order_no,
                execution_id=task_id,
            )
            self._futures[task_id] = self._executor.submit(self._execute_task, task_id, command)
        return result

    def close(self) -> None:
        """Stop accepting work; running atomic work is allowed to finish."""

        self._executor.shutdown(wait=False, cancel_futures=True)

    def _load_configuration(self) -> None:
        try:
            if self.config_store.exists:
                document = self.config_store.load(allow_backup_fallback=True)
                self._configuration_values = with_configuration_defaults(document.values)
            else:
                self._configuration_values = with_configuration_defaults()
                self.config_store.save(ConfigurationDocument(values=self._configuration_values))
            self._state.settings = _settings_from_values(self._configuration_values)
            self._load_capability_policy()
        except Exception as exc:
            # Avoid repr(exc): arbitrary crypto errors must never echo a payload.
            self._configuration_values = with_configuration_defaults()
            self._state.settings = _settings_from_values(self._configuration_values)
            self._state.backend_message = (
                "加密配置无法读取；ERP 写入保持紧急停止。"
                f"错误类型：{type(exc).__name__}。可尝试恢复 config.enc.bak。"
            )
            self._state.policy.emergency_stop_writes = True
            self._append_log(LogLevel.ERROR, "configuration", self._state.backend_message)

    def _load_capability_policy(self) -> None:
        self._state.policy.emergency_stop_writes = not bool(
            self._configuration_values.get("safety.erp_writes_enabled", False)
        )
        for capability in Capability:
            raw = self._configuration_values.get(f"capabilities.{capability.value}")
            if raw is None:
                continue
            try:
                mode = CapabilityMode.coerce(str(raw))
                if capability is not Capability.ALIBABA_LOGISTICS and mode is CapabilityMode.BROWSER:
                    mode = CapabilityMode.API_FIRST
                self._state.policy.set_mode(capability, mode)
            except ValueError:
                self._append_log(
                    LogLevel.WARNING,
                    "configuration",
                    f"忽略未知能力模式：{capability.value}。",
                )

    def _persist_runtime_policy(self) -> None:
        values = dict(self._configuration_values)
        values["safety.erp_writes_enabled"] = not self._state.policy.emergency_stop_writes
        for capability in Capability:
            values[f"capabilities.{capability.value}"] = self._state.policy.configured_mode_for(
                capability
            ).value
        document = ConfigurationDocument(values=with_configuration_defaults(values))
        self.config_store.save(document)
        self._configuration_values = document.values

    def update_capability_mode(
        self,
        capability: Capability,
        mode: CapabilityMode,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            normalized_mode = CapabilityMode.coerce(mode)
            if (
                capability is not Capability.ALIBABA_LOGISTICS
                and normalized_mode is CapabilityMode.BROWSER
            ):
                return ControlResult(False, "官方 API 已覆盖该能力，新程序不允许切回网页实现。")
            previous = self._state.policy.configured_mode_for(capability)
            result = super().update_capability_mode(capability, normalized_mode)
            try:
                self._persist_runtime_policy()
            except Exception as exc:
                self._state.policy.set_mode(capability, previous)
                return ControlResult(False, f"能力模式保存失败：{type(exc).__name__}。")
            return result

    def set_emergency_stop_writes(self, enabled: bool) -> ControlResult:
        with self._lock:
            if not enabled and self._has_active_tasks_locked():
                return ControlResult(
                    False,
                    "后台仍有任务时不能解除 ERP 写入急停；请等待任务结束并核对状态。",
                )
            previous = self._state.policy.emergency_stop_writes
            result = super().set_emergency_stop_writes(enabled)
            try:
                self._persist_runtime_policy()
            except Exception as exc:
                # If saving an enabled stop fails, retain the safer in-memory
                # stop.  If lifting the stop fails, revert to the previous stop.
                self._state.policy.emergency_stop_writes = True if enabled else previous
                if enabled:
                    self._cancel_queued_write_tasks_locked()
                return ControlResult(False, f"写入急停状态保存失败：{type(exc).__name__}。")
            if enabled:
                cancelled = self._cancel_queued_write_tasks_locked()
                if cancelled:
                    return ControlResult(
                        True,
                        f"{result.message} 已阻止 {cancelled} 个尚未开始的写任务。",
                    )
            return result

    def _custom_state_path(self) -> Path:
        return _workspace_path(self.workspace, self._state.settings.custom_state_path)

    def _shipment_state_path(self) -> Path:
        return _workspace_path(self.workspace, self._state.settings.queue_path)

    def _get_custom_store(self) -> CustomWorkflowStore:
        path = self._custom_state_path()
        if self._custom_store is None or self._custom_store.path.resolve() != path.resolve():
            self._custom_store = CustomWorkflowStore(path)
        return self._custom_store

    def _refresh_persistent_rows(self) -> None:
        try:
            custom_store = self._get_custom_store()
            if custom_store.path.exists():
                rows = custom_store.list_workflows(limit=2000)
                output: list[CustomOrderRow] = []
                for row in rows:
                    detail = custom_store.get_workflow(str(row["platform_order_no"])) or {}
                    errors = [
                        str(stage.get("last_error") or "")
                        for stage in detail.get("stages", [])
                        if stage.get("last_error")
                    ]
                    output.append(
                        CustomOrderRow(
                            platform_order_no=str(row.get("platform_order_no") or ""),
                            system_order_no=str(row.get("original_system_order_no") or ""),
                            product_type=str(row.get("product_type") or ""),
                            workflow_stage=str(row.get("workflow_status") or ""),
                            status_text="已忽略" if row.get("ignored") else str(row.get("workflow_status") or ""),
                            last_error="；".join(errors),
                        )
                    )
                self._state.custom_orders = output
        except Exception as exc:
            self._append_log(LogLevel.ERROR, "custom_state", f"读取定制订单状态失败：{type(exc).__name__}。")

        shipment_path = self._shipment_state_path()
        if not shipment_path.is_file():
            self._state.shipments = []
            return
        try:
            from shipment_automation.queue_store import ShipmentWorkflowStore

            rows = ShipmentWorkflowStore(shipment_path).list_all_jobs(limit=2000)
            self._state.shipments = [
                ShipmentRow(
                    platform_order_no=str(row.get("platform_order_no") or ""),
                    system_order_no=str(row.get("system_order_no") or ""),
                    logistics_no=str(row.get("logistics_no") or ""),
                    identity_state=str(row.get("identity_state") or ""),
                    identity_status_text=str(row.get("identity_status_text") or ""),
                    logistics_state=str(row.get("logistics_state") or ""),
                    erp_state=str(row.get("erp_state") or ""),
                    checkpoint=str(row.get("erp_checkpoint") or ""),
                    last_error=str(row.get("last_error") or ""),
                )
                for row in rows
            ]
        except Exception as exc:
            self._append_log(LogLevel.ERROR, "shipment_state", f"读取自动标发状态失败：{type(exc).__name__}。")

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            self._refresh_persistent_rows()
            return super().snapshot()

    def save_settings(self, settings: DesktopSettings) -> ControlResult:
        errors = settings.validate()
        if errors:
            return ControlResult(False, " ".join(errors))
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            previous_custom_path = self._state.settings.custom_state_path
            values = dict(self._configuration_values)
            values.update(_settings_values(settings))
            try:
                document = ConfigurationDocument(values=with_configuration_defaults(values))
                self.config_store.save(document)
            except Exception as exc:
                message = f"加密配置保存失败：{type(exc).__name__}。原配置未被替换。"
                self._append_log(LogLevel.ERROR, "configuration", message)
                return ControlResult(False, message)
            self._configuration_values = document.values
            self._state.settings = settings
            if settings.custom_state_path != previous_custom_path:
                self._custom_store = None
            self._append_log(LogLevel.INFO, "configuration", "统一加密配置已保存。")
            return ControlResult(True, "配置已加密保存；敏感字段不会写入日志。")

    def import_legacy_env(self, env_path: str) -> ControlResult:
        source = _workspace_path(self.workspace, env_path)
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                parsed = parse_env_file(source)
                translated = import_environment_values(parsed)
                values = dict(self._configuration_values)
                from erp_automation.configuration.settings import ENV_KEY_MAP

                for env_key, config_key in ENV_KEY_MAP.items():
                    if env_key in parsed:
                        values[config_key] = translated[config_key]
                document = ConfigurationDocument(values=with_configuration_defaults(values))
                self.config_store.save(document)
            except Exception as exc:
                message = f"导入 .env 失败：{type(exc).__name__}。文件未被修改。"
                self._append_log(LogLevel.ERROR, "configuration", message)
                return ControlResult(False, message)
            self._configuration_values = document.values
            self._state.settings = _settings_from_values(document.values)
            self._append_log(LogLevel.INFO, "configuration", "旧 .env 已导入加密配置。")
            return ControlResult(True, "旧 .env 已导入。确认新程序可用后可人工删除明文 .env。")

    def run_migrations(self, *, dry_run: bool) -> ControlResult:
        source = self.workspace / LEGACY_CUSTOM_STATE_PATH
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            if not source.is_file():
                message = "未发现旧 processed_platform_orders.json；无需迁移。"
                self._state.migration = MigrationInfo(1, 1, (), message)
                return ControlResult(True, message)
            try:
                payload = json.loads(source.read_text(encoding="utf-8-sig"))
                orders = payload.get("orders", {}) if isinstance(payload, dict) else {}
                count = len(orders) if isinstance(orders, dict) else 0
                if dry_run:
                    message = f"迁移预检通过：可导入 {count} 条定制订单状态，原 JSON 会先备份。"
                    self._state.migration = MigrationInfo(0, 1, ("processed JSON → SQLite",), message)
                    return ControlResult(True, message)
                result = self._get_custom_store().import_legacy_json(source, create_backup=True)
            except Exception as exc:
                message = f"状态迁移失败：{type(exc).__name__}。SQLite 事务已回滚。"
                self._state.migration = replace(self._state.migration, last_result=message)
                self._append_log(LogLevel.ERROR, "migration", message)
                return ControlResult(False, message)
            self._refresh_persistent_rows()
            message = f"状态迁移完成：导入 {result.imported_count}/{result.source_count} 条；旧 JSON 备份已保留。"
            self._state.migration = MigrationInfo(1, 1, (), message)
            self._append_log(LogLevel.INFO, "migration", message)
            return ControlResult(True, message)

    def _portable_specs(self) -> tuple[MigrationPathSpec, ...]:
        candidates = (
            self._state.settings.custom_state_path,
            self._state.settings.queue_path,
            "data/china_workdays.json",
            "rules",
        )
        specs: list[MigrationPathSpec] = []
        for value in candidates:
            path = Path(value)
            if path.is_absolute():
                try:
                    path = path.resolve().relative_to(self.workspace)
                except ValueError:
                    continue
            specs.append(MigrationPathSpec(path.as_posix(), required=False))
        return tuple(specs)

    def export_portable_migration(
        self,
        destination: str,
        passphrase: str,
        *,
        include_state: bool,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                if include_state:
                    _checkpoint_sqlite(self._custom_state_path())
                    _checkpoint_sqlite(self._shipment_state_path())
                manifest = self.migration_service.export_from_store(
                    self.config_store,
                    destination,
                    passphrase,
                    scope=MigrationScope.FULL if include_state else MigrationScope.CONFIGURATION_ONLY,
                    workspace_root=self.workspace if include_state else None,
                    path_specs=self._portable_specs() if include_state else (),
                )
            except Exception as exc:
                message = f"跨电脑迁移包导出失败：{type(exc).__name__}。"
                self._append_log(LogLevel.ERROR, "portable_migration", message)
                return ControlResult(False, message)
            message = f"迁移包已导出，共包含 {len(manifest.files)} 个状态/规则文件。"
            self._append_log(LogLevel.INFO, "portable_migration", message)
            return ControlResult(True, message)

    def import_portable_migration(
        self,
        package_path: str,
        passphrase: str,
        *,
        overwrite: bool,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                result = self.migration_service.import_package(
                    package_path,
                    passphrase,
                    config_store=self.config_store,
                    destination_root=self.workspace,
                    overwrite=overwrite,
                )
                self._custom_store = None
                self._load_configuration()
                self._refresh_persistent_rows()
            except Exception as exc:
                message = f"迁移包导入失败：{type(exc).__name__}。原文件保持不变或已有 .bak。"
                self._append_log(LogLevel.ERROR, "portable_migration", message)
                return ControlResult(False, message)
            message = f"迁移包导入完成：恢复 {result.imported_file_count} 个状态/规则文件。"
            self._append_log(LogLevel.INFO, "portable_migration", message)
            return ControlResult(True, message)

    def set_custom_stage_state(
        self,
        platform_order_no: str,
        stage: str,
        state: str,
        *,
        reason: str,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                self._get_custom_store().set_stage_state(
                    platform_order_no,
                    stage,
                    WorkflowStageState(state),
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except Exception as exc:
                return ControlResult(False, f"状态修改失败：{type(exc).__name__}。")
            return ControlResult(True, "订单阶段状态已修改并记录审计原因。")

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                self._get_custom_store().reopen_from_stage(
                    platform_order_no,
                    stage,
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except Exception as exc:
                return ControlResult(False, f"重新打开工作流失败：{type(exc).__name__}。")
            return ControlResult(True, "工作流已从所选阶段重新打开，并保留历史。")

    def retry_shipment_stage(
        self,
        logistics_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        if stage not in {"logistics", "erp", "email"}:
            return ControlResult(False, "未知自动标发阶段。")
        if not reason.strip():
            return ControlResult(False, "重试必须填写原因。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                store = ShipmentWorkflowStore(self._shipment_state_path())
                changed = (
                    store.retry_email_for_logistics_no(logistics_no, reason=reason)
                    if stage == "email"
                    else store.retry_stage(logistics_no, stage, reason=reason)
                )
                self._refresh_persistent_rows()
            except Exception as exc:
                return ControlResult(False, f"自动标发阶段重试失败：{type(exc).__name__}。")
            return ControlResult(changed, "阶段已重新进入自动流程。" if changed else "队列状态无需改变。")

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult:
        if not reason.strip():
            return ControlResult(False, "取消任务必须填写原因。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                changed = ShipmentWorkflowStore(self._shipment_state_path()).cancel(logistics_no, reason)
                self._refresh_persistent_rows()
            except Exception as exc:
                return ControlResult(False, f"取消自动标发任务失败：{type(exc).__name__}。")
            return ControlResult(changed, "自动标发任务已取消并保留历史。" if changed else "队列状态无需改变。")

    def add_shipment_order(
        self,
        *,
        system_order_no: str,
        platform_order_no: str,
        logistics_no: str,
        reason: str,
    ) -> ControlResult:
        """Add one local shipment queue item without performing an ERP write."""

        if not reason.strip():
            return ControlResult(False, "手动添加订单必须填写原因。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                result = ShipmentWorkflowStore(self._shipment_state_path()).add_manual_candidate(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    logistics_no=logistics_no,
                    reason=reason,
                )
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"手动添加自动标发订单失败：{type(exc).__name__}。")
            if result.conflict:
                message = "该物流单号已属于另一订单，队列已标记为身份冲突；请先人工核对。"
                self._append_log(LogLevel.WARNING, "shipment_state", message)
                return ControlResult(False, message)
            message = (
                "订单已手动加入自动标发队列，并保留事件历史。"
                if result.inserted
                else "该物流单号已存在，已刷新订单信息并保留事件历史。"
            )
            self._append_log(LogLevel.INFO, "shipment_state", message)
            return ControlResult(True, message)

    def change_shipment_status(
        self,
        logistics_no: str,
        action: str,
        *,
        reason: str,
    ) -> ControlResult:
        """Apply one guarded queue transition selected by the desktop user."""

        if not reason.strip():
            return ControlResult(False, "修改队列状态必须填写原因。")
        supported = {
            "retry_logistics",
            "retry_erp",
            "cancel",
            "restore_cancelled",
            "mark_manual_done",
            "undo_manual_done",
        }
        if action not in supported:
            return ControlResult(False, "未知的队列状态操作。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                store = ShipmentWorkflowStore(self._shipment_state_path())
                if action == "retry_logistics":
                    changed = store.retry_stage(logistics_no, "logistics", reason=reason)
                elif action == "retry_erp":
                    changed = store.retry_stage(logistics_no, "erp", reason=reason)
                elif action == "cancel":
                    changed = store.cancel(logistics_no, reason)
                elif action == "restore_cancelled":
                    changed = store.restore_cancelled(logistics_no, reason=reason)
                elif action == "mark_manual_done":
                    changed = store.mark_manually_completed(logistics_no, reason=reason)
                else:
                    changed = store.undo_manual_completion(logistics_no, reason=reason)
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"修改自动标发队列状态失败：{type(exc).__name__}。")
            if not changed:
                return ControlResult(False, "当前状态不允许执行该操作，队列未改变。")
            message = "队列状态已修改，原因和前后状态已写入事件历史。"
            self._append_log(LogLevel.INFO, "shipment_state", message)
            return ControlResult(True, message)


__all__ = ["PersistentBackgroundTaskController"]
