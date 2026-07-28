"""Authoritative controller service with instance leases and change revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import threading
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from erp_automation.ui.controller import BackgroundTaskController, ControlResult
from erp_automation.ui.models import (
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    DESKTOP_INSTANCE_ID_PAYLOAD_KEY,
    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY,
    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY,
    DesktopInteractionResponse,
    TaskCommand,
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


@dataclass(frozen=True)
class CoordinationSettings:
    instance_ttl_seconds: float = 45.0
    transient_lease_seconds: float = 30.0
    task_lease_seconds: float = 90.0
    monitor_interval_seconds: float = 0.5
    scheduler_lease_seconds: float = 15.0
    browser_port_start: int = 24000
    browser_port_end: int = 24999


class ClientUpdateRequiredError(ValueError):
    """Raised when a desktop client is not the server-required release."""

    def __init__(self, required_version: str) -> None:
        self.required_version = required_version
        super().__init__(
            f"Client update required. Required version: {required_version}."
        )


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
        self._call_lock = threading.RLock()
        self._snapshot_lock = threading.RLock()
        self._last_snapshot_fingerprints: dict[str, str] = {}
        self._tracked_tasks: set[str] = set()
        self._task_owners: dict[str, str] = {}
        self._task_controllers: dict[str, BackgroundTaskController] = {}
        self._browser_endpoints: dict[str, str] = {}
        self._instance_lock = threading.RLock()
        self._closed = threading.Event()
        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="erp-coordination-monitor",
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
        self._monitor.start()

    def close(self) -> None:
        self._closed.set()
        self._monitor.join(timeout=5)
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

    def _require_client_version(self, client_version: str) -> None:
        if (
            self.required_client_version
            and str(client_version or "").strip() != self.required_client_version
        ):
            raise ClientUpdateRequiredError(self.required_client_version)

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

        self._require_client_version(client_version)
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
                    self._browser_endpoints[instance_id] = endpoint
                    return {
                        "browser_endpoint": endpoint,
                        "browser_port": port,
                        "operator": (
                            {"name": identity.name, "email": identity.email}
                            if identity is not None
                            else {}
                        ),
                        "scheduler": scheduler,
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
        self._require_client_version(client_version)
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
        except KeyError:
            self.store.register_instance(
                instance_id,
                identity.display_name if identity is not None else instance_id,
                ttl_seconds=self.settings.instance_ttl_seconds,
                identity=identity,
            )
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
            if self._task_owners.get(interaction.task_id) in {None, instance_id}
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
        resources = _resource_keys(method, args, kwargs)
        scheduled_claimed = False
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
        elif method == "respond_interaction" and args:
            response_value = args[0]
            if isinstance(response_value, DesktopInteractionResponse):
                pending = {
                    item.request_id: item
                    for item in controller.pending_interactions()
                }
                request = pending.get(response_value.request_id)
                owner = self._task_owners.get(request.task_id) if request else None
                if owner is not None and owner != instance_id:
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
                    "owner_email": conflict.owner_email,
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

        keep_task_lease = False
        try:
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

    def _monitor_loop(self) -> None:
        while not self._closed.wait(self.settings.monitor_interval_seconds):
            try:
                for task_id in tuple(self._tracked_tasks):
                    controller = self._task_controllers.get(task_id)
                    if controller is None:
                        continue
                    snapshot = controller.snapshot()
                    tasks = {task.task_id: task for task in snapshot.tasks}
                    task = tasks.get(task_id)
                    if task is None or task.status.terminal:
                        self.store.release_task(task_id)
                        self._tracked_tasks.discard(task_id)
                        self._task_owners.pop(task_id, None)
                        self._task_controllers.pop(task_id, None)
                    else:
                        self.store.renew_task(
                            task_id,
                            ttl_seconds=self.settings.task_lease_seconds,
                        )
                for key, controller in self._all_controllers():
                    snapshot = controller.snapshot()
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
                self.store.cleanup_expired()
            except Exception:
                # Coordination monitoring must never terminate the server.  The
                # next iteration retries and API calls still use the controller.
                continue
