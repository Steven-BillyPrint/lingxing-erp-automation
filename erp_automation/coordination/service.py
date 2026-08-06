"""Authoritative controller service with instance leases and change revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import socket
import threading
import time
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from erp_automation.ui.controller import BackgroundTaskController, ControlResult
from erp_automation.ui.models import (
    Capability,
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    DESKTOP_INSTANCE_ID_PAYLOAD_KEY,
    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY,
    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY,
    DesktopInteractionResponse,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    TaskArea,
    TaskCommand,
    TaskStatus,
    task_requires_visible_browser,
)

from .access import OperatorIdentity
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


MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES = 4 * 1024 * 1024

SCHEDULED_SCAN_INTERVALS = {
    "five_minute_timer": 5 * 60.0,
    "three_hour_timer": 3 * 60 * 60.0,
}

_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY = "local_visible_logistics_followup"
_NOTIFICATION_COMPENSATION_FOLLOWUP_KIND = "notification_compensation"
_SERVER_FOLLOWUP_INSTANCE_ID = "server-persistent-followups"


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
        "confirm_shipment_tracking_pair",
        "delete_logs_older_than",
    }
)

RPC_METHODS = READ_METHODS | MUTATION_METHODS
ROLLING_UPDATE_DRAIN_RPC_METHODS = READ_METHODS | frozenset(
    {
        "cancel_task",
        "cancel_tasks",
        "respond_interaction",
        "set_emergency_stop_writes",
    }
)


@dataclass(frozen=True)
class CoordinationSettings:
    instance_ttl_seconds: float = 45.0
    transient_lease_seconds: float = 30.0
    task_lease_seconds: float = 90.0
    monitor_interval_seconds: float = 0.5
    receipt_monitor_interval_seconds: float = 15.0
    scheduler_lease_seconds: float = 15.0
    followup_retry_initial_seconds: float = 2.0
    followup_retry_max_seconds: float = 300.0
    followup_claim_seconds: float = 30.0
    followup_max_error_attempts: int = 8
    browser_port_start: int = 24000
    browser_port_end: int = 24999


class ClientUpdateRequiredError(ValueError):
    """Raised when a desktop client is not the server-required release."""

    def __init__(self, required_version: str) -> None:
        self.required_version = required_version
        super().__init__(
            f"Client update required. Required version: {required_version}."
        )


class InstanceRegistrationExpiredError(ValueError):
    """Raised before an operation when a desktop heartbeat lease expired."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _many(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    return tuple(_text(item) for item in value if _text(item))


def _notification_id_strings(value: Any) -> tuple[str, ...]:
    normalized: set[int] = set()
    for item in _many(value):
        try:
            notification_id = int(item)
        except (TypeError, ValueError):
            continue
        if notification_id > 0:
            normalized.add(notification_id)
    return tuple(str(item) for item in sorted(normalized))


def _requires_persistent_notification_followup(command: TaskCommand) -> bool:
    return bool(
        command.area is TaskArea.SHIPMENT
        and command.capability is Capability.LIST_ORDERS
        and command.payload.get(_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY)
    )


def _resource_keys(method: str, args: list[Any], kwargs: dict[str, Any]) -> tuple[str, ...]:
    """Return stable conflict scopes for every mutating controller operation."""

    if method == "submit_task" and args and isinstance(args[0], TaskCommand):
        command = args[0]
        trigger = _text(command.payload.get("trigger"))
        if trigger in {
            SHIPMENT_NOTIFICATION_SEND_TRIGGER,
            NOTIFICATION_CONTACT_REFRESH_TRIGGER,
        }:
            notification_ids = _notification_id_strings(
                command.payload.get("notification_ids")
            )
            if notification_ids:
                return tuple(
                    f"notification:{notification_id}"
                    for notification_id in notification_ids
                )
        order = _text(command.order_no)
        if order:
            return (f"order:{order}",)
        logistics = _text(command.payload.get("logistics_no"))
        if logistics:
            return (f"logistics:{logistics}",)
        return (f"capability:{command.capability.value}",)
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
        "confirm_shipment_tracking_pair",
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


_SINGLE_CUSTOM_ORDER_METHODS = frozenset(
    {
        "set_custom_stage_state",
        "reopen_custom_workflow",
    }
)
_BATCH_CUSTOM_ORDER_METHODS = frozenset(
    {
        "set_custom_stage_states",
        "complete_custom_workflows",
        "cancel_custom_workflows",
        "reopen_custom_workflows",
    }
)
_SINGLE_SHIPMENT_METHODS = frozenset(
    {
        "retry_shipment_stage",
        "cancel_shipment",
        "change_shipment_status",
        "confirm_shipment_tracking_pair",
    }
)
_BATCH_SHIPMENT_METHODS = frozenset(
    {
        "retry_shipment_stages",
        "reopen_shipments_from_stage",
        "cancel_shipments",
        "change_shipment_statuses",
    }
)
_SINGLE_NOTIFICATION_METHODS = frozenset(
    {
        "approve_shipment_notification",
        "retry_shipment_notification",
        "reject_shipment_notification",
        "resubmit_shipment_notification",
        "edit_shipment_notification_contact",
    }
)
_BATCH_NOTIFICATION_METHODS = frozenset(
    {
        "approve_shipment_notifications",
        "mark_shipment_notifications_manually_completed",
        "cancel_shipment_notifications",
    }
)


def _order_resource_keys(
    controller: BackgroundTaskController,
    method: str,
    args: list[Any],
    kwargs: dict[str, Any],
) -> tuple[str, ...]:
    """Resolve every order touched by a direct mutation.

    Background tasks already carry ``TaskCommand.order_no``. Direct queue,
    review and status actions often identify the same business order by a task,
    notification or logistics record instead. Resolving those aliases back to
    the platform order makes the lease global across every client and feature.
    """

    order_numbers: set[str] = set()

    def add_order(value: object) -> None:
        normalized = _text(value)
        if normalized:
            order_numbers.add(normalized)

    if method == "submit_task" and args and isinstance(args[0], TaskCommand):
        add_order(args[0].order_no)
    elif method in _SINGLE_CUSTOM_ORDER_METHODS:
        add_order(args[0] if args else "")
    elif method in _BATCH_CUSTOM_ORDER_METHODS:
        for value in _many(args[0] if args else ()):
            add_order(value)
    elif method == "add_shipment_order":
        add_order(kwargs.get("platform_order_no"))
    elif method in _SINGLE_SHIPMENT_METHODS or method in _BATCH_SHIPMENT_METHODS:
        logistics_numbers = (
            {_text(args[0] if args else "")}
            if method in _SINGLE_SHIPMENT_METHODS
            else set(_many(args[0] if args else ()))
        )
        logistics_numbers.discard("")
        if logistics_numbers:
            for row in controller.snapshot().shipments:
                if _text(row.logistics_no) in logistics_numbers:
                    add_order(row.platform_order_no)
    elif method in _SINGLE_NOTIFICATION_METHODS or method in _BATCH_NOTIFICATION_METHODS:
        notification_ids: set[str] = set()
        if method in _SINGLE_NOTIFICATION_METHODS:
            notification_ids.add(_text(args[0] if args else ""))
        else:
            notification_ids.update(_many(args[0] if args else ()))
        notification_ids.discard("")
        if notification_ids:
            for item in controller.list_shipment_notifications():
                if _text(item.get("id")) in notification_ids:
                    add_order(item.get("platform_order_no"))

    return tuple(f"order:{value}" for value in sorted(order_numbers))


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
        controller: BackgroundTaskController | None,
        store: CoordinationStore,
        *,
        settings: CoordinationSettings | None = None,
        required_client_version: str = "",
        rollout_previous_client_version: str = "",
        client_rollout_grace_seconds: float = 0.0,
        client_rollout_grace_deadline_epoch: float = 0.0,
        client_rollout_pending_activation: bool = False,
        controller_factory: (
            Callable[[OperatorIdentity], BackgroundTaskController] | None
        ) = None,
    ) -> None:
        if controller is None and controller_factory is None:
            raise ValueError("A controller or operator controller factory is required.")
        if controller is not None and controller_factory is not None:
            raise ValueError(
                "Configure a shared controller or an operator controller factory, not both."
            )
        self.controller = controller
        self._controller_factory = controller_factory
        self._operator_controllers: dict[str, BackgroundTaskController] = {}
        self._controller_lock = threading.RLock()
        self._global_capability_modes: dict[Any, Any] = {}
        self._global_emergency_stop: bool | None = None
        self.store = store
        self.settings = settings or CoordinationSettings()
        self.required_client_version = str(required_client_version or "").strip()
        if (
            self.required_client_version
            and not re.fullmatch(
                r"\d{4}\.\d{2}\.\d{2}\.\d+",
                self.required_client_version,
            )
        ):
            raise ValueError("Required client version is invalid.")
        self.rollout_previous_client_version = str(
            rollout_previous_client_version or ""
        ).strip()
        if self.rollout_previous_client_version:
            if (
                not self.required_client_version
                or not re.fullmatch(
                    r"\d{4}\.\d{2}\.\d{2}\.\d+",
                    self.rollout_previous_client_version,
                )
                or tuple(
                    map(int, self.rollout_previous_client_version.split("."))
                )
                >= tuple(map(int, self.required_client_version.split(".")))
            ):
                raise ValueError(
                    "Rollout previous client version must be valid and older "
                    "than the required client version."
                )
        rollout_grace_seconds = float(client_rollout_grace_seconds)
        if (
            not math.isfinite(rollout_grace_seconds)
            or rollout_grace_seconds < 0
            or rollout_grace_seconds > 86_400
        ):
            raise ValueError(
                "Client rollout grace period must be between 0 and 86400 seconds."
            )
        rollout_deadline_epoch = float(client_rollout_grace_deadline_epoch)
        if (
            not math.isfinite(rollout_deadline_epoch)
            or rollout_deadline_epoch < 0
        ):
            raise ValueError(
                "Client rollout grace deadline must be a non-negative epoch."
            )
        if rollout_deadline_epoch and not (
            self.required_client_version
            and self.rollout_previous_client_version
        ):
            raise ValueError(
                "Client rollout grace deadline requires a previous client version."
            )
        self.client_rollout_pending_activation = bool(
            client_rollout_pending_activation
        )
        if self.client_rollout_pending_activation and not (
            self.required_client_version
            and self.rollout_previous_client_version
        ):
            raise ValueError(
                "Pending client rollout activation requires a previous client version."
            )
        if self.client_rollout_pending_activation and rollout_deadline_epoch:
            raise ValueError(
                "Pending client rollout activation cannot already have a deadline."
            )
        if rollout_deadline_epoch:
            self._client_rollout_grace_deadline_epoch = rollout_deadline_epoch
        elif (
            not self.client_rollout_pending_activation
            and self.required_client_version
            and self.rollout_previous_client_version
            and rollout_grace_seconds
        ):
            # The relative fallback is retained for local/test callers. Production
            # supplies an absolute persisted epoch so a service restart cannot
            # reopen an already expired compatibility window.
            self._client_rollout_grace_deadline_epoch = (
                time.time() + rollout_grace_seconds
            )
        else:
            self._client_rollout_grace_deadline_epoch = 0.0
        self._call_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._last_snapshot_fingerprints: dict[str, str] = {}
        self._tracked_tasks: set[str] = set()
        self._task_owners: dict[str, str] = {}
        self._task_controllers: dict[str, BackgroundTaskController] = {}
        # Background workers do not survive a coordinator process restart.
        # Remove leases left by the previous process before this process starts
        # accepting requests; live task leases are thereafter explicit-release.
        self.store.clear_task_leases()
        recovered_followups = self.store.recover_task_followups()
        self._browser_endpoints: dict[str, str] = {}
        for instance_id, endpoint in self.store.active_browser_endpoints().items():
            try:
                normalized_endpoint = self._validate_browser_endpoint(endpoint)
                port = int(urlparse(normalized_endpoint).port or 0)
                if not (
                    self.settings.browser_port_start
                    <= port
                    <= self.settings.browser_port_end
                ):
                    continue
                if normalized_endpoint in self._browser_endpoints.values():
                    continue
                self._browser_endpoints[instance_id] = normalized_endpoint
            except (TypeError, ValueError):
                continue
        self._instance_lock = threading.RLock()
        self._closed = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="erp-coordination-monitor",
            daemon=True,
        )
        self._receipt_monitor = threading.Thread(
            target=self._receipt_monitor_loop,
            name="erp-notification-receipt-monitor",
            daemon=True,
        )
        if controller is not None:
            initial_policy = controller.snapshot().policy
            self._global_capability_modes = dict(initial_policy.modes)
            self._global_emergency_stop = bool(
                initial_policy.emergency_stop_writes
            )
        self.store.publish_event(
            instance_id="server",
            operation="server_started",
            summary="Authoritative controller started.",
        )
        if recovered_followups:
            self.store.publish_event(
                instance_id="server",
                operation="persistent_followups_recovered",
                resources=("capability:list_orders",),
                summary=(
                    f"已恢复 {recovered_followups} 个未完成的客户通知补偿后续任务。"
                ),
            )
        self._monitor.start()
        self._receipt_monitor.start()

    def close(self) -> None:
        self._closed.set()
        self._monitor.join(timeout=5)
        self._receipt_monitor.join(timeout=5)
        if self._controller_factory is not None:
            with self._controller_lock:
                controllers = tuple(self._operator_controllers.values())
                self._operator_controllers.clear()
            for controller in controllers:
                try:
                    prepare_result = controller.prepare_close()
                    if prepare_result.accepted:
                        controller.close()
                except Exception:
                    continue

    def _controller_for(
        self,
        identity: OperatorIdentity | None,
    ) -> BackgroundTaskController:
        if self._controller_factory is None:
            if self.controller is None:
                raise RuntimeError("Shared controller is unavailable.")
            return self.controller
        if identity is None:
            raise ValueError("Verified Cloudflare operator identity is required.")
        key = identity.email.casefold()
        with self._controller_lock:
            controller = self._operator_controllers.get(key)
            if controller is None:
                controller = self._controller_factory(identity)
                policy = controller.snapshot().policy
                if self._global_emergency_stop is None:
                    self._global_capability_modes = dict(policy.modes)
                    self._global_emergency_stop = bool(
                        policy.emergency_stop_writes
                    )
                else:
                    for capability, mode in self._global_capability_modes.items():
                        controller.update_capability_mode(capability, mode)
                    controller.set_emergency_stop_writes(
                        self._global_emergency_stop
                    )
                self._operator_controllers[key] = controller
            return controller

    def _all_controllers(self) -> tuple[tuple[str, BackgroundTaskController], ...]:
        if self._controller_factory is None:
            return (("shared", self._controller_for(None)),)
        with self._controller_lock:
            return tuple(self._operator_controllers.items())

    def _invoke_global_policy(
        self,
        controller: BackgroundTaskController,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        ordered = [controller]
        ordered.extend(
            item
            for _key, item in self._all_controllers()
            if item is not controller
        )
        primary_result: Any = None
        for index, current in enumerate(ordered):
            result = getattr(current, method)(*args, **kwargs)
            if index == 0:
                primary_result = result
            if isinstance(result, ControlResult) and not result.accepted:
                return result
        if not isinstance(primary_result, ControlResult) or primary_result.accepted:
            if method == "update_capability_mode":
                self._global_capability_modes[args[0]] = args[1]
            elif method == "set_emergency_stop_writes":
                self._global_emergency_stop = bool(args[0])
        return primary_result

    def _activate_global_emergency_stop(
        self,
        controller: BackgroundTaskController,
    ) -> ControlResult:
        """Activate the safety stop without waiting for ordinary RPC serialization."""
        # Publish the server-wide intent first.  A controller created while the
        # existing controllers are being stopped will inherit the safe state.
        self._global_emergency_stop = True
        ordered = [controller]
        ordered.extend(
            item
            for _key, item in self._all_controllers()
            if item is not controller
        )
        failures: list[str] = []
        primary_result: ControlResult | None = None
        for index, current in enumerate(ordered):
            try:
                result = current.set_emergency_stop_writes(True)
            except Exception as exc:  # pragma: no cover - defensive safety boundary
                result = ControlResult(
                    False,
                    f"ERP 写入急停执行失败：{type(exc).__name__}。",
                )
            if index == 0:
                primary_result = result
            try:
                stopped = bool(current.snapshot().policy.emergency_stop_writes)
            except Exception:  # pragma: no cover - defensive safety boundary
                stopped = result.accepted
            if not stopped:
                failures.append(result.message or f"控制器 {index + 1} 未进入急停状态。")

        if failures:
            return ControlResult(
                False,
                "ERP 写入急停未能覆盖全部运行实例，请立即联系管理员。",
                details={
                    "emergency_stop_partial_failure": True,
                    "failures": failures,
                },
            )
        if primary_result is not None and primary_result.accepted:
            return primary_result
        return ControlResult(
            True,
            "ERP 写入紧急停止已开启。",
            details={"emergency_stop_verified": True},
        )

    def _require_client_version(
        self,
        client_version: str,
        *,
        instance_id: str = "",
        allow_active_task_drain: bool = False,
    ) -> bool:
        required = self.required_client_version
        supplied = str(client_version or "").strip()
        if not required or supplied == required:
            return False
        if (
            (
                self.client_rollout_pending_activation
                or self.client_rollout_grace_remaining_seconds > 0
            )
            and supplied == self.rollout_previous_client_version
        ):
            return False
        if (
            allow_active_task_drain
            and supplied == self.rollout_previous_client_version
            and str(instance_id or "").strip()
            and self.store.instance_has_active_tasks(instance_id)
        ):
            return True
        raise ClientUpdateRequiredError(required)

    def authorize_client_request(
        self,
        client_version: str,
        *,
        instance_id: str,
        allow_active_task_drain: bool = False,
    ) -> bool:
        """Return true only when an expired previous client is draining its task."""

        return self._require_client_version(
            client_version,
            instance_id=instance_id,
            allow_active_task_drain=allow_active_task_drain,
        )

    @property
    def client_rollout_grace_remaining_seconds(self) -> int:
        if not self._client_rollout_grace_deadline_epoch:
            return 0
        return max(
            0,
            math.ceil(self._client_rollout_grace_deadline_epoch - time.time()),
        )

    @property
    def client_rollout_grace_deadline_epoch(self) -> int:
        return max(0, int(self._client_rollout_grace_deadline_epoch))

    def _scheduler_status(self, instance_id: str) -> dict[str, Any]:
        status = self.store.elect_scheduler(
            instance_id,
            ttl_seconds=self.settings.scheduler_lease_seconds,
        )
        if bool(status.get("changed")):
            self.store.publish_event(
                instance_id=instance_id,
                operation="scheduler_leader_changed",
                resources=("scheduler:automatic_scans",),
                summary=(
                    f"Automatic scan scheduler leader is "
                    f"{status.get('owner_instance_id') or 'unknown'}."
                ),
                identity=self.store.instance_identity(instance_id),
            )
        return status

    @staticmethod
    def _validate_browser_endpoint(endpoint: str) -> str:
        normalized = str(endpoint or "").strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Desktop browser endpoint must be a loopback HTTP port.")
        return f"http://127.0.0.1:{parsed.port}"

    def _remember_browser_endpoint(self, instance_id: str, endpoint: str) -> str:
        normalized = self._validate_browser_endpoint(endpoint)
        port = int(urlparse(normalized).port or 0)
        if not self.settings.browser_port_start <= port <= self.settings.browser_port_end:
            raise ValueError("Desktop browser endpoint is outside the allocated port range.")
        with self._instance_lock:
            for owner, existing in self._browser_endpoints.items():
                if owner != instance_id and existing == normalized:
                    raise ValueError("Desktop browser endpoint is already assigned.")
            current = self._browser_endpoints.get(instance_id)
            if current and current != normalized:
                raise ValueError("Desktop browser endpoint does not match its allocation.")
            self.store.set_browser_endpoint(instance_id, normalized)
            self._browser_endpoints[instance_id] = normalized
        return normalized

    def allocate_browser_endpoint(
        self,
        instance_id: str,
        display_name: str,
        client_version: str = "",
        *,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        """Reserve one server loopback port for this desktop's reverse SSH tunnel."""

        update_deferred = self._require_client_version(
            client_version,
            instance_id=instance_id,
            allow_active_task_drain=True,
        )
        self.store.register_instance(
            instance_id,
            display_name,
            ttl_seconds=self.settings.instance_ttl_seconds,
            identity=identity,
        )
        self._controller_for(identity)
        scheduler = self._scheduler_status(instance_id)
        with self._instance_lock:
            existing = self._browser_endpoints.get(instance_id)
            if existing:
                port = int(urlparse(existing).port or 0)
                return {
                    "browser_endpoint": existing,
                    "browser_port": port,
                    "operator": (
                        {"name": identity.name, "email": identity.email}
                        if identity is not None
                        else {}
                    ),
                    "scheduler": scheduler,
                    "client_update_deferred": update_deferred,
                    "required_version": self.required_client_version,
                }
            used = {
                int(urlparse(endpoint).port or 0)
                for endpoint in self._browser_endpoints.values()
            }
            for port in range(
                self.settings.browser_port_start,
                self.settings.browser_port_end + 1,
            ):
                if port in used:
                    continue
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.01):
                        continue
                except OSError:
                    endpoint = f"http://127.0.0.1:{port}"
                    self._remember_browser_endpoint(instance_id, endpoint)
                    return {
                        "browser_endpoint": endpoint,
                        "browser_port": port,
                        "operator": (
                            {"name": identity.name, "email": identity.email}
                            if identity is not None
                            else {}
                        ),
                        "scheduler": scheduler,
                        "client_update_deferred": update_deferred,
                        "required_version": self.required_client_version,
                    }
        raise ValueError("No desktop browser tunnel port is currently available.")

    def register(
        self,
        instance_id: str,
        display_name: str,
        browser_endpoint: str = "",
        client_version: str = "",
        *,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        update_deferred = self._require_client_version(
            client_version,
            instance_id=instance_id,
            allow_active_task_drain=True,
        )
        self.store.register_instance(
            instance_id,
            display_name,
            ttl_seconds=self.settings.instance_ttl_seconds,
            identity=identity,
        )
        self._controller_for(identity)
        scheduler = self._scheduler_status(instance_id)
        normalized_endpoint = (
            self._remember_browser_endpoint(instance_id, browser_endpoint)
            if str(browser_endpoint or "").strip()
            else self._browser_endpoints.get(instance_id, "")
        )
        return {
            "instance_id": instance_id,
            "revision": self.store.current_revision(),
            "browser_endpoint": normalized_endpoint,
            "heartbeat_interval_seconds": max(
                5.0, self.settings.instance_ttl_seconds / 3
            ),
            "operator": (
                {
                    "name": identity.name,
                    "email": identity.email,
                }
                if identity is not None
                else {}
            ),
            "scheduler": scheduler,
            "client_update_deferred": update_deferred,
            "required_version": self.required_client_version,
        }

    def heartbeat(
        self,
        instance_id: str,
        *,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        try:
            self.store.heartbeat(
                instance_id,
                ttl_seconds=self.settings.instance_ttl_seconds,
                identity=identity,
            )
        except KeyError as exc:
            raise InstanceRegistrationExpiredError(
                "Desktop instance registration expired; reconnecting is required."
            ) from exc
        scheduler = self._scheduler_status(instance_id)
        return {
            "revision": self.store.current_revision(),
            "scheduler": scheduler,
        }

    def deregister(
        self,
        instance_id: str,
        *,
        identity: OperatorIdentity | None = None,
    ) -> None:
        self.heartbeat(instance_id, identity=identity)
        released_scheduler = self.store.deregister(instance_id)
        with self._instance_lock:
            self._browser_endpoints.pop(instance_id, None)
        if released_scheduler:
            self.store.publish_event(
                instance_id=instance_id,
                operation="scheduler_leader_released",
                resources=("scheduler:automatic_scans",),
                summary="Automatic scan scheduler leader disconnected.",
                identity=identity,
            )

    def export_portable_configuration(
        self,
        *,
        instance_id: str,
        request_id: str,
        passphrase: str,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        """Create a configuration-only package and return encrypted bytes."""

        self.heartbeat(instance_id, identity=identity)
        with TemporaryDirectory(prefix="erp-config-export-") as directory:
            destination = Path(directory) / "settings.erp-migrate"
            response = self.invoke(
                instance_id=instance_id,
                request_id=request_id,
                method="export_portable_migration",
                raw_args=[str(destination), str(passphrase or "")],
                raw_kwargs={"include_state": False},
                identity=identity,
            )
            result = response.get("result")
            accepted = isinstance(result, Mapping) and bool(
                result.get("accepted")
            )
            if not accepted:
                return {**response, "package_base64": ""}
            package = destination.read_bytes()
            if (
                not package
                or len(package) > MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES
            ):
                raise ValueError("Portable configuration package size is invalid.")
            return {
                **response,
                "package_base64": base64.b64encode(package).decode("ascii"),
            }

    def import_portable_configuration(
        self,
        *,
        instance_id: str,
        request_id: str,
        passphrase: str,
        package_base64: str,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        """Import encrypted configuration bytes without exposing server paths."""

        self.heartbeat(instance_id, identity=identity)
        try:
            package = base64.b64decode(
                str(package_base64 or ""),
                validate=True,
            )
        except Exception as exc:
            raise ValueError("Portable configuration package is invalid.") from exc
        if not package or len(package) > MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES:
            raise ValueError("Portable configuration package size is invalid.")
        with TemporaryDirectory(prefix="erp-config-import-") as directory:
            source = Path(directory) / "settings.erp-migrate"
            source.write_bytes(package)
            return self.invoke(
                instance_id=instance_id,
                request_id=request_id,
                method="import_portable_migration",
                raw_args=[str(source), str(passphrase or "")],
                raw_kwargs={
                    "overwrite": True,
                    "configuration_only": True,
                },
                identity=identity,
            )

    def snapshot_payload(
        self,
        instance_id: str,
        *,
        known_revision: int | None = None,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        heartbeat = self.heartbeat(instance_id, identity=identity)
        controller = self._controller_for(identity)
        revision = self.store.current_revision()
        if known_revision is not None and known_revision == revision:
            return {
                "revision": revision,
                "unchanged": True,
            }
        snapshot = controller.snapshot()
        if self._controller_factory is not None:
            task_by_id = {task.task_id: task for task in snapshot.tasks}
            today_task_by_id = {
                task.task_id: task for task in snapshot.today_tasks
            }
            log_by_key = {
                (
                    entry.created_at,
                    entry.task_id,
                    entry.source,
                    entry.message,
                    entry.operator_email,
                ): entry
                for entry in snapshot.logs
            }
            for _key, other in self._all_controllers():
                if other is controller:
                    continue
                other_snapshot = other.snapshot()
                task_by_id.update(
                    {task.task_id: task for task in other_snapshot.tasks}
                )
                today_task_by_id.update(
                    {
                        task.task_id: task
                        for task in other_snapshot.today_tasks
                    }
                )
                for entry in other_snapshot.logs:
                    log_by_key[
                        (
                            entry.created_at,
                            entry.task_id,
                            entry.source,
                            entry.message,
                            entry.operator_email,
                        )
                    ] = entry
            snapshot.tasks = sorted(
                task_by_id.values(),
                key=lambda task: task.updated_at,
                reverse=True,
            )
            snapshot.today_tasks = sorted(
                today_task_by_id.values(),
                key=lambda task: task.updated_at,
                reverse=True,
            )
            snapshot.logs = sorted(
                log_by_key.values(),
                key=lambda entry: entry.created_at,
                reverse=True,
            )[:1000]
        snapshot = redact_snapshot_settings(snapshot)
        if identity is not None:
            snapshot.operator_name = identity.name
            snapshot.operator_email = identity.email
        scheduler = heartbeat.get("scheduler")
        if isinstance(scheduler, Mapping):
            snapshot.scheduler_leader_instance_id = str(
                scheduler.get("owner_instance_id") or ""
            )
            snapshot.is_scheduler_leader = bool(scheduler.get("is_leader"))
        snapshot.scheduled_scan_due_at = self.store.scheduled_job_due_times(
            SCHEDULED_SCAN_INTERVALS
        )
        interactions = tuple(
            interaction
            for interaction in controller.pending_interactions()
            if self._task_owners.get(interaction.task_id)
            in {None, instance_id, _SERVER_FOLLOWUP_INSTANCE_ID}
        )
        return {
            "revision": revision,
            "unchanged": False,
            "snapshot": to_jsonable(snapshot),
            "interactions": to_jsonable(interactions),
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
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        if method not in RPC_METHODS:
            raise ValueError("RPC method is not allowed.")
        heartbeat = self.heartbeat(instance_id, identity=identity)
        controller = self._controller_for(identity)
        cached = self.store.cached_response(
            request_id,
            instance_id=instance_id,
            method=method,
        )
        if cached is not None:
            return cached
        args, kwargs = _decode_call(method, raw_args, raw_kwargs)
        emergency_stop_activation = (
            method == "set_emergency_stop_writes"
            and bool(args)
            and bool(args[0])
        )
        resources = tuple(
            dict.fromkeys(
                (
                    *_resource_keys(method, args, kwargs),
                    *_order_resource_keys(controller, method, args, kwargs),
                )
            )
        )
        scheduled_claimed = False
        persistent_followup_requested = False
        persistent_followup_intent: dict[str, Any] | None = None
        if method == "submit_task":
            command = args[0]
            endpoint = self._browser_endpoints.get(instance_id, "")
            if task_requires_visible_browser(command) and not endpoint:
                result = ControlResult(
                    False,
                    "当前电脑的可见 Chrome 通道未连接，网页任务未提交。请重新打开桌面程序。",
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
                if identity is not None:
                    controller.record_operator_event(
                        operator_name=identity.name,
                        operator_email=identity.email,
                        operation=method,
                        resources=resources,
                        message=result.message,
                        accepted=False,
                    )
                return response
            payload = dict(command.payload)
            payload[DESKTOP_INSTANCE_ID_PAYLOAD_KEY] = instance_id
            if identity is not None:
                payload[DESKTOP_OPERATOR_NAME_PAYLOAD_KEY] = identity.name
                payload[DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY] = identity.email
            if endpoint:
                payload[DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY] = endpoint
            args[0] = replace(command, payload=payload)
            persistent_followup_requested = (
                _requires_persistent_notification_followup(args[0])
            )

            trigger = str(payload.get("trigger") or "").strip()
            interval_seconds = SCHEDULED_SCAN_INTERVALS.get(trigger)
            if interval_seconds is not None:
                scheduler = heartbeat.get("scheduler")
                scheduler_owner = (
                    str(scheduler.get("owner_instance_id") or "")
                    if isinstance(scheduler, Mapping)
                    else ""
                )
                claim = self.store.claim_scheduled_job(
                    job_key=trigger,
                    interval_seconds=interval_seconds,
                    instance_id=instance_id,
                    request_id=request_id,
                )
                if not bool(claim.get("claimed")):
                    not_leader = claim.get("reason") == "not_scheduler_leader"
                    result = ControlResult(
                        False,
                        (
                            "当前客户端不是定时扫描主实例，本次自动扫描未提交；"
                            "在线客户端会自动选举并接替。"
                            if not_leader
                            else "本次定时扫描尚未到期，服务器已阻止重复提交。"
                        ),
                        details={
                            "scheduler_rejected": True,
                            "reason": str(claim.get("reason") or ""),
                            "owner_instance_id": scheduler_owner,
                            "next_due_at": float(claim.get("next_due_at") or 0),
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
                    if identity is not None:
                        controller.record_operator_event(
                            operator_name=identity.name,
                            operator_email=identity.email,
                            operation=method,
                            resources=resources,
                            message=result.message,
                            accepted=False,
                        )
                    return response
                scheduled_claimed = True
        elif method in {"cancel_task", "cancel_tasks", "retry_task"}:
            task_ids = (
                {_text(args[0] if args else "")}
                if method in {"cancel_task", "retry_task"}
                else set(_many(args[0] if args else ()))
            )
            task_ids.discard("")
            foreign_owner = next(
                (
                    (task_id, owner)
                    for task_id in sorted(task_ids)
                    if (owner := self._task_owners.get(task_id))
                    and owner
                    not in {instance_id, _SERVER_FOLLOWUP_INSTANCE_ID}
                ),
                None,
            )
            if foreign_owner is not None:
                task_id, owner_instance_id = foreign_owner
                owner_identity = self.store.instance_identity(owner_instance_id)
                owner_display_name = (
                    owner_identity.display_name
                    if owner_identity is not None
                    else owner_instance_id
                )
                result = ControlResult(
                    False,
                    (
                        f"\u4efb\u52a1\u7531\u201c{owner_display_name}\u201d"
                        "\u5728\u53e6\u4e00\u53f0\u7535\u8111\u4e0a"
                        "\u6267\u884c\uff0c\u5f53\u524d\u5b9e\u4f8b"
                        "\u4e0d\u80fd\u53d6\u6d88\u6216\u91cd\u8bd5\u3002"
                        "\u5176\u4ed6\u7a97\u53e3\u4f1a\u81ea\u52a8"
                        "\u5237\u65b0\u3002"
                    ),
                    task_id,
                    details={
                        "conflict": True,
                        "resource": f"task:{task_id}",
                        "owner_instance_id": owner_instance_id,
                        "owner_display_name": owner_display_name,
                        "owner_email": (
                            owner_identity.email if owner_identity is not None else ""
                        ),
                        "operation": method,
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
                if identity is not None:
                    controller.record_operator_event(
                        operator_name=identity.name,
                        operator_email=identity.email,
                        operation=method,
                        resources=resources,
                        message=result.message,
                        task_id=task_id,
                        accepted=False,
                    )
                return response
        elif method == "respond_interaction" and args:
            response_value = args[0]
            if isinstance(response_value, DesktopInteractionResponse):
                pending = {
                    item.request_id: item
                    for item in controller.pending_interactions()
                }
                request = pending.get(response_value.request_id)
                owner = self._task_owners.get(request.task_id) if request else None
                if owner not in {
                    None,
                    instance_id,
                    _SERVER_FOLLOWUP_INSTANCE_ID,
                }:
                    result = ControlResult(
                        False,
                        "该审核请求属于另一台电脑，当前实例不能响应。",
                        request.task_id if request else None,
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
                    if identity is not None:
                        controller.record_operator_event(
                            operator_name=identity.name,
                            operator_email=identity.email,
                            operation=method,
                            resources=resources,
                            message=result.message,
                            task_id=request.task_id if request else None,
                            accepted=False,
                        )
                    return response
        if method == "save_settings":
            submitted = args[0]
            current = controller.snapshot().settings
            args[0] = replace(
                submitted,
                **{
                    name: getattr(current, name)
                    for name in SENSITIVE_SETTINGS_FIELDS
                    if not str(getattr(submitted, name) or "")
                },
            )
        if method in READ_METHODS:
            value = getattr(controller, method)(*args, **kwargs)
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

        conflict = (
            None
            if emergency_stop_activation
            else self.store.acquire(
                resources=resources,
                instance_id=instance_id,
                request_id=request_id,
                operation=method,
                ttl_seconds=self.settings.transient_lease_seconds,
                allow_during_deployment_drain=method
                in {"cancel_task", "cancel_tasks", "respond_interaction"},
            )
        )
        if conflict is not None:
            notification_queue_conflict = (
                method == "submit_task"
                and str(conflict.resource or "").startswith("notification:")
            )
            conflict_notification_id = (
                str(conflict.resource).partition(":")[2]
                if notification_queue_conflict
                else ""
            )
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
                    "owner_email": conflict.owner_email,
                    "operation": conflict.operation,
                    "expires_at": conflict.expires_at,
                    "queue_conflict": notification_queue_conflict,
                    "conflict_notification_ids": (
                        (int(conflict_notification_id),)
                        if conflict_notification_id.isdigit()
                        else ()
                    ),
                    "conflict_task_name": "客户通知处理任务",
                    "conflict_task_status": "已进入处理队列",
                    "conflict_operator_name": conflict.owner_display_name,
                    "conflict_operator_email": conflict.owner_email,
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
            if identity is not None:
                controller.record_operator_event(
                    operator_name=identity.name,
                    operator_email=identity.email,
                    operation=method,
                    resources=resources,
                    message=result.message,
                    accepted=False,
                )
            if scheduled_claimed:
                self.store.defer_scheduled_job(request_id)
            return response

        if persistent_followup_requested:
            try:
                persistent_followup_intent = (
                    self.store.register_task_followup_intent(
                        source_request_id=request_id,
                        source_instance_id=instance_id,
                        followup_kind=_NOTIFICATION_COMPENSATION_FOLLOWUP_KIND,
                        operator_email=identity.email if identity is not None else "",
                        operator_name=identity.name if identity is not None else "",
                        identity_subject=(
                            identity.subject if identity is not None else ""
                        ),
                    )
                )
            except Exception as exc:
                self.store.release_request(request_id)
                if scheduled_claimed:
                    self.store.defer_scheduled_job(request_id)
                result = ControlResult(
                    False,
                    "客户通知补偿后续任务未能持久化，源扫描未提交："
                    f"{type(exc).__name__}。",
                    details={"persistent_followup_error": True},
                )
                revision = self.store.publish_event(
                    instance_id=instance_id,
                    operation="persistent_followup_registration_failed",
                    resources=resources,
                    summary=result.message,
                    identity=identity,
                )
                if identity is not None:
                    controller.record_operator_event(
                        operator_name=identity.name,
                        operator_email=identity.email,
                        operation="persistent_followup_registration_failed",
                        resources=resources,
                        message=result.message,
                        accepted=False,
                    )
                response = {
                    "result_type": "control_result",
                    "result": to_jsonable(result),
                    "revision": revision,
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
            if emergency_stop_activation:
                value = self._activate_global_emergency_stop(controller)
            else:
                with self._call_lock:
                    value = (
                        self._invoke_global_policy(
                            controller,
                            method,
                            args,
                            kwargs,
                        )
                        if method
                        in {"update_capability_mode", "set_emergency_stop_writes"}
                        and self._controller_factory is not None
                        else getattr(controller, method)(*args, **kwargs)
                    )
            accepted_task = bool(
                method == "submit_task"
                and isinstance(value, ControlResult)
                and value.accepted
                and value.task_id
            )
            if persistent_followup_intent is not None:
                if accepted_task:
                    try:
                        self.store.bind_task_followup_source(
                            request_id,
                            str(value.task_id),
                        )
                    except Exception as exc:
                        retry = self.store.retry_task_followup(
                            str(persistent_followup_intent["followup_id"]),
                            error=(
                                "源扫描已接受，但关联任务编号持久化失败："
                                f"{type(exc).__name__}。"
                            ),
                            initial_seconds=(
                                self.settings.followup_retry_initial_seconds
                            ),
                            maximum_seconds=(
                                self.settings.followup_retry_max_seconds
                            ),
                            outcome="SOURCE_BIND_RETRY",
                        )
                        self.store.publish_event(
                            instance_id="server",
                            operation="persistent_followup_retry_scheduled",
                            resources=("capability:list_orders",),
                            summary=(
                                "源扫描已接受，客户通知补偿关联失败，"
                                f"将在 {float(retry.get('next_attempt_at') or 0):.3f} "
                                "之后重试。"
                            ),
                            identity=identity,
                        )
                else:
                    self.store.cancel_task_followup_intent(
                        request_id,
                        getattr(value, "message", "源扫描未被接受。"),
                    )
            if accepted_task:
                self.store.bind_task(
                    request_id,
                    value.task_id,
                    ttl_seconds=self.settings.task_lease_seconds,
                )
                self._tracked_tasks.add(value.task_id)
                self._task_owners[value.task_id] = instance_id
                self._task_controllers[value.task_id] = controller
                keep_task_lease = True
            accepted = not isinstance(value, ControlResult) or value.accepted
            revision = self.store.publish_event(
                instance_id=instance_id,
                operation=method if accepted else f"{method}_rejected",
                resources=resources,
                summary=getattr(value, "message", ""),
                identity=identity,
            )
            if identity is not None:
                controller.record_operator_event(
                    operator_name=identity.name,
                    operator_email=identity.email,
                    operation=method,
                    resources=resources,
                    message=getattr(value, "message", ""),
                    task_id=getattr(value, "task_id", None),
                    accepted=accepted,
                )
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
        except Exception as exc:
            if persistent_followup_intent is not None:
                self.store.mark_task_followup_failed(
                    str(persistent_followup_intent["followup_id"]),
                    "源扫描提交发生异常：" f"{type(exc).__name__}。",
                    outcome="SOURCE_SUBMIT_FAILED",
                )
            raise
        finally:
            if not keep_task_lease:
                self.store.release_request(request_id)
                if scheduled_claimed:
                    self.store.defer_scheduled_job(request_id)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _persistent_followup_identity(
        self,
        followup: Mapping[str, Any],
    ) -> OperatorIdentity | None:
        email = str(followup.get("operator_email") or "").strip()
        if not email:
            return None
        return OperatorIdentity(
            email=email,
            name=str(followup.get("operator_name") or email).strip() or email,
            subject=(
                str(followup.get("identity_subject") or "").strip()
                or f"persistent-followup:{email}"
            ),
        )

    def _record_persistent_followup_event(
        self,
        *,
        controller: BackgroundTaskController | None,
        identity: OperatorIdentity | None,
        operation: str,
        message: str,
        accepted: bool,
        task_id: str | None = None,
    ) -> None:
        resources = ("capability:list_orders",)
        self.store.publish_event(
            instance_id="server",
            operation=operation,
            resources=resources,
            summary=message,
            identity=identity,
        )
        if controller is not None:
            controller.record_operator_event(
                operator_name=identity.name if identity is not None else "ERP 服务端",
                operator_email=identity.email if identity is not None else "",
                operation=operation,
                resources=resources,
                message=message,
                task_id=task_id,
                accepted=accepted,
            )

    def _process_persistent_task_followups(self) -> None:
        claimed = self.store.claim_due_task_followups(
            claim_seconds=self.settings.followup_claim_seconds,
        )
        for followup in claimed:
            followup_id = str(followup.get("followup_id") or "")
            identity = self._persistent_followup_identity(followup)
            controller: BackgroundTaskController | None = None
            if (
                str(followup.get("followup_kind") or "")
                != _NOTIFICATION_COMPENSATION_FOLLOWUP_KIND
            ):
                message = "不支持的服务端持久后续任务类型。"
                self.store.mark_task_followup_failed(
                    followup_id,
                    message,
                    outcome="UNSUPPORTED_KIND",
                )
                self._record_persistent_followup_event(
                    controller=None,
                    identity=identity,
                    operation="persistent_followup_failed",
                    message=message,
                    accepted=False,
                )
                continue
            try:
                controller = self._controller_for(identity)
            except Exception as exc:
                message = (
                    "无法恢复客户通知补偿所属的后台控制器："
                    f"{type(exc).__name__}。"
                )
                self.store.mark_task_followup_failed(
                    followup_id,
                    message,
                    outcome="CONTROLLER_UNAVAILABLE",
                )
                self._record_persistent_followup_event(
                    controller=None,
                    identity=identity,
                    operation="persistent_followup_failed",
                    message=message,
                    accepted=False,
                )
                continue

            source_task_id = str(
                followup.get("source_task_id")
                or followup.get("source_request_id")
                or ""
            )
            payload: dict[str, Any] = {
                "trigger": SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
                "source_scan_task_id": source_task_id,
                "persistent_server_followup": True,
                DESKTOP_INSTANCE_ID_PAYLOAD_KEY: _SERVER_FOLLOWUP_INSTANCE_ID,
            }
            if identity is not None:
                payload[DESKTOP_OPERATOR_NAME_PAYLOAD_KEY] = identity.name
                payload[DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY] = identity.email
            command = TaskCommand(
                name="物流查询后的客户通知增量补偿",
                area=TaskArea.SHIPMENT,
                capability=Capability.LIST_ORDERS,
                payload=payload,
            )
            resources = tuple(
                dict.fromkeys(
                    (
                        *_resource_keys("submit_task", [command], {}),
                        *_order_resource_keys(
                            controller,
                            "submit_task",
                            [command],
                            {},
                        ),
                    )
                )
            )
            request_id = (
                f"{followup_id}:attempt:{int(followup.get('attempt_count') or 0) + 1}"
            )
            conflict = self.store.acquire(
                resources=resources,
                instance_id=_SERVER_FOLLOWUP_INSTANCE_ID,
                request_id=request_id,
                operation="submit_persistent_followup",
                ttl_seconds=self.settings.transient_lease_seconds,
            )
            if conflict is not None:
                message = (
                    "客户通知补偿等待协调锁："
                    f"{conflict.resource} 由 {conflict.owner_display_name} 占用。"
                )
                retry = self.store.retry_task_followup(
                    followup_id,
                    error=message,
                    initial_seconds=self.settings.followup_retry_initial_seconds,
                    maximum_seconds=self.settings.followup_retry_max_seconds,
                    outcome="LEASE_CONFLICT",
                )
                delay = max(
                    0.0,
                    float(retry.get("next_attempt_at") or 0) - time.time(),
                )
                self._record_persistent_followup_event(
                    controller=controller,
                    identity=identity,
                    operation="persistent_followup_retry_scheduled",
                    message=f"{message} 将在约 {delay:.1f} 秒后自动重试。",
                    accepted=False,
                )
                continue

            try:
                with self._call_lock:
                    result = controller.submit_task(command)
                if result.accepted and result.task_id:
                    self.store.mark_task_followup_submitted(
                        followup_id,
                        result.task_id,
                    )
                    self._tracked_tasks.add(result.task_id)
                    self._task_owners[result.task_id] = (
                        _SERVER_FOLLOWUP_INSTANCE_ID
                    )
                    self._task_controllers[result.task_id] = controller
                    try:
                        self.store.bind_task(
                            request_id,
                            result.task_id,
                            ttl_seconds=self.settings.task_lease_seconds,
                        )
                    except Exception as exc:
                        # The controller has already accepted and persists the
                        # task. Keep the follow-up SUBMITTED and tracked instead
                        # of creating a duplicate retry solely because the
                        # coordination lease update failed.
                        try:
                            self._record_persistent_followup_event(
                                controller=controller,
                                identity=identity,
                                operation="persistent_followup_lease_warning",
                                message=(
                                    "客户通知补偿已提交，但协调租约绑定失败："
                                    f"{type(exc).__name__}。"
                                ),
                                accepted=False,
                                task_id=result.task_id,
                            )
                        except Exception:
                            pass
                    try:
                        self._record_persistent_followup_event(
                            controller=controller,
                            identity=identity,
                            operation="persistent_followup_submitted",
                            message=result.message,
                            accepted=True,
                            task_id=result.task_id,
                        )
                    except Exception:
                        # The durable follow-up row and accepted controller task
                        # remain authoritative even if auxiliary audit logging
                        # is temporarily unavailable.
                        pass
                    continue

                self.store.release_request(request_id)
                transient_rejection = bool(
                    result.task_id
                    or result.details.get("conflict")
                    or result.details.get("queue_conflict")
                )
                if transient_rejection:
                    retry = self.store.retry_task_followup(
                        followup_id,
                        error=result.message,
                        initial_seconds=(
                            self.settings.followup_retry_initial_seconds
                        ),
                        maximum_seconds=self.settings.followup_retry_max_seconds,
                        outcome="TASK_CONFLICT",
                    )
                    delay = max(
                        0.0,
                        float(retry.get("next_attempt_at") or 0) - time.time(),
                    )
                    self._record_persistent_followup_event(
                        controller=controller,
                        identity=identity,
                        operation="persistent_followup_retry_scheduled",
                        message=(
                            f"{result.message} 将在约 {delay:.1f} 秒后自动重试。"
                        ),
                        accepted=False,
                    )
                else:
                    self.store.mark_task_followup_failed(
                        followup_id,
                        result.message,
                        outcome="TASK_REJECTED",
                    )
                    self._record_persistent_followup_event(
                        controller=controller,
                        identity=identity,
                        operation="persistent_followup_failed",
                        message=result.message,
                        accepted=False,
                    )
            except Exception as exc:
                self.store.release_request(request_id)
                message = f"客户通知补偿提交异常：{type(exc).__name__}。"
                next_attempt = int(followup.get("attempt_count") or 0) + 1
                if next_attempt >= max(
                    1,
                    int(self.settings.followup_max_error_attempts),
                ):
                    self.store.mark_task_followup_failed(
                        followup_id,
                        message,
                        outcome="SUBMIT_EXCEPTION_EXHAUSTED",
                    )
                    self._record_persistent_followup_event(
                        controller=controller,
                        identity=identity,
                        operation="persistent_followup_failed",
                        message=message,
                        accepted=False,
                    )
                else:
                    retry = self.store.retry_task_followup(
                        followup_id,
                        error=message,
                        initial_seconds=(
                            self.settings.followup_retry_initial_seconds
                        ),
                        maximum_seconds=self.settings.followup_retry_max_seconds,
                        outcome="SUBMIT_EXCEPTION",
                    )
                    delay = max(
                        0.0,
                        float(retry.get("next_attempt_at") or 0) - time.time(),
                    )
                    self._record_persistent_followup_event(
                        controller=controller,
                        identity=identity,
                        operation="persistent_followup_retry_scheduled",
                        message=f"{message} 将在约 {delay:.1f} 秒后自动重试。",
                        accepted=False,
                    )

    def _monitor_loop(self) -> None:
        while not self._closed.wait(self.settings.monitor_interval_seconds):
            try:
                for task_id in tuple(self._tracked_tasks):
                    controller = self._task_controllers.get(task_id)
                    if controller is None:
                        continue
                    # A transient snapshot/database failure must never make a
                    # running task look idle to the production deploy gate.
                    # Renew first and release only after observing an explicit
                    # terminal state.
                    self.store.renew_task(
                        task_id,
                        ttl_seconds=self.settings.task_lease_seconds,
                    )
                    try:
                        snapshot = controller.snapshot()
                    except Exception:
                        continue
                    tasks = {task.task_id: task for task in snapshot.tasks}
                    task = tasks.get(task_id)
                    if task is None or task.status.terminal:
                        self.store.release_task(task_id)
                        self._tracked_tasks.discard(task_id)
                        self._task_owners.pop(task_id, None)
                        self._task_controllers.pop(task_id, None)
                        status = (
                            task.status.value if task is not None else "missing"
                        )
                        message = task.message if task is not None else (
                            "后台任务在协调快照中不再可见。"
                        )
                        activated = self.store.activate_task_followup(
                            task_id,
                            source_status=status,
                            source_message=message,
                        )
                        completed = self.store.complete_task_followup(
                            task_id,
                            succeeded=(
                                task is not None
                                and task.status is TaskStatus.SUCCEEDED
                            ),
                            message=message,
                        )
                        if activated:
                            self.store.publish_event(
                                instance_id="server",
                                operation="persistent_followup_activated",
                                resources=("capability:list_orders",),
                                summary=(
                                    f"源任务 {task_id} 已结束，"
                                    f"{activated} 个客户通知补偿进入持久队列。"
                                ),
                            )
                        if completed:
                            self.store.publish_event(
                                instance_id="server",
                                operation="persistent_followup_completed",
                                resources=("capability:list_orders",),
                                summary=(
                                    f"客户通知补偿任务 {task_id} 已记录终态："
                                    f"{status}。"
                                ),
                            )
                self._process_persistent_task_followups()
                for key, controller in self._all_controllers():
                    try:
                        snapshot = controller.snapshot()
                    except Exception:
                        continue
                    fingerprint = self._fingerprint(snapshot)
                    with self._snapshot_lock:
                        previous = self._last_snapshot_fingerprints.get(key, "")
                        if previous and fingerprint != previous:
                            self.store.publish_event(
                                instance_id="server",
                                operation="background_state_changed",
                                summary="Task or shared state changed.",
                            )
                        self._last_snapshot_fingerprints[key] = fingerprint
                self.store.cleanup_expired(include_task_leases=False)
                active_browser_instances = set(
                    self.store.active_browser_endpoints()
                )
                with self._instance_lock:
                    for instance_id in tuple(self._browser_endpoints):
                        if instance_id not in active_browser_instances:
                            self._browser_endpoints.pop(instance_id, None)
            except Exception:
                # Coordination monitoring must never terminate the server.  The
                # next iteration retries and API calls still use the controller.
                continue

    def _receipt_monitor_loop(self) -> None:
        """Refresh provider receipts independently of any open desktop window."""

        while not self._closed.wait(self.settings.receipt_monitor_interval_seconds):
            for key, controller in self._all_controllers():
                refresh = getattr(
                    controller,
                    "refresh_due_shipment_notification_receipts",
                    None,
                )
                if not callable(refresh):
                    continue
                try:
                    refresh(
                        operator_email="" if key == "shared" else key,
                        owner=f"server-receipts:{key}",
                    )
                except Exception:
                    # A provider or configuration error is retried at the next
                    # durable checkpoint and must never stop coordination.
                    continue
