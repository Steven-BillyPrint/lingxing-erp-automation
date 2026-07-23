"""Manual, local customization-JSON contact refresh for notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from erp_automation.persistence import CustomWorkflowStore

from shipment_automation.notification_domain import (
    NOTIFICATION_AWAITING_REVIEW,
)
from shipment_automation.notification_store import ShipmentNotificationStore

from .notification_contact_backfill import resolve_customization_json_contact


REFRESHABLE_NOTIFICATION_STATES = frozenset(
    {"WAITING_CONTACT", "AWAITING_REVIEW", "BLOCKED", "REJECTED"}
)


@dataclass(frozen=True)
class ContactRefreshItem:
    notification_id: int
    platform_order_no: str
    status: str
    email_found: bool = False
    phone_found: bool = False
    email_conflict: bool = False
    phone_conflict: bool = False
    json_status: str = ""
    detail_request_count: int = 0
    detail_error_count: int = 0
    notification_id_after: int | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "platform_order_no": self.platform_order_no,
            "status": self.status,
            "email_found": self.email_found,
            "phone_found": self.phone_found,
            "email_conflict": self.email_conflict,
            "phone_conflict": self.phone_conflict,
            "json_status": self.json_status,
            "detail_request_count": self.detail_request_count,
            "detail_error_count": self.detail_error_count,
            "notification_id_after": self.notification_id_after,
        }


@dataclass
class ContactRefreshSummary:
    requested_count: int = 0
    refreshed_count: int = 0
    unchanged_count: int = 0
    no_usable_count: int = 0
    conflict_count: int = 0
    failed_count: int = 0
    new_review_count: int = 0
    detail_request_count: int = 0
    detail_error_count: int = 0
    json_resolved_count: int = 0
    json_empty_count: int = 0
    json_missing_count: int = 0
    json_error_count: int = 0
    request_ids: list[str] = field(default_factory=list)
    results: list[ContactRefreshItem] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "requested_count": self.requested_count,
            "refreshed_count": self.refreshed_count,
            "unchanged_count": self.unchanged_count,
            "no_usable_count": self.no_usable_count,
            "conflict_count": self.conflict_count,
            "failed_count": self.failed_count,
            "new_review_count": self.new_review_count,
            "detail_request_count": self.detail_request_count,
            "detail_error_count": self.detail_error_count,
            "json_resolved_count": self.json_resolved_count,
            "json_empty_count": self.json_empty_count,
            "json_missing_count": self.json_missing_count,
            "json_error_count": self.json_error_count,
            "request_ids": list(self.request_ids),
            "results": [item.to_mapping() for item in self.results],
        }


def _system_order_nos(
    notification: Mapping[str, Any],
    store: ShipmentNotificationStore,
) -> tuple[str, ...]:
    platform = str(notification.get("platform_order_no") or "").strip()
    contact = store.get_contact(platform)
    values: list[str] = list(contact.system_order_nos if contact is not None else ())
    for item in notification.get("items") or ():
        if isinstance(item, Mapping):
            values.append(str(item.get("system_order_no") or "").strip())
    return tuple(dict.fromkeys(value for value in values if value))


async def refresh_shipment_notification_contacts(
    store: ShipmentNotificationStore,
    configuration: Any,
    notification_ids: Sequence[int],
    *,
    workflow_store: CustomWorkflowStore,
    folder_root: str | Path,
    staging_root: str | Path | None = None,
) -> ContactRefreshSummary:
    """Refresh selected unsent notifications from archived customization JSON.

    This operation is local-only.  It never calls Lingxing, WMS, mail, SMS, or
    any other external service.  The existing JSON resolver also verifies that
    the JSON ``orderId`` matches the selected platform order before returning a
    contact candidate.
    """

    normalized: list[int] = []
    for value in notification_ids:
        try:
            notification_id = int(value)
        except (TypeError, ValueError):
            continue
        if notification_id > 0 and notification_id not in normalized:
            normalized.append(notification_id)
    normalized_ids = tuple(normalized)
    summary = ContactRefreshSummary(requested_count=len(normalized_ids))
    latest_by_id = {
        int(item.get("id") or 0): item for item in store.list_notifications()
    }
    for notification_id in normalized_ids:
        notification = latest_by_id.get(notification_id)
        if (
            notification is None
            or str(notification.get("state") or "")
            not in REFRESHABLE_NOTIFICATION_STATES
        ):
            summary.failed_count += 1
            summary.results.append(
                ContactRefreshItem(notification_id, "", "invalid_state")
            )
            continue

        platform = str(notification.get("platform_order_no") or "").strip()
        system_order_nos = _system_order_nos(notification, store)
        if not platform or not system_order_nos:
            summary.failed_count += 1
            summary.results.append(
                ContactRefreshItem(notification_id, platform, "system_order_missing")
            )
            continue

        try:
            resolution = resolve_customization_json_contact(
                workflow_store,
                folder_root,
                platform,
                date_hints=(
                    notification.get("erp_completed_at"),
                    notification.get("state_changed_at"),
                    notification.get("created_at"),
                    notification.get("updated_at"),
                ),
                staging_root=staging_root,
            )
        except Exception:
            summary.failed_count += 1
            summary.json_error_count += 1
            summary.results.append(
                ContactRefreshItem(
                    notification_id,
                    platform,
                    "json_read_failed",
                    json_status="read_failed",
                )
            )
            continue

        if resolution.status == "ambiguous":
            summary.conflict_count += 1
            summary.results.append(
                ContactRefreshItem(
                    notification_id,
                    platform,
                    "conflict",
                    email_conflict=True,
                    phone_conflict=True,
                    json_status=resolution.status,
                )
            )
            continue
        if resolution.status == "parse_error":
            summary.failed_count += 1
            summary.json_error_count += 1
            summary.results.append(
                ContactRefreshItem(
                    notification_id,
                    platform,
                    "json_parse_failed",
                    json_status=resolution.status,
                )
            )
            continue
        if not resolution.authoritative:
            summary.no_usable_count += 1
            summary.json_missing_count += 1
            summary.results.append(
                ContactRefreshItem(
                    notification_id,
                    platform,
                    "no_usable_contact",
                    json_status=resolution.status,
                )
            )
            continue

        email = str(resolution.email or "").strip()
        phone = str(resolution.phone or "").strip()
        if resolution.status == "resolved":
            summary.json_resolved_count += 1
        else:
            summary.json_empty_count += 1
            summary.no_usable_count += 1

        try:
            changed = store.upsert_customization_contact(
                platform,
                email=email,
                phone=phone,
                system_order_nos=system_order_nos,
            )
            prepared = store.prepare_notification(platform, configuration)
        except Exception:
            # One bad local draft must not prevent the remaining selected orders
            # from being refreshed.  The desktop task intentionally reports only
            # the category here; the detailed exception stays in the task log.
            summary.failed_count += 1
            summary.results.append(
                ContactRefreshItem(
                    notification_id,
                    platform,
                    "local_update_failed",
                    email_found=bool(email),
                    phone_found=bool(phone),
                    json_status=resolution.status,
                )
            )
            continue
        after_id = int((prepared or {}).get("id") or 0) or None
        new_review = bool(
            after_id is not None
            and after_id != notification_id
            and str((prepared or {}).get("state") or "")
            == NOTIFICATION_AWAITING_REVIEW
        )
        summary.refreshed_count += int(changed)
        summary.unchanged_count += int(not changed)
        summary.new_review_count += int(new_review)
        summary.results.append(
            ContactRefreshItem(
                notification_id,
                platform,
                "refreshed" if changed else "unchanged",
                email_found=bool(email),
                phone_found=bool(phone),
                json_status=resolution.status,
                notification_id_after=after_id,
            )
        )
    return summary


__all__ = [
    "ContactRefreshItem",
    "ContactRefreshSummary",
    "REFRESHABLE_NOTIFICATION_STATES",
    "refresh_shipment_notification_contacts",
]
