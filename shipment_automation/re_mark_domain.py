"""Pure business rules for completed-shipment tracking refreshes.

This module has no SQLite, browser, Qt, or OpenAPI dependency.  Adapters feed
it normalized facts and persist/execute the resulting decision separately.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from .alibaba_logistics import (
    normalize_carrier_name,
    normalize_tracking_number,
    tracking_number_matches_carrier,
    tracking_number_mismatch_reason,
)
from .models import LogisticsDetail


@dataclass(frozen=True)
class ReMarkEligibility:
    eligible: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletedRefreshSnapshot:
    carrier: str
    waybill_no: str
    tracking_no: str
    freight: str
    currency: str
    fee_weight_g: str
    alibaba_status: str
    service_line: str
    snapshot_hash: str

    @property
    def validation_error(self) -> str:
        if not all(
            (
                self.carrier,
                self.waybill_no,
                self.tracking_no,
                self.freight,
                self.currency,
                self.fee_weight_g,
            )
        ):
            return "阿里物流新快照缺少承运商、运单号、ALS、运费、币种或计费重量。"
        if not tracking_number_matches_carrier(self.carrier, self.waybill_no):
            return tracking_number_mismatch_reason(self.carrier, self.waybill_no)
        return ""


def completed_re_mark_eligibility(
    *,
    sales_platform_code: object = "",
    sales_platform_name: object = "",
    platform_order_item_ids: object = (),
    logistics_provider_name: object = "",
    logistics_type_name: object = "",
) -> ReMarkEligibility:
    """Apply exactly the three package-ownership gates requested by business."""

    platform_values = {
        str(sales_platform_code or "").strip().casefold(),
        str(sales_platform_name or "").strip().casefold(),
    }
    amazon = any(
        value == "amazon" or value.startswith("amazon-")
        for value in platform_values
    )
    if isinstance(platform_order_item_ids, str):
        try:
            parsed = json.loads(platform_order_item_ids)
        except (TypeError, json.JSONDecodeError):
            parsed = [platform_order_item_ids]
        else:
            if not isinstance(parsed, (list, tuple, set, frozenset)):
                parsed = [platform_order_item_ids]
    else:
        parsed = platform_order_item_ids
    if not isinstance(parsed, (list, tuple, set, frozenset)):
        parsed = ()
    item_ids = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in parsed
            if str(value or "").strip()
        )
    )
    provider = str(logistics_provider_name or "").strip().casefold()
    route = str(logistics_type_name or "").strip()
    manual = provider in {"手动", "manual"} and bool(route)
    reasons: list[str] = []
    if not amazon:
        reasons.append("非 Amazon 平台订单")
    if not item_ids:
        reasons.append("缺少原始平台商品行 OrderItemId")
    if not manual:
        reasons.append("物流方式不是手动-xxxx")
    return ReMarkEligibility(not reasons, tuple(reasons))


def _split_money(value: object) -> tuple[str, str]:
    text = str(value or "").strip().replace(",", "")
    currency_match = re.search(r"\b([A-Z]{3})\b", text.upper())
    amount_match = re.search(r"-?\d+(?:\.\d+)?", text)
    return (
        currency_match.group(1) if currency_match else "",
        amount_match.group(0) if amount_match else "",
    )


def completed_refresh_snapshot(detail: LogisticsDetail) -> CompletedRefreshSnapshot:
    currency, freight = _split_money(detail.actual_total)
    try:
        fee_weight_g = format(
            Decimal(str(detail.chargeable_weight_kg or "").strip())
            * Decimal("1000"),
            "f",
        )
    except InvalidOperation:
        fee_weight_g = ""
    values = {
        "carrier": normalize_carrier_name(detail.carrier),
        "waybill_no": normalize_tracking_number(detail.international_tracking_no),
        "tracking_no": str(detail.logistics_no or "").strip(),
        "freight": freight,
        "currency": currency,
        "fee_weight_g": fee_weight_g,
        "alibaba_status": str(detail.status_text or "").strip(),
        "service_line": str(detail.service_line or "").strip(),
    }
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return CompletedRefreshSnapshot(
        **values,
        snapshot_hash=hashlib.sha256(encoded).hexdigest(),
    )


def normalize_order_item_ids(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        )
    )


def _wms_status(row: Mapping[str, object]) -> int | None:
    value = row.get("status")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _wms_platform_order_numbers(value: object) -> set[str]:
    if isinstance(value, str):
        return {
            part.strip()
            for part in value.replace("；", ",").split(",")
            if part.strip()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {
            str(part or "").strip()
            for part in value
            if str(part or "").strip()
        }
    text = str(value or "").strip()
    return {text} if text else set()


def current_lingxing_waybill_from_wms_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    system_order_no: str,
    platform_order_no: str,
    logistics_no: str,
) -> str:
    """Return the only active, outbounded WMS waybill for one ALS package.

    The WMS ``waybill_no`` is Lingxing's authoritative current international
    waybill.  Cut-off rows are historical evidence and are ignored.  Any
    incomplete or ambiguous active result fails closed so callers can defer
    the Alibaba comparison instead of creating a false re-mark cycle.
    """

    expected_system = str(system_order_no or "").strip()
    expected_platform = str(platform_order_no or "").strip()
    expected_logistics = normalize_tracking_number(logistics_no)
    if not all((expected_system, expected_platform, expected_logistics)):
        raise ValueError("系统单号、平台单号和 ALS 物流单号必须完整。")

    matching_rows: list[tuple[int, Mapping[str, object]]] = []
    found_system_order = False
    for row in rows:
        if str(row.get("order_number") or "").strip() != expected_system:
            continue
        found_system_order = True
        platform_numbers = _wms_platform_order_numbers(row.get("platform_order_no"))
        if platform_numbers and expected_platform not in platform_numbers:
            raise ValueError("领星 WMS 返回的系统单号与平台单号不一致。")
        if normalize_tracking_number(row.get("tracking_no")) != expected_logistics:
            continue
        status = _wms_status(row)
        if status == 4:
            continue
        if status not in {1, 2, 3}:
            raise ValueError("领星 WMS 返回了无法识别的销售出库单状态。")
        matching_rows.append((status, row))

    if not found_system_order:
        raise ValueError("领星 WMS 未返回该系统单号。")
    if len(matching_rows) != 1:
        raise ValueError("领星 WMS 未返回唯一且与 ALS 一致的有效销售出库单。")
    status, row = matching_rows[0]
    if status != 3:
        raise ValueError("领星 WMS 当前销售出库单尚未出库。")
    waybill_no = normalize_tracking_number(row.get("waybill_no"))
    if not waybill_no:
        raise ValueError("领星 WMS 当前已出库销售单缺少运单号。")
    return waybill_no
