from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .alibaba_logistics import (
    is_tracking_number_mismatch_reason,
    normalize_carrier_name,
    normalize_tracking_number,
    tracking_number_matches_carrier,
    tracking_number_mismatch_reason,
)

from .models import (
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
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
)


SCHEMA_VERSION = 6
DEFAULT_RETRY_HOURS = 3
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
    "浏览器关闭",
)
RETRYABLE_LOGISTICS_ERROR_KEYWORDS = (
    *BROWSER_CLOSED_KEYWORDS,
    "等待阿里国际站物流详情页加载或登录完成超时",
    "登录完成超时",
    "页面加载",
    "timeout",
    "timed out",
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_after(hours: float = DEFAULT_RETRY_HOURS) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _is_retryable_logistics_error(value: Any) -> bool:
    text = str(value or "").lower()
    return any(keyword.lower() in text for keyword in RETRYABLE_LOGISTICS_ERROR_KEYWORDS)


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
                        current_version < SCHEMA_VERSION
                        or not {
                            "tracking_mismatch_action",
                            "tracking_mismatch_reviewed_at",
                        }.issubset(logistics_columns)
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
                self._protect_legacy_table(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._initialized = True

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
                customer_remark TEXT,
                source_status_text TEXT,
                receiver_email TEXT,
                source_page INTEGER,
                source_scroll_top INTEGER,
                source_rowid TEXT,
                sales_channel TEXT NOT NULL DEFAULT 'MARKETPLACE',
                customer_email_required INTEGER NOT NULL DEFAULT 1,
                identity_state TEXT NOT NULL DEFAULT 'ACTIVE',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                cancelled_at TEXT,
                lease_owner TEXT,
                lease_stage TEXT,
                lease_until TEXT,
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_shipment_jobs_platform ON shipment_jobs(platform_order_no);
            CREATE INDEX IF NOT EXISTS idx_shipment_jobs_lease ON shipment_jobs(lease_stage, lease_until);

            CREATE TABLE IF NOT EXISTS shipment_logistics (
                job_id INTEGER PRIMARY KEY REFERENCES shipment_jobs(id) ON DELETE CASCADE,
                state TEXT NOT NULL,
                alibaba_status TEXT,
                service_type TEXT,
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
                updated_at TEXT NOT NULL
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
                completion_source TEXT,
                externally_completed_at TEXT,
                updated_at TEXT NOT NULL
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
                    logistics_state = LOGISTICS_BLOCKED
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
            content_hash = self._email_content_hash(job_rows)
            message_id = self._email_message_id(platform_order_no, 1, content_hash)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO shipment_email_batches (
                    platform_order_no, sequence_no, state, recipient_email, message_id,
                    template_version, content_hash, sent_at, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, 'v1', ?, ?, ?, ?)
                """,
                (platform_order_no, EMAIL_SENT, job_rows[0]["receiver_email"], message_id, content_hash, now, now, now),
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
            SELECT j.*, l.state AS logistics_state, l.alibaba_status, l.service_type,
                   l.carrier_raw, l.carrier_normalized, l.international_tracking_no,
                   l.currency, l.fee_amount, l.chargeable_weight_kg, l.package_count,
                   l.source_url, l.tracking_override_carrier, l.tracking_override_no,
                   l.tracking_override_at, l.tracking_override_reason,
                   l.tracking_mismatch_action, l.tracking_mismatch_reviewed_at,
                   l.last_checked_at, l.next_attempt_at AS logistics_next_attempt_at,
                   l.attempt_count AS logistics_attempt_count, l.last_error AS logistics_last_error,
                   e.state AS erp_state, e.checkpoint AS erp_checkpoint, e.channel_path,
                   e.freight_amount, e.chargeable_weight_g, e.channel_payload_hash,
                   e.logistics_payload_hash, e.next_attempt_at AS erp_next_attempt_at,
                   e.attempt_count AS erp_attempt_count, e.last_error AS erp_last_error,
                   e.channel_set_at, e.audited_at, e.logistics_saved_at, e.outbounded_at,
                   e.completion_source, e.externally_completed_at,
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
                   ) AS email_attempt_count
            FROM shipment_jobs j
            JOIN shipment_logistics l ON l.job_id = j.id
            JOIN shipment_erp e ON e.job_id = j.id
        """

    def _flatten(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        actual_total = None
        if item.get("fee_amount"):
            actual_total = f"{item.get('currency')} {item.get('fee_amount')}".strip()
        item.update(
            {
                "job_id": item["id"],
                "logistics_no": item["logistics_no"],
                "status_text": item.get("source_status_text"),
                "carrier": item.get("carrier_normalized") or item.get("carrier_raw"),
                "actual_total": actual_total,
                "last_error": (
                    item.get("erp_last_error")
                    or item.get("logistics_last_error")
                    or item.get("email_last_error")
                ),
            }
        )
        return item

    def get_by_logistics_no(self, logistics_no: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(self._aggregate_sql() + " WHERE j.logistics_no = ?", (logistics_no,)).fetchone()
        return self._flatten(row) if row else None

    def upsert_candidate(self, candidate: ShipmentCandidate, *, run_id: str | None = None) -> QueueInsertResult:
        self.initialize()
        now = utc_now()
        sales_channel = candidate.sales_channel or normalize_sales_channel(candidate.platform_order_no)
        email_required = (
            customer_email_required_for_sales_channel(sales_channel)
            if candidate.customer_email_required is None
            else bool(candidate.customer_email_required)
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM shipment_jobs WHERE logistics_no = ?", (candidate.logistics_no,)).fetchone()
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
                        SET shipment_tag_name = ?, tag_text = ?, sku_text = ?, customer_remark = ?,
                            source_status_text = ?, receiver_email = COALESCE(?, receiver_email),
                            source_page = ?, source_scroll_top = ?, source_rowid = ?,
                            sales_channel = ?, customer_email_required = ?,
                            last_seen_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            candidate.shipment_tag_name, candidate.tag_text, candidate.sku_text,
                            candidate.customer_remark, candidate.status_text, candidate.receiver_email,
                            candidate.source_page, candidate.source_scroll_top, candidate.rowid,
                            sales_channel, 1 if email_required else 0,
                            now, now, existing["id"],
                        ),
                    )
                    immediate_logistics = False
                    immediate_erp = False
                    if existing["identity_state"] == IDENTITY_ACTIVE and stage_row and stage_row["erp_state"] != ERP_DONE:
                        logistics_state = stage_row["logistics_state"]
                        logistics_is_retryable_blocked = (
                            logistics_state == LOGISTICS_BLOCKED
                            and _is_retryable_logistics_error(stage_row["logistics_last_error"])
                        )
                        if logistics_state in {LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE} or logistics_is_retryable_blocked:
                            next_state = LOGISTICS_RETRYABLE if logistics_is_retryable_blocked else logistics_state
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
                            and stage_row["erp_state"] in {ERP_WAITING, ERP_PENDING, ERP_RETRYABLE}
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
                    tag_text, sku_text, customer_remark, source_status_text, receiver_email,
                    source_page, source_scroll_top, source_rowid, sales_channel, customer_email_required,
                    identity_state,
                    first_seen_at, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.logistics_no, candidate.system_order_no, candidate.platform_order_no,
                    candidate.shipment_tag_name, candidate.tag_text, candidate.sku_text,
                    candidate.customer_remark, candidate.status_text, candidate.receiver_email,
                    candidate.source_page, candidate.source_scroll_top, candidate.rowid,
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

    def list_logistics_check_candidates(self, *, limit: int = 0, **_kwargs: Any) -> list[dict[str, Any]]:
        self.initialize()
        now = utc_now()
        sql = self._aggregate_sql() + """
            WHERE j.identity_state = ?
              AND (
                  l.state IN (?, ?, ?)
                  OR (
                      l.state = ? AND l.tracking_mismatch_action = ?
                      AND l.next_attempt_at IS NOT NULL
                  )
              )
              AND (l.next_attempt_at IS NULL OR l.next_attempt_at <= ?)
              AND e.state <> ?
              AND (j.lease_until IS NULL OR j.lease_until <= ?)
            ORDER BY COALESCE(l.next_attempt_at, j.created_at), j.id
        """
        params: list[Any] = [
            IDENTITY_ACTIVE, LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE,
            LOGISTICS_BLOCKED, TRACKING_REVIEW_AUTO_RECHECK,
            now, ERP_DONE, now,
        ]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._flatten(row) for row in rows]

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
                    j.identity_state = ? AND (
                        l.state IN (?, ?, ?)
                        OR (
                            l.state = ? AND l.tracking_mismatch_action = ?
                            AND l.next_attempt_at IS NOT NULL
                        )
                    )
                    AND (l.next_attempt_at IS NULL OR l.next_attempt_at <= ?)
                    AND e.state <> ?
                """
                params: list[Any] = [
                    IDENTITY_ACTIVE, LOGISTICS_PENDING, LOGISTICS_WAITING, LOGISTICS_RETRYABLE,
                    LOGISTICS_BLOCKED, TRACKING_REVIEW_AUTO_RECHECK, now, ERP_DONE,
                ]
                order = "COALESCE(l.next_attempt_at, j.created_at), j.id"
            else:
                where = """
                    j.identity_state = ? AND l.state = ? AND e.state IN (?, ?, ?)
                    AND (e.next_attempt_at IS NULL OR e.next_attempt_at <= ?)
                """
                params = [IDENTITY_ACTIVE, LOGISTICS_READY, ERP_PENDING, ERP_RETRYABLE, ERP_RUNNING, now]
                order = "COALESCE(e.next_attempt_at, j.created_at), j.id"
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
        currency, fee_amount = _split_money(detail.actual_total)
        now = utc_now()
        mismatch_blocked = state == LOGISTICS_BLOCKED and is_tracking_number_mismatch_reason(last_error)
        next_attempt = (
            utc_after()
            if state in {LOGISTICS_WAITING, LOGISTICS_RETRYABLE}
            or (mismatch_blocked and job.get("tracking_mismatch_action") == TRACKING_REVIEW_AUTO_RECHECK)
            else None
        )
        keep_tracking_override = bool(
            job.get("tracking_override_at")
            and normalize_carrier_name(detail.carrier) == job.get("tracking_override_carrier")
            and normalize_tracking_number(detail.international_tracking_no) == job.get("tracking_override_no")
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
                SET state = ?, alibaba_status = ?, service_type = ?, carrier_raw = ?,
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
                    state, detail.status_text, detail.service_type, detail.carrier, detail.carrier,
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
                conn.execute(
                    """
                    UPDATE shipment_erp SET state = CASE WHEN state = ? THEN ? ELSE state END,
                        next_attempt_at = NULL, updated_at = ? WHERE job_id = ?
                    """,
                    (ERP_WAITING, ERP_PENDING, now, job["job_id"]),
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
        next_attempt = (
            utc_after()
            if job.get("tracking_mismatch_action") == TRACKING_REVIEW_AUTO_RECHECK
            else None
        )
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
                (LOGISTICS_BLOCKED, next_attempt, reason, now, job["job_id"]),
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
                event_type="TRACKING_NUMBER_BLOCKED",
                old_state=f"{job['logistics_state']}/{job['erp_state']}",
                new_state=f"{LOGISTICS_BLOCKED}/{ERP_WAITING}",
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
                  WHERE j.identity_state = ? AND l.state = ?
                    AND l.tracking_mismatch_action IS NULL
                    AND e.state <> ?
                  ORDER BY j.updated_at, j.id
                """,
                (IDENTITY_ACTIVE, LOGISTICS_BLOCKED, ERP_DONE),
            ).fetchall()
        return [
            item
            for item in (self._flatten(row) for row in rows)
            if is_tracking_number_mismatch_reason(item.get("logistics_last_error"))
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
            or job["logistics_state"] != LOGISTICS_BLOCKED
            or not is_tracking_number_mismatch_reason(job.get("logistics_last_error"))
        ):
            return False
        now = utc_now()
        next_attempt = now if action == TRACKING_REVIEW_AUTO_RECHECK else None
        old_action = job.get("tracking_mismatch_action")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE shipment_logistics
                SET tracking_mismatch_action = ?, tracking_mismatch_reviewed_at = ?,
                    next_attempt_at = ?, tracking_override_carrier = NULL,
                    tracking_override_no = NULL, tracking_override_at = NULL,
                    tracking_override_reason = NULL, updated_at = ?
                WHERE job_id = ?
                """,
                (action, now, next_attempt, now, job["job_id"]),
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
        if job["logistics_state"] != LOGISTICS_BLOCKED:
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
                old_state=f"{LOGISTICS_BLOCKED}/{job['erp_state']}",
                new_state=f"{LOGISTICS_READY}/{ERP_PENDING}",
                message=reason,
                details={"carrier": carrier_key, "tracking_no": tracking_key},
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
            sales_channel=item.get("sales_channel") or SALES_CHANNEL_MARKETPLACE,
            customer_email_required=bool(item.get("customer_email_required", 1)),
            tracking_manually_verified=bool(
                item.get("tracking_override_at")
                and normalize_carrier_name(item.get("carrier_normalized") or item.get("carrier_raw"))
                == item.get("tracking_override_carrier")
                and normalize_tracking_number(item.get("international_tracking_no"))
                == item.get("tracking_override_no")
            ),
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
        if checkpoint == ERP_CHECKPOINT_OUTBOUNDED:
            self.prepare_email_batches(platform_order_no=job["platform_order_no"])
        return int(new_version)

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

    def finish_erp_attempt(
        self,
        logistics_no: str,
        *,
        owner: str | None,
        state: str,
        last_error: str | None,
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
                    attempt_count = attempt_count + 1, last_error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (state, next_attempt, last_error, now, job["job_id"]),
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

    def mark_erp_outbounded(self, logistics_no: str) -> bool:
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
                      j.identity_state = ?
                      OR l.state = ? OR e.state = ?
                      OR (l.state = ? AND l.attempt_count >= 3)
                      OR (e.state = ? AND e.attempt_count >= 3)
                  )
                  ORDER BY j.updated_at, j.id
                """,
                (
                    ERP_DONE, IDENTITY_CONFLICT, LOGISTICS_BLOCKED, ERP_BLOCKED,
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
        self.initialize()
        with self.connect() as conn:
            platforms_sql = """
                SELECT DISTINCT j.platform_order_no
                FROM shipment_jobs j JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state = ? AND e.completion_source = ?
                  AND j.customer_email_required = 1
            """
            params: list[Any] = [IDENTITY_ACTIVE, ERP_COMPLETION_AUTOMATION]
            if platform_order_no:
                platforms_sql += " AND j.platform_order_no = ?"
                params.append(platform_order_no)
            platforms = [row[0] for row in conn.execute(platforms_sql, params).fetchall()]
        for platform in platforms:
            self._prepare_platform_batch(platform)
        return self.list_email_batches(platform_order_no=platform_order_no)

    def _prepare_platform_batch(self, platform_order_no: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            all_jobs = conn.execute(
                """
                SELECT j.id, j.logistics_no, j.receiver_email, e.state AS erp_state,
                       e.checkpoint, e.completion_source,
                       l.carrier_normalized, l.carrier_raw, l.international_tracking_no,
                       j.customer_email_required
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                JOIN shipment_logistics l ON l.job_id = j.id
                WHERE j.platform_order_no = ? AND j.identity_state = ?
                ORDER BY j.id
                """,
                (platform_order_no, IDENTITY_ACTIVE),
            ).fetchall()
            if not all_jobs or any(row["erp_state"] != ERP_DONE or row["checkpoint"] != ERP_CHECKPOINT_OUTBOUNDED for row in all_jobs):
                conn.rollback()
                return
            email_jobs = [
                row for row in all_jobs
                if row["completion_source"] == ERP_COMPLETION_AUTOMATION and int(row["customer_email_required"] or 0) == 1
            ]
            if not email_jobs:
                conn.rollback()
                return
            content_hash = self._email_content_hash(email_jobs)
            latest = conn.execute(
                "SELECT * FROM shipment_email_batches WHERE platform_order_no = ? ORDER BY sequence_no DESC LIMIT 1",
                (platform_order_no,),
            ).fetchone()
            if latest and latest["content_hash"] == content_hash:
                conn.rollback()
                return
            emails = {str(row["receiver_email"] or "").strip().lower() for row in email_jobs if str(row["receiver_email"] or "").strip()}
            blocked_reason = None
            recipient = next(iter(emails)) if len(emails) == 1 else None
            if not emails:
                blocked_reason = "Missing receiver email."
            elif len(emails) > 1:
                blocked_reason = "Conflicting receiver emails for the same platform order."
            state = EMAIL_BLOCKED if blocked_reason else EMAIL_PENDING
            if latest and latest["state"] != EMAIL_SENT:
                sequence_no = latest["sequence_no"]
                message_id = self._email_message_id(platform_order_no, sequence_no, content_hash)
                conn.execute(
                    """
                    UPDATE shipment_email_batches
                    SET state = ?, recipient_email = ?, message_id = ?, content_hash = ?,
                        last_error = ?, updated_at = ? WHERE id = ?
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
                new_state=state, message=blocked_reason,
                details={"content_hash": content_hash, "sequence_no": sequence_no},
            )
            conn.commit()

    @staticmethod
    def _email_content_hash(rows: Iterable[sqlite3.Row]) -> str:
        payload = [
            {
                "logistics_no": row["logistics_no"],
                "carrier": row["carrier_normalized"] or row["carrier_raw"],
                "tracking": row["international_tracking_no"],
            }
            for row in rows
        ]
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
        sql = self._aggregate_sql() + """
            WHERE (
               e.state <> ? AND (
                   j.identity_state = ?
                   OR l.state = ? OR e.state = ?
                   OR (l.state = ? AND l.attempt_count >= 3)
                   OR (e.state = ? AND e.attempt_count >= 3)
               )
            ) OR EXISTS (
               SELECT 1
               FROM shipment_email_batches b
               JOIN shipment_email_batch_items bi ON bi.batch_id = b.id
               WHERE bi.job_id = j.id
                 AND (b.state = ? OR (b.state = ? AND b.attempt_count >= 3))
            )
            ORDER BY j.updated_at, j.id
        """
        params: list[Any] = [
            ERP_DONE, IDENTITY_CONFLICT, LOGISTICS_BLOCKED, ERP_BLOCKED,
            LOGISTICS_RETRYABLE, ERP_RETRYABLE, EMAIL_BLOCKED, EMAIL_RETRYABLE,
        ]
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [self._flatten(row) for row in conn.execute(sql, params).fetchall()]

    def list_all_jobs(self, *, limit: int = 0) -> list[dict[str, Any]]:
        self.initialize()
        sql = self._aggregate_sql() + " ORDER BY j.updated_at, j.id"
        params: list[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return [self._flatten(row) for row in conn.execute(sql, params).fetchall()]

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
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            batch = conn.execute("SELECT * FROM shipment_email_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or batch["state"] == EMAIL_SENT:
                return False
            conn.execute(
                "UPDATE shipment_email_batches SET state = ?, next_attempt_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (EMAIL_PENDING, now, now, batch_id),
            )
            self._insert_event_conn(
                conn, batch_id=batch_id, stage="email", event_type="MANUAL_RETRY",
                old_state=batch["state"], new_state=EMAIL_PENDING, message=reason,
            )
        return True

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
        with self.connect() as conn:
            batch = conn.execute(
                """
                SELECT b.id
                FROM shipment_email_batches b
                JOIN shipment_email_batch_items i ON i.batch_id = b.id
                WHERE i.job_id = ?
                ORDER BY b.sequence_no DESC
                LIMIT 1
                """,
                (job["job_id"],),
            ).fetchone()
        return self.retry_email_batch(batch["id"], reason=reason) if batch else False

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

    def cancel(self, logistics_no: str, reason: str) -> bool:
        self.initialize()
        job = self.get_by_logistics_no(logistics_no)
        if not job:
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


# Compatibility name retained for existing imports and third-party scripts.
ShipmentQueueStore = ShipmentWorkflowStore
