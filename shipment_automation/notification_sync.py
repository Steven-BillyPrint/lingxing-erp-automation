from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .notification_domain import (
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    PACKAGE_MANUAL,
    PACKAGE_OVERSEAS_AUTO,
    NotificationConfiguration,
    OrderProductSnapshot,
    PackageSnapshot,
    analyze_order_products,
    customer_carrier_display_name,
    normalize_email,
    normalize_phone,
    normalize_product_sku,
    normalize_recipient_name,
    shorten_product_title,
)
from .notification_store import ShipmentNotificationStore


RecipientNameResolver = Callable[
    [str, tuple[str, ...]], Awaitable[str | None]
]


_CARRIER_ALIASES = (
    "carrier_name",
    "carrier",
    "logistics_company_name",
    "logistics_company",
    "shipping_company",
    "express_company",
    "actual_carrier",
    "provider_name",
)
_PACKAGE_ID_ALIASES = (
    "wo_number",
    "wms_order_no",
    "package_no",
    "package_number",
    "outbound_order_no",
    "delivery_order_no",
)
_WMS_RECIPIENT_NAME_ALIASES = (
    "consignee",
    "consignee_name",
    "receiver_name",
    "recipient_name",
)
_WMS_RECIPIENT_PHONE_ALIASES = (
    "consignee_phone",
    "consigneePhone",
)
_ORDER_PLATFORM_ALIASES = (
    "platform_order_no",
    "platform_order_name",
    "platform_order_nos",
)
_ORDER_ITEM_ALIASES = (
    "item_info",
    "itemInfo",
    "order_item_list",
    "orderItemList",
    "items",
)
_ITEM_KEY_ALIASES = (
    "global_item_no",
    "globalItemNo",
    "id",
    "item_id",
    "itemId",
    "order_item_no",
    "orderItemNo",
)
_ITEM_SKU_ALIASES = ("local_sku", "localSku", "sku")
_ITEM_TITLE_ALIASES = ("title", "product_title", "productTitle")
_ITEM_DATA_ALIASES = ("data_json", "dataJson")
_ORDER_EMAIL_ALIASES = (
    "buyer_email",
    "buyerEmail",
    "receiver_email",
    "receiverEmail",
    "recipient_email",
    "recipientEmail",
)
_PLATFORM_CODE_ALIASES = ("platform_code", "platformCode", "platform_id", "platformId")
_PLATFORM_NAME_ALIASES = (
    "platform_name",
    "platformName",
    "platform",
    "order_from_name",
    "orderFromName",
)
_STORE_NAME_ALIASES = ("shop_name", "shopName", "store_name", "storeName")
_SITE_NAME_ALIASES = (
    "site_name",
    "siteName",
    "marketplace_name",
    "marketplaceName",
)
_ORDER_PAGE_SIZE = 200
_MAX_ORDER_PAGES = 10
_TERMINAL_WMS_STATUS_WORDS = (
    "\u5df2\u622a\u5355",  # 已截单
    "\u5df2\u53d6\u6d88",  # 已取消
    "\u53d6\u6d88",  # 取消
    "\u5df2\u5173\u95ed",  # 已关闭
    "\u5173\u95ed",  # 关闭
    "\u4f5c\u5e9f",  # 作废
    "\u7ec8\u6b62",  # 终止
    "cut off",
    "cut-off",
    "cancelled",
    "canceled",
    "closed",
    "voided",
    "terminated",
)


@dataclass(frozen=True)
class _PlatformOrderFacts:
    system_order_nos: tuple[str, ...]
    products: tuple[OrderProductSnapshot, ...]
    emails: tuple[str, ...]
    sales_platform_code: str = ""
    sales_platform_name: str = ""
    store_name: str = ""
    site_name: str = ""


def _canonical_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _mapping_tree(root: Mapping[str, Any], max_depth: int = 5) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    pending: list[tuple[Mapping[str, Any], int]] = [(root, 0)]
    seen: set[int] = set()
    while pending:
        current, depth = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        output.append(current)
        if depth >= max_depth:
            continue
        for value in current.values():
            if isinstance(value, Mapping):
                pending.append((value, depth + 1))
            elif isinstance(value, (list, tuple)):
                pending.extend(
                    (item, depth + 1) for item in value if isinstance(item, Mapping)
                )
    return output


def _lookup(mappings: Sequence[Mapping[str, Any]], aliases: Sequence[str]) -> str:
    wanted = {_canonical_key(alias) for alias in aliases}
    for mapping in mappings:
        for key, value in mapping.items():
            if _canonical_key(key) not in wanted:
                continue
            if isinstance(value, (str, int, float)) and str(value).strip():
                return str(value).strip()
    return ""


def _lookup_values(
    mappings: Sequence[Mapping[str, Any]], aliases: Sequence[str]
) -> tuple[str, ...]:
    wanted = {_canonical_key(alias) for alias in aliases}
    output: list[str] = []
    for mapping in mappings:
        for key, value in mapping.items():
            if _canonical_key(key) not in wanted:
                continue
            values = value if isinstance(value, (list, tuple, set)) else (value,)
            for item in values:
                if not isinstance(item, (str, int, float)):
                    continue
                text = str(item).strip()
                if text and text not in output:
                    output.append(text)
    return tuple(output)


def _wms_status_code(row: Mapping[str, Any]) -> int | None:
    value = row.get("status")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wms_flag_is_set(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "none",
        "null",
    }


def is_terminal_wms_row(row: Mapping[str, Any]) -> bool:
    """Return whether a historical WMS row must never reach a customer draft."""

    # Lingxing currently uses status=4 for an outbound order that has been cut
    # off. Treat the numeric code as authoritative because localized status text
    # can be absent or vary between API versions.
    if _wms_status_code(row) == 4:
        return True
    mappings = _mapping_tree(row, max_depth=2)
    cancel_status = _lookup(
        mappings,
        (
            "cancel_status",
            "cancelStatus",
            "is_cancelled",
            "is_canceled",
            "cancelled",
            "canceled",
        ),
    )
    if _wms_flag_is_set(cancel_status):
        return True
    status_name = _lookup(
        mappings,
        ("status_name", "statusName", "order_status_name", "state_name"),
    ).casefold()
    return any(word.casefold() in status_name for word in _TERMINAL_WMS_STATUS_WORDS)


def package_from_wms_row(
    row: Mapping[str, Any],
    *,
    platform_order_no: str,
    manual_system_order_nos: frozenset[str] | set[str],
    instruction_system_order_nos: frozenset[str] | set[str] = frozenset(),
) -> PackageSnapshot:
    """Build one package using local queue membership as the only type rule."""

    mappings = _mapping_tree(row, max_depth=2)
    system_order_no = str(
        row.get("order_number") or row.get("global_order_no") or ""
    ).strip()
    if not system_order_no:
        raise ValueError("WMS package does not contain a system order number")
    package_id = _lookup(mappings, _PACKAGE_ID_ALIASES)
    if not package_id:
        raise ValueError("WMS package does not contain a stable package identifier")
    package_key = f"{system_order_no}:{package_id}"
    shipment_type = (
        PACKAGE_MANUAL
        if system_order_no in manual_system_order_nos
        else PACKAGE_OVERSEAS_AUTO
    )
    waybill = str(row.get("waybill_no") or "").strip()
    tracking = str(row.get("tracking_no") or "").strip()
    final_tracking = waybill if shipment_type == PACKAGE_MANUAL else tracking
    customer_visible = system_order_no not in instruction_system_order_nos
    visibility_reason = "" if customer_visible else "instruction"
    carrier_raw = _lookup(mappings, _CARRIER_ALIASES)
    if not carrier_raw:
        carrier_raw = _lookup(mappings, ("logistics_type_name",))
        carrier_raw = re.sub(
            r"^(?:手动|manual)\s*[-–—]?\s*", "", carrier_raw, flags=re.I
        )
    carrier = customer_carrier_display_name(carrier_raw, final_tracking)
    source_hash = hashlib.sha256(
        json.dumps(
            {
                "package_key": package_key,
                "system_order_no": system_order_no,
                "shipment_type": shipment_type,
                "carrier_raw": carrier_raw,
                "carrier": carrier,
                "waybill_no": waybill,
                "tracking_no": tracking,
                "final_tracking_no": final_tracking,
                "customer_visible": customer_visible,
                "visibility_reason": visibility_reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return PackageSnapshot(
        package_key=package_key,
        platform_order_no=platform_order_no,
        system_order_no=system_order_no,
        shipment_type=shipment_type,
        carrier_raw=carrier_raw,
        carrier=carrier,
        waybill_no=waybill,
        tracking_no=tracking,
        final_tracking_no=final_tracking,
        source_payload_hash=source_hash,
        customer_visible=customer_visible,
        visibility_reason=visibility_reason,
    )


def _row_platform_numbers(row: Mapping[str, Any]) -> tuple[str, ...]:
    return _lookup_values(_mapping_tree(row, max_depth=2), _ORDER_PLATFORM_ALIASES)


def _order_record_payload(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    payload = getattr(record, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _order_record_system_order_no(record: Any) -> str:
    if isinstance(record, Mapping):
        direct = record.get("global_order_no") or record.get("system_order_no")
    else:
        direct = getattr(record, "global_order_no", None)
    payload = _order_record_payload(record)
    return str(
        direct
        or payload.get("global_order_no")
        or payload.get("system_order_no")
        or ""
    ).strip()


def _order_record_platform_numbers(record: Any) -> tuple[str, ...]:
    output: list[str] = []
    direct = (
        record.get("order_number")
        if isinstance(record, Mapping)
        else getattr(record, "order_number", None)
    )
    direct_text = str(direct or "").strip()
    if direct_text:
        output.append(direct_text)
    for value in _lookup_values(
        _mapping_tree(_order_record_payload(record), max_depth=3),
        _ORDER_PLATFORM_ALIASES,
    ):
        if value not in output:
            output.append(value)
    return tuple(output)


def _record_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for alias in _ORDER_ITEM_ALIASES:
        value = payload.get(alias)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _truthy_deleted(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "deleted"}


def _product_from_order_item(
    item: Mapping[str, Any],
    *,
    platform_order_no: str,
    system_order_no: str,
    item_index: int,
    source_sequence: int,
) -> OrderProductSnapshot:
    mappings = _mapping_tree(item, max_depth=1)
    item_key = _lookup(mappings, _ITEM_KEY_ALIASES) or f"index-{item_index}"
    local_sku = _lookup(mappings, _ITEM_SKU_ALIASES)
    raw_title = _lookup(mappings, _ITEM_TITLE_ALIASES)
    raw_data: object = None
    for alias in _ITEM_DATA_ALIASES:
        if alias in item:
            raw_data = item.get(alias)
            break
    metadata_valid = True
    if raw_data in (None, ""):
        data: Mapping[str, Any] = {}
    elif isinstance(raw_data, Mapping):
        data = raw_data
    elif isinstance(raw_data, str):
        try:
            decoded = json.loads(raw_data)
        except (TypeError, json.JSONDecodeError):
            decoded = None
        if isinstance(decoded, Mapping):
            data = decoded
        else:
            data = {}
            metadata_valid = False
    else:
        data = {}
        metadata_valid = False
    snapshot_image = data.get("snapshot_image")
    if isinstance(snapshot_image, Mapping):
        has_main_image = any(
            value is not None and str(value).strip()
            for value in snapshot_image.values()
        )
    elif isinstance(snapshot_image, (list, tuple)):
        has_main_image = bool(snapshot_image)
    else:
        has_main_image = bool(str(snapshot_image or "").strip())
    display_title = shorten_product_title(raw_title)
    is_instruction = normalize_product_sku(local_sku) == "instruction"
    source_hash = hashlib.sha256(
        json.dumps(
            {
                "system_order_no": system_order_no,
                "item_key": item_key,
                "source_sequence": source_sequence,
                "local_sku": local_sku,
                "raw_title": raw_title,
                "display_title": display_title,
                "has_main_image": has_main_image,
                "metadata_valid": metadata_valid,
                "is_instruction": is_instruction,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return OrderProductSnapshot(
        platform_order_no=platform_order_no,
        system_order_no=system_order_no,
        item_key=item_key,
        source_sequence=source_sequence,
        local_sku=local_sku,
        raw_title=raw_title,
        display_title=display_title,
        has_main_image=has_main_image,
        metadata_valid=metadata_valid,
        is_instruction=is_instruction,
        source_payload_hash=source_hash,
    )


async def _read_platform_order_facts(
    gateway: Any, platform_order_no: str
) -> _PlatformOrderFacts:
    output: list[str] = []
    products: list[OrderProductSnapshot] = []
    emails: list[str] = []
    platform_codes: list[str] = []
    platform_names: list[str] = []
    store_names: list[str] = []
    site_names: list[str] = []
    offset = 0
    for _page_number in range(_MAX_ORDER_PAGES):
        page = await gateway.list_orders(
            offset=offset,
            length=_ORDER_PAGE_SIZE,
            filters={"platform_order_nos": [platform_order_no]},
            browser=None,
        )
        items = tuple(page.items)
        for record in items:
            platforms = _order_record_platform_numbers(record)
            if platform_order_no not in platforms:
                raise ValueError(
                    f"order API returned a row outside platform {platform_order_no}"
                )
            system_order_no = _order_record_system_order_no(record)
            if not system_order_no:
                raise ValueError(
                    f"order API returned a row without system order number for {platform_order_no}"
                )
            if system_order_no not in output:
                output.append(system_order_no)
            payload = _order_record_payload(record)
            mappings = _mapping_tree(payload, max_depth=3)
            for raw_email in _lookup_values(mappings, _ORDER_EMAIL_ALIASES):
                normalized_email = normalize_email(raw_email)
                if normalized_email and normalized_email not in emails:
                    emails.append(normalized_email)
            for destination, aliases in (
                (platform_codes, _PLATFORM_CODE_ALIASES),
                (platform_names, _PLATFORM_NAME_ALIASES),
                (store_names, _STORE_NAME_ALIASES),
                (site_names, _SITE_NAME_ALIASES),
            ):
                for value in _lookup_values(mappings, aliases):
                    if value not in destination:
                        destination.append(value)
            item_rows = _record_items(payload)
            for item_index, item in enumerate(item_rows, start=1):
                if _truthy_deleted(item.get("is_delete")):
                    continue
                products.append(
                    _product_from_order_item(
                        item,
                        platform_order_no=platform_order_no,
                        system_order_no=system_order_no,
                        item_index=item_index,
                        source_sequence=len(products) + 1,
                    )
                )
        offset += len(items)
        if not items:
            break
        if page.total is not None and offset >= int(page.total):
            break
        if page.total is None and len(items) < _ORDER_PAGE_SIZE:
            break
    else:
        raise ValueError(f"order API pagination exceeded limit for {platform_order_no}")
    if not output:
        raise ValueError(f"order API did not find platform {platform_order_no}")
    return _PlatformOrderFacts(
        system_order_nos=tuple(output),
        products=tuple(products),
        emails=tuple(emails),
        sales_platform_code=platform_codes[0] if platform_codes else "",
        sales_platform_name=platform_names[0] if platform_names else "",
        store_name=store_names[0] if store_names else "",
        site_name=site_names[0] if site_names else "",
    )


async def _read_all_wms_rows(
    gateway: Any, system_order_nos: Sequence[str]
) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    for start in range(0, len(system_order_nos), 50):
        order_chunk = list(system_order_nos[start : start + 50])
        offset = 0
        while True:
            page = await gateway.list_wms_orders(
                filters={
                    "page": (offset // 200) + 1,
                    "page_size": 200,
                    "order_number_arr": order_chunk,
                },
                offset=offset,
                length=200,
                browser=None,
            )
            output.extend(row for row in page.items if isinstance(row, Mapping))
            offset += len(page.items)
            if not page.items:
                break
            if page.total is not None and offset >= int(page.total):
                break
            if page.total is None and len(page.items) < 200:
                break
    return output


async def sync_notification_drafts(
    gateway: Any,
    store: ShipmentNotificationStore,
    configuration: NotificationConfiguration,
    *,
    contact_backfill: Callable[
        [Sequence[Mapping[str, Any]]], Mapping[str, int]
    ]
    | None = None,
    platform_order_nos: Sequence[str] | None = None,
    recipient_name_resolver: RecipientNameResolver | None = None,
) -> dict[str, int]:
    """Read Lingxing facts and create local review drafts; never send externally."""

    targets = store.notification_scan_targets(platform_order_nos)
    backfill_report: dict[str, int] = {}
    # Without a local JSON resolver (mainly unit-level integration use), API
    # fallback is allowed for every target. The desktop always supplies the
    # resolver and narrows this set to orders with no usable matching JSON.
    api_fallback_eligible = {
        str(target.get("platform_order_no") or "").strip()
        for target in targets
        if str(target.get("platform_order_no") or "").strip()
    }
    if contact_backfill:
        try:
            raw_backfill_report = dict(contact_backfill(targets))
            api_fallback_eligible = {
                str(value or "").strip()
                for value in raw_backfill_report.pop(
                    "_api_fallback_eligible_platforms", ()
                )
                if str(value or "").strip()
            }
            backfill_report = {
                str(key): int(value or 0)
                for key, value in raw_backfill_report.items()
            }
        except Exception:
            # Contact recovery is supplementary and must not prevent package
            # facts for otherwise valid orders from being refreshed.
            backfill_report = {"contact_backfill_error_count": len(targets)}
            api_fallback_eligible = set()

    report = {
        "eligible_order_count": len(targets),
        "contact_update_count": 0,
        "product_update_count": 0,
        "package_update_count": 0,
        "notification_count": 0,
        "new_draft_count": 0,
        "partial_logistics_order_count": 0,
        "waiting_logistics_order_count": 0,
        "unchanged_order_count": 0,
        "missing_system_order_count": 0,
        "failed_order_count": 0,
        "wms_validation_error_count": 0,
        "wms_terminal_row_excluded_count": 0,
        "sync_error_count": 0,
        "sync_state_persist_error_count": 0,
        "recipient_name_conflict_count": 0,
        "recipient_name_selection_count": 0,
        "recipient_name_selection_unresolved_count": 0,
        "recipient_name_retry_alert_count": 0,
        **backfill_report,
    }
    targets_by_platform = {
        str(target["platform_order_no"]): target for target in targets
    }

    def record_success(platform: str) -> None:
        target = targets_by_platform[platform]
        try:
            store.record_notification_sync_success(
                platform,
                erp_completed_at=str(target.get("erp_completed_at") or ""),
            )
        except Exception:
            report["sync_state_persist_error_count"] += 1

    def record_retry(platform: str, error: str) -> int:
        target = targets_by_platform[platform]
        try:
            return store.record_notification_sync_retry(
                platform,
                erp_completed_at=str(target.get("erp_completed_at") or ""),
                error=error,
            )
        except Exception:
            report["sync_state_persist_error_count"] += 1
            return 0

    resolved_systems: dict[str, tuple[str, ...]] = {}
    manual_systems: dict[str, frozenset[str]] = {}
    product_facts: dict[str, tuple[OrderProductSnapshot, ...]] = {}
    order_contact_facts: dict[str, _PlatformOrderFacts] = {}
    instruction_systems: dict[str, frozenset[str]] = {}
    system_owners: dict[str, str] = {}
    failed_platforms: set[str] = set()
    facts_by_platform: dict[str, _PlatformOrderFacts] = {}
    facts_errors: dict[str, str] = {}
    facts_semaphore = asyncio.Semaphore(4)

    async def read_platform_facts(platform: str) -> None:
        try:
            async with facts_semaphore:
                facts_by_platform[platform] = await _read_platform_order_facts(
                    gateway,
                    platform,
                )
        except Exception as exc:
            facts_errors[platform] = (
                str(exc).strip() or type(exc).__name__
            )[:500]

    await asyncio.gather(
        *(
            read_platform_facts(str(target["platform_order_no"]))
            for target in targets
        )
    )

    for target in targets:
        platform = str(target["platform_order_no"])
        local_systems = frozenset(
            str(value).strip()
            for value in target["system_order_nos"]
            if str(value).strip()
        )
        try:
            if platform in facts_errors:
                raise RuntimeError(facts_errors[platform])
            facts = facts_by_platform[platform]
            all_platform_systems = facts.system_order_nos
            platform_products = facts.products
            if local_systems.difference(all_platform_systems):
                raise ValueError(
                    f"order API omitted local shipment systems for {platform}"
                )
            for system_order_no in all_platform_systems:
                previous_owner = system_owners.setdefault(system_order_no, platform)
                if previous_owner != platform:
                    failed_platforms.update((previous_owner, platform))
            resolved_systems[platform] = all_platform_systems
            manual_systems[platform] = local_systems
            product_facts[platform] = platform_products
            order_contact_facts[platform] = facts
            product_analysis = analyze_order_products(
                platform_products,
                expected_system_order_nos=all_platform_systems,
            )
            instruction_systems[platform] = frozenset(
                product_analysis.instruction_system_order_nos
            )
        except Exception as exc:
            failed_platforms.add(platform)
            record_retry(
                platform,
                str(exc).strip() or type(exc).__name__,
            )

    valid_platforms = tuple(
        platform
        for platform in resolved_systems
        if platform not in failed_platforms
    )

    all_system_orders = tuple(
        dict.fromkeys(
            order_no
            for platform in valid_platforms
            for systems in (resolved_systems[platform],)
            for order_no in systems
        )
    )
    try:
        rows = (
            await _read_all_wms_rows(gateway, all_system_orders)
            if all_system_orders
            else []
        )
    except Exception as exc:
        failed_platforms.update(valid_platforms)
        retry_error = str(exc).strip() or type(exc).__name__
        for platform in valid_platforms:
            record_retry(platform, retry_error)
        report["failed_order_count"] = len(failed_platforms)
        report["sync_error_count"] = len(failed_platforms)
        return report

    by_system: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        system_order = str(
            row.get("order_number") or row.get("global_order_no") or ""
        ).strip()
        if not system_order:
            report["wms_validation_error_count"] += 1
            continue
        if system_order not in system_owners or system_owners[system_order] in failed_platforms:
            report["wms_validation_error_count"] += 1
            continue
        by_system[system_order].append(row)

    for target in targets:
        platform = str(target["platform_order_no"])
        if platform in failed_platforms or platform not in resolved_systems:
            continue
        systems = resolved_systems[platform]
        try:
            report["product_update_count"] += int(
                store.replace_product_scan(
                    platform,
                    product_facts[platform],
                    systems,
                )
            )
            platform_rows = [
                row
                for system_order in systems
                for row in by_system.get(system_order, ())
            ]
            authoritative_wms_systems = tuple(
                dict.fromkeys(
                    str(
                        row.get("order_number")
                        or row.get("global_order_no")
                        or ""
                    ).strip()
                    for row in platform_rows
                    if str(
                        row.get("order_number")
                        or row.get("global_order_no")
                        or ""
                    ).strip()
                )
            )
            valid_platform_rows = [
                row for row in platform_rows if not is_terminal_wms_row(row)
            ]
            report["wms_terminal_row_excluded_count"] += (
                len(platform_rows) - len(valid_platform_rows)
            )
            packages: list[PackageSnapshot] = []
            package_keys: set[str] = set()
            for row in valid_platform_rows:
                row_platforms = _row_platform_numbers(row)
                if row_platforms and platform not in row_platforms:
                    raise ValueError(
                        f"WMS returned a package outside platform {platform}"
                    )
                package = package_from_wms_row(
                    row,
                    platform_order_no=platform,
                    manual_system_order_nos=manual_systems[platform],
                    instruction_system_order_nos=instruction_systems[platform],
                )
                if package.package_key in package_keys:
                    raise ValueError(
                        f"WMS returned duplicate package {package.package_key}"
                    )
                package_keys.add(package.package_key)
                packages.append(package)
            wms_names = tuple(
                dict.fromkeys(
                    name
                    for name in (
                        normalize_recipient_name(
                            _lookup(
                                _mapping_tree(row, max_depth=2),
                                _WMS_RECIPIENT_NAME_ALIASES,
                            )
                        )
                        for row in valid_platform_rows
                    )
                    if name
                )
            )
            wms_phones = tuple(
                dict.fromkeys(
                    normalized
                    for normalized in (
                        normalize_phone(raw_phone)
                        for row in valid_platform_rows
                        for raw_phone in _lookup_values(
                            _mapping_tree(row, max_depth=2),
                            _WMS_RECIPIENT_PHONE_ALIASES,
                        )
                    )
                    if normalized
                )
            )
            recipient_name = wms_names[0] if len(wms_names) == 1 else ""
            recipient_name_conflict_unresolved = False
            if len(wms_names) > 1:
                report["recipient_name_conflict_count"] += 1
                selected_name = ""
                if recipient_name_resolver is not None:
                    try:
                        selected_name = normalize_recipient_name(
                            await recipient_name_resolver(platform, wms_names)
                        )
                    except Exception:
                        selected_name = ""
                if selected_name in wms_names:
                    recipient_name = selected_name
                    report["recipient_name_selection_count"] += 1
                else:
                    recipient_name_conflict_unresolved = True
                    report["recipient_name_selection_unresolved_count"] += 1
            report["contact_update_count"] += int(
                store.upsert_wms_recipient_name(
                    platform,
                    recipient_name,
                    system_order_nos=systems,
                    preserve_existing_when_empty=(
                        not recipient_name_conflict_unresolved
                    ),
                )
            )
            if platform in api_fallback_eligible:
                contact_facts = order_contact_facts[platform]
                existing_contact = store.get_contact(platform)
                needs_api_email = not (
                    existing_contact is not None
                    and existing_contact.email_source
                    == CONTACT_SOURCE_CUSTOMIZATION_JSON
                )
                needs_api_phone = not (
                    existing_contact is not None
                    and existing_contact.phone_source
                    == CONTACT_SOURCE_CUSTOMIZATION_JSON
                )
                if needs_api_email and len(contact_facts.emails) > 1:
                    raise ValueError(
                        f"order API returned conflicting buyer emails for {platform}"
                    )
                if needs_api_phone and len(wms_phones) > 1:
                    raise ValueError(
                        f"WMS returned conflicting recipient phones for {platform}"
                    )
                fallback_email = (
                    contact_facts.emails[0]
                    if needs_api_email and len(contact_facts.emails) == 1
                    else None
                )
                fallback_phone = (
                    wms_phones[0]
                    if needs_api_phone and len(wms_phones) == 1
                    else None
                )
                if fallback_email is not None or fallback_phone is not None:
                    report["contact_update_count"] += int(
                        store.upsert_lingxing_api_contact(
                            platform,
                            email=fallback_email,
                            phone=fallback_phone,
                            sales_platform_code=contact_facts.sales_platform_code,
                            sales_platform_name=contact_facts.sales_platform_name,
                            store_name=contact_facts.store_name,
                            site_name=contact_facts.site_name,
                            system_order_nos=systems,
                        )
                    )
            merge_report = store.merge_package_scan(
                platform,
                packages,
                systems,
                authoritative_observed_system_order_nos=authoritative_wms_systems,
            )
            report["package_update_count"] += int(merge_report["changed"])
            report["missing_system_order_count"] += int(
                merge_report["missing_system_order_count"]
            )
            package_complete = int(merge_report["package_complete"])
            package_missing = int(merge_report["package_missing"])
            if recipient_name_conflict_unresolved:
                before = store.get_latest_notification(platform)
                notification = store.prepare_notification(
                    platform,
                    configuration,
                    blocked_reason="recipient_name_conflict_unresolved",
                    allow_incomplete_issue=True,
                )
                if notification is not None:
                    report["notification_count"] += 1
                    if before is None or int(before["id"]) != int(notification["id"]):
                        report["new_draft_count"] += 1
                    else:
                        report["unchanged_order_count"] += 1
                failed_platforms.add(platform)
                record_retry(
                    platform,
                    "recipient name conflict remains unresolved after user selection",
                )
                report["recipient_name_retry_alert_count"] += 1
                continue
            if package_complete <= 0:
                report["waiting_logistics_order_count"] += 1
                record_retry(platform, "waiting for complete package logistics")
                continue
            if package_missing > 0:
                report["partial_logistics_order_count"] += 1

            before = store.get_latest_notification(platform)
            notification = store.prepare_notification(platform, configuration)
            if notification is None:
                report["waiting_logistics_order_count"] += 1
                record_retry(platform, "notification draft is not ready")
                continue
            report["notification_count"] += 1
            if before is None or int(before["id"]) != int(notification["id"]):
                report["new_draft_count"] += 1
            else:
                report["unchanged_order_count"] += 1
            if package_missing > 0:
                record_retry(platform, "waiting for remaining package logistics")
            else:
                record_success(platform)
        except Exception as exc:
            failed_platforms.add(platform)
            record_retry(
                platform,
                str(exc).strip() or type(exc).__name__,
            )

    # Include targets that failed before package preparation and keep the public
    # report aggregate-only so addresses, tracking numbers and paths never leak.
    report["failed_order_count"] = len(failed_platforms)
    report["sync_error_count"] = len(failed_platforms)
    return report


__all__ = [
    "is_terminal_wms_row",
    "package_from_wms_row",
    "sync_notification_drafts",
]
