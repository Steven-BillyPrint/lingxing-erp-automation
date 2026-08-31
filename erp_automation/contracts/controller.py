"""Controller protocol shared by presentation and coordination adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .models import (
    Capability,
    CapabilityMode,
    CustomOrderPage,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSettings,
    DesktopSnapshot,
    LogPage,
    ShipmentPage,
    TaskCommand,
)


@dataclass(frozen=True)
class ControlResult:
    accepted: bool
    message: str
    task_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict, repr=False)


@runtime_checkable
class QueueQueryController(Protocol):
    """Read-only paged queue boundary shared by local and remote clients."""

    def list_custom_order_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> CustomOrderPage: ...

    def list_shipment_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> ShipmentPage: ...


@runtime_checkable
class BackgroundTaskController(QueueQueryController, Protocol):
    """Boundary between the desktop shell and a real background worker."""

    def snapshot(self) -> DesktopSnapshot: ...

    # Repeated explicitly because the RPC audit enumerates this protocol's own
    # operations; QueueQueryController also remains usable as a narrow boundary.
    def list_custom_order_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> CustomOrderPage: ...

    def list_shipment_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        search_field: str = "platform_order_no",
        search_query: str = "",
        product_types: Sequence[str] = (),
    ) -> ShipmentPage: ...

    def submit_task(self, command: TaskCommand) -> ControlResult: ...

    def submit_tasks(
        self,
        commands: Sequence[TaskCommand],
    ) -> tuple[ControlResult, ...]: ...

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

    def reveal_sensitive_setting(self, field_name: str) -> Mapping[str, str]: ...

    def test_notification_provider(self, provider: str) -> ControlResult: ...

    def list_shipment_notifications(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        search_field: str = "all",
        search_query: str = "",
        product_types: Sequence[str] = (),
        status: str = "",
        active_notification_ids: Sequence[int] = (),
    ) -> dict[str, Any]: ...

    def get_shipment_notification_details(
        self,
        notification_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...

    def get_shipment_notification_review_previews(
        self,
        notification_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...

    def diagnose_shipment_notification_outbound(
        self,
        platform_order_no: str,
    ) -> dict[str, Any]: ...

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

    def resubmit_shipment_notifications(
        self, notification_ids: Sequence[int], *, reason: str
    ) -> ControlResult: ...

    def edit_shipment_notification_contact(
        self, notification_id: int, *, email: str, phone: str
    ) -> ControlResult: ...

    def edit_shipment_notification_package(
        self,
        notification_id: int,
        *,
        package_key: str,
        carrier: str,
        tracking_no: str,
        reason: str,
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


__all__ = ["BackgroundTaskController", "ControlResult"]
