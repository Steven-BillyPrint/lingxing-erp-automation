from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from lingxing_automation.products.catalog import (
    identify_product_types_from_skus,
    preferred_product_type,
)

from .alibaba_logistics import (
    REAL_OVERSEAS_CARRIER_DISPLAY_NAMES,
    TRACKING_MISMATCH_REASON_PREFIX,
    classify_tracking_candidate,
    is_tracking_number_mismatch_reason,
    is_obvious_tracking_parser_artifact,
    normalize_carrier_name,
    normalize_tracking_number,
    tracking_number_matches_carrier,
    tracking_number_mismatch_reason,
)
from .erp_mark_policy import (
    AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL,
    amazon_main_image_policy_violation,
)

from .models import (
    CUSTOMER_SHIPPING_EXPEDITED,
    CUSTOMER_SHIPPING_STANDARD,
    EMAIL_BLOCKED,
    EMAIL_PENDING,
    EMAIL_RETRYABLE,
    EMAIL_SENT,
    ERP_BLOCKED,
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_NONE,
    ERP_CHECKPOINT_OUTBOUNDED,
    ERP_COMPLETION_AUTOMATION,
    ERP_COMPLETION_MANUAL_DETECTED,
    ERP_DONE,
    ERP_PENDING,
    ERP_RETRYABLE,
    ERP_RUNNING,
    ERP_WAITING,
    EmailBatchPreview,
    IDENTITY_ACTIVE,
    IDENTITY_CANCELLED,
    IDENTITY_CONFLICT,
    IDENTITY_MANUALLY_CANCELLED,
    IDENTITY_PAUSED_TAG_REMOVED,
    IDENTITY_SUPERSEDED,
    LOGISTICS_BLOCKED,
    LOGISTICS_PENDING,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    LogisticsDetail,
    ManualCompletionItem,
    QueueEvent,
    QueueStatusRecord,
    ReadyToMarkItem,
    SALES_CHANNEL_INDEPENDENT_SITE,
    SALES_CHANNEL_MARKETPLACE,
    ShipmentCandidate,
    ShipmentStatusChangeSummary,
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
    normalize_customer_shipping_service,
    shipment_tracking_attention_notice,
    shipment_tracking_deadline,
)


SCHEMA_VERSION = 22
CUSTOMER_SHIPPING_SERVICE_SCAN_ISSUE = "customer_shipping_service_unavailable"
SCAN_ISSUE_KEY_PREFIX = "scan-issue:"
SCAN_ISSUE_ACTIVE = "ACTIVE"
SCAN_ISSUE_MANUAL_REVIEW = "MANUAL_REVIEW"
SCAN_ISSUE_MANUALLY_COMPLETED = "MANUALLY_COMPLETED"
SCAN_ISSUE_MANUALLY_CANCELLED = "MANUALLY_CANCELLED"
SCAN_ISSUE_MANAGED_STATES = frozenset(
    {
        SCAN_ISSUE_ACTIVE,
        SCAN_ISSUE_MANUAL_REVIEW,
        SCAN_ISSUE_MANUALLY_COMPLETED,
        SCAN_ISSUE_MANUALLY_CANCELLED,
    }
)
DEFAULT_RETRY_HOURS = 3
PRODUCT_IDENTITY_RETRY_BASE_MINUTES = 15
PRODUCT_IDENTITY_RETRY_MAX_HOURS = 6
LEGACY_NEW = "NEW"
LEGACY_NOT_READY = "NOT_READY"
LEGACY_READY_TO_MARK = "READY_TO_MARK"
LEGACY_ERP_MARKED = "ERP_MARKED"
LEGACY_EMAIL_SENT = "EMAIL_SENT"
LEGACY_MANUAL_REVIEW = "MANUAL_REVIEW"
LEGACY_ERROR = "ERROR"
BROWSER_CLOSED_KEYWORDS = (
    "target page, context or browser has been closed",
    "browsercontext.new_page",
    "page.wait_for_timeout",
    "browser has been closed",
    "context has been closed",
    "nonetype' object has no attribute 'new_page",
    "浏览器关闭",
)
MANUAL_COMPLETION_V3_SYSTEM_ORDERS = {
    "103717510103539424",
    "103717610707553280",
    "103716991507624096",
    "103715792366366193",
    "103715662040298036",
    "103715553728922198",
}
INDEPENDENT_SITE_ORDER_RE = re.compile(r"^wc\d+$", re.I)
EMAIL_INCOMPLETE_PACKAGES_REASON = (
    "同一平台订单仍有未完成的已知非冲突包裹，邮件预览已阻止。"
)
EMAIL_NO_AUTOMATION_PACKAGES_REASON = (
    "同一平台订单当前没有可生成邮件预览的自动化完成包裹，邮件预览已阻止。"
)
EMAIL_CONFLICT_PACKAGES_REASON = (
    "同一平台订单仍有订单归属冲突包裹，邮件预览已阻止并等待人工解决。"
)
EMAIL_MISSING_RECEIVER_REASON = "邮件预览未生成：缺少收件邮箱（不影响 ERP 标发）。"
EMAIL_CONFLICTING_RECEIVERS_REASON = (
    "邮件预览未生成：同一平台订单存在多个收件邮箱（不影响 ERP 标发）。"
)
LEGACY_EMAIL_MISSING_RECEIVER_REASON = "Missing receiver email."
LEGACY_EMAIL_CONFLICTING_RECEIVERS_REASON = (
    "Conflicting receiver emails for the same platform order."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(hours: float = DEFAULT_RETRY_HOURS) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _has_live_lease(row: sqlite3.Row | dict[str, Any], *, now: str) -> bool:
    """Return whether a queue row is still owned by an unexpired worker lease.

    Repeat scans may make a scheduled retry immediately due, but must never
    revoke a worker that is already inside a logistics lookup or ERP workflow.
    Malformed non-empty lease timestamps are treated as live (fail closed) so a
    scan cannot create a second concurrent writer merely because old data is
    unexpected.
    """

    owner = str(row["lease_owner"] or "").strip()
    lease_until = str(row["lease_until"] or "").strip()
    if not owner:
        return False
    if not lease_until:
        return True
    try:
        expires_at = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
        observed_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    return expires_at > observed_at


def _parse_legacy_timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _split_money(value: Any) -> tuple[str | None, str | None]:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None, None
    currency_match = re.search(r"\b([A-Z]{3})\b", text.upper())
    amount_match = re.search(r"-?\d+(?:\.\d+)?", text)
    return (currency_match.group(1) if currency_match else None, amount_match.group(0) if amount_match else None)


def _normalize_decimal(value: Any) -> str | None:
    text = str(value or "").strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return format(Decimal(match.group(0)), "f")
    except InvalidOperation:
        return None


def _legacy_logistics_complete(row: sqlite3.Row | dict[str, Any]) -> bool:
    return all(
        str(row[key] or "").strip()
        for key in ("carrier", "international_tracking_no", "actual_total", "chargeable_weight_kg")
    )


def _is_browser_closed_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(keyword in text for keyword in BROWSER_CLOSED_KEYWORDS)


def _blocked_logistics_can_refresh_on_candidate_reseen(
    row: sqlite3.Row | dict[str, Any],
) -> bool:
    """Return whether a fresh ERP sighting may re-read blocked Alibaba facts.

    A logistics block is a snapshot of what the Alibaba detail page returned at
    one point in time.  Carrier, international tracking number, and page data
    can all be corrected later without changing the ALS number.  Keeping those
    rows permanently blocked made repeat scans preserve stale values such as an
    old ``Unknown`` carrier indefinitely.

    The one fail-closed exception is an operator's explicit ``ORDER_ISSUE``
    decision.  That is a durable human stop, not a transient external fact, and
    a routine scan must not override it.
    """

    return (
        str(row["logistics_state"] or "") == LOGISTICS_BLOCKED
        and str(row["tracking_mismatch_action"] or "")
        != TRACKING_REVIEW_ORDER_ISSUE
    )


def _is_missing_expected_order_error(value: Any) -> bool:
    text = str(value or "")
    return "没有在列表中找到系统单号" in text or "找不到系统单号" in text


def normalize_sales_channel(platform_order_no: str | None) -> str:
    text = str(platform_order_no or "").strip()
    if INDEPENDENT_SITE_ORDER_RE.fullmatch(text):
        return SALES_CHANNEL_INDEPENDENT_SITE
    return SALES_CHANNEL_MARKETPLACE


def customer_email_required_for_sales_channel(sales_channel: str | None) -> bool:
    return sales_channel != SALES_CHANNEL_INDEPENDENT_SITE


@dataclass
class QueueInsertResult:
    inserted: bool
    candidate: ShipmentCandidate
    existing: dict[str, Any] | None = None
    conflict: bool = False
    immediate_logistics: bool = False
    immediate_erp: bool = False
    auto_resumed: bool = False


@dataclass(frozen=True)
class TagSnapshotReconcileResult:
    snapshot_complete: bool
    paused_count: int = 0
    resumed_count: int = 0
    immediate_logistics_count: int = 0
    immediate_erp_count: int = 0
    paused_logistics_numbers: tuple[str, ...] = ()
    resumed_logistics_numbers: tuple[str, ...] = ()


class ShipmentWorkflowStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_v1_backup = False
        needs_v3_migration = False
        needs_v4_migration = False
        needs_v5_migration = False
        needs_v6_migration = False
        needs_v7_migration = False
        needs_v8_migration = False
        needs_v9_migration = False
        needs_v10_migration = False
        needs_v11_migration = False
        needs_v12_migration = False
        needs_v13_migration = False
        needs_v14_migration = False
        needs_v15_migration = False
        needs_v16_migration = False
        needs_v17_migration = False
        needs_v18_migration = False
        needs_v19_migration = False
        needs_v20_migration = False
        needs_v21_migration = False
        needs_v22_migration = False
        if self.path.exists():
            with self.connect() as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                needs_v1_backup = "shipment_jobs" not in names and bool({"shipment_queue", "shipment_queue_v1"} & names)
                if "shipment_erp" in names:
                    erp_columns = self._table_columns(conn, "shipment_erp")
                    job_columns = self._table_columns(conn, "shipment_jobs") if "shipment_jobs" in names else set()
                    logistics_columns = self._table_columns(conn, "shipment_logistics") if "shipment_logistics" in names else set()
                    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    needs_v3_migration = (
                        current_version < 3
                        or not {"completion_source", "externally_completed_at"}.issubset(erp_columns)
                    )
                    needs_v4_migration = (
                        current_version < 4
                        or not {"sales_channel", "customer_email_required"}.issubset(job_columns)
                    )
                    needs_v5_migration = (
                        current_version < 5
                        or not {
                            "tracking_override_carrier",
                            "tracking_override_no",
                            "tracking_override_at",
                            "tracking_override_reason",
                        }.issubset(logistics_columns)
                    )
                    needs_v6_migration = (
                        current_version < 6
                        or not {
                            "tracking_mismatch_action",
                            "tracking_mismatch_reviewed_at",
                        }.issubset(logistics_columns)
                    )
                    needs_v7_migration = (
                        current_version < 7
                        or "product_type" not in job_columns
                    )
                    needs_v8_migration = (
                        current_version < 8
                        or "shipment_notifications" not in names
                        or "shipment_order_contacts" not in names
                        or "shipment_package_snapshots" not in names
                    )
                    notification_columns = (
                        self._table_columns(conn, "shipment_notifications")
                        if "shipment_notifications" in names
                        else set()
                    )
                    contact_columns = (
                        self._table_columns(conn, "shipment_order_contacts")
                        if "shipment_order_contacts" in names
                        else set()
                    )
                    needs_v9_migration = (
                        current_version < 9
                        or "shipment_notification_exclusions" not in names
                        or not {"erp_completed_at", "state_changed_at"}.issubset(
                            notification_columns
                        )
                        or not {
                            "recipient_name_source",
                            "email_source",
                            "phone_source",
                            "contact_captured_at",
                        }.issubset(contact_columns)
                    )
                    notification_item_columns = (
                        self._table_columns(conn, "shipment_notification_items")
                        if "shipment_notification_items" in names
                        else set()
                    )
                    needs_v10_migration = (
                        current_version < 10
                        or "body_html" not in notification_columns
                        or "tracking_url" not in notification_item_columns
                    )
                    needs_v11_migration = (
                        current_version < 11
                        or not {
                            "selected_wms_wo_number",
                            "selected_wms_candidates_hash",
                            "selected_wms_selected_at",
                            "selected_wms_selected_by",
                        }.issubset(erp_columns)
                    )
                    package_columns = (
                        self._table_columns(conn, "shipment_package_snapshots")
                        if "shipment_package_snapshots" in names
                        else set()
                    )
                    product_columns = (
                        self._table_columns(conn, "shipment_order_product_snapshots")
                        if "shipment_order_product_snapshots" in names
                        else set()
                    )
                    needs_v12_migration = (
                        current_version < 12
                        or "shipment_order_product_snapshots" not in names
                        or "source_sequence" not in product_columns
                        or "product_names_json" not in notification_columns
                        or not {"customer_visible", "visibility_reason"}.issubset(
                            package_columns
                        )
                        or not {"customer_visible", "visibility_reason"}.issubset(
                            notification_item_columns
                        )
                    )
                    needs_v13_migration = (
                        current_version < 13
                        or "service_line" not in logistics_columns
                    )
                    needs_v14_migration = (
                        current_version < 14
                        or not {
                            "provider_operator_email",
                            "receipt_next_check_at",
                            "receipt_last_checked_at",
                            "receipt_deadline_at",
                            "receipt_check_attempt_count",
                            "receipt_check_lease_owner",
                            "receipt_check_lease_until",
                        }.issubset(notification_columns)
                    )
                    needs_v15_migration = (
                        current_version < 15
                        or "shipment_notification_recipient_name_choices"
                        not in names
                    )
                    needs_v16_migration = (
                        current_version < 16
                        or "state_changed_at" not in job_columns
                        or "state_changed_at" not in logistics_columns
                        or "state_changed_at" not in erp_columns
                    )
                    needs_v17_migration = (
                        current_version < 17
                        or "customer_shipping_service" not in job_columns
                    )
                    needs_v18_migration = (
                        current_version < 18
                        or not {
                            "product_identity_catalog_version",
                            "product_identity_checked_at",
                        }.issubset(job_columns)
                    )
                    needs_v19_migration = (
                        current_version < 19
                        or not {
                            "product_identity_retry_count",
                            "product_identity_next_retry_at",
                            "product_identity_last_error",
                        }.issubset(job_columns)
                        or "marketplace_product_id" not in product_columns
                    )
                    needs_v20_migration = (
                        current_version < 20
                        or "logistics_overdue_at" not in job_columns
                    )
                    scan_issue_columns = (
                        self._table_columns(conn, "shipment_scan_issues")
                        if "shipment_scan_issues" in names
                        else set()
                    )
                    needs_v21_migration = (
                        current_version < 21
                        or not {
                            "management_state",
                            "management_reason",
                            "management_updated_at",
                        }.issubset(scan_issue_columns)
                        or "shipment_scan_issue_events" not in names
                    )
                    needs_v22_migration = (
                        current_version < 22
                        or not {
                            "sales_platform_code",
                            "sales_platform_name",
                            "has_main_image",
                        }.issubset(job_columns)
                        or "policy_block_code" not in erp_columns
                    )
        if needs_v1_backup:
            self._backup_before_v2()
        elif needs_v3_migration:
            self._backup_before_v3()
        elif needs_v4_migration:
            self._backup_before_v4()
        elif needs_v5_migration:
            self._backup_before_v5()
        elif needs_v6_migration:
            self._backup_before_v6()
        elif needs_v7_migration:
            self._backup_before_v7()
        elif needs_v8_migration:
            self._backup_before_v8()
        elif needs_v9_migration:
            self._backup_before_v9()
        elif needs_v10_migration:
            self._backup_before_v10()
        elif needs_v11_migration:
            self._backup_before_v11()
        elif needs_v12_migration:
            self._backup_before_v12()
        elif needs_v13_migration:
            self._backup_before_v13()
        elif needs_v14_migration:
            self._backup_before_v14()
        elif needs_v15_migration:
            self._backup_before_v15()
        elif needs_v16_migration:
            self._backup_before_v16()
        elif needs_v17_migration:
            self._backup_before_v17()
        elif needs_v18_migration:
            self._backup_before_v18()
        elif needs_v19_migration:
            self._backup_before_v19()
        elif needs_v20_migration:
            self._backup_before_v20()
        elif needs_v21_migration:
            self._backup_before_v21()
        elif needs_v22_migration:
            self._backup_before_v22()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            try:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self._create_v2_schema(conn)
                if "shipment_queue" in names and "shipment_queue_v1" not in names:
                    conn.execute("ALTER TABLE shipment_queue RENAME TO shipment_queue_v1")
                legacy_count = conn.execute("SELECT COUNT(*) FROM shipment_queue_v1").fetchone()[0] if self._table_exists(conn, "shipment_queue_v1") else 0
                current_count = conn.execute("SELECT COUNT(*) FROM shipment_jobs").fetchone()[0]
                if legacy_count and current_count == 0:
                    self._migrate_v1(conn)
                    migrated_count = conn.execute("SELECT COUNT(*) FROM shipment_jobs").fetchone()[0]
                    if migrated_count != legacy_count:
                        raise RuntimeError(f"Shipment queue migration count mismatch: {legacy_count} != {migrated_count}")
                self._migrate_to_v3(
                    conn,
                    migrate_missing_errors=needs_v1_backup or needs_v3_migration,
                )
                self._migrate_to_v4(conn)
                self._migrate_to_v5(conn)
                self._migrate_to_v6(conn)
                self._migrate_to_v7(conn)
                self._migrate_to_v11(conn)
                self._migrate_to_v13(
                    conn,
                    requery_missing_service_lines=needs_v13_migration,
                )
                self._migrate_to_v16(conn)
                self._migrate_to_v17(conn)
                self._migrate_to_v18(conn)
                self._migrate_to_v19(conn)
                # V20 reconciliation reads the aggregate projection, which
                # includes the V22 structured policy column.
                self._migrate_to_v22(conn)
                self._migrate_to_v20(conn)
                self._migrate_to_v21(conn)
                from .notification_store import initialize_notification_schema

                initialize_notification_schema(conn)
                self._refresh_order_policy_evidence_conn(conn)
                self._reconcile_amazon_main_image_policy_conn(conn)
                self._protect_legacy_table(conn)
                self._reconcile_duplicate_business_identities_conn(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._initialized = True

    def _reconcile_duplicate_business_identities_conn(
        self, conn: sqlite3.Connection
    ) -> int:
        """Hide legacy duplicate rows for one ERP child order without deleting history.

        A platform order may legitimately contain several split system orders, so
        the business identity is the pair ``(platform_order_no,
        system_order_no)``.  Older builds keyed only by ALS and therefore added
        another visible row whenever the customer remark was corrected to a new
        ALS number.  Keep the strongest row and mark its siblings as superseded.
        """

        groups = conn.execute(
            """
            SELECT j.platform_order_no, j.system_order_no
            FROM shipment_jobs j
            WHERE j.identity_state <> ?
            GROUP BY j.platform_order_no, j.system_order_no
            HAVING COUNT(*) > 1
            """,
            (IDENTITY_SUPERSEDED,),
        ).fetchall()
        changed = 0
        now = utc_now()
        for group in groups:
            rows = conn.execute(
                """
                SELECT j.id, j.logistics_no, j.identity_state,
                       l.state AS logistics_state, e.state AS erp_state
                FROM shipment_jobs j
                JOIN shipment_logistics l ON l.job_id = j.id
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.platform_order_no = ? AND j.system_order_no = ?
                  AND j.identity_state <> ?
                ORDER BY
                    CASE WHEN e.state = ? THEN 0 ELSE 1 END,
                    CASE WHEN j.identity_state = ? THEN 0 ELSE 1 END,
                    CASE WHEN l.state = ? THEN 0 ELSE 1 END,
                    j.id DESC
                """,
                (
                    group["platform_order_no"],
                    group["system_order_no"],
                    IDENTITY_SUPERSEDED,
                    ERP_DONE,
                    IDENTITY_ACTIVE,
                    LOGISTICS_READY,
                ),
            ).fetchall()
            if len(rows) < 2:
                continue
            survivor = rows[0]
            for obsolete in rows[1:]:
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, lease_owner = NULL, lease_stage = NULL,
                        lease_until = NULL, updated_at = ?, version = version + 1
                    WHERE id = ? AND identity_state <> ?
                    """,
                    (
                        IDENTITY_SUPERSEDED,
                        now,
                        obsolete["id"],
                        IDENTITY_SUPERSEDED,
                    ),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(obsolete["id"]),
                    stage="identity",
                    event_type="DUPLICATE_BUSINESS_IDENTITY_SUPERSEDED",
                    old_state=str(obsolete["identity_state"]),
                    new_state=IDENTITY_SUPERSEDED,
                    message="同一平台单号和系统单号存在多条历史 ALS 记录，已保留有效记录并隐藏旧记录。",
                    details={
                        "survivor_job_id": int(survivor["id"]),
                        "survivor_logistics_no": str(survivor["logistics_no"]),
                        "superseded_logistics_no": str(obsolete["logistics_no"]),
                    },
                )
                changed += 1
        return changed

    def _backup_before_v2(self) -> Path:
        return self._backup_before_version("v2")

    def _backup_before_v3(self) -> Path:
        return self._backup_before_version("v3")

    def _backup_before_v4(self) -> Path:
        return self._backup_before_version("v4")

    def _backup_before_v5(self) -> Path:
        return self._backup_before_version("v5")

    def _backup_before_v6(self) -> Path:
        return self._backup_before_version("v6")

    def _backup_before_v7(self) -> Path:
        return self._backup_before_version("v7")

    def _backup_before_v8(self) -> Path:
        return self._backup_before_version("v8")

    def _backup_before_v9(self) -> Path:
        return self._backup_before_version("v9")

    def _backup_before_v10(self) -> Path:
        return self._backup_before_version("v10")

    def _backup_before_v11(self) -> Path:
        return self._backup_before_version("v11")

    def _backup_before_v12(self) -> Path:
        return self._backup_before_version("v12")

    def _backup_before_v13(self) -> Path:
        return self._backup_before_version("v13")

    def _backup_before_v14(self) -> Path:
        return self._backup_before_version("v14")

    def _backup_before_v15(self) -> Path:
        return self._backup_before_version("v15")

    def _backup_before_v16(self) -> Path:
        return self._backup_before_version("v16")

    def _backup_before_v17(self) -> Path:
        return self._backup_before_version("v17")

    def _backup_before_v18(self) -> Path:
        return self._backup_before_version("v18")

    def _backup_before_v19(self) -> Path:
        return self._backup_before_version("v19")

    def _backup_before_v20(self) -> Path:
        return self._backup_before_version("v20")

    def _backup_before_v21(self) -> Path:
        return self._backup_before_version("v21")

    def _backup_before_v22(self) -> Path:
        return self._backup_before_version("v22")

    def _backup_before_version(self, version: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = self.path.with_name(f"{self.path.stem}.pre_{version}_{stamp}{self.path.suffix}")
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return backup_path

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone() is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}

    @staticmethod
    def _create_v2_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS shipment_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                logistics_no TEXT NOT NULL UNIQUE,
                system_order_no TEXT NOT NULL,
                platform_order_no TEXT NOT NULL,
                shipment_tag_name TEXT NOT NULL,
                tag_text TEXT,
                sku_text TEXT,
                product_type TEXT,
                product_identity_catalog_version TEXT,
                product_identity_checked_at TEXT,
                product_identity_retry_count INTEGER NOT NULL DEFAULT 0,
                product_identity_next_retry_at TEXT,
                product_identity_last_error TEXT,
                customer_remark TEXT,
                source_status_text TEXT,
                customer_shipping_service TEXT,
                receiver_email TEXT,
                source_page INTEGER,
                source_scroll_top INTEGER,
                source_rowid TEXT,
                sales_platform_code TEXT NOT NULL DEFAULT '',
                sales_platform_name TEXT NOT NULL DEFAULT '',
                has_main_image INTEGER NOT NULL DEFAULT 0,
                sales_channel TEXT NOT NULL DEFAULT 'MARKETPLACE',
                customer_email_required INTEGER NOT NULL DEFAULT 1,
                identity_state TEXT NOT NULL DEFAULT 'ACTIVE',
                first_seen_at TEXT NOT NULL,
                logistics_overdue_at TEXT,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state_changed_at TEXT,
                cancelled_at TEXT,
                lease_owner TEXT,
                lease_stage TEXT,
                lease_until TEXT,
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_jobs_platform ON shipment_jobs(platform_order_no);
            CREATE INDEX IF NOT EXISTS idx_shipment_jobs_lease ON shipment_jobs(lease_stage, lease_until);

            CREATE TABLE IF NOT EXISTS shipment_scan_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_order_no TEXT NOT NULL,
                platform_order_no TEXT NOT NULL,
                issue_code TEXT NOT NULL,
                shipment_tag_name TEXT NOT NULL,
                tag_text TEXT,
                source_status_text TEXT,
                error_message TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                resolved_at TEXT,
                management_state TEXT NOT NULL DEFAULT 'ACTIVE',
                management_reason TEXT,
                management_updated_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(system_order_no, platform_order_no, issue_code)
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_scan_issues_active
                ON shipment_scan_issues(resolved_at, updated_at);

            CREATE TABLE IF NOT EXISTS shipment_scan_issue_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id INTEGER NOT NULL REFERENCES shipment_scan_issues(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                old_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                run_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_scan_issue_events_issue
                ON shipment_scan_issue_events(issue_id, id);

            CREATE TABLE IF NOT EXISTS shipment_logistics (
                job_id INTEGER PRIMARY KEY REFERENCES shipment_jobs(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                alibaba_status TEXT,
                service_type TEXT,
                service_line TEXT,
                carrier_raw TEXT,
                carrier_normalized TEXT,
                international_tracking_no TEXT,
                currency TEXT,
                fee_amount TEXT,
                chargeable_weight_kg TEXT,
                package_count INTEGER,
                source_url TEXT,
                tracking_override_carrier TEXT,
                tracking_override_no TEXT,
                tracking_override_at TEXT,
                tracking_override_reason TEXT,
                tracking_mismatch_action TEXT,
                tracking_mismatch_reviewed_at TEXT,
                last_checked_at TEXT,
                next_attempt_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                state_changed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_logistics_due ON shipment_logistics(state, next_attempt_at);

            CREATE TABLE IF NOT EXISTS shipment_erp (
                job_id INTEGER PRIMARY KEY REFERENCES shipment_jobs(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                channel_path TEXT,
                freight_amount TEXT,
                chargeable_weight_g TEXT,
                channel_payload_hash TEXT,
                logistics_payload_hash TEXT,
                channel_confirmed_at TEXT,
                logistics_confirmed_at TEXT,
                channel_set_at TEXT,
                audited_at TEXT,
                logistics_saved_at TEXT,
                outbounded_at TEXT,
                next_attempt_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                policy_block_code TEXT,
                completion_source TEXT,
                externally_completed_at TEXT,
                selected_wms_wo_number TEXT,
                selected_wms_candidates_hash TEXT,
                selected_wms_selected_at TEXT,
                selected_wms_selected_by TEXT,
                updated_at TEXT NOT NULL,
                state_changed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_erp_due ON shipment_erp(state, next_attempt_at);

            CREATE TABLE IF NOT EXISTS shipment_email_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_order_no TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                state TEXT NOT NULL,
                recipient_email TEXT,
                message_id TEXT NOT NULL UNIQUE,
                template_version TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                subject_preview TEXT,
                body_preview TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform_order_no, sequence_no)
            );

            CREATE TABLE IF NOT EXISTS shipment_email_batch_items (
                batch_id INTEGER NOT NULL REFERENCES shipment_email_batches(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES shipment_jobs(id) ON DELETE RESTRICT,
                logistics_no TEXT NOT NULL,
                carrier TEXT,
                international_tracking_no TEXT,
                PRIMARY KEY(batch_id, job_id)
            );

            CREATE TABLE IF NOT EXISTS shipment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER REFERENCES shipment_jobs(id) ON DELETE SET NULL,
                batch_id INTEGER REFERENCES shipment_email_batches(id) ON DELETE SET NULL,
                stage TEXT NOT NULL,
                event_type TEXT NOT NULL,
                old_state TEXT,
                new_state TEXT,
                message TEXT,
                details_json TEXT,
                run_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_events_job ON shipment_events(job_id, id);
            CREATE INDEX IF NOT EXISTS idx_shipment_events_batch ON shipment_events(batch_id, id);
            """
        )

    def _migrate_to_v3(self, conn: sqlite3.Connection, *, migrate_missing_errors: bool) -> None:
        columns = self._table_columns(conn, "shipment_erp")
        if "completion_source" not in columns:
            conn.execute("ALTER TABLE shipment_erp ADD COLUMN completion_source TEXT")
        if "externally_completed_at" not in columns:
            conn.execute("ALTER TABLE shipment_erp ADD COLUMN externally_completed_at TEXT")

        conn.execute(
            """
            UPDATE shipment_erp
            SET completion_source = ?
            WHERE state = ? AND (completion_source IS NULL OR completion_source = '')
            """,
            (ERP_COMPLETION_AUTOMATION, ERP_DONE),
        )
        if not migrate_missing_errors:
            return
        rows = conn.execute(
            """
            SELECT j.id AS job_id, j.system_order_no, j.platform_order_no, j.logistics_no,
                   e.state, e.last_error
            FROM shipment_jobs j
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE j.identity_state = ? AND e.state <> ? AND e.last_error IS NOT NULL
            """,
            (IDENTITY_ACTIVE, ERP_DONE),
        ).fetchall()
        now = utc_now()
        for row in rows:
            if (
                row["system_order_no"] not in MANUAL_COMPLETION_V3_SYSTEM_ORDERS
                and not _is_missing_expected_order_error(row["last_error"])
            ):
                continue
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, checkpoint = ?, outbounded_at = ?, next_attempt_at = NULL,
                    last_error = NULL, completion_source = ?, externally_completed_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    ERP_DONE, ERP_CHECKPOINT_OUTBOUNDED, now,
                    ERP_COMPLETION_MANUAL_DETECTED, now, now, row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, row["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=row["job_id"],
                stage="erp",
                event_type="MANUAL_COMPLETION_DETECTED",
                old_state=row["state"],
                new_state=ERP_DONE,
                message="历史记录显示订单已不在待审核列表，迁移为人工完成。",
                details={
                    "system_order_no": row["system_order_no"],
                    "platform_order_no": row["platform_order_no"],
                    "logistics_no": row["logistics_no"],
                    "previous_error": row["last_error"],
                    "source": "v3_migration",
                },
            )

    def _migrate_to_v4(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "shipment_jobs")
        if "sales_channel" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN sales_channel TEXT NOT NULL DEFAULT 'MARKETPLACE'"
            )
        if "customer_email_required" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN customer_email_required INTEGER NOT NULL DEFAULT 1"
            )
        rows = conn.execute("SELECT id, platform_order_no FROM shipment_jobs").fetchall()
        for row in rows:
            sales_channel = normalize_sales_channel(row["platform_order_no"])
            conn.execute(
                """
                UPDATE shipment_jobs
                SET sales_channel = ?, customer_email_required = ?
                WHERE id = ?
                """,
                (
                    sales_channel,
                    1 if customer_email_required_for_sales_channel(sales_channel) else 0,
                    row["id"],
                ),
            )

    def _migrate_to_v5(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "shipment_logistics")
        for column in (
            "tracking_override_carrier",
            "tracking_override_no",
            "tracking_override_at",
            "tracking_override_reason",
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE shipment_logistics ADD COLUMN {column} TEXT")

    def _migrate_to_v6(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "shipment_logistics")
        for column in ("tracking_mismatch_action", "tracking_mismatch_reviewed_at"):
            if column not in columns:
                conn.execute(f"ALTER TABLE shipment_logistics ADD COLUMN {column} TEXT")

    def _migrate_to_v7(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "shipment_jobs")
        if "product_type" not in columns:
            conn.execute("ALTER TABLE shipment_jobs ADD COLUMN product_type TEXT")

    def _migrate_to_v11(self, conn: sqlite3.Connection) -> None:
        columns = self._table_columns(conn, "shipment_erp")
        for column in (
            "selected_wms_wo_number",
            "selected_wms_candidates_hash",
            "selected_wms_selected_at",
            "selected_wms_selected_by",
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE shipment_erp ADD COLUMN {column} TEXT")

    def _migrate_to_v13(
        self,
        conn: sqlite3.Connection,
        *,
        requery_missing_service_lines: bool,
    ) -> None:
        columns = self._table_columns(conn, "shipment_logistics")
        if "service_line" not in columns:
            conn.execute("ALTER TABLE shipment_logistics ADD COLUMN service_line TEXT")
        if not requery_missing_service_lines:
            return

        rows = conn.execute(
            """
            SELECT j.id AS job_id, j.logistics_no, l.state AS logistics_state,
                   l.carrier_normalized, l.carrier_raw, e.state AS erp_state
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE j.identity_state = ?
              AND l.state = ?
              AND (l.service_line IS NULL OR TRIM(l.service_line) = '')
              AND e.checkpoint = ?
              AND e.state <> ?
            """,
            (
                IDENTITY_ACTIVE,
                LOGISTICS_READY,
                ERP_CHECKPOINT_NONE,
                ERP_DONE,
            ),
        ).fetchall()
        now = utc_now()
        for row in rows:
            carrier = normalize_carrier_name(
                row["carrier_normalized"] or row["carrier_raw"]
            )
            if carrier not in {"UPS", "FEDEX", "DHL"}:
                continue
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    LOGISTICS_RETRYABLE,
                    now,
                    "需要重新查询阿里服务线路后再选择 ERP 物流渠道。",
                    now,
                    row["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, next_attempt_at = NULL, last_error = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (ERP_WAITING, now, row["job_id"]),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, row["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=row["job_id"],
                stage="migration",
                event_type="SERVICE_LINE_REQUERY_REQUIRED",
                old_state=row["logistics_state"],
                new_state=LOGISTICS_RETRYABLE,
                message="旧任务缺少阿里服务线路，已排队重新读取；尚未写入 ERP 渠道。",
                details={
                    "logistics_no": row["logistics_no"],
                    "carrier": carrier,
                    "source": "v13_migration",
                },
            )

    def _migrate_to_v16(self, conn: sqlite3.Connection) -> None:
        """Keep business-state time separate from routine scan/activity time."""

        for table in ("shipment_jobs", "shipment_logistics", "shipment_erp"):
            if "state_changed_at" not in self._table_columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN state_changed_at TEXT")

        conn.execute(
            """
            UPDATE shipment_jobs AS j
            SET state_changed_at = COALESCE(
                (
                    SELECT MAX(ev.created_at)
                    FROM shipment_events ev
                    WHERE ev.job_id = j.id
                      AND ev.stage = 'identity'
                      AND ev.new_state = j.identity_state
                      AND (ev.old_state IS NULL OR ev.old_state <> ev.new_state)
                ),
                j.created_at,
                j.updated_at
            )
            WHERE state_changed_at IS NULL OR state_changed_at = ''
            """
        )
        conn.execute(
            """
            UPDATE shipment_logistics AS l
            SET state_changed_at = COALESCE(
                (
                    SELECT MAX(ev.created_at)
                    FROM shipment_events ev
                    WHERE ev.job_id = l.job_id
                      AND ev.stage IN ('candidate', 'logistics', 'migration')
                      AND (ev.old_state IS NULL OR ev.old_state <> ev.new_state)
                      AND (
                          ev.new_state = l.state
                          OR ev.new_state LIKE l.state || '/%'
                      )
                ),
                l.last_checked_at,
                l.updated_at
            )
            WHERE state_changed_at IS NULL OR state_changed_at = ''
            """
        )
        conn.execute(
            """
            UPDATE shipment_erp AS e
            SET state_changed_at = COALESCE(
                (
                    SELECT MAX(ev.created_at)
                    FROM shipment_events ev
                    WHERE ev.job_id = e.job_id
                      AND ev.stage IN ('erp', 'migration')
                      AND (ev.old_state IS NULL OR ev.old_state <> ev.new_state)
                      AND (
                          ev.new_state = e.state
                          OR ev.new_state LIKE '%/' || e.state
                          OR ev.new_state = e.checkpoint
                      )
                ),
                e.outbounded_at,
                e.externally_completed_at,
                e.logistics_saved_at,
                e.audited_at,
                e.channel_set_at,
                e.updated_at
            )
            WHERE state_changed_at IS NULL OR state_changed_at = ''
            """
        )

        triggers = {
            "trg_shipment_jobs_state_changed_at_insert": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_jobs_state_changed_at_insert
                AFTER INSERT ON shipment_jobs
                WHEN NEW.state_changed_at IS NULL OR NEW.state_changed_at = ''
                BEGIN
                    UPDATE shipment_jobs
                    SET state_changed_at = COALESCE(NEW.created_at, NEW.updated_at)
                    WHERE id = NEW.id;
                END
            """,
            "trg_shipment_jobs_state_changed_at_update": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_jobs_state_changed_at_update
                AFTER UPDATE OF identity_state ON shipment_jobs
                WHEN OLD.identity_state IS NOT NEW.identity_state
                BEGIN
                    UPDATE shipment_jobs
                    SET state_changed_at = COALESCE(NULLIF(NEW.updated_at, ''), STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    WHERE id = NEW.id;
                END
            """,
            "trg_shipment_logistics_state_changed_at_insert": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_logistics_state_changed_at_insert
                AFTER INSERT ON shipment_logistics
                WHEN NEW.state_changed_at IS NULL OR NEW.state_changed_at = ''
                BEGIN
                    UPDATE shipment_logistics
                    SET state_changed_at = NEW.updated_at
                    WHERE job_id = NEW.job_id;
                END
            """,
            "trg_shipment_logistics_state_changed_at_update": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_logistics_state_changed_at_update
                AFTER UPDATE OF state ON shipment_logistics
                WHEN OLD.state IS NOT NEW.state
                BEGIN
                    UPDATE shipment_logistics
                    SET state_changed_at = COALESCE(NULLIF(NEW.updated_at, ''), STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    WHERE job_id = NEW.job_id;
                END
            """,
            "trg_shipment_erp_state_changed_at_insert": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_erp_state_changed_at_insert
                AFTER INSERT ON shipment_erp
                WHEN NEW.state_changed_at IS NULL OR NEW.state_changed_at = ''
                BEGIN
                    UPDATE shipment_erp
                    SET state_changed_at = NEW.updated_at
                    WHERE job_id = NEW.job_id;
                END
            """,
            "trg_shipment_erp_state_changed_at_update": """
                CREATE TRIGGER IF NOT EXISTS trg_shipment_erp_state_changed_at_update
                AFTER UPDATE OF state, checkpoint ON shipment_erp
                WHEN OLD.state IS NOT NEW.state OR OLD.checkpoint IS NOT NEW.checkpoint
                BEGIN
                    UPDATE shipment_erp
                    SET state_changed_at = COALESCE(NULLIF(NEW.updated_at, ''), STRFTIME('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    WHERE job_id = NEW.job_id;
                END
            """,
        }
        for statement in triggers.values():
            conn.execute(statement)

    def _migrate_to_v17(self, conn: sqlite3.Connection) -> None:
        """Persist customer-selected speed for non-blocking due notices."""

        if "customer_shipping_service" not in self._table_columns(conn, "shipment_jobs"):
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN customer_shipping_service TEXT"
            )

    def _migrate_to_v18(self, conn: sqlite3.Connection) -> None:
        """Checkpoint exact-ASIN product identity backfills by catalog version."""

        columns = self._table_columns(conn, "shipment_jobs")
        if "product_identity_catalog_version" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs "
                "ADD COLUMN product_identity_catalog_version TEXT"
            )
        if "product_identity_checked_at" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN product_identity_checked_at TEXT"
            )

    def _migrate_to_v19(self, conn: sqlite3.Connection) -> None:
        """Keep transient identity failures from starving later historical rows."""

        columns = self._table_columns(conn, "shipment_jobs")
        if "product_identity_retry_count" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN "
                "product_identity_retry_count INTEGER NOT NULL DEFAULT 0"
            )
        if "product_identity_next_retry_at" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs "
                "ADD COLUMN product_identity_next_retry_at TEXT"
            )
        if "product_identity_last_error" not in columns:
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN product_identity_last_error TEXT"
            )

    def _migrate_to_v20(self, conn: sqlite3.Connection) -> None:
        """Persist the first logistics-overdue deadline for permanent display."""

        if "logistics_overdue_at" not in self._table_columns(conn, "shipment_jobs"):
            conn.execute(
                "ALTER TABLE shipment_jobs ADD COLUMN logistics_overdue_at TEXT"
            )
        self._reconcile_logistics_overdue_conn(
            conn,
            include_historical=True,
        )

    def _migrate_to_v21(self, conn: sqlite3.Connection) -> None:
        """Make quarantined scan errors independently manageable and auditable."""

        columns = self._table_columns(conn, "shipment_scan_issues")
        if "management_state" not in columns:
            conn.execute(
                "ALTER TABLE shipment_scan_issues "
                "ADD COLUMN management_state TEXT NOT NULL DEFAULT 'ACTIVE'"
            )
        if "management_reason" not in columns:
            conn.execute(
                "ALTER TABLE shipment_scan_issues ADD COLUMN management_reason TEXT"
            )
        if "management_updated_at" not in columns:
            conn.execute(
                "ALTER TABLE shipment_scan_issues ADD COLUMN management_updated_at TEXT"
            )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shipment_scan_issue_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id INTEGER NOT NULL REFERENCES shipment_scan_issues(id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                old_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                run_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_scan_issue_events_issue
                ON shipment_scan_issue_events(issue_id, id);
            """
        )

    def _migrate_to_v22(self, conn: sqlite3.Connection) -> None:
        """Persist the evidence and structured block for Amazon image orders."""

        job_columns = self._table_columns(conn, "shipment_jobs")
        for column, declaration in (
            ("sales_platform_code", "TEXT NOT NULL DEFAULT ''"),
            ("sales_platform_name", "TEXT NOT NULL DEFAULT ''"),
            ("has_main_image", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in job_columns:
                conn.execute(
                    f"ALTER TABLE shipment_jobs ADD COLUMN {column} {declaration}"
                )
        if "policy_block_code" not in self._table_columns(conn, "shipment_erp"):
            conn.execute("ALTER TABLE shipment_erp ADD COLUMN policy_block_code TEXT")

    def _refresh_order_policy_evidence_conn(self, conn: sqlite3.Connection) -> int:
        """Backfill historical queue rows from notification snapshots when present."""

        changed = 0
        if self._table_exists(conn, "shipment_order_contacts"):
            changed += conn.execute(
                """
                UPDATE shipment_jobs AS j
                SET sales_platform_code = COALESCE(
                        NULLIF(j.sales_platform_code, ''),
                        (SELECT NULLIF(c.sales_platform_code, '')
                         FROM shipment_order_contacts c
                         WHERE c.platform_order_no = j.platform_order_no)
                    ),
                    sales_platform_name = COALESCE(
                        NULLIF(j.sales_platform_name, ''),
                        (SELECT NULLIF(c.sales_platform_name, '')
                         FROM shipment_order_contacts c
                         WHERE c.platform_order_no = j.platform_order_no)
                    )
                WHERE EXISTS (
                    SELECT 1 FROM shipment_order_contacts c
                    WHERE c.platform_order_no = j.platform_order_no
                      AND (
                          (j.sales_platform_code = '' AND c.sales_platform_code <> '')
                          OR (j.sales_platform_name = '' AND c.sales_platform_name <> '')
                      )
                )
                """
            ).rowcount
        if self._table_exists(conn, "shipment_order_product_snapshots"):
            changed += conn.execute(
                """
                UPDATE shipment_jobs AS j
                SET has_main_image = 1
                WHERE j.has_main_image = 0
                  AND EXISTS (
                      SELECT 1
                      FROM shipment_order_product_snapshots p
                      WHERE p.platform_order_no = j.platform_order_no
                        AND p.active = 1
                        AND p.has_main_image = 1
                        AND (
                            p.system_order_no = j.system_order_no
                            OR p.system_order_no = ''
                        )
                  )
                """
            ).rowcount
        return changed

    def _reconcile_amazon_main_image_policy_conn(
        self,
        conn: sqlite3.Connection,
        *,
        logistics_no: str | None = None,
        run_id: str | None = None,
    ) -> int:
        where = """
            j.identity_state = ? AND l.state = ? AND e.state <> ?
        """
        params: list[Any] = [IDENTITY_ACTIVE, LOGISTICS_READY, ERP_DONE]
        if logistics_no is not None:
            where += " AND j.logistics_no = ?"
            params.append(logistics_no)
        rows = conn.execute(
            """
            SELECT j.id, j.platform_order_no, j.sales_platform_code,
                   j.sales_platform_name, j.has_main_image, j.logistics_no,
                   l.carrier_normalized, l.carrier_raw,
                   l.international_tracking_no, e.state AS erp_state,
                   e.policy_block_code
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE """
            + where,
            params,
        ).fetchall()
        now = utc_now()
        changed = 0
        for row in rows:
            violation = amazon_main_image_policy_violation(
                platform_order_no=row["platform_order_no"],
                sales_platform_code=row["sales_platform_code"],
                sales_platform_name=row["sales_platform_name"],
                has_main_image=bool(row["has_main_image"]),
                carrier=row["carrier_normalized"] or row["carrier_raw"],
                tracking_no=row["international_tracking_no"],
            )
            if violation is None:
                continue
            already_blocked = (
                row["erp_state"] == ERP_BLOCKED
                and row["policy_block_code"] == violation.code
            )
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, next_attempt_at = NULL, last_error = ?,
                    policy_block_code = ?, updated_at = ?
                WHERE job_id = ? AND state <> ?
                """,
                (
                    ERP_BLOCKED,
                    violation.message,
                    violation.code,
                    now,
                    row["id"],
                    ERP_DONE,
                ),
            )
            if already_blocked:
                continue
            self._insert_event_conn(
                conn,
                job_id=int(row["id"]),
                stage="erp",
                event_type="AMAZON_MAIN_IMAGE_CHANNEL_BLOCKED",
                old_state=str(row["erp_state"] or ""),
                new_state=ERP_BLOCKED,
                message=violation.message,
                details={
                    "policy_code": violation.code,
                    "carrier_key": violation.carrier_key,
                    "channel_path": list(violation.channel_path),
                    "source": "queue_policy_reconciliation",
                },
                run_id=run_id,
            )
            changed += 1
        return changed

    @staticmethod
    def _protect_legacy_table(conn: sqlite3.Connection) -> None:
        if not ShipmentWorkflowStore._table_exists(conn, "shipment_queue_v1"):
            return
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"trg_shipment_queue_v1_read_only_{operation.lower()}"
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {name}
                BEFORE {operation} ON shipment_queue_v1
                BEGIN
                    SELECT RAISE(ABORT, 'shipment_queue_v1 is read-only after V2 migration');
                END
                """
            )

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT * FROM shipment_queue_v1 ORDER BY id").fetchall()
        for row in rows:
            legacy_status = str(row["queue_status"] or LEGACY_NEW)
            complete = _legacy_logistics_complete(row)
            last_error = str(row["last_error"] or "").strip() or None
            logistics_state = LOGISTICS_PENDING
            erp_state = ERP_WAITING
            erp_checkpoint = ERP_CHECKPOINT_NONE
            completion_source = None
            logistics_error = None
            erp_error = None
            if legacy_status == LEGACY_NOT_READY:
                logistics_state = LOGISTICS_WAITING
            elif legacy_status == LEGACY_READY_TO_MARK:
                logistics_state = LOGISTICS_READY
                erp_state = ERP_PENDING
            elif legacy_status == LEGACY_ERROR:
                if complete:
                    logistics_state = LOGISTICS_READY
                    erp_state = ERP_RETRYABLE
                    erp_error = last_error
                else:
                    logistics_state = LOGISTICS_RETRYABLE
                    logistics_error = last_error
            elif legacy_status == LEGACY_MANUAL_REVIEW:
                if _is_browser_closed_error(last_error):
                    logistics_state = LOGISTICS_RETRYABLE
                    logistics_error = last_error
                elif complete:
                    logistics_state = LOGISTICS_READY
                    erp_state = ERP_BLOCKED
                    erp_error = last_error
                else:
                    # The legacy schema did not record an explicit operator
                    # stop decision.  Treating every old "manual review" row
                    # as a durable block would silently exclude program errors
                    # from future scans.  Only the new ORDER_ISSUE action is
                    # authoritative enough to persist LOGISTICS_BLOCKED.
                    logistics_state = LOGISTICS_RETRYABLE
                    logistics_error = last_error
            elif legacy_status in {LEGACY_ERP_MARKED, LEGACY_EMAIL_SENT}:
                logistics_state = LOGISTICS_READY
                erp_state = ERP_DONE
                erp_checkpoint = ERP_CHECKPOINT_OUTBOUNDED
                completion_source = ERP_COMPLETION_AUTOMATION

            created_at = _parse_legacy_timestamp(row["created_at"]) or utc_now()
            updated_at = _parse_legacy_timestamp(row["updated_at"]) or created_at
            first_seen_at = created_at
            sales_channel = normalize_sales_channel(row["platform_order_no"])
            email_required = customer_email_required_for_sales_channel(sales_channel)
            conn.execute(
                """
                INSERT INTO shipment_jobs (
                    logistics_no, system_order_no, platform_order_no, shipment_tag_name,
                    tag_text, sku_text, customer_remark, source_status_text, receiver_email,
                    source_rowid, sales_channel, customer_email_required,
                    identity_state, first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["als_no"], row["system_order_no"], row["platform_order_no"], row["shipment_tag_name"],
                    row["tag_text"], row["sku_text"], row["customer_remark"], row["status_text"], row["receiver_email"],
                    row["system_order_no"], sales_channel, 1 if email_required else 0,
                    IDENTITY_ACTIVE, first_seen_at, updated_at, created_at, updated_at,
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            currency, fee_amount = _split_money(row["actual_total"])
            conn.execute(
                """
                INSERT INTO shipment_logistics (
                    job_id, state, carrier_raw, carrier_normalized, international_tracking_no,
                    currency, fee_amount, chargeable_weight_kg, package_count,
                    last_checked_at, next_attempt_at, attempt_count, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, logistics_state, row["carrier"], row["carrier"], row["international_tracking_no"],
                    currency, fee_amount, _normalize_decimal(row["chargeable_weight_kg"]), row["package_count"],
                    updated_at if row["carrier"] or last_error else None,
                    utc_now() if logistics_state in {LOGISTICS_WAITING, LOGISTICS_RETRYABLE} else None,
                    0, logistics_error, updated_at,
                ),
            )
            processed_at = _parse_legacy_timestamp(row["processed_at"])
            conn.execute(
                """
                INSERT INTO shipment_erp (
                    job_id, state, checkpoint, outbounded_at, next_attempt_at,
                    attempt_count, last_error, completion_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, erp_state, erp_checkpoint,
                    processed_at if erp_checkpoint == ERP_CHECKPOINT_OUTBOUNDED else None,
                    utc_now() if erp_state == ERP_RETRYABLE else None,
                    0, erp_error, completion_source, updated_at,
                ),
            )
            self._insert_event_conn(
                conn,
                job_id=job_id,
                stage="migration",
                event_type="MIGRATED_FROM_V1",
                old_state=legacy_status,
                new_state=f"{logistics_state}/{erp_state}",
                message=last_error,
                details={"legacy_id": row["id"], "legacy_error": last_error},
            )
        self._migrate_sent_email_batches(conn, rows)

    def _migrate_sent_email_batches(self, conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
        sent_rows = [row for row in rows if str(row["queue_status"] or "") == LEGACY_EMAIL_SENT]
        for platform_order_no in sorted({str(row["platform_order_no"]) for row in sent_rows}):
            job_rows = conn.execute(
                """
                SELECT j.id, j.logistics_no, l.carrier_normalized, l.international_tracking_no,
                       j.receiver_email
                FROM shipment_jobs j JOIN shipment_logistics l ON l.job_id = j.id
                WHERE j.platform_order_no = ?
                ORDER BY j.id
                """,
                (platform_order_no,),
            ).fetchall()
            if not job_rows:
                continue
            recipient, blocked_reason = self._email_delivery_context(job_rows)
            content_hash = self._email_content_hash(
                job_rows,
                recipient_email=recipient,
                blocked_reason=blocked_reason,
            )
            message_id = self._email_message_id(platform_order_no, 1, content_hash)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO shipment_email_batches (
                    platform_order_no, sequence_no, state, recipient_email, message_id,
                    template_version, content_hash, sent_at, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, 'v1', ?, ?, ?, ?)
                """,
                (platform_order_no, EMAIL_SENT, recipient, message_id, content_hash, now, now, now),
            )
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._replace_batch_items_conn(conn, batch_id, job_rows)

    def _insert_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: int | None = None,
        batch_id: int | None = None,
        stage: str,
        event_type: str,
        old_state: str | None = None,
        new_state: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO shipment_events (
                job_id, batch_id, stage, event_type, old_state, new_state,
                message, details_json, run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id, batch_id, stage, event_type, old_state, new_state, message,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True), run_id, utc_now(),
            ),
        )

    def _aggregate_sql(self) -> str:
        return """
            SELECT j.*, j.last_seen_at AS last_scanned_at,
                   j.state_changed_at AS identity_state_changed_at,
                   l.state AS logistics_state, l.alibaba_status, l.service_type,
                   l.service_line,
                   l.carrier_raw, l.carrier_normalized, l.international_tracking_no,
                   l.currency, l.fee_amount, l.chargeable_weight_kg, l.package_count,
                   l.source_url, l.tracking_override_carrier, l.tracking_override_no,
                   l.tracking_override_at, l.tracking_override_reason,
                   l.tracking_mismatch_action, l.tracking_mismatch_reviewed_at,
                   l.last_checked_at,
                   l.last_checked_at AS logistics_last_checked_at,
                   l.state_changed_at AS logistics_state_changed_at,
                   l.next_attempt_at AS logistics_next_attempt_at,
                   l.attempt_count AS logistics_attempt_count, l.last_error AS logistics_last_error,
                   e.state AS erp_state, e.checkpoint AS erp_checkpoint, e.channel_path,
                   e.freight_amount, e.chargeable_weight_g, e.channel_payload_hash,
                   e.logistics_payload_hash, e.next_attempt_at AS erp_next_attempt_at,
                   e.attempt_count AS erp_attempt_count, e.last_error AS erp_last_error,
                   e.policy_block_code,
                   e.channel_set_at, e.audited_at, e.logistics_saved_at, e.outbounded_at,
                   e.completion_source, e.externally_completed_at,
                   e.state_changed_at AS erp_state_changed_at,
                   e.selected_wms_wo_number, e.selected_wms_candidates_hash,
                   e.selected_wms_selected_at, e.selected_wms_selected_by,
                   CASE
                       WHEN e.selected_wms_wo_number IS NULL
                            AND e.checkpoint = 'NONE'
                            AND e.state <> 'DONE'
                            AND EXISTS (
                                SELECT 1
                                FROM shipment_events selection_event
                                WHERE selection_event.job_id = j.id
                                  AND selection_event.stage = 'erp'
                                  AND (
                                      selection_event.event_type = 'ERP_WMS_OUTBOUND_SELECTION_REQUIRED'
                                      OR (
                                          selection_event.event_type = 'ERP_ATTEMPT_FINISHED'
                                          AND selection_event.message LIKE '%同一系统单号对应多个销售出库单%'
                                      )
                                  )
                            )
                       THEN 1 ELSE 0
                   END AS wms_selection_required,
                   (
                       SELECT b.state
                       FROM shipment_email_batches b
                       JOIN shipment_email_batch_items bi ON bi.batch_id = b.id
                       WHERE bi.job_id = j.id
                       ORDER BY b.sequence_no DESC LIMIT 1
                   ) AS email_state,
                   (
                       SELECT b.last_error
                       FROM shipment_email_batches b
                       JOIN shipment_email_batch_items bi ON bi.batch_id = b.id
                       WHERE bi.job_id = j.id
                       ORDER BY b.sequence_no DESC LIMIT 1
                   ) AS email_last_error,
                   (
                       SELECT b.attempt_count
                       FROM shipment_email_batches b
                       JOIN shipment_email_batch_items bi ON bi.batch_id = b.id
                       WHERE bi.job_id = j.id
                       ORDER BY b.sequence_no DESC LIMIT 1
                   ) AS email_attempt_count,
                   (
                       SELECT identity_event.details_json
                       FROM shipment_events identity_event
                       WHERE identity_event.job_id = j.id
                         AND identity_event.event_type IN (
                             'PRODUCT_IDENTITY_BACKFILLED',
                             'PRODUCT_IDENTITY_CHECKED',
                             'PRODUCT_IDENTITY_RETRY_SCHEDULED'
                         )
                       ORDER BY
                           CASE
                               WHEN COALESCE(
                                   json_array_length(
                                       identity_event.details_json,
                                       '$.observed_asins'
                                   ),
                                   0
                               ) > 0
                               THEN 0 ELSE 1
                           END,
                           identity_event.id DESC
                       LIMIT 1
                   ) AS product_identity_evidence_json
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            JOIN shipment_erp e ON e.job_id = j.id
        """

    def _flatten(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        actual_total = None
        carrier = item.get("carrier_normalized") or item.get("carrier_raw")
        tracking_no = item.get("international_tracking_no")
        tracking_validated = self._tracking_validated(item)
        if item.get("fee_amount"):
            actual_total = f"{item.get('currency')} {item.get('fee_amount')}".strip()
        item.update(
            {
                "job_id": item["id"],
                "logistics_no": item["logistics_no"],
                "status_text": item.get("source_status_text"),
                "identity_status_text": (
                    "标签已移除/自动暂停"
                    if item.get("identity_state") == IDENTITY_PAUSED_TAG_REMOVED
                    else item.get("identity_state")
                ),
                "carrier": carrier,
                "tracking_validated": tracking_validated,
                "actual_total": actual_total,
                "shipping_attention_notice": shipment_tracking_attention_notice(
                    customer_shipping_service=item.get("customer_shipping_service"),
                    first_seen_at=item.get("first_seen_at"),
                    carrier=carrier,
                    international_tracking_no=item.get("international_tracking_no"),
                    logistics_state=item.get("logistics_state"),
                    identity_state=item.get("identity_state"),
                    erp_state=item.get("erp_state"),
                    tracking_validated=tracking_validated,
                ),
                "last_error": (
                    item.get("erp_last_error")
                    or item.get("logistics_last_error")
                    or item.get("email_last_error")
                ),
            }
        )
        return item

    @staticmethod
    def _tracking_validated(row: Mapping[str, Any]) -> bool:
        carrier = row.get("carrier_normalized") or row.get("carrier_raw")
        tracking_no = row.get("international_tracking_no")
        return bool(
            str(carrier or "").strip()
            and str(tracking_no or "").strip()
            and (
                str(row.get("logistics_state") or "").strip().upper()
                == LOGISTICS_READY
                or tracking_number_matches_carrier(carrier, tracking_no)
            )
        )

    def _historical_overdue_resolution_at_conn(
        self,
        conn: sqlite3.Connection,
        row: Mapping[str, Any],
    ) -> datetime | None:
        """Return the earliest recorded tracking-ready or terminal timestamp."""

        tracking_candidates: list[datetime] = []
        event = conn.execute(
            """
            SELECT MIN(created_at) AS created_at
            FROM shipment_events
            WHERE job_id = ? AND (
                (event_type = 'LOGISTICS_ATTEMPT_COMPLETED' AND new_state = ?)
                OR event_type IN (
                    'TRACKING_NUMBER_MANUALLY_CONFIRMED',
                    'TRACKING_PAIR_MANUALLY_CONFIRMED'
                )
            )
            """,
            (row["id"], LOGISTICS_READY),
        ).fetchone()
        event_at = _parse_utc_timestamp(event["created_at"] if event else None)
        if event_at is not None:
            tracking_candidates.append(event_at)

        if self._tracking_validated(row):
            fallback_values = [
                row.get("tracking_override_at"),
                row.get("logistics_state_changed_at"),
                row.get("logistics_last_checked_at"),
            ]
            tracking_candidates.extend(
                parsed
                for parsed in (_parse_utc_timestamp(value) for value in fallback_values)
                if parsed is not None
            )

        terminal_candidates: list[datetime] = []
        if str(row.get("erp_state") or "").strip().upper() == ERP_DONE:
            terminal_values = [
                row.get("outbounded_at"),
                row.get("externally_completed_at"),
                row.get("erp_state_changed_at"),
            ]
            terminal_candidates.extend(
                parsed
                for parsed in (_parse_utc_timestamp(value) for value in terminal_values)
                if parsed is not None
            )
        if str(row.get("identity_state") or "").strip().upper() != IDENTITY_ACTIVE:
            terminal_candidates.extend(
                parsed
                for parsed in (
                    _parse_utc_timestamp(row.get("cancelled_at")),
                    _parse_utc_timestamp(row.get("identity_state_changed_at")),
                )
                if parsed is not None
            )

        candidates = [*tracking_candidates, *terminal_candidates]
        return min(candidates) if candidates else None

    def _reconcile_logistics_overdue_conn(
        self,
        conn: sqlite3.Connection,
        *,
        observed_at: datetime | None = None,
        include_historical: bool = False,
        logistics_no: str | None = None,
    ) -> int:
        """Persist newly provable overdue history without changing workflow state."""

        current = observed_at or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        sql = self._aggregate_sql() + " WHERE j.logistics_overdue_at IS NULL"
        params: list[Any] = []
        if logistics_no is not None:
            sql += " AND j.logistics_no = ?"
            params.append(str(logistics_no).strip())
        rows = conn.execute(sql, params).fetchall()
        changed = 0
        for stored_row in rows:
            row = dict(stored_row)
            deadline = shipment_tracking_deadline(
                customer_shipping_service=row.get("customer_shipping_service"),
                first_seen_at=row.get("first_seen_at"),
            )
            if deadline is None:
                continue
            tracking_validated = self._tracking_validated(row)
            currently_overdue = bool(
                shipment_tracking_attention_notice(
                    customer_shipping_service=row.get("customer_shipping_service"),
                    first_seen_at=row.get("first_seen_at"),
                    carrier=row.get("carrier_normalized") or row.get("carrier_raw"),
                    international_tracking_no=row.get("international_tracking_no"),
                    logistics_state=row.get("logistics_state"),
                    identity_state=row.get("identity_state"),
                    erp_state=row.get("erp_state"),
                    tracking_validated=tracking_validated,
                    now=current,
                )
            )
            historical_overdue = False
            if include_historical and not currently_overdue:
                resolved_at = self._historical_overdue_resolution_at_conn(
                    conn,
                    row,
                )
                historical_overdue = bool(
                    resolved_at is not None and resolved_at > deadline
                )
            if not currently_overdue and not historical_overdue:
                continue

            overdue_at = _format_utc_timestamp(deadline)
            result = conn.execute(
                """
                UPDATE shipment_jobs
                SET logistics_overdue_at = ?
                WHERE id = ? AND logistics_overdue_at IS NULL
                """,
                (overdue_at, row["id"]),
            )
            if result.rowcount <= 0:
                continue
            changed += 1
        return changed

    def reconcile_logistics_overdue_history(
        self,
        *,
        now: datetime | None = None,
        include_historical: bool = False,
        logistics_no: str | None = None,
    ) -> int:
        """Persist current (and optionally historical) overdue facts atomically."""

        self.initialize()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._reconcile_logistics_overdue_conn(
                conn,
                observed_at=now,
                include_historical=include_historical,
                logistics_no=logistics_no,
            )
            conn.commit()
        return changed

    def get_by_logistics_no(self, logistics_no: str) -> dict[str, Any] | None:
        self.initialize()
        self.reconcile_logistics_overdue_history(
            include_historical=True,
            logistics_no=logistics_no,
        )
        with self.connect() as conn:
            row = conn.execute(self._aggregate_sql() + " WHERE j.logistics_no = ?", (logistics_no,)).fetchone()
        return self._flatten(row) if row else None

    def upsert_candidate(
        self,
        candidate: ShipmentCandidate,
        *,
        run_id: str | None = None,
        allow_tag_restore: bool = False,
    ) -> QueueInsertResult:
        self.initialize()
        now = utc_now()
        sales_channel = candidate.sales_channel or normalize_sales_channel(candidate.platform_order_no)
        email_required = (
            customer_email_required_for_sales_channel(sales_channel)
            if candidate.customer_email_required is None
            else bool(candidate.customer_email_required)
        )
        customer_shipping_service = (
            normalize_customer_shipping_service(candidate.customer_shipping_service)
            or None
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            managed_issue = conn.execute(
                """
                SELECT id, system_order_no, platform_order_no, management_state,
                       management_reason, error_message
                FROM shipment_scan_issues
                WHERE system_order_no = ? AND platform_order_no = ?
                  AND issue_code = ? AND management_state <> ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    candidate.system_order_no,
                    candidate.platform_order_no,
                    CUSTOMER_SHIPPING_SERVICE_SCAN_ISSUE,
                    SCAN_ISSUE_ACTIVE,
                ),
            ).fetchone()
            if managed_issue is not None:
                management_state = str(managed_issue["management_state"] or "")
                conn.execute(
                    """
                    INSERT INTO shipment_scan_issue_events (
                        issue_id, action, old_state, new_state, reason, run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(managed_issue["id"]),
                        "candidate_blocked_by_manual_state",
                        management_state,
                        management_state,
                        "完整扫描重新发现候选，但保留人工状态并禁止自动入队。",
                        run_id,
                        now,
                    ),
                )
                conn.commit()
                return QueueInsertResult(
                    False,
                    candidate,
                    {
                        "system_order_no": str(managed_issue["system_order_no"] or ""),
                        "platform_order_no": str(managed_issue["platform_order_no"] or ""),
                        "identity_state": management_state,
                        "logistics_state": "",
                        "erp_state": "",
                        "last_error": str(
                            managed_issue["management_reason"]
                            or managed_issue["error_message"]
                            or "扫描错误保留了人工状态。"
                        ),
                    },
                )
            existing = conn.execute("SELECT * FROM shipment_jobs WHERE logistics_no = ?", (candidate.logistics_no,)).fetchone()
            if existing is None:
                # A corrected customer remark commonly replaces an obsolete
                # ALS number for the same ERP child order.  Update the existing
                # business identity in place instead of appending another row.
                identity_match = conn.execute(
                    """
                    SELECT j.*, l.state AS logistics_state,
                           e.state AS erp_state, e.checkpoint AS erp_checkpoint
                    FROM shipment_jobs j
                    JOIN shipment_logistics l ON l.job_id = j.id
                    JOIN shipment_erp e ON e.job_id = j.id
                    WHERE j.platform_order_no = ? AND j.system_order_no = ?
                      AND j.identity_state <> ?
                    ORDER BY
                        CASE WHEN e.state = ? THEN 0 ELSE 1 END,
                        CASE WHEN j.identity_state = ? THEN 0 ELSE 1 END,
                        j.id DESC
                    LIMIT 1
                    """,
                    (
                        candidate.platform_order_no,
                        candidate.system_order_no,
                        IDENTITY_SUPERSEDED,
                        ERP_DONE,
                        IDENTITY_ACTIVE,
                    ),
                ).fetchone()
                if identity_match is not None:
                    old_logistics_no = str(identity_match["logistics_no"])
                    # Completed work records the ALS actually used for the ERP
                    # write and must never be rewritten by a later scan.
                    if str(identity_match["erp_state"]) == ERP_DONE:
                        conn.execute(
                            """
                            UPDATE shipment_jobs
                            SET shipment_tag_name = ?, tag_text = ?, sku_text = ?,
                                product_type = COALESCE(NULLIF(?, ''), product_type),
                                customer_remark = ?, source_status_text = ?,
                                receiver_email = COALESCE(?, receiver_email),
                                source_page = ?, source_scroll_top = ?, source_rowid = ?,
                                sales_platform_code = COALESCE(NULLIF(?, ''), sales_platform_code),
                                sales_platform_name = COALESCE(NULLIF(?, ''), sales_platform_name),
                                has_main_image = CASE WHEN ? THEN 1 ELSE has_main_image END,
                                last_seen_at = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                candidate.shipment_tag_name,
                                candidate.tag_text,
                                candidate.sku_text,
                                candidate.product_type,
                                candidate.customer_remark,
                                candidate.status_text,
                                candidate.receiver_email,
                                candidate.source_page,
                                candidate.source_scroll_top,
                                candidate.rowid,
                                candidate.sales_platform_code,
                                candidate.sales_platform_name,
                                1 if candidate.has_main_image else 0,
                                now,
                                now,
                                identity_match["id"],
                            ),
                        )
                        self._insert_event_conn(
                            conn,
                            job_id=int(identity_match["id"]),
                            stage="identity",
                            event_type="ALS_CHANGE_IGNORED_AFTER_ERP_COMPLETION",
                            old_state=old_logistics_no,
                            new_state=candidate.logistics_no,
                            message="ERP 标发已完成，保留实际使用的 ALS；仅刷新订单来源信息。",
                            details={"observed_logistics_no": candidate.logistics_no},
                            run_id=run_id,
                        )
                        conn.commit()
                        return QueueInsertResult(
                            False,
                            candidate,
                            self.get_by_logistics_no(old_logistics_no),
                        )
                    if _has_live_lease(identity_match, now=now):
                        self._insert_event_conn(
                            conn,
                            job_id=int(identity_match["id"]),
                            stage="identity",
                            event_type="ALS_CHANGE_DEFERRED_DURING_ACTIVE_TASK",
                            old_state=old_logistics_no,
                            new_state=candidate.logistics_no,
                            message="任务正在执行，ALS 变更已安全延后到下一轮扫描。",
                            run_id=run_id,
                        )
                        conn.commit()
                        return QueueInsertResult(
                            False,
                            candidate,
                            self.get_by_logistics_no(old_logistics_no),
                        )
                    conn.execute(
                        """
                        UPDATE shipment_jobs
                        SET logistics_no = ?, shipment_tag_name = ?, tag_text = ?,
                            sku_text = ?, product_type = COALESCE(NULLIF(?, ''), product_type),
                            customer_remark = ?, source_status_text = ?,
                            customer_shipping_service = COALESCE(?, customer_shipping_service),
                            receiver_email = COALESCE(?, receiver_email),
                            source_page = ?, source_scroll_top = ?, source_rowid = ?,
                            sales_platform_code = COALESCE(NULLIF(?, ''), sales_platform_code),
                            sales_platform_name = COALESCE(NULLIF(?, ''), sales_platform_name),
                            has_main_image = CASE WHEN ? THEN 1 ELSE has_main_image END,
                            sales_channel = ?, customer_email_required = ?,
                            last_seen_at = ?, updated_at = ?, version = version + 1
                        WHERE id = ?
                        """,
                        (
                            candidate.logistics_no,
                            candidate.shipment_tag_name,
                            candidate.tag_text,
                            candidate.sku_text,
                            candidate.product_type,
                            candidate.customer_remark,
                            candidate.status_text,
                            customer_shipping_service,
                            candidate.receiver_email,
                            candidate.source_page,
                            candidate.source_scroll_top,
                            candidate.rowid,
                            candidate.sales_platform_code,
                            candidate.sales_platform_name,
                            1 if candidate.has_main_image else 0,
                            sales_channel,
                            1 if email_required else 0,
                            now,
                            now,
                            identity_match["id"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE shipment_logistics
                        SET state = ?, alibaba_status = NULL, service_type = NULL,
                            service_line = NULL,
                            carrier_raw = NULL, carrier_normalized = NULL,
                            international_tracking_no = NULL, currency = NULL,
                            fee_amount = NULL, chargeable_weight_kg = NULL,
                            package_count = NULL, source_url = NULL,
                            tracking_override_carrier = NULL, tracking_override_no = NULL,
                            tracking_override_at = NULL, tracking_override_reason = NULL,
                            tracking_mismatch_action = NULL,
                            tracking_mismatch_reviewed_at = NULL,
                            last_checked_at = NULL, next_attempt_at = NULL,
                            attempt_count = 0, last_error = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (LOGISTICS_PENDING, now, identity_match["id"]),
                    )
                    if str(identity_match["erp_checkpoint"] or ERP_CHECKPOINT_NONE) == ERP_CHECKPOINT_NONE:
                        conn.execute(
                            """
                            UPDATE shipment_erp
                            SET state = ?, next_attempt_at = NULL, attempt_count = 0,
                                last_error = NULL, updated_at = ?
                            WHERE job_id = ?
                            """,
                            (ERP_WAITING, now, identity_match["id"]),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE shipment_erp
                            SET state = ?, next_attempt_at = NULL,
                                last_error = ?, updated_at = ?
                            WHERE job_id = ?
                            """,
                            (
                                ERP_BLOCKED,
                                "ALS 单号在 ERP 标发中途发生变化，请人工核对后从正确阶段重开。",
                                now,
                                identity_match["id"],
                            ),
                        )
                    self._insert_event_conn(
                        conn,
                        job_id=int(identity_match["id"]),
                        stage="identity",
                        event_type="PLATFORM_LOGISTICS_NUMBER_REPLACED",
                        old_state=old_logistics_no,
                        new_state=candidate.logistics_no,
                        message="同一平台单号和系统单号重新扫描到新的首个 ALS，已原位更新队列。",
                        details={
                            "platform_order_no": candidate.platform_order_no,
                            "system_order_no": candidate.system_order_no,
                        },
                        run_id=run_id,
                    )
                    conn.commit()
                    return QueueInsertResult(
                        False,
                        candidate,
                        self.get_by_logistics_no(candidate.logistics_no),
                        immediate_logistics=True,
                    )
            if existing:
                same_identity = (
                    existing["system_order_no"] == candidate.system_order_no
                    and existing["platform_order_no"] == candidate.platform_order_no
                )
                if same_identity:
                    stage_row = conn.execute(
                        """
                        SELECT l.state AS logistics_state, l.next_attempt_at AS logistics_next_attempt_at,
                               l.last_error AS logistics_last_error,
                               l.tracking_mismatch_action AS tracking_mismatch_action,
                               e.state AS erp_state, e.next_attempt_at AS erp_next_attempt_at
                        FROM shipment_logistics l
                        JOIN shipment_erp e ON e.job_id = l.job_id
                        WHERE l.job_id = ?
                        """,
                        (existing["id"],),
                    ).fetchone()
                    conn.execute(
                        """
                        UPDATE shipment_jobs
                        SET shipment_tag_name = ?, tag_text = ?, sku_text = ?,
                            product_type = COALESCE(NULLIF(?, ''), product_type), customer_remark = ?,
                            source_status_text = ?,
                            customer_shipping_service = COALESCE(?, customer_shipping_service),
                            receiver_email = COALESCE(?, receiver_email),
                            source_page = ?, source_scroll_top = ?, source_rowid = ?,
                            sales_platform_code = COALESCE(NULLIF(?, ''), sales_platform_code),
                            sales_platform_name = COALESCE(NULLIF(?, ''), sales_platform_name),
                            has_main_image = CASE WHEN ? THEN 1 ELSE has_main_image END,
                            sales_channel = ?, customer_email_required = ?,
                            last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            candidate.shipment_tag_name, candidate.tag_text, candidate.sku_text,
                            candidate.product_type,
                            candidate.customer_remark, candidate.status_text,
                            customer_shipping_service, candidate.receiver_email,
                            candidate.source_page, candidate.source_scroll_top, candidate.rowid,
                            candidate.sales_platform_code, candidate.sales_platform_name,
                            1 if candidate.has_main_image else 0,
                            sales_channel, 1 if email_required else 0,
                            now, now, existing["id"],
                        ),
                    )
                    auto_resumed = False
                    if (
                        existing["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED
                        and allow_tag_restore
                        and stage_row
                        and stage_row["erp_state"] != ERP_DONE
                    ):
                        changed = conn.execute(
                            """
                            UPDATE shipment_jobs
                            SET identity_state = ?, cancelled_at = NULL,
                                lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                                updated_at = ?, version = version + 1
                            WHERE id = ? AND identity_state = ?
                            """,
                            (
                                IDENTITY_ACTIVE,
                                now,
                                existing["id"],
                                IDENTITY_PAUSED_TAG_REMOVED,
                            ),
                        ).rowcount
                        auto_resumed = bool(changed)
                        if auto_resumed:
                            self._insert_event_conn(
                                conn,
                                job_id=existing["id"],
                                stage="identity",
                                event_type="TAG_RESTORED_AUTO_RESUME",
                                old_state=IDENTITY_PAUSED_TAG_REMOVED,
                                new_state=IDENTITY_ACTIVE,
                                message="完整待审核快照确认自动标发标签已恢复，任务已自动恢复。",
                                details={"source": "candidate_reseen"},
                                run_id=run_id,
                            )
                    immediate_logistics = False
                    immediate_erp = False
                    if (
                        (existing["identity_state"] == IDENTITY_ACTIVE or auto_resumed)
                        and stage_row
                        and stage_row["erp_state"] != ERP_DONE
                        and not _has_live_lease(existing, now=now)
                    ):
                        logistics_state = stage_row["logistics_state"]
                        logistics_refreshes_blocked_facts = (
                            _blocked_logistics_can_refresh_on_candidate_reseen(
                                stage_row
                            )
                        )
                        if logistics_state in {LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE} or logistics_refreshes_blocked_facts:
                            next_state = LOGISTICS_RETRYABLE if logistics_refreshes_blocked_facts else logistics_state
                            conn.execute(
                                """
                                UPDATE shipment_logistics
                                SET state = ?, next_attempt_at = NULL, updated_at = ?
                                WHERE job_id = ?
                                """,
                                (next_state, now, existing["id"]),
                            )
                            conn.execute(
                                """
                                UPDATE shipment_jobs
                                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                                    updated_at = ?, version = version + 1
                                WHERE id = ?
                                """,
                                (now, existing["id"]),
                            )
                            immediate_logistics = True
                            self._insert_event_conn(
                                conn,
                                job_id=existing["id"],
                                stage="logistics",
                                event_type="CANDIDATE_RESEEN_IMMEDIATE",
                                old_state=logistics_state,
                                new_state=next_state,
                                message="Candidate was seen again in ERP pending review; logistics retry was made immediate.",
                                run_id=run_id,
                            )
                        if (
                            logistics_state == LOGISTICS_READY
                            and (
                                stage_row["erp_state"] in {ERP_WAITING, ERP_PENDING, ERP_RETRYABLE}
                                or (auto_resumed and stage_row["erp_state"] == ERP_RUNNING)
                            )
                        ):
                            conn.execute(
                                """
                                UPDATE shipment_erp
                                SET next_attempt_at = NULL, updated_at = ?
                                WHERE job_id = ?
                                """,
                                (now, existing["id"]),
                            )
                            conn.execute(
                                """
                                UPDATE shipment_jobs
                                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                                    updated_at = ?, version = version + 1
                                WHERE id = ?
                                """,
                                (now, existing["id"]),
                            )
                            immediate_erp = True
                            self._insert_event_conn(
                                conn,
                                job_id=existing["id"],
                                stage="erp",
                                event_type="CANDIDATE_RESEEN_IMMEDIATE",
                                old_state=stage_row["erp_state"],
                                new_state=stage_row["erp_state"],
                                message="Candidate was seen again in ERP pending review; ERP retry was made immediate.",
                                run_id=run_id,
                            )
                    conn.commit()
                    return QueueInsertResult(
                        False,
                        candidate,
                        self.get_by_logistics_no(candidate.logistics_no),
                        immediate_logistics=immediate_logistics,
                        immediate_erp=immediate_erp,
                        auto_resumed=auto_resumed,
                    )
                old_state = existing["identity_state"]
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (IDENTITY_CONFLICT, now, existing["id"]),
                )
                self._insert_event_conn(
                    conn,
                    job_id=existing["id"],
                    stage="identity",
                    event_type="LOGISTICS_NUMBER_CONFLICT",
                    old_state=old_state,
                    new_state=IDENTITY_CONFLICT,
                    message="The same logistics number was found on a different ERP order.",
                    details={
                        "existing_system_order_no": existing["system_order_no"],
                        "existing_platform_order_no": existing["platform_order_no"],
                        "new_system_order_no": candidate.system_order_no,
                        "new_platform_order_no": candidate.platform_order_no,
                    },
                    run_id=run_id,
                )
                conn.commit()
                return QueueInsertResult(False, candidate, self.get_by_logistics_no(candidate.logistics_no), True)

            conn.execute(
                """
                INSERT INTO shipment_jobs (
                    logistics_no, system_order_no, platform_order_no, shipment_tag_name,
                    tag_text, sku_text, product_type, customer_remark, source_status_text,
                    customer_shipping_service, receiver_email,
                    source_page, source_scroll_top, source_rowid,
                    sales_platform_code, sales_platform_name, has_main_image,
                    sales_channel, customer_email_required,
                    identity_state,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.logistics_no, candidate.system_order_no, candidate.platform_order_no,
                    candidate.shipment_tag_name, candidate.tag_text, candidate.sku_text,
                    candidate.product_type,
                    candidate.customer_remark, candidate.status_text,
                    customer_shipping_service, candidate.receiver_email,
                    candidate.source_page, candidate.source_scroll_top, candidate.rowid,
                    candidate.sales_platform_code or "",
                    candidate.sales_platform_name or "",
                    1 if candidate.has_main_image else 0,
                    sales_channel, 1 if email_required else 0,
                    IDENTITY_ACTIVE, now, now, now, now,
                ),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO shipment_logistics (job_id, state, next_attempt_at, updated_at) VALUES (?, ?, ?, ?)",
                (job_id, LOGISTICS_PENDING, now, now),
            )
            conn.execute(
                "INSERT INTO shipment_erp (job_id, state, checkpoint, updated_at) VALUES (?, ?, ?, ?)",
                (job_id, ERP_WAITING, ERP_CHECKPOINT_NONE, now),
            )
            self._insert_event_conn(
                conn,
                job_id=job_id,
                stage="candidate",
                event_type="CANDIDATE_DISCOVERED",
                new_state=LOGISTICS_PENDING,
                run_id=run_id,
            )
            conn.commit()
        return QueueInsertResult(True, candidate, self.get_by_logistics_no(candidate.logistics_no))

    def insert_candidate(self, candidate: ShipmentCandidate) -> QueueInsertResult:
        return self.upsert_candidate(candidate)

    def insert_candidates(self, candidates: list[ShipmentCandidate]) -> list[QueueInsertResult]:
        run_id = uuid.uuid4().hex
        return [self.upsert_candidate(candidate, run_id=run_id) for candidate in candidates]

    def reconcile_shipment_tag_snapshot(
        self,
        tag_states: Mapping[str, bool | None],
        *,
        snapshot_complete: bool,
        run_id: str | None = None,
    ) -> TagSnapshotReconcileResult:
        """Pause or restore unfinished jobs using an explicitly complete tag snapshot.

        ``tag_states`` contains only orders whose tag field was successfully read.
        Missing orders and values set to ``None`` are deliberately ignored.  The
        explicit completeness flag prevents a partial API response from changing
        queue identity state.
        """

        if not snapshot_complete:
            return TagSnapshotReconcileResult(snapshot_complete=False)
        normalized_states = {
            str(system_order_no or "").strip(): bool(has_shipment_tag)
            for system_order_no, has_shipment_tag in tag_states.items()
            if str(system_order_no or "").strip() and has_shipment_tag is not None
        }
        if not normalized_states:
            return TagSnapshotReconcileResult(snapshot_complete=True)

        self.initialize()
        now = utc_now()
        paused: list[str] = []
        resumed: list[str] = []
        immediate_logistics_count = 0
        immediate_erp_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reconcile_logistics_overdue_conn(
                conn,
                observed_at=_parse_utc_timestamp(now),
            )
            rows = conn.execute(
                """
                SELECT j.id AS job_id, j.system_order_no, j.logistics_no,
                       j.identity_state, l.state AS logistics_state,
                       l.last_error AS logistics_last_error,
                       e.state AS erp_state
                FROM shipment_jobs j
                JOIN shipment_logistics l ON l.job_id = j.id
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state IN (?, ?, ?) AND e.state <> ?
                ORDER BY j.id
                """,
                (
                    IDENTITY_ACTIVE,
                    IDENTITY_PAUSED_TAG_REMOVED,
                    IDENTITY_CANCELLED,
                    ERP_DONE,
                ),
            ).fetchall()
            for row in rows:
                has_shipment_tag = normalized_states.get(str(row["system_order_no"] or "").strip())
                if has_shipment_tag is None:
                    continue

                resume_old_state: str | None = None
                resume_event_type = ""
                resume_message = ""
                if row["identity_state"] == IDENTITY_CANCELLED:
                    # A desktop cancellation suppresses only the current run.  A
                    # complete pending-review snapshot containing the same tagged
                    # order is the proof required to make it eligible again.
                    if not has_shipment_tag:
                        continue
                    resume_old_state = IDENTITY_CANCELLED
                    resume_event_type = "JOB_AUTO_RESTORED_ON_RESCAN"
                    resume_message = (
                        "完整待审核快照再次发现该自动标发订单，本轮取消已自动恢复。"
                    )
                if not has_shipment_tag and row["identity_state"] == IDENTITY_ACTIVE:
                    changed = conn.execute(
                        """
                        UPDATE shipment_jobs
                        SET identity_state = ?, lease_owner = NULL, lease_stage = NULL,
                            lease_until = NULL, updated_at = ?, version = version + 1
                        WHERE id = ? AND identity_state = ?
                        """,
                        (IDENTITY_PAUSED_TAG_REMOVED, now, row["job_id"], IDENTITY_ACTIVE),
                    ).rowcount
                    if not changed:
                        continue
                    paused.append(row["logistics_no"])
                    self._insert_event_conn(
                        conn,
                        job_id=row["job_id"],
                        stage="identity",
                        event_type="TAG_REMOVED_AUTO_PAUSE",
                        old_state=IDENTITY_ACTIVE,
                        new_state=IDENTITY_PAUSED_TAG_REMOVED,
                        message="完整待审核快照确认自动标发标签已移除，任务已自动暂停。",
                        details={"source": "pending_review_snapshot"},
                        run_id=run_id,
                    )
                    continue
                if row["identity_state"] == IDENTITY_PAUSED_TAG_REMOVED:
                    if not has_shipment_tag:
                        continue
                    resume_old_state = IDENTITY_PAUSED_TAG_REMOVED
                    resume_event_type = "TAG_RESTORED_AUTO_RESUME"
                    resume_message = (
                        "完整待审核快照确认自动标发标签已恢复，任务已自动恢复。"
                    )
                if resume_old_state is None:
                    continue

                changed = conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, cancelled_at = NULL,
                        lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ? AND identity_state = ?
                    """,
                    (IDENTITY_ACTIVE, now, row["job_id"], resume_old_state),
                ).rowcount
                if not changed:
                    continue

                immediate_logistics = False
                immediate_erp = False
                logistics_state = row["logistics_state"]
                logistics_refreshes_blocked_facts = (
                    _blocked_logistics_can_refresh_on_candidate_reseen(row)
                )
                if (
                    logistics_state in {LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE}
                    or logistics_refreshes_blocked_facts
                ):
                    next_state = LOGISTICS_RETRYABLE if logistics_refreshes_blocked_facts else logistics_state
                    conn.execute(
                        """
                        UPDATE shipment_logistics
                        SET state = ?, next_attempt_at = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (next_state, now, row["job_id"]),
                    )
                    immediate_logistics = True
                    immediate_logistics_count += 1
                elif (
                    logistics_state == LOGISTICS_READY
                    and row["erp_state"] in {ERP_WAITING, ERP_PENDING, ERP_RETRYABLE, ERP_RUNNING}
                ):
                    conn.execute(
                        """
                        UPDATE shipment_erp
                        SET next_attempt_at = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (now, row["job_id"]),
                    )
                    immediate_erp = True
                    immediate_erp_count += 1
                resumed.append(row["logistics_no"])
                self._insert_event_conn(
                    conn,
                    job_id=row["job_id"],
                    stage="identity",
                    event_type=resume_event_type,
                    old_state=resume_old_state,
                    new_state=IDENTITY_ACTIVE,
                    message=resume_message,
                    details={
                        "source": "pending_review_snapshot",
                        "immediate_logistics": immediate_logistics,
                        "immediate_erp": immediate_erp,
                    },
                    run_id=run_id,
                )
            conn.commit()
        return TagSnapshotReconcileResult(
            snapshot_complete=True,
            paused_count=len(paused),
            resumed_count=len(resumed),
            immediate_logistics_count=immediate_logistics_count,
            immediate_erp_count=immediate_erp_count,
            paused_logistics_numbers=tuple(paused),
            resumed_logistics_numbers=tuple(resumed),
        )

    def add_manual_candidate(
        self,
        *,
        system_order_no: str,
        platform_order_no: str,
        logistics_no: str,
        reason: str,
    ) -> QueueInsertResult:
        """Add or refresh one operator-supplied queue item with an audit event.

        This only changes the local queue.  It never performs an ERP write and
        therefore cannot bypass the desktop write confirmation or emergency
        stop.  Identity fields are intentionally strict because they become
        idempotency keys for later API operations.
        """

        system = str(system_order_no or "").strip()
        platform = str(platform_order_no or "").strip()
        logistics = str(logistics_no or "").strip()
        audit_reason = str(reason or "").strip()
        if not re.fullmatch(r"\d{15,24}", system):
            raise ValueError("系统单号必须是 15 到 24 位数字。")
        if not re.fullmatch(r"(?:\d{3}-\d{7}-\d{7}|wc\d+)", platform, re.IGNORECASE):
            raise ValueError("平台单号格式无效。")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,127}", logistics):
            raise ValueError("物流单号格式无效。")
        if not audit_reason or len(audit_reason) > 500 or "\x00" in audit_reason:
            raise ValueError("手动添加原因必须为 1 到 500 个字符。")

        run_id = f"desktop-manual-{uuid.uuid4().hex}"
        result = self.upsert_candidate(
            ShipmentCandidate(
                system_order_no=system,
                platform_order_no=platform,
                logistics_no=logistics,
                shipment_tag_name="手动添加",
                tag_text="手动添加",
                status_text="桌面队列手动添加",
            ),
            run_id=run_id,
            allow_tag_restore=False,
        )
        job = self.get_by_logistics_no(logistics)
        if not job:
            raise RuntimeError("手动添加后未能读回队列记录。")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_event_conn(
                conn,
                job_id=int(job["job_id"]),
                stage="candidate",
                event_type=("MANUAL_CANDIDATE_ADDED" if result.inserted else "MANUAL_CANDIDATE_REFRESHED"),
                new_state=str(job.get("logistics_state") or ""),
                message=audit_reason,
                details={"source": "desktop_user"},
                run_id=run_id,
            )
            conn.commit()
        return result

    def list_logistics_check_candidates(self, *, limit: int = 0, **_kwargs: Any) -> list[dict[str, Any]]:
        self.initialize()
        now = utc_now()
        sql = self._aggregate_sql() + """
            WHERE j.identity_state = ?
              AND (
                  l.state IN (?, ?, ?)
              )
              AND (l.next_attempt_at IS NULL OR l.next_attempt_at <= ?)
              AND e.state <> ?
              AND (j.lease_until IS NULL OR j.lease_until <= ?)
            ORDER BY CASE WHEN l.state = 'PENDING' THEN 0 ELSE 1 END,
                     COALESCE(l.next_attempt_at, j.created_at), j.id
        """
        params: list[Any] = [
            IDENTITY_ACTIVE, LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE,
            now, ERP_DONE, now,
        ]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._flatten(row) for row in rows]

    def requeue_automated_logistics_blocks(
        self,
        *,
        run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Convert legacy program-created logistics blocks into retries.

        ``BLOCKED`` is reserved for an operator's explicit ``ORDER_ISSUE``
        decision.  Older versions also used it for missing page data, unknown
        carriers, incomplete fields, and tracking mismatches.  Those facts can
        change upstream without the ALS number changing, so they must remain
        visible errors while continuing to retry automatically.
        """

        self.initialize()
        now = utc_now()
        changed: list[str] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                self._aggregate_sql()
                + """
                WHERE j.identity_state = ?
                  AND l.state = ?
                  AND COALESCE(l.tracking_mismatch_action, '') <> ?
                  AND e.state <> ?
                  AND (j.lease_until IS NULL OR j.lease_until <= ?)
                ORDER BY j.id
                """,
                (
                    IDENTITY_ACTIVE,
                    LOGISTICS_BLOCKED,
                    TRACKING_REVIEW_ORDER_ISSUE,
                    ERP_DONE,
                    now,
                ),
            ).fetchall()
            for row in rows:
                current = self._flatten(row)
                logistics_no = str(current.get("logistics_no") or "").strip()
                if not logistics_no:
                    continue
                updated = conn.execute(
                    """
                    UPDATE shipment_logistics
                    SET state = ?, next_attempt_at = ?, updated_at = ?
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        LOGISTICS_RETRYABLE,
                        now,
                        now,
                        current["job_id"],
                        LOGISTICS_BLOCKED,
                    ),
                ).rowcount
                if not updated:
                    continue
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, current["job_id"]),
                )
                self._insert_event_conn(
                    conn,
                    job_id=current["job_id"],
                    stage="logistics",
                    event_type="AUTOMATED_BLOCK_REQUEUED",
                    old_state=LOGISTICS_BLOCKED,
                    new_state=LOGISTICS_RETRYABLE,
                    message=(
                        "旧版本由程序产生的物流阻止已改为自动重试；"
                        "仅保留人工明确锁定的订单。"
                    ),
                    details={"previous_error": current.get("logistics_last_error")},
                    run_id=run_id,
                )
                changed.append(logistics_no)
            conn.commit()
        return tuple(changed)

    def requeue_tracking_mismatches_resolved_by_current_rules(
        self,
        *,
        run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Recheck rows blocked by an older carrier/tracking rule.

        A newly supported tracking family must not remain permanently hidden in
        ``LOGISTICS_BLOCKED``.  This migration deliberately schedules a fresh
        Alibaba page read instead of trusting the stored detail as ready.  An
        explicit operator choice that the order itself is wrong is preserved.
        """

        self.initialize()
        now = utc_now()
        changed: list[str] = []
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                self._aggregate_sql()
                + """
                WHERE j.identity_state = ?
                  AND l.state = ?
                  AND l.last_error LIKE ?
                  AND l.tracking_override_at IS NULL
                  AND (
                      l.tracking_mismatch_action IS NULL
                      OR l.tracking_mismatch_action = ?
                  )
                  AND e.state <> ?
                  AND (j.lease_until IS NULL OR j.lease_until <= ?)
                ORDER BY j.id
                """,
                (
                    IDENTITY_ACTIVE,
                    LOGISTICS_BLOCKED,
                    f"{TRACKING_MISMATCH_REASON_PREFIX}%",
                    TRACKING_REVIEW_AUTO_RECHECK,
                    ERP_DONE,
                    now,
                ),
            ).fetchall()
            for row in rows:
                current = self._flatten(row)
                carrier = current.get("carrier_normalized") or current.get("carrier_raw")
                tracking_no = current.get("international_tracking_no")
                if not tracking_number_matches_carrier(carrier, tracking_no):
                    continue
                logistics_no = str(current.get("logistics_no") or "").strip()
                if not logistics_no:
                    continue
                updated = conn.execute(
                    """
                    UPDATE shipment_logistics
                    SET state = ?, next_attempt_at = ?, last_error = NULL,
                        tracking_mismatch_action = NULL,
                        tracking_mismatch_reviewed_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        LOGISTICS_RETRYABLE,
                        now,
                        now,
                        current["job_id"],
                        LOGISTICS_BLOCKED,
                    ),
                ).rowcount
                if not updated:
                    continue
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, current["job_id"]),
                )
                self._insert_event_conn(
                    conn,
                    job_id=current["job_id"],
                    stage="logistics",
                    event_type="TRACKING_RULE_MATCH_REQUEUED",
                    old_state=LOGISTICS_BLOCKED,
                    new_state=LOGISTICS_RETRYABLE,
                    message="当前承运商与国际物流单号规则已匹配，已安排重新读取阿里物流页确认。",
                    details={
                        "carrier": normalize_carrier_name(carrier),
                        "tracking_no": normalize_tracking_number(tracking_no),
                    },
                    run_id=run_id,
                )
                changed.append(logistics_no)
            conn.commit()
        return tuple(changed)

    def requeue_obvious_tracking_parser_artifacts(
        self,
        *,
        run_id: str | None = None,
    ) -> tuple[str, ...]:
        """Requeue legacy parser artifacts and recognized intermediary numbers atomically."""

        self.initialize()
        now = utc_now()
        changed: list[str] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                self._aggregate_sql()
                + """
                WHERE j.identity_state = ?
                  AND l.state = ?
                  AND l.last_error LIKE ?
                  AND l.tracking_override_at IS NULL
                  AND (l.tracking_mismatch_action IS NULL OR l.tracking_mismatch_action = ?)
                  AND e.state <> ?
                  AND (j.lease_until IS NULL OR j.lease_until <= ?)
                ORDER BY j.id
                """,
                (
                    IDENTITY_ACTIVE,
                    LOGISTICS_BLOCKED,
                    f"{TRACKING_MISMATCH_REASON_PREFIX}%",
                    TRACKING_REVIEW_AUTO_RECHECK,
                    ERP_DONE,
                    now,
                ),
            ).fetchall()
            for row in rows:
                current = self._flatten(row)
                logistics_no = str(current.get("logistics_no") or "").strip()
                carrier = current.get("carrier_normalized") or current.get("carrier_raw")
                tracking_no = current.get("international_tracking_no")
                if not is_obvious_tracking_parser_artifact(
                    logistics_no,
                    carrier,
                    tracking_no,
                ):
                    continue
                decision = classify_tracking_candidate(
                    logistics_no,
                    carrier,
                    tracking_no,
                )
                old_error = str(current.get("logistics_last_error") or "").strip()
                if decision.category in {"placeholder", "intermediary"}:
                    previous_tracking: str | None = str(tracking_no or "").strip() or None
                    previous_error = old_error or None
                else:
                    previous_tracking = "[页面文案已隐藏]"
                    previous_error = "[旧错误包含页面文案，已隐藏]"
                intermediary = decision.category == "intermediary"
                target_state = LOGISTICS_WAITING if intermediary else LOGISTICS_RETRYABLE
                target_error = (
                    decision.reason
                    if intermediary
                    else "检测到旧版物流字段解析污染，等待重新查询。"
                )
                conn.execute(
                    """
                    UPDATE shipment_logistics
                    SET state = ?, international_tracking_no = NULL,
                        next_attempt_at = ?, last_error = ?,
                        tracking_mismatch_action = NULL,
                        tracking_mismatch_reviewed_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND state = ?
                    """,
                    (
                        target_state,
                        now,
                        target_error,
                        now,
                        int(current["job_id"]),
                        LOGISTICS_BLOCKED,
                    ),
                )
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, int(current["job_id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(current["job_id"]),
                    stage="logistics",
                    event_type=(
                        "LOGISTICS_INTERMEDIARY_TRACKING_REQUEUED"
                        if intermediary
                        else "LOGISTICS_PARSER_ARTIFACT_REQUEUED"
                    ),
                    old_state=LOGISTICS_BLOCKED,
                    new_state=target_state,
                    message=(
                        "阿里中间物流单号已转为等待真实尾程单号。"
                        if intermediary
                        else "旧版物流字段解析污染已清除并重新排队。"
                    ),
                    details={
                        "source": "automatic_parser_repair",
                        "artifact_class": decision.category,
                        "previous_tracking_no": previous_tracking,
                        "previous_error": previous_error,
                        "previous_value_sha256": hashlib.sha256(
                            str(tracking_no or "").encode("utf-8")
                        ).hexdigest(),
                    },
                    run_id=run_id,
                )
                changed.append(logistics_no)
            conn.commit()
        return tuple(changed)

    def claim_logistics_jobs(self, owner: str, *, limit: int = 0, lease_seconds: int = 1200) -> list[dict[str, Any]]:
        return self._claim_jobs("logistics", owner, limit=limit, lease_seconds=lease_seconds)

    def claim_erp_jobs(
        self,
        owner: str,
        *,
        limit: int = 0,
        lease_seconds: int = 14400,
        logistics_no: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._claim_jobs(
            "erp",
            owner,
            limit=limit,
            lease_seconds=lease_seconds,
            logistics_no=logistics_no,
        )

    def _claim_jobs(
        self,
        stage: str,
        owner: str,
        *,
        limit: int,
        lease_seconds: int,
        logistics_no: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        now = utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if stage == "logistics":
                where = """
                    j.identity_state = ? AND l.state IN (?, ?, ?)
                    AND (l.next_attempt_at IS NULL OR l.next_attempt_at <= ?)
                    AND e.state <> ?
                """
                params: list[Any] = [
                    IDENTITY_ACTIVE, LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE,
                    now, ERP_DONE,
                ]
                order = (
                    "CASE WHEN l.state = 'PENDING' THEN 0 ELSE 1 END, "
                    "COALESCE(l.next_attempt_at, j.created_at), j.id"
                )
            else:
                self._refresh_order_policy_evidence_conn(conn)
                self._reconcile_amazon_main_image_policy_conn(
                    conn,
                    logistics_no=logistics_no,
                )
                where = """
                    j.identity_state = ? AND l.state = ? AND e.state IN (?, ?, ?)
                    AND e.policy_block_code IS NULL
                    AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= ?)
                """
                params = [IDENTITY_ACTIVE, LOGISTICS_READY, ERP_PENDING, ERP_RETRYABLE, ERP_RUNNING, now]
                order = "COALESCE(e.next_attempt_at, j.created_at), j.id"
            where += """
                AND NOT EXISTS (
                    SELECT 1
                    FROM shipment_scan_issues scan_issue
                    WHERE scan_issue.system_order_no = j.system_order_no
                      AND scan_issue.platform_order_no = j.platform_order_no
                      AND (
                          scan_issue.resolved_at IS NULL
                          OR scan_issue.management_state <> ?
                      )
                )
            """
            params.append(SCAN_ISSUE_ACTIVE)
            if logistics_no is not None:
                where += " AND j.logistics_no = ?"
                params.append(logistics_no)
            sql = self._aggregate_sql() + f"""
                WHERE {where}
                  AND (j.lease_until IS NULL OR j.lease_until <= ? OR j.lease_owner = ?)
                ORDER BY {order}
            """
            params.extend([now, owner])
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            selected = conn.execute(sql, params).fetchall()
            ids = [row["id"] for row in selected]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE shipment_jobs
                    SET lease_owner = ?, lease_stage = ?, lease_until = ?, version = version + 1
                    WHERE id IN ({placeholders})
                    """,
                    [owner, stage, lease_until, *ids],
                )
            conn.commit()
        return [self.get_by_logistics_no(row["logistics_no"]) for row in selected]

    def renew_lease(self, logistics_no: str, owner: str, *, lease_seconds: int = 14400) -> bool:
        self.initialize()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.connect() as conn:
            result = conn.execute(
                "UPDATE shipment_jobs SET lease_until = ? WHERE logistics_no = ? AND lease_owner = ?",
                (lease_until, logistics_no, owner),
            )
        return result.rowcount > 0

    def release_claimed_jobs(self, owner: str, stage: str) -> int:
        """Release only leases still owned by one interrupted worker."""

        normalized_owner = str(owner or "").strip()
        normalized_stage = str(stage or "").strip().lower()
        if not normalized_owner:
            return 0
        if normalized_stage not in {"logistics", "erp"}:
            raise ValueError(f"Unsupported lease stage: {stage}")
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE lease_owner = ? AND lease_stage = ?
                """,
                (now, normalized_owner, normalized_stage),
            )
            conn.commit()
        return int(result.rowcount)

    def complete_logistics_attempt(
        self,
        logistics_no: str,
        detail: LogisticsDetail,
        *,
        state: str,
        last_error: str | None,
        owner: str | None = None,
        expected_version: int | None = None,
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        old_state = job["logistics_state"]
        if (
            state == LOGISTICS_BLOCKED
            and job.get("tracking_mismatch_action") != TRACKING_REVIEW_ORDER_ISSUE
        ):
            # Programmatic parsing/readiness failures remain retryable.  Only
            # an explicit operator ORDER_ISSUE decision may persist BLOCKED.
            state = LOGISTICS_RETRYABLE
        currency, fee_amount = _split_money(detail.actual_total)
        now = utc_now()
        mismatch_blocked = state == LOGISTICS_BLOCKED and is_tracking_number_mismatch_reason(last_error)
        next_attempt = (
            utc_after()
            if state in {LOGISTICS_WAITING, LOGISTICS_RETRYABLE}
            or (
                mismatch_blocked
                and job.get("tracking_mismatch_action")
                == TRACKING_REVIEW_AUTO_RECHECK
            )
            else None
        )
        keep_tracking_override = bool(
            job.get("tracking_override_at")
            and normalize_carrier_name(detail.carrier) == job.get("tracking_override_carrier")
            and normalize_tracking_number(detail.international_tracking_no) == job.get("tracking_override_no")
        )
        policy_violation = (
            amazon_main_image_policy_violation(
                platform_order_no=job.get("platform_order_no"),
                sales_platform_code=job.get("sales_platform_code"),
                sales_platform_name=job.get("sales_platform_name"),
                has_main_image=job.get("has_main_image"),
                carrier=detail.carrier,
                tracking_no=detail.international_tracking_no,
            )
            if state == LOGISTICS_READY
            else None
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conditions = ["id = ?"]
            params: list[Any] = [job["job_id"]]
            if owner is not None:
                conditions.append("lease_owner = ?")
                conditions.append("lease_stage = 'logistics'")
                params.append(owner)
            if expected_version is not None:
                conditions.append("version = ?")
                params.append(expected_version)
            guarded = conn.execute(
                f"SELECT id FROM shipment_jobs WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
            if not guarded:
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, alibaba_status = ?, service_type = ?, service_line = ?,
                    carrier_raw = ?,
                    carrier_normalized = ?, international_tracking_no = ?, currency = ?,
                    fee_amount = ?, chargeable_weight_kg = ?, package_count = ?, source_url = ?,
                    tracking_override_carrier = CASE WHEN ? THEN tracking_override_carrier ELSE NULL END,
                    tracking_override_no = CASE WHEN ? THEN tracking_override_no ELSE NULL END,
                    tracking_override_at = CASE WHEN ? THEN tracking_override_at ELSE NULL END,
                    tracking_override_reason = CASE WHEN ? THEN tracking_override_reason ELSE NULL END,
                    tracking_mismatch_action = CASE WHEN ? THEN NULL ELSE tracking_mismatch_action END,
                    tracking_mismatch_reviewed_at = CASE WHEN ? THEN NULL ELSE tracking_mismatch_reviewed_at END,
                    last_checked_at = ?, next_attempt_at = ?, attempt_count = attempt_count + 1,
                    last_error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    state, detail.status_text, detail.service_type, detail.service_line,
                    detail.carrier, detail.carrier,
                    detail.international_tracking_no, currency, fee_amount,
                    _normalize_decimal(detail.chargeable_weight_kg), detail.package_count,
                    detail.source_url,
                    keep_tracking_override, keep_tracking_override,
                    keep_tracking_override, keep_tracking_override,
                    state == LOGISTICS_READY, state == LOGISTICS_READY,
                    now, next_attempt, last_error, now, job["job_id"],
                ),
            )
            if state == LOGISTICS_READY:
                if policy_violation is not None:
                    conn.execute(
                        """
                        UPDATE shipment_erp
                        SET state = ?, next_attempt_at = NULL, last_error = ?,
                            policy_block_code = ?, updated_at = ?
                        WHERE job_id = ? AND state <> ?
                        """,
                        (
                            ERP_BLOCKED,
                            policy_violation.message,
                            policy_violation.code,
                            now,
                            job["job_id"],
                            ERP_DONE,
                        ),
                    )
                    self._insert_event_conn(
                        conn,
                        job_id=job["job_id"],
                        stage="erp",
                        event_type="AMAZON_MAIN_IMAGE_CHANNEL_BLOCKED",
                        old_state=job.get("erp_state"),
                        new_state=ERP_BLOCKED,
                        message=policy_violation.message,
                        details={
                            "policy_code": policy_violation.code,
                            "carrier_key": policy_violation.carrier_key,
                            "channel_path": list(policy_violation.channel_path),
                            "source": "logistics_completion",
                        },
                        run_id=run_id,
                    )
                elif job.get("policy_block_code") == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL:
                    conn.execute(
                        """
                        UPDATE shipment_erp
                        SET state = ?, next_attempt_at = NULL, updated_at = ?
                        WHERE job_id = ? AND state <> ?
                        """,
                        (ERP_BLOCKED, now, job["job_id"], ERP_DONE),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE shipment_erp
                        SET state = CASE WHEN state = ? THEN ? ELSE state END,
                            next_attempt_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (
                            ERP_WAITING,
                            ERP_PENDING,
                            now,
                            job["job_id"],
                        ),
                    )
            conn.execute(
                """
                UPDATE shipment_jobs SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1 WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="logistics",
                event_type="LOGISTICS_ATTEMPT_COMPLETED",
                old_state=old_state,
                new_state=state,
                message=last_error,
                run_id=run_id,
            )
            conn.commit()
        return True

    def return_tracking_to_blocked(
        self,
        logistics_no: str,
        *,
        reason: str,
        owner: str | None = None,
        expected_version: int | None = None,
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job or job["erp_state"] == ERP_DONE:
            return False
        now = utc_now()
        manually_blocked = (
            job.get("tracking_mismatch_action") == TRACKING_REVIEW_ORDER_ISSUE
        )
        target_state = LOGISTICS_BLOCKED if manually_blocked else LOGISTICS_RETRYABLE
        next_attempt = None if manually_blocked else utc_after()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conditions = ["id = ?"]
            params: list[Any] = [job["job_id"]]
            if owner is not None:
                conditions.extend(["lease_owner = ?", "lease_stage = 'erp'"])
                params.append(owner)
            if expected_version is not None:
                conditions.append("version = ?")
                params.append(expected_version)
            if not conn.execute(
                f"SELECT 1 FROM shipment_jobs WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone():
                conn.rollback()
                return False
            rollback_logistics = job["erp_checkpoint"] == ERP_CHECKPOINT_LOGISTICS_SAVED
            next_checkpoint = ERP_CHECKPOINT_AUDITED if rollback_logistics else job["erp_checkpoint"]
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (target_state, next_attempt, reason, now, job["job_id"]),
            )
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, checkpoint = ?, next_attempt_at = NULL, last_error = NULL,
                    logistics_payload_hash = CASE WHEN ? THEN NULL ELSE logistics_payload_hash END,
                    logistics_confirmed_at = CASE WHEN ? THEN NULL ELSE logistics_confirmed_at END,
                    logistics_saved_at = CASE WHEN ? THEN NULL ELSE logistics_saved_at END,
                    freight_amount = CASE WHEN ? THEN NULL ELSE freight_amount END,
                    chargeable_weight_g = CASE WHEN ? THEN NULL ELSE chargeable_weight_g END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    ERP_WAITING, next_checkpoint,
                    rollback_logistics, rollback_logistics, rollback_logistics,
                    rollback_logistics, rollback_logistics,
                    now, job["job_id"],
                ),
            )

            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="logistics",
                event_type=(
                    "TRACKING_NUMBER_BLOCKED"
                    if manually_blocked
                    else "TRACKING_NUMBER_RETRYABLE"
                ),
                old_state=f"{job['logistics_state']}/{job['erp_state']}",
                new_state=f"{target_state}/{ERP_WAITING}",
                message=reason,
                details={
                    "carrier": job.get("carrier"),
                    "tracking_no": job.get("international_tracking_no"),
                    "previous_checkpoint": job.get("erp_checkpoint"),
                    "checkpoint": next_checkpoint,
                },
                run_id=run_id,
            )
            conn.commit()
        return True

    def block_invalid_tracking_records(
        self,
        *,
        run_id: str | None = None,
        logistics_no: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        sql = (
            self._aggregate_sql()
            + """
              WHERE j.identity_state = ? AND l.state = ? AND e.state <> ?
            """
        )
        params: list[Any] = [IDENTITY_ACTIVE, LOGISTICS_READY, ERP_DONE]
        if logistics_no is not None:
            sql += " AND j.logistics_no = ?"
            params.append(logistics_no)
        sql += " ORDER BY j.id"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        requeued: list[dict[str, Any]] = []
        for row in rows:
            item = self._flatten(row)
            manually_verified = bool(
                item.get("tracking_override_at")
                and normalize_carrier_name(item.get("carrier")) == item.get("tracking_override_carrier")
                and normalize_tracking_number(item.get("international_tracking_no")) == item.get("tracking_override_no")
            )
            if manually_verified or tracking_number_matches_carrier(
                item.get("carrier"), item.get("international_tracking_no")
            ):
                continue
            reason = tracking_number_mismatch_reason(item.get("carrier"), item.get("international_tracking_no"))
            if self.return_tracking_to_blocked(item["logistics_no"], reason=reason, run_id=run_id):
                requeued.append({**item, "last_error": reason})
        return requeued

    def list_pending_tracking_mismatch_reviews(self) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                self._aggregate_sql()
                + """
                  WHERE j.identity_state = ? AND l.state IN (?, ?)
                    AND l.tracking_mismatch_action IS NULL
                    AND e.state <> ?
                  ORDER BY j.updated_at, j.id
                """,
                (
                    IDENTITY_ACTIVE,
                    LOGISTICS_RETRYABLE,
                    LOGISTICS_BLOCKED,
                    ERP_DONE,
                ),
            ).fetchall()
        return [
            item
            for item in (self._flatten(row) for row in rows)
            if is_tracking_number_mismatch_reason(item.get("logistics_last_error"))
            and not is_obvious_tracking_parser_artifact(
                item.get("logistics_no"),
                item.get("carrier"),
                item.get("international_tracking_no"),
            )
        ]

    def set_tracking_mismatch_review(
        self,
        logistics_no: str,
        action: str,
        *,
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        if action not in {TRACKING_REVIEW_AUTO_RECHECK, TRACKING_REVIEW_ORDER_ISSUE}:
            raise ValueError(f"Unsupported tracking mismatch review action: {action}")
        job = self.get_by_logistics_no(logistics_no)
        if (
            not job
            or job["identity_state"] != IDENTITY_ACTIVE
            or job["erp_state"] == ERP_DONE
            or job["logistics_state"] not in {LOGISTICS_RETRYABLE, LOGISTICS_BLOCKED}
            or not is_tracking_number_mismatch_reason(job.get("logistics_last_error"))
        ):
            return False
        now = utc_now()
        target_state = (
            LOGISTICS_BLOCKED
            if action == TRACKING_REVIEW_ORDER_ISSUE
            else LOGISTICS_RETRYABLE
        )
        next_attempt = now if target_state == LOGISTICS_RETRYABLE else None
        old_action = job.get("tracking_mismatch_action")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, tracking_mismatch_action = ?, tracking_mismatch_reviewed_at = ?,
                    next_attempt_at = ?, tracking_override_carrier = NULL,
                    tracking_override_no = NULL, tracking_override_at = NULL,
                    tracking_override_reason = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (target_state, action, now, next_attempt, now, job["job_id"]),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="logistics",
                event_type="TRACKING_MISMATCH_REVIEWED",
                old_state=old_action,
                new_state=action,
                message=(
                    "已选择每三小时自动复查中间商单号。"
                    if action == TRACKING_REVIEW_AUTO_RECHECK
                    else "已标记订单有问题并停止自动物流查询。"
                ),
                details={
                    "carrier": job.get("carrier"),
                    "tracking_no": job.get("international_tracking_no"),
                },
                run_id=run_id,
            )
            conn.commit()
        return True

    def confirm_tracking_override(
        self,
        logistics_no: str,
        *,
        reason: str = "用户在交互式队列管理中确认当前尾程单号",
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job or job["erp_state"] == ERP_DONE:
            return False
        if job.get("policy_block_code") == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL:
            return False
        if job["logistics_state"] not in {LOGISTICS_RETRYABLE, LOGISTICS_BLOCKED}:
            return False
        if not is_tracking_number_mismatch_reason(job.get("logistics_last_error")):
            return False
        required = (
            job.get("carrier"),
            job.get("international_tracking_no"),
            job.get("actual_total"),
            job.get("chargeable_weight_kg"),
        )
        if not all(str(value or "").strip() for value in required):
            return False
        now = utc_now()
        carrier_key = normalize_carrier_name(job.get("carrier"))
        tracking_key = normalize_tracking_number(job.get("international_tracking_no"))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, next_attempt_at = NULL, last_error = NULL,
                    tracking_override_carrier = ?, tracking_override_no = ?,
                    tracking_override_at = ?, tracking_override_reason = ?,
                    tracking_mismatch_action = NULL,
                    tracking_mismatch_reviewed_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (LOGISTICS_READY, carrier_key, tracking_key, now, reason, now, job["job_id"]),
            )
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, next_attempt_at = NULL, last_error = NULL, updated_at = ?
                WHERE job_id = ? AND state <> ?
                """,
                (ERP_PENDING, now, job["job_id"], ERP_DONE),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="logistics",
                event_type="TRACKING_NUMBER_MANUALLY_CONFIRMED",
                old_state=f"{job['logistics_state']}/{job['erp_state']}",
                new_state=f"{LOGISTICS_READY}/{ERP_PENDING}",
                message=reason,
                details={"carrier": carrier_key, "tracking_no": tracking_key},
                run_id=run_id,
            )
            conn.commit()
        return True

    def confirm_tracking_pair(
        self,
        logistics_no: str,
        *,
        carrier: str,
        tracking_no: str,
        reason: str = "用户在桌面确认承运商和运单号后执行标发及客户通知",
        run_id: str | None = None,
    ) -> bool:
        """原子保存人工核对后的承运商/运单号，并放行这一精确组合。"""

        self.initialize()
        carrier_key = normalize_carrier_name(carrier)
        carrier_display = REAL_OVERSEAS_CARRIER_DISPLAY_NAMES.get(carrier_key)
        tracking_key = normalize_tracking_number(tracking_no)
        audit_reason = str(reason or "").strip()
        if not carrier_display:
            raise ValueError("请选择系统支持的真实尾程承运商。")
        if not tracking_key or not tracking_key.isalnum():
            raise ValueError("国际物流单号只能包含字母和数字。")
        if not tracking_number_matches_carrier(carrier_key, tracking_key):
            raise ValueError(tracking_number_mismatch_reason(carrier_display, tracking_key))
        if not audit_reason or len(audit_reason) > 500:
            raise ValueError("人工确认原因必须为 1 到 500 个字符。")

        job = self.get_by_logistics_no(logistics_no)
        if not job or job["erp_state"] == ERP_DONE:
            return False
        if str(job.get("identity_state") or "") != IDENTITY_ACTIVE:
            return False
        policy_recovery = (
            job.get("policy_block_code") == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL
        )
        if (
            str(job.get("erp_checkpoint") or ERP_CHECKPOINT_NONE)
            != ERP_CHECKPOINT_NONE
            and not policy_recovery
        ):
            return False
        if job.get("lease_until") and str(job["lease_until"]) > utc_now():
            return False
        required = (job.get("actual_total"), job.get("chargeable_weight_kg"))
        if not all(str(value or "").strip() for value in required):
            return False
        policy_violation = amazon_main_image_policy_violation(
            platform_order_no=job.get("platform_order_no"),
            sales_platform_code=job.get("sales_platform_code"),
            sales_platform_name=job.get("sales_platform_name"),
            has_main_image=job.get("has_main_image"),
            carrier=carrier_key,
            tracking_no=tracking_key,
        )
        if policy_violation is not None:
            raise ValueError(
                f"{policy_violation.message} 当前填写的承运商和单号仍未解除限制。"
            )

        now = utc_now()
        old_pair = {
            "carrier": job.get("carrier"),
            "tracking_no": job.get("international_tracking_no"),
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_logistics
                SET state = ?, carrier_raw = ?, carrier_normalized = ?,
                    international_tracking_no = ?, next_attempt_at = NULL,
                    last_error = NULL, tracking_override_carrier = ?,
                    tracking_override_no = ?, tracking_override_at = ?,
                    tracking_override_reason = ?, tracking_mismatch_action = NULL,
                    tracking_mismatch_reviewed_at = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    LOGISTICS_READY,
                    carrier_display,
                    carrier_display,
                    tracking_key,
                    carrier_key,
                    tracking_key,
                    now,
                    audit_reason,
                    now,
                    job["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, checkpoint = CASE WHEN ? THEN ? ELSE checkpoint END,
                    channel_path = CASE WHEN ? THEN NULL ELSE channel_path END,
                    freight_amount = CASE WHEN ? THEN NULL ELSE freight_amount END,
                    chargeable_weight_g = CASE WHEN ? THEN NULL ELSE chargeable_weight_g END,
                    channel_payload_hash = CASE WHEN ? THEN NULL ELSE channel_payload_hash END,
                    logistics_payload_hash = CASE WHEN ? THEN NULL ELSE logistics_payload_hash END,
                    channel_confirmed_at = CASE WHEN ? THEN NULL ELSE channel_confirmed_at END,
                    logistics_confirmed_at = CASE WHEN ? THEN NULL ELSE logistics_confirmed_at END,
                    channel_set_at = CASE WHEN ? THEN NULL ELSE channel_set_at END,
                    audited_at = CASE WHEN ? THEN NULL ELSE audited_at END,
                    logistics_saved_at = CASE WHEN ? THEN NULL ELSE logistics_saved_at END,
                    selected_wms_wo_number = CASE WHEN ? THEN NULL ELSE selected_wms_wo_number END,
                    selected_wms_candidates_hash = CASE WHEN ? THEN NULL ELSE selected_wms_candidates_hash END,
                    selected_wms_selected_at = CASE WHEN ? THEN NULL ELSE selected_wms_selected_at END,
                    selected_wms_selected_by = CASE WHEN ? THEN NULL ELSE selected_wms_selected_by END,
                    next_attempt_at = NULL, last_error = NULL, policy_block_code = NULL,
                    updated_at = ?
                WHERE job_id = ? AND state <> ?
                """,
                (
                    ERP_PENDING,
                    policy_recovery,
                    ERP_CHECKPOINT_NONE,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    policy_recovery,
                    now,
                    job["job_id"],
                    ERP_DONE,
                ),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="logistics",
                event_type="TRACKING_PAIR_MANUALLY_CONFIRMED",
                old_state=f"{job['logistics_state']}/{job['erp_state']}",
                new_state=f"{LOGISTICS_READY}/{ERP_PENDING}",
                message=audit_reason,
                details={
                    "old_pair": old_pair,
                    "carrier": carrier_display,
                    "carrier_key": carrier_key,
                    "tracking_no": tracking_key,
                    "policy_recovery": policy_recovery,
                },
                run_id=run_id,
            )
            conn.commit()
        return True

    def list_ready_to_mark(
        self,
        *,
        limit: int = 0,
        logistics_no: str | None = None,
    ) -> list[ReadyToMarkItem]:
        self.initialize()
        sql = self._aggregate_sql() + """
            WHERE j.identity_state = ? AND l.state = ? AND e.state IN (?, ?, ?)
              AND e.policy_block_code IS NULL
        """
        params: list[Any] = [IDENTITY_ACTIVE, LOGISTICS_READY, ERP_PENDING, ERP_RUNNING, ERP_RETRYABLE]
        if logistics_no is not None:
            sql += " AND j.logistics_no = ?"
            params.append(logistics_no)
        sql += " ORDER BY j.updated_at, j.id"
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._ready_item(row) for row in rows]

    def list_erp_mark_candidates(
        self,
        *,
        limit: int = 0,
        logistics_no: str | None = None,
    ) -> list[ReadyToMarkItem]:
        return self.list_ready_to_mark(limit=limit, logistics_no=logistics_no)

    def claimed_erp_items(
        self,
        owner: str,
        *,
        limit: int = 0,
        logistics_no: str | None = None,
    ) -> list[ReadyToMarkItem]:
        return [
            self._ready_item(row)
            for row in self.claim_erp_jobs(owner, limit=limit, logistics_no=logistics_no)
        ]

    @staticmethod
    def _ready_item(row: sqlite3.Row | dict[str, Any]) -> ReadyToMarkItem:
        item = dict(row)
        actual_total = f"{item.get('currency') or ''} {item.get('fee_amount') or ''}".strip() or None
        return ReadyToMarkItem(
            system_order_no=item["system_order_no"],
            platform_order_no=item["platform_order_no"],
            logistics_no=item["logistics_no"],
            carrier=item.get("carrier_normalized") or item.get("carrier_raw"),
            service_line=item.get("service_line"),
            international_tracking_no=item.get("international_tracking_no"),
            actual_total=actual_total,
            chargeable_weight_kg=item.get("chargeable_weight_kg"),
            job_id=item.get("job_id") or item.get("id"),
            version=int(item.get("version") or 0),
            lease_owner=item.get("lease_owner"),
            erp_state=item.get("erp_state") or ERP_PENDING,
            erp_checkpoint=item.get("erp_checkpoint") or ERP_CHECKPOINT_NONE,
            channel_payload_hash=item.get("channel_payload_hash"),
            logistics_payload_hash=item.get("logistics_payload_hash"),
            selected_wms_wo_number=item.get("selected_wms_wo_number"),
            selected_wms_candidates_hash=item.get("selected_wms_candidates_hash"),
            sales_channel=item.get("sales_channel") or SALES_CHANNEL_MARKETPLACE,
            customer_email_required=bool(item.get("customer_email_required", 1)),
            tracking_manually_verified=bool(
                item.get("tracking_override_at")
                and normalize_carrier_name(item.get("carrier_normalized") or item.get("carrier_raw"))
                == item.get("tracking_override_carrier")
                and normalize_tracking_number(item.get("international_tracking_no"))
                == item.get("tracking_override_no")
            ),
            sales_platform_code=str(item.get("sales_platform_code") or ""),
            sales_platform_name=str(item.get("sales_platform_name") or ""),
            has_main_image=bool(item.get("has_main_image")),
        )

    def record_erp_checkpoint(
        self,
        logistics_no: str,
        *,
        owner: str,
        expected_version: int,
        checkpoint: str,
        channel_path: str | None = None,
        freight_amount: str | None = None,
        chargeable_weight_g: str | None = None,
        channel_payload_hash: str | None = None,
        logistics_payload_hash: str | None = None,
        run_id: str | None = None,
        email_preview_enabled: bool = False,
    ) -> int | None:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return None
        timestamp_column = {
            ERP_CHECKPOINT_CHANNEL_SET: "channel_set_at",
            ERP_CHECKPOINT_AUDITED: "audited_at",
            ERP_CHECKPOINT_LOGISTICS_SAVED: "logistics_saved_at",
            ERP_CHECKPOINT_OUTBOUNDED: "outbounded_at",
        }.get(checkpoint)
        if not timestamp_column:
            raise ValueError(f"Unsupported ERP checkpoint: {checkpoint}")
        now = utc_now()
        state = ERP_DONE if checkpoint == ERP_CHECKPOINT_OUTBOUNDED else ERP_RUNNING
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            guarded = conn.execute(
                """
                SELECT id FROM shipment_jobs
                WHERE id = ? AND lease_owner = ? AND lease_stage = 'erp' AND version = ?
                """,
                (job["job_id"], owner, expected_version),
            ).fetchone()
            if not guarded:
                conn.rollback()
                return None
            conn.execute(
                f"""
                UPDATE shipment_erp
                SET state = ?, checkpoint = ?, channel_path = COALESCE(?, channel_path),
                    freight_amount = COALESCE(?, freight_amount),
                    chargeable_weight_g = COALESCE(?, chargeable_weight_g),
                    channel_payload_hash = COALESCE(?, channel_payload_hash),
                    logistics_payload_hash = COALESCE(?, logistics_payload_hash),
                    {timestamp_column} = ?, last_error = NULL,
                    completion_source = CASE WHEN ? THEN ? ELSE completion_source END,
                    externally_completed_at = CASE WHEN ? THEN NULL ELSE externally_completed_at END,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    state, checkpoint, channel_path, freight_amount, chargeable_weight_g,
                    channel_payload_hash, logistics_payload_hash, now,
                    checkpoint == ERP_CHECKPOINT_OUTBOUNDED, ERP_COMPLETION_AUTOMATION,
                    checkpoint == ERP_CHECKPOINT_OUTBOUNDED, now, job["job_id"],
                ),
            )

            release = checkpoint == ERP_CHECKPOINT_OUTBOUNDED
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_stage = CASE WHEN ? THEN NULL ELSE lease_stage END,
                    lease_until = CASE WHEN ? THEN NULL ELSE lease_until END,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (release, release, release, now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"], stage="erp", event_type="ERP_CHECKPOINT_RECORDED",
                old_state=job["erp_checkpoint"], new_state=checkpoint, run_id=run_id,
            )
            new_version = conn.execute("SELECT version FROM shipment_jobs WHERE id = ?", (job["job_id"],)).fetchone()[0]
            conn.commit()
        if checkpoint == ERP_CHECKPOINT_OUTBOUNDED and email_preview_enabled:
            self.prepare_email_batches(platform_order_no=job["platform_order_no"])
        return int(new_version)

    def record_wms_outbound_selection(
        self,
        logistics_no: str,
        *,
        owner: str,
        expected_version: int,
        selected_wo_number: str,
        candidates: Iterable[Mapping[str, Any]],
        actor: str = "desktop_user",
        run_id: str | None = None,
    ) -> int | None:
        """Persist the one WMS outbound row explicitly selected by the operator."""

        self.initialize()
        selected = str(selected_wo_number or "").strip()
        summaries: list[dict[str, str]] = []
        for candidate in candidates:
            wo_number = str(candidate.get("wo_number") or "").strip()
            if not wo_number:
                continue
            summaries.append(
                {
                    "wo_number": wo_number,
                    "order_number": str(candidate.get("order_number") or "").strip(),
                    "platform_order_no": "|".join(
                        sorted(
                            str(value).strip()
                            for value in (
                                candidate.get("platform_order_no")
                                if isinstance(candidate.get("platform_order_no"), (list, tuple, set))
                                else (candidate.get("platform_order_no"),)
                            )
                            if str(value or "").strip()
                        )
                    ),
                    "status": str(candidate.get("status") or "").strip(),
                }
            )
        summaries.sort(key=lambda value: value["wo_number"])
        candidate_numbers = [value["wo_number"] for value in summaries]
        if not selected or candidate_numbers.count(selected) != 1:
            raise ValueError("选定的销售出库单不在当前唯一候选集合中。")
        candidates_hash = hashlib.sha256(
            json.dumps(summaries, ensure_ascii=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT j.id, j.version, j.lease_owner, j.lease_stage,
                       e.checkpoint, e.selected_wms_wo_number,
                       e.selected_wms_candidates_hash
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.logistics_no = ?
                """,
                (logistics_no,),
            ).fetchone()
            if (
                row is None
                or str(row["lease_owner"] or "") != owner
                or str(row["lease_stage"] or "") != "erp"
                or int(row["version"]) != int(expected_version)
            ):
                conn.rollback()
                return None
            previous = str(row["selected_wms_wo_number"] or "").strip()
            if (
                previous
                and previous != selected
                and str(row["checkpoint"] or ERP_CHECKPOINT_NONE) != ERP_CHECKPOINT_NONE
            ):
                conn.rollback()
                raise ValueError("标发检查点已推进，禁止更换销售出库单。")
            if (
                previous == selected
                and str(row["selected_wms_candidates_hash"] or "") == candidates_hash
            ):
                conn.rollback()
                return int(expected_version)
            conn.execute(
                """
                UPDATE shipment_erp
                SET selected_wms_wo_number = ?, selected_wms_candidates_hash = ?,
                    selected_wms_selected_at = ?, selected_wms_selected_by = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (selected, candidates_hash, now, actor.strip() or "desktop_user", now, row["id"]),
            )
            conn.execute(
                """
                UPDATE shipment_jobs
                SET updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=int(row["id"]),
                stage="erp",
                event_type=(
                    "ERP_WMS_OUTBOUND_SELECTION_CHANGED"
                    if previous and previous != selected
                    else "ERP_WMS_OUTBOUND_SELECTED"
                ),
                old_state=previous or None,
                new_state=selected,
                message="用户明确选择了要继续标发的销售出库单。",
                details={
                    "candidate_wo_numbers": candidate_numbers,
                    "candidates_hash": candidates_hash,
                    "actor": actor.strip() or "desktop_user",
                },
                run_id=run_id,
            )
            new_version = int(
                conn.execute(
                    "SELECT version FROM shipment_jobs WHERE id = ?", (row["id"],)
                ).fetchone()[0]
            )
            conn.commit()
        return new_version

    def record_wms_outbound_selection_required(
        self,
        logistics_no: str,
        *,
        owner: str,
        expected_version: int,
        candidates: Iterable[Mapping[str, Any]],
        run_id: str | None = None,
    ) -> bool:
        """Record the structured manual-choice requirement before showing its dialog."""

        self.initialize()
        candidate_numbers = tuple(
            str(candidate.get("wo_number") or "").strip()
            for candidate in candidates
        )
        if (
            len(candidate_numbers) < 2
            or any(not value for value in candidate_numbers)
            or len(set(candidate_numbers)) != len(candidate_numbers)
        ):
            raise ValueError("销售出库单候选必须包含至少两个唯一 wo_number。")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT j.id, j.version, j.lease_owner, j.lease_stage,
                       e.selected_wms_wo_number
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.logistics_no = ?
                """,
                (logistics_no,),
            ).fetchone()
            if (
                row is None
                or str(row["lease_owner"] or "") != owner
                or str(row["lease_stage"] or "") != "erp"
                or int(row["version"]) != int(expected_version)
                or str(row["selected_wms_wo_number"] or "").strip()
            ):
                conn.rollback()
                return False
            self._insert_event_conn(
                conn,
                job_id=int(row["id"]),
                stage="erp",
                event_type="ERP_WMS_OUTBOUND_SELECTION_REQUIRED",
                message="同一系统单号存在多个销售出库单，等待用户明确选择。",
                details={"candidate_wo_numbers": sorted(candidate_numbers)},
                run_id=run_id,
            )
            conn.commit()
        return True

    def record_erp_confirmation(
        self,
        logistics_no: str,
        *,
        owner: str,
        payload_hash: str,
        confirmation_type: str,
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        column = "channel_payload_hash" if confirmation_type == "channel" else "logistics_payload_hash"
        time_column = "channel_confirmed_at" if confirmation_type == "channel" else "logistics_confirmed_at"
        now = utc_now()
        with self.connect() as conn:
            result = conn.execute(
                f"""
                UPDATE shipment_erp SET {column} = ?, {time_column} = ?, updated_at = ?
                WHERE job_id = ? AND EXISTS (
                    SELECT 1 FROM shipment_jobs j
                    WHERE j.id = shipment_erp.job_id AND j.lease_owner = ? AND j.lease_stage = 'erp'
                )
                """,
                (payload_hash, now, now, job["job_id"], owner),
            )
            if result.rowcount:
                self._insert_event_conn(
                    conn,
                    job_id=job["job_id"], stage="erp", event_type="USER_CONFIRMED_PAYLOAD",
                    message=confirmation_type, details={"payload_hash": payload_hash}, run_id=run_id,
                )
        return result.rowcount > 0

    def record_erp_prompt_confirmation(
        self,
        logistics_no: str,
        *,
        owner: str,
        prompt_hash: str,
        confirmation_source: str,
        confirmation_id: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        """Audit one approved dangerous-write prompt without storing its text."""

        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        with self.connect() as conn:
            leased = conn.execute(
                """
                SELECT 1 FROM shipment_jobs
                WHERE id = ? AND lease_owner = ? AND lease_stage = 'erp'
                """,
                (job["job_id"], owner),
            ).fetchone()
            if leased is None:
                return False
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="erp",
                event_type="DANGEROUS_WRITE_CONFIRMED",
                message=confirmation_source,
                details={
                    "prompt_hash": prompt_hash,
                    "confirmation_id": confirmation_id,
                },
                run_id=run_id,
            )
        return True

    def record_erp_write_audit(
        self,
        logistics_no: str,
        *,
        owner: str,
        event_type: str,
        details: Mapping[str, Any],
        run_id: str | None = None,
    ) -> bool:
        """Persist one sanitized write/reconciliation event under the ERP lease.

        The intent event is committed before the corresponding network write.
        It therefore remains available when the client loses the response or
        exits before it can advance the local checkpoint.
        """

        normalized_event = str(event_type or "").strip()
        if normalized_event not in {
            "ERP_WRITE_INTENT_RECORDED",
            "ERP_WRITE_ACKNOWLEDGED",
            "ERP_WRITE_REJECTED",
            "ERP_WRITE_RESULT_AMBIGUOUS",
            "ERP_WRITE_READBACK_CONFIRMED",
            "ERP_WRITE_READBACK_INCONCLUSIVE",
        }:
            raise ValueError(f"Unsupported ERP write audit event: {normalized_event}")
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            leased = conn.execute(
                """
                SELECT 1 FROM shipment_jobs
                WHERE id = ? AND lease_owner = ? AND lease_stage = 'erp'
                """,
                (job["job_id"], owner),
            ).fetchone()
            if leased is None:
                conn.rollback()
                return False
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="erp",
                event_type=normalized_event,
                message=str(details.get("operation") or ""),
                details=dict(details),
                run_id=run_id,
            )
            conn.commit()
        return True

    def get_pending_erp_review_intent(
        self,
        logistics_no: str,
    ) -> dict[str, Any] | None:
        """Return a review intent that must be reconciled instead of replayed.

        An acknowledged or readback-confirmed write still remains pending until
        the durable ERP checkpoint reaches ``AUDITED``.  A crash can otherwise
        occur between the acknowledgement/audit event and that checkpoint.  An
        explicit target rejection is the only event that proves this intent did
        not execute and therefore closes it before the checkpoint advances.
        """

        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return None
        if str(job.get("erp_checkpoint") or ERP_CHECKPOINT_NONE) in {
            ERP_CHECKPOINT_AUDITED,
            ERP_CHECKPOINT_LOGISTICS_SAVED,
            ERP_CHECKPOINT_OUTBOUNDED,
        }:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT event_type, details_json
                FROM shipment_events
                WHERE job_id = ? AND event_type IN (
                    'ERP_WRITE_INTENT_RECORDED',
                    'ERP_WRITE_REJECTED'
                )
                ORDER BY id
                """,
                (job["job_id"],),
            ).fetchall()

        pending: dict[str, Any] | None = None
        for row in rows:
            try:
                details = json.loads(str(row["details_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(details, dict):
                continue
            event_type = str(row["event_type"] or "")
            if (
                event_type == "ERP_WRITE_INTENT_RECORDED"
                and str(details.get("operation") or "") == "review_orders"
            ):
                pending = dict(details)
                continue
            if (
                event_type == "ERP_WRITE_REJECTED"
                and pending is not None
                and str(details.get("attempt_id") or "")
                == str(pending.get("attempt_id") or "")
            ):
                pending = None
        return pending

    def finish_erp_attempt(
        self,
        logistics_no: str,
        *,
        owner: str | None,
        state: str,
        last_error: str | None,
        policy_block_code: str | None = None,
        expected_version: int | None = None,
        run_id: str | None = None,
    ) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        now = utc_now()
        next_attempt = utc_after() if state == ERP_RETRYABLE else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conditions = ["id = ?"]
            params: list[Any] = [job["job_id"]]
            if owner:
                conditions.extend(["lease_owner = ?", "lease_stage = 'erp'"])
                params.append(owner)
            if expected_version is not None:
                conditions.append("version = ?")
                params.append(expected_version)
            if not conn.execute(f"SELECT 1 FROM shipment_jobs WHERE {' AND '.join(conditions)}", params).fetchone():
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE shipment_erp SET state = ?, next_attempt_at = ?,
                    attempt_count = attempt_count + 1, last_error = ?,
                    policy_block_code = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    state,
                    next_attempt,
                    last_error,
                    policy_block_code,
                    now,
                    job["job_id"],
                ),
            )
            conn.execute(
                """
                UPDATE shipment_jobs SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1 WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"], stage="erp", event_type="ERP_ATTEMPT_FINISHED",
                old_state=job["erp_state"], new_state=state, message=last_error, run_id=run_id,
            )
            conn.commit()
        return True

    def mark_erp_outbounded(
        self,
        logistics_no: str,
        *,
        email_preview_enabled: bool = False,
    ) -> bool:
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipment_erp SET state = ?, checkpoint = ?, outbounded_at = ?,
                    last_error = NULL, completion_source = ?, externally_completed_at = NULL,
                    updated_at = ? WHERE job_id = ?
                """,
                (
                    ERP_DONE, ERP_CHECKPOINT_OUTBOUNDED, now,
                    ERP_COMPLETION_AUTOMATION, now, job["job_id"],
                ),
            )
            conn.execute(
                "UPDATE shipment_jobs SET updated_at = ?, version = version + 1 WHERE id = ?",
                (now, job["job_id"]),
            )
        if email_preview_enabled:
            self.prepare_email_batches(platform_order_no=job["platform_order_no"])
        return True

    def complete_missing_pending_orders(
        self,
        visible_system_order_nos: set[str],
        *,
        discovered_before: str,
        run_id: str | None = None,
    ) -> list[ManualCompletionItem]:
        """Close unfinished jobs absent from a verified complete pending-review snapshot."""

        self.initialize()
        visible = {str(value).strip() for value in visible_system_order_nos if str(value).strip()}
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._reconcile_logistics_overdue_conn(
                conn,
                observed_at=_parse_utc_timestamp(now),
            )
            rows = conn.execute(
                """
                SELECT j.id AS job_id, j.system_order_no, j.platform_order_no, j.logistics_no,
                       e.state AS erp_state, e.last_error
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state = ? AND e.state <> ? AND j.first_seen_at <= ?
                ORDER BY j.id
                """,
                (IDENTITY_ACTIVE, ERP_DONE, discovered_before),
            ).fetchall()
            for row in rows:
                if row["system_order_no"] in visible:
                    continue
                conn.execute(
                    """
                    UPDATE shipment_erp
                    SET state = ?, checkpoint = ?, outbounded_at = ?, next_attempt_at = NULL,
                        last_error = NULL, completion_source = ?, externally_completed_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        ERP_DONE, ERP_CHECKPOINT_OUTBOUNDED, now,
                        ERP_COMPLETION_MANUAL_DETECTED, now, now, row["job_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, row["job_id"]),
                )
                self._insert_event_conn(
                    conn,
                    job_id=row["job_id"],
                    stage="erp",
                    event_type="MANUAL_COMPLETION_DETECTED",
                    old_state=row["erp_state"],
                    new_state=ERP_DONE,
                    message="完整待审核扫描中未发现该历史订单，已按人工完成结案。",
                    details={
                        "system_order_no": row["system_order_no"],
                        "platform_order_no": row["platform_order_no"],
                        "logistics_no": row["logistics_no"],
                        "previous_error": row["last_error"],
                        "source": "pending_review_snapshot",
                    },
                    run_id=run_id,
                )
            completed_rows = conn.execute(
                """
                SELECT j.system_order_no, j.platform_order_no, j.logistics_no
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state = ? AND e.state = ? AND e.completion_source = ?
                  AND e.externally_completed_at >= ?
                ORDER BY j.id
                """,
                (
                    IDENTITY_ACTIVE, ERP_DONE, ERP_COMPLETION_MANUAL_DETECTED,
                    discovered_before,
                ),
            ).fetchall()
            conn.commit()
        return [
            ManualCompletionItem(
                system_order_no=row["system_order_no"],
                platform_order_no=row["platform_order_no"],
                logistics_no=row["logistics_no"],
            )
            for row in completed_rows
            if row["system_order_no"] not in visible
        ]

    def list_logistics_skipped_records(self, *, limit: int = 0) -> list[QueueStatusRecord]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                self._aggregate_sql()
                + """
                  WHERE e.state <> ? AND (
                      j.identity_state IN (?, ?)
                      OR l.state = ? OR e.state = ?
                      OR (l.state = ? AND l.last_error LIKE ?)
                      OR (l.state = ? AND l.attempt_count >= 3)
                      OR (e.state = ? AND e.attempt_count >= 3)
                  )
                  ORDER BY j.updated_at, j.id
                """,
                (
                    ERP_DONE, IDENTITY_CONFLICT, IDENTITY_PAUSED_TAG_REMOVED,
                    LOGISTICS_BLOCKED, ERP_BLOCKED,
                    LOGISTICS_RETRYABLE, f"{TRACKING_MISMATCH_REASON_PREFIX}%",
                    LOGISTICS_RETRYABLE, ERP_RETRYABLE,
                ),
            ).fetchall()
        records = [self._status_record(self._flatten(row)) for row in rows]
        return records[:limit] if limit > 0 else records

    @staticmethod
    def _status_record(item: dict[str, Any]) -> QueueStatusRecord:
        return QueueStatusRecord(
            system_order_no=item["system_order_no"], platform_order_no=item["platform_order_no"],
            logistics_no=item["logistics_no"], last_error=item.get("last_error"),
            identity_state=item["identity_state"], logistics_state=item["logistics_state"],
            erp_state=item["erp_state"], erp_checkpoint=item["erp_checkpoint"],
            attempt_count=max(int(item.get("logistics_attempt_count") or 0), int(item.get("erp_attempt_count") or 0)),
            stage_state=ShipmentWorkflowStore._attention_stage_state(item),
        )

    @staticmethod
    def _attention_stage_state(item: dict[str, Any]) -> str:
        if item.get("identity_state") == IDENTITY_PAUSED_TAG_REMOVED:
            return "标签已移除/自动暂停"
        if item.get("identity_state") == IDENTITY_CONFLICT:
            return "身份/CONFLICT"
        if item.get("erp_state") == ERP_BLOCKED:
            return "ERP/BLOCKED"
        if item.get("logistics_state") == LOGISTICS_BLOCKED:
            return "物流/BLOCKED"
        if item.get("erp_state") == ERP_RETRYABLE and int(item.get("erp_attempt_count") or 0) >= 3:
            return "ERP/RETRYABLE"
        if item.get("logistics_state") == LOGISTICS_RETRYABLE and int(item.get("logistics_attempt_count") or 0) >= 3:
            return "物流/RETRYABLE"
        return "-"

    def prepare_email_batches(self, *, platform_order_no: str | None = None) -> list[EmailBatchPreview]:
        self.prepare_email_batches_with_count(platform_order_no=platform_order_no)
        return self.list_email_batches(platform_order_no=platform_order_no)

    def missing_receiver_email_targets(self, *, limit: int = 200) -> list[dict[str, str]]:
        """Return completed automated jobs whose local email preview lacks a recipient."""

        self.initialize()
        normalized_limit = max(1, min(int(limit), 1000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT j.system_order_no, j.platform_order_no
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                JOIN shipment_email_batch_items bi ON bi.job_id = j.id
                JOIN shipment_email_batches b ON b.id = bi.batch_id
                WHERE j.customer_email_required = 1
                  AND TRIM(COALESCE(j.receiver_email, '')) = ''
                  AND e.completion_source = ?
                  AND b.state = ?
                  AND b.last_error IN (?, ?)
                ORDER BY j.id
                LIMIT ?
                """,
                (
                    ERP_COMPLETION_AUTOMATION,
                    EMAIL_BLOCKED,
                    LEGACY_EMAIL_MISSING_RECEIVER_REASON,
                    EMAIL_MISSING_RECEIVER_REASON,
                    normalized_limit,
                ),
            ).fetchall()
        return [
            {
                "system_order_no": str(row["system_order_no"] or "").strip(),
                "platform_order_no": str(row["platform_order_no"] or "").strip(),
            }
            for row in rows
            if str(row["system_order_no"] or "").strip()
            and str(row["platform_order_no"] or "").strip()
        ]

    def backfill_receiver_email(
        self,
        *,
        system_order_no: str,
        platform_order_no: str,
        receiver_email: str,
        run_id: str | None = None,
    ) -> bool:
        """Store a detail-read email for one exact queued order and audit the source."""

        system = str(system_order_no or "").strip()
        platform = str(platform_order_no or "").strip()
        email = str(receiver_email or "").strip().lower()
        if not system or not platform:
            raise ValueError("补齐收件邮箱需要完整的系统单号和平台单号。")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            return False

        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, receiver_email
                FROM shipment_jobs
                WHERE system_order_no = ? AND platform_order_no = ?
                """,
                (system, platform),
            ).fetchone()
            if row is None or str(row["receiver_email"] or "").strip():
                conn.rollback()
                return False
            changed = conn.execute(
                """
                UPDATE shipment_jobs
                SET receiver_email = ?, updated_at = ?, version = version + 1
                WHERE id = ? AND TRIM(COALESCE(receiver_email, '')) = ''
                """,
                (email, now, int(row["id"])),
            ).rowcount
            if not changed:
                conn.rollback()
                return False
            self._insert_event_conn(
                conn,
                job_id=int(row["id"]),
                stage="email",
                event_type="RECEIVER_EMAIL_BACKFILLED",
                message="已通过订单详情读取补齐收件邮箱。",
                details={"source": "lingxing_order_detail"},
                run_id=run_id,
            )
            conn.commit()
        return True

    def prepare_email_batches_with_count(self, *, platform_order_no: str | None = None) -> int:
        """Prepare local-only email previews and return actual inserts/updates."""

        self.initialize()
        if platform_order_no:
            platforms = {str(platform_order_no).strip()}
        else:
            platforms: set[str] = set()
            with self.connect() as conn:
                platforms.update(
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT DISTINCT j.platform_order_no
                        FROM shipment_jobs j
                        JOIN shipment_erp e ON e.job_id = j.id
                        WHERE e.completion_source = ?
                          AND j.customer_email_required = 1
                        """,
                        (ERP_COMPLETION_AUTOMATION,),
                    ).fetchall()
                )
                platforms.update(
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT DISTINCT platform_order_no
                        FROM shipment_email_batches
                        WHERE state <> ?
                        """,
                        (EMAIL_SENT,),
                    ).fetchall()
                )
        platforms.discard("")
        return sum(1 for platform in sorted(platforms) if self._prepare_platform_batch(platform))

    def _block_unsafe_platform_batch_conn(
        self,
        conn: sqlite3.Connection,
        *,
        latest: sqlite3.Row | None,
        rows: list[sqlite3.Row],
        reason: str,
        now: str,
    ) -> bool:
        """Block an existing unsent preview that is no longer safe to process."""

        if latest is None or latest["state"] == EMAIL_SENT:
            return False
        context_rows = [row for row in rows if int(row["customer_email_required"] or 0) == 1]
        recipient = None
        if context_rows:
            recipient, _ = self._email_delivery_context(context_rows)
        if recipient is None:
            recipient = str(latest["recipient_email"] or "").strip().lower() or None
        content_hash = self._email_content_hash(
            rows,
            recipient_email=recipient,
            blocked_reason=reason,
        )
        if (
            latest["state"] == EMAIL_BLOCKED
            and latest["content_hash"] == content_hash
            and (str(latest["recipient_email"] or "").strip().lower() or None) == recipient
            and str(latest["last_error"] or "").strip() == reason
        ):
            return False
        message_id = self._email_message_id(
            latest["platform_order_no"],
            int(latest["sequence_no"]),
            content_hash,
        )
        conn.execute(
            """
            UPDATE shipment_email_batches
            SET state = ?, recipient_email = ?, message_id = ?, content_hash = ?,
                next_attempt_at = NULL, last_error = ?, sent_at = NULL, updated_at = ?
            WHERE id = ? AND state <> ?
            """,
            (
                EMAIL_BLOCKED,
                recipient,
                message_id,
                content_hash,
                reason,
                now,
                latest["id"],
                EMAIL_SENT,
            ),
        )
        if rows:
            self._replace_batch_items_conn(conn, int(latest["id"]), rows)
        self._insert_event_conn(
            conn,
            batch_id=int(latest["id"]),
            stage="email",
            event_type="EMAIL_BATCH_BLOCKED_UNSAFE",
            old_state=latest["state"],
            new_state=EMAIL_BLOCKED,
            message=reason,
            details={"known_non_conflict_package_count": len(rows)},
        )
        return True

    def _prepare_platform_batch(
        self,
        platform_order_no: str,
        *,
        retry_requested: bool = False,
        retry_reason: str | None = None,
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT * FROM shipment_email_batches WHERE platform_order_no = ? ORDER BY sequence_no DESC LIMIT 1",
                (platform_order_no,),
            ).fetchone()
            if retry_requested and latest is not None and latest["state"] == EMAIL_SENT:
                conn.rollback()
                return False
            all_jobs = conn.execute(
                """
                SELECT j.id, j.logistics_no, j.receiver_email, e.state AS erp_state,
                       e.checkpoint, e.completion_source,
                       l.carrier_normalized, l.carrier_raw, l.international_tracking_no,
                       j.customer_email_required, j.identity_state
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                JOIN shipment_logistics l ON l.job_id = j.id
                WHERE j.platform_order_no = ?
                ORDER BY j.id
                """,
                (platform_order_no,),
            ).fetchall()
            if not all_jobs:
                changed = self._block_unsafe_platform_batch_conn(
                    conn,
                    latest=latest,
                    rows=[],
                    reason=EMAIL_INCOMPLETE_PACKAGES_REASON,
                    now=now,
                )
                conn.commit() if changed else conn.rollback()
                return changed
            if any(row["identity_state"] == IDENTITY_CONFLICT for row in all_jobs):
                changed = self._block_unsafe_platform_batch_conn(
                    conn,
                    latest=latest,
                    rows=list(all_jobs),
                    reason=EMAIL_CONFLICT_PACKAGES_REASON,
                    now=now,
                )
                conn.commit() if changed else conn.rollback()
                return changed
            if any(
                row["erp_state"] != ERP_DONE
                or row["checkpoint"] != ERP_CHECKPOINT_OUTBOUNDED
                for row in all_jobs
            ):
                changed = self._block_unsafe_platform_batch_conn(
                    conn,
                    latest=latest,
                    rows=list(all_jobs),
                    reason=EMAIL_INCOMPLETE_PACKAGES_REASON,
                    now=now,
                )
                conn.commit() if changed else conn.rollback()
                return changed
            email_jobs = [
                row for row in all_jobs
                if row["completion_source"] == ERP_COMPLETION_AUTOMATION
                and int(row["customer_email_required"] or 0) == 1
            ]
            if not email_jobs:
                changed = self._block_unsafe_platform_batch_conn(
                    conn,
                    latest=latest,
                    rows=list(all_jobs),
                    reason=EMAIL_NO_AUTOMATION_PACKAGES_REASON,
                    now=now,
                )
                conn.commit() if changed else conn.rollback()
                return changed
            recipient, blocked_reason = self._email_delivery_context(email_jobs)
            state = EMAIL_BLOCKED if blocked_reason else EMAIL_PENDING
            content_hash = self._email_content_hash(
                email_jobs,
                recipient_email=recipient,
                blocked_reason=blocked_reason,
            )
            legacy_content_hash = self._email_legacy_content_hash(email_jobs)
            if latest:
                latest_recipient = str(latest["recipient_email"] or "").strip().lower() or None
                latest_is_blocked = latest["state"] == EMAIL_BLOCKED
                legacy_sent_matches = (
                    latest["state"] == EMAIL_SENT
                    and latest["content_hash"] == legacy_content_hash
                )
                legacy_context_matches = (
                    latest["content_hash"] == legacy_content_hash
                    and latest_recipient == recipient
                    and (
                        (blocked_reason is None and not latest_is_blocked)
                        or (
                            blocked_reason is not None
                            and latest_is_blocked
                            and str(latest["last_error"] or "").strip() == blocked_reason
                        )
                    )
                )
                equivalent_content = (
                    latest["content_hash"] == content_hash
                    or legacy_sent_matches
                    or legacy_context_matches
                )
                retry_can_wake_safe_batch = (
                    retry_requested
                    and state == EMAIL_PENDING
                    and latest["state"] in {EMAIL_BLOCKED, EMAIL_RETRYABLE}
                )
                if equivalent_content and not retry_can_wake_safe_batch:
                    conn.rollback()
                    return False
            if latest and latest["state"] != EMAIL_SENT:
                sequence_no = latest["sequence_no"]
                message_id = self._email_message_id(platform_order_no, sequence_no, content_hash)
                conn.execute(
                    """
                    UPDATE shipment_email_batches
                    SET state = ?, recipient_email = ?, message_id = ?, content_hash = ?,
                        next_attempt_at = NULL, last_error = ?, sent_at = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (state, recipient, message_id, content_hash, blocked_reason, now, latest["id"]),
                )
                batch_id = latest["id"]
            else:
                sequence_no = int(latest["sequence_no"] if latest else 0) + 1
                message_id = self._email_message_id(platform_order_no, sequence_no, content_hash)
                conn.execute(
                    """
                    INSERT INTO shipment_email_batches (
                        platform_order_no, sequence_no, state, recipient_email, message_id,
                        template_version, content_hash, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'v1', ?, ?, ?, ?)
                    """,
                    (platform_order_no, sequence_no, state, recipient, message_id, content_hash, blocked_reason, now, now),
                )
                batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._replace_batch_items_conn(conn, batch_id, email_jobs)
            self._insert_event_conn(
                conn,
                batch_id=batch_id, stage="email", event_type="EMAIL_BATCH_PREPARED",
                new_state=state,
                message=(retry_reason if retry_requested and state == EMAIL_PENDING else blocked_reason),
                details={
                    "content_hash": content_hash,
                    "sequence_no": sequence_no,
                    "blocked_reason": blocked_reason,
                    "retry_requested": retry_requested,
                },
            )
            conn.commit()
        return True

    @staticmethod
    def _email_delivery_context(rows: Iterable[sqlite3.Row]) -> tuple[str | None, str | None]:
        emails = {
            str(row["receiver_email"] or "").strip().lower()
            for row in rows
            if str(row["receiver_email"] or "").strip()
        }
        if not emails:
            return None, EMAIL_MISSING_RECEIVER_REASON
        if len(emails) > 1:
            return None, EMAIL_CONFLICTING_RECEIVERS_REASON
        return next(iter(emails)), None

    @staticmethod
    def _email_item_payload(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [
            {
                "logistics_no": row["logistics_no"],
                "carrier": row["carrier_normalized"] or row["carrier_raw"],
                "tracking": row["international_tracking_no"],
            }
            for row in rows
        ]

    @classmethod
    def _email_content_hash(
        cls,
        rows: Iterable[sqlite3.Row],
        *,
        recipient_email: str | None = None,
        blocked_reason: str | None = None,
    ) -> str:
        normalized_recipient = str(recipient_email or "").strip().lower() or None
        normalized_blocked_reason = str(blocked_reason or "").strip() or None
        payload = {
            "items": cls._email_item_payload(rows),
            "recipient_email": normalized_recipient,
            "blocked": normalized_blocked_reason is not None,
            "blocked_reason": normalized_blocked_reason,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()

    @classmethod
    def _email_legacy_content_hash(cls, rows: Iterable[sqlite3.Row]) -> str:
        """Return the pre-recipient hash so existing batches remain idempotent."""

        payload = cls._email_item_payload(rows)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _email_message_id(platform_order_no: str, sequence_no: int, content_hash: str) -> str:
        digest = hashlib.sha256(f"{platform_order_no}|{sequence_no}|{content_hash}|v1".encode()).hexdigest()[:32]
        return f"<{digest}@shipment-automation.local>"

    @staticmethod
    def _replace_batch_items_conn(conn: sqlite3.Connection, batch_id: int, rows: Iterable[sqlite3.Row]) -> None:
        conn.execute("DELETE FROM shipment_email_batch_items WHERE batch_id = ?", (batch_id,))
        for row in rows:
            conn.execute(
                """
                INSERT INTO shipment_email_batch_items (
                    batch_id, job_id, logistics_no, carrier, international_tracking_no
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    batch_id, row["id"], row["logistics_no"],
                    row["carrier_normalized"] or row["carrier_raw"], row["international_tracking_no"],
                ),
            )

    def list_email_batches(self, *, platform_order_no: str | None = None) -> list[EmailBatchPreview]:
        self.initialize()
        sql = "SELECT * FROM shipment_email_batches"
        params: list[Any] = []
        if platform_order_no:
            sql += " WHERE platform_order_no = ?"
            params.append(platform_order_no)
        sql += " ORDER BY platform_order_no, sequence_no"
        with self.connect() as conn:
            batches = conn.execute(sql, params).fetchall()
            previews: list[EmailBatchPreview] = []
            for batch in batches:
                items = conn.execute(
                    "SELECT logistics_no, international_tracking_no FROM shipment_email_batch_items WHERE batch_id = ? ORDER BY job_id",
                    (batch["id"],),
                ).fetchall()
                previews.append(
                    EmailBatchPreview(
                        id=batch["id"], platform_order_no=batch["platform_order_no"],
                        sequence_no=batch["sequence_no"], state=batch["state"],
                        recipient_email=batch["recipient_email"], message_id=batch["message_id"],
                        logistics_numbers=[row["logistics_no"] for row in items],
                        tracking_numbers=[row["international_tracking_no"] for row in items],
                        last_error=batch["last_error"],
                    )
                )
        return previews

    def list_attention(self, *, limit: int = 0) -> list[dict[str, Any]]:
        self.initialize()
        self.reconcile_logistics_overdue_history(include_historical=True)
        sql = self._aggregate_sql() + """
            WHERE j.identity_state NOT IN (?, ?) AND ((
               e.state <> ? AND (
                   j.identity_state IN (?, ?)
                   OR l.state = ? OR e.state = ?
                   OR (l.state = ? AND l.last_error LIKE ?)
                   OR (l.state = ? AND l.attempt_count >= 3)
                   OR (e.state = ? AND e.attempt_count >= 3)
               )
            ) OR EXISTS (
               SELECT 1
               FROM shipment_email_batches b
               JOIN shipment_email_batch_items bi ON bi.batch_id = b.id
               WHERE bi.job_id = j.id
                 AND (b.state = ? OR (b.state = ? AND b.attempt_count >= 3))
            ))
            ORDER BY j.updated_at, j.id
        """
        params: list[Any] = [
            IDENTITY_SUPERSEDED, IDENTITY_MANUALLY_CANCELLED,
            ERP_DONE, IDENTITY_CONFLICT, IDENTITY_PAUSED_TAG_REMOVED,
            LOGISTICS_BLOCKED, ERP_BLOCKED,
            LOGISTICS_RETRYABLE, f"{TRACKING_MISMATCH_REASON_PREFIX}%",
            LOGISTICS_RETRYABLE, ERP_RETRYABLE, EMAIL_BLOCKED, EMAIL_RETRYABLE,
        ]
        with self.connect() as conn:
            attention = {
                int(row["id"]): self._flatten(row)
                for row in conn.execute(sql, params).fetchall()
            }
            due_rows = conn.execute(
                self._aggregate_sql()
                + " WHERE j.identity_state = ? AND e.state <> ? ORDER BY j.updated_at, j.id",
                (IDENTITY_ACTIVE, ERP_DONE),
            ).fetchall()
            for row in due_rows:
                item = self._flatten(row)
                if item.get("shipping_attention_notice"):
                    attention.setdefault(int(row["id"]), item)
        rows = sorted(
            attention.values(),
            key=lambda item: (str(item.get("updated_at") or ""), int(item.get("job_id") or 0)),
        )
        return rows[:limit] if limit > 0 else rows

    def reconcile_customer_shipping_service_scan_issues(
        self,
        observations: Sequence[Mapping[str, Any]],
        *,
        snapshot_complete: bool,
        run_id: str | None = None,
    ) -> dict[str, int]:
        """Persist or resolve queue-visible list-scan errors without an ALS key."""

        summary = {
            "observed_count": 0,
            "created_count": 0,
            "refreshed_count": 0,
            "resolved_count": 0,
        }
        if not snapshot_complete:
            return summary
        self.initialize()
        normalized: dict[tuple[str, str], dict[str, str]] = {}
        for observation in observations:
            system_order_no = str(
                observation.get("system_order_no") or ""
            ).strip()
            platform_order_no = str(
                observation.get("platform_order_no") or ""
            ).strip()
            if not system_order_no or not platform_order_no:
                continue
            normalized[(system_order_no, platform_order_no)] = {
                "shipment_tag_name": str(
                    observation.get("shipment_tag_name") or ""
                ).strip(),
                "tag_text": str(observation.get("tag_text") or "").strip(),
                "source_status_text": str(
                    observation.get("source_status_text") or ""
                ).strip(),
                "error_message": str(
                    observation.get("error_message") or ""
                ).strip(),
            }
        summary["observed_count"] = len(normalized)
        if not normalized:
            return summary

        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for (system_order_no, platform_order_no), item in normalized.items():
                existing = conn.execute(
                    """
                    SELECT id, resolved_at
                    FROM shipment_scan_issues
                    WHERE system_order_no = ? AND platform_order_no = ?
                      AND issue_code = ?
                    """,
                    (
                        system_order_no,
                        platform_order_no,
                        CUSTOMER_SHIPPING_SERVICE_SCAN_ISSUE,
                    ),
                ).fetchone()
                error_message = item["error_message"]
                if error_message:
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO shipment_scan_issues (
                                system_order_no, platform_order_no, issue_code,
                                shipment_tag_name, tag_text, source_status_text,
                                error_message, first_seen_at, last_seen_at,
                                resolved_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                            """,
                            (
                                system_order_no,
                                platform_order_no,
                                CUSTOMER_SHIPPING_SERVICE_SCAN_ISSUE,
                                item["shipment_tag_name"],
                                item["tag_text"],
                                item["source_status_text"],
                                error_message,
                                now,
                                now,
                                now,
                            ),
                        )
                        summary["created_count"] += 1
                    else:
                        conn.execute(
                            """
                            UPDATE shipment_scan_issues
                            SET shipment_tag_name = ?, tag_text = ?,
                                source_status_text = ?, error_message = ?,
                                last_seen_at = ?, resolved_at = NULL,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                item["shipment_tag_name"],
                                item["tag_text"],
                                item["source_status_text"],
                                error_message,
                                now,
                                now,
                                existing["id"],
                            ),
                        )
                        summary["refreshed_count"] += 1
                    continue
                if existing is not None and not str(existing["resolved_at"] or "").strip():
                    conn.execute(
                        """
                        UPDATE shipment_scan_issues
                        SET resolved_at = ?, last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, now, existing["id"]),
                    )
                    summary["resolved_count"] += 1
            conn.commit()
        return summary

    def list_active_scan_issues(self, *, limit: int = 0) -> list[dict[str, Any]]:
        """Return unresolved or manually managed scan issues for the queue UI."""

        self.initialize()
        sql = (
            "SELECT * FROM shipment_scan_issues "
            "WHERE resolved_at IS NULL OR management_state <> ? "
            "ORDER BY updated_at DESC, id DESC"
        )
        params: list[Any] = [SCAN_ISSUE_ACTIVE]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            records = [dict(row) for row in conn.execute(sql, params).fetchall()]
        return [
            {
                "job_id": f"scan-issue-{item['id']}",
                "scan_issue_key": f"{SCAN_ISSUE_KEY_PREFIX}{item['id']}",
                "scan_issue_state": str(
                    item.get("management_state") or SCAN_ISSUE_ACTIVE
                ),
                "scan_issue_reason": str(item.get("management_reason") or ""),
                "scan_issue_state_changed_at": str(
                    item.get("management_updated_at") or ""
                ),
                "platform_order_no": item["platform_order_no"],
                "system_order_no": item["system_order_no"],
                "shipment_tag_name": item["shipment_tag_name"],
                "tag_text": item["tag_text"],
                "status_text": item["source_status_text"],
                "source_status_text": item["source_status_text"],
                "logistics_no": "",
                "customer_shipping_service": "",
                "identity_state": "SCAN_ERROR",
                "identity_status_text": "扫描错误",
                "logistics_state": "",
                "erp_state": "",
                "erp_checkpoint": "NONE",
                "last_error": item["error_message"],
                "logistics_last_error": "",
                "erp_last_error": "",
                "email_last_error": "",
                "scan_issue_code": item["issue_code"],
                "first_seen_at": item["first_seen_at"],
                "last_seen_at": item["last_seen_at"],
                "last_scanned_at": item["last_seen_at"],
                "updated_at": item["updated_at"],
            }
            for item in records
        ]

    @staticmethod
    def _scan_issue_id_from_key(value: object) -> int | None:
        text = str(value or "").strip()
        if not text.startswith(SCAN_ISSUE_KEY_PREFIX):
            return None
        try:
            issue_id = int(text[len(SCAN_ISSUE_KEY_PREFIX) :])
        except ValueError:
            return None
        return issue_id if issue_id > 0 else None

    def change_scan_issue_statuses(
        self,
        issue_keys: Iterable[str],
        action: str,
        *,
        reason: str,
        run_id: str | None = None,
    ) -> ShipmentStatusChangeSummary:
        """Apply audited manual management states to quarantined scan errors."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("修改扫描错误状态必须填写原因。")
        normalized = [
            value
            for value in dict.fromkeys(str(item or "").strip() for item in issue_keys)
            if value
        ]
        if not normalized:
            raise ValueError("请先勾选至少一条扫描错误记录。")
        target_by_action = {
            "manual_review": SCAN_ISSUE_MANUAL_REVIEW,
            "mark_manual_done": SCAN_ISSUE_MANUALLY_COMPLETED,
            "undo_manual_done": SCAN_ISSUE_ACTIVE,
            "manual_cancel": SCAN_ISSUE_MANUALLY_CANCELLED,
            "restore_manual_cancelled": SCAN_ISSUE_ACTIVE,
            "restore_scan_issue": SCAN_ISSUE_ACTIVE,
        }
        target_state = target_by_action.get(str(action or "").strip())
        if target_state is None:
            raise ValueError("该状态操作不适用于扫描错误记录。")

        changed: list[str] = []
        skipped: dict[str, str] = {}
        missing_count = 0
        now = utc_now()
        self.initialize()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for issue_key in normalized:
                issue_id = self._scan_issue_id_from_key(issue_key)
                if issue_id is None:
                    missing_count += 1
                    skipped[issue_key] = "扫描错误标识无效"
                    continue
                row = conn.execute(
                    "SELECT * FROM shipment_scan_issues WHERE id = ?",
                    (issue_id,),
                ).fetchone()
                if row is None:
                    missing_count += 1
                    skipped[issue_key] = "扫描错误记录不存在"
                    continue
                current_state = str(
                    row["management_state"] or SCAN_ISSUE_ACTIVE
                ).strip().upper()
                if current_state not in SCAN_ISSUE_MANAGED_STATES:
                    skipped[issue_key] = "扫描错误记录的当前状态无法识别"
                    continue
                if (
                    current_state == SCAN_ISSUE_MANUALLY_CANCELLED
                    and action
                    not in {"restore_manual_cancelled", "restore_scan_issue"}
                ):
                    skipped[issue_key] = "请先恢复人工取消状态"
                    continue
                if action == "undo_manual_done" and current_state != SCAN_ISSUE_MANUALLY_COMPLETED:
                    skipped[issue_key] = "当前记录不是人工已完成状态"
                    continue
                if (
                    action == "restore_manual_cancelled"
                    and current_state != SCAN_ISSUE_MANUALLY_CANCELLED
                ):
                    skipped[issue_key] = "当前记录不是人工取消状态"
                    continue
                if action == "restore_scan_issue" and current_state == SCAN_ISSUE_ACTIVE:
                    skipped[issue_key] = "当前记录已经是扫描错误状态"
                    continue
                if current_state == target_state:
                    skipped[issue_key] = "当前状态无需改变"
                    continue
                conn.execute(
                    """
                    UPDATE shipment_scan_issues
                    SET management_state = ?, management_reason = ?,
                        management_updated_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target_state, audit_reason, now, now, issue_id),
                )
                conn.execute(
                    """
                    INSERT INTO shipment_scan_issue_events (
                        issue_id, action, old_state, new_state, reason, run_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        action,
                        current_state,
                        target_state,
                        audit_reason,
                        run_id,
                        now,
                    ),
                )
                changed.append(issue_key)
            conn.commit()
        return ShipmentStatusChangeSummary(
            requested_count=len(normalized),
            changed_count=len(changed),
            unchanged_count=len(skipped) - missing_count,
            missing_count=missing_count,
            changed_logistics_nos=tuple(changed),
            skipped_reasons=skipped,
        )

    def list_scan_issue_events(self, issue_key: str) -> list[dict[str, Any]]:
        """Return the complete management audit history for one scan issue."""

        issue_id = self._scan_issue_id_from_key(issue_key)
        if issue_id is None:
            return []
        self.initialize()
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM shipment_scan_issue_events
                    WHERE issue_id = ? ORDER BY id
                    """,
                    (issue_id,),
                ).fetchall()
            ]

    def list_all_jobs(
        self,
        *,
        limit: int = 0,
        reconcile_overdue: bool = True,
    ) -> list[dict[str, Any]]:
        self.initialize()
        if reconcile_overdue:
            self.reconcile_logistics_overdue_history(include_historical=True)
        # Keep rows in their original queue position.  State changes (including
        # cancelling the current run) must not make a row jump to the bottom.
        sql = self._aggregate_sql() + " WHERE j.identity_state <> ? ORDER BY j.id"
        params: list[Any] = [IDENTITY_SUPERSEDED]
        with self.connect() as conn:
            jobs = [self._flatten(row) for row in conn.execute(sql, params).fetchall()]
        rows = [*self.list_active_scan_issues(), *jobs]
        return rows[:limit] if limit > 0 else rows

    def count_all_jobs(self) -> tuple[int, str]:
        """Return queue cardinality and latest change time without loading rows."""

        self.initialize()
        with self.connect() as conn:
            job_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(updated_at), '')
                FROM shipment_jobs WHERE identity_state <> ?
                """,
                (IDENTITY_SUPERSEDED,),
            ).fetchone()
            issue_row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(updated_at), '')
                FROM shipment_scan_issues
                WHERE resolved_at IS NULL OR management_state <> ?
                """,
                (SCAN_ISSUE_ACTIVE,),
            ).fetchone()
        latest = max(str(job_row[1] or ""), str(issue_row[1] or ""))
        return int(job_row[0]) + int(issue_row[0]), latest

    def list_missing_product_type_jobs(
        self,
        *,
        catalog_version: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Return a bounded set of exact order identities needing ASIN lookup.

        The checkpoint is tied to the identity-catalog version.  A detail that
        was successfully read but still contains an unknown ASIN is therefore
        reconsidered automatically after the catalog changes, without making
        every normal scan query the same historical order forever.
        """

        self.initialize()
        normalized_version = str(catalog_version or "").strip()
        if not normalized_version:
            raise ValueError("catalog_version is required")
        bounded_limit = max(1, min(int(limit or 25), 500))
        now = utc_now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT system_order_no, platform_order_no,
                       MIN(id) AS first_job_id,
                       COUNT(*) AS pending_job_count,
                       MAX(COALESCE(product_identity_retry_count, 0))
                           AS product_identity_retry_count,
                       MAX(COALESCE(product_identity_next_retry_at, ''))
                           AS product_identity_next_retry_at
                FROM shipment_jobs
                WHERE identity_state <> ?
                  AND TRIM(COALESCE(system_order_no, '')) <> ''
                  AND TRIM(COALESCE(platform_order_no, '')) <> ''
                  AND TRIM(COALESCE(product_type, '')) = ''
                  AND COALESCE(product_identity_catalog_version, '') <> ?
                  AND (
                      TRIM(COALESCE(product_identity_next_retry_at, '')) = ''
                      OR product_identity_next_retry_at <= ?
                  )
                GROUP BY system_order_no, platform_order_no
                ORDER BY
                    CASE
                        WHEN MAX(COALESCE(product_identity_retry_count, 0)) = 0
                        THEN 0 ELSE 1
                    END,
                    MAX(COALESCE(product_identity_next_retry_at, '')),
                    first_job_id
                LIMIT ?
                """,
                (
                    IDENTITY_SUPERSEDED,
                    normalized_version,
                    now,
                    bounded_limit,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def release_deferred_product_identity_retries(
        self,
        *,
        run_id: str | None = None,
    ) -> int:
        """Make deferred blank identities due once for an explicit manual audit.

        The next failed lookup receives a fresh backoff in the same run, so a
        transient order cannot be selected repeatedly by the batch drain.
        """

        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, product_identity_next_retry_at
                FROM shipment_jobs
                WHERE identity_state <> ?
                  AND TRIM(COALESCE(product_type, '')) = ''
                  AND TRIM(COALESCE(product_identity_next_retry_at, '')) <> ''
                  AND product_identity_next_retry_at > ?
                ORDER BY id
                """,
                (IDENTITY_SUPERSEDED, now),
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                previous_retry_at = str(
                    row["product_identity_next_retry_at"] or ""
                ).strip()
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET product_identity_next_retry_at = NULL,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (job_id,),
                )
                self._insert_event_conn(
                    conn,
                    job_id=job_id,
                    stage="identity",
                    event_type="PRODUCT_IDENTITY_RETRY_RELEASED",
                    message="人工历史核验已提前释放商品身份重试等待。",
                    details={"previous_next_retry_at": previous_retry_at},
                    run_id=run_id,
                )
            conn.commit()
        return len(rows)

    def list_completed_sku_product_identity_jobs(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return completed blank identities supported by exact SKU rules.

        This local pre-pass deliberately ignores ASIN retry backoff: it never
        calls an external service and only returns completed historical jobs.
        Unsupported or ambiguous SKU values remain untouched for the normal
        ASIN evidence path.
        """

        self.initialize()
        bounded_limit = max(1, min(int(limit or 500), 2000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.system_order_no, j.platform_order_no,
                       GROUP_CONCAT(DISTINCT TRIM(COALESCE(j.sku_text, '')))
                           AS sku_text,
                       MIN(j.id) AS first_job_id
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state <> ?
                  AND e.state = ?
                  AND TRIM(COALESCE(j.system_order_no, '')) <> ''
                  AND TRIM(COALESCE(j.platform_order_no, '')) <> ''
                  AND TRIM(COALESCE(j.product_type, '')) = ''
                  AND TRIM(COALESCE(j.sku_text, '')) <> ''
                GROUP BY j.system_order_no, j.platform_order_no
                ORDER BY first_job_id
                LIMIT ?
                """,
                (IDENTITY_SUPERSEDED, ERP_DONE, bounded_limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            product_types = identify_product_types_from_skus(
                str(row["sku_text"] or "")
            )
            if product_types:
                output.append({**dict(row), "product_types": product_types})
        return output

    def product_identity_backfill_counts(
        self,
        *,
        catalog_version: str,
    ) -> dict[str, int]:
        """Count current due and deferred identities without changing state."""

        self.initialize()
        normalized_version = str(catalog_version or "").strip()
        if not normalized_version:
            raise ValueError("catalog_version is required")
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_target_count,
                    SUM(
                        CASE WHEN TRIM(COALESCE(next_retry_at, '')) = ''
                                  OR next_retry_at <= ?
                             THEN 1 ELSE 0 END
                    ) AS due_target_count,
                    SUM(
                        CASE WHEN TRIM(COALESCE(next_retry_at, '')) <> ''
                                  AND next_retry_at > ?
                             THEN 1 ELSE 0 END
                    ) AS deferred_target_count
                FROM (
                    SELECT system_order_no, platform_order_no,
                           MAX(COALESCE(product_identity_next_retry_at, ''))
                               AS next_retry_at
                    FROM shipment_jobs
                    WHERE identity_state <> ?
                      AND TRIM(COALESCE(system_order_no, '')) <> ''
                      AND TRIM(COALESCE(platform_order_no, '')) <> ''
                      AND TRIM(COALESCE(product_type, '')) = ''
                      AND COALESCE(product_identity_catalog_version, '') <> ?
                    GROUP BY system_order_no, platform_order_no
                )
                """,
                (
                    now,
                    now,
                    IDENTITY_SUPERSEDED,
                    normalized_version,
                ),
            ).fetchone()
        return {
            "total_target_count": int(row["total_target_count"] or 0),
            "due_target_count": int(row["due_target_count"] or 0),
            "deferred_target_count": int(row["deferred_target_count"] or 0),
        }

    def customer_shipping_service_backfill_targets(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return orders whose stored service is a non-canonical route value.

        Empty values are not included: they are missing evidence, not proven
        historical pollution.  Repairs must be sourced from an explicit
        Lingxing customer-shipping field and never inferred from the bad value.
        """

        self.initialize()
        bounded_limit = max(1, min(int(limit or 500), 2000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id AS job_id, system_order_no, platform_order_no,
                       logistics_no,
                       customer_shipping_service AS expected_old_value
                FROM shipment_jobs
                WHERE TRIM(COALESCE(customer_shipping_service, '')) <> ''
                  AND TRIM(COALESCE(system_order_no, '')) <> ''
                  AND TRIM(COALESCE(platform_order_no, '')) <> ''
                ORDER BY id
                """
            ).fetchall()
        output = [
            dict(row)
            for row in rows
            if normalize_customer_shipping_service(row["expected_old_value"])
            not in {
                CUSTOMER_SHIPPING_STANDARD,
                CUSTOMER_SHIPPING_EXPEDITED,
            }
        ]
        return output[:bounded_limit]

    def customer_shipping_service_backfill_counts(self) -> dict[str, int]:
        """Count contaminated jobs and distinct order-detail targets."""

        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT system_order_no, platform_order_no,
                       customer_shipping_service
                FROM shipment_jobs
                WHERE TRIM(COALESCE(customer_shipping_service, '')) <> ''
                """
            ).fetchall()
        contaminated = [
            row
            for row in rows
            if normalize_customer_shipping_service(
                row["customer_shipping_service"]
            )
            not in {
                CUSTOMER_SHIPPING_STANDARD,
                CUSTOMER_SHIPPING_EXPEDITED,
            }
        ]
        return {
            "contaminated_job_count": len(contaminated),
            "target_count": len(contaminated),
            "detail_target_count": len(
                {
                    (
                        str(row["system_order_no"] or ""),
                        str(row["platform_order_no"] or ""),
                    )
                    for row in contaminated
                }
            ),
        }

    def apply_customer_shipping_service_backfill(
        self,
        observations: Iterable[Mapping[str, Any]],
        *,
        run_id: str | None = None,
    ) -> dict[str, int]:
        """Repair polluted route values from explicit Lingxing detail evidence.

        Only canonical ``standard`` or ``expedited`` values are accepted.  A
        missing/unknown detail leaves the original row untouched so an audit
        can distinguish unresolved evidence from a completed correction.
        """

        self.initialize()
        result = {
            "target_count": 0,
            "resolved_target_count": 0,
            "unresolved_target_count": 0,
            "updated_job_count": 0,
            "already_resolved_target_count": 0,
            "cas_mismatch_target_count": 0,
        }
        seen: set[tuple[str, str, str]] = set()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for observation in observations:
                system_order_no = str(
                    observation.get("system_order_no") or ""
                ).strip()
                platform_order_no = str(
                    observation.get("platform_order_no") or ""
                ).strip()
                logistics_no = str(observation.get("logistics_no") or "").strip()
                expected_old_value = str(
                    observation.get("expected_old_value") or ""
                ).strip()
                key = (system_order_no, platform_order_no, logistics_no)
                if not all(key) or key in seen:
                    continue
                seen.add(key)
                result["target_count"] += 1
                error = str(observation.get("error") or "").strip()
                service = normalize_customer_shipping_service(
                    observation.get("customer_shipping_service")
                )
                if error or service not in {
                    CUSTOMER_SHIPPING_STANDARD,
                    CUSTOMER_SHIPPING_EXPEDITED,
                }:
                    result["unresolved_target_count"] += 1
                    continue

                row = conn.execute(
                    """
                    SELECT id, customer_shipping_service
                    FROM shipment_jobs
                    WHERE system_order_no = ? AND platform_order_no = ?
                      AND logistics_no = ?
                    """,
                    (
                        system_order_no,
                        platform_order_no,
                        logistics_no,
                    ),
                ).fetchone()
                if row is None:
                    result["cas_mismatch_target_count"] += 1
                    continue
                current_value = str(row["customer_shipping_service"] or "").strip()
                if current_value != expected_old_value:
                    if normalize_customer_shipping_service(current_value) in {
                        CUSTOMER_SHIPPING_STANDARD,
                        CUSTOMER_SHIPPING_EXPEDITED,
                    }:
                        result["already_resolved_target_count"] += 1
                    else:
                        result["cas_mismatch_target_count"] += 1
                    continue
                if normalize_customer_shipping_service(current_value) in {
                    CUSTOMER_SHIPPING_STANDARD,
                    CUSTOMER_SHIPPING_EXPEDITED,
                }:
                    result["already_resolved_target_count"] += 1
                    continue
                result["resolved_target_count"] += 1
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET customer_shipping_service = ?, version = version + 1
                    WHERE id = ? AND customer_shipping_service = ?
                    """,
                    (service, int(row["id"]), expected_old_value),
                )
                if not conn.execute("SELECT changes()").fetchone()[0]:
                    result["resolved_target_count"] -= 1
                    result["cas_mismatch_target_count"] += 1
                    continue
                self._insert_event_conn(
                    conn,
                    job_id=int(row["id"]),
                    stage="migration",
                    event_type="CUSTOMER_SHIPPING_SERVICE_REPAIRED",
                    old_state=expected_old_value,
                    new_state=service,
                    message="已按领星订单详情中的明确客选物流字段修复历史污染值。",
                    details={
                        "source": "lingxing_order_detail",
                        "authoritative_field": str(
                            observation.get("authoritative_field") or ""
                        ).strip(),
                        "system_order_no": system_order_no,
                        "platform_order_no": platform_order_no,
                        "logistics_no": logistics_no,
                        "repaired_at": now,
                    },
                    run_id=run_id,
                )
                result["updated_job_count"] += 1
            conn.commit()
        return result

    @staticmethod
    def _product_identity_observation_value(
        observation: object,
        name: str,
        default: object = "",
    ) -> object:
        if isinstance(observation, Mapping):
            return observation.get(name, default)
        return getattr(observation, name, default)

    @staticmethod
    def _normalized_product_type_values(values: object) -> tuple[str, ...]:
        if isinstance(values, str):
            source: Iterable[object] = re.split(r"\s*[|｜]\s*", values)
        elif isinstance(values, Iterable):
            source = values
        else:
            source = ()
        return tuple(
            value
            for value in dict.fromkeys(str(item or "").strip() for item in source)
            if value
        )

    def apply_product_identity_backfill(
        self,
        observations: Iterable[object],
        *,
        catalog_version: str,
        run_id: str | None = None,
    ) -> dict[str, int]:
        """Fill only missing product types and checkpoint successful detail reads.

        This method deliberately does not touch identity, logistics or ERP
        workflow state.  Failed detail calls are not checkpointed, so they can
        be retried on a later scan; a successful response with an unknown ASIN
        is checkpointed until the identity catalog changes.
        """

        self.initialize()
        normalized_version = str(catalog_version or "").strip()
        if not normalized_version:
            raise ValueError("catalog_version is required")
        result = {
            "target_count": 0,
            "checked_job_count": 0,
            "resolved_job_count": 0,
            "unresolved_job_count": 0,
            "failed_target_count": 0,
            "retry_scheduled_job_count": 0,
        }
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for observation in observations:
                result["target_count"] += 1
                system_order_no = str(
                    self._product_identity_observation_value(
                        observation,
                        "system_order_no",
                    )
                    or ""
                ).strip()
                platform_order_no = str(
                    self._product_identity_observation_value(
                        observation,
                        "platform_order_no",
                    )
                    or ""
                ).strip()
                error = str(
                    self._product_identity_observation_value(
                        observation,
                        "error",
                    )
                    or ""
                ).strip()
                if not system_order_no or not platform_order_no:
                    result["failed_target_count"] += 1
                    continue
                if error:
                    result["failed_target_count"] += 1
                    retry_rows = conn.execute(
                        """
                        SELECT id, COALESCE(product_identity_retry_count, 0)
                            AS retry_count
                        FROM shipment_jobs
                        WHERE system_order_no = ? AND platform_order_no = ?
                          AND identity_state <> ?
                          AND TRIM(COALESCE(product_type, '')) = ''
                        ORDER BY id
                        """,
                        (
                            system_order_no,
                            platform_order_no,
                            IDENTITY_SUPERSEDED,
                        ),
                    ).fetchall()
                    for row in retry_rows:
                        retry_count = min(int(row["retry_count"] or 0) + 1, 20)
                        delay_minutes = min(
                            PRODUCT_IDENTITY_RETRY_BASE_MINUTES
                            * (2 ** (retry_count - 1)),
                            PRODUCT_IDENTITY_RETRY_MAX_HOURS * 60,
                        )
                        next_retry_at = utc_after(delay_minutes / 60)
                        conn.execute(
                            """
                            UPDATE shipment_jobs
                            SET product_identity_retry_count = ?,
                                product_identity_next_retry_at = ?,
                                product_identity_last_error = ?,
                                version = version + 1
                            WHERE id = ?
                            """,
                            (retry_count, next_retry_at, error[:500], row["id"]),
                        )
                        result["retry_scheduled_job_count"] += 1
                        self._insert_event_conn(
                            conn,
                            job_id=int(row["id"]),
                            stage="identity",
                            event_type="PRODUCT_IDENTITY_RETRY_SCHEDULED",
                            message="商品身份详情读取未完成，已延后重试且不会阻塞后续订单。",
                            details={
                                "catalog_version": normalized_version,
                                "error": error[:500],
                                "retry_count": retry_count,
                                "next_retry_at": next_retry_at,
                                "evidence_scope": str(
                                    self._product_identity_observation_value(
                                        observation,
                                        "evidence_scope",
                                    )
                                    or ""
                                ).strip(),
                                "evidence_system_order_nos": list(
                                    self._normalized_product_type_values(
                                        self._product_identity_observation_value(
                                            observation,
                                            "evidence_system_order_nos",
                                            (),
                                        )
                                    )
                                ),
                            },
                            run_id=run_id,
                        )
                    continue
                product_types = self._normalized_product_type_values(
                    self._product_identity_observation_value(
                        observation,
                        "product_types",
                        (),
                    )
                )
                product_type = preferred_product_type(product_types)
                raw_observed_asins = self._product_identity_observation_value(
                    observation,
                    "observed_asins",
                    (),
                )
                if isinstance(raw_observed_asins, str):
                    raw_observed_asins = (raw_observed_asins,)
                observed_asins = tuple(
                    value
                    for value in dict.fromkeys(
                        str(item or "").strip()
                        for item in (raw_observed_asins or ())
                    )
                    if value
                )
                raw_observed_skus = self._product_identity_observation_value(
                    observation,
                    "observed_skus",
                    (),
                )
                if isinstance(raw_observed_skus, str):
                    raw_observed_skus = re.split(
                        r"\s*[|｜、,]\s*",
                        raw_observed_skus,
                    )
                observed_skus = tuple(
                    value
                    for value in dict.fromkeys(
                        str(item or "").strip()
                        for item in (raw_observed_skus or ())
                    )
                    if value
                )
                match_platform_siblings = bool(
                    self._product_identity_observation_value(
                        observation,
                        "match_platform_siblings",
                        False,
                    )
                )
                completed_only = bool(
                    self._product_identity_observation_value(
                        observation,
                        "completed_only",
                        False,
                    )
                )
                if match_platform_siblings:
                    rows = conn.execute(
                        """
                        SELECT j.id
                        FROM shipment_jobs j
                        JOIN shipment_erp e ON e.job_id = j.id
                        WHERE j.platform_order_no = ?
                          AND j.identity_state <> ?
                          AND TRIM(COALESCE(j.product_type, '')) = ''
                          AND (? = 0 OR e.state = ?)
                        ORDER BY j.id
                        """,
                        (
                            platform_order_no,
                            IDENTITY_SUPERSEDED,
                            int(completed_only),
                            ERP_DONE,
                        ),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT j.id
                        FROM shipment_jobs j
                        JOIN shipment_erp e ON e.job_id = j.id
                        WHERE j.system_order_no = ? AND j.platform_order_no = ?
                          AND j.identity_state <> ?
                          AND TRIM(COALESCE(j.product_type, '')) = ''
                          AND (? = 0 OR e.state = ?)
                        ORDER BY j.id
                        """,
                        (
                            system_order_no,
                            platform_order_no,
                            IDENTITY_SUPERSEDED,
                            int(completed_only),
                            ERP_DONE,
                        ),
                    ).fetchall()
                for row in rows:
                    job_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE shipment_jobs
                        SET product_type = COALESCE(NULLIF(?, ''), product_type),
                            product_identity_catalog_version = ?,
                            product_identity_checked_at = ?,
                            product_identity_retry_count = 0,
                            product_identity_next_retry_at = NULL,
                            product_identity_last_error = NULL,
                            version = version + 1
                        WHERE id = ?
                        """,
                        (product_type, normalized_version, now, job_id),
                    )
                    result["checked_job_count"] += 1
                    if product_type:
                        result["resolved_job_count"] += 1
                    else:
                        result["unresolved_job_count"] += 1
                    self._insert_event_conn(
                        conn,
                        job_id=job_id,
                        stage="identity",
                        event_type=(
                            "PRODUCT_IDENTITY_BACKFILLED"
                            if product_type
                            else "PRODUCT_IDENTITY_CHECKED"
                        ),
                        message=(
                            "已根据完整订单商品证据补齐商品类型。"
                            if product_type
                            else "已核验订单详情，当前商品目录仍无法识别商品身份。"
                        ),
                        details={
                            "catalog_version": normalized_version,
                            "observed_asins": list(observed_asins),
                            "observed_skus": list(observed_skus),
                            "product_types": list(product_types),
                            "evidence_scope": str(
                                self._product_identity_observation_value(
                                    observation,
                                    "evidence_scope",
                                )
                                or ""
                            ).strip(),
                            "evidence_system_order_nos": list(
                                self._normalized_product_type_values(
                                    self._product_identity_observation_value(
                                        observation,
                                        "evidence_system_order_nos",
                                        (),
                                    )
                                )
                            ),
                        },
                        run_id=run_id,
                    )
            conn.commit()
        return result

    def history(self, logistics_no: str) -> list[QueueEvent]:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return []
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM shipment_events WHERE job_id = ? ORDER BY id", (job["job_id"],)).fetchall()
        return [
            QueueEvent(
                id=row["id"], job_id=row["job_id"], batch_id=row["batch_id"], stage=row["stage"],
                event_type=row["event_type"], old_state=row["old_state"], new_state=row["new_state"],
                message=row["message"], details=json.loads(row["details_json"] or "{}"),
                run_id=row["run_id"], created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _normalized_logistics_nos(logistics_nos: Iterable[str]) -> list[str]:
        return [
            value
            for value in dict.fromkeys(
                str(value or "").strip() for value in logistics_nos
            )
            if value
        ]

    def reopen_shipments_from_stage(
        self,
        logistics_nos: Iterable[str],
        stage: str,
        *,
        reason: str,
    ) -> ShipmentStatusChangeSummary:
        """Reopen queue jobs from one operator-selected business stage.

        The selected checkpoint asserts that earlier stages have already been
        verified by the operator.  Existing logistics values are retained for
        comparison, while completion evidence is copied into the audit event
        before the active ERP state is reset.
        """

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("重新打开自动标发阶段必须填写原因。")
        target_stage = str(stage or "").strip().lower()
        checkpoint_by_stage = {
            "set_channel": ERP_CHECKPOINT_NONE,
            "audit": ERP_CHECKPOINT_CHANNEL_SET,
            "tracking": ERP_CHECKPOINT_AUDITED,
            "outbound": ERP_CHECKPOINT_LOGISTICS_SAVED,
        }
        if target_stage != "logistics" and target_stage not in checkpoint_by_stage:
            raise ValueError("未知的自动标发重开阶段。")
        normalized = self._normalized_logistics_nos(logistics_nos)
        if not normalized:
            raise ValueError("请先勾选至少一条自动标发任务。")

        self.initialize()
        now = utc_now()
        changed: list[str] = []
        skipped: dict[str, str] = {}
        missing_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for logistics_no in normalized:
                row = conn.execute(
                    self._aggregate_sql() + " WHERE j.logistics_no = ?",
                    (logistics_no,),
                ).fetchone()
                if row is None:
                    missing_count += 1
                    skipped[logistics_no] = "队列中不存在该物流单号"
                    continue
                current = dict(row)
                if str(current.get("identity_state") or "") != IDENTITY_ACTIVE:
                    skipped[logistics_no] = "任务不是活动状态，请先恢复已取消任务"
                    continue
                if _has_live_lease(current, now=now):
                    skipped[logistics_no] = "任务正在执行，不能修改检查点"
                    continue
                if (
                    target_stage != "logistics"
                    and current.get("policy_block_code")
                    == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL
                ):
                    skipped[logistics_no] = (
                        "该订单必须先人工选择允许的承运商并填写格式匹配的正确单号"
                    )
                    continue
                if target_stage != "logistics" and str(
                    current.get("logistics_state") or ""
                ) != LOGISTICS_READY:
                    skipped[logistics_no] = "物流资料尚未校验为可用"
                    continue

                previous = {
                    "identity_state": current.get("identity_state"),
                    "logistics_state": current.get("logistics_state"),
                    "logistics_next_attempt_at": current.get("logistics_next_attempt_at"),
                    "logistics_last_error": current.get("logistics_last_error"),
                    "alibaba_status": current.get("alibaba_status"),
                    "carrier_raw": current.get("carrier_raw"),
                    "carrier_normalized": current.get("carrier_normalized"),
                    "international_tracking_no": current.get("international_tracking_no"),
                    "currency": current.get("currency"),
                    "fee_amount": current.get("fee_amount"),
                    "chargeable_weight_kg": current.get("chargeable_weight_kg"),
                    "erp_state": current.get("erp_state"),
                    "erp_checkpoint": current.get("erp_checkpoint"),
                    "erp_next_attempt_at": current.get("erp_next_attempt_at"),
                    "erp_last_error": current.get("erp_last_error"),
                    "channel_path": current.get("channel_path"),
                    "freight_amount": current.get("freight_amount"),
                    "chargeable_weight_g": current.get("chargeable_weight_g"),
                    "channel_payload_hash": current.get("channel_payload_hash"),
                    "logistics_payload_hash": current.get("logistics_payload_hash"),
                    "channel_set_at": current.get("channel_set_at"),
                    "audited_at": current.get("audited_at"),
                    "logistics_saved_at": current.get("logistics_saved_at"),
                    "outbounded_at": current.get("outbounded_at"),
                    "completion_source": current.get("completion_source"),
                    "externally_completed_at": current.get("externally_completed_at"),
                }

                if target_stage == "logistics":
                    already_target = (
                        current.get("logistics_state") == LOGISTICS_RETRYABLE
                        and current.get("erp_state") == ERP_WAITING
                        and current.get("erp_checkpoint") == ERP_CHECKPOINT_NONE
                        and not current.get("logistics_last_error")
                        and not current.get("erp_last_error")
                        and not current.get("completion_source")
                    )
                    if already_target:
                        skipped[logistics_no] = "已经处于查询物流待处理阶段"
                        continue
                    conn.execute(
                        """
                        UPDATE shipment_logistics
                        SET state = ?, next_attempt_at = ?, last_error = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (LOGISTICS_RETRYABLE, now, now, int(current["id"])),
                    )
                    conn.execute(
                        """
                        UPDATE shipment_erp
                        SET state = ?, checkpoint = ?, channel_path = NULL,
                            freight_amount = NULL, chargeable_weight_g = NULL,
                            channel_payload_hash = NULL, logistics_payload_hash = NULL,
                            channel_confirmed_at = NULL, logistics_confirmed_at = NULL,
                            channel_set_at = NULL, audited_at = NULL,
                            logistics_saved_at = NULL, outbounded_at = NULL,
                            next_attempt_at = NULL, last_error = NULL,
                            completion_source = NULL, externally_completed_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (ERP_WAITING, ERP_CHECKPOINT_NONE, now, int(current["id"])),
                    )
                    new_state = f"{LOGISTICS_RETRYABLE}/{ERP_WAITING}/{ERP_CHECKPOINT_NONE}"
                    event_stage = "logistics"
                else:
                    target_checkpoint = checkpoint_by_stage[target_stage]
                    already_target = (
                        current.get("erp_state") == ERP_RETRYABLE
                        and current.get("erp_checkpoint") == target_checkpoint
                        and not current.get("erp_last_error")
                        and not current.get("completion_source")
                    )
                    if already_target:
                        skipped[logistics_no] = "已经处于所选 ERP 待处理阶段"
                        continue
                    reset_fragments = [
                        "state = ?",
                        "checkpoint = ?",
                        "next_attempt_at = ?",
                        "last_error = NULL",
                        "outbounded_at = NULL",
                        "completion_source = NULL",
                        "externally_completed_at = NULL",
                        "updated_at = ?",
                    ]
                    if target_stage == "set_channel":
                        reset_fragments.extend(
                            [
                                "channel_path = NULL",
                                "channel_payload_hash = NULL",
                                "channel_confirmed_at = NULL",
                                "channel_set_at = NULL",
                                "audited_at = NULL",
                                "logistics_payload_hash = NULL",
                                "logistics_confirmed_at = NULL",
                                "freight_amount = NULL",
                                "chargeable_weight_g = NULL",
                                "logistics_saved_at = NULL",
                            ]
                        )
                    elif target_stage == "audit":
                        reset_fragments.extend(
                            [
                                "audited_at = NULL",
                                "logistics_payload_hash = NULL",
                                "logistics_confirmed_at = NULL",
                                "freight_amount = NULL",
                                "chargeable_weight_g = NULL",
                                "logistics_saved_at = NULL",
                            ]
                        )
                    elif target_stage == "tracking":
                        reset_fragments.extend(
                            [
                                "logistics_payload_hash = NULL",
                                "logistics_confirmed_at = NULL",
                                "freight_amount = NULL",
                                "chargeable_weight_g = NULL",
                                "logistics_saved_at = NULL",
                            ]
                        )
                    conn.execute(
                        f"UPDATE shipment_erp SET {', '.join(reset_fragments)} WHERE job_id = ?",
                        (
                            ERP_RETRYABLE,
                            target_checkpoint,
                            now,
                            now,
                            int(current["id"]),
                        ),
                    )
                    new_state = f"{LOGISTICS_READY}/{ERP_RETRYABLE}/{target_checkpoint}"
                    event_stage = "erp"

                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, int(current["id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(current["id"]),
                    stage=event_stage,
                    event_type="SHIPMENT_STAGE_MANUALLY_REOPENED",
                    old_state=(
                        f"{current.get('logistics_state')}/{current.get('erp_state')}/"
                        f"{current.get('erp_checkpoint')}"
                    ),
                    new_state=new_state,
                    message=audit_reason,
                    details={
                        "source": "desktop_user",
                        "target_stage": target_stage,
                        "previous": previous,
                    },
                )
                changed.append(logistics_no)
            conn.commit()

        return ShipmentStatusChangeSummary(
            requested_count=len(normalized),
            changed_count=len(changed),
            unchanged_count=len(skipped) - missing_count,
            missing_count=missing_count,
            changed_logistics_nos=tuple(changed),
            skipped_reasons=skipped,
        )

    def move_completed_to_manual_review_many(
        self,
        logistics_nos: Iterable[str],
        *,
        reason: str,
    ) -> ShipmentStatusChangeSummary:
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("转为人工复核必须填写原因。")
        normalized = self._normalized_logistics_nos(logistics_nos)
        if not normalized:
            raise ValueError("请先勾选至少一条自动标发任务。")
        self.initialize()
        now = utc_now()
        changed: list[str] = []
        skipped: dict[str, str] = {}
        missing_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for logistics_no in normalized:
                row = conn.execute(
                    self._aggregate_sql() + " WHERE j.logistics_no = ?",
                    (logistics_no,),
                ).fetchone()
                if row is None:
                    missing_count += 1
                    skipped[logistics_no] = "队列中不存在该物流单号"
                    continue
                current = dict(row)
                if current.get("identity_state") != IDENTITY_ACTIVE:
                    skipped[logistics_no] = "任务不是活动状态"
                    continue
                if current.get("erp_state") != ERP_DONE:
                    skipped[logistics_no] = "订单尚未完成"
                    continue
                if _has_live_lease(current, now=now):
                    skipped[logistics_no] = "任务正在执行"
                    continue
                conn.execute(
                    """
                    UPDATE shipment_erp
                    SET state = ?, next_attempt_at = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (ERP_BLOCKED, f"人工复核：{audit_reason}", now, int(current["id"])),
                )
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, int(current["id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(current["id"]),
                    stage="erp",
                    event_type="MANUAL_COMPLETION_REVIEW_OPENED",
                    old_state=ERP_DONE,
                    new_state=ERP_BLOCKED,
                    message=audit_reason,
                    details={
                        "source": "desktop_user",
                        "preserved_checkpoint": current.get("erp_checkpoint"),
                        "completion_source": current.get("completion_source"),
                        "outbounded_at": current.get("outbounded_at"),
                        "externally_completed_at": current.get("externally_completed_at"),
                    },
                )
                changed.append(logistics_no)
            conn.commit()
        return ShipmentStatusChangeSummary(
            requested_count=len(normalized),
            changed_count=len(changed),
            unchanged_count=len(skipped) - missing_count,
            missing_count=missing_count,
            changed_logistics_nos=tuple(changed),
            skipped_reasons=skipped,
        )

    def retry_stage(self, logistics_no: str, stage: str, *, reason: str = "Manual retry") -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job or job["identity_state"] != IDENTITY_ACTIVE or job["erp_state"] == ERP_DONE:
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if stage == "logistics":
                old_state = job["logistics_state"]
                conn.execute(
                    "UPDATE shipment_logistics SET state = ?, next_attempt_at = ?, last_error = NULL, updated_at = ? WHERE job_id = ?",
                    (LOGISTICS_RETRYABLE, now, now, job["job_id"]),
                )
                new_state = LOGISTICS_RETRYABLE
            elif stage == "erp":
                if job["logistics_state"] != LOGISTICS_READY:
                    conn.rollback()
                    return False
                if job.get("policy_block_code") == AMAZON_MAIN_IMAGE_FORBIDDEN_CHANNEL:
                    conn.rollback()
                    return False
                old_state = job["erp_state"]
                conn.execute(
                    "UPDATE shipment_erp SET state = ?, next_attempt_at = ?, last_error = NULL, updated_at = ? WHERE job_id = ?",
                    (ERP_RETRYABLE, now, now, job["job_id"]),
                )
                new_state = ERP_RETRYABLE
            else:
                conn.rollback()
                return False
            conn.execute(
                "UPDATE shipment_jobs SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL, version = version + 1, updated_at = ? WHERE id = ?",
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn, job_id=job["job_id"], stage=stage, event_type="MANUAL_RETRY",
                old_state=old_state, new_state=new_state, message=reason,
            )
            conn.commit()
        return True

    def retry_email_batch(self, batch_id: int, *, reason: str = "Manual retry") -> bool:
        """Re-evaluate a local preview instead of overriding its safety state."""

        self.initialize()
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM shipment_email_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or batch["state"] == EMAIL_SENT:
                return False
            latest = conn.execute(
                """
                SELECT id, state
                FROM shipment_email_batches
                WHERE platform_order_no = ?
                ORDER BY sequence_no DESC LIMIT 1
                """,
                (batch["platform_order_no"],),
            ).fetchone()
            if latest is None or int(latest["id"]) != int(batch_id) or latest["state"] == EMAIL_SENT:
                return False
            platform_order_no = str(batch["platform_order_no"] or "").strip()
        if not platform_order_no:
            return False
        return self._prepare_platform_batch(
            platform_order_no,
            retry_requested=True,
            retry_reason=reason,
        )

    def mark_email_batch_sent(self, batch_id: int, *, sent_at: str | None = None) -> bool:
        self.initialize()
        now = sent_at or utc_now()
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM shipment_email_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch:
                return False
            if batch["state"] == EMAIL_SENT:
                return True
            conn.execute(
                """
                UPDATE shipment_email_batches
                SET state = ?, sent_at = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (EMAIL_SENT, now, now, batch_id),
            )
            self._insert_event_conn(
                conn, batch_id=batch_id, stage="email", event_type="EMAIL_MARKED_SENT",
                old_state=batch["state"], new_state=EMAIL_SENT,
            )
        return True

    def retry_email_for_logistics_no(self, logistics_no: str, *, reason: str = "Manual retry") -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
            return False
        platform_order_no = str(job.get("platform_order_no") or "").strip()
        if not platform_order_no:
            return False
        with self.connect() as conn:
            latest = conn.execute(
                """
                SELECT id, state
                FROM shipment_email_batches
                WHERE platform_order_no = ?
                ORDER BY sequence_no DESC LIMIT 1
                """,
                (platform_order_no,),
            ).fetchone()
        if latest is not None and latest["state"] == EMAIL_SENT:
            return False
        return self._prepare_platform_batch(
            platform_order_no,
            retry_requested=True,
            retry_reason=reason,
        )

    def resolve_conflict(self, logistics_no: str, system_order_no: str, platform_order_no: str) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job or job["identity_state"] != IDENTITY_CONFLICT:
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipment_jobs SET system_order_no = ?, platform_order_no = ?, identity_state = ?,
                    lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1 WHERE id = ?
                """,
                (system_order_no, platform_order_no, IDENTITY_ACTIVE, now, job["job_id"]),
            )
            self._insert_event_conn(
                conn, job_id=job["job_id"], stage="identity", event_type="CONFLICT_RESOLVED",
                old_state=IDENTITY_CONFLICT, new_state=IDENTITY_ACTIVE,
                details={"system_order_no": system_order_no, "platform_order_no": platform_order_no},
            )
        return True

    def restore_cancelled(self, logistics_no: str, *, reason: str) -> bool:
        """Restore a cancelled queue item without changing either stage state."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("恢复任务必须填写原因。")
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job or job["identity_state"] != IDENTITY_CANCELLED:
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            last_cancel = conn.execute(
                """
                SELECT id, old_state
                FROM shipment_events
                WHERE job_id = ? AND event_type = 'JOB_CANCELLED'
                ORDER BY id DESC LIMIT 1
                """,
                (job["job_id"],),
            ).fetchone()
            if last_cancel and last_cancel["old_state"] == IDENTITY_PAUSED_TAG_REMOVED:
                latest_tag_observation = conn.execute(
                    """
                    SELECT event_type FROM shipment_events
                    WHERE job_id = ?
                      AND event_type IN (
                          'TAG_RESTORED_WHILE_CANCELLED',
                          'TAG_REMOVED_WHILE_CANCELLED'
                      )
                      AND id > ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (job["job_id"], last_cancel["id"]),
                ).fetchone()
                if (
                    latest_tag_observation is None
                    or latest_tag_observation["event_type"] != "TAG_RESTORED_WHILE_CANCELLED"
                ):
                    conn.rollback()
                    return False
            changed = conn.execute(
                """
                UPDATE shipment_jobs
                SET identity_state = ?, cancelled_at = NULL, updated_at = ?,
                    lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    version = version + 1
                WHERE id = ? AND identity_state = ?
                """,
                (IDENTITY_ACTIVE, now, job["job_id"], IDENTITY_CANCELLED),
            ).rowcount
            if not changed:
                conn.rollback()
                return False
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="identity",
                event_type="JOB_RESTORED",
                old_state=IDENTITY_CANCELLED,
                new_state=IDENTITY_ACTIVE,
                message=audit_reason,
                details={"source": "desktop_user"},
            )
            conn.commit()
        return True

    def mark_manually_completed(self, logistics_no: str, *, reason: str) -> bool:
        """Record an operator-confirmed external completion without writing ERP."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("标记人工完成必须填写原因。")
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if (
            not job
            or job["identity_state"] != IDENTITY_ACTIVE
            or job["erp_state"] == ERP_DONE
        ):
            return False
        now = utc_now()
        previous = {
            "erp_state": str(job.get("erp_state") or ERP_WAITING),
            "erp_checkpoint": str(job.get("erp_checkpoint") or ERP_CHECKPOINT_NONE),
            "erp_last_error": job.get("erp_last_error"),
            "erp_next_attempt_at": job.get("erp_next_attempt_at"),
            "outbounded_at": job.get("outbounded_at"),
            "completion_source": job.get("completion_source"),
            "externally_completed_at": job.get("externally_completed_at"),
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, checkpoint = ?, outbounded_at = ?, next_attempt_at = NULL,
                    last_error = NULL, completion_source = ?, externally_completed_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND state <> ?
                """,
                (
                    ERP_DONE,
                    ERP_CHECKPOINT_OUTBOUNDED,
                    now,
                    ERP_COMPLETION_MANUAL_DETECTED,
                    now,
                    now,
                    job["job_id"],
                    ERP_DONE,
                ),
            )
            if conn.total_changes == 0:
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="erp",
                event_type="MANUAL_STATUS_SET_DONE",
                old_state=previous["erp_state"],
                new_state=ERP_DONE,
                message=audit_reason,
                details={"source": "desktop_user", "previous": previous},
            )
            conn.commit()
        return True

    def undo_manual_completion(self, logistics_no: str, *, reason: str) -> bool:
        """Undo only a completion created by :meth:`mark_manually_completed`."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("撤销人工完成必须填写原因。")
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if (
            not job
            or job["identity_state"] != IDENTITY_ACTIVE
            or job["erp_state"] != ERP_DONE
            or job.get("completion_source") != ERP_COMPLETION_MANUAL_DETECTED
        ):
            return False
        with self.connect() as conn:
            event = conn.execute(
                """
                SELECT details_json
                FROM shipment_events
                WHERE job_id = ? AND event_type = 'MANUAL_STATUS_SET_DONE'
                ORDER BY id DESC LIMIT 1
                """,
                (job["job_id"],),
            ).fetchone()
            if not event:
                return False
            try:
                previous = dict(json.loads(event["details_json"] or "{}").get("previous") or {})
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            previous_state = str(previous.get("erp_state") or "")
            previous_checkpoint = str(previous.get("erp_checkpoint") or "")
            if previous_state not in {
                ERP_WAITING,
                ERP_PENDING,
                ERP_RUNNING,
                ERP_RETRYABLE,
                ERP_BLOCKED,
            } or previous_checkpoint not in {
                ERP_CHECKPOINT_NONE,
                ERP_CHECKPOINT_CHANNEL_SET,
                ERP_CHECKPOINT_AUDITED,
                ERP_CHECKPOINT_LOGISTICS_SAVED,
            }:
                return False
            now = utc_now()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_erp
                SET state = ?, checkpoint = ?, next_attempt_at = ?, last_error = ?,
                    outbounded_at = ?, completion_source = ?, externally_completed_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND state = ? AND completion_source = ?
                """,
                (
                    previous_state,
                    previous_checkpoint,
                    previous.get("erp_next_attempt_at"),
                    previous.get("erp_last_error"),
                    previous.get("outbounded_at"),
                    previous.get("completion_source"),
                    previous.get("externally_completed_at"),
                    now,
                    job["job_id"],
                    ERP_DONE,
                    ERP_COMPLETION_MANUAL_DETECTED,
                ),
            )
            if conn.total_changes == 0:
                conn.rollback()
                return False
            conn.execute(
                """
                UPDATE shipment_jobs
                SET lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                    updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, job["job_id"]),
            )
            self._insert_event_conn(
                conn,
                job_id=job["job_id"],
                stage="erp",
                event_type="MANUAL_STATUS_UNDONE",
                old_state=ERP_DONE,
                new_state=previous_state,
                message=audit_reason,
                details={"source": "desktop_user", "restored_checkpoint": previous_checkpoint},
            )
            conn.commit()
        return True

    def cancel(self, logistics_no: str, reason: str) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if (
            not job
            or job["identity_state"] == IDENTITY_CANCELLED
            or job["erp_state"] == ERP_DONE
        ):
            return False
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE shipment_jobs SET identity_state = ?, cancelled_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_stage = NULL, lease_until = NULL, version = version + 1
                WHERE id = ?
                """,
                (IDENTITY_CANCELLED, now, now, job["job_id"]),
            )
            self._insert_event_conn(
                conn, job_id=job["job_id"], stage="identity", event_type="JOB_CANCELLED",
                old_state=job["identity_state"], new_state=IDENTITY_CANCELLED, message=reason,
            )
        return True

    def cancel_many(self, logistics_nos: Iterable[str], reason: str) -> int:
        """Cancel multiple local queue jobs in one SQLite transaction."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("取消任务必须填写原因。")
        normalized = list(
            dict.fromkeys(str(value or "").strip() for value in logistics_nos)
        )
        normalized = [value for value in normalized if value]
        if not normalized:
            raise ValueError("请先勾选至少一条自动标发任务。")
        self.initialize()
        now = utc_now()
        changed = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for logistics_no in normalized:
                job = conn.execute(
                    """
                    SELECT j.id, j.identity_state, e.state AS erp_state
                    FROM shipment_jobs j
                    JOIN shipment_erp e ON e.job_id = j.id
                    WHERE j.logistics_no = ?
                    """,
                    (logistics_no,),
                ).fetchone()
                if (
                    job is None
                    or str(job["identity_state"]) == IDENTITY_CANCELLED
                    or str(job["erp_state"]) == ERP_DONE
                ):
                    continue
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, cancelled_at = ?, updated_at = ?,
                        lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (IDENTITY_CANCELLED, now, now, int(job["id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(job["id"]),
                    stage="identity",
                    event_type="JOB_CANCELLED",
                    old_state=str(job["identity_state"]),
                    new_state=IDENTITY_CANCELLED,
                    message=audit_reason,
                    details={"source": "desktop_batch"},
                )
                changed += 1
            conn.commit()
        return changed

    def mark_manually_cancelled_many(
        self,
        logistics_nos: Iterable[str],
        *,
        reason: str,
    ) -> ShipmentStatusChangeSummary:
        """Persistently cancel local queue records, including completed ones."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("人工取消自动标发任务必须填写原因。")
        normalized = self._normalized_logistics_nos(logistics_nos)
        if not normalized:
            raise ValueError("请先勾选至少一条自动标发任务。")
        self.initialize()
        now = utc_now()
        changed: list[str] = []
        skipped: dict[str, str] = {}
        missing_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for logistics_no in normalized:
                row = conn.execute(
                    self._aggregate_sql() + " WHERE j.logistics_no = ?",
                    (logistics_no,),
                ).fetchone()
                if row is None:
                    missing_count += 1
                    skipped[logistics_no] = "队列中不存在该物流单号"
                    continue
                current = dict(row)
                identity_state = str(current.get("identity_state") or "")
                if identity_state == IDENTITY_MANUALLY_CANCELLED:
                    skipped[logistics_no] = "已经是人工取消状态"
                    continue
                if identity_state == IDENTITY_SUPERSEDED:
                    skipped[logistics_no] = "该记录已被新的 ALS 记录替代"
                    continue
                if _has_live_lease(current, now=now):
                    skipped[logistics_no] = "任务正在执行，请先暂停并等待当前原子操作结束"
                    continue
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, cancelled_at = ?, updated_at = ?,
                        lease_owner = NULL, lease_stage = NULL, lease_until = NULL,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (IDENTITY_MANUALLY_CANCELLED, now, now, int(current["id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(current["id"]),
                    stage="identity",
                    event_type="JOB_MANUALLY_CANCELLED",
                    old_state=identity_state,
                    new_state=IDENTITY_MANUALLY_CANCELLED,
                    message=audit_reason,
                    details={
                        "source": "desktop_user",
                        "logistics_state_preserved": current.get("logistics_state"),
                        "erp_state_preserved": current.get("erp_state"),
                        "erp_checkpoint_preserved": current.get("erp_checkpoint"),
                    },
                )
                changed.append(logistics_no)
            conn.commit()
        return ShipmentStatusChangeSummary(
            requested_count=len(normalized),
            changed_count=len(changed),
            unchanged_count=len(skipped) - missing_count,
            missing_count=missing_count,
            changed_logistics_nos=tuple(changed),
            skipped_reasons=skipped,
        )

    def restore_manually_cancelled_many(
        self,
        logistics_nos: Iterable[str],
        *,
        reason: str,
    ) -> ShipmentStatusChangeSummary:
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("恢复人工取消任务必须填写原因。")
        normalized = self._normalized_logistics_nos(logistics_nos)
        if not normalized:
            raise ValueError("请先勾选至少一条自动标发任务。")
        self.initialize()
        now = utc_now()
        changed: list[str] = []
        skipped: dict[str, str] = {}
        missing_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for logistics_no in normalized:
                row = conn.execute(
                    "SELECT id, identity_state FROM shipment_jobs WHERE logistics_no = ?",
                    (logistics_no,),
                ).fetchone()
                if row is None:
                    missing_count += 1
                    skipped[logistics_no] = "队列中不存在该物流单号"
                    continue
                if str(row["identity_state"]) != IDENTITY_MANUALLY_CANCELLED:
                    skipped[logistics_no] = "当前不是人工取消状态"
                    continue
                conn.execute(
                    """
                    UPDATE shipment_jobs
                    SET identity_state = ?, cancelled_at = NULL, updated_at = ?,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (IDENTITY_ACTIVE, now, int(row["id"])),
                )
                self._insert_event_conn(
                    conn,
                    job_id=int(row["id"]),
                    stage="identity",
                    event_type="JOB_MANUAL_CANCELLATION_RESTORED",
                    old_state=IDENTITY_MANUALLY_CANCELLED,
                    new_state=IDENTITY_ACTIVE,
                    message=audit_reason,
                    details={"source": "desktop_user"},
                )
                changed.append(logistics_no)
            conn.commit()
        return ShipmentStatusChangeSummary(
            requested_count=len(normalized),
            changed_count=len(changed),
            unchanged_count=len(skipped) - missing_count,
            missing_count=missing_count,
            changed_logistics_nos=tuple(changed),
            skipped_reasons=skipped,
        )


# Compatibility name retained for existing imports and third-party scripts.
ShipmentQueueStore = ShipmentWorkflowStore
