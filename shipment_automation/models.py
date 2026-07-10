from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


QUEUE_STATUS_NEW = "NEW"
QUEUE_STATUS_NOT_READY = "NOT_READY"
QUEUE_STATUS_READY_TO_MARK = "READY_TO_MARK"
QUEUE_STATUS_ERP_MARKED = "ERP_MARKED"
QUEUE_STATUS_EMAIL_SENT = "EMAIL_SENT"
QUEUE_STATUS_MANUAL_REVIEW = "MANUAL_REVIEW"
QUEUE_STATUS_ERROR = "ERROR"

QUEUE_STATUSES = {
    QUEUE_STATUS_NEW,
    QUEUE_STATUS_NOT_READY,
    QUEUE_STATUS_READY_TO_MARK,
    QUEUE_STATUS_ERP_MARKED,
    QUEUE_STATUS_EMAIL_SENT,
    QUEUE_STATUS_MANUAL_REVIEW,
    QUEUE_STATUS_ERROR,
}


@dataclass
class ShipmentCandidate:
    system_order_no: str
    platform_order_no: str
    als_no: str
    shipment_tag_name: str
    tag_text: str = ""
    sku_text: str = ""
    customer_remark: str = ""
    status_text: str = ""
    receiver_email: str | None = None
    carrier: str | None = None
    international_tracking_no: str | None = None
    logistics_order_no: str | None = None
    actual_total: str | None = None
    chargeable_weight_kg: str | None = None
    package_count: int | None = None
    queue_status: str = QUEUE_STATUS_NEW
    last_error: str | None = None
    source_page: int | None = None
    source_scroll_top: int | None = None
    rowid: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class LogisticsDetail:
    als_no: str
    status_text: str = ""
    service_type: str | None = None
    logistics_order_no: str | None = None
    carrier: str | None = None
    international_tracking_no: str | None = None
    actual_total: str | None = None
    chargeable_weight_kg: str | None = None
    package_count: int | None = None
    source_url: str | None = None
    page_error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LogisticsReadinessDecision:
    queue_status: str
    should_continue: bool
    reason: str
    status_text: str = ""


@dataclass
class ReadyToMarkItem:
    system_order_no: str
    platform_order_no: str
    als_no: str
    logistics_order_no: str | None = None
    carrier: str | None = None
    international_tracking_no: str | None = None
    actual_total: str | None = None
    chargeable_weight_kg: str | None = None


@dataclass
class ErpMarkResult:
    system_order_no: str
    platform_order_no: str
    als_no: str
    erp_step: str = ""
    queue_status: str = QUEUE_STATUS_READY_TO_MARK
    last_error: str | None = None


@dataclass
class ErpMarkReport:
    status: str
    message: str = ""
    queue_path: str = ""
    dry_run: bool = True
    execute: bool = False
    total_count: int = 0
    marked_count: int = 0
    error_count: int = 0
    manual_review_count: int = 0
    results: list[ErpMarkResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LogisticsQueryResult:
    system_order_no: str
    platform_order_no: str
    als_no: str
    status_text: str = ""
    queue_status: str = QUEUE_STATUS_ERROR
    last_error: str | None = None
    detail: LogisticsDetail | None = None


@dataclass
class LogisticsWorkerReport:
    status: str
    message: str = ""
    queue_path: str = ""
    dry_run: bool = True
    update_queue: bool = False
    scanned_page_count: int = 0
    parsed_count: int = 0
    ready_to_mark_count: int = 0
    not_ready_count: int = 0
    manual_review_count: int = 0
    error_count: int = 0
    query_results: list[LogisticsQueryResult] = field(default_factory=list)
    ready_to_mark_items: list[ReadyToMarkItem] = field(default_factory=list)
    skipped_query_records: list[QueueStatusRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ManualReviewItem:
    system_order_no: str
    platform_order_no: str
    reason: str
    als_numbers: list[str] = field(default_factory=list)
    selected_als_no: str | None = None
    message: str = ""


@dataclass
class DuplicateShipmentItem:
    system_order_no: str
    platform_order_no: str
    als_no: str
    existing_system_order_no: str | None = None
    existing_platform_order_no: str | None = None
    existing_queue_status: str | None = None
    existing_last_error: str | None = None


@dataclass
class QueueStatusRecord:
    system_order_no: str
    platform_order_no: str
    als_no: str
    queue_status: str
    last_error: str | None = None


@dataclass
class ShipmentScanReport:
    status: str
    message: str = ""
    shipment_tag_name: str = ""
    queue_path: str = ""
    dry_run: bool = True
    scanned_row_count: int = 0
    tagged_row_count: int = 0
    valid_als_row_count: int = 0
    enqueued_count: int = 0
    duplicate_skipped_count: int = 0
    manual_review_count: int = 0
    candidates: list[ShipmentCandidate] = field(default_factory=list)
    enqueued_candidates: list[ShipmentCandidate] = field(default_factory=list)
    duplicate_skipped: list[DuplicateShipmentItem] = field(default_factory=list)
    manual_reviews: list[ManualReviewItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scan_log_file: str | None = None
