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
from typing import Iterable

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
