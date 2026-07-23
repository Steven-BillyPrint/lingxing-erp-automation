"""Authoritative controller service with instance leases and change revisions."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from erp_automation.ui.controller import BackgroundTaskController, ControlResult
from erp_automation.ui.models import TaskCommand

from .codec import (
    decode_capability,
    decode_capability_mode,
    decode_interaction_response,
    decode_settings,
    decode_task_command,
    redact_snapshot_settings,
    SENSITIVE_SETTINGS_FIELDS,
    to_jsonable,
)
from .store import CoordinationStore


READ_METHODS = frozenset(
    {
        "pending_interactions",
        "list_shipment_notifications",
        "full_log_text",
        "log_directory",
        "list_log_entries",
    }
)

MUTATION_METHODS = frozenset(
    {
        "submit_task",
        "cancel_task",
        "cancel_tasks",
        "retry_task",
        "respond_interaction",
        "update_capability_mode",
        "set_emergency_stop_writes",
        "save_settings",
        "test_notification_provider",
        "refresh_shipment_notification_receipts",
        "approve_shipment_notification",
        "approve_shipment_notifications",
        "retry_shipment_notification",
        "reject_shipment_notification",
        "mark_shipment_notifications_manually_completed",
        "cancel_shipment_notifications",
        "resubmit_shipment_notification",
        "edit_shipment_notification_contact",
        "run_migrations",
        "export_portable_migration",
        "import_portable_migration",
        "import_legacy_env",
        "set_custom_stage_state",
        "set_custom_stage_states",
        "complete_custom_workflows",
        "cancel_custom_workflows",
        "reopen_custom_workflow",
        "reopen_custom_workflows",
        "retry_shipment_stage",
        "retry_shipment_stages",
        "reopen_shipments_from_stage",
        "cancel_shipment",
        "cancel_shipments",
        "add_shipment_order",
        "change_shipment_status",
        "change_shipment_statuses",
        "delete_logs_older_than",
    }
)

RPC_METHODS = READ_METHODS | MUTATION_METHODS


@dataclass(frozen=True)
class CoordinationSettings:
    instance_ttl_seconds: float = 45.0
    transient_lease_seconds: float = 30.0
    task_lease_seconds: float = 90.0
    monitor_interval_seconds: float = 0.5


def _text(value: Any) -> str:
    return str(value or "").strip()


def _many(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(_text(item) for item in value if _text(item))


def _resource_keys(method: str, args: list[Any], kwargs: dict[str, Any]) -> tuple[str, ...]:
    """Return stable conflict scopes for every mutating controller operation."""

    if method == "submit_task" and args and isinstance(args[0], TaskCommand):
        command = args[0]
        order = _text(command.order_no)
        subject = order or _text(command.payload.get("logistics_no"))
        subject = subject or command.capability.value
        return (
            f"operation:{command.area.value}:{subject}",
            f"capability:{command.capability.value}",
        )
    if method in {"update_capability_mode", "set_emergency_stop_writes"}:
        return ("configuration:policy",)
    if method == "save_settings":
        return ("configuration:settings",)
    if method in {
        "run_migrations",
        "export_portable_migration",
        "import_portable_migration",
        "import_legacy_env",
    }:
        return ("maintenance:migration", "configuration:settings", "state:all")
    if method in {"cancel_task", "retry_task"}:
        return (f"task:{_text(args[0]) if args else 'unknown'}",)
    if method == "cancel_tasks":
        return tuple(f"task:{item}" for item in _many(args[0] if args else ()))
    if method == "respond_interaction":
        response = args[0] if args else None
        return (f"interaction:{_text(getattr(response, 'request_id', 'unknown'))}",)
    if method == "test_notification_provider":
        return (f"notification-provider:{_text(args[0]) if args else 'unknown'}",)
    if method == "refresh_shipment_notification_receipts":
        return ("notifications:receipts",)
    if method in {
        "approve_shipment_notification",
        "retry_shipment_notification",
        "reject_shipment_notification",
        "resubmit_shipment_notification",
        "edit_shipment_notification_contact",
    }:
        return (f"notification:{_text(args[0]) if args else 'unknown'}",)
    if method in {
        "approve_shipment_notifications",
        "mark_shipment_notifications_manually_completed",
        "cancel_shipment_notifications",
    }:
        return tuple(
            f"notification:{item}" for item in _many(args[0] if args else ())
        ) or ("notifications:batch",)
    if method in {
        "set_custom_stage_state",
        "reopen_custom_workflow",
    }:
        return (f"custom-order:{_text(args[0]) if args else 'unknown'}",)
    if method in {
        "set_custom_stage_states",
        "complete_custom_workflows",
        "cancel_custom_workflows",
        "reopen_custom_workflows",
    }:
        return tuple(
            f"custom-order:{item}" for item in _many(args[0] if args else ())
        ) or ("custom-orders:batch",)
    if method in {
        "retry_shipment_stage",
        "cancel_shipment",
        "change_shipment_status",
    }:
        return (f"shipment:{_text(args[0]) if args else 'unknown'}",)
    if method in {
        "retry_shipment_stages",
        "reopen_shipments_from_stage",
        "cancel_shipments",
        "change_shipment_statuses",
    }:
        return tuple(
            f"shipment:{item}" for item in _many(args[0] if args else ())
        ) or ("shipments:batch",)
    if method == "add_shipment_order":
        logistics_no = _text(kwargs.get("logistics_no"))
        platform_order_no = _text(kwargs.get("platform_order_no"))
        return (f"shipment:{logistics_no or platform_order_no or 'new'}",)
    if method == "delete_logs_older_than":
        return ("maintenance:logs",)
    return (f"controller:{method}",)


def _decode_call(
    method: str,
    raw_args: Any,
    raw_kwargs: Any,
) -> tuple[list[Any], dict[str, Any]]:
    if not isinstance(raw_args, list):
        raise ValueError("RPC args must be a JSON array.")
    if not isinstance(raw_kwargs, Mapping):
        raise ValueError("RPC kwargs must be a JSON object.")
    args = list(raw_args)
    kwargs = dict(raw_kwargs)
    if method == "submit_task":
        if len(args) != 1:
            raise ValueError("submit_task expects one TaskCommand.")
        args[0] = decode_task_command(args[0])
    elif method == "respond_interaction":
        if len(args) != 1:
            raise ValueError("respond_interaction expects one response.")
        args[0] = decode_interaction_response(args[0])
    elif method == "update_capability_mode":
        if len(args) != 2:
            raise ValueError("update_capability_mode expects capability and mode.")
        args[0] = decode_capability(args[0])
        args[1] = decode_capability_mode(args[1])
    elif method == "save_settings":
        if len(args) != 1:
            raise ValueError("save_settings expects one settings document.")
        args[0] = decode_settings(args[0])
    return args, kwargs


def _result_type(value: Any) -> str:
    if isinstance(value, ControlResult):
        return "control_result"
    if method_result_is_log_page(value):
        return "log_page"
    if isinstance(value, tuple) and all(
        hasattr(item, "request_id") and hasattr(item, "task_id") for item in value
    ):
        return "interactions"
    return "json"


def method_result_is_log_page(value: Any) -> bool:
    return all(hasattr(value, name) for name in ("items", "page", "page_size", "total"))


class CoordinatedControllerService:
    """Expose one real controller safely to any number of desktop windows."""

    def __init__(
        self,
        controller: BackgroundTaskController,
        store: CoordinationStore,
        *,
        settings: CoordinationSettings | None = None,
    ) -> None:
        self.controller = controller
        self.store = store
        self.settings = settings or CoordinationSettings()
        self._call_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._last_snapshot_fingerprint = ""
        self._tracked_tasks: set[str] = set()
        self._closed = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="erp-coordination-monitor",
            daemon=True,
        )
        self.store.publish_event(
            instance_id="server",
            operation="server_started",
            summary="Authoritative controller started.",
        )
        self._monitor.start()

    def close(self) -> None:
        self._closed.set()
        self._monitor.join(timeout=5)

    def register(self, instance_id: str, display_name: str) -> dict[str, Any]:
        self.store.register_instance(
            instance_id,
            display_name,
            ttl_seconds=self.settings.instance_ttl_seconds,
        )
        return {
            "instance_id": instance_id,
            "revision": self.store.current_revision(),
            "heartbeat_interval_seconds": max(
                5.0, self.settings.instance_ttl_seconds / 3
            ),
        }

    def heartbeat(self, instance_id: str) -> dict[str, Any]:
        try:
            self.store.heartbeat(
                instance_id, ttl_seconds=self.settings.instance_ttl_seconds
            )
        except KeyError:
            self.store.register_instance(
                instance_id,
                instance_id,
                ttl_seconds=self.settings.instance_ttl_seconds,
            )
        return {"revision": self.store.current_revision()}

    def deregister(self, instance_id: str) -> None:
        self.store.deregister(instance_id)

    def snapshot_payload(self, instance_id: str) -> dict[str, Any]:
        self.heartbeat(instance_id)
        snapshot = redact_snapshot_settings(self.controller.snapshot())
        return {
            "revision": self.store.current_revision(),
            "snapshot": to_jsonable(snapshot),
            "leases": self.store.active_leases(),
        }

    def invoke(
        self,
        *,
        instance_id: str,
        request_id: str,
        method: str,
        raw_args: Any,
        raw_kwargs: Any,
    ) -> dict[str, Any]:
        if method not in RPC_METHODS:
            raise ValueError("RPC method is not allowed.")
        cached = self.store.cached_response(request_id)
        if cached is not None:
            return cached
        self.heartbeat(instance_id)
        args, kwargs = _decode_call(method, raw_args, raw_kwargs)
        if method == "save_settings":
            submitted = args[0]
            current = self.controller.snapshot().settings
            args[0] = replace(
                submitted,
                **{
                    name: getattr(current, name)
                    for name in SENSITIVE_SETTINGS_FIELDS
                    if not str(getattr(submitted, name) or "")
                },
            )
        if method in READ_METHODS:
            value = getattr(self.controller, method)(*args, **kwargs)
            response = {
                "result_type": _result_type(value),
                "result": to_jsonable(value),
                "revision": self.store.current_revision(),
            }
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
                response=response,
            )
            return response

        resources = _resource_keys(method, args, kwargs)
        conflict = self.store.acquire(
            resources=resources,
            instance_id=instance_id,
            request_id=request_id,
            operation=method,
            ttl_seconds=self.settings.transient_lease_seconds,
        )
        if conflict is not None:
            result = ControlResult(
                False,
                (
                    f"操作已被“{conflict.owner_display_name}”占用："
                    f"{conflict.resource}（{conflict.operation}）。"
                    "请等待该操作完成，其他窗口会自动刷新。"
                ),
                details={
                    "conflict": True,
                    "resource": conflict.resource,
                    "owner_instance_id": conflict.owner_instance_id,
                    "owner_display_name": conflict.owner_display_name,
                    "operation": conflict.operation,
                    "expires_at": conflict.expires_at,
                },
            )
            response = {
                "result_type": "control_result",
                "result": to_jsonable(result),
                "revision": self.store.current_revision(),
            }
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
                response=response,
            )
            return response

        keep_task_lease = False
        try:
            with self._call_lock:
                value = getattr(self.controller, method)(*args, **kwargs)
            if (
                method == "submit_task"
                and isinstance(value, ControlResult)
                and value.accepted
                and value.task_id
            ):
                self.store.bind_task(
                    request_id,
                    value.task_id,
                    ttl_seconds=self.settings.task_lease_seconds,
                )
                self._tracked_tasks.add(value.task_id)
                keep_task_lease = True
            if not isinstance(value, ControlResult) or value.accepted:
                revision = self.store.publish_event(
                    instance_id=instance_id,
                    operation=method,
                    resources=resources,
                    summary=getattr(value, "message", ""),
                )
            else:
                revision = self.store.current_revision()
            response = {
                "result_type": _result_type(value),
                "result": to_jsonable(value),
                "revision": revision,
            }
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
                response=response,
            )
            return response
        finally:
            if not keep_task_lease:
                self.store.release_request(request_id)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _monitor_loop(self) -> None:
        while not self._closed.wait(self.settings.monitor_interval_seconds):
            try:
                snapshot = self.controller.snapshot()
                tasks = {task.task_id: task for task in snapshot.tasks}
                for task_id in tuple(self._tracked_tasks):
                    task = tasks.get(task_id)
                    if task is None or task.status.terminal:
                        self.store.release_task(task_id)
                        self._tracked_tasks.discard(task_id)
                    else:
                        self.store.renew_task(
                            task_id,
                            ttl_seconds=self.settings.task_lease_seconds,
                        )
                fingerprint = self._fingerprint(snapshot)
                with self._snapshot_lock:
                    if (
                        self._last_snapshot_fingerprint
                        and fingerprint != self._last_snapshot_fingerprint
                    ):
                        self.store.publish_event(
                            instance_id="server",
                            operation="background_state_changed",
                            summary="Task or shared state changed.",
                        )
                    self._last_snapshot_fingerprint = fingerprint
                self.store.cleanup_expired()
            except Exception:
                # Coordination monitoring must never terminate the server.  The
                # next iteration retries and API calls still use the controller.
                continue
