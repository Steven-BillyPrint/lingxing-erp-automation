from __future__ import annotations

import hashlib
import json
import sqlite3
from threading import RLock
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .notification_domain import (
    CHANNEL_EMAIL,
    CHANNEL_MANUAL_EMAIL,
    CHANNEL_SMS,
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    CONTACT_SOURCE_DESKTOP_MANUAL,
    CONTACT_SOURCE_LINGXING_API_FALLBACK,
    CONTACT_SOURCE_LINGXING_ORDER_LIST,
    CONTACT_SOURCE_LINGXING_DETAIL_REFRESH,
    CONTACT_SOURCE_WMS,
    EMAIL_PRESENCE_NOT_PROVIDED,
    EMAIL_PRESENCE_PROVIDED,
    EMAIL_PRESENCE_UNKNOWN,
    INDEPENDENT_SITE_ORDER_RE,
    NOTIFICATION_ACCEPTED,
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_BLOCKED,
    NOTIFICATION_CANCELLED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DELIVERY_UNCONFIRMED,
    NOTIFICATION_FAILED,
    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
    NOTIFICATION_MANUALLY_COMPLETED,
    NOTIFICATION_REJECTED,
    NOTIFICATION_RETRYABLE,
    NOTIFICATION_SENDING,
    NOTIFICATION_SUPPRESSED,
    NOTIFICATION_WAITING_CONTACT,
    PHONE_VERIFICATION_MATCHED,
    PHONE_VERIFICATION_MISSING,
    PHONE_VERIFICATION_NOT_REQUIRED,
    PHONE_VERIFICATION_UNKNOWN,
    PLATFORM_POLICY_AMAZON,
    PLATFORM_POLICY_INDEPENDENT_SITE,
    NotificationConfiguration,
    OrderContact,
    OrderProductSnapshot,
    PackageSnapshot,
    RenderedNotification,
    is_independent_site_order,
    is_virtual_email,
    normalize_email,
    normalize_phone,
    normalize_recipient_name,
    render_notification,
    shorten_product_title,
    stable_package_label,
    tracking_url_for,
)
from .models import ERP_COMPLETION_AUTOMATION


NOTIFICATION_SYNC_RETRYABLE = "RETRYABLE"
NOTIFICATION_SYNC_SYNCED = "SYNCED"
NOTIFICATION_SYNC_RETRY_BASE_SECONDS = 15 * 60
NOTIFICATION_SYNC_RETRY_MAX_SECONDS = 24 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _recipient_name_key(value: str | None) -> str:
    return " ".join(normalize_recipient_name(value).split()).casefold()


def _recipient_name_candidates(values: Sequence[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(normalize_recipient_name(value).split())
        key = _recipient_name_key(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(normalized)
    return tuple(candidates)


_RECEIPT_CHECK_OFFSETS_MINUTES = (1, 5, 15, 30, 60)
_RECEIPT_DEADLINE_HOURS = 24


def _parse_utc(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _receipt_deadline(sent_at: str, *, fallback: datetime) -> datetime:
    return (_parse_utc(sent_at) or fallback) + timedelta(hours=_RECEIPT_DEADLINE_HOURS)


def _next_receipt_check(sent_at: str, *, after: datetime) -> str:
    started = _parse_utc(sent_at) or after
    deadline = started + timedelta(hours=_RECEIPT_DEADLINE_HOURS)
    for offset in _RECEIPT_CHECK_OFFSETS_MINUTES:
        candidate = started + timedelta(minutes=offset)
        if candidate > after:
            return _format_utc(candidate)
    elapsed_hours = max(1, int((after - started).total_seconds() // 3600) + 1)
    candidate = started + timedelta(hours=elapsed_hours)
    return _format_utc(candidate) if candidate <= deadline else ""


class NotificationStateError(RuntimeError):
    pass


class StaleNotificationError(NotificationStateError):
    pass


def initialize_notification_schema(conn: sqlite3.Connection) -> None:
    """Create the v10 notification tables without altering legacy mail history."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shipment_order_contacts (
            platform_order_no TEXT PRIMARY KEY,
            recipient_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            email_presence TEXT NOT NULL DEFAULT 'UNKNOWN',
            phone_raw TEXT NOT NULL DEFAULT '',
            phone_e164 TEXT NOT NULL DEFAULT '',
            sales_platform_code TEXT NOT NULL DEFAULT '',
            sales_platform_name TEXT NOT NULL DEFAULT '',
            store_name TEXT NOT NULL DEFAULT '',
            site_name TEXT NOT NULL DEFAULT '',
            contact_source TEXT NOT NULL DEFAULT '',
            recipient_name_source TEXT NOT NULL DEFAULT '',
            email_source TEXT NOT NULL DEFAULT '',
            phone_source TEXT NOT NULL DEFAULT '',
            verified_phone_e164 TEXT NOT NULL DEFAULT '',
            phone_verification_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            contact_captured_at TEXT NOT NULL DEFAULT '',
            system_order_nos_json TEXT NOT NULL DEFAULT '[]',
            contact_updated_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_package_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_order_no TEXT NOT NULL,
            package_key TEXT NOT NULL,
            stable_sequence INTEGER NOT NULL,
            stable_label TEXT NOT NULL,
            system_order_no TEXT NOT NULL DEFAULT '',
            shipment_type TEXT NOT NULL,
            carrier_raw TEXT NOT NULL DEFAULT '',
            carrier_normalized TEXT NOT NULL DEFAULT '',
            waybill_no TEXT NOT NULL DEFAULT '',
            tracking_no TEXT NOT NULL DEFAULT '',
            final_tracking_no TEXT NOT NULL DEFAULT '',
            wms_outbound_order_no TEXT NOT NULL DEFAULT '',
            wms_status_code INTEGER,
            wms_status_name TEXT NOT NULL DEFAULT '',
            outbound_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            outbound_observed_at TEXT NOT NULL DEFAULT '',
            customer_visible INTEGER NOT NULL DEFAULT 1,
            visibility_reason TEXT NOT NULL DEFAULT '',
            source_payload_hash TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform_order_no, package_key),
            UNIQUE(platform_order_no, stable_sequence)
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_packages_platform_active
            ON shipment_package_snapshots(platform_order_no, active, stable_sequence);

        CREATE TABLE IF NOT EXISTS shipment_notification_outbound_eligibility (
            platform_order_no TEXT PRIMARY KEY,
            outbound_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            reason TEXT NOT NULL DEFAULT '',
            expected_system_order_nos_json TEXT NOT NULL DEFAULT '[]',
            observed_system_order_nos_json TEXT NOT NULL DEFAULT '[]',
            package_set_hash TEXT NOT NULL DEFAULT '',
            snapshot_complete INTEGER NOT NULL DEFAULT 0,
            observed_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_order_product_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_order_no TEXT NOT NULL,
            system_order_no TEXT NOT NULL,
            item_key TEXT NOT NULL,
            source_sequence INTEGER NOT NULL DEFAULT 0,
            local_sku TEXT NOT NULL DEFAULT '',
            raw_title TEXT NOT NULL DEFAULT '',
            display_title TEXT NOT NULL DEFAULT '',
            has_main_image INTEGER NOT NULL DEFAULT 0,
            metadata_valid INTEGER NOT NULL DEFAULT 1,
            is_instruction INTEGER NOT NULL DEFAULT 0,
            source_payload_hash TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform_order_no, system_order_no, item_key)
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_products_platform_active
            ON shipment_order_product_snapshots(
                platform_order_no, active, system_order_no, id
            );

        CREATE TABLE IF NOT EXISTS shipment_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_order_no TEXT NOT NULL,
            revision INTEGER NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'AUTO_ERP',
            channel TEXT,
            state TEXT NOT NULL,
            recipient_name TEXT NOT NULL DEFAULT '',
            recipient_email TEXT NOT NULL DEFAULT '',
            email_presence TEXT NOT NULL DEFAULT 'UNKNOWN',
            recipient_phone TEXT NOT NULL DEFAULT '',
            sales_platform_code TEXT NOT NULL DEFAULT '',
            sales_platform_name TEXT NOT NULL DEFAULT '',
            store_name TEXT NOT NULL DEFAULT '',
            site_name TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '',
            sender_email TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            body_html TEXT NOT NULL DEFAULT '',
            sms_encoding TEXT NOT NULL DEFAULT '',
            sms_character_count INTEGER NOT NULL DEFAULT 0,
            sms_segment_count INTEGER NOT NULL DEFAULT 0,
            package_total INTEGER NOT NULL DEFAULT 0,
            package_complete INTEGER NOT NULL DEFAULT 0,
            package_missing INTEGER NOT NULL DEFAULT 0,
            product_names_json TEXT NOT NULL DEFAULT '[]',
            queue_total INTEGER NOT NULL DEFAULT 0,
            queue_complete INTEGER NOT NULL DEFAULT 0,
            template_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            approved_content_hash TEXT,
            provider_message_id TEXT,
            provider_status TEXT,
            provider_operator_email TEXT NOT NULL DEFAULT '',
            receipt_next_check_at TEXT NOT NULL DEFAULT '',
            receipt_last_checked_at TEXT NOT NULL DEFAULT '',
            receipt_deadline_at TEXT NOT NULL DEFAULT '',
            receipt_check_attempt_count INTEGER NOT NULL DEFAULT 0,
            receipt_check_lease_owner TEXT NOT NULL DEFAULT '',
            receipt_check_lease_until TEXT NOT NULL DEFAULT '',
            legacy_email_batch_id INTEGER UNIQUE,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            approved_at TEXT,
            sent_at TEXT,
            delivered_at TEXT,
            erp_completed_at TEXT NOT NULL DEFAULT '',
            state_changed_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform_order_no, revision),
            UNIQUE(idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_notifications_review
            ON shipment_notifications(state, updated_at, id);
        CREATE TABLE IF NOT EXISTS shipment_notification_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL
                REFERENCES shipment_notifications(id) ON DELETE CASCADE,
            package_snapshot_id INTEGER,
            package_key TEXT NOT NULL,
            stable_sequence INTEGER NOT NULL,
            stable_label TEXT NOT NULL,
            system_order_no TEXT NOT NULL DEFAULT '',
            shipment_type TEXT NOT NULL,
            carrier_raw TEXT NOT NULL DEFAULT '',
            carrier_normalized TEXT NOT NULL DEFAULT '',
            waybill_no TEXT NOT NULL DEFAULT '',
            tracking_no TEXT NOT NULL DEFAULT '',
            final_tracking_no TEXT NOT NULL DEFAULT '',
            wms_outbound_order_no TEXT NOT NULL DEFAULT '',
            wms_status_code INTEGER,
            wms_status_name TEXT NOT NULL DEFAULT '',
            outbound_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            outbound_observed_at TEXT NOT NULL DEFAULT '',
            tracking_url TEXT NOT NULL DEFAULT '',
            customer_visible INTEGER NOT NULL DEFAULT 1,
            visibility_reason TEXT NOT NULL DEFAULT '',
            is_complete INTEGER NOT NULL,
            UNIQUE(notification_id, package_key)
        );

        CREATE TABLE IF NOT EXISTS shipment_notification_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL
                REFERENCES shipment_notifications(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            action TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'desktop_user',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_notification_reviews_notification
            ON shipment_notification_reviews(notification_id, created_at, id);

        CREATE TABLE IF NOT EXISTS shipment_notification_exclusions (
            platform_order_no TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            excluded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_notification_sync_state (
            platform_order_no TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            erp_completed_at TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            last_synced_at TEXT NOT NULL DEFAULT '',
            next_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_notification_sync_due
            ON shipment_notification_sync_state(state, next_attempt_at);

        CREATE TABLE IF NOT EXISTS shipment_notification_order_sources (
            platform_order_no TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL DEFAULT 'AMAZON_FULL_SCAN',
            system_order_nos_json TEXT NOT NULL DEFAULT '[]',
            purchased_at TEXT NOT NULL DEFAULT '',
            eligibility_reason TEXT NOT NULL DEFAULT '',
            baseline_pending INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_notification_sources_active
            ON shipment_notification_order_sources(active, last_seen_at);

        CREATE TABLE IF NOT EXISTS shipment_notification_package_events (
            platform_order_no TEXT NOT NULL,
            package_key TEXT NOT NULL,
            first_tracking_no TEXT NOT NULL DEFAULT '',
            last_tracking_no TEXT NOT NULL DEFAULT '',
            baseline_suppressed INTEGER NOT NULL DEFAULT 0,
            handled_notification_id INTEGER,
            first_completed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(platform_order_no, package_key)
        );
        CREATE INDEX IF NOT EXISTS idx_shipment_notification_events_pending
            ON shipment_notification_package_events(
                platform_order_no, baseline_suppressed, handled_notification_id
            );

        CREATE TABLE IF NOT EXISTS shipment_notification_runtime_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_notification_recipient_name_choices (
            platform_order_no TEXT PRIMARY KEY,
            selected_name TEXT NOT NULL,
            candidate_names_json TEXT NOT NULL DEFAULT '[]',
            selection_source TEXT NOT NULL DEFAULT 'USER',
            selected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_notification_scan_locks (
            lock_name TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shipment_notification_wc_baselines (
            platform_order_no TEXT PRIMARY KEY,
            baseline_completed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    contact_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(shipment_order_contacts)")
    }
    if "email_presence" not in contact_columns:
        conn.execute(
            "ALTER TABLE shipment_order_contacts "
            "ADD COLUMN email_presence TEXT NOT NULL DEFAULT 'UNKNOWN'"
        )
    for column in (
        "recipient_name_source",
        "email_source",
        "phone_source",
        "verified_phone_e164",
        "phone_verification_state",
        "contact_captured_at",
    ):
        if column not in contact_columns:
            conn.execute(
                f"ALTER TABLE shipment_order_contacts "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    conn.execute(
        "UPDATE shipment_order_contacts SET phone_verification_state = ? "
        "WHERE TRIM(COALESCE(phone_verification_state, '')) = ''",
        (PHONE_VERIFICATION_UNKNOWN,),
    )
    notification_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(shipment_notifications)")
    }
    for column in (
        "sales_platform_code",
        "sales_platform_name",
        "store_name",
        "site_name",
    ):
        if column not in notification_columns:
            conn.execute(
                f"ALTER TABLE shipment_notifications "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    if "email_presence" not in notification_columns:
        conn.execute(
            "ALTER TABLE shipment_notifications "
            "ADD COLUMN email_presence TEXT NOT NULL DEFAULT 'UNKNOWN'"
        )
    if "source_kind" not in notification_columns:
        conn.execute(
            "ALTER TABLE shipment_notifications "
            "ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'AUTO_ERP'"
        )
    if "body_html" not in notification_columns:
        conn.execute(
            "ALTER TABLE shipment_notifications "
            "ADD COLUMN body_html TEXT NOT NULL DEFAULT ''"
        )
    if "product_names_json" not in notification_columns:
        conn.execute(
            "ALTER TABLE shipment_notifications "
            "ADD COLUMN product_names_json TEXT NOT NULL DEFAULT '[]'"
        )
    for column in ("erp_completed_at", "state_changed_at"):
        if column not in notification_columns:
            conn.execute(
                f"ALTER TABLE shipment_notifications "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    for column, declaration in (
        ("provider_operator_email", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_next_check_at", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_last_checked_at", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_deadline_at", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_check_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("receipt_check_lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("receipt_check_lease_until", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in notification_columns:
            conn.execute(
                f"ALTER TABLE shipment_notifications ADD COLUMN {column} {declaration}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shipment_notifications_receipt_due "
        "ON shipment_notifications(state, receipt_next_check_at, id)"
    )
    conn.execute(
        "UPDATE shipment_notifications SET state_changed_at = updated_at "
        "WHERE state_changed_at = ''"
    )
    now = utc_now()
    conn.execute(
        """
        INSERT OR IGNORE INTO shipment_notification_runtime_state (
            state_key, state_value, updated_at
        ) VALUES ('wc_notification_cutover_at', ?, ?)
        """,
        (now, now),
    )
    item_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(shipment_notification_items)")
    }
    if "tracking_url" not in item_columns:
        conn.execute(
            "ALTER TABLE shipment_notification_items "
            "ADD COLUMN tracking_url TEXT NOT NULL DEFAULT ''"
        )
    for column, declaration in (
        ("customer_visible", "INTEGER NOT NULL DEFAULT 1"),
        ("visibility_reason", "TEXT NOT NULL DEFAULT ''"),
        ("wms_outbound_order_no", "TEXT NOT NULL DEFAULT ''"),
        ("wms_status_code", "INTEGER"),
        ("wms_status_name", "TEXT NOT NULL DEFAULT ''"),
        ("outbound_state", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("outbound_observed_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in item_columns:
            conn.execute(
                f"ALTER TABLE shipment_notification_items "
                f"ADD COLUMN {column} {declaration}"
            )
    package_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(shipment_package_snapshots)")
    }
    for column, declaration in (
        ("customer_visible", "INTEGER NOT NULL DEFAULT 1"),
        ("visibility_reason", "TEXT NOT NULL DEFAULT ''"),
        ("wms_outbound_order_no", "TEXT NOT NULL DEFAULT ''"),
        ("wms_status_code", "INTEGER"),
        ("wms_status_name", "TEXT NOT NULL DEFAULT ''"),
        ("outbound_state", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
        ("outbound_observed_at", "TEXT NOT NULL DEFAULT ''"),
    ):
        if column not in package_columns:
            conn.execute(
                f"ALTER TABLE shipment_package_snapshots "
                f"ADD COLUMN {column} {declaration}"
            )
    product_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(shipment_order_product_snapshots)")
    }
    if "source_sequence" not in product_columns:
        conn.execute(
            "ALTER TABLE shipment_order_product_snapshots "
            "ADD COLUMN source_sequence INTEGER NOT NULL DEFAULT 0"
        )
    _migrate_legacy_email_batches(conn)
    _suppress_unsent_revisions_after_confirmed_send(conn)
    _suppress_independent_site_unsent_notifications(conn)


def _migrate_legacy_email_batches(conn: sqlite3.Connection) -> None:
    names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if "shipment_email_batches" not in names:
        return
    now = utc_now()
    rows = conn.execute(
        """
        SELECT id, platform_order_no, sequence_no, state, recipient_email,
               message_id, template_version, content_hash, attempt_count,
               last_error, sent_at, created_at, updated_at
        FROM shipment_email_batches
        ORDER BY id
        """
    ).fetchall()
    state_map = {
        "PENDING": NOTIFICATION_AWAITING_REVIEW,
        "RETRYABLE": NOTIFICATION_RETRYABLE,
        "BLOCKED": NOTIFICATION_BLOCKED,
        "SENT": NOTIFICATION_ACCEPTED,
    }
    for row in rows:
        exists = conn.execute(
            "SELECT 1 FROM shipment_notifications WHERE legacy_email_batch_id = ?",
            (row[0],),
        ).fetchone()
        if exists:
            continue
        platform_order_no = str(row[1] or "")
        legacy_hash = str(row[7] or "") or hashlib.sha256(
            f"legacy-email-batch:{row[0]}".encode("utf-8")
        ).hexdigest()
        idempotency_key = f"legacy-email-batch-{row[0]}-{legacy_hash[:24]}"
        revision = -int(row[2] or row[0] or 1)
        conn.execute(
            """
            INSERT INTO shipment_notifications (
                platform_order_no, revision, channel, state, recipient_email,
                target, template_version, content_hash, idempotency_key,
                provider_message_id, legacy_email_batch_id, attempt_count,
                last_error, sent_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform_order_no,
                revision,
                CHANNEL_EMAIL,
                state_map.get(str(row[3] or ""), NOTIFICATION_BLOCKED),
                str(row[4] or ""),
                str(row[4] or ""),
                str(row[6] or "legacy-v1"),
                legacy_hash,
                idempotency_key,
                str(row[5] or ""),
                int(row[0]),
                int(row[8] or 0),
                str(row[9] or "") or None,
                row[10],
                str(row[11] or now),
                str(row[12] or now),
            ),
        )


def _suppress_unsent_revisions_after_confirmed_send(
    conn: sqlite3.Connection,
) -> None:
    """Close automatically regenerated drafts when the order was already sent.

    Older releases deliberately created supplemental drafts whenever logistics
    data changed after delivery.  That can place an already completed order
    back into the send queue.  Preserve the row and audit trail, but make the
    automatically regenerated duplicate non-sendable.
    """

    now = utc_now()
    rows = conn.execute(
        """
        SELECT current.*
        FROM shipment_notifications AS current
        WHERE current.legacy_email_batch_id IS NULL
          AND current.state IN (?, ?, ?, ?, ?, ?)
          AND current.id = (
              SELECT MAX(latest.id)
              FROM shipment_notifications AS latest
              WHERE latest.platform_order_no = current.platform_order_no
                AND latest.legacy_email_batch_id IS NULL
          )
          AND NOT EXISTS (
              SELECT 1
              FROM shipment_notification_reviews AS review
              WHERE review.notification_id = current.id
                AND review.action = 'MANUAL_REOPEN'
          )
          AND EXISTS (
              SELECT 1
              FROM shipment_notifications AS sent
              WHERE sent.platform_order_no = current.platform_order_no
                AND sent.id <> current.id
                AND (
                    sent.state IN (?, ?)
                    OR TRIM(COALESCE(sent.provider_message_id, '')) <> ''
                    OR TRIM(COALESCE(sent.sent_at, '')) <> ''
                )
          )
        """,
        (
            NOTIFICATION_AWAITING_REVIEW,
            NOTIFICATION_BLOCKED,
            NOTIFICATION_WAITING_CONTACT,
            NOTIFICATION_REJECTED,
            NOTIFICATION_RETRYABLE,
            NOTIFICATION_FAILED,
            NOTIFICATION_ACCEPTED,
            NOTIFICATION_DELIVERED,
        ),
    ).fetchall()
    for row in rows:
        prior_sent = conn.execute(
            """
            SELECT *
            FROM shipment_notifications
            WHERE platform_order_no = ?
              AND id <> ?
              AND (
                  state IN (?, ?)
                  OR TRIM(COALESCE(provider_message_id, '')) <> ''
                  OR TRIM(COALESCE(sent_at, '')) <> ''
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                str(row["platform_order_no"]),
                int(row["id"]),
                NOTIFICATION_ACCEPTED,
                NOTIFICATION_DELIVERED,
            ),
        ).fetchone()
        if (
            prior_sent is not None
            and _is_legitimate_supplement_revision_conn(conn, prior_sent, row)
        ):
            continue
        notification_id = int(row["id"])
        changed = conn.execute(
            """
            UPDATE shipment_notifications
            SET state = ?, provider_status = 'PREVIOUSLY_SENT',
                last_error = NULL, state_changed_at = ?, updated_at = ?
            WHERE id = ? AND state <> ?
            """,
            (
                NOTIFICATION_SUPPRESSED,
                now,
                now,
                notification_id,
                NOTIFICATION_SUPPRESSED,
            ),
        ).rowcount
        if not changed:
            continue
        conn.execute(
            """
            INSERT INTO shipment_notification_reviews (
                notification_id, revision, action, content_hash, actor, note, created_at
            ) VALUES (?, ?, 'AUTO_SUPPRESS_ALREADY_SENT', ?, 'system',
                      'Earlier notification already sent; automatic resend suppressed.', ?)
            """,
            (
                notification_id,
                int(row["revision"]),
                str(row["content_hash"] or ""),
                now,
            ),
        )


def _suppress_independent_site_unsent_notifications(
    conn: sqlite3.Connection,
) -> None:
    """Keep WC history while making every never-sent draft permanently non-sendable."""

    now = utc_now()
    rows = conn.execute(
        """
        SELECT id, platform_order_no, revision, content_hash
        FROM shipment_notifications
        WHERE legacy_email_batch_id IS NULL
          AND TRIM(COALESCE(provider_message_id, '')) = ''
          AND TRIM(COALESCE(sent_at, '')) = ''
          AND (
              state <> ?
              OR COALESCE(last_error, '') <> 'independent_site_customer_notification_disabled'
          )
        """,
        (NOTIFICATION_SUPPRESSED,),
    ).fetchall()
    for row in rows:
        if not INDEPENDENT_SITE_ORDER_RE.fullmatch(
            str(row["platform_order_no"] or "").strip()
        ):
            continue
        changed = conn.execute(
            """
            UPDATE shipment_notifications
            SET state = ?, provider_status = 'POLICY_SUPPRESSED',
                last_error = 'independent_site_customer_notification_disabled',
                receipt_next_check_at = '', receipt_check_lease_owner = '',
                receipt_check_lease_until = '', state_changed_at = ?, updated_at = ?
            WHERE id = ? AND TRIM(COALESCE(provider_message_id, '')) = ''
              AND TRIM(COALESCE(sent_at, '')) = ''
            """,
            (NOTIFICATION_SUPPRESSED, now, now, int(row["id"])),
        ).rowcount
        if changed:
            conn.execute(
                """
                INSERT INTO shipment_notification_reviews (
                    notification_id, revision, action, content_hash, actor, note, created_at
                ) VALUES (?, ?, 'AUTO_SUPPRESS_INDEPENDENT_SITE', ?, 'system',
                          'Independent-site customer notifications are disabled.', ?)
                """,
                (
                    int(row["id"]),
                    int(row["revision"]),
                    str(row["content_hash"] or ""),
                    now,
                ),
            )


def _completed_package_identities_conn(
    conn: sqlite3.Connection,
    notification_id: int,
) -> set[tuple[str, str]]:
    return {
        (
            str(row["package_key"] or ""),
            str(row["final_tracking_no"] or ""),
        )
        for row in conn.execute(
            """
            SELECT package_key, final_tracking_no
            FROM shipment_notification_items
            WHERE notification_id = ?
              AND customer_visible = 1
              AND is_complete = 1
              AND TRIM(COALESCE(final_tracking_no, '')) <> ''
            """,
            (notification_id,),
        ).fetchall()
    }


def _is_legitimate_supplement_revision_conn(
    conn: sqlite3.Connection,
    prior_sent: sqlite3.Row,
    current: sqlite3.Row,
) -> bool:
    """Return true only when a partial send gained a genuinely new package."""

    prior_items = _completed_package_identities_conn(conn, int(prior_sent["id"]))
    current_items = _completed_package_identities_conn(conn, int(current["id"]))
    prior_keys = {package_key for package_key, _tracking in prior_items}
    current_keys = {package_key for package_key, _tracking in current_items}
    return bool(current_keys - prior_keys)


def _has_newly_completed_package_conn(
    conn: sqlite3.Connection,
    prior_sent: sqlite3.Row,
    packages: Sequence[PackageSnapshot],
    *,
    package_complete: int,
    package_missing: int,
) -> bool:
    del package_complete, package_missing
    prior_items = _completed_package_identities_conn(conn, int(prior_sent["id"]))
    prior_keys = {package_key for package_key, _tracking in prior_items}
    current_keys = {
        str(item.package_key or "")
        for item in packages
        if item.customer_visible and item.complete and item.final_tracking_no
    }
    return bool(current_keys - prior_keys)


class ShipmentNotificationStore:
    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 15.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)
        self._initialized = False
        self._initialize_lock = RLock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        busy_timeout_ms = max(1, round(self.timeout_seconds * 1000))
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        return conn

    def try_acquire_scan_lock(
        self,
        owner: str,
        *,
        lease_seconds: int = 7200,
    ) -> bool:
        """Claim the one notification scan lease shared by every scan source."""

        self.initialize()
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("notification scan lock owner is required")
        if lease_seconds <= 0:
            raise ValueError("notification scan lease_seconds must be positive")
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        expires_at = (
            now_dt + timedelta(seconds=int(lease_seconds))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT owner, expires_at FROM shipment_notification_scan_locks "
                "WHERE lock_name = 'customer_notification_scan'"
            ).fetchone()
            if (
                current is not None
                and str(current["owner"] or "") != normalized_owner
                and str(current["expires_at"] or "") > now
            ):
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO shipment_notification_scan_locks (
                    lock_name, owner, expires_at, updated_at
                ) VALUES ('customer_notification_scan', ?, ?, ?)
                ON CONFLICT(lock_name) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized_owner, expires_at, now),
            )
            conn.commit()
        return True

    def release_scan_lock(self, owner: str) -> bool:
        self.initialize()
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            return False
        with self.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM shipment_notification_scan_locks "
                "WHERE lock_name = 'customer_notification_scan' AND owner = ?",
                (normalized_owner,),
            ).rowcount
            conn.commit()
        return deleted == 1

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            with self.connect() as conn:
                initialize_notification_schema(conn)
                conn.commit()
            self._initialized = True

    def notification_scan_targets(
        self,
        platform_order_nos: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the union of automated ERP and Amazon full-scan targets."""

        self.initialize()
        with self.connect() as conn:
            auto_rows = conn.execute(
                """
                SELECT j.platform_order_no,
                       COUNT(*) AS queue_total,
                       SUM(CASE WHEN j.identity_state = 'ACTIVE'
                                      AND e.state = 'DONE'
                                      AND e.checkpoint = 'OUTBOUNDED'
                                      AND e.completion_source = ?
                                THEN 1 ELSE 0 END) AS queue_complete,
                       GROUP_CONCAT(DISTINCT j.system_order_no) AS system_order_nos,
                       MAX(COALESCE(e.outbounded_at, e.updated_at))
                           AS erp_completed_at,
                       s.state AS sync_state,
                       s.erp_completed_at AS sync_erp_completed_at,
                       s.attempt_count AS sync_attempt_count,
                       s.next_attempt_at AS sync_next_attempt_at,
                       wc.baseline_completed_at AS wc_baseline_completed_at
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                LEFT JOIN shipment_notification_exclusions x
                       ON x.platform_order_no = j.platform_order_no
                LEFT JOIN shipment_notification_sync_state s
                       ON s.platform_order_no = j.platform_order_no
                LEFT JOIN shipment_notification_wc_baselines wc
                       ON wc.platform_order_no = j.platform_order_no
                WHERE j.identity_state <> 'CANCELLED'
                  AND x.platform_order_no IS NULL
                GROUP BY j.platform_order_no
                HAVING COUNT(*) > 0
                   AND SUM(
                       CASE WHEN j.identity_state = 'ACTIVE'
                                  AND e.state = 'DONE'
                                  AND e.checkpoint = 'OUTBOUNDED'
                                  AND e.completion_source = ?
                            THEN 1 ELSE 0 END
                   ) > 0
                ORDER BY j.platform_order_no
                """,
                (ERP_COMPLETION_AUTOMATION, ERP_COMPLETION_AUTOMATION),
            ).fetchall()
            source_rows = conn.execute(
                """
                SELECT src.*, s.state AS sync_state,
                       s.attempt_count AS sync_attempt_count,
                       s.next_attempt_at AS sync_next_attempt_at
                FROM shipment_notification_order_sources src
                LEFT JOIN shipment_notification_exclusions x
                       ON x.platform_order_no = src.platform_order_no
                LEFT JOIN shipment_notification_sync_state s
                       ON s.platform_order_no = src.platform_order_no
                WHERE src.active = 1 AND x.platform_order_no IS NULL
                ORDER BY src.platform_order_no
                """
            ).fetchall()
            cutover_row = conn.execute(
                "SELECT state_value FROM shipment_notification_runtime_state "
                "WHERE state_key = 'wc_notification_cutover_at'"
            ).fetchone()
            wc_cutover_at = str(cutover_row[0] or "") if cutover_row else ""
        current_time = utc_now()
        explicit_request = platform_order_nos is not None
        targets_by_platform: dict[str, dict[str, Any]] = {}
        for row in auto_rows:
            platform_order_no = str(row[0] or "").strip()
            if INDEPENDENT_SITE_ORDER_RE.fullmatch(platform_order_no):
                continue
            erp_completed_at = str(row[4] or "").strip()
            historical_wc = bool(
                INDEPENDENT_SITE_ORDER_RE.fullmatch(platform_order_no)
                and wc_cutover_at
                and erp_completed_at < wc_cutover_at
            )
            wc_baseline_pending = bool(historical_wc and not str(row[9] or "").strip())
            sync_state = str(row[5] or "").strip().upper()
            sync_erp_completed_at = str(row[6] or "").strip()
            sync_next_attempt_at = str(row[8] or "").strip()
            completion_changed = sync_erp_completed_at != erp_completed_at
            if (
                not explicit_request
                and
                not completion_changed
                and sync_state == NOTIFICATION_SYNC_RETRYABLE
                and sync_next_attempt_at
                and sync_next_attempt_at > current_time
            ):
                continue
            targets_by_platform[platform_order_no] = {
                "platform_order_no": str(row[0]),
                "source_kind": "AUTO_ERP",
                "queue_total": int(row[1]),
                "queue_complete": int(row[2]),
                "system_order_nos": tuple(
                    value for value in str(row[3] or "").split(",") if value
                ),
                "auto_system_order_nos": tuple(
                    value for value in str(row[3] or "").split(",") if value
                ),
                "erp_completed_at": erp_completed_at,
                "sync_state": sync_state,
                "sync_attempt_count": int(row[7] or 0),
                "baseline_pending": wc_baseline_pending,
            }
        for row in source_rows:
            platform_order_no = str(row["platform_order_no"] or "").strip()
            if not platform_order_no or INDEPENDENT_SITE_ORDER_RE.fullmatch(
                platform_order_no
            ):
                continue
            sync_state = str(row["sync_state"] or "").strip().upper()
            sync_next_attempt_at = str(row["sync_next_attempt_at"] or "").strip()
            if (
                not explicit_request
                and
                sync_state == NOTIFICATION_SYNC_RETRYABLE
                and sync_next_attempt_at
                and sync_next_attempt_at > current_time
            ):
                continue
            try:
                source_systems = tuple(
                    str(value).strip()
                    for value in json.loads(row["system_order_nos_json"] or "[]")
                    if str(value).strip()
                )
            except (TypeError, json.JSONDecodeError):
                source_systems = ()
            existing = targets_by_platform.get(platform_order_no)
            if existing is None:
                targets_by_platform[platform_order_no] = {
                    "platform_order_no": platform_order_no,
                    "source_kind": "AMAZON_FULL_SCAN",
                    "queue_total": 0,
                    "queue_complete": 0,
                    "system_order_nos": source_systems,
                    "auto_system_order_nos": (),
                    "erp_completed_at": str(row["purchased_at"] or ""),
                    "sync_state": sync_state,
                    "sync_attempt_count": int(row["sync_attempt_count"] or 0),
                    "baseline_pending": bool(row["baseline_pending"]),
                }
            else:
                existing["source_kind"] = "AUTO_ERP+AMAZON_FULL_SCAN"
                existing["system_order_nos"] = tuple(
                    dict.fromkeys([*existing["system_order_nos"], *source_systems])
                )
                existing["baseline_pending"] = bool(row["baseline_pending"])
        targets = [targets_by_platform[key] for key in sorted(targets_by_platform)]
        if platform_order_nos is None:
            return targets
        requested = {
            str(value).strip() for value in platform_order_nos if str(value).strip()
        }
        return [
            target
            for target in targets
            if str(target["platform_order_no"]) in requested
        ]

    def merge_full_scan_sources(
        self,
        orders: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Replace the active Amazon discovery set after a complete list scan."""

        self.initialize()
        now = utc_now()
        normalized: dict[str, dict[str, Any]] = {}
        for order in orders:
            platform = str(order.get("platform_order_no") or "").strip()
            if not platform or INDEPENDENT_SITE_ORDER_RE.fullmatch(platform):
                continue
            systems = tuple(
                dict.fromkeys(
                    str(value or "").strip()
                    for value in order.get("system_order_nos") or ()
                    if str(value or "").strip()
                )
            )
            if not systems:
                continue
            normalized[platform] = {
                "systems": systems,
                "purchased_at": str(order.get("purchased_at") or "").strip(),
                "eligibility_reason": str(
                    order.get("eligibility_reason") or ""
                ).strip(),
            }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bootstrap = conn.execute(
                "SELECT state_value FROM shipment_notification_runtime_state "
                "WHERE state_key = 'amazon_full_scan_bootstrapped_at'"
            ).fetchone()
            first_complete_scan = bootstrap is None or not str(bootstrap[0] or "").strip()
            conn.execute(
                "UPDATE shipment_notification_order_sources SET active = 0, updated_at = ?",
                (now,),
            )
            inserted = 0
            updated = 0
            for platform, values in normalized.items():
                previous = conn.execute(
                    "SELECT * FROM shipment_notification_order_sources "
                    "WHERE platform_order_no = ?",
                    (platform,),
                ).fetchone()
                baseline_pending = (
                    1
                    if first_complete_scan
                    else int(previous["baseline_pending"] or 0) if previous else 0
                )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_order_sources (
                        platform_order_no, source_kind, system_order_nos_json,
                        purchased_at, eligibility_reason, baseline_pending,
                        active, first_seen_at, last_seen_at, updated_at
                    ) VALUES (?, 'AMAZON_FULL_SCAN', ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(platform_order_no) DO UPDATE SET
                        system_order_nos_json = excluded.system_order_nos_json,
                        purchased_at = excluded.purchased_at,
                        eligibility_reason = excluded.eligibility_reason,
                        baseline_pending = excluded.baseline_pending,
                        active = 1,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        platform,
                        json.dumps(values["systems"], ensure_ascii=False),
                        values["purchased_at"],
                        values["eligibility_reason"],
                        baseline_pending,
                        str(previous["first_seen_at"] or now) if previous else now,
                        now,
                        now,
                    ),
                )
                inserted += int(previous is None)
                updated += int(previous is not None)
            if first_complete_scan:
                conn.execute(
                    """
                    INSERT INTO shipment_notification_runtime_state (
                        state_key, state_value, updated_at
                    ) VALUES ('amazon_full_scan_bootstrapped_at', ?, ?)
                    ON CONFLICT(state_key) DO UPDATE SET
                        state_value = excluded.state_value,
                        updated_at = excluded.updated_at
                    """,
                    (now, now),
                )
            conn.commit()
        return {
            "discovered_order_count": len(normalized),
            "source_inserted_count": inserted,
            "source_updated_count": updated,
            "bootstrap_order_count": len(normalized) if first_complete_scan else 0,
        }

    def complete_full_scan_baseline(self, platform_order_no: str) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "UPDATE shipment_notification_order_sources "
                "SET baseline_pending = 0, updated_at = ? WHERE platform_order_no = ?",
                (utc_now(), str(platform_order_no or "").strip()),
            )
            conn.commit()

    def record_notification_sync_success(
        self,
        platform_order_no: str,
        *,
        erp_completed_at: str,
    ) -> int:
        return self._record_notification_sync_result(
            platform_order_no,
            erp_completed_at=erp_completed_at,
            succeeded=True,
            error="",
        )

    def record_notification_sync_retry(
        self,
        platform_order_no: str,
        *,
        erp_completed_at: str,
        error: str,
    ) -> int:
        return self._record_notification_sync_result(
            platform_order_no,
            erp_completed_at=erp_completed_at,
            succeeded=False,
            error=error,
        )

    def _record_notification_sync_result(
        self,
        platform_order_no: str,
        *,
        erp_completed_at: str,
        succeeded: bool,
        error: str,
    ) -> int:
        self.initialize()
        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        completed_at = str(erp_completed_at or "").strip()
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute(
                """
                SELECT state, erp_completed_at, attempt_count, created_at
                FROM shipment_notification_sync_state
                WHERE platform_order_no = ?
                """,
                (platform,),
            ).fetchone()
            same_completion = bool(
                previous is not None
                and str(previous["erp_completed_at"] or "") == completed_at
            )
            attempt_count = (
                0
                if succeeded
                else (
                    int(previous["attempt_count"] or 0) + 1
                    if same_completion
                    else 1
                )
            )
            next_attempt_at = None
            if not succeeded:
                delay_seconds = min(
                    NOTIFICATION_SYNC_RETRY_BASE_SECONDS
                    * (2 ** max(0, attempt_count - 1)),
                    NOTIFICATION_SYNC_RETRY_MAX_SECONDS,
                )
                next_attempt_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                """
                INSERT INTO shipment_notification_sync_state (
                    platform_order_no, state, erp_completed_at, attempt_count,
                    last_error, last_synced_at, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_order_no) DO UPDATE SET
                    state = excluded.state,
                    erp_completed_at = excluded.erp_completed_at,
                    attempt_count = excluded.attempt_count,
                    last_error = excluded.last_error,
                    last_synced_at = excluded.last_synced_at,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    (
                        NOTIFICATION_SYNC_SYNCED
                        if succeeded
                        else NOTIFICATION_SYNC_RETRYABLE
                    ),
                    completed_at,
                    attempt_count,
                    str(error or "").strip()[:500],
                    now if succeeded else "",
                    next_attempt_at,
                    (
                        str(previous["created_at"])
                        if previous is not None
                        else now
                    ),
                    now,
                ),
            )
            conn.commit()
        return attempt_count

    def upsert_contact(
        self,
        contact: OrderContact,
        *,
        replace_system_order_nos: bool = False,
    ) -> bool:
        self.initialize()
        platform_order_no = contact.platform_order_no.strip()
        if not platform_order_no:
            raise ValueError("platform_order_no is required")
        now = utc_now()
        captured_at = contact.captured_at.strip() or now
        phone_e164 = normalize_phone(contact.phone_raw) or ""
        system_orders = tuple(
            dict.fromkeys(value.strip() for value in contact.system_order_nos if value.strip())
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM shipment_order_contacts WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            verification_state = str(
                contact.phone_verification_state or PHONE_VERIFICATION_UNKNOWN
            ).strip().upper()
            verified_phone_e164 = normalize_phone(contact.verified_phone_e164) or ""
            if previous is not None and verification_state == PHONE_VERIFICATION_UNKNOWN:
                verification_state = str(
                    previous["phone_verification_state"] or PHONE_VERIFICATION_UNKNOWN
                ).strip().upper()
                verified_phone_e164 = str(previous["verified_phone_e164"] or "")
            if previous is not None and not replace_system_order_nos:
                previous_orders = json.loads(previous["system_order_nos_json"] or "[]")
                system_orders = tuple(dict.fromkeys([*previous_orders, *system_orders]))
            values = (
                contact.recipient_name.strip(),
                contact.email.strip(),
                str(contact.email_presence or EMAIL_PRESENCE_UNKNOWN).strip().upper(),
                contact.phone_raw.strip(),
                phone_e164,
                contact.sales_platform_code.strip(),
                contact.sales_platform_name.strip(),
                contact.store_name.strip(),
                contact.site_name.strip(),
                contact.source.strip(),
                contact.recipient_name_source.strip(),
                contact.email_source.strip(),
                contact.phone_source.strip(),
                verified_phone_e164,
                verification_state,
                captured_at,
                json.dumps(system_orders, ensure_ascii=False),
            )
            if previous is not None:
                old_values = tuple(
                    str(previous[key] or "")
                    for key in (
                        "recipient_name",
                        "email",
                        "email_presence",
                        "phone_raw",
                        "phone_e164",
                        "sales_platform_code",
                        "sales_platform_name",
                        "store_name",
                        "site_name",
                        "contact_source",
                        "recipient_name_source",
                        "email_source",
                        "phone_source",
                        "verified_phone_e164",
                        "phone_verification_state",
                        "contact_captured_at",
                        "system_order_nos_json",
                    )
                )
                new_values = tuple(str(value) for value in values)
                if old_values == new_values:
                    conn.rollback()
                    return False
                conn.execute(
                    """
                    UPDATE shipment_order_contacts
                    SET recipient_name = ?, email = ?, email_presence = ?,
                        phone_raw = ?, phone_e164 = ?,
                        sales_platform_code = ?, sales_platform_name = ?, store_name = ?,
                        site_name = ?, contact_source = ?, recipient_name_source = ?,
                        email_source = ?, phone_source = ?, verified_phone_e164 = ?,
                        phone_verification_state = ?, contact_captured_at = ?,
                        system_order_nos_json = ?,
                        contact_updated_at = ?, updated_at = ?
                    WHERE platform_order_no = ?
                    """,
                    (*values, now, now, platform_order_no),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO shipment_order_contacts (
                        platform_order_no, recipient_name, email, email_presence,
                        phone_raw, phone_e164,
                        sales_platform_code, sales_platform_name, store_name, site_name,
                        contact_source, recipient_name_source, email_source, phone_source,
                        verified_phone_e164, phone_verification_state,
                        contact_captured_at, system_order_nos_json, contact_updated_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (platform_order_no, *values, now, now, now),
                )
            conn.commit()
        return True

    def upsert_wms_recipient_name(
        self,
        platform_order_no: str,
        recipient_name: str,
        *,
        system_order_nos: Sequence[str] = (),
        preserve_existing_when_empty: bool = True,
    ) -> bool:
        """Update only the WMS-authoritative recipient name.

        E-mail and phone are copied from the existing JSON snapshot verbatim;
        no other WMS response field is allowed into notification contacts.
        """

        platform = platform_order_no.strip()
        name = normalize_recipient_name(recipient_name)
        existing = self.get_contact(platform)
        if (
            not name
            and preserve_existing_when_empty
            and existing is not None
            and existing.recipient_name_source == CONTACT_SOURCE_WMS
        ):
            # A partial WMS response may omit every not-yet-outbound sibling.
            # Do not erase a recipient name that was already established by a
            # previous authoritative WMS package snapshot.
            name = existing.recipient_name
        previous_orders = existing.system_order_nos if existing is not None else ()
        incoming_orders = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in system_order_nos
                if str(value or "").strip()
            )
        )
        orders = incoming_orders or previous_orders
        return self.upsert_contact(
            OrderContact(
                platform_order_no=platform,
                recipient_name=name,
                email=existing.email if existing is not None else "",
                email_presence=(
                    existing.email_presence
                    if existing is not None
                    else EMAIL_PRESENCE_UNKNOWN
                ),
                phone_raw=existing.phone_raw if existing is not None else "",
                sales_platform_code=(
                    existing.sales_platform_code if existing is not None else ""
                ),
                sales_platform_name=(
                    existing.sales_platform_name if existing is not None else ""
                ),
                store_name=existing.store_name if existing is not None else "",
                site_name=existing.site_name if existing is not None else "",
                source=(
                    existing.source
                    if existing is not None
                    and existing.source == CONTACT_SOURCE_CUSTOMIZATION_JSON
                    else CONTACT_SOURCE_WMS
                ),
                recipient_name_source=CONTACT_SOURCE_WMS,
                email_source=existing.email_source if existing is not None else "",
                phone_source=existing.phone_source if existing is not None else "",
                captured_at=existing.captured_at if existing is not None else "",
                system_order_nos=orders,
            ),
            replace_system_order_nos=True,
        )

    def remember_recipient_name_choice(
        self,
        platform_order_no: str,
        selected_name: str,
        candidate_names: Sequence[str],
        *,
        source: str = "USER",
    ) -> str:
        """Persist one explicit conflict decision independently of WMS refreshes."""

        self.initialize()
        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        candidates = _recipient_name_candidates(candidate_names)
        selected_key = _recipient_name_key(selected_name)
        selected = next(
            (
                candidate
                for candidate in candidates
                if _recipient_name_key(candidate) == selected_key
            ),
            "",
        )
        if not selected:
            raise ValueError("selected recipient name is not a current candidate")
        normalized_source = str(source or "USER").strip().upper()[:40] or "USER"
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT selected_name, selected_at "
                "FROM shipment_notification_recipient_name_choices "
                "WHERE platform_order_no = ?",
                (platform,),
            ).fetchone()
            selected_at = (
                str(previous["selected_at"] or now)
                if previous is not None
                and _recipient_name_key(str(previous["selected_name"] or ""))
                == _recipient_name_key(selected)
                else now
            )
            conn.execute(
                """
                INSERT INTO shipment_notification_recipient_name_choices (
                    platform_order_no, selected_name, candidate_names_json,
                    selection_source, selected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform_order_no) DO UPDATE SET
                    selected_name = excluded.selected_name,
                    candidate_names_json = excluded.candidate_names_json,
                    selection_source = excluded.selection_source,
                    selected_at = excluded.selected_at,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    selected,
                    json.dumps(candidates, ensure_ascii=False),
                    normalized_source,
                    selected_at,
                    now,
                ),
            )
            conn.commit()
        return selected

    def remembered_recipient_name_choice(
        self,
        platform_order_no: str,
        candidate_names: Sequence[str],
    ) -> str:
        """Return the current candidate matching a durable or legacy decision."""

        self.initialize()
        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        candidates = _recipient_name_candidates(candidate_names)
        if not candidates:
            return ""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT selected_name "
                "FROM shipment_notification_recipient_name_choices "
                "WHERE platform_order_no = ?",
                (platform,),
            ).fetchone()
            legacy_notification_names = (
                ()
                if row is not None
                else tuple(
                    str(history_row["recipient_name"] or "")
                    for history_row in conn.execute(
                        "SELECT recipient_name FROM shipment_notifications "
                        "WHERE platform_order_no = ? AND recipient_name <> '' "
                        "ORDER BY id DESC",
                        (platform,),
                    ).fetchall()
                )
            )
        selected_key = _recipient_name_key(
            str(row["selected_name"] or "") if row is not None else ""
        )
        match = next(
            (
                candidate
                for candidate in candidates
                if _recipient_name_key(candidate) == selected_key
            ),
            "",
        )
        if match:
            return match
        if row is not None:
            # A durable user decision exists but is no longer among the live
            # candidates. Do not silently replace it with a contact value that
            # may have come from a later one-name/partial WMS observation.
            return ""

        # A pre-v15 user decision also survives in the generated notification.
        # Prefer this immutable history over the current contact: a later
        # one-name/partial WMS observation may have overwritten the contact.
        for legacy_name in legacy_notification_names:
            legacy_key = _recipient_name_key(legacy_name)
            legacy_match = next(
                (
                    candidate
                    for candidate in candidates
                    if _recipient_name_key(candidate) == legacy_key
                ),
                "",
            )
            if legacy_match:
                return self.remember_recipient_name_choice(
                    platform,
                    legacy_match,
                    candidates,
                    source="LEGACY_NOTIFICATION",
                )

        # Releases before the choice table persisted a user's decision in the
        # WMS-authoritative contact row. Import it lazily when it still matches
        # a live candidate so already reviewed production orders do not prompt
        # again after upgrading.
        existing = self.get_contact(platform)
        if (
            existing is None
            or existing.recipient_name_source != CONTACT_SOURCE_WMS
        ):
            return ""
        legacy_key = _recipient_name_key(existing.recipient_name)
        legacy_match = next(
            (
                candidate
                for candidate in candidates
                if _recipient_name_key(candidate) == legacy_key
            ),
            "",
        )
        if not legacy_match:
            return ""
        return self.remember_recipient_name_choice(
            platform,
            legacy_match,
            candidates,
            source="LEGACY_CONTACT",
        )

    def upsert_customization_contact(
        self,
        platform_order_no: str,
        *,
        email: str = "",
        phone: str = "",
        system_order_nos: Sequence[str] = (),
    ) -> bool:
        """Persist JSON destinations unless a desktop user overrode a field."""

        platform = str(platform_order_no or "").strip()
        existing = self.get_contact(platform)
        previous_orders = existing.system_order_nos if existing is not None else ()
        orders = tuple(dict.fromkeys([*previous_orders, *system_order_nos]))
        normalized_email = normalize_email(email) or ""
        raw_phone = str(phone or "").strip()
        verified_phone = normalize_phone(raw_phone) or ""
        normalized_phone = raw_phone if verified_phone else ""
        manual_email = bool(
            existing is not None
            and existing.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
        )
        manual_phone = bool(
            existing is not None
            and existing.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL
        )
        next_email = existing.email if manual_email else normalized_email
        next_phone = existing.phone_raw if manual_phone else normalized_phone
        recipient_name = (
            existing.recipient_name
            if existing is not None
            and existing.recipient_name_source == CONTACT_SOURCE_WMS
            else ""
        )
        captured_at = (
            existing.captured_at
            if existing is not None
            and existing.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
            and existing.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
            else ""
        )
        return self.upsert_contact(
            OrderContact(
                platform_order_no=platform,
                recipient_name=recipient_name,
                email=next_email,
                email_presence=(
                    EMAIL_PRESENCE_PROVIDED
                    if next_email
                    else EMAIL_PRESENCE_NOT_PROVIDED
                ),
                phone_raw=next_phone,
                sales_platform_code=(
                    existing.sales_platform_code if existing is not None else ""
                ),
                sales_platform_name=(
                    existing.sales_platform_name if existing is not None else ""
                ),
                store_name=existing.store_name if existing is not None else "",
                site_name=existing.site_name if existing is not None else "",
                source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                recipient_name_source=CONTACT_SOURCE_WMS if recipient_name else "",
                email_source=(
                    CONTACT_SOURCE_DESKTOP_MANUAL
                    if manual_email
                    else CONTACT_SOURCE_CUSTOMIZATION_JSON
                ),
                phone_source=(
                    CONTACT_SOURCE_DESKTOP_MANUAL
                    if manual_phone
                    else CONTACT_SOURCE_CUSTOMIZATION_JSON
                ),
                verified_phone_e164=(
                    existing.verified_phone_e164
                    if manual_phone and existing is not None
                    else verified_phone
                ),
                phone_verification_state=(
                    existing.phone_verification_state
                    if manual_phone and existing is not None
                    else (
                        PHONE_VERIFICATION_MATCHED
                        if normalized_phone
                        else PHONE_VERIFICATION_MISSING
                    )
                ),
                captured_at=captured_at,
                system_order_nos=orders,
            )
        )

    def set_customization_phone_verification(
        self,
        platform_order_no: str,
        *,
        matched_phone: str = "",
        state: str = PHONE_VERIFICATION_MISSING,
    ) -> bool:
        """Persist current JSON evidence without trusting writeback provenance."""

        platform = str(platform_order_no or "").strip()
        existing = self.get_contact(platform)
        if (
            existing is not None
            and existing.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL
        ):
            return False
        if existing is None:
            existing = OrderContact(platform_order_no=platform)
        normalized = normalize_phone(matched_phone) or ""
        normalized_state = str(state or PHONE_VERIFICATION_MISSING).strip().upper()
        if normalized_state == PHONE_VERIFICATION_MATCHED and not normalized:
            normalized_state = PHONE_VERIFICATION_MISSING
        return self.upsert_contact(
            replace(
                existing,
                verified_phone_e164=normalized,
                phone_verification_state=normalized_state,
            )
        )

    def upsert_lingxing_detail_contact(
        self,
        platform_order_no: str,
        *,
        email: str | None = None,
        phone: str | None = None,
    ) -> bool:
        """Persist fields obtained by an explicit desktop detail refresh.

        ``None`` means the field was not obtained reliably and must not erase
        a previously trusted value. Recipient name remains WMS-owned.
        """

        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        existing = self.get_contact(platform)
        normalized_email = normalize_email(email) if email is not None else None
        normalized_phone = normalize_phone(phone) if phone is not None else None
        manual_email = bool(
            existing is not None
            and existing.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
        )
        manual_phone = bool(
            existing is not None
            and existing.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL
        )
        next_email = (
            existing.email
            if manual_email
            else (
                normalized_email
                if normalized_email is not None
                else (existing.email if existing is not None else "")
            )
        )
        next_phone = (
            existing.phone_raw
            if manual_phone
            else (
                normalized_phone
                if normalized_phone is not None
                else (existing.phone_raw if existing is not None else "")
            )
        )
        email_source = (
            CONTACT_SOURCE_DESKTOP_MANUAL
            if manual_email
            else (
                CONTACT_SOURCE_LINGXING_DETAIL_REFRESH
                if normalized_email is not None
                else (existing.email_source if existing is not None else "")
            )
        )
        phone_source = (
            CONTACT_SOURCE_DESKTOP_MANUAL
            if manual_phone
            else (
                CONTACT_SOURCE_LINGXING_DETAIL_REFRESH
                if normalized_phone is not None
                else (existing.phone_source if existing is not None else "")
            )
        )
        values_unchanged = bool(
            existing is not None
            and existing.email == next_email
            and existing.phone_raw == next_phone
            and existing.email_source == email_source
            and existing.phone_source == phone_source
        )
        return self.upsert_contact(
            OrderContact(
                platform_order_no=platform,
                recipient_name=existing.recipient_name if existing is not None else "",
                email=next_email,
                email_presence=(
                    EMAIL_PRESENCE_PROVIDED
                    if next_email
                    else (
                        existing.email_presence
                        if existing is not None
                        else EMAIL_PRESENCE_UNKNOWN
                    )
                ),
                phone_raw=next_phone,
                sales_platform_code=(
                    existing.sales_platform_code if existing is not None else ""
                ),
                sales_platform_name=(
                    existing.sales_platform_name if existing is not None else ""
                ),
                store_name=existing.store_name if existing is not None else "",
                site_name=existing.site_name if existing is not None else "",
                source=CONTACT_SOURCE_LINGXING_DETAIL_REFRESH,
                recipient_name_source=(
                    existing.recipient_name_source if existing is not None else ""
                ),
                email_source=email_source,
                phone_source=phone_source,
                captured_at=(
                    existing.captured_at
                    if existing is not None and values_unchanged
                    else ""
                ),
                system_order_nos=(
                    existing.system_order_nos if existing is not None else ()
                ),
            )
        )

    def upsert_lingxing_api_contact(
        self,
        platform_order_no: str,
        *,
        email: str | None = None,
        phone: str | None = None,
        sales_platform_code: str = "",
        sales_platform_name: str = "",
        store_name: str = "",
        site_name: str = "",
        system_order_nos: Sequence[str] = (),
    ) -> bool:
        """Persist the no-JSON Lingxing API fallback contact.

        E-mail comes only from the multi-platform order-list API and phone
        comes only from the WMS sales-outbound list.  A field already marked
        as customization-JSON or desktop-manual authoritative is retained
        verbatim, including an explicitly empty manual value, so the fallback
        cannot overwrite trusted contact data.
        """

        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        existing = self.get_contact(platform)
        independent_site = bool(INDEPENDENT_SITE_ORDER_RE.fullmatch(platform))
        existing_email_is_authoritative = bool(
            existing is not None
            and (
                existing.email_source == CONTACT_SOURCE_DESKTOP_MANUAL
                or (
                    existing.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
                    and bool(existing.email)
                )
            )
        )
        existing_phone_is_authoritative = bool(
            existing is not None
            and (
                existing.phone_source == CONTACT_SOURCE_DESKTOP_MANUAL
                or (
                    existing.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
                    and existing.phone_verification_state
                    == PHONE_VERIFICATION_MATCHED
                    and bool(existing.verified_phone_e164)
                )
            )
        )
        normalized_email = normalize_email(email) if email is not None else None
        normalized_phone = normalize_phone(phone) if phone is not None else None

        if existing_email_is_authoritative:
            next_email = existing.email
            email_presence = existing.email_presence
            email_source = existing.email_source
        elif normalized_email is not None:
            next_email = normalized_email
            email_presence = EMAIL_PRESENCE_PROVIDED
            email_source = CONTACT_SOURCE_LINGXING_ORDER_LIST
        else:
            next_email = existing.email if existing is not None else ""
            email_presence = (
                existing.email_presence
                if existing is not None
                else EMAIL_PRESENCE_UNKNOWN
            )
            email_source = existing.email_source if existing is not None else ""

        if existing_phone_is_authoritative:
            next_phone = existing.phone_raw
            phone_source = existing.phone_source
        elif normalized_phone is not None:
            next_phone = normalized_phone
            phone_source = CONTACT_SOURCE_WMS
        else:
            next_phone = existing.phone_raw if existing is not None else ""
            phone_source = existing.phone_source if existing is not None else ""

        previous_orders = existing.system_order_nos if existing is not None else ()
        incoming_orders = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in system_order_nos
                if str(value or "").strip()
            )
        )
        orders = incoming_orders or previous_orders
        values_unchanged = bool(
            existing is not None
            and existing.email == next_email
            and existing.email_presence == email_presence
            and existing.phone_raw == next_phone
            and existing.email_source == email_source
            and existing.phone_source == phone_source
            and existing.system_order_nos == orders
            and (not sales_platform_code or existing.sales_platform_code == sales_platform_code)
            and (not sales_platform_name or existing.sales_platform_name == sales_platform_name)
            and (not store_name or existing.store_name == store_name)
            and (not site_name or existing.site_name == site_name)
        )
        return self.upsert_contact(
            OrderContact(
                platform_order_no=platform,
                recipient_name=existing.recipient_name if existing is not None else "",
                email=next_email,
                email_presence=email_presence,
                phone_raw=next_phone,
                sales_platform_code=(
                    sales_platform_code.strip()
                    or (existing.sales_platform_code if existing is not None else "")
                ),
                sales_platform_name=(
                    sales_platform_name.strip()
                    or (existing.sales_platform_name if existing is not None else "")
                ),
                store_name=(
                    store_name.strip()
                    or (existing.store_name if existing is not None else "")
                ),
                site_name=(
                    site_name.strip()
                    or (existing.site_name if existing is not None else "")
                ),
                source=(
                    existing.source
                    if existing is not None
                    and existing.source
                    in {
                        CONTACT_SOURCE_CUSTOMIZATION_JSON,
                        CONTACT_SOURCE_DESKTOP_MANUAL,
                    }
                    else CONTACT_SOURCE_LINGXING_API_FALLBACK
                ),
                recipient_name_source=(
                    existing.recipient_name_source if existing is not None else ""
                ),
                email_source=email_source,
                phone_source=phone_source,
                verified_phone_e164=(
                    existing.verified_phone_e164 if existing is not None else ""
                ),
                phone_verification_state=(
                    PHONE_VERIFICATION_NOT_REQUIRED
                    if independent_site
                    else (
                        existing.phone_verification_state
                        if existing is not None
                        else PHONE_VERIFICATION_UNKNOWN
                    )
                ),
                captured_at=(
                    existing.captured_at
                    if existing is not None and values_unchanged
                    else ""
                ),
                system_order_nos=orders,
            ),
            replace_system_order_nos=True,
        )

    @staticmethod
    def _product_from_snapshot_row(
        row: sqlite3.Row,
        *,
        platform_order_no: str,
    ) -> OrderProductSnapshot:
        return OrderProductSnapshot(
            platform_order_no=platform_order_no,
            system_order_no=str(row["system_order_no"] or ""),
            item_key=str(row["item_key"] or ""),
            source_sequence=int(row["source_sequence"] or 0),
            local_sku=str(row["local_sku"] or ""),
            raw_title=str(row["raw_title"] or ""),
            display_title=str(row["display_title"] or ""),
            has_main_image=bool(row["has_main_image"]),
            metadata_valid=bool(row["metadata_valid"]),
            is_instruction=bool(row["is_instruction"]),
            source_payload_hash=str(row["source_payload_hash"] or ""),
        )

    def replace_product_scan(
        self,
        platform_order_no: str,
        products: Sequence[OrderProductSnapshot],
        expected_system_order_nos: Sequence[str],
    ) -> bool:
        """Replace one platform order's active API product evidence atomically."""

        self.initialize()
        platform = platform_order_no.strip()
        expected = {
            str(value or "").strip()
            for value in expected_system_order_nos
            if str(value or "").strip()
        }
        if not platform or not expected:
            raise ValueError("platform_order_no and expected system orders are required")
        if any(product.platform_order_no.strip() != platform for product in products):
            raise ValueError("Product platform order mismatch")
        if any(product.system_order_no.strip() not in expected for product in products):
            raise ValueError("Product system order is outside expected scope")
        keys = [
            (product.system_order_no.strip(), product.item_key.strip())
            for product in products
        ]
        if any(not system or not item_key for system, item_key in keys):
            raise ValueError("Product snapshots require stable system and item keys")
        if len(set(keys)) != len(keys):
            raise ValueError("Product snapshot keys must be unique")

        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_rows = conn.execute(
                "SELECT * FROM shipment_order_product_snapshots "
                "WHERE platform_order_no = ?",
                (platform,),
            ).fetchall()
            current = {
                (str(row["system_order_no"]), str(row["item_key"])): row
                for row in current_rows
            }
            active_keys = set(keys)
            changed = any(
                bool(row["active"])
                != ((str(row["system_order_no"]), str(row["item_key"])) in active_keys)
                for row in current_rows
            )
            conn.execute(
                "UPDATE shipment_order_product_snapshots "
                "SET active = 0, updated_at = ? "
                "WHERE platform_order_no = ? AND active = 1",
                (now, platform),
            )
            for product in products:
                key = (product.system_order_no.strip(), product.item_key.strip())
                previous = current.get(key)
                values = (
                    int(product.source_sequence),
                    product.local_sku.strip(),
                    product.raw_title.strip(),
                    product.display_title.strip(),
                    int(product.has_main_image),
                    int(product.metadata_valid),
                    int(product.is_instruction),
                    product.source_payload_hash,
                )
                if previous is None:
                    changed = True
                    conn.execute(
                        """
                        INSERT INTO shipment_order_product_snapshots (
                            platform_order_no, system_order_no, item_key,
                            source_sequence, local_sku, raw_title, display_title, has_main_image,
                            metadata_valid, is_instruction, source_payload_hash,
                            active, first_seen_at, last_seen_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (platform, *key, *values, now, now, now),
                    )
                    continue
                old_values = tuple(
                    previous[column]
                    for column in (
                        "source_sequence",
                        "local_sku",
                        "raw_title",
                        "display_title",
                        "has_main_image",
                        "metadata_valid",
                        "is_instruction",
                        "source_payload_hash",
                    )
                )
                if old_values != values or not previous["active"]:
                    changed = True
                conn.execute(
                    """
                    UPDATE shipment_order_product_snapshots
                    SET source_sequence = ?, local_sku = ?, raw_title = ?, display_title = ?,
                        has_main_image = ?, metadata_valid = ?, is_instruction = ?,
                        source_payload_hash = ?, active = 1,
                        last_seen_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, now, now, previous["id"]),
                )
            conn.commit()
        return changed

    @staticmethod
    def _package_from_snapshot_row(
        row: sqlite3.Row,
        *,
        platform_order_no: str,
    ) -> PackageSnapshot:
        return PackageSnapshot(
            package_key=str(row["package_key"]),
            platform_order_no=platform_order_no,
            system_order_no=str(row["system_order_no"] or ""),
            shipment_type=str(row["shipment_type"]),
            carrier_raw=str(row["carrier_raw"] or ""),
            carrier=str(row["carrier_normalized"] or ""),
            waybill_no=str(row["waybill_no"] or ""),
            tracking_no=str(row["tracking_no"] or ""),
            final_tracking_no=str(row["final_tracking_no"] or ""),
            wms_outbound_order_no=str(row["wms_outbound_order_no"] or ""),
            wms_status_code=(
                int(row["wms_status_code"])
                if row["wms_status_code"] is not None
                else None
            ),
            wms_status_name=str(row["wms_status_name"] or ""),
            outbound_state=str(row["outbound_state"] or "UNKNOWN"),
            outbound_observed_at=str(row["outbound_observed_at"] or ""),
            stable_sequence=int(row["stable_sequence"]),
            stable_label=str(row["stable_label"]),
            source_payload_hash=str(row["source_payload_hash"] or ""),
            customer_visible=bool(row["customer_visible"]),
            visibility_reason=str(row["visibility_reason"] or ""),
        )

    @staticmethod
    def _validate_package_scan(
        platform_order_no: str,
        packages: Sequence[PackageSnapshot],
    ) -> str:
        platform = platform_order_no.strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        if any(item.platform_order_no.strip() != platform for item in packages):
            raise ValueError("Package platform order mismatch")
        if len({item.package_key for item in packages}) != len(packages):
            raise ValueError("Package keys must be unique within a full scan")
        return platform

    @staticmethod
    def package_set_hash(packages: Sequence[PackageSnapshot]) -> str:
        payload = [
            {
                "package_key": item.package_key,
                "system_order_no": item.system_order_no,
                "shipment_type": item.shipment_type,
                "carrier": item.carrier,
                "waybill_no": item.waybill_no,
                "tracking_no": item.tracking_no,
                "final_tracking_no": item.final_tracking_no,
                "wms_outbound_order_no": item.wms_outbound_order_no,
                "wms_status_code": item.wms_status_code,
                "wms_status_name": item.wms_status_name,
                "outbound_state": item.outbound_state.strip().upper() or "UNKNOWN",
                "customer_visible": item.customer_visible,
                "visibility_reason": item.visibility_reason,
            }
            for item in sorted(packages, key=lambda candidate: candidate.package_key)
            if item.customer_visible
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _record_outbound_eligibility_conn(
        conn: sqlite3.Connection,
        *,
        platform: str,
        outbound_state: str,
        reason: str,
        expected_system_order_nos: Sequence[str],
        observed_system_order_nos: Sequence[str],
        package_set_hash: str,
        snapshot_complete: bool,
        observed_at: str,
        now: str,
    ) -> int:
        state = outbound_state.strip().upper() or "UNKNOWN"
        if state not in {"OUTBOUNDED", "TERMINAL", "WAITING", "UNKNOWN"}:
            raise ValueError(f"Unsupported outbound state: {outbound_state}")
        expected = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in expected_system_order_nos
                if str(value or "").strip()
            )
        )
        observed = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in observed_system_order_nos
                if str(value or "").strip()
            )
        )
        conn.execute(
            """
            INSERT INTO shipment_notification_outbound_eligibility (
                platform_order_no, outbound_state, reason,
                expected_system_order_nos_json, observed_system_order_nos_json,
                package_set_hash, snapshot_complete, observed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform_order_no) DO UPDATE SET
                outbound_state = excluded.outbound_state,
                reason = excluded.reason,
                expected_system_order_nos_json = excluded.expected_system_order_nos_json,
                observed_system_order_nos_json = excluded.observed_system_order_nos_json,
                package_set_hash = excluded.package_set_hash,
                snapshot_complete = excluded.snapshot_complete,
                observed_at = excluded.observed_at,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                state,
                reason.strip(),
                json.dumps(expected, ensure_ascii=False),
                json.dumps(observed, ensure_ascii=False),
                package_set_hash.strip(),
                int(snapshot_complete),
                observed_at.strip() or now,
                now,
            ),
        )
        if state == "OUTBOUNDED" and snapshot_complete:
            return 0
        latest = conn.execute(
            """
            SELECT * FROM shipment_notifications
            WHERE platform_order_no = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (platform,),
        ).fetchone()
        if latest is None or latest["state"] not in {
            NOTIFICATION_AWAITING_REVIEW,
            NOTIFICATION_BLOCKED,
            NOTIFICATION_RETRYABLE,
            NOTIFICATION_WAITING_CONTACT,
            NOTIFICATION_MANUAL_EMAIL_REQUIRED,
        }:
            return 0
        error = f"outbound_ineligible:{state}:{reason.strip() or 'unconfirmed'}"
        if (
            latest["state"] == NOTIFICATION_BLOCKED
            and str(latest["last_error"] or "") == error
            and not latest["approved_content_hash"]
        ):
            return 0
        conn.execute(
            """
            UPDATE shipment_notifications
            SET state = ?, approved_content_hash = NULL, approved_at = NULL,
                last_error = ?, state_changed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (NOTIFICATION_BLOCKED, error, now, now, latest["id"]),
        )
        conn.execute(
            """
            INSERT INTO shipment_notification_reviews (
                notification_id, revision, action, content_hash, actor, note, created_at
            ) VALUES (?, ?, 'INVALIDATED_BY_OUTBOUND_STATE', ?, 'system', ?, ?)
            """,
            (
                latest["id"],
                latest["revision"],
                latest["content_hash"],
                error,
                now,
            ),
        )
        return 1

    def record_outbound_eligibility(
        self,
        platform_order_no: str,
        *,
        outbound_state: str,
        reason: str,
        expected_system_order_nos: Sequence[str] = (),
        observed_system_order_nos: Sequence[str] = (),
        package_set_hash: str = "",
        snapshot_complete: bool = False,
        observed_at: str = "",
    ) -> int:
        self.initialize()
        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            blocked = self._record_outbound_eligibility_conn(
                conn,
                platform=platform,
                outbound_state=outbound_state,
                reason=reason,
                expected_system_order_nos=expected_system_order_nos,
                observed_system_order_nos=observed_system_order_nos,
                package_set_hash=package_set_hash,
                snapshot_complete=snapshot_complete,
                observed_at=observed_at,
                now=now,
            )
            conn.commit()
        return blocked

    def get_outbound_eligibility(
        self, platform_order_no: str
    ) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM shipment_notification_outbound_eligibility "
                "WHERE platform_order_no = ?",
                (str(platform_order_no or "").strip(),),
            ).fetchone()
            return dict(row) if row is not None else None

    @staticmethod
    def _apply_package_scan_conn(
        conn: sqlite3.Connection,
        *,
        platform: str,
        packages: Sequence[PackageSnapshot],
        now: str,
        deactivate_when_empty: bool = False,
    ) -> bool:
        changed = False
        current = {
            str(row["package_key"]): row
            for row in conn.execute(
                "SELECT * FROM shipment_package_snapshots WHERE platform_order_no = ?",
                (platform,),
            ).fetchall()
        }
        active_keys = {item.package_key for item in packages}
        if packages or deactivate_when_empty:
            for row in current.values():
                should_be_active = str(row["package_key"]) in active_keys
                if bool(row["active"]) != should_be_active:
                    changed = True
            conn.execute(
                "UPDATE shipment_package_snapshots SET active = 0, updated_at = ? "
                "WHERE platform_order_no = ? AND active = 1",
                (now, platform),
            )
        next_sequence = max(
            (int(row["stable_sequence"]) for row in current.values()),
            default=0,
        )
        for item in packages:
            previous = current.get(item.package_key)
            if previous is None:
                next_sequence += 1
                sequence = next_sequence
                label = stable_package_label(sequence)
            else:
                sequence = int(previous["stable_sequence"])
                label = str(previous["stable_label"])
            values = (
                item.system_order_no.strip(),
                item.shipment_type,
                item.carrier_raw.strip(),
                item.carrier.strip(),
                item.waybill_no.strip(),
                item.tracking_no.strip(),
                item.final_tracking_no.strip(),
                item.wms_outbound_order_no.strip(),
                item.wms_status_code,
                item.wms_status_name.strip(),
                item.outbound_state.strip().upper() or "UNKNOWN",
                item.outbound_observed_at.strip(),
                int(item.customer_visible),
                item.visibility_reason.strip(),
                item.source_payload_hash,
            )
            if previous is None:
                changed = True
                conn.execute(
                    """
                    INSERT INTO shipment_package_snapshots (
                        platform_order_no, package_key, stable_sequence, stable_label,
                        system_order_no, shipment_type, carrier_raw, carrier_normalized,
                        waybill_no, tracking_no, final_tracking_no, wms_outbound_order_no,
                        wms_status_code, wms_status_name, outbound_state,
                        outbound_observed_at, customer_visible, visibility_reason,
                        source_payload_hash, active, first_seen_at,
                        last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        platform,
                        item.package_key,
                        sequence,
                        label,
                        *values,
                        now,
                        now,
                        now,
                    ),
                )
                continue
            old_values = tuple(
                str(previous[key] or "")
                for key in (
                    "system_order_no",
                    "shipment_type",
                    "carrier_raw",
                    "carrier_normalized",
                    "waybill_no",
                    "tracking_no",
                    "final_tracking_no",
                    "wms_outbound_order_no",
                    "wms_status_code",
                    "wms_status_name",
                    "outbound_state",
                    "outbound_observed_at",
                    "customer_visible",
                    "visibility_reason",
                    "source_payload_hash",
                )
            )
            if old_values != tuple(str(value) for value in values) or not previous["active"]:
                changed = True
            conn.execute(
                """
                UPDATE shipment_package_snapshots
                SET system_order_no = ?, shipment_type = ?, carrier_raw = ?,
                    carrier_normalized = ?, waybill_no = ?, tracking_no = ?,
                    final_tracking_no = ?, wms_outbound_order_no = ?, wms_status_code = ?,
                    wms_status_name = ?, outbound_state = ?, outbound_observed_at = ?,
                    customer_visible = ?, visibility_reason = ?, source_payload_hash = ?,
                    active = 1, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, now, now, previous["id"]),
            )
        return changed

    def replace_package_scan(
        self,
        platform_order_no: str,
        packages: Sequence[PackageSnapshot],
    ) -> bool:
        """Atomically replace the active package set while retaining stable labels."""

        self.initialize()
        platform = self._validate_package_scan(platform_order_no, packages)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = self._apply_package_scan_conn(
                conn,
                platform=platform,
                packages=packages,
                now=now,
            )
            customer_packages = [item for item in packages if item.customer_visible]
            fully_outbounded = bool(customer_packages) and all(
                item.outbound_state.strip().upper() == "OUTBOUNDED"
                and item.wms_status_code == 3
                for item in customer_packages
            )
            customer_systems = tuple(
                dict.fromkeys(
                    item.system_order_no.strip()
                    for item in customer_packages
                    if item.system_order_no.strip()
                )
            )
            self._record_outbound_eligibility_conn(
                conn,
                platform=platform,
                outbound_state=("OUTBOUNDED" if fully_outbounded else "UNKNOWN"),
                reason=(
                    "direct_package_scan_confirmed"
                    if fully_outbounded
                    else "direct_package_scan_not_confirmed"
                ),
                expected_system_order_nos=customer_systems,
                observed_system_order_nos=customer_systems,
                package_set_hash=(
                    self.package_set_hash(packages) if fully_outbounded else ""
                ),
                snapshot_complete=fully_outbounded,
                observed_at=now,
                now=now,
            )
            conn.commit()
        return changed

    def merge_package_scan(
        self,
        platform_order_no: str,
        observed_packages: Sequence[PackageSnapshot],
        expected_system_order_nos: Sequence[str],
        *,
        authoritative_observed_system_order_nos: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Merge a partial WMS scan without forgetting previously observed logistics.

        WMS legitimately omits system orders that have not produced an outbound
        package yet.  A system returned in the current response is authoritative;
        for an omitted system, its prior snapshot remains active so tracking
        numbers already sent to a customer never disappear from a later revision.
        ``authoritative_observed_system_order_nos`` may additionally include
        systems whose only returned rows were terminal and filtered by the caller;
        their former active snapshots must be deactivated rather than preserved.
        """

        self.initialize()
        platform = self._validate_package_scan(platform_order_no, observed_packages)
        expected = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in expected_system_order_nos
                if str(value or "").strip()
            )
        )
        if not expected:
            raise ValueError("expected_system_order_nos is required")
        expected_set = set(expected)
        observed_systems = {
            item.system_order_no.strip()
            for item in observed_packages
            if item.system_order_no.strip()
        }
        if not observed_systems.issubset(expected_set):
            raise ValueError("Observed package system order is outside expected scope")
        authoritative_systems = (
            {
                str(value or "").strip()
                for value in authoritative_observed_system_order_nos
                if str(value or "").strip()
            }
            if authoritative_observed_system_order_nos is not None
            else set(observed_systems)
        )
        if not observed_systems.issubset(authoritative_systems):
            raise ValueError(
                "Observed packages must be included in authoritative WMS systems"
            )
        if not authoritative_systems.issubset(expected_set):
            raise ValueError(
                "Authoritative WMS system order is outside expected scope"
            )

        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            instruction_systems = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT system_order_no
                    FROM shipment_order_product_snapshots
                    WHERE platform_order_no = ? AND active = 1
                    GROUP BY system_order_no
                    HAVING COUNT(*) > 0
                       AND SUM(CASE WHEN is_instruction = 1 THEN 1 ELSE 0 END) = COUNT(*)
                    """,
                    (platform,),
                ).fetchall()
            }
            customer_expected_set = expected_set.difference(instruction_systems)
            current_active = conn.execute(
                "SELECT * FROM shipment_package_snapshots "
                "WHERE platform_order_no = ? AND active = 1 "
                "ORDER BY stable_sequence",
                (platform,),
            ).fetchall()
            preserved = [
                replace(
                    self._package_from_snapshot_row(row, platform_order_no=platform),
                    customer_visible=(
                        str(row["system_order_no"] or "") not in instruction_systems
                    ),
                    visibility_reason=(
                        "instruction"
                        if str(row["system_order_no"] or "") in instruction_systems
                        else ""
                    ),
                )
                for row in current_active
                if str(row["system_order_no"] or "") in expected_set
                and str(row["system_order_no"] or "") not in authoritative_systems
            ]
            merged = [*observed_packages, *preserved]
            if len({item.package_key for item in merged}) != len(merged):
                conn.rollback()
                raise ValueError("Merged package keys must be unique")
            changed = self._apply_package_scan_conn(
                conn,
                platform=platform,
                packages=merged,
                now=now,
                deactivate_when_empty=True,
            )
            active = conn.execute(
                "SELECT system_order_no, carrier_normalized, final_tracking_no, "
                "customer_visible, outbound_state, wms_status_code "
                "FROM shipment_package_snapshots "
                "WHERE platform_order_no = ? AND active = 1",
                (platform,),
            ).fetchall()
            conn.commit()

        active_systems = {
            str(row["system_order_no"] or "").strip()
            for row in active
            if str(row["system_order_no"] or "").strip()
        }
        customer_active_systems = {
            str(row["system_order_no"] or "").strip()
            for row in active
            if bool(row["customer_visible"])
            and str(row["system_order_no"] or "").strip()
        }
        missing_systems = customer_expected_set.difference(customer_active_systems)
        complete = sum(
            1
            for row in active
            if bool(row["customer_visible"])
            and str(row["outbound_state"] or "").upper() == "OUTBOUNDED"
            and row["wms_status_code"] == 3
            and str(row["carrier_normalized"] or "").strip()
            and str(row["final_tracking_no"] or "").strip()
        )
        # Expected system orders are internal scan coverage, not synthetic
        # packages.  Customer-facing counts include only real WMS snapshots;
        # missing systems remain available separately for retry diagnostics.
        total = sum(1 for row in active if bool(row["customer_visible"]))
        return {
            "changed": int(changed),
            "observed_package_count": len(observed_packages),
            "preserved_package_count": len(preserved),
            "expected_system_order_count": len(expected),
            "missing_system_order_count": len(missing_systems),
            "package_total": total,
            "package_complete": complete,
            "package_missing": total - complete,
        }

    def observe_package_events(
        self,
        platform_order_no: str,
        packages: Sequence[PackageSnapshot],
        *,
        baseline_pending: bool,
        source_kind: str,
    ) -> dict[str, int]:
        """Record first-complete customer packages and return pending event counts."""

        self.initialize()
        platform = str(platform_order_no or "").strip()
        suppress_new = bool(baseline_pending)
        now = utc_now()
        inserted = 0
        baseline_count = 0
        corrected = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for package in packages:
                if not (
                    package.customer_visible
                    and package.complete
                    and str(package.final_tracking_no or "").strip()
                    and package.wms_status_code == 3
                    and package.outbound_state.strip().upper() == "OUTBOUNDED"
                ):
                    continue
                previous = conn.execute(
                    "SELECT * FROM shipment_notification_package_events "
                    "WHERE platform_order_no = ? AND package_key = ?",
                    (platform, package.package_key),
                ).fetchone()
                tracking = str(package.final_tracking_no or "").strip()
                if previous is None:
                    conn.execute(
                        """
                        INSERT INTO shipment_notification_package_events (
                            platform_order_no, package_key, first_tracking_no,
                            last_tracking_no, baseline_suppressed,
                            handled_notification_id, first_completed_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                        """,
                        (
                            platform,
                            package.package_key,
                            tracking,
                            tracking,
                            int(suppress_new),
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                    baseline_count += int(suppress_new)
                elif str(previous["last_tracking_no"] or "") != tracking:
                    conn.execute(
                        "UPDATE shipment_notification_package_events "
                        "SET last_tracking_no = ?, updated_at = ? "
                        "WHERE platform_order_no = ? AND package_key = ?",
                        (tracking, now, platform, package.package_key),
                    )
                    corrected += 1
            if baseline_pending and "AMAZON_FULL_SCAN" in str(source_kind or ""):
                conn.execute(
                    "UPDATE shipment_notification_order_sources "
                    "SET baseline_pending = 0, updated_at = ? "
                    "WHERE platform_order_no = ?",
                    (now, platform),
                )
            if baseline_pending and INDEPENDENT_SITE_ORDER_RE.fullmatch(platform):
                conn.execute(
                    """
                    INSERT INTO shipment_notification_wc_baselines (
                        platform_order_no, baseline_completed_at, updated_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(platform_order_no) DO UPDATE SET
                        baseline_completed_at = excluded.baseline_completed_at,
                        updated_at = excluded.updated_at
                    """,
                    (platform, now, now),
                )
            pending = conn.execute(
                """
                SELECT COUNT(*)
                FROM shipment_notification_package_events
                WHERE platform_order_no = ?
                  AND baseline_suppressed = 0
                  AND handled_notification_id IS NULL
                """,
                (platform,),
            ).fetchone()
            conn.commit()
        return {
            "inserted_event_count": inserted,
            "baseline_suppressed_count": baseline_count,
            "corrected_event_count": corrected,
            "pending_event_count": int(pending[0] or 0) if pending else 0,
        }

    @staticmethod
    def _mark_package_events_handled_conn(
        conn: sqlite3.Connection,
        notification_id: int,
    ) -> None:
        conn.execute(
            """
            UPDATE shipment_notification_package_events
            SET handled_notification_id = ?, updated_at = ?
            WHERE baseline_suppressed = 0
              AND handled_notification_id IS NULL
              AND (platform_order_no, package_key) IN (
                  SELECT n.platform_order_no, i.package_key
                  FROM shipment_notifications n
                  JOIN shipment_notification_items i ON i.notification_id = n.id
                  WHERE n.id = ? AND i.customer_visible = 1 AND i.is_complete = 1
              )
            """,
            (notification_id, utc_now(), notification_id),
        )

    def _contact_conn(
        self, conn: sqlite3.Connection, platform_order_no: str
    ) -> OrderContact | None:
        row = conn.execute(
            "SELECT * FROM shipment_order_contacts WHERE platform_order_no = ?",
            (platform_order_no,),
        ).fetchone()
        if row is None:
            return None
        try:
            system_order_nos = tuple(json.loads(row["system_order_nos_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            system_order_nos = ()
        return OrderContact(
            platform_order_no=platform_order_no,
            recipient_name=str(row["recipient_name"] or ""),
            email=str(row["email"] or ""),
            email_presence=str(row["email_presence"] or EMAIL_PRESENCE_UNKNOWN),
            phone_raw=str(row["phone_raw"] or ""),
            sales_platform_code=str(row["sales_platform_code"] or ""),
            sales_platform_name=str(row["sales_platform_name"] or ""),
            store_name=str(row["store_name"] or ""),
            site_name=str(row["site_name"] or ""),
            source=str(row["contact_source"] or ""),
            recipient_name_source=str(row["recipient_name_source"] or ""),
            email_source=str(row["email_source"] or ""),
            phone_source=str(row["phone_source"] or ""),
            verified_phone_e164=str(row["verified_phone_e164"] or ""),
            phone_verification_state=str(
                row["phone_verification_state"] or PHONE_VERIFICATION_UNKNOWN
            ),
            captured_at=str(row["contact_captured_at"] or ""),
            system_order_nos=system_order_nos,
        )

    def get_contact(self, platform_order_no: str) -> OrderContact | None:
        self.initialize()
        with self.connect() as conn:
            return self._contact_conn(conn, platform_order_no.strip())

    def _packages_conn(
        self, conn: sqlite3.Connection, platform_order_no: str
    ) -> list[PackageSnapshot]:
        rows = conn.execute(
            """
            SELECT * FROM shipment_package_snapshots
            WHERE platform_order_no = ? AND active = 1
            ORDER BY stable_sequence
            """,
            (platform_order_no,),
        ).fetchall()
        return [
            PackageSnapshot(
                package_key=str(row["package_key"]),
                platform_order_no=platform_order_no,
                system_order_no=str(row["system_order_no"] or ""),
                shipment_type=str(row["shipment_type"]),
                carrier_raw=str(row["carrier_raw"] or ""),
                carrier=str(row["carrier_normalized"] or ""),
                waybill_no=str(row["waybill_no"] or ""),
                tracking_no=str(row["tracking_no"] or ""),
                final_tracking_no=str(row["final_tracking_no"] or ""),
                wms_outbound_order_no=str(row["wms_outbound_order_no"] or ""),
                wms_status_code=(
                    int(row["wms_status_code"])
                    if row["wms_status_code"] is not None
                    else None
                ),
                wms_status_name=str(row["wms_status_name"] or ""),
                outbound_state=str(row["outbound_state"] or "UNKNOWN"),
                outbound_observed_at=str(row["outbound_observed_at"] or ""),
                stable_sequence=int(row["stable_sequence"]),
                stable_label=str(row["stable_label"]),
                source_payload_hash=str(row["source_payload_hash"] or ""),
                customer_visible=bool(row["customer_visible"]),
                visibility_reason=str(row["visibility_reason"] or ""),
            )
            for row in rows
        ]

    def list_packages(self, platform_order_no: str) -> list[PackageSnapshot]:
        self.initialize()
        with self.connect() as conn:
            return self._packages_conn(conn, str(platform_order_no or "").strip())

    def _products_conn(
        self, conn: sqlite3.Connection, platform_order_no: str
    ) -> list[OrderProductSnapshot]:
        rows = conn.execute(
            """
            SELECT * FROM shipment_order_product_snapshots
            WHERE platform_order_no = ? AND active = 1
            ORDER BY source_sequence, id
            """,
            (platform_order_no,),
        ).fetchall()
        return [
            self._product_from_snapshot_row(row, platform_order_no=platform_order_no)
            for row in rows
        ]

    @staticmethod
    def _queue_counts_conn(
        conn: sqlite3.Connection, platform_order_no: str
    ) -> tuple[int, int, str]:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN j.identity_state = 'ACTIVE'
                                  AND e.state = 'DONE'
                                  AND e.checkpoint = 'OUTBOUNDED'
                                  AND e.completion_source = ?
                            THEN 1 ELSE 0 END) AS complete
                   ,MAX(COALESCE(e.outbounded_at, e.externally_completed_at, e.updated_at))
                        AS erp_completed_at
            FROM shipment_jobs j
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE j.platform_order_no = ? AND j.identity_state <> 'CANCELLED'
            """,
            (ERP_COMPLETION_AUTOMATION, platform_order_no),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), str(row[2] or "")

    @staticmethod
    def _source_kind_conn(conn: sqlite3.Connection, platform_order_no: str) -> str:
        auto = conn.execute(
            """
            SELECT 1
            FROM shipment_jobs j
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE j.platform_order_no = ?
              AND j.identity_state = 'ACTIVE'
              AND e.state = 'DONE'
              AND e.checkpoint = 'OUTBOUNDED'
              AND e.completion_source = ?
            LIMIT 1
            """,
            (platform_order_no, ERP_COMPLETION_AUTOMATION),
        ).fetchone()
        full = conn.execute(
            "SELECT purchased_at FROM shipment_notification_order_sources "
            "WHERE platform_order_no = ? AND active = 1",
            (platform_order_no,),
        ).fetchone()
        if auto and full:
            return "AUTO_ERP+AMAZON_FULL_SCAN"
        if auto:
            return "AUTO_ERP"
        if full:
            return "AMAZON_FULL_SCAN"
        return ""

    def _render_current_conn(
        self,
        conn: sqlite3.Connection,
        platform_order_no: str,
        configuration: NotificationConfiguration,
    ) -> tuple[
        RenderedNotification | None,
        list[PackageSnapshot],
        int,
        int,
        str,
        bool,
        OrderContact | None,
    ]:
        contact = self._contact_conn(conn, platform_order_no)
        packages = self._packages_conn(conn, platform_order_no)
        products = self._products_conn(conn, platform_order_no)
        outbound_eligibility = conn.execute(
            "SELECT * FROM shipment_notification_outbound_eligibility "
            "WHERE platform_order_no = ?",
            (platform_order_no,),
        ).fetchone()
        queue_total, queue_complete, erp_completed_at = self._queue_counts_conn(
            conn, platform_order_no
        )
        source_kind = self._source_kind_conn(conn, platform_order_no)
        if "AMAZON_FULL_SCAN" in source_kind and not erp_completed_at:
            source_row = conn.execute(
                "SELECT purchased_at FROM shipment_notification_order_sources "
                "WHERE platform_order_no = ? AND active = 1",
                (platform_order_no,),
            ).fetchone()
            erp_completed_at = str(source_row[0] or "") if source_row else ""
        if not source_kind or (
            source_kind == "AUTO_ERP" and (not queue_total or queue_complete <= 0)
        ):
            return (
                None,
                packages,
                queue_total,
                queue_complete,
                erp_completed_at,
                False,
                contact,
            )
        current_package_hash = self.package_set_hash(packages)
        if (
            outbound_eligibility is None
            or str(outbound_eligibility["outbound_state"] or "").upper()
            != "OUTBOUNDED"
            or not bool(outbound_eligibility["snapshot_complete"])
            or not str(outbound_eligibility["package_set_hash"] or "").strip()
            or str(outbound_eligibility["package_set_hash"]) != current_package_hash
        ):
            return (
                None,
                packages,
                queue_total,
                queue_complete,
                erp_completed_at,
                False,
                contact,
            )
        if contact is None:
            contact = OrderContact(platform_order_no=platform_order_no, source="missing")
        # Missing system-order rows are retry diagnostics, not customer
        # packages.  Do not synthesize ``pending_wms`` items into the review
        # snapshot: otherwise a single real WMS package is displayed as 1/2
        # even though the second system has never produced a package.
        packages = [
            item
            for item in packages
            if item.customer_visible
            and item.complete
            and item.visibility_reason != "pending_wms"
        ]
        rendered = render_notification(
            contact,
            packages,
            configuration,
            expected_system_order_nos=contact.system_order_nos,
            products=products,
            platform_policy=(
                PLATFORM_POLICY_INDEPENDENT_SITE
                if INDEPENDENT_SITE_ORDER_RE.fullmatch(platform_order_no)
                else PLATFORM_POLICY_AMAZON
            ),
        )
        # Customization JSON remains the first choice.  When no usable JSON is
        # available, the documented Lingxing order list (e-mail) and WMS sales
        # outbound list (phone) are trusted field-specific fallbacks.
        independent_site = is_independent_site_order(platform_order_no)
        trusted_phone_sources = {
            CONTACT_SOURCE_CUSTOMIZATION_JSON,
            CONTACT_SOURCE_DESKTOP_MANUAL,
            CONTACT_SOURCE_WMS,
            CONTACT_SOURCE_LINGXING_DETAIL_REFRESH,
        }
        normalized_contact_email = normalize_email(contact.email)
        amazon_virtual_email = bool(
            not independent_site
            and normalized_contact_email
            and is_virtual_email(
                normalized_contact_email,
                platform_code=contact.sales_platform_code,
                platform_name=contact.sales_platform_name,
                configuration=configuration,
            )
        )
        channel_source_is_trusted = bool(
            (
                rendered.channel in {CHANNEL_EMAIL, CHANNEL_MANUAL_EMAIL}
                and contact.email_source
                in {
                    CONTACT_SOURCE_CUSTOMIZATION_JSON,
                    CONTACT_SOURCE_DESKTOP_MANUAL,
                    CONTACT_SOURCE_LINGXING_ORDER_LIST,
                }
            )
            or (
                rendered.channel == CHANNEL_SMS
                and (
                    (
                        independent_site
                        and contact.phone_source in trusted_phone_sources
                    )
                    or (
                        not independent_site
                        and (
                            (
                                contact.phone_verification_state
                                == PHONE_VERIFICATION_MATCHED
                                and normalize_phone(contact.phone_raw)
                                == normalize_phone(contact.verified_phone_e164)
                            )
                            or (
                                amazon_virtual_email
                                and contact.phone_source in trusted_phone_sources
                                and bool(normalize_phone(contact.phone_raw))
                            )
                        )
                    )
                )
            )
        )

        contact_ready = (
            bool(normalize_recipient_name(contact.recipient_name))
            and contact.recipient_name_source == CONTACT_SOURCE_WMS
            and channel_source_is_trusted
        )
        return (
            rendered,
            packages,
            queue_total,
            queue_complete,
            erp_completed_at,
            contact_ready,
            contact,
        )

    @staticmethod
    def _matches_stored_business_snapshot_conn(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        rendered: RenderedNotification,
        packages: Sequence[PackageSnapshot],
        *,
        queue_total: int,
        queue_complete: int,
        contact: OrderContact,
    ) -> bool:
        """Compare reviewed business data while deliberately ignoring template output."""

        expected_fields = {
            "channel": rendered.channel,
            "recipient_name": rendered.recipient_name,
            "recipient_email": rendered.recipient_email,
            "email_presence": str(
                contact.email_presence or EMAIL_PRESENCE_UNKNOWN
            ).strip().upper(),
            "recipient_phone": rendered.recipient_phone,
            "sales_platform_code": contact.sales_platform_code,
            "sales_platform_name": contact.sales_platform_name,
            "store_name": contact.store_name,
            "site_name": contact.site_name,
            "target": rendered.target,
            "sender_email": rendered.sender_email,
        }
        for field_name, expected in expected_fields.items():
            if str(row[field_name] or "") != str(expected or ""):
                return False
        if (
            int(row["package_total"] or 0) != rendered.package_total
            or int(row["package_complete"] or 0) != rendered.package_complete
            or int(row["package_missing"] or 0) != rendered.package_missing
            or int(row["queue_total"] or 0) != queue_total
            or int(row["queue_complete"] or 0) != queue_complete
        ):
            return False
        try:
            stored_product_names = tuple(json.loads(row["product_names_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            return False
        if stored_product_names != rendered.product_names:
            return False

        stored_items = conn.execute(
            """
            SELECT package_key, stable_sequence, stable_label, system_order_no,
                   shipment_type, carrier_raw, carrier_normalized, waybill_no,
                   tracking_no, final_tracking_no, wms_outbound_order_no,
                   wms_status_code, wms_status_name, outbound_state, customer_visible,
                   visibility_reason, is_complete
            FROM shipment_notification_items
            WHERE notification_id = ?
            ORDER BY stable_sequence
            """,
            (int(row["id"]),),
        ).fetchall()
        ordered = sorted(packages, key=lambda item: item.stable_sequence)
        if len(stored_items) != len(ordered):
            return False
        for stored, current in zip(stored_items, ordered):
            expected_item = (
                current.package_key,
                current.stable_sequence,
                current.stable_label,
                current.system_order_no,
                current.shipment_type,
                current.carrier_raw,
                current.carrier,
                current.waybill_no,
                current.tracking_no,
                current.final_tracking_no,
                current.wms_outbound_order_no,
                current.wms_status_code,
                current.wms_status_name,
                current.outbound_state.strip().upper() or "UNKNOWN",
                int(current.customer_visible),
                current.visibility_reason,
                int(current.complete),
            )
            actual_item = tuple(stored[index] for index in range(len(expected_item)))
            if actual_item != expected_item:
                return False
        return True

    @staticmethod
    def _matches_stored_delivery_content(
        row: sqlite3.Row,
        rendered: RenderedNotification,
    ) -> bool:
        """Compare exactly what the approved provider request will deliver.

        Contact provenance is audit metadata, not customer-visible content.  A
        provenance-only refresh must not invalidate an otherwise identical
        review.  Template/body changes, however, must always be reviewed again.
        """

        expected = {
            "subject": rendered.subject,
            "body": rendered.body,
            "body_html": rendered.body_html,
            "template_version": rendered.template_version,
            "sms_encoding": rendered.sms_encoding,
            "sms_character_count": rendered.sms_character_count,
            "sms_segment_count": rendered.sms_segment_count,
        }
        return all(
            str(row[field_name] or "") == str(value or "")
            for field_name, value in expected.items()
        )

    @staticmethod
    def _matches_legacy_raw_business_snapshot_conn(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        rendered: RenderedNotification,
        packages: Sequence[PackageSnapshot],
        *,
        queue_total: int,
        queue_complete: int,
        contact: OrderContact,
    ) -> bool:
        """Freeze sent/approved pre-v4 content until raw logistics actually changes."""

        expected_fields = {
            "channel": rendered.channel,
            "recipient_name": rendered.recipient_name,
            "recipient_email": rendered.recipient_email,
            "email_presence": str(
                contact.email_presence or EMAIL_PRESENCE_UNKNOWN
            ).strip().upper(),
            "recipient_phone": rendered.recipient_phone,
            "sales_platform_code": contact.sales_platform_code,
            "sales_platform_name": contact.sales_platform_name,
            "store_name": contact.store_name,
            "site_name": contact.site_name,
            "target": rendered.target,
            "sender_email": rendered.sender_email,
        }
        if any(
            str(row[field_name] or "") != str(value or "")
            for field_name, value in expected_fields.items()
        ):
            return False
        if (
            int(row["queue_total"] or 0) != queue_total
            or int(row["queue_complete"] or 0) != queue_complete
        ):
            return False
        stored_items = conn.execute(
            """
            SELECT package_key, stable_sequence, stable_label, system_order_no,
                   shipment_type, carrier_raw, carrier_normalized, waybill_no,
                   tracking_no, final_tracking_no, wms_outbound_order_no,
                   wms_status_code, wms_status_name, outbound_state, is_complete
            FROM shipment_notification_items
            WHERE notification_id = ? AND package_snapshot_id IS NOT NULL
            ORDER BY stable_sequence
            """,
            (int(row["id"]),),
        ).fetchall()
        ordered = sorted(
            (
                item
                for item in packages
                if item.visibility_reason != "pending_wms"
            ),
            key=lambda item: item.stable_sequence,
        )
        if len(stored_items) != len(ordered):
            return False
        for stored, current in zip(stored_items, ordered):
            expected_item = (
                current.package_key,
                current.stable_sequence,
                current.stable_label,
                current.system_order_no,
                current.shipment_type,
                current.carrier_raw,
                current.carrier,
                current.waybill_no,
                current.tracking_no,
                current.final_tracking_no,
                current.wms_outbound_order_no,
                current.wms_status_code,
                current.wms_status_name,
                current.outbound_state.strip().upper() or "UNKNOWN",
                int(current.complete),
            )
            if tuple(stored[index] for index in range(len(expected_item))) != expected_item:
                return False
        return True

    def prepare_notification(
        self,
        platform_order_no: str,
        configuration: NotificationConfiguration,
        *,
        force_reopen_notification_id: int | None = None,
        reopen_actor: str = "desktop_user",
        reopen_note: str = "",
        blocked_reason: str = "",
        allow_incomplete_issue: bool = False,
    ) -> dict[str, Any] | None:
        self.initialize()
        platform = platform_order_no.strip()
        if INDEPENDENT_SITE_ORDER_RE.fullmatch(platform):
            return self.get_latest_notification(platform)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            (
                rendered,
                packages,
                queue_total,
                queue_complete,
                erp_completed_at,
                contact_ready,
                contact_snapshot,
            ) = self._render_current_conn(conn, platform, configuration)
            if rendered is None:
                conn.rollback()
                return None
            # An expected system order without a WMS row is a normal partial
            # shipment.  Do not create a blocked draft until at least one real,
            # customer-visible tracking number exists.
            if rendered.package_complete <= 0 and not allow_incomplete_issue:
                conn.rollback()
                return None
            latest = conn.execute(
                """
                SELECT * FROM shipment_notifications
                WHERE platform_order_no = ? AND legacy_email_batch_id IS NULL
                ORDER BY revision DESC LIMIT 1
                """,
                (platform,),
            ).fetchone()
            force_reopen = force_reopen_notification_id is not None
            if not force_reopen and latest is None and not blocked_reason:
                historical_sent = conn.execute(
                    """
                    SELECT id
                    FROM shipment_notifications
                    WHERE platform_order_no = ?
                      AND (
                          state IN (?, ?)
                          OR TRIM(COALESCE(provider_message_id, '')) <> ''
                          OR TRIM(COALESCE(sent_at, '')) <> ''
                      )
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        platform,
                        NOTIFICATION_ACCEPTED,
                        NOTIFICATION_DELIVERED,
                    ),
                ).fetchone()
                if historical_sent is not None:
                    conn.rollback()
                    return self.get_notification(int(historical_sent["id"]))
            if force_reopen:
                allowed_states = {
                    NOTIFICATION_AWAITING_REVIEW,
                    NOTIFICATION_REJECTED,
                    NOTIFICATION_BLOCKED,
                    NOTIFICATION_WAITING_CONTACT,
                    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
                    NOTIFICATION_FAILED,
                    NOTIFICATION_RETRYABLE,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_MANUALLY_COMPLETED,
                    NOTIFICATION_CANCELLED,
                }
                note = reopen_note.strip()
                if not note:
                    conn.rollback()
                    raise NotificationStateError("A manual reopen reason is required.")
                if (
                    latest is None
                    or int(latest["id"]) != int(force_reopen_notification_id)
                    or latest["state"] not in allowed_states
                    or latest["legacy_email_batch_id"] is not None
                ):
                    conn.rollback()
                    raise NotificationStateError(
                        "Only the latest notification in a safe state can be reopened."
                    )
                if not contact_ready:
                    conn.rollback()
                    raise NotificationStateError(
                        "Current recipient contact is incomplete and cannot enter review."
                    )
                if rendered.blocked_reasons:
                    conn.rollback()
                    raise NotificationStateError(
                        "Current notification content is blocked and cannot enter review: "
                        + ",".join(rendered.blocked_reasons)
                    )
            if not force_reopen and latest is not None:
                permanently_closed = latest["state"] in {
                    NOTIFICATION_CANCELLED,
                    NOTIFICATION_SUPPRESSED,
                }
                already_sent = (
                    latest["state"]
                    in {
                        NOTIFICATION_ACCEPTED,
                        NOTIFICATION_DELIVERED,
                        NOTIFICATION_MANUALLY_COMPLETED,
                    }
                    or bool(str(latest["provider_message_id"] or "").strip())
                    or bool(str(latest["sent_at"] or "").strip())
                )
                legitimate_supplement = (
                    already_sent
                    and _has_newly_completed_package_conn(
                        conn,
                        latest,
                        packages,
                        package_complete=rendered.package_complete,
                        package_missing=rendered.package_missing,
                    )
                )
                if (
                    not blocked_reason
                    and (
                        permanently_closed
                        or (already_sent and not legitimate_supplement)
                    )
                ):
                    # Complete sends and unchanged partial sends are terminal.
                    # Only a genuinely new package that was absent from a prior
                    # partial notification may create an automatic review draft.
                    conn.rollback()
                    return self.get_notification(int(latest["id"]))
            business_unchanged: bool | None = None
            if (
                not force_reopen
                and not blocked_reason
                and latest is not None
                and contact_snapshot is not None
            ):
                legacy_frozen_state = latest["state"] in {
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                } or (
                    latest["state"] == NOTIFICATION_RETRYABLE
                    and latest["approved_content_hash"] == latest["content_hash"]
                )
                if (
                    legacy_frozen_state
                    and str(latest["template_version"] or "")
                    != rendered.template_version
                    and self._matches_legacy_raw_business_snapshot_conn(
                        conn,
                        latest,
                        rendered,
                        packages,
                        queue_total=queue_total,
                        queue_complete=queue_complete,
                        contact=contact_snapshot,
                    )
                ):
                    conn.rollback()
                    return self.get_notification(int(latest["id"]))
                business_unchanged = self._matches_stored_business_snapshot_conn(
                    conn,
                    latest,
                    rendered,
                    packages,
                    queue_total=queue_total,
                    queue_complete=queue_complete,
                    contact=contact_snapshot,
                )
                if latest["state"] in {
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                } and business_unchanged:
                    conn.rollback()
                    return self.get_notification(int(latest["id"]))
                if (
                    latest["state"] == NOTIFICATION_RETRYABLE
                    and latest["approved_content_hash"] == latest["content_hash"]
                    and business_unchanged
                ):
                    conn.rollback()
                    return self.get_notification(int(latest["id"]))
            if (
                not force_reopen
                and latest is not None
                and latest["content_hash"] == rendered.content_hash
                # The customer-visible body may stay identical while audited
                # logistics/contact metadata changes.  Reusing that old row
                # makes _claim() reject the same review forever because its
                # business snapshot is stale.  Create a new revision instead.
                and business_unchanged is not False
                and not (
                    latest["state"] == NOTIFICATION_BLOCKED
                    and str(latest["last_error"] or "").startswith(
                        "outbound_ineligible:"
                    )
                )
                and (
                    not blocked_reason
                    or (
                        latest["state"] == NOTIFICATION_BLOCKED
                        and str(latest["last_error"] or "") == blocked_reason
                    )
                )
            ):
                conn.rollback()
                return self.get_notification(int(latest["id"]))
            revision = int(latest["revision"] if latest is not None else 0) + 1
            state = (
                NOTIFICATION_AWAITING_REVIEW
                if force_reopen
                else (
                    NOTIFICATION_BLOCKED
                    if blocked_reason
                    else (
                        NOTIFICATION_WAITING_CONTACT
                        if not contact_ready
                        else (
                            NOTIFICATION_BLOCKED
                            if rendered.blocked_reasons
                            else (
                                NOTIFICATION_MANUAL_EMAIL_REQUIRED
                                if rendered.channel == CHANNEL_MANUAL_EMAIL
                                else NOTIFICATION_AWAITING_REVIEW
                            )
                        )
                    )
                )
            )
            if (
                latest is not None
                and latest["state"]
                in {
                    NOTIFICATION_AWAITING_REVIEW,
                    NOTIFICATION_BLOCKED,
                    NOTIFICATION_RETRYABLE,
                    NOTIFICATION_WAITING_CONTACT,
                    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
                }
                and (
                    not force_reopen
                    or latest["state"]
                    in {
                        NOTIFICATION_AWAITING_REVIEW,
                        NOTIFICATION_RETRYABLE,
                    }
                )
            ):
                conn.execute(
                    "UPDATE shipment_notifications SET state = ?, last_error = 'superseded', "
                    "state_changed_at = ?, updated_at = ? WHERE id = ?",
                    (NOTIFICATION_REJECTED, now, now, latest["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, note, created_at
                    ) VALUES (?, ?, ?, ?, '', ?)
                    """,
                    (
                        latest["id"],
                        latest["revision"],
                        (
                            "INVALIDATED_BY_MANUAL_REOPEN"
                            if force_reopen
                            else "INVALIDATED_BY_CHANGE"
                        ),
                        latest["content_hash"],
                        now,
                    ),
                )
            idempotency_key = hashlib.sha256(
                f"{platform}|{revision}|{rendered.content_hash}".encode("utf-8")
            ).hexdigest()
            if contact_snapshot is None:  # defensive: render_current always supplies one
                conn.rollback()
                return None
            state_changed_at = erp_completed_at if latest is None else now
            rendered_blocked_reason = ",".join(rendered.blocked_reasons)
            if blocked_reason:
                last_error = blocked_reason
            elif state == NOTIFICATION_WAITING_CONTACT:
                last_error = rendered_blocked_reason or "recipient_contact_unavailable"
            elif state == NOTIFICATION_MANUAL_EMAIL_REQUIRED:
                last_error = "manual_email_required_virtual_contact"
            else:
                last_error = rendered_blocked_reason or None
            conn.execute(
                """
                INSERT INTO shipment_notifications (
                    platform_order_no, revision, source_kind, channel, state, recipient_name,
                    recipient_email, email_presence, recipient_phone, sales_platform_code,
                    sales_platform_name, store_name, site_name, target, sender_email, subject,
                    body, body_html, sms_encoding, sms_character_count, sms_segment_count,
                    package_total, package_complete, package_missing, product_names_json,
                    queue_total,
                    queue_complete, template_version, content_hash, idempotency_key,
                    last_error, erp_completed_at, state_changed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    revision,
                    self._source_kind_conn(conn, platform),
                    rendered.channel,
                    state,
                    rendered.recipient_name,
                    rendered.recipient_email,
                    contact_snapshot.email_presence,
                    rendered.recipient_phone,
                    contact_snapshot.sales_platform_code,
                    contact_snapshot.sales_platform_name,
                    contact_snapshot.store_name,
                    contact_snapshot.site_name,
                    rendered.target,
                    rendered.sender_email,
                    rendered.subject,
                    rendered.body,
                    rendered.body_html,
                    rendered.sms_encoding,
                    rendered.sms_character_count,
                    rendered.sms_segment_count,
                    rendered.package_total,
                    rendered.package_complete,
                    rendered.package_missing,
                    json.dumps(rendered.product_names, ensure_ascii=False),
                    queue_total,
                    queue_complete,
                    rendered.template_version,
                    rendered.content_hash,
                    idempotency_key,
                    last_error,
                    erp_completed_at,
                    state_changed_at,
                    now,
                    now,
                ),
            )
            notification_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            package_rows = {
                str(row["package_key"]): row
                for row in conn.execute(
                    """
                    SELECT * FROM shipment_package_snapshots
                    WHERE platform_order_no = ? AND active = 1
                    """,
                    (platform,),
                ).fetchall()
            }
            for item in packages:
                source = package_rows.get(item.package_key)
                if source is None and item.visibility_reason != "pending_wms":
                    conn.rollback()
                    raise NotificationStateError(
                        "Current notification package snapshot is unavailable."
                    )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_items (
                        notification_id, package_snapshot_id, package_key,
                        stable_sequence, stable_label, system_order_no, shipment_type,
                        carrier_raw, carrier_normalized, waybill_no, tracking_no,
                        final_tracking_no, wms_outbound_order_no, wms_status_code,
                        wms_status_name, outbound_state, outbound_observed_at,
                        tracking_url, customer_visible, visibility_reason, is_complete
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        notification_id,
                        source["id"] if source is not None else None,
                        item.package_key,
                        item.stable_sequence,
                        item.stable_label,
                        item.system_order_no,
                        item.shipment_type,
                        item.carrier_raw,
                        item.carrier,
                        item.waybill_no,
                        item.tracking_no,
                        item.final_tracking_no,
                        item.wms_outbound_order_no,
                        item.wms_status_code,
                        item.wms_status_name,
                        item.outbound_state.strip().upper() or "UNKNOWN",
                        item.outbound_observed_at,
                        (
                            tracking_url_for(item.carrier, item.final_tracking_no)
                            if item.complete and item.customer_visible
                            else ""
                        ),
                        int(item.customer_visible),
                        item.visibility_reason,
                        int(item.complete),
                    ),
                )
            if force_reopen:
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, actor, note, created_at
                    ) VALUES (?, ?, 'MANUAL_REOPEN', ?, ?, ?, ?)
                    """,
                    (
                        notification_id,
                        revision,
                        rendered.content_hash,
                        reopen_actor.strip() or "desktop_user",
                        reopen_note.strip(),
                        now,
                    ),
                )
            conn.commit()
        return self.get_notification(notification_id)

    def prepare_all(
        self, configuration: NotificationConfiguration
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for target in self.notification_scan_targets():
            notification = self.prepare_notification(
                target["platform_order_no"], configuration
            )
            if notification is not None:
                output.append(notification)
        return output

    def get_notification(self, notification_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM shipment_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                return None
            items = conn.execute(
                """
                SELECT * FROM shipment_notification_items
                WHERE notification_id = ? ORDER BY stable_sequence
                """,
                (notification_id,),
            ).fetchall()
            reviews = conn.execute(
                """
                SELECT revision, action, content_hash, actor, note, created_at
                FROM shipment_notification_reviews
                WHERE notification_id = ? ORDER BY id
                """,
                (notification_id,),
            ).fetchall()
            earlier_delivered = conn.execute(
                """
                SELECT 1
                FROM shipment_notifications
                WHERE platform_order_no = ?
                  AND legacy_email_batch_id IS NULL
                  AND revision < ?
                  AND state IN (?, ?, ?)
                LIMIT 1
                """,
                (
                    str(row["platform_order_no"]),
                    int(row["revision"]),
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_MANUALLY_COMPLETED,
                ),
            ).fetchone()
            shipment_job_columns = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(shipment_jobs)")
            }
            if "product_type" in shipment_job_columns:
                raw_product_types = [
                    str(product_row[0] or "").strip()
                    for product_row in conn.execute(
                        """
                        SELECT DISTINCT TRIM(COALESCE(product_type, ''))
                        FROM shipment_jobs
                        WHERE platform_order_no = ?
                        ORDER BY TRIM(COALESCE(product_type, '')) COLLATE NOCASE
                        """,
                        (str(row["platform_order_no"]),),
                    ).fetchall()
                ]
                product_types = list(
                    dict.fromkeys(
                        part.strip()
                        for value in raw_product_types
                        for part in value.replace("、", "|").split("|")
                        if part.strip()
                    )
                )
            else:
                product_types = []
        result = dict(row)
        result["product_types"] = product_types or [""]
        result["product_type"] = "、".join(
            value or "未识别" for value in result["product_types"]
        )
        try:
            result["product_names"] = list(
                json.loads(result.get("product_names_json") or "[]")
            )
        except (TypeError, json.JSONDecodeError):
            result["product_names"] = []
        item_values = [dict(item) for item in items]
        display_index = 0
        for item in item_values:
            if bool(item.get("customer_visible", 1)) and bool(item.get("is_complete")):
                display_index += 1
                item["display_label"] = stable_package_label(display_index)
            else:
                item["display_label"] = ""
        result["items"] = item_values
        result["reviews"] = [dict(review) for review in reviews]
        result["is_supplemental_revision"] = earlier_delivered is not None
        return result

    def record_unsent_send_failure(
        self,
        notification_id: int,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        """Expose a pre-provider send failure on the latest review row.

        Provider attempts already transition to RETRYABLE/FAILED and persist
        their own error.  This method is deliberately limited to the latest
        AWAITING_REVIEW row, where validation failures previously disappeared
        from the review table and looked like a no-op.
        """

        self.initialize()
        message = " ".join(str(error or "").split())[:1000]
        if not message:
            message = "发送未开始：未知校验错误，未调用邮件或短信服务。"
        now = utc_now()
        with self.connect() as conn:
            source = conn.execute(
                "SELECT platform_order_no FROM shipment_notifications WHERE id = ?",
                (int(notification_id),),
            ).fetchone()
            if source is None:
                return None
            latest = conn.execute(
                """
                SELECT id, state, provider_message_id, sent_at
                FROM shipment_notifications
                WHERE platform_order_no = ? AND legacy_email_batch_id IS NULL
                ORDER BY revision DESC, id DESC
                LIMIT 1
                """,
                (str(source["platform_order_no"]),),
            ).fetchone()
            if (
                latest is None
                or latest["state"] != NOTIFICATION_AWAITING_REVIEW
                or str(latest["provider_message_id"] or "").strip()
                or str(latest["sent_at"] or "").strip()
            ):
                latest_id = int(latest["id"]) if latest is not None else 0
            else:
                latest_id = int(latest["id"])
                conn.execute(
                    """
                    UPDATE shipment_notifications
                    SET last_error = ?, state_changed_at = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        message,
                        now,
                        now,
                        latest_id,
                        NOTIFICATION_AWAITING_REVIEW,
                    ),
                )
                conn.commit()
        return self.get_notification(latest_id) if latest_id else None

    def refresh_current_unsent_product_titles(
        self,
        configuration: NotificationConfiguration,
    ) -> int:
        """Apply the five-word product rule once to existing non-WC unsent drafts."""

        self.initialize()
        marker_key = "product_title_five_words_v1_applied"
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = conn.execute(
                "SELECT 1 FROM shipment_notification_runtime_state WHERE state_key = ?",
                (marker_key,),
            ).fetchone()
            if marker is not None:
                conn.rollback()
                return 0
            product_rows = conn.execute(
                """
                SELECT id, platform_order_no, raw_title, display_title
                FROM shipment_order_product_snapshots
                WHERE active = 1
                """
            ).fetchall()
            for row in product_rows:
                if INDEPENDENT_SITE_ORDER_RE.fullmatch(
                    str(row["platform_order_no"] or "").strip()
                ):
                    continue
                shortened = shorten_product_title(str(row["raw_title"] or ""))
                if shortened and shortened != str(row["display_title"] or ""):
                    conn.execute(
                        "UPDATE shipment_order_product_snapshots "
                        "SET display_title = ?, updated_at = ? WHERE id = ?",
                        (shortened, now, int(row["id"])),
                    )
            rows = conn.execute(
                """
                SELECT current.platform_order_no
                FROM shipment_notifications AS current
                WHERE current.legacy_email_batch_id IS NULL
                  AND current.state IN (?, ?, ?, ?, ?)
                  AND TRIM(COALESCE(current.provider_message_id, '')) = ''
                  AND TRIM(COALESCE(current.sent_at, '')) = ''
                  AND current.id = (
                      SELECT MAX(latest.id)
                      FROM shipment_notifications AS latest
                      WHERE latest.platform_order_no = current.platform_order_no
                        AND latest.legacy_email_batch_id IS NULL
                  )
                ORDER BY current.id
                """,
                (
                    NOTIFICATION_AWAITING_REVIEW,
                    NOTIFICATION_BLOCKED,
                    NOTIFICATION_WAITING_CONTACT,
                    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
                    NOTIFICATION_RETRYABLE,
                ),
            ).fetchall()
            platforms = [
                str(row["platform_order_no"] or "").strip()
                for row in rows
                if not INDEPENDENT_SITE_ORDER_RE.fullmatch(
                    str(row["platform_order_no"] or "").strip()
                )
            ]
            conn.commit()
        refreshed = 0
        for platform in platforms:
            before = self.get_latest_notification(platform)
            prepared = self.prepare_notification(platform, configuration)
            if prepared is not None and (
                before is None or int(prepared["id"]) != int(before["id"])
            ):
                refreshed += 1
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO shipment_notification_runtime_state (
                    state_key, state_value, updated_at
                ) VALUES (?, ?, ?)
                """,
                (marker_key, str(refreshed), utc_now()),
            )
            conn.commit()
        return refreshed

    def get_latest_notification(
        self,
        platform_order_no: str,
    ) -> dict[str, Any] | None:
        """Return the latest non-legacy revision for one platform order."""

        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id
                FROM shipment_notifications
                WHERE platform_order_no = ? AND legacy_email_batch_id IS NULL
                ORDER BY revision DESC, id DESC
                LIMIT 1
                """,
                (platform_order_no.strip(),),
            ).fetchone()
        return self.get_notification(int(row[0])) if row is not None else None

    def list_notifications(
        self,
        *,
        states: Iterable[str] | None = None,
        include_legacy: bool = False,
        latest_only: bool = True,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses = [] if include_legacy else ["legacy_email_batch_id IS NULL"]
        params: list[Any] = []
        normalized_states = tuple(dict.fromkeys(states or ()))
        if normalized_states:
            clauses.append(f"state IN ({','.join('?' for _ in normalized_states)})")
            params.extend(normalized_states)
        if latest_only:
            clauses.append(
                "id IN (SELECT MAX(id) FROM shipment_notifications "
                "WHERE legacy_email_batch_id IS NULL GROUP BY platform_order_no)"
            )
        sql = "SELECT id FROM shipment_notifications"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC"
        with self.connect() as conn:
            ids = [int(row[0]) for row in conn.execute(sql, params).fetchall()]
        return [item for item in (self.get_notification(row_id) for row_id in ids) if item]

    def edit_contact_and_prepare(
        self,
        platform_order_no: str,
        *,
        email: str,
        phone: str,
        configuration: NotificationConfiguration,
    ) -> dict[str, Any] | None:
        platform = str(platform_order_no or "").strip()
        if not platform:
            raise NotificationStateError("平台单号不能为空。")
        raw_email = str(email or "").strip()
        raw_phone = str(phone or "").strip()
        normalized_email = normalize_email(raw_email) if raw_email else None
        normalized_phone = normalize_phone(raw_phone) if raw_phone else None
        if raw_email and not normalized_email:
            raise NotificationStateError("邮箱格式无效，请检查后重试。")
        if raw_phone and not normalized_phone:
            raise NotificationStateError("电话格式无效，请填写包含国家区号的有效号码。")
        if not normalized_email and not normalized_phone:
            raise NotificationStateError("邮箱和电话不能同时为空。")

        existing = self.get_contact(platform) or OrderContact(
            platform_order_no=platform
        )
        self.upsert_contact(
            replace(
                existing,
                email=normalized_email or "",
                email_presence=(
                    EMAIL_PRESENCE_PROVIDED
                    if normalized_email
                    else EMAIL_PRESENCE_NOT_PROVIDED
                ),
                phone_raw=normalized_phone or "",
                source=CONTACT_SOURCE_DESKTOP_MANUAL,
                email_source=CONTACT_SOURCE_DESKTOP_MANUAL,
                phone_source=CONTACT_SOURCE_DESKTOP_MANUAL,
                verified_phone_e164=normalized_phone or "",
                phone_verification_state=(
                    PHONE_VERIFICATION_MATCHED
                    if normalized_phone
                    else PHONE_VERIFICATION_MISSING
                ),
                captured_at="",
            )
        )
        return self.prepare_notification(platform, configuration)

    def _claim(
        self,
        notification_id: int,
        configuration: NotificationConfiguration,
        *,
        retry: bool,
        actor: str,
    ) -> dict[str, Any]:
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM shipment_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise NotificationStateError("Notification does not exist.")
            if INDEPENDENT_SITE_ORDER_RE.fullmatch(
                str(row["platform_order_no"] or "").strip()
            ):
                conn.rollback()
                raise NotificationStateError(
                    "Independent-site customer notifications are disabled."
                )
            expected_state = NOTIFICATION_RETRYABLE if retry else NOTIFICATION_AWAITING_REVIEW
            if row["state"] != expected_state:
                conn.rollback()
                raise NotificationStateError(
                    f"Notification state must be {expected_state}, got {row['state']}."
                )
            (
                rendered,
                packages,
                queue_total,
                queue_complete,
                _erp_completed_at,
                contact_ready,
                contact,
            ) = self._render_current_conn(
                conn, str(row["platform_order_no"]), configuration
            )
            stale = (
                rendered is None
                or not contact_ready
                or queue_total != int(row["queue_total"])
                or queue_complete != int(row["queue_complete"])
            )
            if not stale and rendered is not None:
                business_unchanged = (
                    contact is not None
                    and self._matches_stored_business_snapshot_conn(
                        conn,
                        row,
                        rendered,
                        packages,
                        queue_total=queue_total,
                        queue_complete=queue_complete,
                        contact=contact,
                    )
                )
                if retry:
                    stale = not business_unchanged
                else:
                    stale = not business_unchanged or not self._matches_stored_delivery_content(
                        row,
                        rendered,
                    )
            if stale:
                conn.rollback()
                self.prepare_notification(str(row["platform_order_no"]), configuration)
                raise StaleNotificationError(
                    "Notification content changed and must be reviewed again."
                )
            if rendered.blocked_reasons:
                conn.execute(
                    "UPDATE shipment_notifications SET state = ?, last_error = ?, "
                    "state_changed_at = ?, updated_at = ? WHERE id = ?",
                    (
                        NOTIFICATION_BLOCKED,
                        ",".join(rendered.blocked_reasons),
                        now,
                        now,
                        notification_id,
                    ),
                )
                conn.commit()
                raise NotificationStateError("Notification is not sendable.")
            if retry:
                if row["approved_content_hash"] != row["content_hash"]:
                    conn.rollback()
                    raise StaleNotificationError("Approved content hash is no longer valid.")
                action = "RETRY_APPROVED_CONTENT"
            else:
                action = "APPROVE_AND_SEND"
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, actor, note, created_at
                    ) VALUES (?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        notification_id,
                        row["revision"],
                        action,
                        row["content_hash"],
                        actor,
                        now,
                    ),
                )
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, approved_content_hash = ?, approved_at = COALESCE(approved_at, ?),
                    attempt_count = attempt_count + 1, last_error = NULL,
                    state_changed_at = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    NOTIFICATION_SENDING,
                    row["content_hash"],
                    now,
                    now,
                    now,
                    notification_id,
                    expected_state,
                ),
            ).rowcount
            if updated != 1:
                conn.rollback()
                raise NotificationStateError("Notification was claimed by another process.")
            conn.commit()
        claimed = self.get_notification(notification_id)
        if claimed is None:
            raise NotificationStateError("Claimed notification disappeared.")
        return claimed

    def approve_and_claim(
        self,
        notification_id: int,
        configuration: NotificationConfiguration,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        return self._claim(
            notification_id, configuration, retry=False, actor=actor
        )

    def retry_approved_and_claim(
        self,
        notification_id: int,
        configuration: NotificationConfiguration,
        *,
        actor: str = "desktop_user",
    ) -> dict[str, Any]:
        return self._claim(notification_id, configuration, retry=True, actor=actor)

    def reject(
        self, notification_id: int, *, actor: str = "desktop_user", note: str = ""
    ) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM shipment_notifications WHERE id = ?", (notification_id,)
            ).fetchone()
            if row is None or row["state"] not in {
                NOTIFICATION_AWAITING_REVIEW,
                NOTIFICATION_BLOCKED,
                NOTIFICATION_WAITING_CONTACT,
                NOTIFICATION_MANUAL_EMAIL_REQUIRED,
            }:
                conn.rollback()
                raise NotificationStateError("Only an unapproved notification can be rejected.")
            conn.execute(
                "UPDATE shipment_notifications SET state = ?, state_changed_at = ?, "
                "updated_at = ? WHERE id = ?",
                (NOTIFICATION_REJECTED, now, now, notification_id),
            )
            conn.execute(
                """
                INSERT INTO shipment_notification_reviews (
                    notification_id, revision, action, content_hash, actor, note, created_at
                ) VALUES (?, ?, 'REJECT', ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    row["revision"],
                    row["content_hash"],
                    actor,
                    note.strip(),
                    now,
                ),
            )
            conn.commit()

    def mark_manually_completed(
        self,
        notification_ids: Sequence[int],
        *,
        actor: str = "desktop_user",
        note: str = "",
    ) -> dict[str, int]:
        """Close latest notifications after an operator verifies manual completion.

        A provider may already have accepted the message without confirming
        delivery.  That is precisely when an operator needs to reconcile the
        order manually, so sent/accepted/error states must not be excluded.
        Immutable attempts and review history retain the original provider
        evidence; only the current business state is closed here.
        """

        self.initialize()
        ids = tuple(dict.fromkeys(int(value) for value in notification_ids if int(value) > 0))
        reason = note.strip()
        if not ids:
            raise NotificationStateError("At least one notification is required.")
        if not reason:
            raise NotificationStateError("A manual completion reason is required.")
        now = utc_now()
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT n.*
                FROM shipment_notifications n
                WHERE n.id IN ({placeholders})
                  AND n.legacy_email_batch_id IS NULL
                  AND n.id = (
                      SELECT MAX(latest.id)
                      FROM shipment_notifications latest
                      WHERE latest.platform_order_no = n.platform_order_no
                        AND latest.legacy_email_batch_id IS NULL
                  )
                """,
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                conn.rollback()
                raise NotificationStateError(
                    "Every notification must exist and be the latest revision for its order."
                )
            invalid = [
                row
                for row in rows
                if row["state"] == NOTIFICATION_MANUALLY_COMPLETED
            ]
            if invalid:
                conn.rollback()
                raise NotificationStateError(
                    "Notifications already manually completed cannot be completed again."
                )
            for row in rows:
                conn.execute(
                    """
                    UPDATE shipment_notifications
                    SET state = ?, provider_status = CASE
                            WHEN TRIM(COALESCE(provider_status, '')) = ''
                                THEN 'MANUAL_COMPLETION'
                            ELSE provider_status
                        END,
                        last_error = NULL, state_changed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (NOTIFICATION_MANUALLY_COMPLETED, now, now, row["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, actor, note, created_at
                    ) VALUES (?, ?, 'MANUAL_COMPLETION', ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["revision"],
                        row["content_hash"],
                        actor,
                        reason,
                        now,
                    ),
                )
                self._mark_package_events_handled_conn(conn, int(row["id"]))
            conn.commit()
        return {"completed": len(rows)}

    def cancel_notifications(
        self,
        notification_ids: Sequence[int],
        *,
        actor: str = "desktop_user",
        note: str,
    ) -> dict[str, int]:
        """Persistently cancel latest unsent notifications without external calls."""

        self.initialize()
        ids = tuple(dict.fromkeys(int(value) for value in notification_ids if int(value) > 0))
        reason = note.strip()
        if not ids:
            raise NotificationStateError("At least one notification is required.")
        if not reason:
            raise NotificationStateError("A cancellation reason is required.")
        allowed_states = {
            NOTIFICATION_AWAITING_REVIEW,
            NOTIFICATION_BLOCKED,
            NOTIFICATION_REJECTED,
            NOTIFICATION_WAITING_CONTACT,
            NOTIFICATION_MANUAL_EMAIL_REQUIRED,
            NOTIFICATION_RETRYABLE,
            NOTIFICATION_FAILED,
        }
        now = utc_now()
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT n.*
                FROM shipment_notifications n
                WHERE n.id IN ({placeholders})
                  AND n.legacy_email_batch_id IS NULL
                  AND n.id = (
                      SELECT MAX(latest.id)
                      FROM shipment_notifications latest
                      WHERE latest.platform_order_no = n.platform_order_no
                        AND latest.legacy_email_batch_id IS NULL
                  )
                """,
                ids,
            ).fetchall()
            if len(rows) != len(ids):
                conn.rollback()
                raise NotificationStateError(
                    "Every notification must exist and be the latest revision for its order."
                )
            invalid = [
                row
                for row in rows
                if row["state"] not in allowed_states
                or row["provider_message_id"] is not None
                or row["sent_at"] is not None
            ]
            if invalid:
                conn.rollback()
                raise NotificationStateError(
                    "Only latest unsent notifications can be cancelled."
                )
            for row in rows:
                conn.execute(
                    """
                    UPDATE shipment_notifications
                    SET state = ?, provider_status = 'MANUAL_CANCELLATION',
                        last_error = NULL, state_changed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (NOTIFICATION_CANCELLED, now, now, row["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, actor, note, created_at
                    ) VALUES (?, ?, 'CANCEL', ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["revision"],
                        row["content_hash"],
                        actor,
                        reason,
                        now,
                    ),
                )
            conn.commit()
        return {"cancelled": len(rows)}

    def exclude_and_delete_platforms(
        self,
        platform_order_nos: Sequence[str],
        *,
        reason: str,
    ) -> dict[str, int]:
        """Remove visible notification data and prevent historical regeneration."""

        self.initialize()
        platforms = tuple(
            dict.fromkeys(str(value or "").strip() for value in platform_order_nos)
        )
        platforms = tuple(value for value in platforms if value)
        if not platforms:
            return {
                "excluded": 0,
                "notifications_deleted": 0,
                "contacts_deleted": 0,
                "packages_deleted": 0,
            }
        note = reason.strip()
        if not note:
            raise ValueError("reason is required")
        now = utc_now()
        placeholders = ",".join("?" for _ in platforms)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before_exclusions = conn.total_changes
            for platform in platforms:
                conn.execute(
                    """
                    INSERT INTO shipment_notification_exclusions (
                        platform_order_no, reason, excluded_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(platform_order_no) DO NOTHING
                    """,
                    (platform, note, now),
                )
            excluded = conn.total_changes - before_exclusions
            notifications_deleted = conn.execute(
                f"DELETE FROM shipment_notifications "
                f"WHERE legacy_email_batch_id IS NULL "
                f"AND platform_order_no IN ({placeholders})",
                platforms,
            ).rowcount
            contacts_deleted = conn.execute(
                f"DELETE FROM shipment_order_contacts "
                f"WHERE platform_order_no IN ({placeholders})",
                platforms,
            ).rowcount
            packages_deleted = conn.execute(
                f"DELETE FROM shipment_package_snapshots "
                f"WHERE platform_order_no IN ({placeholders})",
                platforms,
            ).rowcount
            conn.execute(
                f"DELETE FROM shipment_order_product_snapshots "
                f"WHERE platform_order_no IN ({placeholders})",
                platforms,
            )
            conn.commit()
        return {
            "excluded": int(excluded),
            "notifications_deleted": int(notifications_deleted),
            "contacts_deleted": int(contacts_deleted),
            "packages_deleted": int(packages_deleted),
        }

    def reopen_for_review(
        self,
        notification_id: int,
        configuration: NotificationConfiguration,
        *,
        actor: str = "desktop_user",
        note: str,
    ) -> dict[str, Any]:
        """Create a new review revision while preserving all prior delivery history."""

        current = self.get_notification(notification_id)
        if current is None:
            raise NotificationStateError("Notification does not exist.")
        reopened = self.prepare_notification(
            str(current["platform_order_no"]),
            configuration,
            force_reopen_notification_id=notification_id,
            reopen_actor=actor,
            reopen_note=note,
        )
        if reopened is None:
            raise NotificationStateError("Notification is not currently eligible for review.")
        return reopened

    def resubmit(
        self,
        notification_id: int,
        configuration: NotificationConfiguration,
        *,
        actor: str = "desktop_user",
        note: str,
    ) -> dict[str, Any]:
        """Compatibility alias for the explicit version-preserving reopen workflow."""

        return self.reopen_for_review(
            notification_id,
            configuration,
            actor=actor,
            note=note,
        )

    def finalize_send(
        self,
        notification_id: int,
        *,
        accepted: bool,
        provider_message_id: str = "",
        provider_status: str = "",
        retryable: bool = False,
        error: str = "",
        provider_operator_email: str = "",
    ) -> None:
        self.initialize()
        now_dt = datetime.now(timezone.utc)
        now = _format_utc(now_dt)
        receipt_next_check_at = _next_receipt_check(now, after=now_dt)
        receipt_deadline_at = _format_utc(
            now_dt + timedelta(hours=_RECEIPT_DEADLINE_HOURS)
        )
        if accepted:
            state = NOTIFICATION_ACCEPTED
        elif retryable:
            state = NOTIFICATION_RETRYABLE
        else:
            state = NOTIFICATION_FAILED
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, provider_message_id = NULLIF(?, ''), provider_status = ?,
                    last_error = NULLIF(?, ''), sent_at = CASE WHEN ? THEN ? ELSE sent_at END,
                    provider_operator_email = CASE WHEN ? THEN ? ELSE provider_operator_email END,
                    receipt_next_check_at = CASE WHEN ? THEN ? ELSE '' END,
                    receipt_deadline_at = CASE WHEN ? THEN ? ELSE '' END,
                    receipt_last_checked_at = CASE WHEN ? THEN '' ELSE receipt_last_checked_at END,
                    receipt_check_attempt_count = CASE
                        WHEN ? THEN 0 ELSE receipt_check_attempt_count
                    END,
                    receipt_check_lease_owner = '', receipt_check_lease_until = '',
                    state_changed_at = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    state,
                    provider_message_id,
                    provider_status,
                    error,
                    int(accepted),
                    now,
                    int(accepted),
                    str(provider_operator_email or "").strip().casefold(),
                    int(accepted),
                    receipt_next_check_at,
                    int(accepted),
                    receipt_deadline_at,
                    int(accepted),
                    int(accepted),
                    now,
                    now,
                    notification_id,
                    NOTIFICATION_SENDING,
                ),
            ).rowcount
            if updated == 1 and accepted:
                self._mark_package_events_handled_conn(conn, notification_id)
            conn.commit()
        if updated != 1:
            raise NotificationStateError("Notification is not in SENDING state.")

    def mark_delivered(self, notification_id: int, *, provider_status: str) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, provider_status = ?, delivered_at = COALESCE(delivered_at, ?),
                    last_error = NULL,
                    receipt_next_check_at = '', receipt_check_lease_owner = '',
                    receipt_check_lease_until = '',
                    state_changed_at = CASE WHEN state = ? THEN state_changed_at ELSE ? END,
                    updated_at = CASE WHEN state = ? THEN updated_at ELSE ? END
                WHERE id = ? AND state IN (?, ?, ?, ?, ?)
                """,
                (
                    NOTIFICATION_DELIVERED,
                    provider_status,
                    now,
                    NOTIFICATION_DELIVERED,
                    now,
                    NOTIFICATION_DELIVERED,
                    now,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_FAILED,
                    NOTIFICATION_RETRYABLE,
                    NOTIFICATION_DELIVERY_UNCONFIRMED,
                ),
            ).rowcount
            conn.commit()
        if updated != 1:
            raise NotificationStateError(
                "Only an accepted or delivered notification can be marked delivered."
            )

    def update_provider_status(self, notification_id: int, *, provider_status: str) -> None:
        self.initialize()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET provider_status = ?
                WHERE id = ? AND state IN (?, ?, ?, ?)
                """,
                (
                    provider_status,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_FAILED,
                    NOTIFICATION_DELIVERY_UNCONFIRMED,
                ),
            ).rowcount
            conn.commit()
        if updated != 1:
            raise NotificationStateError(
                "Provider status can only be updated after provider acceptance."
            )

    def update_provider_message_id(
        self, notification_id: int, *, provider_message_id: str
    ) -> None:
        self.initialize()
        value = str(provider_message_id or "").strip()
        if not value:
            raise NotificationStateError("Provider message id cannot be blank.")
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET provider_message_id = ?
                WHERE id = ? AND state IN (?, ?, ?, ?)
                """,
                (
                    value,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_FAILED,
                    NOTIFICATION_DELIVERY_UNCONFIRMED,
                ),
            ).rowcount
            conn.commit()
        if updated != 1:
            raise NotificationStateError(
                "Provider message id can only be updated after provider acceptance."
            )

    def mark_delivery_failed(
        self,
        notification_id: int,
        *,
        provider_status: str,
        error: str = "",
    ) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, provider_status = ?,
                    last_error = ?,
                    receipt_next_check_at = '', receipt_check_lease_owner = '',
                    receipt_check_lease_until = '',
                    state_changed_at = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?, ?)
                """,
                (
                    NOTIFICATION_RETRYABLE,
                    provider_status,
                    error.strip()
                    or "发送失败：供应商明确返回通知未能送达。可核对联系方式后重试已批准内容。",
                    now,
                    now,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_FAILED,
                    NOTIFICATION_DELIVERY_UNCONFIRMED,
                ),
            ).rowcount
            conn.commit()
        if updated != 1:
            raise NotificationStateError(
                "Only an accepted notification can report delivery failure."
            )

    def mark_delivery_status_check_failed(
        self,
        notification_id: int,
        *,
        provider_status: str,
        error: str,
    ) -> None:
        """Record an exhausted status check without claiming the send failed."""

        self.initialize()
        message = str(error or "").strip()
        if not message:
            raise NotificationStateError("A delivery status failure reason is required.")
        now = utc_now()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, provider_status = ?, last_error = ?,
                    state_changed_at = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?)
                  AND provider_message_id IS NOT NULL
                  AND provider_message_id <> ''
                """,
                (
                    NOTIFICATION_FAILED,
                    provider_status,
                    message,
                    now,
                    now,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_FAILED,
                ),
            ).rowcount
            conn.commit()
        if updated != 1:
            raise NotificationStateError(
                "Only a provider-accepted notification can fail status confirmation."
            )

    def claim_receipt_check(
        self,
        notification_id: int,
        *,
        owner: str,
        due_only: bool = False,
        lease_seconds: int = 120,
    ) -> bool:
        """Atomically lease one receipt lookup so manual and background checks cannot race."""

        self.initialize()
        claim_owner = str(owner or "").strip()
        if not claim_owner:
            raise NotificationStateError("A receipt check owner is required.")
        now_dt = datetime.now(timezone.utc)
        now = _format_utc(now_dt)
        lease_until = _format_utc(now_dt + timedelta(seconds=max(30, lease_seconds)))
        due_clause = (
            "AND (receipt_next_check_at = '' OR receipt_next_check_at <= ?)"
            if due_only
            else ""
        )
        params: list[Any] = [claim_owner, lease_until, now, notification_id]
        if due_only:
            params.append(now)
        params.extend(
            (
                NOTIFICATION_ACCEPTED,
                NOTIFICATION_FAILED,
                NOTIFICATION_DELIVERY_UNCONFIRMED,
            )
        )
        with self.connect() as conn:
            updated = conn.execute(
                f"""
                UPDATE shipment_notifications
                SET receipt_check_lease_owner = ?, receipt_check_lease_until = ?,
                    updated_at = ?
                WHERE id = ? {due_clause}
                  AND state IN (?, ?, ?)
                  AND TRIM(COALESCE(provider_message_id, '')) <> ''
                  AND (
                      receipt_check_lease_owner = ''
                      OR receipt_check_lease_until = ''
                      OR receipt_check_lease_until <= ?
                      OR receipt_check_lease_owner = ?
                  )
                """,
                (*params, now, claim_owner),
            ).rowcount
            conn.commit()
        return updated == 1

    def claim_due_receipt_checks(
        self,
        *,
        owner: str,
        operator_email: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Lease due accepted notifications, including a one-time legacy reconciliation."""

        self.initialize()
        now = utc_now()
        operator = str(operator_email or "").strip().casefold()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM shipment_notifications
                WHERE state IN (?, ?)
                  AND TRIM(COALESCE(provider_message_id, '')) <> ''
                  AND (receipt_next_check_at = '' OR receipt_next_check_at <= ?)
                  AND (receipt_check_lease_until = '' OR receipt_check_lease_until <= ?)
                  AND (
                      ? = ''
                      OR provider_operator_email = ''
                      OR LOWER(provider_operator_email) = ?
                  )
                  AND (
                      state = ?
                      OR last_error LIKE '状态核验超时：%'
                      OR last_error LIKE '状态查询失败：%'
                  )
                ORDER BY CASE WHEN receipt_next_check_at = '' THEN 0 ELSE 1 END,
                         receipt_next_check_at, id
                LIMIT ?
                """,
                (
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_FAILED,
                    now,
                    now,
                    operator,
                    operator,
                    NOTIFICATION_ACCEPTED,
                    max(1, min(int(limit), 500)),
                ),
            ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            notification_id = int(row[0])
            if not self.claim_receipt_check(
                notification_id,
                owner=owner,
                due_only=True,
            ):
                continue
            notification = self.get_notification(notification_id)
            if notification is not None:
                claimed.append(notification)
        return claimed

    def finish_receipt_check(
        self,
        notification_id: int,
        *,
        owner: str,
        query_succeeded: bool,
    ) -> dict[str, Any] | None:
        """Persist one check and schedule the next checkpoint through the 24-hour deadline."""

        self.initialize()
        claim_owner = str(owner or "").strip()
        now_dt = datetime.now(timezone.utc)
        now = _format_utc(now_dt)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM shipment_notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            state = str(row["state"] or "")
            stored_owner = str(row["receipt_check_lease_owner"] or "")
            if stored_owner != claim_owner and not (
                not stored_owner
                and state in {NOTIFICATION_DELIVERED, NOTIFICATION_RETRYABLE}
            ):
                conn.rollback()
                return self.get_notification(notification_id)
            deadline = _parse_utc(str(row["receipt_deadline_at"] or "")) or _receipt_deadline(
                str(row["sent_at"] or ""), fallback=now_dt
            )
            next_check = ""
            next_state = state
            last_error = row["last_error"]
            state_changed_at = str(row["state_changed_at"] or now)
            if state in {NOTIFICATION_DELIVERED, NOTIFICATION_RETRYABLE}:
                pass
            elif state == NOTIFICATION_DELIVERY_UNCONFIRMED:
                last_error = (
                    "发送服务已接收，但 24 小时内未确认送达；这不代表发送失败，"
                    "系统不会自动重发。"
                )
            elif now_dt >= deadline:
                next_state = NOTIFICATION_DELIVERY_UNCONFIRMED
                state_changed_at = now
                last_error = (
                    "发送服务已接收，但 24 小时内未确认送达；这不代表发送失败，"
                    "系统不会自动重发。"
                )
            else:
                next_check = _next_receipt_check(str(row["sent_at"] or ""), after=now_dt)
                if state == NOTIFICATION_FAILED and query_succeeded:
                    next_state = NOTIFICATION_ACCEPTED
                    state_changed_at = now
                    last_error = None
            conn.execute(
                """
                UPDATE shipment_notifications
                SET state = ?, last_error = ?, receipt_last_checked_at = ?,
                    receipt_deadline_at = ?, receipt_next_check_at = ?,
                    receipt_check_attempt_count = receipt_check_attempt_count + 1,
                    receipt_check_lease_owner = '', receipt_check_lease_until = '',
                    state_changed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_state,
                    last_error,
                    now,
                    _format_utc(deadline),
                    next_check,
                    state_changed_at,
                    now,
                    notification_id,
                ),
            )
            conn.commit()
        return self.get_notification(notification_id)


__all__ = [
    "NotificationStateError",
    "ShipmentNotificationStore",
    "StaleNotificationError",
    "initialize_notification_schema",
]
