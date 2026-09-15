"""Automatic dispatch uses the same commands and execution gates as manual work."""
from __future__ import annotations

from typing import Any, Mapping

from erp_automation.contracts.models import (
    AUTOMATIC_PROCESSING_PAYLOAD_KEY,
    Capability, TaskArea, TaskCommand, TaskRecord, DesktopWriteAction,
    DesktopWriteConfirmation, DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY, SHIPMENT_SUBMISSION_ID_PAYLOAD_KEY,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER, SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    notification_confirmation_order_no,
    task_requires_visible_browser,
)

AUTOMATIC_PROCESSING_FEATURE = "automatic_processing_v1"
AUTOMATIC_PROCESSING_MIN_CLIENT_VERSION = "2026.09.15.1"
AUTOMATIC_CUSTOM_SCAN_INTERVAL_SECONDS = 5 * 60.0
AUTOMATIC_SHIPMENT_SCAN_INTERVAL_SECONDS = 3 * 60 * 60.0


def is_automatic(command: TaskCommand | TaskRecord) -> bool:
    confirmation = command.payload.get(DESKTOP_CONFIRMATION_PAYLOAD_KEY) or {}
    return bool(command.payload.get(AUTOMATIC_PROCESSING_PAYLOAD_KEY)) or (
        isinstance(confirmation, Mapping) and confirmation.get("source") == "automatic_mode"
    )


def automatic_schedule_key(command: TaskCommand) -> str:
    if not is_automatic(command):
        return ""
    if command.capability is Capability.ALIBABA_LOGISTICS:
        return "automatic_logistics"
    if command.capability is Capability.LIST_ORDERS:
        if command.area is TaskArea.CUSTOMIZATION:
            return "automatic_custom_scan"
        if command.payload.get("trigger") == NOTIFICATION_REVIEW_RESCAN_TRIGGER:
            return "automatic_notification_scan"
        if command.area is TaskArea.SHIPMENT:
            return "automatic_shipment_scan"
    return ""


def dispatch_identity(command: TaskCommand) -> str:
    if command.area is TaskArea.CUSTOMIZATION and command.capability.is_write:
        return f"custom:{command.order_no}"
    if command.capability is Capability.OUTBOUND_ORDER:
        return f"shipment:{command.payload.get('logistics_no') or ''}"
    if command.capability is Capability.SEND_NOTIFICATION:
        ids = command.payload.get("notification_ids") or ()
        if len(ids) == 1:
            return f"notification:{int(ids[0])}"
    return ""


def automatic_scan_commands() -> tuple[TaskCommand, ...]:
    return tuple(
        TaskCommand(name, area, capability, payload={
            AUTOMATIC_PROCESSING_PAYLOAD_KEY: True, "trigger": trigger,
        })
        for name, area, capability, trigger in (
            ("自动扫描定制订单", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS, "automatic_scan"),
            ("自动扫描标发订单", TaskArea.SHIPMENT, Capability.LIST_ORDERS, "automatic_scan"),
            ("自动同步客户通知", TaskArea.SHIPMENT, Capability.LIST_ORDERS, NOTIFICATION_REVIEW_RESCAN_TRIGGER),
            ("自动查询阿里物流", TaskArea.SHIPMENT, Capability.ALIBABA_LOGISTICS, "automatic_scan"),
        )
    )


AUTOMATIC_SCAN_INTERVALS = {
    "automatic_custom_scan": AUTOMATIC_CUSTOM_SCAN_INTERVAL_SECONDS,
    "automatic_shipment_scan": AUTOMATIC_SHIPMENT_SCAN_INTERVAL_SECONDS,
    "automatic_notification_scan": AUTOMATIC_SHIPMENT_SCAN_INTERVAL_SECONDS,
    "automatic_logistics": AUTOMATIC_SHIPMENT_SCAN_INTERVAL_SECONDS,
}


def automatic_write_command(kind: str, row: Mapping[str, Any]) -> TaskCommand:
    payload: dict[str, Any] = {AUTOMATIC_PROCESSING_PAYLOAD_KEY: True}
    system = str(row.get("system_order_no") or "")
    platform = str(row.get("platform_order_no") or "")
    logistics = ""
    if kind == "custom":
        action, capability, area = DesktopWriteAction.PROCESS_CUSTOM_ORDER, Capability.UPDATE_CONTACT, TaskArea.CUSTOMIZATION
        name = f"自动处理定制订单：{platform}"
        payload["system_order_no"] = system
    elif kind == "shipment":
        action, capability, area = DesktopWriteAction.EXECUTE_ERP_MARK, Capability.OUTBOUND_ORDER, TaskArea.SHIPMENT
        name = f"自动标发：{platform}"
        logistics = str(row["logistics_no"])
        payload.update(system_order_no=system, logistics_no=logistics)
    elif kind == "notification":
        action, capability, area = DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION, Capability.SEND_NOTIFICATION, TaskArea.SHIPMENT
        ids = [int(row["id"])]
        platform = notification_confirmation_order_no(ids)
        system = ""
        name = f"自动发送客户通知：{row['platform_order_no']}"
        payload.update(trigger=SHIPMENT_NOTIFICATION_SEND_TRIGGER, notification_ids=ids, retry=False)
    else:
        raise ValueError(f"未知自动处理类型：{kind}")
    confirmation = DesktopWriteConfirmation.create(
        action, platform, system_order_no=system, logistics_no=logistics,
        source="automatic_mode",
    )
    payload[DESKTOP_CONFIRMATION_PAYLOAD_KEY] = confirmation.to_payload()
    if kind in {"custom", "shipment"}:
        payload[CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY if kind == "custom" else SHIPMENT_SUBMISSION_ID_PAYLOAD_KEY] = confirmation.confirmation_id
    return TaskCommand(name, area, capability, order_no=platform, payload=payload)


def command_document(command: TaskCommand) -> dict[str, Any]:
    return {"name": command.name, "area": command.area.value,
            "capability": command.capability.value, "order_no": command.order_no,
            "payload": dict(command.payload)}


def run_automatic_processing(controller: Any) -> Any:
    """Called on a client worker; browser setup and submission receipts stay intact."""
    from erp_automation.contracts.controller import ControlResult

    accepted = 0
    rejected: list[str] = []
    browser_unavailable = False
    for item in controller.get_automatic_processing_tasks():
        command = TaskCommand(
            str(item["name"]), TaskArea(item["area"]), Capability(item["capability"]),
            order_no=item.get("order_no"), payload=dict(item.get("payload") or {}),
        )
        if browser_unavailable and task_requires_visible_browser(command):
            continue
        result = controller.submit_task(command)
        accepted += int(result.accepted)
        if not result.accepted:
            rejected.append(result.message)
        browser_unavailable = browser_unavailable or bool(result.details.get("local_browser_unavailable"))
        if result.details.get("submission_outcome_unknown"):
            break
    message = f"自动处理已排队 {accepted} 项。" if accepted else ""
    if rejected:
        message += f" {rejected[0]}"
    return ControlResult(True, message.strip(), details={
        "submitted": accepted, "rejected": rejected, "non_modal": True,
    })
