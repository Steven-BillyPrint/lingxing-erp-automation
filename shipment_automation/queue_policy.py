"""Pure shipment queue display policy, independent of controllers and storage."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from .alibaba_logistics import tracking_number_matches_carrier
from .models import shipment_tracking_attention_notice


class ShipmentStatusRow(Protocol):
    """Only the scalar facts needed by the queue display policy."""

    actual_total: str
    carrier: str
    chargeable_weight_kg: str
    checkpoint: str
    customer_shipping_service: str
    erp_state: str
    erp_state_changed_at: str
    externally_completed_at: str
    first_seen_at: str
    identity_state: str
    identity_state_changed_at: str
    international_tracking_no: str
    last_scanned_at: str
    lease_owner: str
    lease_stage: str
    lease_until: str
    logistics_state: str
    logistics_state_changed_at: str
    manual_review_created_at: str
    manual_review_reason: str
    outbounded_at: str
    re_mark_state: str
    re_mark_updated_at: str
    scan_issue_code: str
    scan_issue_state: str
    scan_issue_state_changed_at: str
    tracking_validated: bool | None
    updated_at: str


SHIPMENT_STATUS_PRIORITY = {
    "重新标发": -10,
    "重新标发处理中": -9,
    "重新标发需人工复核": -8,
    "重新标发完成": -7,
    "扫描错误": -1,
    "可标发": 0,
    "可继续标发": 0,
    "等待标发": 1,
    "等待用户确认": 1,
    "标发处理中": 1,
    "标发失败可重试": 2,
    "物流逾期异常": 2,
    "待查询物流": 3,
    "查询失败待重试": 4,
    "等待物流就绪": 5,
    "物流信息需复核": 6,
    "标发需人工复核": 6,
    "已完成": 7,
    "已取消": 8,
    "标签已移除": 8,
    "本轮已取消": 8,
    "订单信息冲突": 9,
}

def timestamp_value(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()

def shipment_status_timestamp(row: ShipmentStatusRow) -> str:
    if row.manual_review_reason:
        return row.manual_review_created_at
    if row.scan_issue_code:
        return row.scan_issue_state_changed_at or row.updated_at or row.last_scanned_at
    if row.re_mark_state:
        return row.re_mark_updated_at or row.updated_at
    identity = row.identity_state.strip().upper()
    logistics = row.logistics_state.strip().upper()
    erp = row.erp_state.strip().upper()
    if erp == "DONE":
        return row.outbounded_at or row.externally_completed_at or row.erp_state_changed_at or row.updated_at
    if identity and identity != "ACTIVE":
        return row.identity_state_changed_at or row.updated_at
    if logistics != "READY":
        return row.logistics_state_changed_at or row.updated_at
    return row.erp_state_changed_at or row.updated_at

def _has_live_lease(row: ShipmentStatusRow, *, now: datetime | None = None) -> bool:
    return has_live_lease(row.lease_owner, row.lease_stage, row.lease_until, now=now)


def has_live_lease(owner: str, stage: str, until: str, *, now: datetime | None = None) -> bool:
    if not (owner or stage or until):
        return False
    text = until.strip()
    if not text:
        return True
    try:
        expires_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

def shipment_business_status(row: ShipmentStatusRow, *, now: datetime | None = None) -> str:
    if row.manual_review_reason and (
        row.erp_state.strip().upper() != "DONE"
        or row.re_mark_state.strip().upper() not in {"", "COMPLETED", "CANCELLED"}
    ):
        return "标发需人工复核"
    if row.scan_issue_code:
        return {
            "MANUAL_REVIEW": "标发需人工复核",
            "MANUALLY_COMPLETED": "已完成",
            "MANUALLY_CANCELLED": "已取消",
        }.get(row.scan_issue_state.strip().upper(), "扫描错误")
    re_mark_state = row.re_mark_state.strip().upper()
    if re_mark_state == "DETECTED":
        return "重新标发"
    if re_mark_state == "MANUAL_REVIEW":
        return "重新标发需人工复核"
    if re_mark_state == "COMPLETED":
        return "重新标发完成"
    if re_mark_state and re_mark_state != "CANCELLED":
        # Active task overlays turn this into 重新标发处理中.  Without an
        # active task the persisted cycle is a recoverable, user-actionable
        # item; the state machine performs readback before any possible replay.
        return "重新标发"
    identity = row.identity_state.strip().upper()
    logistics = row.logistics_state.strip().upper()
    erp = row.erp_state.strip().upper()
    checkpoint = row.checkpoint.strip().upper()
    if identity == "CANCELLED":
        return "本轮已取消"
    if identity == "MANUALLY_CANCELLED":
        return "已取消"
    if identity == "PAUSED_TAG_REMOVED":
        return "标签已移除"
    if identity and identity != "ACTIVE":
        return "订单信息冲突"
    if erp == "DONE":
        return "已完成"
    if logistics == "CANCELLED":
        return "已取消"
    if shipment_tracking_attention_notice(
        customer_shipping_service=row.customer_shipping_service,
        first_seen_at=row.first_seen_at,
        carrier=row.carrier,
        international_tracking_no=row.international_tracking_no,
        logistics_state=logistics,
        identity_state=identity,
        erp_state=erp,
        tracking_validated=row.tracking_validated,
        now=now,
    ):
        return "物流逾期异常"
    if logistics in {"", "PENDING"}:
        return "待查询物流"
    if logistics == "WAITING":
        return "等待物流就绪"
    if logistics == "RETRYABLE":
        return "查询失败待重试"
    if logistics != "READY":
        return "物流信息需复核"
    if not all((row.carrier.strip(), row.international_tracking_no.strip(), row.actual_total.strip(), row.chargeable_weight_kg.strip())):
        return "物流信息需复核"
    if erp == "BLOCKED":
        return "标发需人工复核"
    if _has_live_lease(row, now=now):
        return "标发处理中"
    if erp == "RUNNING" or checkpoint not in {"", "NONE"}:
        return "可继续标发"
    if erp == "RETRYABLE":
        return "标发失败可重试"
    return "可标发"

def product_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split("|") if part.strip())

def shipment_status_bucket(value: str) -> int:
    if value == "重新标发":
        return -1
    if value in {"重新标发处理中", "标发处理中"}:
        return 0
    if value in {"等待标发", "等待用户确认"}:
        return 1
    if value in {"可标发", "可继续标发"}:
        return 2
    if value in {"已完成", "重新标发完成"}:
        return 4
    if value in {"已取消", "标签已移除", "本轮已取消"}:
        return 5
    return 3


def tracking_validated(carrier: object, tracking_no: object, logistics_state: object) -> bool:
    return bool(
        str(carrier or "").strip() and str(tracking_no or "").strip()
        and (str(logistics_state or "").strip().upper() == "READY"
             or tracking_number_matches_carrier(carrier, tracking_no))
    )


def actual_total(currency: object, fee_amount: object) -> str | None:
    return f"{currency} {fee_amount}".strip() if fee_amount else None
