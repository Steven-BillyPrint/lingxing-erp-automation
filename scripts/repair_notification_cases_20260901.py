"""Preview and repair the three approved 2026-09-01 notification cases.

The default mode is read-only.  ``--apply`` is fail-closed: it verifies the
exact provider receipt, checks the exact DHL tracking pair and partial-package
facts, acquires the notification scan lock, creates an integrity-checked SQLite
backup, applies only the approved mutations, and restores the backup if any
postcondition fails.  It never sends email or SMS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.configuration.settings import with_configuration_defaults
from erp_automation.ui.persistent_controller import _settings_from_values
from shipment_automation.alibaba_logistics import (
    logistics_readiness_decision,
    normalize_carrier_name,
)
from shipment_automation.models import (
    ERP_CHECKPOINT_NONE,
    ERP_DONE,
    IDENTITY_ACTIVE,
    LOGISTICS_READY,
    LogisticsDetail,
)
from shipment_automation.notification_domain import (
    CHANNEL_EMAIL,
    EMAIL_TEMPLATE_VERSION,
    NOTIFICATION_AWAITING_REVIEW,
    NOTIFICATION_DELIVERED,
    NotificationConfiguration,
)
from shipment_automation.notification_providers import AlimailClient
from shipment_automation.notification_store import ShipmentNotificationStore, utc_now
from shipment_automation.queue_store import ShipmentWorkflowStore


STATE_REPAIR_ORDER = "114-9608129-2148247"
DHL_REPAIR_ORDER = "111-1829451-7385063"
DRAFT_REPAIR_ORDER = "112-7217878-8825061"
APPROVED_ORDERS = (STATE_REPAIR_ORDER, DHL_REPAIR_ORDER, DRAFT_REPAIR_ORDER)

DHL_RAW_CARRIER = "3PL-DHL"
DHL_TRACKING_NO = "7723922905"
REPAIR_ACTOR = "codex_notification_case_repair_20260901"
REPAIR_NOTE = "Approved 2026-09-01 customer notification case repair"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-order-count", type=int, default=0)
    return parser


def _configuration_store(workspace: Path) -> EncryptedConfigurationStore:
    if str(os.environ.get("ERP_AUTOMATION_HOST_KEY_FILE") or "").strip():
        from erp_automation.operations.customer_shipping_preflight import (
            _configuration_store as server_configuration_store,
        )

        return server_configuration_store(workspace)
    return EncryptedConfigurationStore(workspace / "data" / "config.enc")


def _backup_database(source_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = source_path.with_name(
        f"{source_path.stem}.pre_notification_case_repair_{stamp}{source_path.suffix}"
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


def _latest_required_notification(
    store: ShipmentNotificationStore,
    platform_order_no: str,
) -> dict[str, Any]:
    notification = store.get_latest_notification(platform_order_no)
    if notification is None:
        raise RuntimeError(f"notification missing for {platform_order_no}")
    return notification


def _find_dhl_job(store: ShipmentWorkflowStore) -> dict[str, Any]:
    candidates = [
        row
        for row in store.list_all_jobs(reconcile_overdue=False)
        if str(row.get("platform_order_no") or "") == DHL_REPAIR_ORDER
        and str(row.get("identity_state") or "") == IDENTITY_ACTIVE
        and (
            str(row.get("carrier_raw") or "").strip().casefold()
            == DHL_RAW_CARRIER.casefold()
            or (
                str(row.get("carrier") or "").strip() == "DHL"
                and str(row.get("international_tracking_no") or "").strip()
                == DHL_TRACKING_NO
            )
        )
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected exactly one active DHL repair row, found {len(candidates)}"
        )
    row = candidates[0]
    if str(row.get("international_tracking_no") or "").strip() != DHL_TRACKING_NO:
        raise RuntimeError("DHL repair tracking number differs from approved evidence")
    if str(row.get("erp_state") or "") == ERP_DONE:
        raise RuntimeError("DHL repair row is already ERP-complete")
    if str(row.get("erp_checkpoint") or ERP_CHECKPOINT_NONE) != ERP_CHECKPOINT_NONE:
        raise RuntimeError("DHL repair row has already crossed an ERP write checkpoint")
    lease_until = str(row.get("lease_until") or "").strip()
    if lease_until and lease_until > utc_now():
        raise RuntimeError("DHL repair row currently has an active worker lease")
    return row


def _dhl_detail(row: Mapping[str, Any]) -> LogisticsDetail:
    detail = LogisticsDetail(
        logistics_no=str(row.get("logistics_no") or ""),
        status_text=str(row.get("alibaba_status") or ""),
        service_type=str(row.get("service_type") or "") or None,
        service_line=str(row.get("service_line") or "") or None,
        carrier=str(row.get("carrier_raw") or ""),
        international_tracking_no=str(row.get("international_tracking_no") or ""),
        actual_total=str(row.get("actual_total") or "") or None,
        chargeable_weight_kg=str(row.get("chargeable_weight_kg") or "") or None,
        package_count=row.get("package_count"),
        source_url=str(row.get("source_url") or "") or None,
    )
    decision = logistics_readiness_decision(detail)
    if decision.logistics_state != LOGISTICS_READY or not decision.should_continue:
        raise RuntimeError(f"DHL repair evidence is not ready: {decision.reason}")
    if normalize_carrier_name(detail.carrier) != "DHL":
        raise RuntimeError("DHL alias did not normalize to DHL")
    return detail


def _validate_corrected_draft(notification: Mapping[str, Any]) -> None:
    if str(notification.get("platform_order_no") or "") != DRAFT_REPAIR_ORDER:
        raise RuntimeError("corrected draft belongs to an unexpected order")
    if str(notification.get("state") or "") != NOTIFICATION_AWAITING_REVIEW:
        raise RuntimeError("corrected notification is not awaiting review")
    if str(notification.get("channel") or "") != CHANNEL_EMAIL:
        raise RuntimeError("corrected notification is not an email draft")
    if str(notification.get("template_version") or "") != EMAIL_TEMPLATE_VERSION:
        raise RuntimeError("corrected notification does not use the current template")
    progress = (
        int(notification.get("package_complete") or 0),
        int(notification.get("package_total") or 0),
        int(notification.get("package_missing") or 0),
    )
    if progress != (2, 4, 2):
        raise RuntimeError(f"corrected draft progress differs from approved 2/4: {progress}")
    for body_field in ("body", "body_html"):
        body = str(notification.get(body_field) or "")
        if body.count("Available soon") != 2:
            raise RuntimeError(f"{body_field} does not contain two pending packages")
        for label in ("c", "d"):
            if f"Package {label}: Available soon." not in body:
                raise RuntimeError(f"{body_field} is missing package {label}")
    if str(notification.get("provider_message_id") or "").strip() or str(
        notification.get("sent_at") or ""
    ).strip():
        raise RuntimeError("corrected review draft contains provider send evidence")


async def _verify_provider_success(
    notification: Mapping[str, Any],
    configuration: NotificationConfiguration,
) -> dict[str, str]:
    if str(notification.get("state") or "") == NOTIFICATION_DELIVERED:
        if str(notification.get("provider_status") or "").strip().casefold() != "success":
            raise RuntimeError("reconciled notification does not contain provider success")
        provider_message_id = str(notification.get("provider_message_id") or "").strip()
        if not provider_message_id or not str(notification.get("sent_at") or "").strip():
            raise RuntimeError("reconciled notification lacks immutable provider evidence")
        return {
            "send_status": "success",
            "message_id": provider_message_id,
            "already_reconciled": "1",
        }
    if str(notification.get("state") or "") != NOTIFICATION_AWAITING_REVIEW:
        raise RuntimeError("state repair notification is not awaiting review")
    if str(notification.get("provider_status") or "").strip().casefold() != "success":
        raise RuntimeError("state repair notification does not contain success")
    provider_message_id = str(notification.get("provider_message_id") or "").strip()
    if not provider_message_id or not str(notification.get("sent_at") or "").strip():
        raise RuntimeError("state repair notification lacks immutable provider evidence")
    client = AlimailClient(
        configuration.alimail_app_id,
        configuration.alimail_app_secret,
    )
    try:
        receipt = await client.receipt(
            sender_email=str(notification.get("sender_email") or ""),
            message_id=provider_message_id,
            idempotency_key=str(notification.get("idempotency_key") or ""),
            subject=str(notification.get("subject") or ""),
            recipient_email=str(notification.get("target") or ""),
            sent_at=str(notification.get("sent_at") or ""),
        )
    finally:
        await client.aclose()
    send_status = str(receipt.get("send_status") or "").strip().casefold()
    verified_message_id = str(receipt.get("message_id") or "").strip()
    if send_status != "success" or not verified_message_id:
        raise RuntimeError("provider did not verify an exact success receipt")
    return {"send_status": send_status, "message_id": verified_message_id}


async def _preview(
    notification_store: ShipmentNotificationStore,
    shipment_store: ShipmentWorkflowStore,
    configuration: NotificationConfiguration,
) -> dict[str, Any]:
    state_notification = _latest_required_notification(
        notification_store,
        STATE_REPAIR_ORDER,
    )
    provider_receipt = await _verify_provider_success(
        state_notification,
        configuration,
    )
    dhl_job = _find_dhl_job(shipment_store)
    _dhl_detail(dhl_job)
    draft_source = _latest_required_notification(
        notification_store,
        DRAFT_REPAIR_ORDER,
    )
    draft_already_correct = False
    try:
        _validate_corrected_draft(draft_source)
        draft_already_correct = True
    except RuntimeError:
        progress = (
            int(draft_source.get("package_complete") or 0),
            int(draft_source.get("package_total") or 0),
            int(draft_source.get("package_missing") or 0),
        )
        if progress != (2, 4, 2):
            raise RuntimeError(
                f"draft repair source differs from approved 2/4: {progress}"
            )
    return {
        "orders": list(APPROVED_ORDERS),
        "state_repair": {
            "notification_id": int(state_notification["id"]),
            "stored_state": str(state_notification.get("state") or ""),
            "provider_success_verified": provider_receipt["send_status"] == "success",
            "already_reconciled": str(state_notification.get("state") or "")
            == NOTIFICATION_DELIVERED,
        },
        "dhl_repair": {
            "logistics_no": str(dhl_job.get("logistics_no") or ""),
            "stored_carrier_raw": str(dhl_job.get("carrier_raw") or ""),
            "normalized_carrier": normalize_carrier_name(
                str(dhl_job.get("carrier_raw") or dhl_job.get("carrier") or "")
            ),
            "tracking_no": str(dhl_job.get("international_tracking_no") or ""),
            "already_ready": str(dhl_job.get("logistics_state") or "")
            == LOGISTICS_READY
            and str(dhl_job.get("carrier") or "") == "DHL",
        },
        "draft_repair": {
            "source_notification_id": int(draft_source["id"]),
            "source_revision": int(draft_source["revision"]),
            "progress": "2/4",
            "already_correct": draft_already_correct,
        },
        "external_send_calls": 0,
    }


async def _apply(
    notification_store: ShipmentNotificationStore,
    shipment_store: ShipmentWorkflowStore,
    configuration: NotificationConfiguration,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    state_notification = _latest_required_notification(
        notification_store,
        STATE_REPAIR_ORDER,
    )
    receipt = await _verify_provider_success(state_notification, configuration)
    if str(state_notification.get("state") or "") == NOTIFICATION_DELIVERED:
        repaired_state = state_notification
    else:
        repaired_state = notification_store.reconcile_verified_provider_success(
            int(state_notification["id"]),
            expected_provider_message_id=str(
                state_notification.get("provider_message_id") or ""
            ),
            verified_provider_message_id=receipt["message_id"],
            actor=REPAIR_ACTOR,
            note=REPAIR_NOTE,
        )
    if str(repaired_state.get("state") or "") != NOTIFICATION_DELIVERED:
        raise RuntimeError("state repair did not finish as DELIVERED")

    dhl_job = _find_dhl_job(shipment_store)
    if not (
        str(dhl_job.get("logistics_state") or "") == LOGISTICS_READY
        and str(dhl_job.get("carrier") or "") == "DHL"
    ):
        detail = _dhl_detail(dhl_job)
        updated = shipment_store.complete_logistics_attempt(
            str(dhl_job["logistics_no"]),
            detail,
            state=LOGISTICS_READY,
            last_error=None,
            expected_version=int(dhl_job.get("version") or 0),
            run_id=REPAIR_ACTOR,
        )
        if not updated:
            raise RuntimeError("DHL repair lost its version/lease guard")
    repaired_dhl = _find_dhl_job(shipment_store)
    if not (
        str(repaired_dhl.get("carrier_raw") or "") == DHL_RAW_CARRIER
        and str(repaired_dhl.get("carrier") or "") == "DHL"
        and str(repaired_dhl.get("logistics_state") or "") == LOGISTICS_READY
    ):
        raise RuntimeError("DHL repair postcondition failed")

    current_draft = _latest_required_notification(
        notification_store,
        DRAFT_REPAIR_ORDER,
    )
    try:
        _validate_corrected_draft(current_draft)
        corrected_draft = current_draft
    except RuntimeError:
        corrected_draft = notification_store.reopen_for_review(
            int(current_draft["id"]),
            configuration,
            actor=REPAIR_ACTOR,
            note=REPAIR_NOTE,
        )
        _validate_corrected_draft(corrected_draft)

    return {
        "state_repair": {
            "notification_id": int(repaired_state["id"]),
            "state": str(repaired_state["state"]),
        },
        "dhl_repair": {
            "logistics_no": str(repaired_dhl["logistics_no"]),
            "carrier_raw": str(repaired_dhl["carrier_raw"]),
            "carrier": str(repaired_dhl["carrier"]),
            "logistics_state": str(repaired_dhl["logistics_state"]),
        },
        "draft_repair": {
            "notification_id": int(corrected_draft["id"]),
            "revision": int(corrected_draft["revision"]),
            "state": str(corrected_draft["state"]),
            "template_version": str(corrected_draft["template_version"]),
            "available_soon_count": str(corrected_draft["body"]).count(
                "Available soon"
            ),
        },
        "external_send_calls": 0,
        "preview": dict(preview),
    }


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
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
    configuration = NotificationConfiguration.from_mapping(values)
    notification_store = ShipmentNotificationStore(queue_path)
    shipment_store = ShipmentWorkflowStore(queue_path)
    preview = await _preview(notification_store, shipment_store, configuration)
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "preview",
        "generated_at": utc_now(),
        "database": str(queue_path),
        "preview": preview,
        "external_send_calls": 0,
    }
    if not args.apply:
        return result
    if int(args.confirm_order_count or 0) != len(APPROVED_ORDERS):
        raise RuntimeError(
            f"--apply requires --confirm-order-count {len(APPROVED_ORDERS)}"
        )
    owner = f"{REPAIR_ACTOR}:{uuid.uuid4().hex}"
    if not notification_store.try_acquire_scan_lock(owner, lease_seconds=1800):
        raise RuntimeError("customer notification scan is active; repair was not started")
    backup_path: Path | None = None
    try:
        backup_path = _backup_database(queue_path)
        result["backup"] = str(backup_path)
        result["apply"] = await _apply(
            notification_store,
            shipment_store,
            configuration,
            preview,
        )
        with notification_store.connect() as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"post-repair database integrity failed: {integrity}")
        result["database_integrity"] = integrity
    except Exception:
        if backup_path is not None:
            _restore_database(backup_path, queue_path)
            result["restored_after_failure"] = True
        raise
    finally:
        notification_store.release_scan_lock(owner)
    return result


def main() -> int:
    args = _parser().parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = args.output or (
        args.workspace / "output" / f"notification_case_repair_{stamp}.json"
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
            "external_send_calls": 0,
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
                "orders": list(APPROVED_ORDERS),
                "external_send_calls": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
