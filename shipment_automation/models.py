from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


IDENTITY_ACTIVE = "ACTIVE"
IDENTITY_CONFLICT = "CONFLICT"
IDENTITY_CANCELLED = "CANCELLED"
IDENTITY_PAUSED_TAG_REMOVED = "PAUSED_TAG_REMOVED"
IDENTITY_MANUALLY_CANCELLED = "MANUALLY_CANCELLED"
IDENTITY_SUPERSEDED = "SUPERSEDED"

LOGISTICS_PENDING = "PENDING"
LOGISTICS_WAITING = "WAITING"
LOGISTICS_READY = "READY"
LOGISTICS_RETRYABLE = "RETRYABLE"
LOGISTICS_BLOCKED = "BLOCKED"

TRACKING_REVIEW_AUTO_RECHECK = "AUTO_RECHECK"
TRACKING_REVIEW_ORDER_ISSUE = "ORDER_ISSUE"

ERP_WAITING = "WAITING"
ERP_PENDING = "PENDING"
ERP_RUNNING = "RUNNING"
ERP_RETRYABLE = "RETRYABLE"
ERP_BLOCKED = "BLOCKED"
ERP_DONE = "DONE"

ERP_CHECKPOINT_NONE = "NONE"
ERP_CHECKPOINT_CHANNEL_SET = "CHANNEL_SET"
ERP_CHECKPOINT_AUDITED = "AUDITED"
ERP_CHECKPOINT_LOGISTICS_SAVED = "LOGISTICS_SAVED"
ERP_CHECKPOINT_OUTBOUNDED = "OUTBOUNDED"

ERP_COMPLETION_AUTOMATION = "AUTOMATION"
ERP_COMPLETION_MANUAL_DETECTED = "MANUAL_DETECTED"

EMAIL_PENDING = "PENDING"
EMAIL_RETRYABLE = "RETRYABLE"
EMAIL_BLOCKED = "BLOCKED"
EMAIL_SENT = "SENT"

STAGE_LOGISTICS = "logistics"
STAGE_ERP = "erp"
STAGE_EMAIL = "email"

SALES_CHANNEL_MARKETPLACE = "MARKETPLACE"
SALES_CHANNEL_INDEPENDENT_SITE = "INDEPENDENT_SITE"

@dataclass
class ShipmentCandidate:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    shipment_tag_name: str
    tag_text: str = ""
    sku_text: str = ""
    product_type: str = ""
    customer_remark: str = ""
    status_text: str = ""
    receiver_name: str | None = None
    receiver_email: str | None = None
    receiver_phone: str | None = None
    sales_platform_code: str | None = None
    sales_platform_name: str | None = None
    store_name: str | None = None
    site_name: str | None = None
    carrier: str | None = None
    international_tracking_no: str | None = None
    actual_total: str | None = None
    chargeable_weight_kg: str | None = None
    package_count: int | None = None
    source_page: int | None = None
    source_scroll_top: int | None = None
    rowid: str | None = None
    sales_channel: str | None = None
    customer_email_required: bool | None = None
    warnings: list[str] = field(default_factory=list)

@dataclass
class LogisticsDetail:
    logistics_no: str
    status_text: str = ""
    service_type: str | None = None
    service_line: str | None = None
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
    logistics_state: str
    should_continue: bool
    reason: str
    status_text: str = ""


@dataclass
class ReadyToMarkItem:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    carrier: str | None = None
    service_line: str | None = None
    international_tracking_no: str | None = None
    actual_total: str | None = None
    chargeable_weight_kg: str | None = None
    job_id: int | None = None
    version: int = 0
    lease_owner: str | None = None
    erp_state: str = ERP_PENDING
    erp_checkpoint: str = ERP_CHECKPOINT_NONE
    channel_payload_hash: str | None = None
    logistics_payload_hash: str | None = None
    selected_wms_wo_number: str | None = None
    selected_wms_candidates_hash: str | None = None
    sales_channel: str = SALES_CHANNEL_MARKETPLACE
    customer_email_required: bool = True
    tracking_manually_verified: bool = False


@dataclass(frozen=True)
class ShipmentStatusChangeSummary:
    requested_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    missing_count: int = 0
    changed_logistics_nos: tuple[str, ...] = ()
    skipped_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def skipped_count(self) -> int:
        return self.unchanged_count + self.missing_count

@dataclass
class ErpMarkResult:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    erp_step: str = ""
    last_error: str | None = None
    erp_state: str = ERP_PENDING
    erp_checkpoint: str = ERP_CHECKPOINT_NONE
    carrier: str | None = None
    international_tracking_no: str | None = None
    sales_channel: str = SALES_CHANNEL_MARKETPLACE
    customer_email_required: bool = True


@dataclass
class StoreFulfillmentReminder:
    independent_order_no: str
    system_order_no: str
    logistics_no: str
    carrier: str | None = None
    international_tracking_no: str | None = None
    message: str = "ERP 已标发出库，请在店小秘标发该独立站订单。"


@dataclass
class ErpMarkReport:
    status: str
    message: str = ""
    queue_path: str = ""
    dry_run: bool = True
    execute: bool = False
    total_count: int = 0
    done_count: int = 0
    skipped_count: int = 0
    tracking_blocked_count: int = 0
    retryable_count: int = 0
    blocked_count: int = 0
    paused_count: int = 0
    results: list[ErpMarkResult] = field(default_factory=list)
    store_fulfillment_reminders: list[StoreFulfillmentReminder] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LogisticsQueryResult:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    status_text: str = ""
    last_error: str | None = None
    detail: LogisticsDetail | None = None
    logistics_state: str = LOGISTICS_RETRYABLE


@dataclass
class QueueStatusRecord:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    last_error: str | None = None
    identity_state: str = IDENTITY_ACTIVE
    logistics_state: str = LOGISTICS_PENDING
    erp_state: str = ERP_WAITING
    erp_checkpoint: str = ERP_CHECKPOINT_NONE
    email_state: str | None = None
    attempt_count: int = 0
    stage_state: str = ""


@dataclass
class LogisticsWorkerReport:
    status: str
    message: str = ""
    queue_path: str = ""
    dry_run: bool = True
    update_queue: bool = False
    target_count: int = 0
    batch_count: int = 0
    scanned_page_count: int = 0
    parsed_count: int = 0
    ready_count: int = 0
    waiting_count: int = 0
    blocked_count: int = 0
    retryable_count: int = 0
    parser_artifact_requeued_count: int = 0
    query_results: list[LogisticsQueryResult] = field(default_factory=list)
    ready_to_mark_items: list[ReadyToMarkItem] = field(default_factory=list)
    skipped_query_records: list[QueueStatusRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ManualReviewItem:
    system_order_no: str
    platform_order_no: str
    reason: str
    logistics_numbers: list[str] = field(default_factory=list)
    selected_logistics_no: str | None = None
    message: str = ""


@dataclass
class DuplicateShipmentItem:
    system_order_no: str
    platform_order_no: str
    logistics_no: str
    existing_system_order_no: str | None = None
    existing_platform_order_no: str | None = None
    existing_identity_state: str | None = None
    existing_logistics_state: str | None = None
    existing_erp_state: str | None = None
    existing_last_error: str | None = None
    conflict: bool = False
    immediate_logistics: bool = False
    immediate_erp: bool = False


@dataclass
class ManualCompletionItem:
    system_order_no: str
    platform_order_no: str
    logistics_no: str


@dataclass
class ShipmentScanReport:
    status: str
    message: str = ""
    shipment_tag_name: str = ""
    queue_path: str = ""
    dry_run: bool = True
    scanned_row_count: int = 0
    tagged_row_count: int = 0
    valid_logistics_row_count: int = 0
    enqueued_count: int = 0
    refreshed_count: int = 0
    immediate_logistics_count: int = 0
    immediate_erp_count: int = 0
    conflict_count: int = 0
    duplicate_skipped_count: int = 0
    manual_review_count: int = 0
    table_total_count: int | None = None
    scan_complete: bool = False
    incomplete_field_count: int = 0
    manual_completed_count: int = 0
    candidates: list[ShipmentCandidate] = field(default_factory=list)
    enqueued_candidates: list[ShipmentCandidate] = field(default_factory=list)
    duplicate_skipped: list[DuplicateShipmentItem] = field(default_factory=list)
    manual_reviews: list[ManualReviewItem] = field(default_factory=list)
    manual_completed: list[ManualCompletionItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scan_log_file: str | None = None


@dataclass
class EmailBatchPreview:
    id: int
    platform_order_no: str
    sequence_no: int
    state: str
    recipient_email: str | None
    message_id: str
    logistics_numbers: list[str] = field(default_factory=list)
    tracking_numbers: list[str | None] = field(default_factory=list)
    last_error: str | None = None


@dataclass
class QueueEvent:
    id: int
    job_id: int | None
    batch_id: int | None
    stage: str
    event_type: str
    old_state: str | None
    new_state: str | None
    message: str | None
    details: dict[str, Any]
    run_id: str | None
    created_at: str
