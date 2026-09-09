"""Read-only queue projections shared by controllers and transports.

The queue UI must not know how SQLite rows, task overlays, or product facets are
assembled.  Keeping those rules here gives local and coordinated controllers
the same deterministic paging behavior without coupling them to Qt widgets.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from erp_automation.contracts.models import (
    CustomOrderPage,
    CustomOrderRow,
    QueueFacets,
    ShipmentPage,
    ShipmentRow,
)
from erp_automation.operations.product_identity_report import (
    classify_product_identity_evidence,
)
from lingxing_automation.products.catalog import PRODUCT_IDENTITY_CATALOG_VERSION
from shipment_automation.queue_policy import (
    SHIPMENT_STATUS_PRIORITY as _SHIPMENT_STATUS_PRIORITY,
    product_values as _product_values,
    shipment_business_status,
    shipment_status_bucket,
    shipment_status_timestamp,
    timestamp_value as _timestamp_value,
)


QUEUE_PAGINATION_FEATURES = (
    "custom_order_pagination_v1",
    "shipment_pagination_v1",
    "notification_pagination_v2",
    "snapshot_summary_v1",
)

_CUSTOM_PENDING_STATUSES = frozenset(
    {
        "pending",
        "folder_pending",
        "sku_adjustment_pending",
        "package_split_pending",
        "instruction_remark_pending",
        "warehouse_logistics_pending",
    }
)


def sqlite_dataset_revision(path: str | Path) -> str:
    """Return a non-secret change marker for a SQLite database and its WAL."""

    resolved = Path(path).resolve()
    parts = [str(resolved)]
    for candidate in (resolved, Path(f"{resolved}-wal")):
        try:
            stat = candidate.stat()
        except FileNotFoundError:
            parts.extend(("0", "0", "0"))
        else:
            parts.extend(("1", str(stat.st_size), str(stat.st_mtime_ns)))
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]


def custom_order_row_from_mapping(row: Mapping[str, Any]) -> CustomOrderRow:
    return CustomOrderRow(
        platform_order_no=str(row.get("platform_order_no") or ""),
        system_order_no=str(row.get("original_system_order_no") or row.get("system_order_no") or ""),
        product_type=str(row.get("product_type") or ""),
        workflow_stage=str(row.get("workflow_status") or row.get("workflow_stage") or ""),
        status_text=str(
            row.get("display_status")
            or ("已忽略" if bool(row.get("ignored")) else "")
            or row.get("status_text")
            or row.get("workflow_status")
            or ""
        ),
        last_error=str(row.get("last_error") or ""),
        result_detail=str(row.get("result_detail") or ""),
        retry_confirmation_required=bool(row.get("retry_confirmation_required")),
        status_updated_at=str(row.get("updated_at") or row.get("status_updated_at") or ""),
        tag_text=str(row.get("tag_text") or ""),
    )


def _shipment_product_identity_status(row: Mapping[str, Any]) -> str:
    try:
        details = json.loads(str(row.get("product_identity_evidence_json") or "{}"))
    except (TypeError, json.JSONDecodeError):
        details = {}
    if not isinstance(details, Mapping):
        details = {}
    return classify_product_identity_evidence(
        row,
        details,
        catalog_version=PRODUCT_IDENTITY_CATALOG_VERSION,
    )


def shipment_row_from_mapping(row: Mapping[str, Any]) -> ShipmentRow:
    values: dict[str, Any] = {
        "platform_order_no": str(row.get("platform_order_no") or ""),
        "system_order_no": str(row.get("system_order_no") or ""),
        "product_type": str(row.get("product_type") or ""),
        "checkpoint": str(row.get("erp_checkpoint") or row.get("checkpoint") or ""),
        "product_identity_status_text": _shipment_product_identity_status(row),
        "tracking_validated": (
            bool(row.get("tracking_validated"))
            if row.get("tracking_validated") is not None
            else None
        ),
        "product_identity_retry_count": int(row.get("product_identity_retry_count") or 0),
        "wms_selection_required": bool(row.get("wms_selection_required")),
        "re_mark_cycle_id": int(row.get("re_mark_cycle_id") or 0),
    }
    direct_fields = (
        "logistics_no", "customer_shipping_service", "first_seen_at",
        "international_tracking_no", "carrier", "alibaba_status", "actual_total",
        "chargeable_weight_kg", "identity_state", "identity_status_text",
        "identity_conflict_description",
        "logistics_state", "logistics_next_attempt_at", "erp_state",
        "erp_next_attempt_at", "lease_owner", "lease_stage", "lease_until",
        "last_error", "updated_at", "last_scanned_at", "identity_state_changed_at",
        "logistics_state_changed_at", "logistics_last_checked_at",
        "erp_state_changed_at", "outbounded_at", "externally_completed_at",
        "completion_source", "erp_last_error", "logistics_last_error", "email_state",
        "email_last_error", "sku_text", "product_identity_catalog_version",
        "product_identity_checked_at", "product_identity_next_retry_at",
        "product_identity_last_error", "product_identity_evidence_json",
        "scan_issue_code", "logistics_overdue_at", "scan_issue_key", "scan_issue_state",
        "scan_issue_reason", "scan_issue_state_changed_at",
        "re_mark_state", "re_mark_checkpoint", "re_mark_wo_number",
        "re_mark_old_carrier", "re_mark_old_waybill_no",
        "re_mark_new_carrier", "re_mark_new_service_line",
        "re_mark_new_waybill_no",
        "re_mark_new_tracking_no", "re_mark_new_freight",
        "re_mark_new_currency", "re_mark_new_fee_weight_g",
        "re_mark_last_error", "re_mark_updated_at",
    )
    values.update({name: str(row.get(name) or "") for name in direct_fields})
    return ShipmentRow(**values)


def paginate_custom_order_rows(
    rows: Sequence[CustomOrderRow],
    *,
    page: int = 1,
    page_size: int = 50,
    status: str = "",
    search_field: str = "platform_order_no",
    search_query: str = "",
    product_types: Sequence[str] = (),
    active_statuses: Mapping[str, str] | None = None,
    dataset_revision: str = "",
) -> CustomOrderPage:
    overlays = dict(active_statuses or {})
    selected_products = {str(value).strip().casefold() for value in product_types if str(value).strip()}
    needle = str(search_query or "").strip().casefold()
    field = search_field if search_field in {"platform_order_no", "system_order_no", "product_type"} else "platform_order_no"
    normalized_status = str(status or "").strip()

    def display_status(row: CustomOrderRow) -> str:
        return str(overlays.get(row.platform_order_no) or row.status_text or row.workflow_stage)

    filtered = [
        row for row in rows
        if (not normalized_status or display_status(row) == normalized_status)
        and (not needle or needle in str(getattr(row, field, "") or "").casefold())
        and (not selected_products or bool(selected_products & {value.casefold() for value in _product_values(row.product_type)}))
    ]

    def sort_key(row: CustomOrderRow) -> tuple[object, ...]:
        value = display_status(row)
        bucket = 0 if value == "processing" else 1 if value == "waiting" else 2 if value in _CUSTOM_PENDING_STATUSES else 4 if value in {"not_required", "completed"} else 5 if value in {"cancelled", "已忽略"} else 3
        return bucket, -_timestamp_value(row.status_updated_at), row.platform_order_no

    filtered.sort(key=sort_key)
    normalized_size = max(1, min(int(page_size), 200))
    total = len(filtered)
    page_count = max(1, (total + normalized_size - 1) // normalized_size)
    normalized_page = min(max(1, int(page)), page_count)
    start = (normalized_page - 1) * normalized_size
    all_statuses = {display_status(row) for row in rows if display_status(row)}
    all_products = {value for row in rows for value in _product_values(row.product_type)}
    return CustomOrderPage(
        items=tuple(filtered[start : start + normalized_size]),
        page=normalized_page,
        page_size=normalized_size,
        total=total,
        dataset_revision=dataset_revision,
        facets=QueueFacets(tuple(sorted(all_statuses)), tuple(sorted(all_products))),
    )


def paginate_shipment_rows(
    rows: Sequence[ShipmentRow],
    *,
    page: int = 1,
    page_size: int = 50,
    status: str = "",
    search_field: str = "platform_order_no",
    search_query: str = "",
    product_types: Sequence[str] = (),
    active_statuses: Mapping[str, str] | None = None,
    dataset_revision: str = "",
    now: datetime | None = None,
) -> ShipmentPage:
    observed_at = now or datetime.now(timezone.utc)
    overlays = dict(active_statuses or {})
    selected_products = {str(value).strip().casefold() for value in product_types if str(value).strip()}
    needle = str(search_query or "").strip().casefold()
    field = search_field if search_field in {"platform_order_no", "system_order_no"} else "platform_order_no"
    normalized_status = str(status or "").strip()

    def display_status(row: ShipmentRow) -> str:
        if row.manual_review_reason:
            return shipment_business_status(row, now=observed_at)
        return str(overlays.get(row.logistics_no) or shipment_business_status(row, now=observed_at))

    filtered = [
        row for row in rows
        if (not normalized_status or display_status(row) == normalized_status)
        and (not needle or needle in str(getattr(row, field, "") or "").casefold())
        and (not selected_products or bool(selected_products & {value.casefold() for value in _product_values(row.product_type)}))
    ]

    def sort_key(row: ShipmentRow) -> tuple[object, ...]:
        value = display_status(row)
        bucket = shipment_status_bucket(value)
        return bucket, _SHIPMENT_STATUS_PRIORITY.get(value, 99), -_timestamp_value(shipment_status_timestamp(row)), row.platform_order_no, row.logistics_no

    filtered.sort(key=sort_key)
    normalized_size = max(1, min(int(page_size), 200))
    total = len(filtered)
    page_count = max(1, (total + normalized_size - 1) // normalized_size)
    normalized_page = min(max(1, int(page)), page_count)
    start = (normalized_page - 1) * normalized_size
    all_statuses = {display_status(row) for row in rows if display_status(row)}
    all_products = {value for row in rows for value in _product_values(row.product_type)}
    return ShipmentPage(
        items=tuple(filtered[start : start + normalized_size]),
        page=normalized_page,
        page_size=normalized_size,
        total=total,
        dataset_revision=dataset_revision,
        facets=QueueFacets(tuple(sorted(all_statuses)), tuple(sorted(all_products))),
    )
