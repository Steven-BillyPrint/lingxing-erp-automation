"""Persistent desktop controller backed by encrypted config and SQLite state."""

from __future__ import annotations

import json
import asyncio
import re
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

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
from erp_automation.persistence import (
    CustomWorkflowStore,
    WorkflowPauseKind,
    WorkflowStageState,
)
from erp_automation.operations import cleanup_expired_logs

from .controller import ControlResult, InMemoryBackgroundTaskController
from .models import (
    CustomOrderRow,
    Capability,
    CapabilityMode,
    DesktopSettings,
    DesktopSnapshot,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopWriteConfirmation,
    LogLevel,
    LogEntry,
    LogPage,
    MigrationInfo,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
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
            rf"(?:custom_order_scan|shipment_scan)[\\/]\d{{4}}-\d{{2}}-\d{{2}}"
            rf"[\\/](?:custom_order_scan|shipment_scan)_\d{{8}}_\d{{6}}_"
            rf"{escaped_task_id}(?:\.attempt-\d+)?\.json",
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
    raw_virtual_domains = normalized.get("notifications.virtual_email_domains", {})
    if isinstance(raw_virtual_domains, str):
        virtual_domains_json = raw_virtual_domains
    else:
        virtual_domains_json = json.dumps(
            raw_virtual_domains, ensure_ascii=False, indent=2, sort_keys=True
        )
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
        alimail_application_name=str(normalized.get("alimail.application_name") or ""),
        alimail_app_id=str(normalized.get("alimail.app_id") or ""),
        alimail_app_secret=str(normalized.get("alimail.app_secret") or ""),
        alimail_amazon_sender_email=str(
            normalized.get("alimail.amazon_sender_email") or "acs@billyprint.com"
        ),
        alimail_independent_sender_email=str(
            normalized.get("alimail.independent_sender_email") or "cs@billyprint.com"
        ),
        alimail_sender_display_name=str(
            normalized.get("alimail.sender_display_name")
            or "BillyPrint Customer Service"
        ),
        clicksend_username=str(normalized.get("clicksend.username") or ""),
        clicksend_api_key=str(normalized.get("clicksend.api_key") or ""),
        clicksend_sender_id=str(normalized.get("clicksend.sender_id") or ""),
        notification_virtual_email_domains_json=virtual_domains_json,
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
    virtual_domains = json.loads(settings.notification_virtual_email_domains_json or "{}")
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
        "alimail.application_name": settings.alimail_application_name.strip(),
        "alimail.app_id": settings.alimail_app_id.strip(),
        "alimail.app_secret": settings.alimail_app_secret,
        "alimail.amazon_sender_email": settings.alimail_amazon_sender_email.strip(),
        "alimail.independent_sender_email": settings.alimail_independent_sender_email.strip(),
        "alimail.sender_display_name": settings.alimail_sender_display_name.strip(),
        "clicksend.username": settings.clicksend_username.strip(),
        "clicksend.api_key": settings.clicksend_api_key,
        "clicksend.sender_id": settings.clicksend_sender_id.strip(),
        "notifications.virtual_email_domains": virtual_domains,
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
        "email.mode": "disabled",
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
        self._session_id = uuid4().hex
        self._diagnosed_bad_log_lines: set[tuple[str, int]] = set()
        self._application_log_lock = threading.Lock()
        self.config_store = config_store or EncryptedConfigurationStore(self.workspace / CONFIG_PATH)
        self.migration_service = migration_service or PortableMigrationService()
        self._configuration_values: dict[str, Any] = {}
        self._custom_store: CustomWorkflowStore | None = None
        self._custom_rows_signature: tuple[Any, ...] | None = None
        self._shipment_rows_signature: tuple[Any, ...] | None = None
        self._task_runner = task_runner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="erp-desktop-worker")
        self._futures: dict[str, Future[Any]] = {}
        self._pending_interactions: dict[str, DesktopInteractionRequest] = {}
        self._interaction_responses: dict[str, DesktopInteractionResponse] = {}
        self._closing_requested = False
        self._shutdown_cancel_requested: set[str] = set()
        super().__init__(initial, log_initial_backend_message=False)
        self._state.backend_message = "桌面程序已连接加密配置和 SQLite 状态库。"
        if not self._state.logs:
            self._append_log(LogLevel.INFO, "desktop", self._state.backend_message)
        self._load_configuration()
        self._refresh_persistent_rows(force=True)

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
                "session_id": self._session_id,
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._application_log_lock:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
        except (OSError, TypeError, ValueError):
            # Logging must never recurse or break the business operation.
            return

    def _write_task_snapshot(self, task: TaskRecord) -> None:
        """Persist one redacted task transition for today's cross-restart view."""

        try:
            from erp_automation.operations.scan_audit import redact_audit_text

            root = (self.workspace / "logs").resolve()
            root.mkdir(parents=True, exist_ok=True)
            if not root.is_relative_to(self.workspace):
                return
            path = root / "app_events" / f"{task.updated_at.astimezone():%Y-%m-%d}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            safe_message = _redact_application_message(task.message, task_id=task.task_id)
            payload = {
                "timestamp": task.updated_at.astimezone().isoformat(timespec="milliseconds"),
                "level": "ERROR" if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED} else "INFO",
                "task_id": redact_audit_text(task.task_id, redact_phone=False),
                "source": "task_journal",
                "message": f"{task.name}：{task.status.label}。{safe_message}".strip(),
                "session_id": self._session_id,
                "event_type": "task_snapshot",
                "task": {
                    "task_id": task.task_id,
                    "name": redact_audit_text(task.name, redact_phone=False),
                    "area": task.area.value,
                    "capability": task.capability.value,
                    "status": task.status.value,
                    "message": safe_message,
                    "order_no": redact_audit_text(task.order_no or "", redact_phone=False),
                    "progress_percent": task.progress_percent,
                    "created_at": task.created_at.isoformat(),
                    "updated_at": task.updated_at.isoformat(),
                },
            }
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._application_log_lock:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
        except (OSError, TypeError, ValueError):
            return

    @staticmethod
    def _parse_event_datetime(value: object) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value or ""))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    def _today_task_history(self) -> list[TaskRecord]:
        path = Path(self.log_directory()) / "app_events" / f"{datetime.now().astimezone():%Y-%m-%d}.jsonl"
        latest: dict[str, TaskRecord] = {}
        if path.is_file():
            try:
                with self._application_log_lock:
                    lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                lines = []
            for line in lines:
                try:
                    item = json.loads(line)
                    task = item.get("task") if item.get("event_type") == "task_snapshot" else None
                    if not isinstance(task, Mapping):
                        continue
                    task_id = str(task.get("task_id") or "").strip()
                    if not task_id:
                        continue
                    latest[task_id] = TaskRecord(
                        task_id=task_id,
                        name=str(task.get("name") or "后台任务"),
                        area=TaskArea(str(task.get("area") or TaskArea.MAINTENANCE.value)),
                        capability=Capability(str(task.get("capability") or Capability.LIST_ORDERS.value)),
                        status=TaskStatus(str(task.get("status") or TaskStatus.QUEUED.value)),
                        message=str(task.get("message") or ""),
                        order_no=str(task.get("order_no") or "") or None,
                        progress_percent=max(0, min(100, int(task.get("progress_percent") or 0))),
                        created_at=self._parse_event_datetime(task.get("created_at")),
                        updated_at=self._parse_event_datetime(task.get("updated_at")),
                    )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
        with self._lock:
            for task in self._state.tasks:
                latest[task.task_id] = task
        return sorted(latest.values(), key=lambda task: task.updated_at, reverse=True)

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
        normalized_level = str(level or "").strip().upper()
        needle = str(query or "").strip().casefold()
        indexed_rows: list[tuple[datetime, int, LogEntry]] = []
        event_ordinal = 0
        bad_lines: list[tuple[str, int]] = []
        root = Path(self.log_directory()) / "app_events"
        for path in sorted(root.glob("*.jsonl"), reverse=True):
            try:
                with self._application_log_lock:
                    lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                event_ordinal += 1
                try:
                    item = json.loads(line)
                    entry_level = LogLevel(str(item.get("level") or "INFO").upper())
                    entry = LogEntry(
                        level=entry_level,
                        source=str(item.get("source") or "application"),
                        message=str(item.get("message") or ""),
                        task_id=str(item.get("task_id") or "") or None,
                        created_at=self._parse_event_datetime(item.get("timestamp")),
                    )
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    marker = (str(path), line_number)
                    if marker not in self._diagnosed_bad_log_lines:
                        self._diagnosed_bad_log_lines.add(marker)
                        bad_lines.append(marker)
                    continue
                if normalized_level and entry.level.value != normalized_level:
                    continue
                if needle and needle not in f"{entry.task_id or ''} {entry.source} {entry.message}".casefold():
                    continue
                indexed_rows.append((entry.created_at, event_ordinal, entry))
        indexed_rows.sort(key=lambda value: (value[0], value[1]), reverse=True)
        rows = [value[2] for value in indexed_rows]
        total = len(rows)
        page_count = max(1, (total + normalized_size - 1) // normalized_size)
        normalized_page = min(normalized_page, page_count)
        start = (normalized_page - 1) * normalized_size
        if bad_lines:
            self._append_log(
                LogLevel.WARNING,
                "logging",
                f"读取日志时跳过 {len(bad_lines)} 条损坏记录。",
            )
        return LogPage(
            tuple(rows[start : start + normalized_size]),
            normalized_page,
            normalized_size,
            total,
        )

    def log_directory(self) -> str:
        root = self.workspace / "logs"
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
        return str(resolved if resolved.is_relative_to(self.workspace) else self.workspace)

    def delete_logs_older_than(self, days: int) -> ControlResult:
        """Delete only expired files beneath the guarded workspace log root."""

        retention_days = int(days)
        if retention_days not in {30, 90}:
            return ControlResult(False, "只允许清理 1 个月或 3 个月以前的日志。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return ControlResult(
                    False,
                    "后台仍有任务运行，为避免正在写入的日志被清理，请等待任务结束后再试。",
                )
            try:
                with self._application_log_lock:
                    report = cleanup_expired_logs(
                        self.log_directory(),
                        retention_days=retention_days,
                    )
            except (OSError, ValueError) as exc:
                return ControlResult(False, f"日志清理失败：{type(exc).__name__}。")

            size_mb = report.deleted_bytes / (1024 * 1024)
            message = (
                f"已删除 {retention_days // 30} 个月以前的日志 "
                f"{report.deleted_count} 个，释放 {size_mb:.1f} MB。"
            )
            if report.errors or report.skipped_paths:
                message += (
                    f" 安全跳过 {len(report.skipped_paths)} 项，"
                    f"处理失败 {len(report.errors)} 项；未强制删除。"
                )
            self._append_log(LogLevel.INFO, "logging", message)
            return ControlResult(
                True,
                message,
                details={
                    "retention_days": retention_days,
                    "deleted_count": report.deleted_count,
                    "deleted_bytes": report.deleted_bytes,
                    "skipped_count": len(report.skipped_paths),
                    "error_count": len(report.errors),
                },
            )

    def full_log_text(self, task_id: str | None = None) -> tuple[str, str]:
        """Return one safe audit document or recent concise application events."""

        normalized_task_id = str(task_id or "").strip()
        if normalized_task_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized_task_id):
            return "完整日志", "任务 ID 格式无效。"
        root = Path(self.log_directory())
        if normalized_task_id:
            escaped_task_id = re.escape(normalized_task_id)
            candidates_by_path: dict[str, Path] = {}
            for pattern in (
                f"api_scan/*/{normalized_task_id}*.json",
                f"custom_order_scan/*/*{normalized_task_id}*.json",
                f"shipment_scan/*/*{normalized_task_id}*.json",
            ):
                for candidate in root.glob(pattern):
                    candidates_by_path[str(candidate)] = candidate
            candidates = sorted(
                candidates_by_path.values(),
                key=lambda path: path.stat().st_mtime if path.is_file() else 0,
            )
            documents: list[tuple[Path, str]] = []
            for candidate in candidates:
                legacy_name = bool(
                    re.fullmatch(
                        rf"{escaped_task_id}(?:\.attempt-\d+)?\.json",
                        candidate.name,
                    )
                )
                current_name = bool(
                    re.fullmatch(
                        rf"(?:custom_order_scan|shipment_scan)_\d{{8}}_\d{{6}}_"
                        rf"{escaped_task_id}(?:\.attempt-\d+)?\.json",
                        candidate.name,
                    )
                )
                if not legacy_name and not current_name:
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

    async def request_interaction(
        self,
        *,
        task_id: str,
        stage: str,
        title: str,
        message: str,
        options: Sequence[Any] = (),
        approve_label: str = "确认执行",
        reject_label: str = "拒绝 / 停止",
    ) -> DesktopInteractionResponse:
        """Pause a worker until the Qt thread supplies one explicit decision."""

        request = DesktopInteractionRequest(
            request_id=uuid4().hex,
            task_id=str(task_id or "").strip(),
            stage=str(stage or "unknown").strip(),
            title=str(title or "需要用户确认").strip(),
            message=str(message or "").strip(),
            options=tuple(options),
            approve_label=str(approve_label or "确认执行"),
            reject_label=str(reject_label or "拒绝 / 停止"),
        )
        with self._lock:
            if self._closing_requested:
                self._append_log(
                    LogLevel.WARNING,
                    "interaction",
                    f"程序正在关闭，已拒绝新的阶段确认：{request.stage}",
                    task_id=request.task_id,
                )
                return DesktopInteractionResponse(request.request_id, False)
            match = self._find_task(request.task_id)
            if match is None:
                return DesktopInteractionResponse(request.request_id, False)
            self._pending_interactions[request.request_id] = request
            self.set_task_status(
                request.task_id,
                TaskStatus.WAITING_USER,
                message=f"等待用户确认：{request.title}",
            )
            self._append_log(
                LogLevel.INFO,
                "interaction",
                f"等待桌面确认：{request.stage} / {request.request_id}",
                task_id=request.task_id,
            )

        while True:
            with self._lock:
                response = self._interaction_responses.pop(request.request_id, None)
                task_match = self._find_task(request.task_id)
                if response is not None:
                    self._pending_interactions.pop(request.request_id, None)
                    if task_match is not None and not task_match[1].status.terminal:
                        self.set_task_status(
                            request.task_id,
                            TaskStatus.RUNNING,
                            message="已收到用户决定，继续执行。",
                        )
                    outcome = "approved" if response.accepted else "rejected"
                    selected = response.selected_value or "-"
                    self._append_log(
                        LogLevel.INFO if response.accepted else LogLevel.WARNING,
                        "interaction",
                        f"桌面确认已响应：{request.stage} / {outcome} / {selected} / "
                        f"{request.request_id}",
                        task_id=request.task_id,
                    )
                    return response
            await asyncio.sleep(0.1)

    def pending_interactions(self) -> tuple[DesktopInteractionRequest, ...]:
        with self._lock:
            return tuple(
                sorted(self._pending_interactions.values(), key=lambda item: item.created_at)
            )

    def respond_interaction(self, response: DesktopInteractionResponse) -> ControlResult:
        with self._lock:
            request = self._pending_interactions.get(response.request_id)
            if request is None:
                return ControlResult(False, "该确认请求已结束或不存在。")
            if response.selected_value is not None:
                allowed = {option.value for option in request.options}
                if response.selected_value not in allowed:
                    return ControlResult(False, "确认选项无效。", request.task_id)
            if response.accepted and request.options and response.selected_value is None:
                return ControlResult(False, "请先选择一个选项。", request.task_id)
            existing = self._interaction_responses.get(response.request_id)
            if existing is not None:
                if (
                    existing.accepted == response.accepted
                    and existing.selected_value == response.selected_value
                ):
                    return ControlResult(True, "该确认请求已经按相同结果响应。", request.task_id)
                return ControlResult(False, "该确认请求已经响应。", request.task_id)
            self._interaction_responses[response.request_id] = response
            return ControlResult(True, "已提交确认结果。", request.task_id)

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
                TaskStatus.CANCELLED,
                message=message,
                progress_percent=100,
            )
            self._append_log(LogLevel.WARNING, match[1].area.value, message)
            self._futures.pop(task_id, None)
            cancelled += 1
        return cancelled

    def _reject_pending_write_interactions_locked(self) -> int:
        """Resolve visible write prompts as rejected when emergency stop is raised."""

        rejected = 0
        for request_id, request in tuple(self._pending_interactions.items()):
            match = self._find_task(request.task_id)
            if match is None or not match[1].capability.is_write:
                continue
            if request_id in self._interaction_responses:
                continue
            self._interaction_responses[request_id] = DesktopInteractionResponse(
                request_id,
                False,
            )
            self._append_log(
                LogLevel.WARNING,
                match[1].area.value,
                f"ERP 写入急停已开启；等待中的阶段确认已自动拒绝：{request.stage}",
                task_id=request.task_id,
            )
            rejected += 1
        return rejected

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
        if status == "not_required":
            return "该订单已因买家申请取消标记为不需要处理；如确需重做，请先填写原因并从目标阶段重开。"
        if status == "cancelled":
            return "该订单已被人工取消；如确需恢复，请填写原因并从目标阶段重开。"
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
            if self._closing_requested:
                return ControlResult(False, "程序正在安全关闭，不再接受新任务。")
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
                        and task.status
                        in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_USER}
                    ),
                    None,
                )
                if duplicate is not None:
                    return ControlResult(False, "该订单已有等待或运行中的任务，不能重复排队。")
            if (
                command.area.value == "shipment"
                and command.capability is Capability.OUTBOUND_ORDER
            ):
                logistics_no = str(command.payload.get("logistics_no") or "").strip()
                duplicate = next(
                    (
                        task
                        for task in self._state.tasks
                        if task.area.value == "shipment"
                        and task.capability is Capability.OUTBOUND_ORDER
                        and str(task.payload.get("logistics_no") or "").strip()
                        == logistics_no
                        and task.status
                        in {
                            TaskStatus.QUEUED,
                            TaskStatus.RUNNING,
                            TaskStatus.WAITING_USER,
                        }
                    ),
                    None,
                )
                if logistics_no and duplicate is not None:
                    return ControlResult(
                        False,
                        "该物流单已有等待或运行中的标发任务，不能重复排队。",
                    )
            try:
                gate_message = self._custom_workflow_gate_message_locked(command)
            except Exception as exc:
                return ControlResult(False, f"读取订单工作流状态失败：{type(exc).__name__}。")
            if gate_message:
                return ControlResult(False, gate_message)

            result = super().submit_task(command)
            if not result.accepted or not result.task_id:
                return result
            created = self._find_task(result.task_id)
            if created is not None:
                self._write_task_snapshot(created[1])
            if confirmation is not None:
                source_label = (
                    "勾选执行按钮"
                    if confirmation.source == "qt_checked_action"
                    else "桌面确认弹窗"
                )
                self._append_log(
                    LogLevel.INFO,
                    "safety",
                    "已记录桌面写入授权："
                    f"{confirmation.action.value} / {confirmation.order_no} / "
                    f"{confirmation.confirmation_id} / 来源：{source_label}",
                )
            execution_command = replace(command, execution_id=result.task_id)
            self._futures[result.task_id] = self._executor.submit(
                self._execute_task,
                result.task_id,
                execution_command,
            )
            return ControlResult(True, f"任务“{command.name}”已进入后台队列。", result.task_id)

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        message: str = "",
        progress_percent: int | None = None,
    ) -> ControlResult:
        result = super().set_task_status(
            task_id,
            status,
            message=message,
            progress_percent=progress_percent,
        )
        if result.accepted:
            with self._lock:
                match = self._find_task(task_id)
                task = match[1] if match is not None else None
            if task is not None:
                self._write_task_snapshot(task)
        return result

    def _execute_task(self, task_id: str, command: TaskCommand) -> None:
        try:
            with self._lock:
                current = self._find_task(task_id)
                if current is None or current[1].status is not TaskStatus.QUEUED:
                    return
                if self._closing_requested:
                    message = "程序关闭，尚未开始执行的任务已自动取消。"
                    self.set_task_status(
                        task_id,
                        TaskStatus.CANCELLED,
                        message=message,
                        progress_percent=100,
                    )
                    self._append_log(
                        LogLevel.WARNING,
                        command.area.value,
                        message,
                        task_id=task_id,
                    )
                    return
                mode = self._state.policy.effective_mode_for(command.capability)
                local_json_refresh = (
                    str(command.payload.get("trigger") or "")
                    == NOTIFICATION_CONTACT_REFRESH_TRIGGER
                )
                gate_message = self._custom_workflow_gate_message_locked(command)
                cancelled_before_start = bool(
                    gate_message and "不需要处理" in gate_message
                )
                if mode is CapabilityMode.DISABLED and not local_json_refresh:
                    gate_message = f"“{command.capability.label}”已被急停或禁用；排队任务未执行。"
                    cancelled_before_start = True
                if gate_message:
                    self.set_task_status(
                        task_id,
                        TaskStatus.CANCELLED if cancelled_before_start else TaskStatus.BLOCKED,
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
            scheduled_trigger = str(command.payload.get("trigger") or "")
            if scheduled_trigger in {"five_minute_timer", "three_hour_timer"}:
                message = self._scheduled_scan_summary(
                    command,
                    payload,
                    failed=failed,
                    task_id=task_id,
                )
            blocked = bool(getattr(result, "blocked", False)) or str(
                payload.get("status") or ""
            ).lower() in {"blocked", "manual_review", "unknown"}
            cancelled = bool(getattr(result, "cancelled", False)) or str(
                payload.get("status") or ""
            ).lower() in {"cancelled", "paused"}
            if blocked and command.order_no:
                payload.setdefault("platform_order_no", command.order_no)
            with self._lock:
                self._apply_task_payload(payload)
            self.set_task_status(
                task_id,
                (
                    TaskStatus.CANCELLED
                    if cancelled
                    else TaskStatus.BLOCKED
                    if blocked
                    else TaskStatus.FAILED
                    if failed
                    else TaskStatus.SUCCEEDED
                ),
                message=message,
                progress_percent=100,
            )
            self._append_log(
                LogLevel.WARNING
                if blocked or cancelled
                else (LogLevel.ERROR if failed else LogLevel.INFO),
                command.area.value,
                message,
                task_id=task_id,
            )
            if payload.get("shared_prerequisite_error"):
                self._block_queued_tasks_for_shared_prerequisite(
                    failed_task_id=task_id,
                    capability=command.capability,
                    message=message,
                )
        except Exception as exc:
            message = f"后台任务失败：{type(exc).__name__}。请在日志中查看对应任务。"
            self.set_task_status(task_id, TaskStatus.FAILED, message=message, progress_percent=100)
            self._append_log(LogLevel.ERROR, command.area.value, message, task_id=task_id)
        finally:
            with self._lock:
                self._futures.pop(task_id, None)
                self._shutdown_cancel_requested.discard(task_id)
                self._refresh_persistent_rows()

    def _block_queued_tasks_for_shared_prerequisite(
        self,
        *,
        failed_task_id: str,
        capability: Capability,
        message: str,
    ) -> int:
        with self._lock:
            queued_task_ids = [
                task.task_id
                for task in self._state.tasks
                if task.task_id != failed_task_id
                and task.capability is capability
                and task.status is TaskStatus.QUEUED
            ]
        blocked_message = (
            f"未执行：同类任务的共享前置条件不可用。{message} "
            "修复前置条件后，请重新勾选订单提交。"
        )
        for queued_task_id in queued_task_ids:
            self.set_task_status(
                queued_task_id,
                TaskStatus.BLOCKED,
                message=blocked_message,
                progress_percent=100,
            )
            self._append_log(
                LogLevel.WARNING,
                "prerequisite",
                blocked_message,
                task_id=queued_task_id,
            )
        return len(queued_task_ids)

    @staticmethod
    def _scheduled_scan_summary(
        command: TaskCommand,
        payload: Mapping[str, Any],
        *,
        failed: bool,
        task_id: str,
    ) -> str:
        if failed:
            return f"后台扫描失败（任务 {task_id}）；请打开详细扫描日志检查。"
        if command.area is TaskArea.CUSTOMIZATION:
            return (
                "定制订单后台扫描完成："
                f"候选 {int(payload.get('candidate_count') or 0)}，"
                "买家取消转不需要 "
                f"{int(payload.get('buyer_cancel_reconciled_count') or 0)}，"
                "取消撤销待再次确认 "
                f"{int(payload.get('buyer_cancel_clear_observed_count') or 0)}，"
                "取消申请已撤销，订单已重新入队 "
                f"{int(payload.get('buyer_cancel_reactivated_count') or 0)}，"
                "消失候选文件夹对账：完成 "
                f"{int(payload.get('folder_reconciled_completed_count') or 0)}、"
                "待处理 "
                f"{int(payload.get('folder_reconciled_pending_count') or 0)}、"
                "保留报错 "
                f"{int(payload.get('folder_reconciliation_error_preserved_count') or 0)}。"
            )
        return (
            f"{'自动标发后台扫描部分完成：' if str(payload.get('status') or '') == 'completed_with_warnings' else '自动标发后台扫描完成：'}"
            f"候选 {int(payload.get('candidate_count') or 0)}，"
            f"新增队列 {int(payload.get('enqueued_count') or 0)}，"
            f"查询物流 {int(payload.get('logistics_query_count') or 0)}，"
            f"可标发 {int(payload.get('logistics_ready_count') or 0)}，"
            "需复核 "
            f"{int(payload.get('manual_review_count') or 0) + int(payload.get('logistics_blocked_count') or 0)}，"
            f"待重试 {int(payload.get('logistics_retryable_count') or 0)}。"
        )

    def _apply_task_payload(self, payload: Mapping[str, Any]) -> None:
        # A scan payload contains only this scan's candidates, not the complete
        # persistent workflow queue. Replacing the UI snapshot with that list
        # made the whole table disappear whenever a later scan found zero new
        # candidates. The SQLite store is the sole source for queue rows and is
        # refreshed after every task in _execute_task.finally.

        status = str(payload.get("status") or "").strip().lower()
        stage = str(
            payload.get("workflow_paused_stage")
            or payload.get("workflow_blocked_stage")
            or ""
        ).strip()
        platform_order_no = str(payload.get("platform_order_no") or "").strip()
        if (
            status in {"failed", "cancelled", "paused", "blocked", "manual_review", "unknown"}
            and stage
            and platform_order_no
            and not bool(payload.get("workflow_pause_recorded"))
        ):
            reason = str(payload.get("message") or "本阶段处理已暂停。").strip()
            try:
                store = self._get_custom_store()
                if store.get_workflow(platform_order_no) is None:
                    store.mutate_legacy_record(
                        platform_order_no,
                        lambda current: {**current, "workflow_status": "pending"},
                        event_type="desktop_processing_paused_initialized",
                        actor="desktop_worker",
                        reason=reason,
                    )
                if status in {"blocked", "manual_review", "unknown"}:
                    pause_kind = WorkflowPauseKind.AMBIGUOUS_WRITE
                elif status in {"cancelled", "paused"}:
                    pause_kind = WorkflowPauseKind.USER_CANCELLED
                else:
                    pause_kind = WorkflowPauseKind.RETRYABLE_FAILURE
                store.record_workflow_paused(
                    platform_order_no,
                    stage,
                    reason=reason,
                    actor="desktop_worker",
                    result_status=status,
                    pause_kind=pause_kind,
                )
            except Exception as exc:
                self._append_log(
                    LogLevel.ERROR,
                    "custom_state",
                    f"阶段暂停状态持久化失败：{type(exc).__name__}。",
                )

    def cancel_task(self, task_id: str) -> ControlResult:
        with self._lock:
            future = self._futures.get(task_id)
            if future is not None and future.running():
                return ControlResult(False, "任务已经开始运行，不能安全强制终止；请等待当前原子步骤完成。", task_id)
            if future is not None and not future.cancel():
                return ControlResult(False, "任务当前无法安全取消。", task_id)
        return super().cancel_task(task_id)

    def cancellation_requested(self, task_id: str) -> bool:
        with self._lock:
            return str(task_id or "") in self._shutdown_cancel_requested

    def _request_shipment_task_stops_locked(
        self,
        logistics_nos: Sequence[str],
    ) -> tuple[int, int]:
        """Stop matching desktop attempts without interrupting an atomic write."""

        wanted = {str(value or "").strip() for value in logistics_nos if str(value or "").strip()}
        queued_cancelled = 0
        cooperative_requested = 0
        for task in tuple(self._state.tasks):
            if (
                task.area is not TaskArea.SHIPMENT
                or task.capability is not Capability.OUTBOUND_ORDER
                or task.status.terminal
                or str(task.payload.get("logistics_no") or "").strip() not in wanted
            ):
                continue
            future = self._futures.get(task.task_id)
            if future is not None and not future.running() and future.cancel():
                InMemoryBackgroundTaskController.cancel_task(self, task.task_id)
                self._futures.pop(task.task_id, None)
                queued_cancelled += 1
                continue

            # A running write cannot be killed in the middle of an HTTP request.
            # The task runner checks this flag before every following write
            # boundary and stops after the current atomic step returns.
            self._shutdown_cancel_requested.add(task.task_id)
            for request_id, request in self._pending_interactions.items():
                if request.task_id == task.task_id:
                    self._interaction_responses.setdefault(
                        request_id,
                        DesktopInteractionResponse(request_id, False),
                    )
            cooperative_requested += 1
        return queued_cancelled, cooperative_requested

    def prepare_close(self) -> ControlResult:
        """Cancel safe work and report whether the window may exit now.

        Queued work is cancelled immediately.  Pending stage prompts are
        rejected.  Running read-only work receives a cooperative cancellation
        request, while an already-running confirmed write is allowed to finish
        its current safe workflow before the process exits.
        """

        cancelled = 0
        waiting: list[TaskRecord] = []
        with self._lock:
            self._closing_requested = True
            for task_id, future in list(self._futures.items()):
                match = self._find_task(task_id)
                task = match[1] if match is not None else None
                if future.done():
                    continue
                if not future.running() and future.cancel():
                    if task is not None and not task.status.terminal:
                        self.set_task_status(
                            task_id,
                            TaskStatus.CANCELLED,
                            message="程序关闭，尚未开始的任务已自动取消。",
                            progress_percent=100,
                        )
                        self._append_log(
                            LogLevel.WARNING,
                            task.area.value,
                            "程序关闭，尚未开始的任务已自动取消。",
                            task_id=task_id,
                        )
                    self._futures.pop(task_id, None)
                    cancelled += 1
                    continue
                if task is not None and task.status is TaskStatus.WAITING_USER:
                    for request_id, request in self._pending_interactions.items():
                        if request.task_id == task_id:
                            self._interaction_responses.setdefault(
                                request_id,
                                DesktopInteractionResponse(request_id, False),
                            )
                elif task is not None and not task.capability.is_write:
                    self._shutdown_cancel_requested.add(task_id)
                if task is not None:
                    waiting.append(task)

        if waiting:
            confirmed_writes = sum(
                1
                for task in waiting
                if task.capability.is_write and task.status is TaskStatus.RUNNING
            )
            cancelling = len(waiting) - confirmed_writes
            return ControlResult(
                False,
                f"已自动取消 {cancelled} 个尚未开始的任务；"
                f"正在结束 {cancelling} 个可取消任务，并等待 {confirmed_writes} 个已确认写入任务安全完成。",
            )
        return ControlResult(True, f"已自动取消 {cancelled} 个尚未开始的任务，可以安全关闭。")

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
        """Release the executor after ``prepare_close`` proves work is drained."""
        with self._lock:
            self._closing_requested = True
            for request_id, request in self._pending_interactions.items():
                self._interaction_responses.setdefault(
                    request_id,
                    DesktopInteractionResponse(request_id, False),
                )
        self._executor.shutdown(wait=True, cancel_futures=True)

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
                if capability is Capability.UPDATE_CONTACT and mode is not CapabilityMode.DISABLED:
                    mode = CapabilityMode.BROWSER
                elif (
                    capability is not Capability.ALIBABA_LOGISTICS
                    and mode is CapabilityMode.BROWSER
                ):
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
            if capability is Capability.UPDATE_CONTACT:
                if normalized_mode is not CapabilityMode.DISABLED:
                    normalized_mode = CapabilityMode.BROWSER
            elif (
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
                    self._reject_pending_write_interactions_locked()
                return ControlResult(False, f"写入急停状态保存失败：{type(exc).__name__}。")
            if enabled:
                cancelled = self._cancel_queued_write_tasks_locked()
                rejected = self._reject_pending_write_interactions_locked()
                if cancelled or rejected:
                    return ControlResult(
                        True,
                        f"{result.message} 已取消 {cancelled} 个尚未开始的写任务，"
                        f"拒绝 {rejected} 个等待中的写入确认。"
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
            if path.exists():
                self._custom_store.repair_automated_blocked_stages()
        return self._custom_store

    @staticmethod
    def _sqlite_state_signature(path: Path) -> tuple[Any, ...]:
        resolved = path.resolve()
        signature: list[Any] = [str(resolved)]
        for candidate in (resolved, Path(f"{resolved}-wal")):
            try:
                stat = candidate.stat()
            except FileNotFoundError:
                signature.extend((False, 0, 0))
            else:
                signature.extend((True, stat.st_size, stat.st_mtime_ns))
        return tuple(signature)

    def _refresh_persistent_rows(self, *, force: bool = False) -> None:
        try:
            custom_store = self._get_custom_store()
            custom_signature = self._sqlite_state_signature(custom_store.path)
            if force or custom_signature != self._custom_rows_signature:
                if custom_store.path.exists():
                    rows = custom_store.list_workflow_summaries(limit=2000)
                    self._state.custom_orders = [
                        CustomOrderRow(
                            platform_order_no=str(row.get("platform_order_no") or ""),
                            system_order_no=str(row.get("original_system_order_no") or ""),
                            product_type=str(row.get("product_type") or ""),
                            workflow_stage=str(row.get("workflow_status") or ""),
                            status_text="已忽略" if row.get("ignored") else str(row.get("workflow_status") or ""),
                            last_error=str(row.get("last_error") or ""),
                            result_detail=str(row.get("result_detail") or ""),
                            retry_confirmation_required=bool(
                                row.get("retry_confirmation_required")
                            ),
                            status_updated_at=str(row.get("updated_at") or ""),
                        )
                        for row in rows
                    ]
                else:
                    self._state.custom_orders = []
                self._custom_rows_signature = self._sqlite_state_signature(custom_store.path)
        except Exception as exc:
            self._append_log(LogLevel.ERROR, "custom_state", f"读取定制订单状态失败：{type(exc).__name__}。")

        shipment_path = self._shipment_state_path()
        try:
            shipment_signature = self._sqlite_state_signature(shipment_path)
            if force or shipment_signature != self._shipment_rows_signature:
                if shipment_path.is_file():
                    from shipment_automation.queue_store import ShipmentWorkflowStore

                    rows = ShipmentWorkflowStore(shipment_path).list_all_jobs(limit=2000)
                    self._state.shipments = [
                        ShipmentRow(
                            platform_order_no=str(row.get("platform_order_no") or ""),
                            system_order_no=str(row.get("system_order_no") or ""),
                            product_type=str(row.get("product_type") or ""),
                            logistics_no=str(row.get("logistics_no") or ""),
                            international_tracking_no=str(
                                row.get("international_tracking_no") or ""
                            ),
                            carrier=str(row.get("carrier") or ""),
                            alibaba_status=str(row.get("alibaba_status") or ""),
                            actual_total=str(row.get("actual_total") or ""),
                            chargeable_weight_kg=str(
                                row.get("chargeable_weight_kg") or ""
                            ),
                            identity_state=str(row.get("identity_state") or ""),
                            identity_status_text=str(row.get("identity_status_text") or ""),
                            logistics_state=str(row.get("logistics_state") or ""),
                            logistics_next_attempt_at=str(
                                row.get("logistics_next_attempt_at") or ""
                            ),
                            erp_state=str(row.get("erp_state") or ""),
                            erp_next_attempt_at=str(row.get("erp_next_attempt_at") or ""),
                            checkpoint=str(row.get("erp_checkpoint") or ""),
                            lease_owner=str(row.get("lease_owner") or ""),
                            lease_stage=str(row.get("lease_stage") or ""),
                            lease_until=str(row.get("lease_until") or ""),
                            last_error=str(row.get("last_error") or ""),
                            updated_at=str(row.get("updated_at") or ""),
                            outbounded_at=str(row.get("outbounded_at") or ""),
                            externally_completed_at=str(
                                row.get("externally_completed_at") or ""
                            ),
                            completion_source=str(row.get("completion_source") or ""),
                            erp_last_error=str(row.get("erp_last_error") or ""),
                            logistics_last_error=str(
                                row.get("logistics_last_error") or ""
                            ),
                            email_state=str(row.get("email_state") or ""),
                            email_last_error=str(row.get("email_last_error") or ""),
                            wms_selection_required=bool(
                                row.get("wms_selection_required")
                            ),
                        )
                        for row in rows
                    ]
                else:
                    self._state.shipments = []
                self._shipment_rows_signature = self._sqlite_state_signature(shipment_path)
        except Exception as exc:
            self._append_log(LogLevel.ERROR, "shipment_state", f"读取自动标发状态失败：{type(exc).__name__}。")

    def snapshot(self) -> DesktopSnapshot:
        with self._lock:
            self._refresh_persistent_rows()
            snapshot = super().snapshot()
        snapshot.today_tasks = self._today_task_history()
        return snapshot

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

    def _shipment_notification_context(self):
        from shipment_automation.notification_domain import NotificationConfiguration
        from shipment_automation.notification_store import ShipmentNotificationStore

        with self._lock:
            queue_path = self._state.settings.queue_path
            configuration_values = dict(self._configuration_values)
        store = ShipmentNotificationStore(_workspace_path(self.workspace, queue_path))
        configuration = NotificationConfiguration.from_mapping(configuration_values)
        return store, configuration

    def list_shipment_notifications(self) -> list[dict[str, Any]]:
        store, _configuration = self._shipment_notification_context()
        try:
            return store.list_notifications()
        except Exception as exc:
            self._append_log(
                LogLevel.ERROR,
                "shipment_notification",
                f"读取客户通知审核队列失败：{type(exc).__name__}。",
            )
            return []

    def refresh_shipment_notification_receipts(self) -> ControlResult:
        from shipment_automation.notification_service import ShipmentNotificationService

        store, configuration = self._shipment_notification_context()
        service = ShipmentNotificationService(
            store,
            configuration,
            timeout_seconds=self._state.settings.api_timeout_seconds,
        )

        async def run() -> dict[str, int]:
            try:
                return await service.refresh_pending_receipts()
            finally:
                await service.aclose()

        try:
            result = asyncio.run(run())
        except Exception as exc:
            message = f"发送状态刷新失败：{type(exc).__name__}。未发送任何邮件或短信。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message)
        checked = int(result.get("checked") or 0)
        completed = int(result.get("completed") or 0)
        retryable = int(result.get("retryable") or 0)
        status_check_failed = int(result.get("status_check_failed") or 0)
        errors = int(result.get("errors") or 0)
        message = (
            f"发送状态刷新完成：查询 {checked} 条，完成 {completed} 条，"
            f"供应商确认发送失败 {retryable} 条，状态仍未确认 {status_check_failed} 条，"
            f"查询请求失败 {errors} 条。未发送任何邮件或短信。"
        )
        level = LogLevel.WARNING if errors or status_check_failed else LogLevel.INFO
        self._append_log(level, "shipment_notification", message)
        return ControlResult(errors == 0, message, details=result)

    def test_notification_provider(self, provider: str) -> ControlResult:
        from shipment_automation.notification_providers import NotificationProviderError
        from shipment_automation.notification_service import ShipmentNotificationService

        store, configuration = self._shipment_notification_context()
        service = ShipmentNotificationService(
            store,
            configuration,
            timeout_seconds=self._state.settings.api_timeout_seconds,
        )

        async def run() -> bool:
            try:
                if provider.strip().lower() == "alimail":
                    return await service.test_alimail_connection()
                if provider.strip().lower() == "clicksend":
                    return await service.test_clicksend_connection()
                raise ValueError("Unknown notification provider")
            finally:
                await service.aclose()

        provider_key = provider.strip().lower()
        try:
            asyncio.run(run())
        except NotificationProviderError as exc:
            message = f"供应商连接测试失败：{exc} 凭证未回显。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message)
        except Exception as exc:
            message = f"供应商连接测试失败：{type(exc).__name__}。凭证未回显。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message)
        if provider_key == "alimail":
            message = (
                "阿里邮箱 Token 获取成功；未发送邮件，也未验证创建草稿和发送邮件权限。"
            )
        else:
            message = "ClickSend 账号连接测试成功；未发送短信，凭证未回显。"
        self._append_log(LogLevel.INFO, "shipment_notification", message)
        return ControlResult(True, message)

    def _send_shipment_notification(self, notification_id: int, *, retry: bool) -> ControlResult:
        from shipment_automation.notification_providers import NotificationProviderError
        from shipment_automation.notification_service import ShipmentNotificationService
        from shipment_automation.notification_store import StaleNotificationError

        store, configuration = self._shipment_notification_context()
        service = ShipmentNotificationService(
            store,
            configuration,
            timeout_seconds=self._state.settings.api_timeout_seconds,
        )

        async def run() -> dict[str, Any]:
            try:
                if retry:
                    return await service.retry_send_and_wait(notification_id)
                return await service.approve_send_and_wait(notification_id)
            finally:
                await service.aclose()

        try:
            result = asyncio.run(run())
        except StaleNotificationError:
            message = (
                "通知内容在审核后发生了实际变化，系统已生成新的待审核版本；"
                "本条未发送，请重新核对后再发送。"
            )
            self._append_log(LogLevel.WARNING, "shipment_notification", message)
            return ControlResult(
                False,
                message,
                details={"notification_id": notification_id},
            )
        except NotificationProviderError as exc:
            detail = str(exc)
            if "HTTP 403" in detail and "Alimail" in detail:
                guidance = (
                    "请检查阿里邮箱应用的创建草稿/发送草稿权限，以及发件账号的可操作范围；"
                    "修正后使用“重试已批准内容”。"
                )
            else:
                guidance = "请查看通知状态后处理。"
            message = f"客户通知发送未完成：{detail} {guidance}"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message, details={"notification_id": notification_id})
        except Exception as exc:
            message = f"客户通知发送未完成：{type(exc).__name__}。请查看通知状态后处理。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message, details={"notification_id": notification_id})
        state = str(result.get("state") or "")
        last_error = str(result.get("last_error") or "").strip()
        if state == "DELIVERED":
            message = "客户通知发送成功，供应商已确认送达。"
            accepted = True
            level = LogLevel.INFO
        elif state == "RETRYABLE":
            message = last_error or "客户通知发送失败，供应商已明确返回失败状态。"
            accepted = False
            level = LogLevel.ERROR
        elif state == "FAILED":
            message = last_error or "客户通知状态核验失败；这不等于发送失败。"
            accepted = False
            level = LogLevel.WARNING
        else:
            message = "供应商已接受客户通知，但最终送达状态尚未确认。"
            accepted = False
            level = LogLevel.WARNING
        self._append_log(level, "shipment_notification", message)
        return ControlResult(
            accepted,
            message,
            details={
                "notification_id": notification_id,
                "state": state,
                "channel": result.get("channel"),
                "provider_accepted": bool(result.get("provider_message_id")),
            },
        )

    def approve_shipment_notification(self, notification_id: int) -> ControlResult:
        return self._send_shipment_notification(notification_id, retry=False)

    def approve_shipment_notifications(
        self, notification_ids: Sequence[int]
    ) -> ControlResult:
        normalized_ids: list[int] = []
        for value in notification_ids:
            try:
                notification_id = int(value)
            except (TypeError, ValueError):
                continue
            if notification_id > 0 and notification_id not in normalized_ids:
                normalized_ids.append(notification_id)
        ids = tuple(normalized_ids)
        if not ids:
            return ControlResult(False, "请先勾选至少一条待审核通知。")

        store, _configuration = self._shipment_notification_context()
        latest = {int(item["id"]): item for item in store.list_notifications()}
        invalid = [
            notification_id
            for notification_id in ids
            if notification_id not in latest
            or latest[notification_id].get("state") != "AWAITING_REVIEW"
        ]
        if invalid:
            message = (
                "批量发送未开始：所有勾选记录必须是最新的待审核版本。"
                f"请取消或刷新 {len(invalid)} 条无效选择。"
            )
            self._append_log(LogLevel.WARNING, "shipment_notification", message)
            return ControlResult(False, message, details={"invalid_ids": invalid})

        results: list[dict[str, Any]] = []
        delivered_count = 0
        provider_accepted_count = 0
        for notification_id in ids:
            result = self._send_shipment_notification(notification_id, retry=False)
            current = store.get_notification(notification_id)
            item = {
                "notification_id": notification_id,
                "platform_order_no": str(
                    latest[notification_id].get("platform_order_no") or ""
                ),
                "accepted": bool(result.accepted),
                "provider_accepted": bool((current or {}).get("provider_message_id")),
                "message": result.message,
                "state": str((current or {}).get("state") or ""),
            }
            results.append(item)
            delivered_count += int(result.accepted)
            provider_accepted_count += int(item["provider_accepted"])

        failed_count = len(results) - delivered_count
        message = (
            f"批量审核发送已完成：确认送达 {delivered_count} 条，"
            f"供应商已接收 {provider_accepted_count} 条，未确认或失败 {failed_count} 条。"
        )
        self._append_log(
            LogLevel.WARNING if failed_count else LogLevel.INFO,
            "shipment_notification",
            message,
        )
        return ControlResult(
            True,
            message,
            details={
                "requested": len(ids),
                "accepted": delivered_count,
                "provider_accepted": provider_accepted_count,
                "failed": failed_count,
                "all_succeeded": failed_count == 0,
                "results": results,
            },
        )

    def retry_shipment_notification(self, notification_id: int) -> ControlResult:
        return self._send_shipment_notification(notification_id, retry=True)

    def reject_shipment_notification(self, notification_id: int) -> ControlResult:
        store, _configuration = self._shipment_notification_context()
        try:
            store.reject(notification_id)
        except Exception as exc:
            return ControlResult(False, f"驳回失败：{type(exc).__name__}。")
        return ControlResult(True, "通知已驳回，未发生外部发送。")

    def mark_shipment_notifications_manually_completed(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult:
        store, _configuration = self._shipment_notification_context()
        try:
            result = store.mark_manually_completed(
                notification_ids,
                actor="desktop_user",
                note=reason,
            )
        except Exception as exc:
            message = f"标记人工完成失败：{type(exc).__name__}。未修改任何通知。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message)
        count = int(result.get("completed") or 0)
        message = f"已将 {count} 条标发邮件通知设为人工完成；未调用邮件或短信接口。"
        self._append_log(LogLevel.INFO, "shipment_notification", message)
        return ControlResult(True, message, details=result)

    def cancel_shipment_notifications(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult:
        store, _configuration = self._shipment_notification_context()
        try:
            result = store.cancel_notifications(
                notification_ids,
                actor="desktop_user",
                note=reason,
            )
        except Exception as exc:
            message = f"取消客户通知失败：{type(exc).__name__}。未修改任何通知。"
            self._append_log(LogLevel.ERROR, "shipment_notification", message)
            return ControlResult(False, message)
        count = int(result.get("cancelled") or 0)
        message = f"已将 {count} 条客户通知设为已取消；后续扫描不会自动重新生成草稿。"
        self._append_log(LogLevel.INFO, "shipment_notification", message)
        return ControlResult(True, message, details=result)

    def resubmit_shipment_notification(
        self, notification_id: int, *, reason: str
    ) -> ControlResult:
        store, configuration = self._shipment_notification_context()
        if not reason.strip():
            return ControlResult(False, "重新提交审核的原因不能为空。")
        try:
            reopened = store.reopen_for_review(
                notification_id,
                configuration,
                actor="desktop_user",
                note=reason,
            )
        except Exception as exc:
            message = f"重新提交失败：{exc}"
            self._append_log(LogLevel.WARNING, "shipment_notification", message)
            return ControlResult(False, message)
        message = "已保留原通知历史并创建新的待审核版本。"
        self._append_log(LogLevel.INFO, "shipment_notification", message)
        return ControlResult(
            True,
            message,
            details={"notification_id": int(reopened["id"])},
        )

    def edit_shipment_notification_contact(
        self, notification_id: int, *, email: str, phone: str
    ) -> ControlResult:
        store, configuration = self._shipment_notification_context()
        notification = store.get_notification(notification_id)
        if notification is None:
            return ControlResult(False, "通知不存在。")
        try:
            updated = store.edit_contact_and_prepare(
                str(notification["platform_order_no"]),
                email=email,
                phone=phone,
                configuration=configuration,
            )
        except Exception as exc:
            return ControlResult(False, f"联系方式修改失败：{type(exc).__name__}。")
        return ControlResult(
            True,
            "联系方式已保存并重新计算渠道；必须重新审核后才能发送。",
            details={"notification_id": (updated or {}).get("id")},
        )

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
        return self.set_custom_stage_states(
            [platform_order_no],
            stage,
            state,
            reason=reason,
        )

    def set_custom_stage_states(
        self,
        platform_order_nos: Sequence[str],
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
                summary = self._get_custom_store().set_stage_states_for_workflows(
                    platform_order_nos,
                    stage,
                    WorkflowStageState(state),
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"批量状态修改失败：{type(exc).__name__}。")
            if not summary.changed_order_count:
                return ControlResult(
                    True,
                    f"所选 {summary.requested_count} 张订单的阶段已经是目标状态，未重复修改。",
                )
            message = f"已修改 {summary.changed_order_count} 张订单的阶段状态"
            if summary.unchanged_order_count:
                message += f"；跳过 {summary.unchanged_order_count} 张状态未变化的订单"
            return ControlResult(True, f"{message}。仅更新本地状态，未请求 ERP。")

    def complete_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                summary = self._get_custom_store().mark_workflows_manually_completed(
                    platform_order_nos,
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"批量标记完成失败：{type(exc).__name__}。")
            if not summary.completed_count:
                return ControlResult(
                    True,
                    f"所选 {summary.requested_count} 张订单已经是 completed，未重复修改。",
                )
            message = (
                f"已将 {summary.completed_count} 张订单标记为 completed，"
                f"共完成 {summary.changed_stage_count} 个阶段"
            )
            if summary.already_completed_count:
                message += f"；跳过 {summary.already_completed_count} 张已完成订单"
            return ControlResult(True, f"{message}。仅更新本地状态，未请求 ERP。")

    def cancel_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                summary = self._get_custom_store().mark_workflows_cancelled(
                    platform_order_nos,
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"批量取消定制订单失败：{type(exc).__name__}。")
        message = f"已将 {summary.changed_order_count} 张定制订单设为已取消"
        if summary.unchanged_order_count:
            message += f"；跳过 {summary.unchanged_order_count} 张已经取消的订单"
        return ControlResult(
            bool(summary.changed_order_count),
            f"{message}。阶段进度均已保留，仅修改本地队列，未请求 ERP。",
        )

    def reopen_custom_workflow(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        return self.reopen_custom_workflows(
            [platform_order_no],
            stage,
            reason=reason,
        )

    def reopen_custom_workflows(
        self,
        platform_order_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                summary = self._get_custom_store().reopen_workflows_from_stage(
                    platform_order_nos,
                    stage,
                    reason=reason,
                    actor="desktop_user",
                )
                self._refresh_persistent_rows()
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"批量重新打开工作流失败：{type(exc).__name__}。")
            if not summary.changed_order_count:
                return ControlResult(
                    True,
                    f"所选 {summary.requested_count} 张订单从该阶段起已处于待处理状态，未重复修改。",
                )
            message = (
                f"已从所选阶段重新打开 {summary.changed_order_count} 张订单，"
                f"共重置 {summary.changed_stage_count} 个阶段"
            )
            if summary.unchanged_order_count:
                message += f"；跳过 {summary.unchanged_order_count} 张无需修改的订单"
            return ControlResult(True, f"{message}。仅更新本地状态，未请求 ERP。")

    def retry_shipment_stage(
        self,
        logistics_no: str,
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        return self.retry_shipment_stages([logistics_no], stage, reason=reason)

    def retry_shipment_stages(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        if stage not in {"logistics", "erp", "email"}:
            return ControlResult(False, "未知自动标发阶段。")
        if not reason.strip():
            return ControlResult(False, "重试必须填写原因。")
        normalized = [
            value
            for value in dict.fromkeys(
                str(value or "").strip() for value in logistics_nos
            )
            if value
        ]
        if not normalized:
            return ControlResult(False, "请先勾选至少一条自动标发任务。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                store = ShipmentWorkflowStore(self._shipment_state_path())
                changed_logistics_nos: list[str] = []
                for logistics_no in normalized:
                    changed = (
                        store.retry_email_for_logistics_no(logistics_no, reason=reason)
                        if stage == "email"
                        else store.retry_stage(logistics_no, stage, reason=reason)
                    )
                    if changed:
                        changed_logistics_nos.append(logistics_no)
                self._refresh_persistent_rows(force=True)
            except Exception as exc:
                return ControlResult(False, f"自动标发阶段重试失败：{type(exc).__name__}。")
            unchanged = len(normalized) - len(changed_logistics_nos)
            message = f"已将 {len(changed_logistics_nos)} 条任务重新放回所选阶段"
            if unchanged:
                message += f"；{unchanged} 条当前状态不允许或无需改变"
            return ControlResult(
                bool(changed_logistics_nos),
                f"{message}。",
                details={
                    "changed_logistics_nos": tuple(changed_logistics_nos),
                    "unchanged_count": unchanged,
                },
            )

    def reopen_shipments_from_stage(
        self,
        logistics_nos: Sequence[str],
        stage: str,
        *,
        reason: str,
    ) -> ControlResult:
        if not reason.strip():
            return ControlResult(False, "重新打开自动标发阶段必须填写原因。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                summary = ShipmentWorkflowStore(
                    self._shipment_state_path()
                ).reopen_shipments_from_stage(logistics_nos, stage, reason=reason)
                self._refresh_persistent_rows(force=True)
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"重新打开自动标发阶段失败：{type(exc).__name__}。")
        message = f"已从所选阶段重新打开 {summary.changed_count} 条自动标发任务"
        if summary.skipped_count:
            message += f"；跳过 {summary.skipped_count} 条"
        return ControlResult(
            bool(summary.changed_count),
            f"{message}。仅修改本地检查点，未请求 ERP。",
            details={
                "changed_logistics_nos": summary.changed_logistics_nos,
                "skipped_reasons": dict(summary.skipped_reasons),
                "requested_count": summary.requested_count,
            },
        )

    def cancel_shipment(self, logistics_no: str, *, reason: str) -> ControlResult:
        return self.cancel_shipments([logistics_no], reason=reason)

    def cancel_shipments(
        self,
        logistics_nos: Sequence[str],
        *,
        reason: str,
    ) -> ControlResult:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in logistics_nos))
        normalized = [value for value in normalized if value]
        if not normalized:
            return ControlResult(False, "请先勾选至少一条自动标发任务。")
        if not reason.strip():
            return ControlResult(False, "暂停勾选订单必须填写原因。")
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            # Matching shipment tasks are themselves the work being stopped, so
            # they must not trip the generic maintenance gate.  Unrelated active
            # tasks remain protected by that gate.
            unrelated_active = any(
                not task.status.terminal
                and not (
                    task.area is TaskArea.SHIPMENT
                    and task.capability is Capability.OUTBOUND_ORDER
                    and str(task.payload.get("logistics_no") or "").strip() in normalized
                )
                for task in self._state.tasks
            )
            if blocked is not None and unrelated_active:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                store = ShipmentWorkflowStore(self._shipment_state_path())
                cancellable = []
                skipped_reasons: dict[str, str] = {}
                for logistics_no in normalized:
                    job = store.get_by_logistics_no(logistics_no)
                    if job is None:
                        skipped_reasons[logistics_no] = "任务不存在"
                    elif str(job.get("identity_state") or "") == "CANCELLED":
                        skipped_reasons[logistics_no] = "本轮处理已经暂停"
                    elif str(job.get("erp_state") or "") == "DONE":
                        skipped_reasons[logistics_no] = "ERP 标发已经完成，不需要暂停本轮处理"
                    else:
                        cancellable.append(logistics_no)
                changed = store.cancel_many(cancellable, reason) if cancellable else 0
                changed_logistics_nos = tuple(cancellable[:changed])
                queued_stopped, running_stops = self._request_shipment_task_stops_locked(
                    changed_logistics_nos
                )
                self._refresh_persistent_rows(force=True)
            except Exception as exc:
                return ControlResult(False, f"取消本轮自动标发处理失败：{type(exc).__name__}。")
        message = f"已暂停 {changed} 条勾选自动标发任务的本轮处理"
        if queued_stopped:
            message += f"；立即停止 {queued_stopped} 个尚未开始的后台任务"
        if running_stops:
            message += f"；{running_stops} 个运行中任务将在当前原子步骤返回后停止"
        if skipped_reasons:
            message += f"；跳过 {len(skipped_reasons)} 条"
        return ControlResult(
            bool(changed),
            f"{message}。下次完整扫描再次发现后会自动恢复。",
            details={
                "changed_logistics_nos": changed_logistics_nos,
                "skipped_reasons": skipped_reasons,
                "queued_task_stop_count": queued_stopped,
                "running_task_stop_count": running_stops,
            },
        )

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
        return self.change_shipment_statuses([logistics_no], action, reason=reason)

    def change_shipment_statuses(
        self,
        logistics_nos: Sequence[str],
        action: str,
        *,
        reason: str,
    ) -> ControlResult:
        """Apply one guarded local transition to checked shipment rows."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            return ControlResult(False, "修改队列状态必须填写原因。")
        normalized = [
            value
            for value in dict.fromkeys(
                str(value or "").strip() for value in logistics_nos
            )
            if value
        ]
        if not normalized:
            return ControlResult(False, "请先勾选至少一条自动标发任务。")
        supported = {
            "retry_logistics",
            "retry_erp",
            "cancel",
            "restore_cancelled",
            "manual_cancel",
            "restore_manual_cancelled",
            "mark_manual_done",
            "undo_manual_done",
            "manual_review",
        }
        if action not in supported:
            return ControlResult(False, "未知的队列状态操作。")
        if action == "cancel":
            return self.cancel_shipments(normalized, reason=audit_reason)
        with self._lock:
            blocked = self._maintenance_blocked_result_locked()
            if blocked is not None:
                return blocked
            try:
                from shipment_automation.queue_store import ShipmentWorkflowStore

                store = ShipmentWorkflowStore(self._shipment_state_path())
                if action == "manual_review":
                    summary = store.move_completed_to_manual_review_many(
                        normalized,
                        reason=audit_reason,
                    )
                    changed_logistics_nos = list(summary.changed_logistics_nos)
                    skipped_reasons = dict(summary.skipped_reasons)
                elif action == "manual_cancel":
                    summary = store.mark_manually_cancelled_many(
                        normalized,
                        reason=audit_reason,
                    )
                    changed_logistics_nos = list(summary.changed_logistics_nos)
                    skipped_reasons = dict(summary.skipped_reasons)
                elif action == "restore_manual_cancelled":
                    summary = store.restore_manually_cancelled_many(
                        normalized,
                        reason=audit_reason,
                    )
                    changed_logistics_nos = list(summary.changed_logistics_nos)
                    skipped_reasons = dict(summary.skipped_reasons)
                else:
                    changed_logistics_nos = []
                    skipped_reasons: dict[str, str] = {}
                    for logistics_no in normalized:
                        if action == "retry_logistics":
                            changed = store.retry_stage(
                                logistics_no, "logistics", reason=audit_reason
                            )
                        elif action == "retry_erp":
                            changed = store.retry_stage(
                                logistics_no, "erp", reason=audit_reason
                            )
                        elif action == "restore_cancelled":
                            changed = store.restore_cancelled(
                                logistics_no, reason=audit_reason
                            )
                        elif action == "mark_manual_done":
                            changed = store.mark_manually_completed(
                                logistics_no, reason=audit_reason
                            )
                        else:
                            changed = store.undo_manual_completion(
                                logistics_no, reason=audit_reason
                            )
                        if changed:
                            changed_logistics_nos.append(logistics_no)
                        else:
                            skipped_reasons[logistics_no] = "当前状态不允许或无需改变"
                self._refresh_persistent_rows(force=True)
            except ValueError as exc:
                return ControlResult(False, str(exc))
            except Exception as exc:
                return ControlResult(False, f"修改自动标发队列状态失败：{type(exc).__name__}。")
            if not changed_logistics_nos:
                return ControlResult(False, "当前状态不允许执行该操作，队列未改变。")
            message = f"已修改 {len(changed_logistics_nos)} 条队列状态"
            if skipped_reasons:
                message += f"；跳过 {len(skipped_reasons)} 条"
            message += "，原因和前后状态已写入事件历史。"
            self._append_log(LogLevel.INFO, "shipment_state", message)
            return ControlResult(
                True,
                message,
                details={
                    "changed_logistics_nos": tuple(changed_logistics_nos),
                    "skipped_reasons": skipped_reasons,
                },
            )


__all__ = ["PersistentBackgroundTaskController"]
