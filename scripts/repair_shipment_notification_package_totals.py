"""Validate and repair the approved 42 Amazon shipment-notification drafts.

The command never calls an e-mail or SMS provider.  Its default mode is a
read-only Lingxing preview.  ``--apply`` is fail-closed: it acquires the shared
notification scan lease, creates an integrity-checked SQLite backup, rebuilds
only the approved orders, verifies every rendered draft, and restores the
backup automatically if any assertion fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_automation.application.desktop_services import DesktopApiServices
from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.configuration.settings import with_configuration_defaults
from erp_automation.ui.models import CapabilityPolicy
from erp_automation.ui.persistent_controller import _settings_from_values
from shipment_automation import notification_sync as notification_sync_module
from shipment_automation.notification_domain import (
    CHANNEL_EMAIL,
    CHANNEL_MANUAL_EMAIL,
    EMAIL_TEMPLATE_VERSION,
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
    SMS_TEMPLATE_VERSION,
    NotificationConfiguration,
    analyze_order_products,
    customer_carrier_display_name,
    stable_package_label,
)
from shipment_automation.notification_store import (
    ShipmentNotificationStore,
    utc_now,
)
from shipment_automation.notification_sync import sync_notification_drafts


@dataclass(frozen=True)
class ExpectedOrder:
    package_total: int
    tracking_numbers: tuple[str, ...]

    @property
    def package_complete(self) -> int:
        return len(self.tracking_numbers)

    @property
    def package_missing(self) -> int:
        return self.package_total - self.package_complete


EXPECTED_ORDERS: dict[str, ExpectedOrder] = {
    "111-0919575-7553008": ExpectedOrder(4, ("383235956506", "876084712995", "876161194692")),
    "111-1766162-0161009": ExpectedOrder(4, ("875908466147", "383257101682", "876165719210")),
    "111-3076088-8856246": ExpectedOrder(3, ("383257092504", "876084727370")),
    "111-3840449-8473832": ExpectedOrder(3, ("875188303226", "383065177668")),
    "111-5171191-9662620": ExpectedOrder(3, ("WNBAA0492227985YQ",)),
    "111-6102358-1535436": ExpectedOrder(3, ("874967322696", "1Z9253126732405050")),
    "111-7132426-0640244": ExpectedOrder(3, ("420840669235990358939200162094",)),
    "111-7776318-7435421": ExpectedOrder(3, ("383302834771", "876088281081")),
    "111-7942732-0205061": ExpectedOrder(3, ("875077845524", "WNBAA0488952453YQ")),
    "111-8063596-3168240": ExpectedOrder(3, ("875729552812", "520071749443")),
    "111-8931904-5748267": ExpectedOrder(3, ("875920243520", "383257096094")),
    "111-9258218-1978614": ExpectedOrder(2, ("876161964452",)),
    "111-9677801-8945001": ExpectedOrder(4, ("383235958667", "875978040668")),
    "112-4024775-5253026": ExpectedOrder(3, ("875920475061", "9235990374018503397650")),
    "112-5768606-0841027": ExpectedOrder(3, ("383257079963", "875977966850")),
    "112-7401824-5567462": ExpectedOrder(4, ("383257104122", "876025107199")),
    "112-8004970-0417042": ExpectedOrder(4, ("875660504527", "383151814691", "1Z9253126709911494")),
    "112-9374762-4638625": ExpectedOrder(4, ("9334610990150195243347", "875280406035", "YWNJC010165647768")),
    "112-9395988-5409869": ExpectedOrder(2, ("383151798496",)),
    "112-9917304-4422608": ExpectedOrder(4, ("875079259744", "382895711290", "383047477093")),
    "113-1421105-0479421": ExpectedOrder(2, ("876091137766",)),
    "113-2509678-9009054": ExpectedOrder(2, ("383235985568",)),
    "113-4599836-7015463": ExpectedOrder(4, ("383278402001", "876090497226")),
    "113-5739376-6632227": ExpectedOrder(2, ("875280312597",)),
    "113-6170521-1976253": ExpectedOrder(4, ("875654978627", "9334610990150197995268", "1Z6922TT0498493885")),
    "113-7069284-7168267": ExpectedOrder(3, ("875920029430", "383278277060")),
    "113-7492897-6669068": ExpectedOrder(3, ("875919756601", "383278267415")),
    "113-8873565-0796215": ExpectedOrder(3, ("875340040793", "420356229235990416420601091579")),
    "113-9130699-3238645": ExpectedOrder(4, ("525885602122", "9334610990370301831415", "875029097550")),
    "113-9629041-3918640": ExpectedOrder(4, ("382980977808", "875295179252", "875817368263")),
    "114-0221616-1108270": ExpectedOrder(2, ("875977786684",)),
    "114-0805291-5411421": ExpectedOrder(3, ("9334610990150198337852",)),
    "114-1634591-5809060": ExpectedOrder(4, ("382895492929", "875339988549", "383047463237")),
    "114-1641635-8177858": ExpectedOrder(3, ("875821674822",)),
    "114-2893736-2488215": ExpectedOrder(4, ("875768624065", "383235958288", "876098705761")),
    "114-3787262-0771436": ExpectedOrder(2, ("383278405099",)),
    "114-5135526-2449009": ExpectedOrder(2, ("9334611043900000776229",)),
    "114-7253223-0753048": ExpectedOrder(3, ("383234720249", "875920181952")),
    "114-8399398-9427419": ExpectedOrder(3, ("YWIND010032929331", "874967333270")),
    "114-8661675-4188249": ExpectedOrder(2, ("875978449942",)),
    "114-9717300-0918653": ExpectedOrder(3, ("383278384134",)),
    "114-9947862-6941834": ExpectedOrder(4, ("382873516903", "875077579307", "420229689235990323596307221389")),
}

SKIPPED_INDEPENDENT_ORDERS = (
    "wc40268",
    "wc40309",
    "wc40398",
    "wc40591",
)

EXCLUDED_ORDERS = (
    "111-1960388-9992218",
    "112-0654759-4532239",
    "112-4307454-0565047",
    "112-7410676-8171401",
    "112-8496268-3010615",
    "113-4376138-8360225",
    "114-3208815-5239441",
    "114-6001103-5339437",
    "114-8813992-4450642",
)

REPAIR_MARKER = "PACKAGE_TOTAL_REPAIR_2026_08_24_V1"
REPAIR_ACTOR = "codex_package_total_repair"
SAFE_REVIEW_STATES = {
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_MANUAL_EMAIL_REQUIRED,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-order-count", type=int, default=0)
    return parser


def _configuration_store(workspace: Path) -> EncryptedConfigurationStore:
    """Use the desktop backend locally and the existing host-key backend on server."""

    if str(os.environ.get("ERP_AUTOMATION_HOST_KEY_FILE") or "").strip():
        from erp_automation.operations.customer_shipping_preflight import (
            _configuration_store as server_configuration_store,
        )

        return server_configuration_store(workspace)
    return EncryptedConfigurationStore(workspace / "data" / "config.enc")


def _validate_manifest() -> None:
    if len(EXPECTED_ORDERS) != 42:
        raise RuntimeError("repair manifest must contain exactly 42 Amazon orders")
    if set(EXPECTED_ORDERS) & set(EXCLUDED_ORDERS):
        raise RuntimeError("repair and exclusion manifests overlap")
    if set(EXPECTED_ORDERS) & set(SKIPPED_INDEPENDENT_ORDERS):
        raise RuntimeError("independent-site orders must not enter the repair manifest")
    total_distribution = Counter(
        expected.package_total for expected in EXPECTED_ORDERS.values()
    )
    progress_distribution = Counter(
        (expected.package_complete, expected.package_total)
        for expected in EXPECTED_ORDERS.values()
    )
    if total_distribution != Counter({2: 9, 3: 19, 4: 14}):
        raise RuntimeError(f"unexpected package-total distribution: {total_distribution}")
    if progress_distribution != Counter(
        {(1, 2): 9, (1, 3): 5, (2, 3): 14, (2, 4): 3, (3, 4): 11}
    ):
        raise RuntimeError(f"unexpected progress distribution: {progress_distribution}")
    all_tracking = [
        tracking
        for expected in EXPECTED_ORDERS.values()
        for tracking in expected.tracking_numbers
    ]
    if len(all_tracking) != len(set(all_tracking)):
        raise RuntimeError("repair manifest contains duplicate tracking numbers")
    if any(expected.package_missing <= 0 for expected in EXPECTED_ORDERS.values()):
        raise RuntimeError("every approved repair order must remain partially shipped")


async def _read_api_preview(
    gateway: Any,
    platform_order_nos: Sequence[str],
) -> dict[str, Any]:
    platforms = tuple(dict.fromkeys(str(value).strip() for value in platform_order_nos))
    facts_by_platform, fact_errors, order_api_calls = (
        await notification_sync_module._read_platform_order_facts_batch(
            gateway,
            platforms,
        )
    )
    if fact_errors:
        raise RuntimeError(
            "order facts unavailable: "
            + json.dumps(fact_errors, ensure_ascii=False, sort_keys=True)
        )
    missing = sorted(set(platforms) - set(facts_by_platform))
    if missing:
        raise RuntimeError(f"order API omitted {len(missing)} requested orders: {missing}")

    all_systems = tuple(
        dict.fromkeys(
            system_order_no
            for platform in platforms
            for system_order_no in facts_by_platform[platform].system_order_nos
        )
    )
    rows, wms_api_calls = await notification_sync_module._read_all_wms_rows_with_count(
        gateway,
        all_systems,
    )
    by_system: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        system_order_no = str(
            row.get("order_number") or row.get("global_order_no") or ""
        ).strip()
        if system_order_no in all_systems:
            by_system[system_order_no].append(row)

    warehouse_lookup = notification_sync_module._EMPTY_WAREHOUSE_CODE_LOOKUP
    warehouse_lookup_error = ""
    try:
        warehouse_lookup = await notification_sync_module._read_warehouse_code_lookup(
            gateway
        )
    except Exception as error:
        warehouse_lookup_error = type(error).__name__

    order_results: dict[str, Any] = {}
    for platform in platforms:
        facts = facts_by_platform[platform]
        product_analysis = analyze_order_products(
            facts.products,
            expected_system_order_nos=facts.system_order_nos,
        )
        instruction_systems = frozenset(
            product_analysis.instruction_system_order_nos
        )
        evaluation = notification_sync_module._evaluate_platform_outbound(
            platform_order_no=platform,
            system_orders=facts.system_orders,
            instruction_system_order_nos=instruction_systems,
            rows_by_system=by_system,
        )
        packages = []
        package_keys: set[str] = set()
        for row in evaluation.package_rows:
            package = notification_sync_module.package_from_wms_row(
                row,
                platform_order_no=platform,
                instruction_system_order_nos=instruction_systems,
                warehouse_code_lookup=warehouse_lookup,
            )
            if package.package_key in package_keys:
                raise RuntimeError(f"duplicate WMS package in preview: {package.package_key}")
            package_keys.add(package.package_key)
            if package.customer_visible and package.complete:
                packages.append(package)
        packages.sort(key=lambda item: item.package_key)
        order_results[platform] = {
            "platform_order_no": platform,
            "system_order_nos": list(facts.system_order_nos),
            "system_orders": [
                {
                    "system_order_no": fact.system_order_no,
                    "status": fact.status_code,
                    "is_delete": fact.is_delete,
                    "has_instruction": fact.has_instruction,
                    "has_physical_items": fact.has_physical_items,
                }
                for fact in facts.system_orders
            ],
            "instruction_system_order_nos": sorted(instruction_systems),
            "outbound_state": evaluation.state,
            "outbound_reason": evaluation.reason,
            "known_customer_package_total": evaluation.known_customer_package_count,
            "package_complete": len(packages),
            "package_missing": max(
                0,
                evaluation.known_customer_package_count - len(packages),
            ),
            "unknown_status_count": evaluation.unknown_status_count,
            "conflicting_status_count": evaluation.conflicting_status_count,
            "packages": [
                {
                    "package_key": package.package_key,
                    "system_order_no": package.system_order_no,
                    "wms_outbound_order_no": package.wms_outbound_order_no,
                    "carrier": customer_carrier_display_name(
                        package.carrier,
                        package.final_tracking_no,
                    ),
                    "tracking_number": package.final_tracking_no,
                    "wms_status": package.wms_status_code,
                }
                for package in packages
            ],
            "diagnostics": [dict(item) for item in evaluation.diagnostics],
        }
    return {
        "read_only": True,
        "order_api_call_count": order_api_calls,
        "wms_api_call_count": wms_api_calls,
        "warehouse_lookup_error": warehouse_lookup_error,
        "orders": order_results,
    }


def _validate_api_preview(preview: Mapping[str, Any]) -> dict[str, Any]:
    orders = dict(preview.get("orders") or {})
    issues: list[str] = []
    for platform, expected in EXPECTED_ORDERS.items():
        actual = dict(orders.get(platform) or {})
        actual_tracking = {
            str(item.get("tracking_number") or "")
            for item in actual.get("packages") or ()
        }
        expected_tracking = set(expected.tracking_numbers)
        checks = {
            "outbound_state": actual.get("outbound_state") == "OUTBOUNDED",
            "known_total": int(actual.get("known_customer_package_total") or 0)
            == expected.package_total,
            "complete": int(actual.get("package_complete") or 0)
            == expected.package_complete,
            "missing": int(actual.get("package_missing") or 0)
            == expected.package_missing,
            "tracking": actual_tracking == expected_tracking,
            "unknown": int(actual.get("unknown_status_count") or 0) == 0,
            "conflict": int(actual.get("conflicting_status_count") or 0) == 0,
        }
        if not all(checks.values()):
            issues.append(
                f"{platform}: failed={sorted(key for key, ok in checks.items() if not ok)} "
                f"expected={expected.package_complete}/{expected.package_total} "
                f"actual={actual.get('package_complete')}/{actual.get('known_customer_package_total')}"
            )
    for platform in EXCLUDED_ORDERS:
        actual = dict(orders.get(platform) or {})
        if int(actual.get("package_complete") or 0) != 0:
            issues.append(
                f"{platform}: excluded Instruction order now has a customer-visible completed package"
            )
    if issues:
        raise RuntimeError("API preview differs from approved manifest:\n" + "\n".join(issues))
    return {
        "validated_repair_order_count": len(EXPECTED_ORDERS),
        "validated_exclusion_order_count": len(EXCLUDED_ORDERS),
        "total_distribution": dict(
            sorted(
                Counter(
                    expected.package_total for expected in EXPECTED_ORDERS.values()
                ).items()
            )
        ),
        "progress_distribution": {
            f"{complete}/{total}": count
            for (complete, total), count in sorted(
                Counter(
                    (expected.package_complete, expected.package_total)
                    for expected in EXPECTED_ORDERS.values()
                ).items()
            )
        },
    }


def _backup_database(source_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = source_path.with_name(
        f"{source_path.stem}.pre_package_total_repair_{stamp}{source_path.suffix}"
    )
    source = sqlite3.connect(source_path, timeout=30)
    target = sqlite3.connect(backup_path, timeout=30)
    try:
        source.execute("PRAGMA busy_timeout = 30000")
        source.backup(target)
        integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"backup integrity check failed: {integrity}")
    finally:
        target.close()
        source.close()
    return backup_path


def _restore_database(backup_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(backup_path, timeout=30)
    destination = sqlite3.connect(destination_path, timeout=30)
    try:
        destination.execute("PRAGMA busy_timeout = 30000")
        source.backup(destination)
        integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"restored database integrity check failed: {integrity}")
    finally:
        destination.close()
        source.close()


def _upsert_missing_repair_sources(
    store: ShipmentNotificationStore,
    preview_orders: Mapping[str, Any],
) -> int:
    existing_targets = {
        str(item["platform_order_no"])
        for item in store.notification_scan_targets(tuple(EXPECTED_ORDERS))
    }
    missing = sorted(set(EXPECTED_ORDERS) - existing_targets)
    if not missing:
        return 0
    now = utc_now()
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for platform in missing:
            systems = tuple(
                str(value).strip()
                for value in preview_orders[platform]["system_order_nos"]
                if str(value).strip()
            )
            if not systems:
                conn.rollback()
                raise RuntimeError(f"cannot add repair source without systems: {platform}")
            previous = conn.execute(
                "SELECT first_seen_at FROM shipment_notification_order_sources "
                "WHERE platform_order_no = ?",
                (platform,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO shipment_notification_order_sources (
                    platform_order_no, source_kind, system_order_nos_json,
                    purchased_at, eligibility_reason, baseline_pending,
                    active, first_seen_at, last_seen_at, updated_at
                ) VALUES (?, 'PACKAGE_TOTAL_REPAIR', ?, '', ?, 0, 1, ?, ?, ?)
                ON CONFLICT(platform_order_no) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    system_order_nos_json = excluded.system_order_nos_json,
                    eligibility_reason = excluded.eligibility_reason,
                    baseline_pending = 0,
                    active = 1,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    platform,
                    json.dumps(systems, ensure_ascii=False),
                    REPAIR_MARKER,
                    str(previous["first_seen_at"] or now) if previous else now,
                    now,
                    now,
                ),
            )
        conn.commit()
    observed_targets = {
        str(item["platform_order_no"])
        for item in store.notification_scan_targets(tuple(EXPECTED_ORDERS))
    }
    if observed_targets != set(EXPECTED_ORDERS):
        missing_after = sorted(set(EXPECTED_ORDERS) - observed_targets)
        raise RuntimeError(f"repair targets remain unavailable: {missing_after}")
    return len(missing)


def _has_repair_marker(notification: Mapping[str, Any] | None) -> bool:
    return bool(
        notification
        and any(
            str(review.get("note") or "") == REPAIR_MARKER
            for review in notification.get("reviews") or ()
        )
    )


def _record_repair_marker(
    store: ShipmentNotificationStore,
    notification: Mapping[str, Any],
) -> None:
    if _has_repair_marker(notification):
        return
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO shipment_notification_reviews (
                notification_id, revision, action, content_hash, actor, note, created_at
            ) VALUES (?, ?, 'PACKAGE_TOTAL_REPAIR_PREPARED', ?, ?, ?, ?)
            """,
            (
                int(notification["id"]),
                int(notification["revision"]),
                str(notification["content_hash"] or ""),
                REPAIR_ACTOR,
                REPAIR_MARKER,
                utc_now(),
            ),
        )
        conn.commit()


def _notification_matches_expected(
    notification: Mapping[str, Any],
    expected: ExpectedOrder,
) -> list[str]:
    problems: list[str] = []
    if int(notification.get("package_total") or 0) != expected.package_total:
        problems.append("package_total")
    if int(notification.get("package_complete") or 0) != expected.package_complete:
        problems.append("package_complete")
    if int(notification.get("package_missing") or 0) != expected.package_missing:
        problems.append("package_missing")
    items = [
        item
        for item in notification.get("items") or ()
        if bool(item.get("customer_visible")) and bool(item.get("is_complete"))
    ]
    actual_tracking = {str(item.get("final_tracking_no") or "") for item in items}
    if actual_tracking != set(expected.tracking_numbers):
        problems.append("tracking_numbers")
    expected_version = (
        EMAIL_TEMPLATE_VERSION
        if str(notification.get("channel") or "")
        in {CHANNEL_EMAIL, CHANNEL_MANUAL_EMAIL}
        else SMS_TEMPLATE_VERSION
    )
    if str(notification.get("template_version") or "") != expected_version:
        problems.append("template_version")
    if str(notification.get("state") or "") not in SAFE_REVIEW_STATES:
        problems.append("review_state")
    if notification.get("provider_message_id") or notification.get("sent_at"):
        problems.append("provider_evidence")
    progress = (
        f"Shipment progress: {expected.package_complete} of "
        f"{expected.package_total} packages have shipped."
    )
    body = str(notification.get("body") or "")
    if progress not in body:
        problems.append("progress_text")
    placeholder = (
        f"Package {stable_package_label(expected.package_complete + 1)}: "
        "Available soon."
    )
    if body.count("Available soon") != 1 or placeholder not in body:
        problems.append("available_soon")
    if str(notification.get("channel") or "") in {CHANNEL_EMAIL, CHANNEL_MANUAL_EMAIL}:
        body_html = str(notification.get("body_html") or "")
        if progress not in body_html or body_html.count("Available soon") != 1:
            problems.append("email_html")
    return problems


def _notification_summary(notification: Mapping[str, Any]) -> dict[str, Any]:
    items = sorted(
        (
            item
            for item in notification.get("items") or ()
            if bool(item.get("customer_visible")) and bool(item.get("is_complete"))
        ),
        key=lambda item: int(item.get("stable_sequence") or 0),
    )
    package_complete = int(notification.get("package_complete") or 0)
    package_total = int(notification.get("package_total") or 0)
    return {
        "platform_order_no": str(notification["platform_order_no"]),
        "notification_id": int(notification["id"]),
        "revision": int(notification["revision"]),
        "state": str(notification["state"]),
        "channel": str(notification["channel"]),
        "template_version": str(notification["template_version"]),
        "progress": f"{package_complete}/{package_total}",
        "packages": [
            {
                "label": stable_package_label(index),
                "carrier": customer_carrier_display_name(
                    str(item.get("carrier_normalized") or ""),
                    str(item.get("final_tracking_no") or ""),
                    prefer_tracking_inference=not bool(item.get("manual_override")),
                ),
                "tracking_number": str(item.get("final_tracking_no") or ""),
            }
            for index, item in enumerate(items, start=1)
        ],
        "placeholder": (
            f"Package {stable_package_label(package_complete + 1)}: Available soon."
        ),
    }


async def _apply_repair(
    gateway: Any,
    store: ShipmentNotificationStore,
    configuration: NotificationConfiguration,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    before = {
        platform: store.get_latest_notification(platform)
        for platform in EXPECTED_ORDERS
    }
    sources_added = _upsert_missing_repair_sources(
        store,
        dict(preview["orders"]),
    )
    sync_result = await sync_notification_drafts(
        gateway,
        store,
        configuration,
        platform_order_nos=tuple(EXPECTED_ORDERS),
        include_deferred_retries=True,
    )
    if int(sync_result.get("failed_order_count") or 0):
        raise RuntimeError(
            "repair synchronization failed: "
            + json.dumps(sync_result, ensure_ascii=False, sort_keys=True)
        )

    created = 0
    already_prepared = 0
    summaries: list[dict[str, Any]] = []
    for platform, expected in EXPECTED_ORDERS.items():
        latest = store.get_latest_notification(platform)
        if latest is None:
            raise RuntimeError(f"synchronization did not create a draft for {platform}")
        if _has_repair_marker(latest):
            problems = _notification_matches_expected(latest, expected)
            if problems:
                raise RuntimeError(
                    f"marked repair draft is stale for {platform}: {problems}"
                )
            already_prepared += 1
            summaries.append(_notification_summary(latest))
            continue

        before_id = int(before[platform]["id"]) if before[platform] else 0
        if int(latest["id"]) == before_id:
            latest = store.reopen_for_review(
                int(latest["id"]),
                configuration,
                actor=REPAIR_ACTOR,
                note=REPAIR_MARKER,
            )
        problems = _notification_matches_expected(latest, expected)
        if problems:
            raise RuntimeError(f"generated draft is invalid for {platform}: {problems}")
        _record_repair_marker(store, latest)
        latest = store.get_notification(int(latest["id"]))
        if latest is None or not _has_repair_marker(latest):
            raise RuntimeError(f"repair audit marker missing for {platform}")
        if int(latest["id"]) == before_id:
            raise RuntimeError(f"repair did not create a new notification row for {platform}")
        created += 1
        summaries.append(_notification_summary(latest))

    with store.connect() as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"post-repair database integrity check failed: {integrity}")
    return {
        "sources_added": sources_added,
        "new_notification_count": created,
        "already_prepared_count": already_prepared,
        "sync": sync_result,
        "database_integrity": integrity,
        "notifications": summaries,
        "external_provider_calls": 0,
    }


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_manifest()
    workspace = args.workspace.resolve(strict=True)
    configuration_store = _configuration_store(workspace)
    values = with_configuration_defaults(
        configuration_store.load(allow_backup_fallback=True).values
    )
    settings = _settings_from_values(values)
    queue_path = args.database or Path(settings.queue_path)
    if not queue_path.is_absolute():
        queue_path = workspace / queue_path
    queue_path = queue_path.resolve(strict=True)
    notification_configuration = NotificationConfiguration.from_mapping(values)
    services = DesktopApiServices(
        workspace,
        configuration_store=configuration_store,
        policy_provider=CapabilityPolicy,
    )
    gateway, client = await services.create_gateway(settings)
    try:
        requested = (*EXPECTED_ORDERS, *EXCLUDED_ORDERS)
        preview = await _read_api_preview(gateway, requested)
        validation = _validate_api_preview(preview)
        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "preview",
            "generated_at": utc_now(),
            "database": str(queue_path),
            "manifest_validation": validation,
            "skipped_independent_orders": list(SKIPPED_INDEPENDENT_ORDERS),
            "api_preview": preview,
            "external_provider_calls": 0,
        }
        if not args.apply:
            return result
        if args.confirm_order_count != len(EXPECTED_ORDERS):
            raise RuntimeError(
                f"--apply requires --confirm-order-count {len(EXPECTED_ORDERS)}"
            )

        store = ShipmentNotificationStore(queue_path)
        owner = f"{REPAIR_ACTOR}:{uuid.uuid4().hex}"
        if not store.try_acquire_scan_lock(owner, lease_seconds=7200):
            raise RuntimeError("customer notification scan is active; repair was not started")
        backup_path: Path | None = None
        try:
            backup_path = _backup_database(queue_path)
            result["backup"] = str(backup_path)
            result["apply"] = await _apply_repair(
                gateway,
                store,
                notification_configuration,
                preview,
            )
        except Exception:
            if backup_path is not None:
                _restore_database(backup_path, queue_path)
                result["restored_after_failure"] = True
            raise
        finally:
            store.release_scan_lock(owner)
        return result
    finally:
        await client.aclose()


def main() -> int:
    args = _parser().parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or (
        args.workspace / "output" / f"shipment_package_total_repair_{stamp}.json"
    )
    try:
        report = asyncio.run(_run(args))
    except Exception as error:
        failure = {
            "mode": "apply" if args.apply else "preview",
            "generated_at": utc_now(),
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "external_provider_calls": 0,
        }
        _write_report(output, failure)
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 1
    report["status"] = "ok"
    _write_report(output, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "mode": report["mode"],
                "output": str(output),
                "validated_orders": len(EXPECTED_ORDERS),
                "external_provider_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
