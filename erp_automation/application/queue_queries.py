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
from shipment_automation.models import shipment_tracking_attention_notice


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
_SHIPMENT_STATUS_PRIORITY = {
    "扫描错误": -1,
    "可标发": 0,
    "可继续标发": 0,
    "等待标发": 1,
    "等待用户确认": 1,
    "标发处理中": 1,
    "标发失败可重试": 2,
    "物流逾期异常": 2,
    "待查询物流": 3,
    "查询失败待重试": 4,
    "等待物流就绪": 5,
    "物流信息需复核": 6,
    "标发需人工复核": 6,
    "已完成": 7,
    "已取消": 8,
    "标签已移除": 8,
    "本轮已取消": 8,
    "订单信息冲突": 9,
}


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
    }
    direct_fields = (
        "logistics_no", "customer_shipping_service", "first_seen_at",
        "international_tracking_no", "carrier", "alibaba_status", "actual_total",
        "chargeable_weight_kg", "identity_state", "identity_status_text",
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
    )
    values.update({name: str(row.get(name) or "") for name in direct_fields})
    return ShipmentRow(**values)


def _timestamp_value(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def shipment_status_timestamp(row: ShipmentRow) -> str:
    if row.scan_issue_code:
        return row.scan_issue_state_changed_at or row.updated_at or row.last_scanned_at
    identity = row.identity_state.strip().upper()
    logistics = row.logistics_state.strip().upper()
    erp = row.erp_state.strip().upper()
    if erp == "DONE":
        return row.outbounded_at or row.externally_completed_at or row.erp_state_changed_at or row.updated_at
    if identity and identity != "ACTIVE":
        return row.identity_state_changed_at or row.updated_at
    if logistics != "READY":
        return row.logistics_state_changed_at or row.updated_at
    return row.erp_state_changed_at or row.updated_at


def _has_live_lease(row: ShipmentRow, *, now: datetime | None = None) -> bool:
    if not (row.lease_owner or row.lease_stage or row.lease_until):
        return False
    text = row.lease_until.strip()
    if not text:
        return True
    try:
        expires_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def shipment_business_status(row: ShipmentRow, *, now: datetime | None = None) -> str:
    if row.scan_issue_code:
        return {
            "MANUAL_REVIEW": "标发需人工复核",
            "MANUALLY_COMPLETED": "已完成",
            "MANUALLY_CANCELLED": "已取消",
        }.get(row.scan_issue_state.strip().upper(), "扫描错误")
    identity = row.identity_state.strip().upper()
    logistics = row.logistics_state.strip().upper()
    erp = row.erp_state.strip().upper()
    checkpoint = row.checkpoint.strip().upper()
    if identity == "CANCELLED":
        return "本轮已取消"
    if identity == "MANUALLY_CANCELLED":
        return "已取消"
    if identity == "PAUSED_TAG_REMOVED":
        return "标签已移除"
    if identity and identity != "ACTIVE":
        return "订单信息冲突"
    if erp == "DONE":
        return "已完成"
    if logistics == "CANCELLED":
        return "已取消"
    if shipment_tracking_attention_notice(
        customer_shipping_service=row.customer_shipping_service,
        first_seen_at=row.first_seen_at,
        carrier=row.carrier,
        international_tracking_no=row.international_tracking_no,
        logistics_state=logistics,
        identity_state=identity,
        erp_state=erp,
        tracking_validated=row.tracking_validated,
        now=now,
    ):
        return "物流逾期异常"
    if logistics in {"", "PENDING"}:
        return "待查询物流"
    if logistics == "WAITING":
        return "等待物流就绪"
    if logistics == "RETRYABLE":
        return "查询失败待重试"
    if logistics != "READY":
        return "物流信息需复核"
    if not all((row.carrier.strip(), row.international_tracking_no.strip(), row.actual_total.strip(), row.chargeable_weight_kg.strip())):
        return "物流信息需复核"
    if erp == "BLOCKED":
        return "标发需人工复核"
    if _has_live_lease(row, now=now):
        return "标发处理中"
    if erp == "RUNNING" or checkpoint not in {"", "NONE"}:
        return "可继续标发"
    if erp == "RETRYABLE":
        return "标发失败可重试"
    return "可标发"


def _product_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split("|") if part.strip())


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
) -> ShipmentPage:
    overlays = dict(active_statuses or {})
    selected_products = {str(value).strip().casefold() for value in product_types if str(value).strip()}
    needle = str(search_query or "").strip().casefold()
    field = search_field if search_field in {"platform_order_no", "system_order_no"} else "platform_order_no"
    normalized_status = str(status or "").strip()

    def display_status(row: ShipmentRow) -> str:
        return str(overlays.get(row.logistics_no) or shipment_business_status(row))

    filtered = [
        row for row in rows
        if (not normalized_status or display_status(row) == normalized_status)
        and (not needle or needle in str(getattr(row, field, "") or "").casefold())
        and (not selected_products or bool(selected_products & {value.casefold() for value in _product_values(row.product_type)}))
    ]

    def sort_key(row: ShipmentRow) -> tuple[object, ...]:
        value = display_status(row)
        bucket = 0 if value == "标发处理中" else 1 if value in {"等待标发", "等待用户确认"} else 2 if value in {"可标发", "可继续标发"} else 4 if value == "已完成" else 5 if value in {"已取消", "标签已移除", "本轮已取消"} else 3
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
