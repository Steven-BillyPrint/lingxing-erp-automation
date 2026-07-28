"""Strict JSON codecs for the desktop controller boundary.

The wire format intentionally contains only JSON primitives.  Decoding is
explicit for every model accepted from an untrusted client so arbitrary Python
objects can never be constructed by the coordination API.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Mapping

from erp_automation.ui.controller import ControlResult
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    CapabilityPolicy,
    CustomOrderRow,
    DesktopInteractionOption,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSettings,
    DesktopSnapshot,
    LogEntry,
    LogLevel,
    LogPage,
    MigrationInfo,
    SERVER_CONFIGURED_SECRET,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)


def to_jsonable(value: Any) -> Any:
    """Convert supported controller values to JSON-safe primitives."""

    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key.value if isinstance(key, Enum) else key): to_jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"Unsupported coordination value: {type(value).__name__}")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _known_kwargs(model: type[Any], value: Any) -> dict[str, Any]:
    payload = _mapping(value, label=model.__name__)
    allowed = {field.name for field in fields(model)}
    return {key: item for key, item in payload.items() if key in allowed}


def decode_task_command(value: Any) -> TaskCommand:
    payload = _mapping(value, label="TaskCommand")
    return TaskCommand(
        name=str(payload.get("name") or ""),
        area=TaskArea(str(payload.get("area") or "")),
        capability=Capability(str(payload.get("capability") or "")),
        payload=dict(_mapping(payload.get("payload") or {}, label="TaskCommand.payload")),
        order_no=str(payload.get("order_no") or "") or None,
        execution_id=str(payload.get("execution_id") or "") or None,
    )


def decode_interaction_response(value: Any) -> DesktopInteractionResponse:
    payload = _mapping(value, label="DesktopInteractionResponse")
    return DesktopInteractionResponse(
        request_id=str(payload.get("request_id") or ""),
        accepted=bool(payload.get("accepted")),
        selected_value=(
            str(payload.get("selected_value"))
            if payload.get("selected_value") is not None
            else None
        ),
    )


def decode_settings(value: Any) -> DesktopSettings:
    return DesktopSettings(**_known_kwargs(DesktopSettings, value))


SENSITIVE_SETTINGS_FIELDS = frozenset(
    field.name for field in fields(DesktopSettings) if not field.repr
)

MAX_CONFIGURED_SECRET_LENGTH = 16_384

SCHEDULED_SCAN_TRIGGERS = frozenset(
    {"five_minute_timer", "three_hour_timer"}
)


def decode_configured_secret_lengths(value: Any) -> dict[str, int]:
    """Accept only bounded length metadata for known secret setting fields."""

    if not isinstance(value, Mapping):
        return {}
    decoded: dict[str, int] = {}
    for name, length in value.items():
        if (
            name in SENSITIVE_SETTINGS_FIELDS
            and type(length) is int
            and 0 <= length <= MAX_CONFIGURED_SECRET_LENGTH
        ):
            decoded[str(name)] = length
    return decoded


def decode_scheduled_scan_due_times(value: Any) -> dict[str, float]:
    """Accept finite due times for the two server-managed scan schedules."""

    if not isinstance(value, Mapping):
        return {}
    decoded: dict[str, float] = {}
    for name, due_at in value.items():
        if (
            name in SCHEDULED_SCAN_TRIGGERS
            and isinstance(due_at, (int, float))
            and not isinstance(due_at, bool)
            and math.isfinite(float(due_at))
            and 0 <= float(due_at) <= 32_503_680_000
        ):
            decoded[str(name)] = float(due_at)
    return decoded


def redact_snapshot_settings(snapshot: DesktopSnapshot) -> DesktopSnapshot:
    """Remove credentials while preserving their exact character counts."""

    safe_snapshot = deepcopy(snapshot)
    safe_snapshot.configured_secret_lengths = {
        name: len(value)
        for name in SENSITIVE_SETTINGS_FIELDS
        if (
            (value := str(getattr(snapshot.settings, name) or ""))
            and len(value) <= MAX_CONFIGURED_SECRET_LENGTH
        )
    }
    safe_snapshot.settings = replace(
        safe_snapshot.settings,
        **{
            name: (
                SERVER_CONFIGURED_SECRET
                if str(getattr(snapshot.settings, name) or "")
                else ""
            )
            for name in SENSITIVE_SETTINGS_FIELDS
        },
    )
    return safe_snapshot


def decode_capability(value: Any) -> Capability:
    return Capability(str(value or ""))


def decode_capability_mode(value: Any) -> CapabilityMode:
    return CapabilityMode.coerce(str(value or ""))


def decode_control_result(value: Any) -> ControlResult:
    payload = _mapping(value, label="ControlResult")
    details = payload.get("details")
    return ControlResult(
        accepted=bool(payload.get("accepted")),
        message=str(payload.get("message") or ""),
        task_id=str(payload.get("task_id") or "") or None,
        details=dict(details) if isinstance(details, Mapping) else {},
    )


def _decode_task(value: Any) -> TaskRecord:
    payload = _mapping(value, label="TaskRecord")
    raw_payload = payload.get("payload")
    return TaskRecord(
        task_id=str(payload.get("task_id") or ""),
        name=str(payload.get("name") or ""),
        area=TaskArea(str(payload.get("area") or TaskArea.MAINTENANCE.value)),
        capability=Capability(
            str(payload.get("capability") or Capability.LIST_ORDERS.value)
        ),
        status=TaskStatus(str(payload.get("status") or TaskStatus.QUEUED.value)),
        message=str(payload.get("message") or ""),
        order_no=str(payload.get("order_no") or "") or None,
        payload=dict(raw_payload) if isinstance(raw_payload, Mapping) else {},
        progress_percent=max(0, min(100, int(payload.get("progress_percent") or 0))),
        created_at=_datetime(payload.get("created_at")),
        updated_at=_datetime(payload.get("updated_at")),
        operator_name=str(payload.get("operator_name") or ""),
        operator_email=str(payload.get("operator_email") or ""),
    )


def _decode_log_entry(value: Any) -> LogEntry:
    payload = _mapping(value, label="LogEntry")
    return LogEntry(
        level=LogLevel(str(payload.get("level") or LogLevel.INFO.value)),
        source=str(payload.get("source") or ""),
        message=str(payload.get("message") or ""),
        task_id=str(payload.get("task_id") or "") or None,
        created_at=_datetime(payload.get("created_at")),
        operator_name=str(payload.get("operator_name") or ""),
        operator_email=str(payload.get("operator_email") or ""),
    )


def decode_log_page(value: Any) -> LogPage:
    payload = _mapping(value, label="LogPage")
    items = payload.get("items")
    return LogPage(
        items=tuple(_decode_log_entry(item) for item in items)
        if isinstance(items, list)
        else (),
        page=max(1, int(payload.get("page") or 1)),
        page_size=max(1, int(payload.get("page_size") or 100)),
        total=max(0, int(payload.get("total") or 0)),
    )


def decode_interactions(value: Any) -> tuple[DesktopInteractionRequest, ...]:
    if not isinstance(value, list):
        raise ValueError("Desktop interactions must be a JSON array.")
    decoded: list[DesktopInteractionRequest] = []
    for item in value:
        payload = _mapping(item, label="DesktopInteractionRequest")
        raw_options = payload.get("options")
        options = (
            tuple(
                DesktopInteractionOption(
                    value=str(option.get("value") or ""),
                    label=str(option.get("label") or ""),
                    description=str(option.get("description") or ""),
                )
                for option in raw_options
                if isinstance(option, Mapping)
            )
            if isinstance(raw_options, list)
            else ()
        )
        decoded.append(
            DesktopInteractionRequest(
                request_id=str(payload.get("request_id") or ""),
                task_id=str(payload.get("task_id") or ""),
                stage=str(payload.get("stage") or ""),
                title=str(payload.get("title") or ""),
                message=str(payload.get("message") or ""),
                options=options,
                approve_label=str(payload.get("approve_label") or "确认执行"),
                reject_label=str(payload.get("reject_label") or "拒绝 / 停止"),
                created_at=_datetime(payload.get("created_at")),
            )
        )
    return tuple(decoded)


def decode_snapshot(value: Any) -> DesktopSnapshot:
    payload = _mapping(value, label="DesktopSnapshot")
    policy_payload = _mapping(payload.get("policy") or {}, label="CapabilityPolicy")
    raw_modes = policy_payload.get("modes")
    modes = (
        {
            Capability(str(key)): CapabilityMode.coerce(str(item))
            for key, item in raw_modes.items()
        }
        if isinstance(raw_modes, Mapping)
        else {}
    )
    raw_tasks = payload.get("tasks")
    raw_today_tasks = payload.get("today_tasks")
    raw_custom = payload.get("custom_orders")
    raw_shipments = payload.get("shipments")
    raw_logs = payload.get("logs")
    migration_kwargs = _known_kwargs(MigrationInfo, payload.get("migration") or {})
    if "pending_migrations" in migration_kwargs:
        migration_kwargs["pending_migrations"] = tuple(
            str(item) for item in migration_kwargs["pending_migrations"]
        )
    return DesktopSnapshot(
        policy=CapabilityPolicy(
            modes=modes,
            emergency_stop_writes=bool(
                policy_payload.get("emergency_stop_writes", True)
            ),
        ),
        tasks=[_decode_task(item) for item in raw_tasks]
        if isinstance(raw_tasks, list)
        else [],
        today_tasks=[_decode_task(item) for item in raw_today_tasks]
        if isinstance(raw_today_tasks, list)
        else [],
        custom_orders=[
            CustomOrderRow(**_known_kwargs(CustomOrderRow, item)) for item in raw_custom
        ]
        if isinstance(raw_custom, list)
        else [],
        shipments=[
            ShipmentRow(**_known_kwargs(ShipmentRow, item)) for item in raw_shipments
        ]
        if isinstance(raw_shipments, list)
        else [],
        settings=DesktopSettings(
            **_known_kwargs(DesktopSettings, payload.get("settings") or {})
        ),
        configured_secret_lengths=decode_configured_secret_lengths(
            payload.get("configured_secret_lengths")
        ),
        migration=MigrationInfo(**migration_kwargs),
        logs=[_decode_log_entry(item) for item in raw_logs]
        if isinstance(raw_logs, list)
        else [],
        operator_name=str(payload.get("operator_name") or ""),
        operator_email=str(payload.get("operator_email") or ""),
        scheduler_leader_instance_id=str(
            payload.get("scheduler_leader_instance_id") or ""
        ),
        is_scheduler_leader=bool(payload.get("is_scheduler_leader", True)),
        scheduled_scan_due_at=decode_scheduled_scan_due_times(
            payload.get("scheduled_scan_due_at")
        ),
        backend_message=str(payload.get("backend_message") or ""),
    )
