"""One-time, idempotent v9 shipment-notification queue reset.

The command intentionally performs no provider send.  It validates the four
retained customization JSON files, creates a consistent SQLite backup, records
historical exclusions, removes the old notification snapshots, rebuilds the
authoritative JSON contact snapshots, and finally performs a read-only Lingxing
WMS package synchronization to create review drafts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from erp_automation.application.desktop_services import DesktopApiServices
from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.configuration.settings import with_configuration_defaults
from erp_automation.ui.models import CapabilityPolicy
from erp_automation.ui.persistent_controller import _settings_from_values
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.services.customization_json_parser import (
    parse_customization_json_info,
)
from shipment_automation.notification_domain import (
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    CONTACT_SOURCE_WMS,
    EMAIL_PRESENCE_PROVIDED,
    NotificationConfiguration,
    OrderContact,
    normalize_email,
    normalize_phone,
    normalize_recipient_name,
)
from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.notification_sync import sync_notification_drafts
from shipment_automation.queue_store import SCHEMA_VERSION, ShipmentWorkflowStore


RETAINED_PLATFORM_ORDERS = (
    "112-0282203-3275405",
    "112-9271203-1985816",
    "114-0266130-5143414",
    "114-0455929-8841033",
)
RESET_REASON = "historical ERP-complete notifications already sent manually before v9 reset"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--json", dest="json_paths", type=Path, action="append", required=True)
    parser.add_argument("--expected-old-count", type=int, default=33)
    parser.add_argument("--apply", action="store_true")
    return parser


def _backup_database(source_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = source_path.with_name(
        f"{source_path.stem}.pre_v9_notification_reset_{stamp}{source_path.suffix}"
    )
    source = sqlite3.connect(source_path, timeout=15)
    target = sqlite3.connect(backup_path, timeout=15)
    try:
        source.execute("PRAGMA busy_timeout = 15000")
        source.backup(target)
        result = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        if result != "ok":
            raise RuntimeError(f"backup integrity check failed: {result}")
    finally:
        target.close()
        source.close()
    return backup_path


def _load_json_contacts(paths: Iterable[Path]) -> tuple[dict[str, tuple[str, str]], dict[str, Path]]:
    grouped: dict[str, list[tuple[str, str]]] = {
        platform: [] for platform in RETAINED_PLATFORM_ORDERS
    }
    source_paths: dict[str, Path] = {}
    for raw_path in paths:
        path = raw_path.resolve(strict=True)
        matches = [platform for platform in RETAINED_PLATFORM_ORDERS if platform in str(path)]
        if len(matches) != 1:
            raise RuntimeError("each JSON path must identify exactly one retained platform order")
        platform = matches[0]
        if platform in source_paths:
            raise RuntimeError(f"duplicate JSON file supplied for {platform}")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"customization JSON root must be an object: {platform}")
        info = parse_customization_json_info(payload, raw_json_path=str(path))
        candidates = extract_contact_candidates_from_json_items([info])
        grouped[platform].extend(
            (str(item.email or "").strip(), str(item.phone or "").strip())
            for item in candidates
        )
        source_paths[platform] = path

    contacts: dict[str, tuple[str, str]] = {}
    for platform in RETAINED_PLATFORM_ORDERS:
        candidates = tuple(dict.fromkeys(grouped[platform]))
        if len(candidates) != 1:
            raise RuntimeError(f"expected exactly one JSON contact candidate for {platform}")
        email, phone = candidates[0]
        if normalize_email(email) is None or normalize_phone(phone) is None:
            raise RuntimeError(f"retained JSON contact is incomplete or invalid for {platform}")
        contacts[platform] = (email, phone)
    return contacts, source_paths


def _read_reset_inputs(
    queue_path: Path,
    *,
    expected_old_count: int,
) -> tuple[tuple[str, ...], dict[str, tuple[str, tuple[str, ...], dict[str, str]]]]:
    with sqlite3.connect(queue_path, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("source database integrity check failed")
        names = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        latest = conn.execute(
            """
            SELECT n.platform_order_no, n.state, n.provider_message_id, n.sent_at
            FROM shipment_notifications n
            WHERE n.legacy_email_batch_id IS NULL
              AND n.id IN (
                  SELECT MAX(id) FROM shipment_notifications
                  WHERE legacy_email_batch_id IS NULL GROUP BY platform_order_no
              )
            ORDER BY n.platform_order_no
            """
        ).fetchall()
        fresh_old = tuple(str(row["platform_order_no"]) for row in latest)
        is_prereset = (
            len(latest) == expected_old_count
            and all(str(row["state"]) == "MANUALLY_COMPLETED" for row in latest)
            and all(not row["provider_message_id"] and not row["sent_at"] for row in latest)
            and not (set(fresh_old) & set(RETAINED_PLATFORM_ORDERS))
        )
        excluded = ()
        if "shipment_notification_exclusions" in names:
            excluded = tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT platform_order_no FROM shipment_notification_exclusions "
                    "WHERE reason = ? ORDER BY platform_order_no",
                    (RESET_REASON,),
                )
            )
        if is_prereset:
            historical = fresh_old
        elif len(excluded) == expected_old_count and not (
            set(excluded) & set(RETAINED_PLATFORM_ORDERS)
        ):
            historical = excluded
        else:
            raise RuntimeError(
                "database is neither the validated 33-row pre-reset state nor an idempotent reset state"
            )

        retained: dict[str, tuple[str, tuple[str, ...], dict[str, str]]] = {}
        for platform in RETAINED_PLATFORM_ORDERS:
            jobs = conn.execute(
                """
                SELECT j.system_order_no, e.state, e.checkpoint
                FROM shipment_jobs j JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.platform_order_no = ? AND j.identity_state = 'ACTIVE'
                ORDER BY j.system_order_no
                """,
                (platform,),
            ).fetchall()
            if not jobs or any(
                str(row["state"]) != "DONE" or str(row["checkpoint"]) != "OUTBOUNDED"
                for row in jobs
            ):
                raise RuntimeError(f"retained ERP queue is not fully completed for {platform}")
            contact = conn.execute(
                "SELECT * FROM shipment_order_contacts WHERE platform_order_no = ?",
                (platform,),
            ).fetchone()
            recipient_name = (
                str(contact["recipient_name"] or "").strip()
                if contact
                and str(contact["recipient_name_source"] or "") == CONTACT_SOURCE_WMS
                else ""
            )
            metadata = {
                "sales_platform_code": str(contact["sales_platform_code"] or "") if contact else "",
                "sales_platform_name": str(contact["sales_platform_name"] or "") if contact else "",
                "store_name": str(contact["store_name"] or "") if contact else "",
                "site_name": str(contact["site_name"] or "") if contact else "",
            }
            retained[platform] = (
                recipient_name,
                tuple(str(row["system_order_no"]) for row in jobs),
                metadata,
            )
    return historical, retained


def _delete_invalid_retained_drafts(queue_path: Path) -> int:
    placeholders = ",".join("?" for _ in RETAINED_PLATFORM_ORDERS)
    with sqlite3.connect(queue_path, timeout=15) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM shipment_notifications WHERE legacy_email_batch_id IS NULL "
            f"AND platform_order_no IN ({placeholders})",
            RETAINED_PLATFORM_ORDERS,
        ).fetchall()
        wms_name_platforms = {
            str(row[0])
            for row in conn.execute(
                f"SELECT platform_order_no FROM shipment_order_contacts "
                f"WHERE platform_order_no IN ({placeholders}) "
                f"AND recipient_name_source = ?",
                (*RETAINED_PLATFORM_ORDERS, CONTACT_SOURCE_WMS),
            )
        }
        invalid_platforms = {
            str(row["platform_order_no"])
            for row in rows
            if (
                str(row["platform_order_no"]) not in wms_name_platforms
                or not normalize_recipient_name(str(row["recipient_name"] or ""))
            )
        }
        if not invalid_platforms:
            return 0
        unsafe = [
            row
            for row in rows
            if str(row["platform_order_no"]) in invalid_platforms
            and (
                row["provider_message_id"]
                or row["sent_at"]
                or str(row["state"])
                not in {"WAITING_CONTACT", "AWAITING_REVIEW", "BLOCKED", "REJECTED"}
            )
        ]
        if unsafe:
            raise RuntimeError("invalid-name retained notifications are no longer safe to rebuild")
        selected = tuple(sorted(invalid_platforms))
        selected_placeholders = ",".join("?" for _ in selected)
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            f"DELETE FROM shipment_notifications WHERE legacy_email_batch_id IS NULL "
            f"AND platform_order_no IN ({selected_placeholders})",
            selected,
        ).rowcount
        conn.commit()
        return int(deleted)


async def _sync_wms(
    workspace: Path,
    values: dict[str, Any],
    settings: Any,
    store: ShipmentNotificationStore,
) -> dict[str, int]:
    config_store = EncryptedConfigurationStore(workspace / "data" / "config.enc")
    services = DesktopApiServices(
        workspace,
        configuration_store=config_store,
        policy_provider=CapabilityPolicy,
    )
    gateway, client = await services.create_gateway(settings)
    try:
        return await sync_notification_drafts(
            gateway,
            store,
            NotificationConfiguration.from_mapping(values),
        )
    finally:
        await client.aclose()


def main() -> int:
    args = _parser().parse_args()
    workspace = args.workspace.resolve(strict=True)
    config_store = EncryptedConfigurationStore(workspace / "data" / "config.enc")
    values = with_configuration_defaults(
        config_store.load(allow_backup_fallback=True).values
    )
    settings = _settings_from_values(values)
    queue_path = Path(settings.queue_path)
    if not queue_path.is_absolute():
        queue_path = workspace / queue_path
    queue_path = queue_path.resolve(strict=True)

    contacts, _json_paths = _load_json_contacts(args.json_paths)
    historical, retained = _read_reset_inputs(
        queue_path,
        expected_old_count=args.expected_old_count,
    )
    if not args.apply:
        print(
            f"validated: historical={len(historical)} retained={len(retained)} "
            f"json_contacts={len(contacts)}; pass --apply to execute"
        )
        return 0

    backup_path = _backup_database(queue_path)
    ShipmentWorkflowStore(queue_path).initialize()
    store = ShipmentNotificationStore(queue_path)
    deleted = store.exclude_and_delete_platforms(historical, reason=RESET_REASON)
    invalid_retained_deleted = _delete_invalid_retained_drafts(queue_path)
    for platform in RETAINED_PLATFORM_ORDERS:
        recipient_name, system_order_nos, metadata = retained[platform]
        email, phone = contacts[platform]
        recipient_name_source = CONTACT_SOURCE_WMS if recipient_name else ""
        store.upsert_contact(
            OrderContact(
                platform_order_no=platform,
                recipient_name=recipient_name,
                email=email,
                email_presence=EMAIL_PRESENCE_PROVIDED,
                phone_raw=phone,
                sales_platform_code=metadata["sales_platform_code"],
                sales_platform_name=metadata["sales_platform_name"],
                store_name=metadata["store_name"],
                site_name=metadata["site_name"],
                source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                recipient_name_source=recipient_name_source,
                email_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                phone_source=CONTACT_SOURCE_CUSTOMIZATION_JSON,
                system_order_nos=system_order_nos,
            )
        )

    targets = {str(row["platform_order_no"]) for row in store.notification_scan_targets()}
    if targets != set(RETAINED_PLATFORM_ORDERS):
        raise RuntimeError(
            f"expected exactly four post-reset scan targets, observed {len(targets)}"
        )
    sync_result = asyncio.run(_sync_wms(workspace, values, settings, store))

    latest = store.list_notifications()
    if {str(row["platform_order_no"]) for row in latest} != set(RETAINED_PLATFORM_ORDERS):
        raise RuntimeError("post-reset notification set does not match retained orders")
    if any(row.get("provider_message_id") or row.get("sent_at") for row in latest):
        raise RuntimeError("provider evidence unexpectedly appeared during draft rebuild")
    with sqlite3.connect(queue_path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if integrity != "ok" or version != SCHEMA_VERSION:
        raise RuntimeError(f"post-reset database validation failed: {integrity}, v{version}")

    states = dict(Counter(str(row["state"]) for row in latest))
    print(f"backup={backup_path}")
    print(f"deleted={deleted}")
    print(f"invalid_retained_drafts_deleted={invalid_retained_deleted}")
    print(f"sync={sync_result}")
    print(f"notifications={len(latest)} states={states} schema=v{version} integrity={integrity}")
    print("external_provider_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
