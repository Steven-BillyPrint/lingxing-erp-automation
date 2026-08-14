from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from erp_automation.operations import safe_exception_summary

from .notification_domain import (
    CONTACT_SOURCE_CUSTOMIZATION_JSON,
    PACKAGE_MANUAL,
    PACKAGE_MATCHED_COLUMNS,
    PACKAGE_OVERSEAS_AUTO,
    PACKAGE_UNKNOWN,
    PHONE_VERIFICATION_MATCHED,
    NotificationConfiguration,
    OrderProductSnapshot,
    PackageSnapshot,
    analyze_order_products,
    customer_carrier_display_name,
    normalize_email,
    normalize_phone,
    normalize_product_sku,
    normalize_recipient_name,
    is_amazon_platform,
    is_independent_site_order,
    shorten_product_title,
)
from .notification_store import ShipmentNotificationStore


logger = logging.getLogger(__name__)


RecipientNameResolver = Callable[
    [str, tuple[str, ...]], Awaitable[str | None]
]
SyncProgressCallback = Callable[[str, int], None]


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
_WAREHOUSE_CODE_ALIASES = (
    "t_warehouse_code",
    "warehouse_code",
    "warehouseCode",
    "warehouse_no",
    "warehouseNo",
    "warehouse_sn",
    "warehouseSn",
)
_WAREHOUSE_ID_ALIASES = (
    "wid",
    "sys_wid",
    "warehouse_id",
    "warehouseId",
)
_WAREHOUSE_NAME_ALIASES = (
    "warehouse_name",
    "warehouseName",
    "warehouse",
)
_LOGISTICS_PROVIDER_ALIASES = (
    "logistics_provider_name",
    "logisticsProviderName",
    "provider_name",
    "providerName",
)
_LOGISTICS_TYPE_ALIASES = (
    "logistics_type_name",
    "logisticsTypeName",
)
_OVERSEAS_WAREHOUSE_CODES = frozenset({"CA", "NJ"})
_OVERSEAS_WAREHOUSE_NAME_CODES = {
    "港通洛杉矶仓": "CA",
    "港通新泽西仓": "NJ",
}
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


def is_policy_masked_recipient_name(value: object) -> bool:
    """Return whether WMS supplied an Amazon privacy placeholder, not a name."""

    normalized = normalize_recipient_name(value)
    compact = re.sub(r"[\s,，。:：;；_\-]+", "", normalized).casefold()
    if not compact:
        return False
    chinese_policy = "亚马逊政策" in compact and any(
        marker in compact
        for marker in ("暂停显示", "不允许显示", "无法显示", "禁止显示")
    )
    english_policy = "amazon" in compact and "policy" in compact and any(
        marker in compact
        for marker in (
            "doesnotallowdisplay",
            "notallowedtodisplay",
            "displaypaused",
            "cannotdisplay",
        )
    )
    return chinese_policy or english_policy
_WMS_PACKAGE_ITEM_LIST_ALIASES = (
    "item_info",
    "itemInfo",
    "item_list",
    "itemList",
    "items",
    "product_list",
    "productList",
)
_WMS_PACKAGE_ITEM_SKU_ALIASES = (
    "local_sku",
    "localSku",
    "sku",
    "seller_sku",
    "sellerSku",
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
_ITEM_MARKETPLACE_PRODUCT_ALIASES = (
    "product_no",
    "productNo",
    "asin",
    "amazon_asin",
    "amazonAsin",
)
_ORDER_EMAIL_ALIASES = (
    "buyer_email",
    "buyerEmail",
    "receiver_email",
    "receiverEmail",
    "recipient_email",
    "recipientEmail",
)
_ORDER_PAID_AT_ALIASES = (
    "paid_at",
    "paidAt",
    "payment_time",
    "paymentTime",
    "global_purchase_time",
    "globalPurchaseTime",
    "order_pay_time",
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
_OUTBOUNDED_WMS_STATUS_WORDS = (
    "\u5df2\u51fa\u5e93",  # 已出库
    "\u51fa\u5e93\u5b8c\u6210",  # 出库完成
    "\u5df2\u53d1\u8d27",  # 已发货
    "outbounded",
    "shipped",
    "dispatched",
)
_WAITING_WMS_STATUS_WORDS = (
    "\u5f85\u5ba1\u6838",  # 待审核
    "\u5f85\u4eba\u5de5\u5ba1\u6838",  # 待人工审核
    "\u5f85\u53d1\u8d27",  # 待发货
    "\u5f85\u51fa\u5e93",  # 待出库
    "\u5df2\u5ba1\u6838",  # 已审核
    "pending",
    "waiting",
    "ready to ship",
)
_WMS_STATUS_NAME_ALIASES = (
    "status_name",
    "statusName",
    "order_status_name",
    "orderStatusName",
    "state_name",
    "stateName",
)
_WMS_TERMINAL_FLAG_ALIASES = (
    "cancel_status",
    "cancelStatus",
    "is_cancelled",
    "is_canceled",
    "cancelled",
    "canceled",
    "cutoff_status",
    "cutoffStatus",
    "is_cutoff",
    "isCutoff",
    "intercept_status",
    "interceptStatus",
    "is_closed",
    "isClosed",
)

OUTBOUND_STATE_OUTBOUNDED = "OUTBOUNDED"
OUTBOUND_STATE_TERMINAL = "TERMINAL"
OUTBOUND_STATE_WAITING = "WAITING"
OUTBOUND_STATE_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class _WmsOutboundStatus:
    state: str
    status_code: int | None
    status_name: str
    conflicting: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _PlatformOrderFacts:
    system_order_nos: tuple[str, ...]
    products: tuple[OrderProductSnapshot, ...]
    emails: tuple[str, ...]
    sales_platform_code: str = ""
    sales_platform_name: str = ""
    store_name: str = ""
    site_name: str = ""


@dataclass(frozen=True)
class _DiscoverySnapshot:
    sources: tuple[dict[str, Any], ...]
    facts_by_platform: Mapping[str, _PlatformOrderFacts]
    api_call_count: int = 0


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


def _normalized_warehouse_code(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def _normalized_warehouse_name(value: object) -> str:
    return re.sub(r"[\s\-_—–]+", "", str(value or "").strip()).casefold()


def _manual_logistics_provider(value: object) -> bool:
    return bool(
        re.match(
            r"^(?:手动|manual)(?:\s*[-—–>:：]|\s*$)",
            str(value or "").strip(),
            flags=re.IGNORECASE,
        )
    )


def _meaningful_logistics_provider(value: object) -> bool:
    return str(value or "").strip().casefold() not in {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "none",
        "null",
        "unknown",
        "未知",
    }


@dataclass(frozen=True)
class _WarehouseCodeLookup:
    by_id: Mapping[str, str]
    by_name: Mapping[str, str]


_EMPTY_WAREHOUSE_CODE_LOOKUP = _WarehouseCodeLookup(by_id={}, by_name={})


def _warehouse_code_from_wms_row(
    row: Mapping[str, Any],
    lookup: _WarehouseCodeLookup,
) -> tuple[str, str, str]:
    mappings = _mapping_tree(row, max_depth=2)
    direct_code = _normalized_warehouse_code(
        _lookup(mappings, _WAREHOUSE_CODE_ALIASES)
    )
    warehouse_id = _lookup(mappings, _WAREHOUSE_ID_ALIASES)
    warehouse_name = _lookup(mappings, _WAREHOUSE_NAME_ALIASES)
    mapped_by_id = _normalized_warehouse_code(lookup.by_id.get(warehouse_id, ""))
    mapped_by_name = _normalized_warehouse_code(
        lookup.by_name.get(_normalized_warehouse_name(warehouse_name), "")
    )
    resolved_codes = tuple(
        dict.fromkeys(
            value for value in (direct_code, mapped_by_id, mapped_by_name) if value
        )
    )
    if len(resolved_codes) > 1:
        return "", warehouse_name, "WAREHOUSE_IDENTITY_CONFLICT"
    code = resolved_codes[0] if resolved_codes else ""
    if not code:
        normalized_name = _normalized_warehouse_name(warehouse_name)
        code = _OVERSEAS_WAREHOUSE_NAME_CODES.get(normalized_name, "")
        if not code and normalized_name.upper() in _OVERSEAS_WAREHOUSE_CODES:
            code = normalized_name.upper()
    return code, warehouse_name, ""


def _shipment_type_from_wms_row(
    row: Mapping[str, Any],
    lookup: _WarehouseCodeLookup,
) -> tuple[str, str, str, str]:
    mappings = _mapping_tree(row, max_depth=2)
    warehouse_code, warehouse_name, identity_error = _warehouse_code_from_wms_row(
        row,
        lookup,
    )
    provider_name = _lookup(mappings, _LOGISTICS_PROVIDER_ALIASES)
    logistics_type_name = _lookup(mappings, _LOGISTICS_TYPE_ALIASES)
    provider_is_manual = _manual_logistics_provider(
        provider_name
    ) or _manual_logistics_provider(logistics_type_name)

    # An explicit manual marker in the provider or logistics type means the
    # customer-visible number was entered in waybill_no, regardless of the
    # warehouse. Any other explicit provider represents a system-obtained label
    # whose customer-visible number is tracking_no.
    if provider_is_manual:
        return PACKAGE_MANUAL, warehouse_code, warehouse_name, "MANUAL_PROVIDER"
    if _meaningful_logistics_provider(provider_name):
        return (
            PACKAGE_OVERSEAS_AUTO,
            warehouse_code,
            warehouse_name,
            "SYSTEM_LABEL_PROVIDER",
        )

    # Warehouse identity is only auxiliary evidence. Known overseas warehouses
    # still imply a system label when the provider field is absent, while a
    # default/local warehouse no longer silently implies manual entry.
    if identity_error:
        return PACKAGE_UNKNOWN, warehouse_code, warehouse_name, identity_error
    if warehouse_code in _OVERSEAS_WAREHOUSE_CODES:
        return (
            PACKAGE_OVERSEAS_AUTO,
            warehouse_code,
            warehouse_name,
            "OVERSEAS_WAREHOUSE_FALLBACK",
        )
    return (
        PACKAGE_UNKNOWN,
        warehouse_code,
        warehouse_name,
        "TRACKING_SOURCE_MISSING",
    )


def _wms_status_code(row: Mapping[str, Any]) -> int | None:
    value = row.get("status")
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        return value
    normalized = str(value or "").strip()
    if not re.fullmatch(r"[+-]?\d+", normalized):
        return None
    return int(normalized)


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


def _classify_wms_outbound_status(
    row: Mapping[str, Any],
) -> _WmsOutboundStatus:
    mappings = _mapping_tree(row, max_depth=2)
    status_code = _wms_status_code(row)
    status_name = _lookup(mappings, _WMS_STATUS_NAME_ALIASES).strip()
    folded_name = status_name.casefold()
    terminal_flag = any(
        _wms_flag_is_set(_lookup(mappings, (alias,)))
        for alias in _WMS_TERMINAL_FLAG_ALIASES
    )
    terminal_text = any(
        word.casefold() in folded_name for word in _TERMINAL_WMS_STATUS_WORDS
    )
    outbound_text = any(
        word.casefold() in folded_name for word in _OUTBOUNDED_WMS_STATUS_WORDS
    )
    waiting_text = any(
        word.casefold() in folded_name for word in _WAITING_WMS_STATUS_WORDS
    )
    text_categories = sum((terminal_text, outbound_text, waiting_text))
    if text_categories > 1:
        return _WmsOutboundStatus(
            OUTBOUND_STATE_UNKNOWN,
            status_code,
            status_name,
            conflicting=True,
            reason="wms_status_text_conflict",
        )
    if status_code is None:
        if terminal_flag or terminal_text:
            return _WmsOutboundStatus(
                OUTBOUND_STATE_TERMINAL,
                None,
                status_name,
                reason="wms_terminal_marker_without_status",
            )
        return _WmsOutboundStatus(
            OUTBOUND_STATE_UNKNOWN,
            None,
            status_name,
            reason="wms_status_missing_or_invalid",
        )
    if status_code == 3:
        if terminal_flag or terminal_text or waiting_text:
            return _WmsOutboundStatus(
                OUTBOUND_STATE_UNKNOWN,
                status_code,
                status_name,
                conflicting=True,
                reason="wms_status_code_text_conflict",
            )
        return _WmsOutboundStatus(
            OUTBOUND_STATE_OUTBOUNDED,
            status_code,
            status_name,
            reason="wms_status_3",
        )
    if status_code in {1, 2}:
        if terminal_flag or terminal_text or outbound_text:
            return _WmsOutboundStatus(
                OUTBOUND_STATE_UNKNOWN,
                status_code,
                status_name,
                conflicting=True,
                reason="wms_status_code_text_conflict",
            )
        return _WmsOutboundStatus(
            OUTBOUND_STATE_WAITING,
            status_code,
            status_name,
            reason=f"wms_status_{status_code}",
        )
    if status_code == 4:
        if outbound_text or waiting_text:
            return _WmsOutboundStatus(
                OUTBOUND_STATE_UNKNOWN,
                status_code,
                status_name,
                conflicting=True,
                reason="wms_status_code_text_conflict",
            )
        return _WmsOutboundStatus(
            OUTBOUND_STATE_TERMINAL,
            status_code,
            status_name,
            reason="wms_status_4",
        )
    return _WmsOutboundStatus(
        OUTBOUND_STATE_UNKNOWN,
        status_code,
        status_name,
        reason="wms_status_code_unknown",
    )


def classify_wms_outbound_state(row: Mapping[str, Any]) -> str:
    """Classify a Lingxing WMS row without inferring shipment from tracking data."""

    return _classify_wms_outbound_status(row).state


def is_outbounded_wms_row(row: Mapping[str, Any]) -> bool:
    return classify_wms_outbound_state(row) == OUTBOUND_STATE_OUTBOUNDED


def is_terminal_wms_row(row: Mapping[str, Any]) -> bool:
    """Return whether a historical WMS row must never reach a customer draft."""

    # Lingxing currently uses status=4 for an outbound order that has been cut
    # off. Treat the numeric code as authoritative because localized status text
    # can be absent or vary between API versions.
    if _wms_status_code(row) == 4:
        return True
    mappings = _mapping_tree(row, max_depth=2)
    if any(
        _wms_flag_is_set(_lookup(mappings, (alias,)))
        for alias in _WMS_TERMINAL_FLAG_ALIASES
    ):
        return True
    status_name = _lookup(mappings, _WMS_STATUS_NAME_ALIASES).casefold()
    return any(word.casefold() in status_name for word in _TERMINAL_WMS_STATUS_WORDS)


def _wms_package_is_instruction_only(row: Mapping[str, Any]) -> bool:
    """Hide a mixed-system package only when WMS item detail proves it."""

    list_keys = {_canonical_key(value) for value in _WMS_PACKAGE_ITEM_LIST_ALIASES}
    skus: list[str] = []
    for mapping in _mapping_tree(row, max_depth=2):
        for key, value in mapping.items():
            if _canonical_key(key) not in list_keys or not isinstance(value, (list, tuple)):
                continue
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                sku = _lookup(_mapping_tree(item, max_depth=1), _WMS_PACKAGE_ITEM_SKU_ALIASES)
                if sku:
                    skus.append(normalize_product_sku(sku))
    return bool(skus) and all(value == "instruction" for value in skus)


def package_from_wms_row(
    row: Mapping[str, Any],
    *,
    platform_order_no: str,
    instruction_system_order_nos: frozenset[str] | set[str] = frozenset(),
    warehouse_code_lookup: _WarehouseCodeLookup = _EMPTY_WAREHOUSE_CODE_LOOKUP,
    outbound_observed_at: str = "",
) -> PackageSnapshot:
    """Build one package using only authoritative WMS warehouse identity."""

    outbound_status = _classify_wms_outbound_status(row)
    if outbound_status.state != OUTBOUND_STATE_OUTBOUNDED:
        raise ValueError(
            "WMS package is not confirmed outbound: "
            f"{outbound_status.state}:{outbound_status.reason}"
        )
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
    (
        shipment_type,
        warehouse_code,
        warehouse_name,
        classification_reason,
    ) = _shipment_type_from_wms_row(
        row,
        warehouse_code_lookup,
    )
    waybill = str(row.get("waybill_no") or "").strip()
    tracking = str(row.get("tracking_no") or "").strip()
    if shipment_type == PACKAGE_UNKNOWN:
        if waybill and not tracking:
            shipment_type = PACKAGE_MANUAL
            classification_reason = "WAYBILL_ONLY_FALLBACK"
        elif tracking and not waybill:
            shipment_type = PACKAGE_OVERSEAS_AUTO
            classification_reason = "TRACKING_ONLY_FALLBACK"
        elif (
            waybill
            and tracking
            and waybill.casefold() == tracking.casefold()
        ):
            shipment_type = PACKAGE_MATCHED_COLUMNS
            classification_reason = "MATCHING_TRACKING_COLUMNS"

    if shipment_type == PACKAGE_MANUAL:
        final_tracking = waybill
        selection_reason = "manual_waybill" if final_tracking else "tracking_pending"
    elif shipment_type == PACKAGE_OVERSEAS_AUTO:
        final_tracking = tracking
        selection_reason = (
            "system_label_tracking" if final_tracking else "tracking_pending"
        )
    elif shipment_type == PACKAGE_MATCHED_COLUMNS:
        final_tracking = tracking or waybill
        selection_reason = "matching_columns"
    else:
        final_tracking = ""
        selection_reason = (
            "tracking_source_unresolved"
            if waybill or tracking
            else "tracking_pending"
        )
    customer_visible = (
        system_order_no not in instruction_system_order_nos
        and not _wms_package_is_instruction_only(row)
    )
    visibility_reason = selection_reason if customer_visible else "instruction"
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
                "warehouse_code": warehouse_code,
                "warehouse_name": warehouse_name,
                "classification_reason": classification_reason,
                "customer_visible": customer_visible,
                "visibility_reason": visibility_reason,
                "wms_outbound_order_no": package_id,
                "wms_status_code": outbound_status.status_code,
                "wms_status_name": outbound_status.status_name,
                "outbound_state": outbound_status.state,
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
        wms_outbound_order_no=package_id,
        wms_status_code=outbound_status.status_code,
        wms_status_name=outbound_status.status_name,
        outbound_state=outbound_status.state,
        outbound_observed_at=(
            outbound_observed_at.strip()
            or datetime.now(UTC).replace(microsecond=0).isoformat()
        ),
        source_payload_hash=source_hash,
        customer_visible=customer_visible,
        visibility_reason=visibility_reason,
    )


def _row_platform_numbers(row: Mapping[str, Any]) -> tuple[str, ...]:
    return _lookup_values(_mapping_tree(row, max_depth=2), _ORDER_PLATFORM_ALIASES)


@dataclass(frozen=True)
class _PlatformOutboundEvaluation:
    state: str
    reason: str
    package_rows: tuple[Mapping[str, Any], ...]
    expected_customer_systems: tuple[str, ...]
    observed_customer_systems: tuple[str, ...]
    waiting_package_count: int = 0
    unknown_status_count: int = 0
    conflicting_status_count: int = 0
    terminal_row_count: int = 0
    diagnostics: tuple[dict[str, Any], ...] = ()


def _wms_package_identifier(row: Mapping[str, Any]) -> str:
    return _lookup(_mapping_tree(row, max_depth=2), _PACKAGE_ID_ALIASES).strip()


def _outbound_diagnostic(
    *,
    platform_order_no: str,
    system_order_no: str,
    row: Mapping[str, Any] | None,
    state: str,
    reason: str,
) -> dict[str, Any]:
    status = (
        _classify_wms_outbound_status(row)
        if row is not None
        else _WmsOutboundStatus(
            OUTBOUND_STATE_WAITING,
            None,
            "",
            reason="wms_record_missing",
        )
    )
    return {
        "platform_order_no": platform_order_no,
        "system_order_no": system_order_no,
        "wms_outbound_order_no": (
            _wms_package_identifier(row) if row is not None else ""
        ),
        "wms_status_code": status.status_code,
        "wms_status_name": status.status_name,
        "has_waybill_no": bool(
            row is not None
            and str(row.get("waybill_no") or row.get("tracking_no") or "").strip()
        ),
        "outbound_state": state,
        "reason": reason,
    }


def _evaluate_platform_outbound(
    *,
    platform_order_no: str,
    system_order_nos: Sequence[str],
    instruction_system_order_nos: frozenset[str] | set[str],
    rows_by_system: Mapping[str, Sequence[Mapping[str, Any]]],
) -> _PlatformOutboundEvaluation:
    expected_customer_systems = tuple(
        system_order_no
        for system_order_no in system_order_nos
        if system_order_no not in instruction_system_order_nos
    )
    if not expected_customer_systems:
        return _PlatformOutboundEvaluation(
            OUTBOUND_STATE_UNKNOWN,
            "no_customer_visible_system_order",
            (),
            (),
            (),
        )

    package_rows: list[Mapping[str, Any]] = []
    observed_systems: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    waiting_packages = 0
    unknown_statuses = 0
    conflicts = 0
    terminal_rows = 0

    for system_order_no in expected_customer_systems:
        visible_rows = [
            row
            for row in rows_by_system.get(system_order_no, ())
            if not _wms_package_is_instruction_only(row)
        ]
        if not visible_rows:
            waiting_packages += 1
            diagnostics.append(
                _outbound_diagnostic(
                    platform_order_no=platform_order_no,
                    system_order_no=system_order_no,
                    row=None,
                    state=OUTBOUND_STATE_WAITING,
                    reason="customer_visible_wms_record_missing",
                )
            )
            continue
        observed_systems.append(system_order_no)
        grouped_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in visible_rows:
            package_id = _wms_package_identifier(row)
            if not package_id:
                unknown_statuses += 1
                diagnostics.append(
                    _outbound_diagnostic(
                        platform_order_no=platform_order_no,
                        system_order_no=system_order_no,
                        row=row,
                        state=OUTBOUND_STATE_UNKNOWN,
                        reason="wms_package_identifier_missing",
                    )
                )
                continue
            grouped_rows[package_id].append(row)

        for same_package_rows in grouped_rows.values():
            statuses = [
                _classify_wms_outbound_status(row) for row in same_package_rows
            ]
            logistics_signatures = {
                (
                    _lookup(_mapping_tree(row, max_depth=2), _CARRIER_ALIASES),
                    str(row.get("waybill_no") or "").strip(),
                    str(row.get("tracking_no") or "").strip(),
                )
                for row in same_package_rows
            }
            package_conflict = (
                any(status.conflicting for status in statuses)
                or len({status.state for status in statuses}) > 1
                or len(logistics_signatures) > 1
            )
            if package_conflict:
                conflicts += 1
                diagnostics.append(
                    _outbound_diagnostic(
                        platform_order_no=platform_order_no,
                        system_order_no=system_order_no,
                        row=same_package_rows[0],
                        state=OUTBOUND_STATE_UNKNOWN,
                        reason="conflicting_wms_package_snapshot",
                    )
                )
                continue
            status = statuses[0]
            if status.state == OUTBOUND_STATE_OUTBOUNDED:
                package_rows.append(same_package_rows[0])
                continue
            if status.state == OUTBOUND_STATE_WAITING:
                waiting_packages += 1
            elif status.state == OUTBOUND_STATE_TERMINAL:
                terminal_rows += len(same_package_rows)
            else:
                unknown_statuses += 1
            diagnostics.append(
                _outbound_diagnostic(
                    platform_order_no=platform_order_no,
                    system_order_no=system_order_no,
                    row=same_package_rows[0],
                    state=status.state,
                    reason=status.reason,
                )
            )

    if conflicts:
        state = OUTBOUND_STATE_UNKNOWN
        reason = "conflicting_wms_status"
    elif package_rows:
        state = OUTBOUND_STATE_OUTBOUNDED
        reason = (
            "partial_customer_visible_packages_outbounded"
            if waiting_packages or unknown_statuses or terminal_rows
            else "all_customer_visible_packages_outbounded"
        )
    elif unknown_statuses:
        state = OUTBOUND_STATE_UNKNOWN
        reason = "unknown_wms_outbound_status"
    elif waiting_packages:
        state = OUTBOUND_STATE_WAITING
        reason = "waiting_for_all_customer_visible_packages_outbound"
    elif terminal_rows:
        # A terminal WMS row ends only that outbound attempt.  It must not
        # override a different active WAITING/OUTBOUNDED attempt for the same
        # system order.  Reaching this branch means every observed attempt is
        # terminal and there is no missing customer-visible system to retry.
        state = OUTBOUND_STATE_TERMINAL
        reason = "all_wms_outbound_attempts_terminal"
    else:
        state = OUTBOUND_STATE_UNKNOWN
        reason = "customer_visible_wms_state_unresolved"
    return _PlatformOutboundEvaluation(
        state,
        reason,
        tuple(package_rows),
        expected_customer_systems,
        tuple(dict.fromkeys(observed_systems)),
        waiting_package_count=waiting_packages,
        unknown_status_count=unknown_statuses,
        conflicting_status_count=conflicts,
        terminal_row_count=terminal_rows,
        diagnostics=tuple(diagnostics),
    )


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


def _marketplace_product_identity(item: Mapping[str, Any]) -> str:
    """Return the original marketplace product identity carried by an order item.

    Lingxing's documented multi-platform order shape uses ``product_no`` for the
    platform product number; Amazon responses can also expose the same value as
    ``asin``.  Local SKUs and Lingxing's own item IDs are intentionally excluded:
    manually created/split fulfillment rows have those values too and must remain
    eligible for customer-notification compensation.
    """

    return _lookup(
        _mapping_tree(item, max_depth=1),
        _ITEM_MARKETPLACE_PRODUCT_ALIASES,
    )


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


_ORDER_RECORD_INACTIVE_FLAG_ALIASES = (
    "is_delete",
    "isDelete",
    "deleted",
    "is_deleted",
    "isDeleted",
    "is_cancelled",
    "isCancelled",
    "is_canceled",
    "isCanceled",
)


def _order_record_is_inactive(record: Any) -> bool:
    """Return whether Lingxing explicitly marks this system-order row inactive."""

    wanted = {
        _canonical_key(alias) for alias in _ORDER_RECORD_INACTIVE_FLAG_ALIASES
    }
    if not isinstance(record, Mapping):
        for alias in _ORDER_RECORD_INACTIVE_FLAG_ALIASES:
            if _truthy_deleted(getattr(record, alias, None)):
                return True
    for mapping in _mapping_tree(_order_record_payload(record), max_depth=2):
        for key, value in mapping.items():
            if _canonical_key(key) in wanted and _truthy_deleted(value):
                return True
    return False


def _platform_order_facts_from_records(
    platform_order_no: str,
    records: Sequence[Any],
) -> _PlatformOrderFacts:
    output: list[str] = []
    products: list[OrderProductSnapshot] = []
    emails: list[str] = []
    platform_codes: list[str] = []
    platform_names: list[str] = []
    store_names: list[str] = []
    site_names: list[str] = []
    for record in records:
        platforms = _order_record_platform_numbers(record)
        if platform_order_no not in platforms:
            raise ValueError(
                f"order API returned a row outside platform {platform_order_no}"
            )
        if _order_record_is_inactive(record):
            continue
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
    if not output:
        raise ValueError(f"order API did not find active platform {platform_order_no}")
    return _PlatformOrderFacts(
        system_order_nos=tuple(output),
        products=tuple(products),
        emails=tuple(emails),
        sales_platform_code=platform_codes[0] if platform_codes else "",
        sales_platform_name=platform_names[0] if platform_names else "",
        store_name=store_names[0] if store_names else "",
        site_name=site_names[0] if site_names else "",
    )


async def _read_order_records(
    gateway: Any,
    platform_order_nos: Sequence[str],
) -> tuple[tuple[Any, ...], int]:
    platforms = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in platform_order_nos
            if str(value or "").strip()
        )
    )
    if not platforms:
        return (), 0
    output: list[Any] = []
    offset = 0
    api_call_count = 0
    for _page_number in range(_MAX_ORDER_PAGES):
        page = await gateway.list_orders(
            offset=offset,
            length=_ORDER_PAGE_SIZE,
            filters={
                "platform_order_nos": list(platforms),
                "include_delete": False,
            },
            browser=None,
        )
        api_call_count += 1
        items = tuple(page.items)
        output.extend(items)
        offset += len(items)
        if not items:
            break
        if page.total is not None and offset >= int(page.total):
            break
        if page.total is None and len(items) < _ORDER_PAGE_SIZE:
            break
    else:
        raise ValueError("order API pagination exceeded limit for platform batch")
    return tuple(output), api_call_count


async def _read_platform_order_facts_with_count(
    gateway: Any,
    platform_order_no: str,
) -> tuple[_PlatformOrderFacts, int]:
    records, api_call_count = await _read_order_records(
        gateway,
        (platform_order_no,),
    )
    return (
        _platform_order_facts_from_records(platform_order_no, records),
        api_call_count,
    )


async def _read_platform_order_facts(
    gateway: Any, platform_order_no: str
) -> _PlatformOrderFacts:
    facts, _api_call_count = await _read_platform_order_facts_with_count(
        gateway,
        platform_order_no,
    )
    return facts


async def _read_platform_order_facts_batch(
    gateway: Any,
    platform_order_nos: Sequence[str],
    *,
    batch_size: int = 50,
) -> tuple[dict[str, _PlatformOrderFacts], dict[str, str], int]:
    """Read platform facts in batches and isolate only omitted/invalid orders."""

    platforms = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in platform_order_nos
            if str(value or "").strip()
        )
    )
    facts_by_platform: dict[str, _PlatformOrderFacts] = {}
    errors: dict[str, str] = {}
    api_call_count = 0
    normalized_batch_size = max(1, min(int(batch_size), 100))
    for start in range(0, len(platforms), normalized_batch_size):
        chunk = platforms[start : start + normalized_batch_size]
        requested = set(chunk)
        try:
            records, call_count = await _read_order_records(gateway, chunk)
            api_call_count += call_count
        except Exception:
            records = ()
        records_by_platform: dict[str, list[Any]] = defaultdict(list)
        for record in records:
            owners = requested.intersection(_order_record_platform_numbers(record))
            if not owners:
                continue
            for platform in owners:
                records_by_platform[platform].append(record)
        for platform in chunk:
            platform_records = records_by_platform.get(platform, ())
            if platform_records:
                try:
                    facts_by_platform[platform] = _platform_order_facts_from_records(
                        platform,
                        platform_records,
                    )
                    continue
                except Exception:
                    pass
            # Some Lingxing deployments do not accept more than one platform
            # number even though the field is an array.  Fall back only for a
            # missing/invalid member instead of discarding the successful batch.
            try:
                facts, call_count = await _read_platform_order_facts_with_count(
                    gateway,
                    platform,
                )
                api_call_count += call_count
                facts_by_platform[platform] = facts
            except Exception as exc:
                errors[platform] = (str(exc).strip() or type(exc).__name__)[:500]
    return facts_by_platform, errors, api_call_count


async def _read_all_wms_rows_with_count(
    gateway: Any, system_order_nos: Sequence[str]
) -> tuple[list[Mapping[str, Any]], int]:
    output: list[Mapping[str, Any]] = []
    api_call_count = 0
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
            api_call_count += 1
            output.extend(row for row in page.items if isinstance(row, Mapping))
            offset += len(page.items)
            if not page.items:
                break
            if page.total is not None and offset >= int(page.total):
                break
            if page.total is None and len(page.items) < 200:
                break
    return output, api_call_count


async def _read_all_wms_rows(
    gateway: Any, system_order_nos: Sequence[str]
) -> list[Mapping[str, Any]]:
    rows, _api_call_count = await _read_all_wms_rows_with_count(
        gateway,
        system_order_nos,
    )
    return rows


def _wms_terminal_marker_values(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the cancellation/cut-off fields used by classification."""

    wanted = {_canonical_key(alias) for alias in _WMS_TERMINAL_FLAG_ALIASES}
    output: dict[str, Any] = {}
    for mapping in _mapping_tree(row, max_depth=2):
        for key, value in mapping.items():
            if _canonical_key(key) not in wanted:
                continue
            if value is None or isinstance(value, (bool, int, float)):
                output.setdefault(str(key), value)
                continue
            if isinstance(value, str):
                output.setdefault(str(key), value.strip()[:100])
    return output


async def diagnose_notification_outbound(
    gateway: Any,
    platform_order_no: str,
) -> dict[str, Any]:
    """Read one platform order and its WMS rows without changing notification state.

    The response is intentionally field-whitelisted.  It contains the exact
    order/WMS identifiers and state inputs needed to explain classification,
    but never includes contacts, addresses, credentials, or arbitrary raw API
    payloads.
    """

    platform = str(platform_order_no or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", platform):
        raise ValueError("platform order number is invalid")

    facts = await _read_platform_order_facts(gateway, platform)
    product_analysis = analyze_order_products(
        facts.products,
        expected_system_order_nos=facts.system_order_nos,
    )
    instruction_systems = frozenset(
        product_analysis.instruction_system_order_nos
    )
    rows = await _read_all_wms_rows(gateway, facts.system_order_nos)
    rows_by_system: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        system_order_no = str(
            row.get("order_number") or row.get("global_order_no") or ""
        ).strip()
        if system_order_no in facts.system_order_nos:
            rows_by_system[system_order_no].append(row)

    evaluation = _evaluate_platform_outbound(
        platform_order_no=platform,
        system_order_nos=facts.system_order_nos,
        instruction_system_order_nos=instruction_systems,
        rows_by_system=rows_by_system,
    )
    wms_rows: list[dict[str, Any]] = []
    for row in rows:
        system_order_no = str(
            row.get("order_number") or row.get("global_order_no") or ""
        ).strip()
        if system_order_no not in facts.system_order_nos:
            continue
        status = _classify_wms_outbound_status(row)
        wms_rows.append(
            {
                "system_order_no": system_order_no,
                "platform_order_nos": list(_row_platform_numbers(row)),
                "wms_outbound_order_no": _wms_package_identifier(row),
                "raw_status": (
                    row.get("status")
                    if row.get("status") is None
                    or isinstance(row.get("status"), (str, int, float, bool))
                    else type(row.get("status")).__name__
                ),
                "wms_status_code": status.status_code,
                "wms_status_name": status.status_name,
                "outbound_state": status.state,
                "classification_reason": status.reason,
                "conflicting": status.conflicting,
                "terminal_markers": _wms_terminal_marker_values(row),
                "waybill_no": str(row.get("waybill_no") or "").strip()[:200],
                "tracking_no": str(row.get("tracking_no") or "").strip()[:200],
                "instruction_only": (
                    system_order_no in instruction_systems
                    or _wms_package_is_instruction_only(row)
                ),
            }
        )
    wms_rows.sort(
        key=lambda item: (
            str(item["system_order_no"]),
            str(item["wms_outbound_order_no"]),
            str(item["raw_status"]),
        )
    )
    return {
        "platform_order_no": platform,
        "query_parameters": {
            "platform_order_nos": [platform],
            "order_number_arr": list(facts.system_order_nos),
        },
        "system_order_nos": list(facts.system_order_nos),
        "instruction_system_order_nos": sorted(instruction_systems),
        "outbound_state": evaluation.state,
        "outbound_reason": evaluation.reason,
        "waiting_package_count": evaluation.waiting_package_count,
        "unknown_status_count": evaluation.unknown_status_count,
        "conflicting_status_count": evaluation.conflicting_status_count,
        "terminal_row_count": evaluation.terminal_row_count,
        "diagnostics": [dict(item) for item in evaluation.diagnostics],
        "wms_rows": wms_rows,
        "read_only": True,
    }


async def _read_warehouse_code_lookup(gateway: Any) -> _WarehouseCodeLookup:
    list_warehouses = getattr(gateway, "list_warehouses", None)
    if not callable(list_warehouses):
        return _EMPTY_WAREHOUSE_CODE_LOOKUP

    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    offset = 0
    for _ in range(10):
        page = await list_warehouses(
            warehouse_type=3,
            is_delete=0,
            offset=offset,
            length=1000,
        )
        items = tuple(getattr(page, "items", ()) or ())
        for item in items:
            if isinstance(item, Mapping):
                payload = item
                mappings = _mapping_tree(item, max_depth=1)
                warehouse_id = _lookup(mappings, _WAREHOUSE_ID_ALIASES)
                warehouse_name = _lookup(mappings, _WAREHOUSE_NAME_ALIASES)
            else:
                payload = getattr(item, "payload", {})
                if not isinstance(payload, Mapping):
                    payload = {}
                warehouse_id = str(getattr(item, "identifier", "") or "").strip()
                warehouse_name = str(getattr(item, "name", "") or "").strip()
            code = _normalized_warehouse_code(
                _lookup(_mapping_tree(payload, max_depth=1), _WAREHOUSE_CODE_ALIASES)
            )
            if not code:
                continue
            if warehouse_id:
                by_id[warehouse_id] = code
            normalized_name = _normalized_warehouse_name(warehouse_name)
            if normalized_name:
                by_name[normalized_name] = code

        next_offset = getattr(page, "next_offset", None)
        if next_offset is not None:
            offset = int(next_offset)
            continue
        offset += len(items)
        total = getattr(page, "total", None)
        if not items or total is None or offset >= int(total) or len(items) < 1000:
            break
    else:
        raise ValueError("warehouse master pagination exceeded safety limit")
    return _WarehouseCodeLookup(by_id=by_id, by_name=by_name)


async def _discover_recent_amazon_order_snapshot(
    gateway: Any,
    configuration: NotificationConfiguration,
    filter_windows: Sequence[Mapping[str, Any]],
) -> _DiscoverySnapshot:
    """Read a complete recent-order snapshot and return notification candidates."""

    records: dict[tuple[str, str], Any] = {}
    api_call_count = 0
    for raw_window in filter_windows:
        offset = 0
        for _page_number in range(_MAX_ORDER_PAGES):
            page = await gateway.list_orders(
                offset=offset,
                length=_ORDER_PAGE_SIZE,
                filters=dict(raw_window),
                browser=None,
            )
            api_call_count += 1
            items = tuple(page.items)
            for record in items:
                system_order_no = _order_record_system_order_no(record)
                for platform_order_no in _order_record_platform_numbers(record):
                    if not system_order_no or not platform_order_no:
                        continue
                    records[(platform_order_no, system_order_no)] = record
            offset += len(items)
            if not items:
                break
            if page.total is not None and offset >= int(page.total):
                break
            if page.total is None and len(items) < _ORDER_PAGE_SIZE:
                break
        else:
            raise ValueError("recent Amazon order pagination exceeded safety limit")

    grouped: dict[str, dict[str, Any]] = {}
    for (platform, system), record in records.items():
        if is_independent_site_order(platform):
            continue
        if _order_record_is_inactive(record):
            continue
        payload = _order_record_payload(record)
        mappings = _mapping_tree(payload, max_depth=3)
        platform_code = _lookup(mappings, _PLATFORM_CODE_ALIASES)
        platform_name = _lookup(mappings, _PLATFORM_NAME_ALIASES)
        if not is_amazon_platform(
            platform,
            platform_code,
            platform_name,
            configuration,
        ):
            continue
        group = grouped.setdefault(
            platform,
            {
                "systems": [],
                "products": [],
                "purchased_at": "",
                "contains_manual_fulfillment_item": False,
            },
        )
        if system not in group["systems"]:
            group["systems"].append(system)
        purchased_at = _lookup(mappings, _ORDER_PAID_AT_ALIASES)
        if purchased_at and not group["purchased_at"]:
            group["purchased_at"] = purchased_at
        for item_index, item in enumerate(_record_items(payload), start=1):
            if _truthy_deleted(item.get("is_delete")):
                continue
            product = _product_from_order_item(
                item,
                platform_order_no=platform,
                system_order_no=system,
                item_index=item_index,
                source_sequence=len(group["products"]) + 1,
            )
            group["products"].append(product)
            if (
                not product.is_instruction
                and not _marketplace_product_identity(item)
            ):
                group["contains_manual_fulfillment_item"] = True

    discovered: list[dict[str, Any]] = []
    for platform, group in grouped.items():
        products = tuple(group["products"])
        contains_instruction = any(product.is_instruction for product in products)
        contains_manual_fulfillment_item = bool(
            group.get("contains_manual_fulfillment_item")
        )
        if not (contains_instruction or contains_manual_fulfillment_item):
            continue
        reasons = []
        if contains_manual_fulfillment_item:
            reasons.append("MANUAL_FULFILLMENT_ITEM")
        if contains_instruction:
            reasons.append("CONTAINS_INSTRUCTION")
        discovered.append(
            {
                "platform_order_no": platform,
                "system_order_nos": tuple(group["systems"]),
                "purchased_at": str(group["purchased_at"] or ""),
                "eligibility_reason": ",".join(reasons),
            }
        )
    facts_by_platform: dict[str, _PlatformOrderFacts] = {}
    for source in discovered:
        platform = str(source["platform_order_no"])
        platform_records = [
            record
            for (record_platform, _system), record in records.items()
            if record_platform == platform
        ]
        facts_by_platform[platform] = _platform_order_facts_from_records(
            platform,
            platform_records,
        )
    return _DiscoverySnapshot(
        sources=tuple(discovered),
        facts_by_platform=facts_by_platform,
        api_call_count=api_call_count,
    )


async def _discover_recent_amazon_orders(
    gateway: Any,
    configuration: NotificationConfiguration,
    filter_windows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only discovered source rows."""

    snapshot = await _discover_recent_amazon_order_snapshot(
        gateway,
        configuration,
        filter_windows,
    )
    return list(snapshot.sources)


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
    discovery_filter_windows: Sequence[Mapping[str, Any]] | None = None,
    progress_callback: SyncProgressCallback | None = None,
) -> dict[str, Any]:
    """Read Lingxing facts and create local review drafts; never send externally."""

    total_started = time.monotonic()
    cached_facts_by_platform: dict[str, _PlatformOrderFacts] = {}
    discovery_api_call_count = 0
    discovery_started = time.monotonic()

    def progress(message: str, percent: int) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(str(message), max(0, min(int(percent), 100)))
        except Exception:
            # Progress reporting must never turn a read-only sync into failure.
            return

    progress("正在扫描最近订单并识别客户通知来源。", 10)
    discovery_report: dict[str, Any] = {}
    if platform_order_nos is None and discovery_filter_windows is not None:
        try:
            discovery_snapshot = await _discover_recent_amazon_order_snapshot(
                gateway,
                configuration,
                discovery_filter_windows,
            )
            cached_facts_by_platform.update(discovery_snapshot.facts_by_platform)
            discovery_api_call_count = int(discovery_snapshot.api_call_count)
            discovery_report = store.merge_full_scan_sources(
                discovery_snapshot.sources
            )
        except Exception as error:
            # A partial list scan must never replace the active discovery set.
            safe_error = safe_exception_summary(error)
            discovery_report = {
                "discovery_error_count": 1,
                "discovery_error_id": str(safe_error.get("error_id") or ""),
                "discovery_error_type": str(
                    safe_error.get("exception_type") or type(error).__name__
                ),
                "discovery_error": safe_error,
            }
            for field in (
                "request_id",
                "api_code",
                "http_status",
                "operation",
            ):
                if safe_error.get(field) is not None:
                    discovery_report[f"discovery_error_{field}"] = safe_error[field]
    targets = store.notification_scan_targets(platform_order_nos)
    # Without a local JSON resolver (mainly unit-level integration use), API
    # fallback is allowed for every target. The desktop always supplies the
    # resolver and narrows this set to orders with no usable matching JSON.
    api_fallback_eligible = {
        str(target.get("platform_order_no") or "").strip()
        for target in targets
        if str(target.get("platform_order_no") or "").strip()
    }

    report = {
        "eligible_order_count": len(targets),
        "contact_update_count": 0,
        "product_update_count": 0,
        "package_update_count": 0,
        "notification_count": 0,
        "new_draft_count": 0,
        "partial_logistics_order_count": 0,
        "waiting_logistics_order_count": 0,
        "waiting_outbound_order_count": 0,
        "waiting_outbound_package_count": 0,
        "unknown_outbound_status_count": 0,
        "conflicting_wms_status_count": 0,
        "blocked_existing_notification_count": 0,
        "outbound_block_diagnostics": [],
        "tracking_selection_review_count": 0,
        "tracking_selection_diagnostics": [],
        "unchanged_order_count": 0,
        "missing_system_order_count": 0,
        "inactive_system_order_count": 0,
        "failed_order_count": 0,
        "wms_validation_error_count": 0,
        "wms_terminal_row_excluded_count": 0,
        "warehouse_lookup_error_count": 0,
        "sync_error_count": 0,
        "sync_state_persist_error_count": 0,
        "recipient_name_conflict_count": 0,
        "recipient_name_policy_masked_count": 0,
        "recipient_name_selection_prompt_count": 0,
        "recipient_name_selection_count": 0,
        "recipient_name_selection_reused_count": 0,
        "recipient_name_selection_unresolved_count": 0,
        "recipient_name_retry_alert_count": 0,
        "package_event_count": 0,
        "baseline_suppressed_count": 0,
        "corrected_package_event_count": 0,
        "order_discovery_duration_ms": round(
            (time.monotonic() - discovery_started) * 1000
        ),
        "order_discovery_api_call_count": discovery_api_call_count,
        "order_facts_duration_ms": 0,
        "order_facts_api_call_count": 0,
        "order_facts_cache_hit_count": 0,
        "wms_duration_ms": 0,
        "wms_api_call_count": 0,
        "warehouse_lookup_duration_ms": 0,
        "contact_backfill_duration_ms": 0,
        "database_merge_duration_ms": 0,
        "total_duration_ms": 0,
        **discovery_report,
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

    def recover_contacts() -> None:
        nonlocal api_fallback_eligible
        if contact_backfill is None:
            return
        contact_started = time.monotonic()
        try:
            raw_backfill_report = dict(contact_backfill(targets))
            api_fallback_eligible = {
                str(value or "").strip()
                for value in raw_backfill_report.pop(
                    "_api_fallback_eligible_platforms", ()
                )
                if str(value or "").strip()
            }
            report.update(
                {
                    str(key): int(value or 0)
                    for key, value in raw_backfill_report.items()
                }
            )
        except Exception:
            # Contact recovery is supplementary and must not prevent package
            # facts for otherwise valid orders from being refreshed.
            report["contact_backfill_error_count"] = len(targets)
            api_fallback_eligible = set()
        finally:
            report["contact_backfill_duration_ms"] = int(
                report.get("contact_backfill_duration_ms") or 0
            ) + round((time.monotonic() - contact_started) * 1000)

    resolved_systems: dict[str, tuple[str, ...]] = {}
    product_facts: dict[str, tuple[OrderProductSnapshot, ...]] = {}
    order_contact_facts: dict[str, _PlatformOrderFacts] = {}
    instruction_systems: dict[str, frozenset[str]] = {}
    system_owners: dict[str, str] = {}
    failed_platforms: set[str] = set()
    requested_platforms = tuple(
        str(target["platform_order_no"]) for target in targets
    )
    facts_by_platform: dict[str, _PlatformOrderFacts] = {
        platform: cached_facts_by_platform[platform]
        for platform in requested_platforms
        if platform in cached_facts_by_platform
    }
    facts_errors: dict[str, str] = {}
    report["order_facts_cache_hit_count"] = len(facts_by_platform)
    uncached_platforms = tuple(
        platform
        for platform in requested_platforms
        if platform not in facts_by_platform
    )
    progress(
        f"正在批量核对 {len(targets)} 个平台订单的有效系统子单。",
        25,
    )
    facts_started = time.monotonic()
    if uncached_platforms:
        fetched_facts, facts_errors, facts_api_calls = (
            await _read_platform_order_facts_batch(
                gateway,
                uncached_platforms,
            )
        )
        facts_by_platform.update(fetched_facts)
        report["order_facts_api_call_count"] = facts_api_calls
    report["order_facts_duration_ms"] = round(
        (time.monotonic() - facts_started) * 1000
    )

    for target in targets:
        platform = str(target["platform_order_no"])
        expected_local_systems = frozenset(
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
            # The current non-deleted order snapshot is authoritative for active
            # system children. Older local/full-scan identifiers are retained
            # only as diagnostics and must not become synthetic packages.
            report["inactive_system_order_count"] += len(
                expected_local_systems.difference(all_platform_systems)
            )
            for system_order_no in all_platform_systems:
                previous_owner = system_owners.setdefault(system_order_no, platform)
                if previous_owner != platform:
                    failed_platforms.update((previous_owner, platform))
            resolved_systems[platform] = all_platform_systems
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
            report["blocked_existing_notification_count"] += int(
                store.record_outbound_eligibility(
                    platform,
                    outbound_state=OUTBOUND_STATE_UNKNOWN,
                    reason="platform_order_facts_unavailable",
                    expected_system_order_nos=tuple(expected_local_systems),
                    snapshot_complete=False,
                )
            )
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
    progress(
        f"正在批量查询 {len(all_system_orders)} 个有效系统子单的 WMS 状态。",
        45,
    )
    wms_started = time.monotonic()
    try:
        if all_system_orders:
            rows, wms_api_call_count = await _read_all_wms_rows_with_count(
                gateway,
                all_system_orders,
            )
        else:
            rows, wms_api_call_count = [], 0
        report["wms_api_call_count"] = wms_api_call_count
    except Exception as exc:
        report["wms_duration_ms"] = round(
            (time.monotonic() - wms_started) * 1000
        )
        failed_platforms.update(valid_platforms)
        retry_error = str(exc).strip() or type(exc).__name__
        for platform in valid_platforms:
            report["blocked_existing_notification_count"] += int(
                store.record_outbound_eligibility(
                    platform,
                    outbound_state=OUTBOUND_STATE_UNKNOWN,
                    reason="wms_snapshot_unavailable",
                    expected_system_order_nos=resolved_systems[platform],
                    snapshot_complete=False,
                )
            )
            record_retry(platform, retry_error)
        recover_contacts()
        report["failed_order_count"] = len(failed_platforms)
        report["sync_error_count"] = len(failed_platforms)
        report["total_duration_ms"] = round(
            (time.monotonic() - total_started) * 1000
        )
        return report
    report["wms_duration_ms"] = round(
        (time.monotonic() - wms_started) * 1000
    )

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

    warehouse_code_lookup = _EMPTY_WAREHOUSE_CODE_LOOKUP
    warehouse_started = time.monotonic()
    try:
        warehouse_code_lookup = await _read_warehouse_code_lookup(gateway)
    except Exception:
        # The WMS row normally carries a warehouse name.  A failed master-data
        # lookup may use that name, but a row with no usable identity remains
        # UNKNOWN instead of silently becoming an overseas shipment.
        report["warehouse_lookup_error_count"] += 1
    finally:
        report["warehouse_lookup_duration_ms"] = round(
            (time.monotonic() - warehouse_started) * 1000
        )

    # Contact data may be cached early, but no WMS-name conflict is shown until
    # at least one customer-visible package is authoritatively outbound and
    # logistics-ready.
    progress("正在复用或补齐缺失的客户联系方式。", 65)
    recover_contacts()
    outbound_observed_at = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )

    progress("正在合并真实 WMS 包裹并生成待审核通知。", 75)
    database_merge_started = time.monotonic()
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
            for row in platform_rows:
                row_platforms = _row_platform_numbers(row)
                if row_platforms and platform not in row_platforms:
                    raise ValueError(
                        f"WMS returned a package outside platform {platform}"
                    )
            outbound = _evaluate_platform_outbound(
                platform_order_no=platform,
                system_order_nos=systems,
                instruction_system_order_nos=instruction_systems[platform],
                rows_by_system=by_system,
            )
            report["waiting_outbound_package_count"] += int(
                outbound.waiting_package_count
            )
            report["unknown_outbound_status_count"] += int(
                outbound.unknown_status_count
            )
            report["conflicting_wms_status_count"] += int(
                outbound.conflicting_status_count
            )
            report["wms_terminal_row_excluded_count"] += int(
                outbound.terminal_row_count
            )
            if outbound.waiting_package_count or outbound.unknown_status_count:
                report["waiting_outbound_order_count"] += 1
            diagnostics = report["outbound_block_diagnostics"]
            if isinstance(diagnostics, list) and len(diagnostics) < 200:
                diagnostics.extend(outbound.diagnostics[: 200 - len(diagnostics)])
            for diagnostic in outbound.diagnostics:
                logger.warning(
                    "customer notification outbound blocked "
                    "platform_order_no=%s system_order_no=%s wms_outbound_order_no=%s "
                    "wms_status_code=%s wms_status_name=%s has_waybill_no=%s "
                    "outbound_state=%s reason=%s",
                    diagnostic["platform_order_no"],
                    diagnostic["system_order_no"],
                    diagnostic["wms_outbound_order_no"],
                    diagnostic["wms_status_code"],
                    diagnostic["wms_status_name"],
                    diagnostic["has_waybill_no"],
                    diagnostic["outbound_state"],
                    diagnostic["reason"],
                )
            if outbound.state != OUTBOUND_STATE_OUTBOUNDED:
                if (
                    outbound.state == OUTBOUND_STATE_WAITING
                    and not outbound.waiting_package_count
                ):
                    report["waiting_outbound_order_count"] += 1
                report["blocked_existing_notification_count"] += int(
                    store.record_outbound_eligibility(
                        platform,
                        outbound_state=outbound.state,
                        reason=outbound.reason,
                        expected_system_order_nos=outbound.expected_customer_systems,
                        observed_system_order_nos=outbound.observed_customer_systems,
                        snapshot_complete=False,
                        observed_at=outbound_observed_at,
                    )
                )
                record_retry(platform, outbound.reason)
                continue

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
            package_rows: list[tuple[Mapping[str, Any], PackageSnapshot]] = []
            package_keys: set[str] = set()
            for row in outbound.package_rows:
                package = package_from_wms_row(
                    row,
                    platform_order_no=platform,
                    instruction_system_order_nos=instruction_systems[platform],
                    warehouse_code_lookup=warehouse_code_lookup,
                    outbound_observed_at=outbound_observed_at,
                )
                if package.package_key in package_keys:
                    raise ValueError(
                        f"WMS returned duplicate package {package.package_key}"
                    )
                package_keys.add(package.package_key)
                package_rows.append((row, package))
            tracking_review_packages = [
                package
                for _row, package in package_rows
                if package.customer_visible
                and package.visibility_reason == "tracking_source_unresolved"
            ]
            if tracking_review_packages:
                report["tracking_selection_review_count"] += len(
                    tracking_review_packages
                )
                selection_diagnostics = report["tracking_selection_diagnostics"]
                if isinstance(selection_diagnostics, list):
                    for package in tracking_review_packages:
                        if len(selection_diagnostics) >= 200:
                            break
                        selection_diagnostics.append(
                            {
                                "platform_order_no": platform,
                                "system_order_no": package.system_order_no,
                                "wms_outbound_order_no": (
                                    package.wms_outbound_order_no
                                ),
                                "shipment_type": package.shipment_type,
                                "waybill_no": package.waybill_no,
                                "tracking_no": package.tracking_no,
                                "reason": package.visibility_reason,
                            }
                        )
                logger.warning(
                    "customer notification tracking source requires review "
                    "platform_order_no=%s affected_package_count=%s",
                    platform,
                    len(tracking_review_packages),
                )
            sendable_rows = [
                (row, package)
                for row, package in package_rows
                if package.customer_visible and package.complete
            ]
            packages = (
                [package for _row, package in package_rows]
                if tracking_review_packages
                else [package for _row, package in sendable_rows]
            )
            sendable_package_keys = {package.package_key for package in packages}
            sendable_systems = {
                package.system_order_no.strip()
                for package in packages
                if package.system_order_no.strip()
            }
            previously_outbounded_unconfirmed = [
                package
                for package in store.list_packages(platform)
                if package.customer_visible
                and package.complete
                and package.wms_status_code == 3
                and package.outbound_state.strip().upper()
                == OUTBOUND_STATE_OUTBOUNDED
                and package.package_key not in sendable_package_keys
            ]
            if previously_outbounded_unconfirmed:
                report["blocked_existing_notification_count"] += int(
                    store.record_outbound_eligibility(
                        platform,
                        outbound_state=OUTBOUND_STATE_UNKNOWN,
                        reason="previously_outbounded_package_unconfirmed",
                        expected_system_order_nos=outbound.expected_customer_systems,
                        observed_system_order_nos=tuple(sorted(sendable_systems)),
                        snapshot_complete=False,
                        observed_at=outbound_observed_at,
                    )
                )
                logger.warning(
                    "customer notification previously outbound package is no longer "
                    "confirmed platform_order_no=%s affected_package_count=%s",
                    platform,
                    len(previously_outbounded_unconfirmed),
                )
                record_retry(platform, "previously_outbounded_package_unconfirmed")
                continue
            if not packages:
                report["waiting_logistics_order_count"] += 1
                report["blocked_existing_notification_count"] += int(
                    store.record_outbound_eligibility(
                        platform,
                        outbound_state=OUTBOUND_STATE_WAITING,
                        reason="outbound_confirmed_logistics_incomplete",
                        expected_system_order_nos=outbound.expected_customer_systems,
                        observed_system_order_nos=outbound.observed_customer_systems,
                        snapshot_complete=False,
                        observed_at=outbound_observed_at,
                    )
                )
                record_retry(platform, "outbound confirmed but logistics incomplete")
                continue
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
            current_packages = store.list_packages(platform)
            partial_outbound = bool(
                outbound.waiting_package_count
                or outbound.unknown_status_count
                or package_missing
                or len(sendable_rows) != len(package_rows)
            )
            report["blocked_existing_notification_count"] += int(
                store.record_outbound_eligibility(
                    platform,
                    outbound_state=OUTBOUND_STATE_OUTBOUNDED,
                    reason=(
                        "tracking_number_source_requires_review"
                        if tracking_review_packages
                        else (
                            "partial_customer_visible_packages_outbounded"
                            if partial_outbound
                            else "all_customer_visible_packages_outbounded"
                        )
                    ),
                    expected_system_order_nos=outbound.expected_customer_systems,
                    observed_system_order_nos=tuple(sorted(sendable_systems)),
                    package_set_hash=store.package_set_hash(current_packages),
                    snapshot_complete=True,
                    observed_at=outbound_observed_at,
                )
            )
            if partial_outbound:
                report["waiting_logistics_order_count"] += 1
                if package_complete > 0:
                    report["partial_logistics_order_count"] += 1

            raw_wms_names = tuple(
                dict.fromkeys(
                    name
                    for name in (
                        normalize_recipient_name(
                            _lookup(
                                _mapping_tree(row, max_depth=2),
                                _WMS_RECIPIENT_NAME_ALIASES,
                            )
                        )
                        for row, _package in sendable_rows
                    )
                    if name
                )
            )
            report["recipient_name_policy_masked_count"] += sum(
                1
                for name in raw_wms_names
                if is_policy_masked_recipient_name(name)
            )
            wms_names = tuple(
                name
                for name in raw_wms_names
                if not is_policy_masked_recipient_name(name)
            )
            recipient_name = wms_names[0] if len(wms_names) == 1 else ""
            recipient_name_conflict_unresolved = False
            if len(wms_names) > 1:
                report["recipient_name_conflict_count"] += 1
                recipient_name = store.remembered_recipient_name_choice(
                    platform,
                    wms_names,
                )
                if recipient_name:
                    report["recipient_name_selection_reused_count"] += 1
                elif recipient_name_resolver is not None:
                    report["recipient_name_selection_prompt_count"] += 1
                    try:
                        requested_name = normalize_recipient_name(
                            await recipient_name_resolver(platform, wms_names)
                        )
                    except Exception:
                        requested_name = ""
                    if requested_name:
                        try:
                            recipient_name = store.remember_recipient_name_choice(
                                platform,
                                requested_name,
                                wms_names,
                            )
                        except ValueError:
                            recipient_name = ""
                    if recipient_name:
                        report["recipient_name_selection_count"] += 1
                if not recipient_name:
                    recipient_name_conflict_unresolved = True
                    report["recipient_name_selection_unresolved_count"] += 1

            wms_phones = tuple(
                dict.fromkeys(
                    normalized
                    for normalized in (
                        normalize_phone(raw_phone)
                        for row, _package in sendable_rows
                        for raw_phone in _lookup_values(
                            _mapping_tree(row, max_depth=2),
                            _WMS_RECIPIENT_PHONE_ALIASES,
                        )
                    )
                    if normalized
                )
            )
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
                independent_site = is_independent_site_order(platform)
                needs_api_email = not (
                    existing_contact is not None
                    and existing_contact.email_source
                    == CONTACT_SOURCE_CUSTOMIZATION_JSON
                    and bool(existing_contact.email)
                )
                needs_api_phone = not (
                    existing_contact is not None
                    and not independent_site
                    and existing_contact.phone_verification_state
                    == PHONE_VERIFICATION_MATCHED
                    and bool(existing_contact.verified_phone_e164)
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
            event_report = store.observe_package_events(
                platform,
                store.list_packages(platform),
                baseline_pending=bool(target.get("baseline_pending")),
                source_kind=str(target.get("source_kind") or "AUTO_ERP"),
            )
            report["package_event_count"] += int(
                event_report["inserted_event_count"]
            )
            report["baseline_suppressed_count"] += int(
                event_report["baseline_suppressed_count"]
            )
            report["corrected_package_event_count"] += int(
                event_report["corrected_event_count"]
            )
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
            if tracking_review_packages:
                record_retry(platform, "tracking number source requires review")
            elif partial_outbound:
                record_retry(platform, "waiting for remaining package outbound or logistics")
            else:
                record_success(platform)
        except Exception as exc:
            failed_platforms.add(platform)
            report["blocked_existing_notification_count"] += int(
                store.record_outbound_eligibility(
                    platform,
                    outbound_state=OUTBOUND_STATE_UNKNOWN,
                    reason="notification_sync_processing_failed",
                    expected_system_order_nos=systems,
                    snapshot_complete=False,
                )
            )
            record_retry(
                platform,
                str(exc).strip() or type(exc).__name__,
            )

    # Diagnostics intentionally contain order identifiers and WMS state only;
    # contacts, addresses, tracking numbers and local paths are never included.
    report["failed_order_count"] = len(failed_platforms)
    report["sync_error_count"] = len(failed_platforms)
    report["database_merge_duration_ms"] = round(
        (time.monotonic() - database_merge_started) * 1000
    )
    report["total_duration_ms"] = round(
        (time.monotonic() - total_started) * 1000
    )
    progress("客户通知物流同步完成。", 100)
    return report


__all__ = [
    "OUTBOUND_STATE_OUTBOUNDED",
    "OUTBOUND_STATE_TERMINAL",
    "OUTBOUND_STATE_UNKNOWN",
    "OUTBOUND_STATE_WAITING",
    "classify_wms_outbound_state",
    "diagnose_notification_outbound",
    "is_outbounded_wms_row",
    "is_terminal_wms_row",
    "package_from_wms_row",
    "sync_notification_drafts",
]
