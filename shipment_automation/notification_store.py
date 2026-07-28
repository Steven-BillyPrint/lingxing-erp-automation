from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .notification_domain import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
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
    NOTIFICATION_FAILED,
    NOTIFICATION_MANUALLY_COMPLETED,
    NOTIFICATION_REJECTED,
    NOTIFICATION_RETRYABLE,
    NOTIFICATION_SENDING,
    NOTIFICATION_WAITING_CONTACT,
    PACKAGE_UNKNOWN,
    NotificationConfiguration,
    OrderContact,
    OrderProductSnapshot,
    PackageSnapshot,
    RenderedNotification,
    analyze_order_products,
    normalize_email,
    normalize_phone,
    normalize_recipient_name,
    render_notification,
    stable_package_label,
    tracking_url_for,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
        "contact_captured_at",
    ):
        if column not in contact_columns:
            conn.execute(
                f"ALTER TABLE shipment_order_contacts "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
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
    conn.execute(
        "UPDATE shipment_notifications SET state_changed_at = updated_at "
        "WHERE state_changed_at = ''"
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


class ShipmentNotificationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            initialize_notification_schema(conn)
            conn.commit()

    def notification_scan_targets(
        self,
        platform_order_nos: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return only platform orders whose entire local queue is ERP complete."""

        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.platform_order_no,
                       COUNT(*) AS queue_total,
                       SUM(CASE WHEN j.identity_state = 'ACTIVE'
                                      AND e.state = 'DONE'
                                      AND e.checkpoint = 'OUTBOUNDED'
                                THEN 1 ELSE 0 END) AS queue_complete,
                       GROUP_CONCAT(DISTINCT j.system_order_no) AS system_order_nos,
                       MAX(COALESCE(e.outbounded_at, e.externally_completed_at, e.updated_at))
                           AS erp_completed_at
                FROM shipment_jobs j
                JOIN shipment_erp e ON e.job_id = j.id
                LEFT JOIN shipment_notification_exclusions x
                       ON x.platform_order_no = j.platform_order_no
                WHERE j.identity_state <> 'CANCELLED'
                  AND x.platform_order_no IS NULL
                GROUP BY j.platform_order_no
                HAVING COUNT(*) > 0
                   AND COUNT(*) = SUM(
                       CASE WHEN j.identity_state = 'ACTIVE'
                                  AND e.state = 'DONE'
                                  AND e.checkpoint = 'OUTBOUNDED'
                            THEN 1 ELSE 0 END
                   )
                ORDER BY j.platform_order_no
                """
            ).fetchall()
        targets = [
            {
                "platform_order_no": str(row[0]),
                "queue_total": int(row[1]),
                "queue_complete": int(row[2]),
                "system_order_nos": tuple(
                    value for value in str(row[3] or "").split(",") if value
                ),
                "erp_completed_at": str(row[4] or "").strip(),
            }
            for row in rows
            if not INDEPENDENT_SITE_ORDER_RE.match(str(row[0] or "").strip())
        ]
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
                        email_source = ?, phone_source = ?, contact_captured_at = ?,
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
                        contact_captured_at, system_order_nos_json, contact_updated_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def upsert_customization_contact(
        self,
        platform_order_no: str,
        *,
        email: str = "",
        phone: str = "",
        system_order_nos: Sequence[str] = (),
    ) -> bool:
        """Persist JSON-authoritative destinations while retaining the WMS name."""

        platform = str(platform_order_no or "").strip()
        existing = self.get_contact(platform)
        previous_orders = existing.system_order_nos if existing is not None else ()
        orders = tuple(dict.fromkeys([*previous_orders, *system_order_nos]))
        normalized_email = str(email or "").strip()
        normalized_phone = str(phone or "").strip()
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
                email=normalized_email,
                email_presence=(
                    EMAIL_PRESENCE_PROVIDED
                    if normalized_email
                    else EMAIL_PRESENCE_NOT_PROVIDED
                ),
                phone_raw=normalized_phone,
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
                email_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                phone_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                captured_at=captured_at,
                system_order_nos=orders,
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
        next_email = normalized_email if normalized_email is not None else (
            existing.email if existing is not None else ""
        )
        next_phone = normalized_phone if normalized_phone is not None else (
            existing.phone_raw if existing is not None else ""
        )
        email_source = (
            CONTACT_SOURCE_LINGXING_DETAIL_REFRESH
            if normalized_email is not None
            else (existing.email_source if existing is not None else "")
        )
        phone_source = (
            CONTACT_SOURCE_LINGXING_DETAIL_REFRESH
            if normalized_phone is not None
            else (existing.phone_source if existing is not None else "")
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
        as customization-JSON authoritative is retained verbatim, including
        an explicitly empty value, so the fallback can never overwrite what
        the buyer entered in the customization flow.
        """

        platform = str(platform_order_no or "").strip()
        if not platform:
            raise ValueError("platform_order_no is required")
        existing = self.get_contact(platform)
        existing_email_is_json = bool(
            existing is not None
            and existing.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
        )
        existing_phone_is_json = bool(
            existing is not None
            and existing.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
        )
        normalized_email = normalize_email(email) if email is not None else None
        normalized_phone = normalize_phone(phone) if phone is not None else None

        if existing_email_is_json:
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

        if existing_phone_is_json:
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
                    CONTACT_SOURCE_CUSTOMIZATION_JSON
                    if existing is not None
                    and existing.source == CONTACT_SOURCE_CUSTOMIZATION_JSON
                    else CONTACT_SOURCE_LINGXING_API_FALLBACK
                ),
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
                        waybill_no, tracking_no, final_tracking_no, customer_visible,
                        visibility_reason, source_payload_hash, active, first_seen_at,
                        last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
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
                    final_tracking_no = ?, customer_visible = ?, visibility_reason = ?,
                    source_payload_hash = ?, active = 1, last_seen_at = ?, updated_at = ?
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
                "customer_visible "
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
            and str(row["carrier_normalized"] or "").strip()
            and str(row["final_tracking_no"] or "").strip()
        )
        total = sum(1 for row in active if bool(row["customer_visible"])) + len(
            missing_systems
        )
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
                stable_sequence=int(row["stable_sequence"]),
                stable_label=str(row["stable_label"]),
                source_payload_hash=str(row["source_payload_hash"] or ""),
                customer_visible=bool(row["customer_visible"]),
                visibility_reason=str(row["visibility_reason"] or ""),
            )
            for row in rows
        ]

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
    def _with_pending_package_placeholders(
        platform_order_no: str,
        packages: Sequence[PackageSnapshot],
        products: Sequence[OrderProductSnapshot],
        expected_system_order_nos: Sequence[str],
    ) -> list[PackageSnapshot]:
        """Materialize missing WMS systems for the immutable review snapshot."""

        output = list(packages)
        expected = tuple(
            dict.fromkeys(
                str(value or "").strip()
                for value in expected_system_order_nos
                if str(value or "").strip()
            )
        )
        if not expected:
            return output
        instruction_systems = set(
            analyze_order_products(
                products,
                expected_system_order_nos=expected,
            ).instruction_system_order_nos
        )
        observed_customer_systems = {
            item.system_order_no.strip()
            for item in output
            if item.customer_visible
            and item.system_order_no.strip()
            and item.system_order_no.strip() not in instruction_systems
        }
        next_sequence = max(
            (int(item.stable_sequence) for item in output),
            default=0,
        )
        for system_order_no in expected:
            if (
                system_order_no in instruction_systems
                or system_order_no in observed_customer_systems
            ):
                continue
            next_sequence += 1
            output.append(
                PackageSnapshot(
                    package_key=f"pending-wms:{system_order_no}",
                    platform_order_no=platform_order_no,
                    system_order_no=system_order_no,
                    shipment_type=PACKAGE_UNKNOWN,
                    stable_sequence=next_sequence,
                    stable_label="",
                    customer_visible=True,
                    visibility_reason="pending_wms",
                )
            )
        return output

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
                            THEN 1 ELSE 0 END) AS complete
                   ,MAX(COALESCE(e.outbounded_at, e.externally_completed_at, e.updated_at))
                        AS erp_completed_at
            FROM shipment_jobs j
            JOIN shipment_erp e ON e.job_id = j.id
            WHERE j.platform_order_no = ? AND j.identity_state <> 'CANCELLED'
            """,
            (platform_order_no,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0), str(row[2] or "")

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
        queue_total, queue_complete, erp_completed_at = self._queue_counts_conn(
            conn, platform_order_no
        )
        if not queue_total or queue_total != queue_complete:
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
        packages = self._with_pending_package_placeholders(
            platform_order_no,
            packages,
            products,
            contact.system_order_nos,
        )
        rendered = render_notification(
            contact,
            packages,
            configuration,
            expected_system_order_nos=contact.system_order_nos,
            products=products,
        )
        # Customization JSON remains the first choice.  When no usable JSON is
        # available, the documented Lingxing order list (e-mail) and WMS sales
        # outbound list (phone) are trusted field-specific fallbacks.
        channel_source_is_trusted = bool(
            (
                rendered.channel == CHANNEL_EMAIL
                and contact.email_source
                in {
                    CONTACT_SOURCE_CUSTOMIZATION_JSON,
                    CONTACT_SOURCE_LINGXING_ORDER_LIST,
                }
            )
            or (
                rendered.channel == CHANNEL_SMS
                and contact.phone_source
                in {CONTACT_SOURCE_CUSTOMIZATION_JSON, CONTACT_SOURCE_WMS}
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
                   tracking_no, final_tracking_no, customer_visible,
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
                   tracking_no, final_tracking_no, is_complete
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
    ) -> dict[str, Any] | None:
        self.initialize()
        platform = platform_order_no.strip()
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
            if rendered.package_complete <= 0:
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
            if force_reopen:
                allowed_states = {
                    NOTIFICATION_REJECTED,
                    NOTIFICATION_BLOCKED,
                    NOTIFICATION_WAITING_CONTACT,
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
            if (
                not force_reopen
                and latest is not None
                and latest["state"] in {
                    NOTIFICATION_MANUALLY_COMPLETED,
                    NOTIFICATION_CANCELLED,
                }
            ):
                # A manually completed notification is an explicit historical opt-out.
                # Keep collecting contact/package snapshots, but never reopen the order
                # or create another sendable revision during later logistics scans.
                conn.rollback()
                return self.get_notification(int(latest["id"]))
            if not force_reopen and latest is not None and contact_snapshot is not None:
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
            ):
                conn.rollback()
                return self.get_notification(int(latest["id"]))
            revision = int(latest["revision"] if latest is not None else 0) + 1
            state = (
                NOTIFICATION_AWAITING_REVIEW
                if force_reopen
                else (
                    NOTIFICATION_WAITING_CONTACT
                    if not contact_ready
                    else (
                        NOTIFICATION_BLOCKED
                        if rendered.blocked_reasons
                        else NOTIFICATION_AWAITING_REVIEW
                    )
                )
            )
            if not force_reopen and latest is not None and latest["state"] in {
                NOTIFICATION_AWAITING_REVIEW,
                NOTIFICATION_BLOCKED,
                NOTIFICATION_RETRYABLE,
                NOTIFICATION_WAITING_CONTACT,
            }:
                conn.execute(
                    "UPDATE shipment_notifications SET state = ?, last_error = 'superseded', "
                    "state_changed_at = ?, updated_at = ? WHERE id = ?",
                    (NOTIFICATION_REJECTED, now, now, latest["id"]),
                )
                conn.execute(
                    """
                    INSERT INTO shipment_notification_reviews (
                        notification_id, revision, action, content_hash, note, created_at
                    ) VALUES (?, ?, 'INVALIDATED_BY_CHANGE', ?, '', ?)
                    """,
                    (latest["id"], latest["revision"], latest["content_hash"], now),
                )
            idempotency_key = hashlib.sha256(
                f"{platform}|{revision}|{rendered.content_hash}".encode("utf-8")
            ).hexdigest()
            if contact_snapshot is None:  # defensive: render_current always supplies one
                conn.rollback()
                return None
            state_changed_at = erp_completed_at if latest is None else now
            last_error = (
                "recipient_contact_unavailable"
                if state == NOTIFICATION_WAITING_CONTACT
                else ",".join(rendered.blocked_reasons) or None
            )
            conn.execute(
                """
                INSERT INTO shipment_notifications (
                    platform_order_no, revision, channel, state, recipient_name,
                    recipient_email, email_presence, recipient_phone, sales_platform_code,
                    sales_platform_name, store_name, site_name, target, sender_email, subject,
                    body, body_html, sms_encoding, sms_character_count, sms_segment_count,
                    package_total, package_complete, package_missing, product_names_json,
                    queue_total,
                    queue_complete, template_version, content_hash, idempotency_key,
                    last_error, erp_completed_at, state_changed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    revision,
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
                        final_tracking_no, tracking_url, customer_visible,
                        visibility_reason, is_complete
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                  AND state = ?
                LIMIT 1
                """,
                (
                    str(row["platform_order_no"]),
                    int(row["revision"]),
                    NOTIFICATION_DELIVERED,
                ),
            ).fetchone()
        result = dict(row)
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
        del platform_order_no, email, phone, configuration
        raise NotificationStateError(
            "Notification e-mail and phone must be captured from customization JSON."
        )

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
        """Close latest unsent notifications without calling an external provider."""

        self.initialize()
        ids = tuple(dict.fromkeys(int(value) for value in notification_ids if int(value) > 0))
        reason = note.strip()
        if not ids:
            raise NotificationStateError("At least one notification is required.")
        if not reason:
            raise NotificationStateError("A manual completion reason is required.")
        now = utc_now()
        placeholders = ",".join("?" for _ in ids)
        allowed_states = {
            NOTIFICATION_AWAITING_REVIEW,
            NOTIFICATION_BLOCKED,
            NOTIFICATION_REJECTED,
            NOTIFICATION_WAITING_CONTACT,
        }
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
                    "Only latest, unsent review notifications can be manually completed."
                )
            for row in rows:
                conn.execute(
                    """
                    UPDATE shipment_notifications
                    SET state = ?, provider_status = 'MANUAL_COMPLETION',
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
    ) -> None:
        self.initialize()
        now = utc_now()
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
                    now,
                    now,
                    notification_id,
                    NOTIFICATION_SENDING,
                ),
            ).rowcount
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
                    state_changed_at = CASE WHEN state = ? THEN state_changed_at ELSE ? END,
                    updated_at = CASE WHEN state = ? THEN updated_at ELSE ? END
                WHERE id = ? AND state IN (?, ?, ?, ?)
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
                WHERE id = ? AND state IN (?, ?, ?)
                """,
                (
                    provider_status,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
                    NOTIFICATION_FAILED,
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
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    value,
                    notification_id,
                    NOTIFICATION_ACCEPTED,
                    NOTIFICATION_DELIVERED,
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
                    state_changed_at = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?)
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


__all__ = [
    "NotificationStateError",
    "ShipmentNotificationStore",
    "StaleNotificationError",
    "initialize_notification_schema",
]
