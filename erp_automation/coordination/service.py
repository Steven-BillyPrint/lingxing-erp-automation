"""Authoritative controller service with instance leases and change revisions."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from erp_automation.contracts.controller import BackgroundTaskController, ControlResult
from erp_automation.contracts.models import (
    Capability,
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    DESKTOP_INSTANCE_ID_PAYLOAD_KEY,
    DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY,
    DESKTOP_OPERATOR_NAME_PAYLOAD_KEY,
    DesktopInteractionResponse,
    CustomOrderPage,
    ShipmentPage,
    LogPage,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_PROVIDER_TEST_TRIGGER,
    NOTIFICATION_RECEIPT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
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


LOGGER = logging.getLogger(__name__)


MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES = 4 * 1024 * 1024

SCHEDULED_SCAN_INTERVALS = {
    "five_minute_timer": 5 * 60.0,
    "three_hour_timer": 3 * 60 * 60.0,
}

_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY = "local_visible_logistics_followup"
_NOTIFICATION_COMPENSATION_FOLLOWUP_KIND = "notification_compensation"
_SERVER_FOLLOWUP_INSTANCE_ID = "server-persistent-followups"
_PORTABLE_CONFIGURATION_EXPORT_REQUEST = "export_portable_configuration"
_PORTABLE_CONFIGURATION_IMPORT_REQUEST = "import_portable_configuration"


def _with_configuration_identity(
    response: Mapping[str, Any],
    identity: OperatorIdentity | None,
) -> dict[str, Any]:
    """Attach the verified target account without mutating cached responses."""

    copied = dict(response)
    result = copied.get("result")
    if identity is None or not isinstance(result, Mapping):
        return copied
    copied_result = dict(result)
    details = copied_result.get("details")
    copied_details = dict(details) if isinstance(details, Mapping) else {}
    copied_details["target_operator_email"] = identity.email
    copied_result["details"] = copied_details
    copied["result"] = copied_result
    return copied


READ_METHODS = frozenset(
    {
        "pending_interactions",
        "list_shipment_notifications",
        "get_shipment_notification_details",
        "get_shipment_notification_review_previews",
        "diagnose_shipment_notification_outbound",
        "full_log_text",
        "scan_log_text",
        "log_directory",
        "list_log_entries",
        "list_custom_order_page",
        "list_shipment_page",
        "reveal_sensitive_setting",
    }
)

MUTATION_METHODS = frozenset(
    {
        "submit_task",
        "submit_tasks",
        "cancel_task",
        "cancel_tasks",
        "retry_task",
        "respond_interaction",
        "update_capability_mode",
        "set_emergency_stop_writes",
        "set_execution_paused",
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
        "resubmit_shipment_notifications",
        "edit_shipment_notification_contact",
        "edit_shipment_notification_package",
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
        "set_execution_paused",
    }
)
_MANUAL_REVIEW_UNLOCK_METHODS = frozenset(
    {
        "reopen_custom_workflow",
        "reopen_custom_workflows",
        "retry_shipment_stage",
        "retry_shipment_stages",
        "reopen_shipments_from_stage",
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
    operator_controller_reclamation_enabled: bool = True
    operator_controller_idle_seconds: float = 30.0 * 60.0


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


def _manual_review_resources_for_task(task: Any) -> tuple[str, ...]:
    order_no = _text(getattr(task, "order_no", ""))
    payload = getattr(task, "payload", {})
    logistics_no = _text(
        payload.get("logistics_no") if isinstance(payload, Mapping) else ""
    )
    resources: list[str] = []
    if order_no:
        resources.append(f"order:{order_no}")
    if logistics_no:
        resources.extend(
            (f"logistics:{logistics_no}", f"shipment:{logistics_no}")
        )
    return tuple(resources)


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
        if trigger == NOTIFICATION_RECEIPT_REFRESH_TRIGGER:
            return ("notifications:receipts",)
        if trigger == NOTIFICATION_PROVIDER_TEST_TRIGGER:
            provider = _text(command.payload.get("provider")) or "unknown"
            return (f"notification-provider:{provider}",)
        order = _text(command.order_no)
        if order:
            return (f"order:{order}",)
        logistics = _text(command.payload.get("logistics_no"))
        if logistics:
            return (f"logistics:{logistics}",)
        if command.capability is Capability.LIST_ORDERS:
            if command.area is TaskArea.CUSTOMIZATION:
                return ("scan:customization",)
            if command.area is TaskArea.SHIPMENT:
                if trigger in {
                    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
                    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
                }:
                    return ("scan:notification",)
                return ("scan:shipment",)
            return (f"scan:{command.area.value}",)
        return (
            f"capability:{command.area.value}:{command.capability.value}",
        )
    if method in {
        "update_capability_mode",
        "set_emergency_stop_writes",
    }:
        return ("configuration:policy",)
    if method == "save_settings":
        return ("configuration:settings",)
    if method == "reveal_sensitive_setting":
        field_name = _text(args[0]) if args else "unknown"
        return (f"configuration:secret:{field_name}",)
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
        "edit_shipment_notification_package",
    }:
        return (f"notification:{_text(args[0]) if args else 'unknown'}",)
    if method in {
        "approve_shipment_notifications",
        "mark_shipment_notifications_manually_completed",
        "cancel_shipment_notifications",
        "resubmit_shipment_notifications",
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
        "edit_shipment_notification_package",
    }
)
_BATCH_NOTIFICATION_METHODS = frozenset(
    {
        "approve_shipment_notifications",
        "mark_shipment_notifications_manually_completed",
        "cancel_shipment_notifications",
        "resubmit_shipment_notifications",
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
                if (
                    _text(row.logistics_no) in logistics_numbers
                    or _text(row.scan_issue_key) in logistics_numbers
                ):
                    add_order(row.platform_order_no)
    elif method in _SINGLE_NOTIFICATION_METHODS or method in _BATCH_NOTIFICATION_METHODS:
        notification_ids: set[str] = set()
        if method in _SINGLE_NOTIFICATION_METHODS:
            notification_ids.add(_text(args[0] if args else ""))
        else:
            notification_ids.update(_many(args[0] if args else ()))
        notification_ids.discard("")
        if notification_ids:
            details = controller.get_shipment_notification_details(
                tuple(int(value) for value in sorted(notification_ids, key=int))
            )
            if not details:
                legacy_result = controller.list_shipment_notifications()
                if isinstance(legacy_result, Mapping):
                    raw_items = legacy_result.get("items") or ()
                else:
                    raw_items = legacy_result
                details = [
                    dict(item)
                    for item in raw_items
                    if isinstance(item, Mapping)
                ]
            for item in details:
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
    elif method == "submit_tasks":
        if len(args) != 1 or kwargs:
            raise ValueError("submit_tasks expects one TaskCommand array.")
        if not isinstance(args[0], list) or not 1 <= len(args[0]) <= 200:
            raise ValueError("TaskCommand array must contain 1 to 200 items.")
        args[0] = tuple(decode_task_command(value) for value in args[0])
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
    elif method == "reveal_sensitive_setting":
        if len(args) != 1 or kwargs:
            raise ValueError(
                "reveal_sensitive_setting expects one sensitive field name."
            )
        field_name = str(args[0] or "").strip()
        if field_name not in SENSITIVE_SETTINGS_FIELDS:
            raise ValueError("Unsupported sensitive setting field.")
        args[0] = field_name
    elif method == "set_emergency_stop_writes":
        if len(args) != 1 or type(args[0]) is not bool:
            raise ValueError("set_emergency_stop_writes expects one boolean.")
    elif method == "set_execution_paused":
        if not 1 <= len(args) <= 2 or type(args[0]) is not bool:
            raise ValueError(
                "set_execution_paused expects a boolean and optional reason."
            )
        if len(args) == 2:
            args[1] = str(args[1] or "").strip()[:500]
    elif method in {"list_custom_order_page", "list_shipment_page"}:
        if args:
            raise ValueError(f"{method} accepts keyword arguments only.")
        page = max(1, int(kwargs.get("page", 1)))
        page_size = min(200, max(1, int(kwargs.get("page_size", 50))))
        search_field = str(
            kwargs.get("search_field") or "platform_order_no"
        ).strip()
        allowed_fields = (
            {"platform_order_no", "system_order_no", "product_type"}
            if method == "list_custom_order_page"
            else {"platform_order_no", "system_order_no"}
        )
        if search_field not in allowed_fields:
            raise ValueError("Unsupported queue search field.")
        raw_product_types = kwargs.get("product_types") or []
        if not isinstance(raw_product_types, list):
            raise ValueError("product_types must be an array.")
        kwargs = {
            "page": page,
            "page_size": page_size,
            "status": str(kwargs.get("status") or "").strip()[:100],
            "search_field": search_field,
            "search_query": " ".join(
                str(kwargs.get("search_query") or "").split()
            )[:200],
            "product_types": tuple(
                dict.fromkeys(
                    str(value or "").strip()[:100]
                    for value in raw_product_types[:50]
                    if str(value or "").strip()
                )
            ),
        }
    elif method == "list_shipment_notifications":
        if args:
            raise ValueError("list_shipment_notifications accepts keyword arguments only.")
        page = max(1, int(kwargs.get("page", 1)))
        page_size = min(100, max(1, int(kwargs.get("page_size", 50))))
        status = str(kwargs.get("status") or "").strip()[:80]
        search_field = str(kwargs.get("search_field") or "all").strip()
        if search_field not in {
            "all",
            "platform_order_no",
            "recipient_name",
            "recipient_email",
            "recipient_phone",
            "state",
        }:
            raise ValueError("Unsupported notification search field.")
        search_query = " ".join(str(kwargs.get("search_query") or "").split())[:200]
        raw_product_types = kwargs.get("product_types") or []
        if not isinstance(raw_product_types, list):
            raise ValueError("product_types must be an array.")
        product_types = tuple(
            dict.fromkeys(
                str(value or "").strip()[:100]
                for value in raw_product_types[:50]
                if str(value or "").strip()
            )
        )
        raw_active_notification_ids = kwargs.get("active_notification_ids") or []
        if not isinstance(raw_active_notification_ids, list) or len(
            raw_active_notification_ids
        ) > 100:
            raise ValueError("active_notification_ids must be an array of at most 100 ids.")
        active_notification_ids: list[int] = []
        for value in raw_active_notification_ids:
            notification_id = int(value)
            if notification_id <= 0:
                raise ValueError("active_notification_ids must contain positive ids.")
            if notification_id not in active_notification_ids:
                active_notification_ids.append(notification_id)
        kwargs = {
            "page": page,
            "page_size": page_size,
            "status": status,
            "search_field": search_field,
            "search_query": search_query,
            "product_types": product_types,
            "active_notification_ids": tuple(active_notification_ids),
        }
    elif method in {
        "get_shipment_notification_details",
        "get_shipment_notification_review_previews",
    }:
        if len(args) != 1 or kwargs:
            raise ValueError(
                f"{method} expects one notification id array."
            )
        if not isinstance(args[0], list) or len(args[0]) > 100:
            raise ValueError("Notification id array is invalid.")
        normalized_ids: list[int] = []
        for value in args[0]:
            notification_id = int(value)
            if notification_id <= 0:
                raise ValueError("Notification ids must be positive.")
            if notification_id not in normalized_ids:
                normalized_ids.append(notification_id)
        args[0] = tuple(normalized_ids)
    elif method == "diagnose_shipment_notification_outbound":
        if len(args) != 1 or kwargs:
            raise ValueError(
                "diagnose_shipment_notification_outbound expects one platform order number."
            )
        platform_order_no = str(args[0] or "").strip()
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
            platform_order_no,
        ):
            raise ValueError("Platform order number is invalid.")
        args[0] = platform_order_no
    elif method == "edit_shipment_notification_package":
        if len(args) != 1 or set(kwargs) != {
            "package_key",
            "carrier",
            "tracking_no",
            "reason",
        }:
            raise ValueError(
                "edit_shipment_notification_package expects one notification id "
                "and package_key, carrier, tracking_no, reason."
            )
        notification_id = int(args[0])
        if notification_id <= 0:
            raise ValueError("Notification id must be positive.")
        package_key = str(kwargs.get("package_key") or "").strip()
        carrier = " ".join(str(kwargs.get("carrier") or "").split())
        tracking_no = "".join(str(kwargs.get("tracking_no") or "").split())
        reason = " ".join(str(kwargs.get("reason") or "").split())
        if not package_key or len(package_key) > 300:
            raise ValueError("Package key is invalid.")
        if not carrier or len(carrier) > 100:
            raise ValueError("Carrier is invalid.")
        if not tracking_no or len(tracking_no) > 100:
            raise ValueError("Tracking number is invalid.")
        if not reason or len(reason) > 500:
            raise ValueError("Override reason is invalid.")
        args[0] = notification_id
        kwargs = {
            "package_key": package_key,
            "carrier": carrier,
            "tracking_no": tracking_no,
            "reason": reason,
        }
    return args, kwargs


def _result_type(value: Any) -> str:
    if isinstance(value, ControlResult):
        return "control_result"
    if isinstance(value, CustomOrderPage):
        return "custom_order_page"
    if isinstance(value, ShipmentPage):
        return "shipment_page"
    if isinstance(value, LogPage):
        return "log_page"
    if isinstance(value, tuple) and all(
        hasattr(item, "request_id") and hasattr(item, "task_id") for item in value
    ):
        return "interactions"
    return "json"


def method_result_is_log_page(value: Any) -> bool:
    return isinstance(value, LogPage)


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
        self._operator_controller_last_used: dict[str, float] = {}
        self._global_policy_lock = threading.RLock()
        self._controller_lock = threading.RLock()
        self._global_capability_modes: dict[Any, Any] = {}
        self._global_emergency_stop: bool | None = None
        self.store = store
        self.store.reset_instance_execution_pauses()
        self._global_emergency_stop = self.store.emergency_stop_writes()
        self.settings = settings or CoordinationSettings()
        operator_idle_seconds = float(
            self.settings.operator_controller_idle_seconds
        )
        if (
            not math.isfinite(operator_idle_seconds)
            or operator_idle_seconds < 60
            or operator_idle_seconds > 86_400
        ):
            raise ValueError(
                "Operator controller idle timeout must be between 60 and "
                "86400 seconds."
            )
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
        self._lost_task_owners: set[str] = set()
        self._instance_pause_targets: dict[str, set[str]] = {}
        # Background workers do not survive a coordinator process restart.
        # Remove leases left by the previous process before this process starts
        # accepting requests; live task leases are thereafter explicit-release.
        recovered_task_leases = self.store.clear_task_leases()
        if recovered_task_leases:
            self._global_emergency_stop = True
            self.store.set_emergency_stop_writes(True)
            recovered_by_task: dict[str, list[str]] = {}
            for lease in recovered_task_leases:
                resource = str(lease.get("resource") or "").strip().casefold()
                task_id = str(lease.get("task_id") or "").strip()
                if not task_id or not resource.startswith(
                    ("order:", "logistics:", "shipment:")
                ):
                    continue
                recovered_by_task.setdefault(task_id, []).append(resource)
            for task_id, resources in recovered_by_task.items():
                self.store.set_manual_review_locks(
                    resources,
                    task_id=task_id,
                    reason="协调服务重启时任务写入结果不确定，请人工读回核对。",
                )
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
        self._logistics_browser_endpoints: dict[str, str] = {}
        for (
            instance_id,
            endpoint,
        ) in self.store.active_logistics_browser_endpoints().items():
            try:
                normalized_endpoint = self._validate_browser_endpoint(endpoint)
                port = int(urlparse(normalized_endpoint).port or 0)
                if not (
                    self.settings.browser_port_start
                    <= port
                    <= self.settings.browser_port_end
                ):
                    continue
                if normalized_endpoint in {
                    *self._browser_endpoints.values(),
                    *self._logistics_browser_endpoints.values(),
                }:
                    continue
                self._logistics_browser_endpoints[instance_id] = normalized_endpoint
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
        self._read_cache_maintenance: threading.Thread | None = None
        initial_snapshot = None
        if controller is not None:
            initial_snapshot = controller.snapshot()
            initial_policy = initial_snapshot.policy
            self._global_capability_modes = dict(initial_policy.modes)
            if self._global_emergency_stop is None:
                self._global_emergency_stop = bool(
                    initial_policy.emergency_stop_writes
                )
                self.store.set_emergency_stop_writes(
                    self._global_emergency_stop
                )
            else:
                emergency_result = controller.set_emergency_stop_writes(
                    self._global_emergency_stop
                )
                if (
                    not self._global_emergency_stop
                    and isinstance(emergency_result, ControlResult)
                    and not emergency_result.accepted
                ):
                    # A recovered active task can make a stale per-controller
                    # emergency stop impossible to release during startup.
                    # Prefer a consistent fail-safe global stop over publishing
                    # a false global flag while this controller remains stopped.
                    self._activate_global_emergency_stop_locked(controller)
        startup_has_active_tasks = bool(
            initial_snapshot is not None
            and any(not task.status.terminal for task in initial_snapshot.tasks)
        )
        should_clean_legacy_read_cache = (
            not recovered_task_leases
            and not recovered_followups
            and not startup_has_active_tasks
            and self.store.path.stat().st_size >= 16 * 1024 * 1024
        )
        if should_clean_legacy_read_cache:
            self._read_cache_maintenance = threading.Thread(
                target=self._clean_legacy_read_cache,
                name="erp-legacy-read-cache-maintenance",
                daemon=True,
            )
        self.store.publish_event(
            instance_id="server",
            operation="server_started",
            summary="Authoritative controller started.",
        )
        if recovered_task_leases:
            self.store.publish_event(
                instance_id="server",
                operation="interrupted_tasks_recovered",
                resources=("safety:erp_write_emergency_stop",),
                summary=(
                    f"协调服务重启时发现 {len(recovered_task_leases)} 条未释放任务租约，"
                    "已自动开启 ERP 写入急停并锁定受影响订单等待人工复核。"
                ),
            )
        if recovered_followups:
            self.store.publish_event(
                instance_id="server",
                operation="persistent_followups_recovered",
                resources=("capability:shipment:list_orders",),
                summary=(
                    f"已恢复 {recovered_followups} 个未完成的客户通知补偿后续任务。"
                ),
            )
        self._monitor.start()
        self._receipt_monitor.start()
        if self._read_cache_maintenance is not None:
            self._read_cache_maintenance.start()

    def _clean_legacy_read_cache(self) -> None:
        """Delete disposable legacy reads without delaying server readiness."""

        try:
            maintenance = self.store.compact_legacy_read_responses(
                tuple(READ_METHODS),
                create_backup=False,
                vacuum_database=False,
            )
        except Exception:
            # Read responses are only a cache. Failure can be retried at the
            # next quiet startup and must never take the coordinator offline.
            return
        deleted = int(maintenance.get("deleted") or 0)
        if deleted <= 0:
            return
        self.store.publish_event(
            instance_id="server",
            operation="legacy_read_cache_compacted",
            resources=("maintenance:coordination-cache",),
            summary=(
                f"已后台清理 {deleted} 条旧只读 RPC 缓存；"
                "对应 SQLite 页面已可供后续写入复用。"
            ),
        )

    def close(self) -> None:
        self._reconcile_shutdown_task_leases()
        self._closed.set()
        self._monitor.join(timeout=5)
        self._receipt_monitor.join(timeout=5)
        if self._read_cache_maintenance is not None:
            self._read_cache_maintenance.join(timeout=5)
        if self._controller_factory is not None:
            with self._controller_lock:
                controllers = tuple(self._operator_controllers.values())
                self._operator_controllers.clear()
                self._operator_controller_last_used.clear()
            for controller in controllers:
                try:
                    prepare_result = controller.prepare_close()
                    if prepare_result.accepted:
                        controller.close()
                except Exception:
                    continue

    def _reconcile_shutdown_task_leases(self) -> None:
        """Distinguish a clean terminal drain from interrupted active work."""

        active_controllers: list[BackgroundTaskController] = []
        for task_id in tuple(self._tracked_tasks):
            controller = self._task_controllers.get(task_id) or self.controller
            if controller is None:
                continue
            try:
                task = next(
                    (
                        item
                        for item in controller.snapshot().tasks
                        if item.task_id == task_id
                    ),
                    None,
                )
            except Exception:
                task = None
                active_controllers.append(controller)
                continue
            if task is not None and not task.status.terminal:
                active_controllers.append(controller)
                continue
            self.store.release_task(task_id)
            self._tracked_tasks.discard(task_id)
            self._task_owners.pop(task_id, None)
            self._task_controllers.pop(task_id, None)

        if active_controllers:
            self._activate_global_emergency_stop(active_controllers[0])
            for current in dict.fromkeys(active_controllers):
                current.set_execution_paused(
                    True,
                    "协调服务正在关闭，已暂停本机未结束任务。",
                )
            for task_id in tuple(self._tracked_tasks):
                controller = self._task_controllers.get(task_id) or self.controller
                if controller is None:
                    continue
                try:
                    task = next(
                        (
                            item
                            for item in controller.snapshot().tasks
                            if item.task_id == task_id
                        ),
                        None,
                    )
                except Exception:
                    continue
                if task is not None and not task.status.terminal:
                    continue
                self.store.release_task(task_id)
                self._tracked_tasks.discard(task_id)
                self._task_owners.pop(task_id, None)
                self._task_controllers.pop(task_id, None)

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
        # Controller creation and global policy transitions share one lock.
        # A user connecting while a release is committing therefore inherits
        # either the complete old state or the complete new state, never an
        # intermediate mix.
        with self._global_policy_lock:
            with self._controller_lock:
                controller = self._operator_controllers.get(key)
                if controller is None:
                    controller = self._controller_factory(identity)
                    summary_reader = getattr(controller, "summary_snapshot", None)
                    policy = (
                        summary_reader()
                        if callable(summary_reader)
                        else controller.snapshot()
                    ).policy
                    if self._global_emergency_stop is None:
                        self._global_capability_modes = dict(policy.modes)
                        self._global_emergency_stop = bool(
                            policy.emergency_stop_writes
                        )
                        self.store.set_emergency_stop_writes(
                            self._global_emergency_stop
                        )
                    else:
                        for capability, mode in self._global_capability_modes.items():
                            controller.update_capability_mode(capability, mode)
                        emergency_result = controller.set_emergency_stop_writes(
                            self._global_emergency_stop
                        )
                        if (
                            not self._global_emergency_stop
                            and isinstance(emergency_result, ControlResult)
                            and not emergency_result.accepted
                        ):
                            # Never admit a newly loaded controller in a state
                            # that contradicts the authoritative global flag.
                            # If its recovered work makes release unsafe, move
                            # the whole service back to the fail-safe state.
                            self._activate_global_emergency_stop_locked(
                                controller
                            )
                    self._operator_controllers[key] = controller
                self._operator_controller_last_used[key] = time.monotonic()
                return controller

    def _evict_idle_operator_controllers(
        self,
        active_instance_ids: set[str],
    ) -> int:
        with self._global_policy_lock:
            return self._evict_idle_operator_controllers_locked(
                active_instance_ids
            )

    def _evict_idle_operator_controllers_locked(
        self,
        active_instance_ids: set[str],
    ) -> int:
        """Close an idle operator's private controller and its worker lanes."""

        if (
            self._controller_factory is None
            or not self.settings.operator_controller_reclamation_enabled
        ):
            return 0
        idle_seconds = float(self.settings.operator_controller_idle_seconds)
        cutoff = time.monotonic() - idle_seconds
        active_emails = {
            identity.email.casefold()
            for instance_id in active_instance_ids
            if (identity := self.store.instance_identity(instance_id)) is not None
        }
        with self._controller_lock:
            task_controllers = set(self._task_controllers.values())
            candidates = tuple(
                (key, controller, float(self._operator_controller_last_used.get(key, 0)))
                for key, controller in self._operator_controllers.items()
                if key not in active_emails
                and controller not in task_controllers
                and float(self._operator_controller_last_used.get(key, 0)) <= cutoff
            )
        evicted = 0
        for key, controller, observed_last_used in candidates:
            try:
                if any(not task.status.terminal for task in controller.snapshot().tasks):
                    continue
            except Exception:
                continue
            # Atomically detach the idle controller before closing it. A fresh
            # request either updates last_used first (and prevents eviction) or
            # creates a new controller after this pop; it can never receive a
            # controller that has already entered its closing state.
            with self._controller_lock:
                if self._operator_controllers.get(key) is not controller:
                    continue
                if self._operator_controller_last_used.get(key) != observed_last_used:
                    continue
                if controller in self._task_controllers.values():
                    continue
                self._operator_controllers.pop(key, None)
                self._operator_controller_last_used.pop(key, None)
            # Do not nest the controller and snapshot locks. Snapshot polling
            # can continue for other operators while this controller shuts down.
            with self._snapshot_lock:
                self._last_snapshot_fingerprints.pop(key, None)
            try:
                controller.close()
            except Exception as exc:
                try:
                    self.store.publish_event(
                        instance_id="server",
                        operation="idle_operator_controller_reclaim_failed",
                        resources=(f"operator:{key}",),
                        summary=(
                            "Idle operator controller was detached but cleanup "
                            f"failed: {type(exc).__name__}."
                        ),
                    )
                except Exception:
                    pass
                continue
            evicted += 1
            try:
                self.store.publish_event(
                    instance_id="server",
                    operation="idle_operator_controller_reclaimed",
                    resources=(f"operator:{key}",),
                    summary="Idle operator controller resources were reclaimed.",
                )
            except Exception:
                # Cleanup already succeeded; telemetry failure must not make the
                # monitor treat the controller as still resident.
                pass
        return evicted

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
        with self._global_policy_lock:
            return self._invoke_global_policy_locked(
                controller,
                method,
                args,
                kwargs,
            )

    def _invoke_global_policy_locked(
        self,
        controller: BackgroundTaskController,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> Any:
        ordered_with_keys = [("requesting", controller)]
        ordered_with_keys.extend(
            (key, item)
            for key, item in self._all_controllers()
            if item is not controller
        )
        ordered = [item for _key, item in ordered_with_keys]

        if (
            method == "set_emergency_stop_writes"
            and args
            and not bool(args[0])
        ):
            return self._release_global_emergency_stop(
                ordered_with_keys,
                args,
                kwargs,
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
                self.store.set_emergency_stop_writes(
                    self._global_emergency_stop
                )
        return primary_result

    def _release_global_emergency_stop(
        self,
        ordered_with_keys: Sequence[tuple[str, BackgroundTaskController]],
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> ControlResult:
        """Lift the global stop after inspecting every loaded controller.

        The caller holds the narrow global-policy ``_call_lock``. Ordinary
        resource-scoped mutations do not use this lock; controllers still
        enforce the emergency-stop write gate at task admission/execution.
        Controllers already released require no transition, so their active
        work cannot reject an idempotent repair.
        """

        original_global_state = bool(self._global_emergency_stop)
        controller_states: list[
            tuple[str, BackgroundTaskController, bool]
        ] = []
        blockers: list[dict[str, Any]] = []
        for key, current in ordered_with_keys:
            try:
                snapshot = current.snapshot()
            except Exception as exc:
                blockers.append(
                    {
                        "controller": key,
                        "reason": "snapshot_failed",
                        "error_type": type(exc).__name__,
                        "tasks": [],
                    }
                )
                continue
            emergency_stop_writes = bool(
                snapshot.policy.emergency_stop_writes
            )
            controller_states.append(
                (key, current, emergency_stop_writes)
            )
            # A controller that is already released needs no transition. Its
            # active work must not reject a repeated release requested by a
            # different operator (the original yrq/Evelyn failure mode).
            if not emergency_stop_writes:
                continue
            active_tasks = [task for task in snapshot.tasks if not task.status.terminal]
            if not active_tasks:
                continue
            blockers.append(
                {
                    "controller": key,
                    "reason": "active_tasks",
                    "tasks": [
                        {
                            "task_id": task.task_id,
                            "name": task.name,
                            "status": task.status.value,
                        }
                        for task in active_tasks[:20]
                    ],
                    "active_task_count": len(active_tasks),
                }
            )

        if blockers:
            fail_safe_reactivated = False
            if not original_global_state:
                # The service was already split: its shared flag said
                # "released" while at least one controller still required a
                # transition that cannot safely complete. Converge in the
                # safe direction instead of preserving that contradiction.
                self._activate_global_emergency_stop_locked(
                    ordered_with_keys[0][1]
                )
                fail_safe_reactivated = True
            blocked_accounts = [
                str(item.get("controller") or "")
                for item in blockers
                if str(item.get("controller") or "") != "requesting"
            ]
            account_hint = (
                f" 阻塞账号：{', '.join(blocked_accounts[:5])}。"
                if blocked_accounts
                else ""
            )
            return ControlResult(
                False,
                "后台仍有任务或状态无法核对，不能解除 ERP 写入急停；"
                f"所有控制器均保持急停。{account_hint}",
                details={
                    "emergency_stop_writes": True,
                    "authoritative_global_state": True,
                    "preflight_rejected": True,
                    "fail_safe_reactivated": fail_safe_reactivated,
                    "blockers": blockers,
                },
            )

        release_targets = [
            (key, current)
            for key, current, emergency_stop_writes in controller_states
            if emergency_stop_writes
        ]
        if not release_targets and not original_global_state:
            return ControlResult(
                True,
                "ERP 写入急停已经解除，无需重复操作。",
                details={
                    "emergency_stop_writes": False,
                    "already_applied": True,
                    "authoritative_global_state": False,
                    "controller_count": len(controller_states),
                },
            )

        results: list[ControlResult] = []
        failure: ControlResult | None = None
        for key, current in release_targets:
            try:
                result = current.set_emergency_stop_writes(*args, **kwargs)
            except Exception as exc:  # pragma: no cover - defensive boundary
                failure = ControlResult(
                    False,
                    f"控制器 {key} 解除急停失败：{type(exc).__name__}。",
                )
                break
            if not isinstance(result, ControlResult):
                failure = ControlResult(
                    False,
                    f"控制器 {key} 返回了无效的急停结果。",
                )
                break
            results.append(result)
            if not result.accepted:
                failure = result
                break

        if failure is not None:
            rollback_failures: list[str] = []
            for key, current, original_state in controller_states:
                try:
                    rollback = current.set_emergency_stop_writes(original_state)
                except Exception as exc:  # pragma: no cover - defensive boundary
                    rollback_failures.append(f"{key}:{type(exc).__name__}")
                    continue
                if isinstance(rollback, ControlResult) and not rollback.accepted:
                    rollback_failures.append(key)
            self._global_emergency_stop = original_global_state
            self.store.set_emergency_stop_writes(original_global_state)
            fail_safe_reactivated = False
            if not original_global_state or rollback_failures:
                self._activate_global_emergency_stop_locked(
                    ordered_with_keys[0][1]
                )
                fail_safe_reactivated = True
            final_global_state = bool(self._global_emergency_stop)
            return ControlResult(
                False,
                (
                    "解除 ERP 写入急停时有控制器提交失败，"
                    "已统一切回全局急停。"
                    if fail_safe_reactivated
                    else "解除 ERP 写入急停时有控制器提交失败，"
                    "已恢复操作前状态。"
                ),
                details={
                    "emergency_stop_writes": final_global_state,
                    "authoritative_global_state": final_global_state,
                    "transition_rolled_back": True,
                    "fail_safe_reactivated": fail_safe_reactivated,
                    "failure": failure.message,
                    "rollback_failures": rollback_failures,
                },
            )

        self._global_emergency_stop = False
        self.store.set_emergency_stop_writes(False)
        primary = results[0] if results else None
        return ControlResult(
            True,
            primary.message if primary is not None else "ERP 紧急停止写入已解除。",
            details={
                **(dict(primary.details) if primary is not None else {}),
                "emergency_stop_writes": False,
                "authoritative_global_state": False,
                "preflight_passed": True,
                "controller_count": len(controller_states),
                "released_controller_count": len(release_targets),
                "reconciled_inconsistent_state": (
                    not original_global_state and bool(release_targets)
                ),
            },
        )

    def _instance_pause_counts(self, instance_id: str) -> dict[str, int]:
        targets = set(self._instance_pause_targets.get(instance_id, set()))
        if not targets:
            return {"target_count": 0, "stopped_count": 0, "stopping_count": 0, "review_count": 0}
        tasks: dict[str, Any] = {}
        controllers = {
            self._task_controllers.get(task_id)
            for task_id in targets
            if self._task_controllers.get(task_id) is not None
        }
        controllers.update(controller for _key, controller in self._all_controllers())
        for controller in controllers:
            try:
                tasks.update({task.task_id: task for task in controller.snapshot().tasks})
            except Exception:
                continue
        stopping = 0
        review = 0
        stopped = 0
        for task_id in targets:
            task = tasks.get(task_id)
            if task is None or task.status.terminal:
                if task is not None and task.status is TaskStatus.BLOCKED:
                    review += 1
                else:
                    stopped += 1
            else:
                stopping += 1
        return {
            "target_count": len(targets),
            "stopped_count": stopped,
            "stopping_count": stopping,
            "review_count": review,
        }

    def _activate_instance_execution_pause(
        self,
        instance_id: str,
        reason: str,
        *,
        require_registered: bool = True,
    ) -> ControlResult:
        """Close one desktop's admission gate, then stop only its owned tasks."""

        normalized_reason = (
            str(reason or "").strip()[:500] or "用户暂停本机任务。"
        )
        registered = True
        try:
            released_scheduler = self.store.set_instance_execution_paused(
                instance_id,
                enabled=True,
                state="pausing",
                reason=normalized_reason,
            )
        except KeyError:
            if require_registered:
                raise
            registered = False
            released_scheduler = False
        failures: list[str] = []
        with self._call_lock:
            target_ids = {
                task_id
                for task_id, owner in self._task_owners.items()
                if owner == instance_id
            }
            self._instance_pause_targets.setdefault(instance_id, set()).update(target_ids)
            grouped: dict[BackgroundTaskController, list[str]] = {}
            for task_id in target_ids:
                controller = self._task_controllers.get(task_id)
                if controller is not None:
                    grouped.setdefault(controller, []).append(task_id)
            for controller, task_ids in grouped.items():
                try:
                    result = controller.pause_tasks(task_ids, normalized_reason)
                    if not result.accepted:
                        failures.append(result.message)
                except Exception as exc:
                    failures.append(f"任务停止失败：{type(exc).__name__}。")
            for task_id in target_ids:
                controller = self._task_controllers.get(task_id)
                if controller is None:
                    continue
                try:
                    task = next(
                        (
                            item
                            for item in controller.snapshot().tasks
                            if item.task_id == task_id
                        ),
                        None,
                    )
                except Exception:
                    continue
                if task is None or task.status.terminal:
                    if (
                        task is not None
                        and task.status is TaskStatus.BLOCKED
                        and bool(task.payload.get("_manual_review_lock"))
                    ):
                        self.store.set_manual_review_locks(
                            _manual_review_resources_for_task(task),
                            task_id=task_id,
                            reason=task.message,
                        )
                    # Keep tracking metadata until the monitor records follow-up
                    # terminal state, but release the scheduler/business lease as
                    # soon as the task is demonstrably stopped.
                    self.store.release_task(task_id)
        counts = self._instance_pause_counts(instance_id)
        state = "pausing" if counts["stopping_count"] or failures else "paused"
        if registered:
            self.store.set_instance_execution_paused(
                instance_id,
                enabled=True,
                state=state,
                reason=normalized_reason,
            )
        message = (
            f"正在停止本机任务：已停止 {counts['stopped_count']}/{counts['target_count']}，"
            f"仍有 {counts['stopping_count']} 个正在安全停止。"
            if state == "pausing"
            else f"本机任务已暂停：已停止 {counts['stopped_count']}/{counts['target_count']}。"
        )
        return ControlResult(
            not failures,
            message,
            details={
                "execution_paused": True,
                "instance_execution_paused": True,
                "instance_execution_pause_state": state,
                "scheduler_released": released_scheduler,
                **counts,
                "failures": failures,
            },
        )

    def _resume_instance_execution(self, instance_id: str) -> ControlResult:
        current = self.store.instance_execution_pause(instance_id)
        if str(current.get("execution_pause_state") or "active") == "pausing":
            counts = self._instance_pause_counts(instance_id)
            return ControlResult(
                False,
                "本机任务仍在安全停止，完成或转人工复核前不能恢复任务准入。",
                details={
                    "execution_paused": True,
                    "instance_execution_paused": True,
                    "instance_execution_pause_state": "pausing",
                    **counts,
                },
            )
        self.store.set_instance_execution_paused(
            instance_id,
            enabled=False,
            state="active",
        )
        self._instance_pause_targets.pop(instance_id, None)
        return ControlResult(
            True,
            "已恢复本机任务准入；此前暂停的任务不会自动重跑。",
            details={
                "execution_paused": False,
                "instance_execution_paused": False,
                "instance_execution_pause_state": "active",
            },
        )

    def _refresh_instance_pause_state(self, instance_id: str) -> None:
        try:
            current = self.store.instance_execution_pause(instance_id)
        except KeyError:
            return
        if str(current.get("execution_pause_state") or "active") != "pausing":
            return
        counts = self._instance_pause_counts(instance_id)
        if counts["stopping_count"]:
            return
        self.store.set_instance_execution_paused(
            instance_id,
            enabled=True,
            state="paused",
            reason=str(current.get("execution_pause_reason") or ""),
        )
        self.store.publish_event(
            instance_id=instance_id,
            operation="instance_execution_pause_completed",
            resources=(f"instance:{instance_id}:execution_pause",),
            summary=(
                f"本机任务已暂停：已停止 {counts['stopped_count']}/{counts['target_count']}，"
                f"人工复核 {counts['review_count']} 个。"
            ),
            identity=self.store.instance_identity(instance_id),
        )

    def activate_fail_safe_pause(
        self,
        instance_id: str,
        reason: str = "",
    ) -> ControlResult:
        """Token-authenticated fail-safe path scoped to a registered desktop."""

        self.store.instance_execution_pause(instance_id)
        result = self._activate_instance_execution_pause(
            instance_id,
            reason or "客户端请求了本机失联保护暂停。",
        )
        self.store.publish_event(
            instance_id=instance_id,
            operation="instance_execution_paused_failsafe",
            resources=(f"instance:{instance_id}:execution_pause",),
            summary=result.message,
        )
        return result

    def _activate_global_emergency_stop(
        self,
        controller: BackgroundTaskController,
    ) -> ControlResult:
        with self._global_policy_lock:
            return self._activate_global_emergency_stop_locked(controller)

    def _activate_global_emergency_stop_locked(
        self,
        controller: BackgroundTaskController,
    ) -> ControlResult:
        """Activate the safety stop without waiting for ordinary RPC serialization."""
        # Publish the server-wide intent first.  A controller created while the
        # existing controllers are being stopped will inherit the safe state.
        self._global_emergency_stop = True
        self.store.set_emergency_stop_writes(True)
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
    def _public_scheduler_status(status: Mapping[str, Any]) -> dict[str, Any]:
        """Expose leadership state without another client's instance id."""

        return {
            "is_leader": bool(status.get("is_leader")),
            "changed": bool(status.get("changed")),
        }

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
            for owner, existing in {
                **self._browser_endpoints,
                **{
                    f"logistics:{key}": value
                    for key, value in self._logistics_browser_endpoints.items()
                },
            }.items():
                if owner != instance_id and existing == normalized:
                    raise ValueError("Desktop browser endpoint is already assigned.")
            current = self._browser_endpoints.get(instance_id)
            if current and current != normalized:
                raise ValueError("Desktop browser endpoint does not match its allocation.")
            self.store.set_browser_endpoint(instance_id, normalized)
            self._browser_endpoints[instance_id] = normalized
        return normalized

    def _remember_logistics_browser_endpoint(
        self,
        instance_id: str,
        endpoint: str,
    ) -> str:
        normalized = self._validate_browser_endpoint(endpoint)
        port = int(urlparse(normalized).port or 0)
        if not self.settings.browser_port_start <= port <= self.settings.browser_port_end:
            raise ValueError("Desktop browser endpoint is outside the allocated port range.")
        with self._instance_lock:
            for owner, existing in self._browser_endpoints.items():
                if existing == normalized:
                    raise ValueError("Desktop browser endpoint is already assigned.")
            for owner, existing in self._logistics_browser_endpoints.items():
                if owner != instance_id and existing == normalized:
                    raise ValueError("Desktop browser endpoint is already assigned.")
            current = self._logistics_browser_endpoints.get(instance_id)
            if current and current != normalized:
                raise ValueError("Desktop browser endpoint does not match its allocation.")
            self.store.set_logistics_browser_endpoint(instance_id, normalized)
            self._logistics_browser_endpoints[instance_id] = normalized
        return normalized

    def allocate_browser_endpoint(
        self,
        instance_id: str,
        display_name: str,
        client_version: str = "",
        *,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        """Reserve isolated order/default and logistics-query browser tunnels."""

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
        # Port allocation is an admission-plane operation. Full operator state
        # recovery may read encrypted configuration, SQLite queues and task
        # journals, so it must not delay the desktop's startup handshake.
        scheduler = self._scheduler_status(instance_id)
        with self._instance_lock:
            existing = self._browser_endpoints.get(instance_id)
            logistics_existing = self._logistics_browser_endpoints.get(instance_id)
            used = {
                int(urlparse(endpoint).port or 0)
                for endpoint in (
                    *self._browser_endpoints.values(),
                    *self._logistics_browser_endpoints.values(),
                )
            }

            def allocate_one(*, logistics: bool) -> str:
                for port in range(
                    self.settings.browser_port_start,
                    self.settings.browser_port_end + 1,
                ):
                    if port in used:
                        continue
                    try:
                        with socket.create_connection(
                            ("127.0.0.1", port),
                            timeout=0.01,
                        ):
                            continue
                    except OSError:
                        endpoint = f"http://127.0.0.1:{port}"
                        if logistics:
                            self._remember_logistics_browser_endpoint(
                                instance_id,
                                endpoint,
                            )
                        else:
                            self._remember_browser_endpoint(instance_id, endpoint)
                        used.add(port)
                        return endpoint
                raise ValueError(
                    "No desktop browser tunnel port is currently available."
                )

            existing = existing or allocate_one(logistics=False)
            logistics_existing = logistics_existing or allocate_one(logistics=True)
            return {
                "browser_endpoint": existing,
                "browser_port": int(urlparse(existing).port or 0),
                "logistics_browser_endpoint": logistics_existing,
                "logistics_browser_port": int(
                    urlparse(logistics_existing).port or 0
                ),
                "operator": (
                    {"name": identity.name, "email": identity.email}
                    if identity is not None
                    else {}
                ),
                "scheduler": self._public_scheduler_status(scheduler),
                "client_update_deferred": update_deferred,
                "required_version": self.required_client_version,
                "instance_pause_supported": True,
            }

    def register(
        self,
        instance_id: str,
        display_name: str,
        browser_endpoint: str = "",
        client_version: str = "",
        logistics_browser_endpoint: str = "",
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
        # The controller is initialized lazily by the first snapshot or RPC.
        # Registration therefore remains responsive while recovery runs.
        scheduler = self._scheduler_status(instance_id)
        normalized_endpoint = (
            self._remember_browser_endpoint(instance_id, browser_endpoint)
            if str(browser_endpoint or "").strip()
            else self._browser_endpoints.get(instance_id, "")
        )
        normalized_logistics_endpoint = (
            self._remember_logistics_browser_endpoint(
                instance_id,
                logistics_browser_endpoint,
            )
            if str(logistics_browser_endpoint or "").strip()
            else self._logistics_browser_endpoints.get(instance_id, "")
        )
        return {
            "instance_id": instance_id,
            "revision": self.store.current_revision(),
            "browser_endpoint": normalized_endpoint,
            "logistics_browser_endpoint": normalized_logistics_endpoint,
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
            "scheduler": self._public_scheduler_status(scheduler),
            "client_update_deferred": update_deferred,
            "required_version": self.required_client_version,
            "instance_pause_supported": True,
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
            "scheduler": self._public_scheduler_status(scheduler),
        }

    def deregister(
        self,
        instance_id: str,
        *,
        identity: OperatorIdentity | None = None,
    ) -> None:
        self.heartbeat(instance_id, identity=identity)
        had_active_tasks = self.store.instance_has_active_tasks(instance_id)
        if had_active_tasks:
            result = self._activate_instance_execution_pause(
                instance_id,
                "任务所属客户端已关闭，已停止该客户端任务。",
            )
            self.store.publish_event(
                instance_id=instance_id,
                operation="owner_disconnected_tasks_paused",
                resources=(f"instance:{instance_id}:execution_pause",),
                summary=result.message,
                identity=identity,
            )
        released_scheduler = self.store.deregister(instance_id)
        with self._instance_lock:
            self._browser_endpoints.pop(instance_id, None)
            self._logistics_browser_endpoints.pop(instance_id, None)
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
        cached = self.store.cached_response(
            request_id,
            instance_id=instance_id,
            method=_PORTABLE_CONFIGURATION_EXPORT_REQUEST,
        )
        if cached is not None:
            return _with_configuration_identity(cached, identity)
        admission = self.store.begin_request(
            request_id=request_id,
            instance_id=instance_id,
            method=_PORTABLE_CONFIGURATION_EXPORT_REQUEST,
        )
        if not bool(admission.get("created")):
            raise RuntimeError("Configuration export is already running or failed.")
        try:
            with TemporaryDirectory(prefix="erp-config-export-") as directory:
                destination = Path(directory) / "settings.erp-migrate"
                response = self.invoke(
                    instance_id=instance_id,
                    request_id=f"{request_id}:export",
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
                    completed = {**response, "package_base64": ""}
                else:
                    package = destination.read_bytes()
                    if (
                        not package
                        or len(package) > MAX_PORTABLE_CONFIGURATION_PACKAGE_BYTES
                    ):
                        raise ValueError("Portable configuration package size is invalid.")
                    completed = {
                        **response,
                        "package_base64": base64.b64encode(package).decode("ascii"),
                    }
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=_PORTABLE_CONFIGURATION_EXPORT_REQUEST,
                response=completed,
            )
            return _with_configuration_identity(completed, identity)
        except Exception as exc:
            self.store.fail_request(
                request_id=request_id,
                instance_id=instance_id,
                method=_PORTABLE_CONFIGURATION_EXPORT_REQUEST,
                error=f"{type(exc).__name__}: 配置导出失败。",
            )
            raise

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
        cached = self.store.cached_response(
            request_id,
            instance_id=instance_id,
            method=_PORTABLE_CONFIGURATION_IMPORT_REQUEST,
        )
        if cached is not None:
            return _with_configuration_identity(cached, identity)
        admission = self.store.begin_request(
            request_id=request_id,
            instance_id=instance_id,
            method=_PORTABLE_CONFIGURATION_IMPORT_REQUEST,
        )
        if not bool(admission.get("created")):
            raise RuntimeError("Configuration import is already running or failed.")
        try:
            with TemporaryDirectory(prefix="erp-config-import-") as directory:
                source = Path(directory) / "settings.erp-migrate"
                source.write_bytes(package)
                response = self.invoke(
                    instance_id=instance_id,
                    request_id=f"{request_id}:import",
                    method="import_portable_migration",
                    raw_args=[str(source), str(passphrase or "")],
                    raw_kwargs={
                        "overwrite": True,
                        "configuration_only": True,
                    },
                    identity=identity,
                )
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=_PORTABLE_CONFIGURATION_IMPORT_REQUEST,
                response=response,
            )
            return _with_configuration_identity(response, identity)
        except Exception as exc:
            self.store.fail_request(
                request_id=request_id,
                instance_id=instance_id,
                method=_PORTABLE_CONFIGURATION_IMPORT_REQUEST,
                error=f"{type(exc).__name__}: 配置导入失败。",
            )
            raise

    def snapshot_payload(
        self,
        instance_id: str,
        *,
        known_revision: int | None = None,
        summary_only: bool = False,
        include_queue_pages: bool = False,
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
        summary_reader = getattr(controller, "summary_snapshot", None)
        snapshot = (
            summary_reader()
            if summary_only and callable(summary_reader)
            else controller.snapshot()
        )
        # Controllers created by ``controller_factory`` are operator-private.
        # Do not merge their task streams back together at the HTTP boundary:
        # task names, order numbers, messages and payload metadata belong only
        # to the verified operator whose controller produced this snapshot.
        # Shared business rows and concurrency locks remain authoritative in
        # their dedicated stores and coordination tables.
        snapshot = redact_snapshot_settings(snapshot)
        if identity is not None:
            snapshot.operator_name = identity.name
            snapshot.operator_email = identity.email
        scheduler = heartbeat.get("scheduler")
        if isinstance(scheduler, Mapping):
            snapshot.is_scheduler_leader = bool(scheduler.get("is_leader"))
            snapshot.scheduler_leader_instance_id = (
                instance_id if snapshot.is_scheduler_leader else ""
            )
        snapshot.scheduled_scan_due_at = self.store.scheduled_job_due_times(
            SCHEDULED_SCAN_INTERVALS
        )
        instance_pause = self.store.instance_execution_pause(instance_id)
        pause_counts = self._instance_pause_counts(instance_id)
        snapshot.policy.instance_execution_paused = bool(
            instance_pause.get("execution_paused")
        )
        snapshot.policy.instance_execution_pause_state = str(
            instance_pause.get("execution_pause_state") or "active"
        )
        snapshot.policy.execution_paused = bool(
            instance_pause.get("execution_paused")
        )
        if instance_pause.get("execution_paused"):
            snapshot.policy.execution_pause_reason = str(
                instance_pause.get("execution_pause_reason") or ""
            )
        else:
            snapshot.policy.execution_pause_reason = ""
        snapshot.policy.instance_pause_target_count = pause_counts["target_count"]
        snapshot.policy.instance_pause_stopped_count = pause_counts["stopped_count"]
        snapshot.policy.instance_pause_stopping_count = pause_counts["stopping_count"]
        snapshot.policy.instance_pause_review_count = pause_counts["review_count"]
        interactions = tuple(
            interaction
            for interaction in controller.pending_interactions()
            if (
                interaction.target_instance_id == instance_id
                if interaction.target_instance_id
                else self._task_owners.get(interaction.task_id)
                in {None, instance_id, _SERVER_FOLLOWUP_INSTANCE_ID}
            )
        )
        if summary_only and "snapshot_summary_v1" in snapshot.server_features:
            # New clients fetch complete datasets through typed page RPCs.  Old
            # clients omit snapshot_mode and continue receiving legacy lists.
            snapshot.custom_orders = []
            snapshot.shipments = []
            snapshot.logs = []
        custom_order_page = None
        shipment_page = None
        if include_queue_pages:
            custom_order_page = controller.list_custom_order_page()
            shipment_page = controller.list_shipment_page()
            snapshot.custom_orders_summary = replace(
                snapshot.custom_orders_summary,
                total=custom_order_page.total,
                revision=custom_order_page.dataset_revision,
            )
            snapshot.shipments_summary = replace(
                snapshot.shipments_summary,
                total=shipment_page.total,
                revision=shipment_page.dataset_revision,
            )
        response = {
            "revision": revision,
            "unchanged": False,
            "snapshot": to_jsonable(snapshot),
            "interactions": to_jsonable(interactions),
            "instance_pause_supported": True,
        }
        if custom_order_page is not None and shipment_page is not None:
            response["custom_order_page"] = to_jsonable(custom_order_page)
            response["shipment_page"] = to_jsonable(shipment_page)
        return response

    def request_status_payload(
        self,
        *,
        instance_id: str,
        request_id: str,
        method: str,
        identity: OperatorIdentity | None = None,
    ) -> dict[str, Any]:
        """Return the durable outcome of one mutation without re-executing it."""

        self.heartbeat(instance_id, identity=identity)
        status = self.store.request_status(
            request_id,
            instance_id=instance_id,
            method=method,
        )
        if status is None:
            return {
                "request_id": request_id,
                "method": method,
                "state": "NOT_FOUND",
                "response": None,
                "error": "服务器尚未记录该请求。",
            }
        return status

    def fail_request(
        self,
        *,
        instance_id: str,
        request_id: str,
        method: str,
        error: str,
    ) -> None:
        """Persist a terminal RPC failure for timeout reconciliation."""

        if method not in MUTATION_METHODS or not request_id:
            return
        self.store.fail_request(
            request_id=request_id,
            instance_id=instance_id,
            method=method,
            error=error,
        )

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
        cached = (
            None
            if method in READ_METHODS
            else self.store.cached_response(
                request_id,
                instance_id=instance_id,
                method=method,
            )
        )
        if cached is not None:
            return cached
        args, kwargs = _decode_call(method, raw_args, raw_kwargs)
        if method in MUTATION_METHODS:
            admission = self.store.begin_request(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
            )
            if not bool(admission.get("created")):
                state = str(admission.get("state") or "RUNNING")
                encoded = str(admission.get("response_json") or "")
                if state == "SUCCEEDED" and encoded:
                    decoded = json.loads(encoded)
                    if isinstance(decoded, dict):
                        return decoded
                message = (
                    str(admission.get("error") or "").strip()
                    if state == "FAILED"
                    else "请求正在服务器执行；请通过请求状态继续核对，勿重复操作。"
                )
                result = ControlResult(
                    False,
                    message or "请求结果尚未确认。",
                    details={
                        "request_id": request_id,
                        "request_state": state,
                        "submission_outcome_unknown": state != "FAILED",
                        "non_modal": True,
                        "retry_suppressed": True,
                    },
                )
                return {
                    "result_type": "control_result",
                    "result": to_jsonable(result),
                    "revision": self.store.current_revision(),
                }
        if method == "submit_tasks":
            # Keep every existing per-task safety gate, coordination lease,
            # idempotency record and audit event. Only the desktop/server
            # transport is batched, eliminating one network round-trip per
            # selected order. Child request ids make a partially completed
            # batch safe to retry after a connection interruption.
            results: list[Any] = []
            latest_revision = self.store.current_revision()
            for index, command in enumerate(args[0]):
                child = self.invoke(
                    instance_id=instance_id,
                    request_id=f"{request_id}:task:{index}",
                    method="submit_task",
                    raw_args=to_jsonable([command]),
                    raw_kwargs={},
                    identity=identity,
                )
                if str(child.get("result_type") or "") != "control_result":
                    raise ValueError("submit_task returned an invalid batch result.")
                results.append(child.get("result"))
                latest_revision = max(
                    latest_revision,
                    int(child.get("revision") or latest_revision),
                )
            response = {
                "result_type": "control_results",
                "result": results,
                "revision": latest_revision,
            }
            self.store.save_response(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
                response=response,
            )
            return response
        emergency_stop_activation = (
            method == "set_emergency_stop_writes"
            and bool(args)
            and bool(args[0])
        )
        emergency_stop_release_already_applied = (
            method == "set_emergency_stop_writes"
            and bool(args)
            and not bool(args[0])
            and not bool(self._global_emergency_stop)
        )
        execution_pause_activation = (
            method == "set_execution_paused"
            and bool(args)
            and bool(args[0])
        )
        safety_activation = (
            emergency_stop_activation
            or execution_pause_activation
            or emergency_stop_release_already_applied
        )
        resources = tuple(
            dict.fromkeys(
                (
                    *_resource_keys(method, args, kwargs),
                    *_order_resource_keys(controller, method, args, kwargs),
                )
            )
        )
        if method == "set_execution_paused":
            resources = (f"instance:{instance_id}:execution_pause",)
        review_lock = (
            self.store.manual_review_lock(resources)
            if method == "submit_task"
            else None
        )
        if review_lock is not None:
            result = ControlResult(
                False,
                "该订单存在写入结果不确定的人工复核锁；请先读回核对并从对应阶段重新打开。",
                str(review_lock.get("task_id") or "") or None,
                details={
                    "manual_review_lock": True,
                    "resource": str(review_lock.get("resource") or ""),
                    "reason": str(review_lock.get("reason") or ""),
                },
            )
            revision = self.store.publish_event(
                instance_id=instance_id,
                operation="operation_rejected_by_manual_review_lock",
                resources=resources,
                summary=result.message,
                identity=identity,
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
            if identity is not None:
                controller.record_operator_event(
                    operator_name=identity.name,
                    operator_email=identity.email,
                    operation=method,
                    resources=resources,
                    message=result.message,
                    task_id=result.task_id,
                    accepted=False,
                )
            return response
        instance_pause = self.store.instance_execution_pause(instance_id)
        instance_pause_override_allowed = method in {
            "cancel_task",
            "cancel_tasks",
            "respond_interaction",
            "set_execution_paused",
            "set_emergency_stop_writes",
        }
        if (
            bool(instance_pause.get("execution_paused"))
            and method in MUTATION_METHODS
            and not instance_pause_override_allowed
        ):
            result = ControlResult(
                False,
                "本机任务已暂停；当前操作被直接拒绝，未创建任务或加入队列。",
                details={
                    "execution_paused": True,
                    "instance_execution_paused": True,
                    "instance_execution_pause_state": str(
                        instance_pause.get("execution_pause_state") or "paused"
                    ),
                    "reason": "instance_execution_paused",
                },
            )
            revision = self.store.publish_event(
                instance_id=instance_id,
                operation="operation_rejected_by_instance_pause",
                resources=resources,
                summary=result.message,
                identity=identity,
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
        scheduled_claimed = False
        persistent_followup_requested = False
        persistent_followup_intent: dict[str, Any] | None = None
        batch_owner_conflicts: list[tuple[str, str]] = []
        if method == "submit_task":
            command = args[0]
            endpoint = (
                self._logistics_browser_endpoints.get(instance_id, "")
                if command.capability is Capability.ALIBABA_LOGISTICS
                else self._browser_endpoints.get(instance_id, "")
            )
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
            active_instances = self.store.active_instance_ids()
            foreign_owners = [
                (task_id, owner)
                for task_id in sorted(task_ids)
                if (owner := self._task_owners.get(task_id))
                and owner not in {instance_id, _SERVER_FOLLOWUP_INSTANCE_ID}
                and owner in active_instances
            ]
            foreign_owner = foreign_owners[0] if foreign_owners else None
            if method == "cancel_tasks" and foreign_owners:
                blocked_ids = {task_id for task_id, _owner in foreign_owners}
                allowed_ids = [
                    task_id
                    for task_id in _many(args[0] if args else ())
                    if task_id not in blocked_ids
                ]
                if allowed_ids:
                    args[0] = allowed_ids
                    batch_owner_conflicts = foreign_owners
                    foreign_owner = None
                    resources = tuple(
                        dict.fromkeys(
                            (
                                *_resource_keys(method, args, kwargs),
                                *_order_resource_keys(
                                    controller,
                                    method,
                                    args,
                                    kwargs,
                                ),
                            )
                        )
                    )
            if foreign_owner is not None:
                task_id, _owner_instance_id = foreign_owner
                result = ControlResult(
                    False,
                    (
                        "\u4efb\u52a1\u7531\u53e6\u4e00\u53f0\u7535\u8111\u6b63\u5728"
                        "\u6267\u884c\uff0c\u5f53\u524d\u5b9e\u4f8b"
                        "\u4e0d\u80fd\u53d6\u6d88\u6216\u91cd\u8bd5\u3002"
                        "\u5176\u4ed6\u7a97\u53e3\u4f1a\u81ea\u52a8"
                        "\u5237\u65b0\u3002"
                    ),
                    task_id,
                    details={
                        "conflict": True,
                        "resource": f"task:{task_id}",
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
                owner = (
                    request.target_instance_id
                    if request is not None and request.target_instance_id
                    else self._task_owners.get(request.task_id)
                    if request is not None
                    else None
                )
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
        if method == "reveal_sensitive_setting":
            if identity is None:
                raise PermissionError("查看敏感配置需要已验证的企业账号。")
            value = controller.reveal_sensitive_setting(args[0])
            audit_message = "已查看敏感配置；内容未写入日志。"
            revision = self.store.publish_event(
                instance_id=instance_id,
                operation=method,
                resources=resources,
                summary=audit_message,
                identity=identity,
            )
            controller.record_operator_event(
                operator_name=identity.name,
                operator_email=identity.email,
                operation=method,
                resources=resources,
                message=audit_message,
                accepted=True,
            )
            return {
                "result_type": "json",
                "result": to_jsonable(value),
                "revision": revision,
            }
        if method in READ_METHODS:
            value = getattr(controller, method)(*args, **kwargs)
            response = {
                "result_type": _result_type(value),
                "result": to_jsonable(value),
                "revision": self.store.current_revision(),
            }
            return response

        conflict = (
            None
            if safety_activation
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
                    "该业务对象正在由其他客户端处理，当前操作未执行。"
                    "请等待处理完成后重试，其他窗口会自动刷新。"
                ),
                details={
                    "conflict": True,
                    "resource": conflict.resource,
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
            if execution_pause_activation:
                value = self._activate_instance_execution_pause(
                    instance_id,
                    str(args[1] if len(args) > 1 else "")
                    or "用户暂停本机任务。",
                )
            elif method == "set_execution_paused":
                value = self._resume_instance_execution(instance_id)
            elif emergency_stop_activation:
                value = self._activate_global_emergency_stop(controller)
            else:
                def invoke_controller() -> Any:
                    raced_pause = self.store.instance_execution_pause(instance_id)
                    if (
                        bool(raced_pause.get("execution_paused"))
                        and method in MUTATION_METHODS
                        and not instance_pause_override_allowed
                    ):
                        return ControlResult(
                            False,
                            "本机暂停门禁已生效；当前操作被直接拒绝，未创建任务或加入队列。",
                            details={
                                "execution_paused": True,
                                "instance_execution_paused": True,
                                "instance_execution_pause_state": str(
                                    raced_pause.get("execution_pause_state") or "paused"
                                ),
                                "reason": "instance_execution_paused",
                            },
                        )
                    return (
                        self._invoke_global_policy(
                            controller,
                            method,
                            args,
                            kwargs,
                        )
                        if method
                        in {
                            "update_capability_mode",
                            "set_emergency_stop_writes",
                        }
                        else getattr(controller, method)(*args, **kwargs)
                    )

                # Ordinary operations are protected by durable per-resource
                # leases and each controller's own state lock.  The service-wide
                # lock is reserved for the two truly global policy transitions;
                # no provider, file, crypto or bulk database I/O may hold it.
                if method in {
                    "update_capability_mode",
                    "set_emergency_stop_writes",
                }:
                    lock_wait_started = time.monotonic()
                    with self._call_lock:
                        lock_wait_ms = round(
                            (time.monotonic() - lock_wait_started) * 1000
                        )
                        value = invoke_controller()
                    if lock_wait_ms >= 100:
                        LOGGER.warning(
                            "coordination_rpc_lock_wait method=%s request_id=%s "
                            "lock_wait_ms=%s",
                            method,
                            request_id,
                            lock_wait_ms,
                        )
                else:
                    value = invoke_controller()
            if batch_owner_conflicts and isinstance(value, ControlResult):
                skipped = len(batch_owner_conflicts)
                value = ControlResult(
                    value.accepted,
                    (
                        f"{value.message}\n另有 {skipped} 个任务仍由在线电脑执行，"
                        "已跳过，不影响其余任务暂停。"
                    ),
                    value.task_id,
                    details={
                        **dict(value.details),
                        "partial_success": bool(value.accepted),
                        "foreign_owner_conflicts": [
                            {"task_id": task_id}
                            for task_id, _owner in batch_owner_conflicts
                        ],
                    },
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
                            resources=("capability:shipment:list_orders",),
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
                if self.store.instance_execution_pause(instance_id).get(
                    "execution_paused"
                ):
                    self._activate_instance_execution_pause(
                        instance_id,
                        "本机暂停门禁生效时任务刚完成提交，现已立即停止。",
                    )
            accepted = not isinstance(value, ControlResult) or value.accepted
            if accepted and method in _MANUAL_REVIEW_UNLOCK_METHODS:
                self.store.clear_manual_review_locks(resources)
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
            self.store.fail_request(
                request_id=request_id,
                instance_id=instance_id,
                method=method,
                error=f"{type(exc).__name__}: 请求执行失败。",
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
        resources = ("capability:shipment:list_orders",)
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
                    f"{conflict.resource} 正由其他客户端处理。"
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
                active_instances = self.store.active_instance_ids()
                for task_id in tuple(self._tracked_tasks):
                    controller = self._task_controllers.get(task_id)
                    if controller is None:
                        continue
                    owner = self._task_owners.get(task_id, "")
                    owner_lost = bool(
                        owner
                        and owner != _SERVER_FOLLOWUP_INSTANCE_ID
                        and owner not in active_instances
                    )
                    if owner_lost:
                        if owner not in self._lost_task_owners:
                            self._lost_task_owners.add(owner)
                            result = self._activate_instance_execution_pause(
                                owner,
                                "检测到任务所属客户端心跳超时（断网、断电或意外关机），"
                                "已停止该客户端任务。",
                                require_registered=False,
                            )
                            self.store.publish_event(
                                instance_id=owner,
                                operation="owner_heartbeat_expired_tasks_paused",
                                resources=(f"instance:{owner}:execution_pause",),
                                summary=result.message,
                            )
                    else:
                        # Renew only while the owning desktop heartbeat is alive.
                        # A lost owner must never retain an immortal task lease.
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
                        if (
                            task is not None
                            and task.status is TaskStatus.BLOCKED
                            and bool(task.payload.get("_manual_review_lock"))
                        ):
                            self.store.set_manual_review_locks(
                                _manual_review_resources_for_task(task),
                                task_id=task_id,
                                reason=task.message,
                            )
                        self.store.release_task(task_id)
                        self._tracked_tasks.discard(task_id)
                        self._task_owners.pop(task_id, None)
                        self._task_controllers.pop(task_id, None)
                        if (
                            owner
                            and owner not in active_instances
                            and owner not in self._task_owners.values()
                        ):
                            self._instance_pause_targets.pop(owner, None)
                            self._lost_task_owners.discard(owner)
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
                                resources=("capability:shipment:list_orders",),
                                summary=(
                                    f"源任务 {task_id} 已结束，"
                                    f"{activated} 个客户通知补偿进入持久队列。"
                                ),
                            )
                        if completed:
                            self.store.publish_event(
                                instance_id="server",
                                operation="persistent_followup_completed",
                                resources=("capability:shipment:list_orders",),
                                summary=(
                                    f"客户通知补偿任务 {task_id} 已记录终态："
                                    f"{status}。"
                                ),
                            )
                for instance_id in tuple(self.store.pausing_instance_ids()):
                    self._refresh_instance_pause_state(instance_id)
                self._evict_idle_operator_controllers(active_instances)
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
                # Task leases are released only after a terminal snapshot. A
                # delayed monitor iteration must not erase a still-running
                # task's deployment guard merely because its TTL elapsed.
                # Prior-process leases are cleared once, during service
                # startup, so retaining them here is fail-safe without making
                # them immortal across coordinator restarts.
                self.store.cleanup_expired(include_task_leases=False)
                # Read the authoritative endpoint owners under the same lock
                # used by registration.  Without this boundary, the monitor
                # could read an old empty set, a new host could register, and
                # the monitor would then delete that newly registered endpoint
                # from memory using its stale snapshot.
                with self._instance_lock:
                    active_browser_instances = set(
                        self.store.active_browser_endpoints()
                    )
                    active_logistics_browser_instances = set(
                        self.store.active_logistics_browser_endpoints()
                    )
                    for instance_id in tuple(self._browser_endpoints):
                        if instance_id not in active_browser_instances:
                            self._browser_endpoints.pop(instance_id, None)
                    for instance_id in tuple(self._logistics_browser_endpoints):
                        if instance_id not in active_logistics_browser_instances:
                            self._logistics_browser_endpoints.pop(instance_id, None)
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
                    result = refresh(
                        operator_email="" if key == "shared" else key,
                        owner=f"server-receipts:{key}",
                    )
                    checked = int(result.get("checked") or 0)
                    completed = int(result.get("completed") or 0)
                    unconfirmed = int(result.get("unconfirmed") or 0)
                    errors = int(result.get("errors") or 0)
                    if checked or completed or unconfirmed or errors:
                        self.store.publish_event(
                            instance_id="server",
                            operation="notification_receipts_refreshed",
                            resources=("notifications:receipts",),
                            summary=(
                                f"Customer notification receipts refreshed for {key}: "
                                f"checked={checked}, completed={completed}, "
                                f"unconfirmed={unconfirmed}, errors={errors}."
                            ),
                        )
                except Exception:
                    # A provider or configuration error is retried at the next
                    # durable checkpoint and must never stop coordination.
                    continue
