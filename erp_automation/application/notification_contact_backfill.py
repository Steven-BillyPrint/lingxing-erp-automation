"""Recover authoritative notification contacts from completed customization JSON.

This compatibility path is intentionally local-only.  It reads the normalized
custom-order workflow dates and the archived customization JSON files, then
updates the notification contact snapshot.  It never calls Lingxing, WMS, mail,
or SMS providers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from erp_automation.persistence import CustomWorkflowStore
from lingxing_automation.parsers.contact import (
    customization_json_has_contact_fields,
    extract_contact_candidates_from_json_items,
)
from lingxing_automation.services.customization_json_parser import (
    parse_customization_json_info,
)
from lingxing_automation.services.folder_builder import (
    find_existing_platform_order_folder,
)
from shipment_automation.notification_domain import (
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
)
from shipment_automation.notification_store import ShipmentNotificationStore


@dataclass(frozen=True)
class ContactBackfillResolution:
    status: str
    email: str = ""
    phone: str = ""

    @property
    def authoritative(self) -> bool:
        return self.status in {"resolved", "authoritative_empty"}


def _parse_workflow_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _workflow_month_dates(workflow: Mapping[str, Any]) -> tuple[date, ...]:
    candidates: list[Any] = []
    stages = workflow.get("stages")
    if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes)):
        indexed = {
            str(stage.get("stage") or ""): stage
            for stage in stages
            if isinstance(stage, Mapping)
        }
        for stage_name in ("contact", "folder"):
            stage = indexed.get(stage_name)
            if stage is not None:
                candidates.append(stage.get("completed_at"))
    candidates.extend(
        (
            workflow.get("last_seen_at"),
            workflow.get("processed_at"),
            workflow.get("created_at"),
            workflow.get("updated_at"),
        )
    )
    months: list[date] = []
    seen: set[tuple[int, int]] = set()
    for value in candidates:
        parsed = _parse_workflow_date(value)
        if parsed is None:
            continue
        key = (parsed.year, parsed.month)
        if key in seen:
            continue
        seen.add(key)
        months.append(parsed.replace(day=1))
    return tuple(months)


def _merge_month_dates(*groups: Sequence[Any]) -> tuple[date, ...]:
    """Normalize workflow and notification dates into unique calendar months."""

    months: list[date] = []
    seen: set[tuple[int, int]] = set()
    for group in groups:
        for value in group:
            parsed = value if isinstance(value, date) else _parse_workflow_date(value)
            if parsed is None:
                continue
            month = parsed.replace(day=1)
            key = (month.year, month.month)
            if key in seen:
                continue
            seen.add(key)
            months.append(month)
    return tuple(months)


def resolve_customization_json_contact(
    workflow_store: CustomWorkflowStore,
    folder_root: str | Path,
    platform_order_no: str,
    *,
    date_hints: Sequence[Any] = (),
    staging_root: str | Path | None = None,
) -> ContactBackfillResolution:
    """Resolve one order without guessing between folders or contact candidates."""

    platform = str(platform_order_no or "").strip()
    workflow = workflow_store.get_workflow(platform)
    workflow_dates = _workflow_month_dates(workflow) if workflow is not None else ()
    month_dates = _merge_month_dates(workflow_dates, date_hints)

    folders: list[Path] = []
    for month_date in month_dates:
        folder = find_existing_platform_order_folder(folder_root, month_date, platform)
        if folder is not None and folder not in folders:
            folders.append(folder)
    if staging_root is not None:
        staging_folder = Path(staging_root) / platform
        try:
            if staging_folder.is_dir() and staging_folder not in folders:
                folders.append(staging_folder)
        except OSError:
            pass
    if not folders:
        if workflow is None and not month_dates:
            return ContactBackfillResolution("workflow_missing")
        if not month_dates:
            return ContactBackfillResolution("workflow_date_missing")
        return ContactBackfillResolution("folder_missing")

    json_seen = False
    matching_items = []
    parse_error_seen = False
    for folder in folders:
        try:
            json_paths = sorted(folder.rglob("*.json"), key=lambda item: str(item))
        except OSError:
            parse_error_seen = True
            continue
        for json_path in json_paths:
            json_seen = True
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict):
                    parse_error_seen = True
                    continue
                info = parse_customization_json_info(
                    payload,
                    raw_json_path=str(json_path),
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                parse_error_seen = True
                continue
            if str(info.order_id or "").strip() == platform:
                matching_items.append(info)

    if not json_seen:
        return ContactBackfillResolution("json_missing")
    if not matching_items:
        return ContactBackfillResolution(
            "parse_error" if parse_error_seen else "order_mismatch"
        )

    candidates = extract_contact_candidates_from_json_items(matching_items)
    if len(candidates) == 1:
        candidate = candidates[0]
        return ContactBackfillResolution(
            "resolved",
            email=str(candidate.email or "").strip(),
            phone=str(candidate.phone or "").strip(),
        )
    if len(candidates) > 1:
        return ContactBackfillResolution("ambiguous")
    if customization_json_has_contact_fields(matching_items):
        return ContactBackfillResolution("authoritative_empty")
    return ContactBackfillResolution("contact_fields_missing")


def backfill_missing_notification_contacts(
    targets: Sequence[Mapping[str, Any]],
    *,
    notification_store: ShipmentNotificationStore,
    workflow_store: CustomWorkflowStore,
    folder_root: str | Path,
    staging_root: str | Path | None = None,
) -> dict[str, Any]:
    """Backfill all eligible non-excluded targets lacking JSON provenance."""

    report = {
        "contact_backfill_candidate_count": 0,
        "contact_backfill_update_count": 0,
        "contact_backfill_resolved_count": 0,
        "contact_backfill_empty_count": 0,
        "contact_backfill_ambiguous_count": 0,
        "contact_backfill_missing_count": 0,
        "contact_backfill_error_count": 0,
        # Consumed internally by notification_sync and removed before the
        # public aggregate report is produced.  These orders have no usable
        # matching customization JSON, so Lingxing API field fallbacks are
        # allowed. Ambiguous, unreadable and explicitly empty JSON is excluded.
        "_api_fallback_eligible_platforms": [],
    }
    for target in targets:
        platform = str(target.get("platform_order_no") or "").strip()
        if not platform:
            continue
        existing = notification_store.get_contact(platform)
        if (
            existing is not None
            and existing.email_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
            and existing.phone_source == CONTACT_SOURCE_CUSTOMIZATION_JSON
        ):
            continue
        report["contact_backfill_candidate_count"] += 1
        try:
            resolution = resolve_customization_json_contact(
                workflow_store,
                folder_root,
                platform,
                date_hints=(target.get("erp_completed_at"),),
                staging_root=staging_root,
            )
        except Exception:
            # The scheduled scan must continue for other orders.  Counts are
            # deliberately aggregate-only so contacts and paths never leak.
            report["contact_backfill_error_count"] += 1
            continue
        if resolution.authoritative:
            system_order_nos = tuple(
                str(value or "").strip()
                for value in target.get("system_order_nos") or ()
                if str(value or "").strip()
            )
            changed = notification_store.upsert_customization_contact(
                platform,
                email=resolution.email,
                phone=resolution.phone,
                system_order_nos=system_order_nos,
            )
            report["contact_backfill_update_count"] += int(changed)
            counter = (
                "contact_backfill_resolved_count"
                if resolution.status == "resolved"
                else "contact_backfill_empty_count"
            )
            report[counter] += 1
        elif resolution.status == "ambiguous":
            report["contact_backfill_ambiguous_count"] += 1
        elif resolution.status == "parse_error":
            report["contact_backfill_error_count"] += 1
        else:
            report["contact_backfill_missing_count"] += 1
            if resolution.status in {
                "workflow_missing",
                "workflow_date_missing",
                "folder_missing",
                "json_missing",
                "order_mismatch",
            }:
                report["_api_fallback_eligible_platforms"].append(platform)
    return report


__all__ = [
    "ContactBackfillResolution",
    "backfill_missing_notification_contacts",
    "resolve_customization_json_contact",
]
