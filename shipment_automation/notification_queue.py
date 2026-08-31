from __future__ import annotations

from collections.abc import Iterable, Mapping


PRODUCT_BLOCK_REASONS = frozenset(
    {
        "product_data_invalid",
        "product_items_missing",
        "product_main_image_missing",
        "product_title_missing",
        "product_sku_missing",
        "instruction_mixed_with_physical",
    }
)
STATUS_CHECK_FAILURE_PREFIXES = ("状态核验超时：", "状态查询失败：")

STATUS_AWAITING_REVIEW_SUPPLEMENTAL = "AWAITING_REVIEW_SUPPLEMENTAL"
STATUS_BLOCKED_LOGISTICS_REVIEW = "BLOCKED_LOGISTICS_REVIEW"
STATUS_BLOCKED_NAME_CONFLICT = "BLOCKED_NAME_CONFLICT"
STATUS_BLOCKED_PRODUCT = "BLOCKED_PRODUCT"
STATUS_DELIVERED_PARTIAL = "DELIVERED_PARTIAL"
STATUS_FAILED_STATUS_CHECK = "FAILED_STATUS_CHECK"
STATUS_FAILED_UNSENT = "FAILED_UNSENT"
STATUS_SUPPRESSED_DISABLED = "SUPPRESSED_DISABLED"
STATUS_UNKNOWN = "UNKNOWN"


NOTIFICATION_QUEUE_STATUS_ORDER = (
    "SENDING",
    "QUEUED",
    STATUS_AWAITING_REVIEW_SUPPLEMENTAL,
    "AWAITING_REVIEW",
    "RETRYABLE",
    "MANUAL_EMAIL_REQUIRED",
    STATUS_BLOCKED_NAME_CONFLICT,
    "WAITING_CONTACT",
    STATUS_BLOCKED_PRODUCT,
    STATUS_BLOCKED_LOGISTICS_REVIEW,
    "BLOCKED",
    STATUS_FAILED_UNSENT,
    STATUS_FAILED_STATUS_CHECK,
    STATUS_DELIVERED_PARTIAL,
    "ACCEPTED",
    "DELIVERY_UNCONFIRMED",
    "DELIVERED",
    "MANUALLY_COMPLETED",
    "SUPPRESSED",
    STATUS_SUPPRESSED_DISABLED,
    "REJECTED",
    "CANCELLED",
    "DRAFT",
    "APPROVED",
    STATUS_UNKNOWN,
)

_STATUS_PRIORITY = {
    "SENDING": 0,
    "QUEUED": 1,
    STATUS_AWAITING_REVIEW_SUPPLEMENTAL: 2,
    "AWAITING_REVIEW": 3,
    "RETRYABLE": 4,
    "MANUAL_EMAIL_REQUIRED": 5,
    STATUS_BLOCKED_NAME_CONFLICT: 6,
    "WAITING_CONTACT": 7,
    STATUS_BLOCKED_PRODUCT: 8,
    STATUS_BLOCKED_LOGISTICS_REVIEW: 8,
    "BLOCKED": 8,
    STATUS_FAILED_UNSENT: 9,
    STATUS_FAILED_STATUS_CHECK: 9,
    STATUS_DELIVERED_PARTIAL: 10,
    "ACCEPTED": 11,
    "DELIVERY_UNCONFIRMED": 12,
    "DELIVERED": 13,
    "MANUALLY_COMPLETED": 14,
    "SUPPRESSED": 15,
    STATUS_SUPPRESSED_DISABLED: 15,
    "REJECTED": 16,
    "CANCELLED": 17,
    "DRAFT": 18,
    "APPROVED": 18,
    STATUS_UNKNOWN: 18,
}

_STATUS_LABELS = {
    "DRAFT": "草稿",
    "AWAITING_REVIEW": "待审核",
    STATUS_AWAITING_REVIEW_SUPPLEMENTAL: "待审核补发",
    "QUEUED": "等待发送",
    "APPROVED": "已审核",
    "REJECTED": "已驳回",
    "SENDING": "发送中",
    "ACCEPTED": "发送服务已接收",
    "DELIVERED": "已完成",
    STATUS_DELIVERED_PARTIAL: "已发送，待补物流",
    "DELIVERY_UNCONFIRMED": "24小时未确认送达",
    "MANUALLY_COMPLETED": "人工完成",
    "SUPPRESSED": "已发送（自动去重）",
    STATUS_SUPPRESSED_DISABLED: "独立站通知已禁用",
    "WAITING_CONTACT": "待补联系方式",
    "MANUAL_EMAIL_REQUIRED": "需人工发送邮件",
    "RETRYABLE": "发送未成功，可重试",
    STATUS_BLOCKED_NAME_CONFLICT: "姓名冲突待选择",
    STATUS_BLOCKED_PRODUCT: "待补商品信息",
    STATUS_BLOCKED_LOGISTICS_REVIEW: "物流信息需复核",
    "BLOCKED": "暂不可发送",
    STATUS_FAILED_UNSENT: "发送未成功，需人工处理",
    STATUS_FAILED_STATUS_CHECK: "发送结果待核验",
    "CANCELLED": "已取消",
    STATUS_UNKNOWN: "未知状态",
}


def notification_has_product_block(last_error: object = "") -> bool:
    reasons = {
        value.strip()
        for value in str(last_error or "").split(",")
        if value.strip()
    }
    return bool(reasons & PRODUCT_BLOCK_REASONS)


def notification_has_missing_packages(
    state: object,
    package_missing: object = 0,
) -> bool:
    try:
        missing = int(package_missing or 0)
    except (TypeError, ValueError):
        missing = 0
    return str(state or "") == "DELIVERED" and missing > 0


def notification_status_check_failed(last_error: object = "") -> bool:
    return str(last_error or "").startswith(STATUS_CHECK_FAILURE_PREFIXES)


def notification_queue_status_key(
    state: object,
    package_missing: object = 0,
    is_supplemental_revision: object = False,
    last_error: object = "",
) -> str:
    raw = str(state or "").strip()
    error = str(last_error or "")
    if notification_has_missing_packages(raw, package_missing):
        return STATUS_DELIVERED_PARTIAL
    if raw == "AWAITING_REVIEW" and bool(is_supplemental_revision):
        return STATUS_AWAITING_REVIEW_SUPPLEMENTAL
    if raw == "FAILED":
        return (
            STATUS_FAILED_STATUS_CHECK
            if notification_status_check_failed(error)
            else STATUS_FAILED_UNSENT
        )
    if raw == "BLOCKED" and error == "recipient_name_conflict_unresolved":
        return STATUS_BLOCKED_NAME_CONFLICT
    if raw == "BLOCKED" and notification_has_product_block(error):
        return STATUS_BLOCKED_PRODUCT
    if raw == "BLOCKED" and "tracking_number_source_requires_review" in error:
        return STATUS_BLOCKED_LOGISTICS_REVIEW
    if raw == "SUPPRESSED" and error == (
        "independent_site_customer_notification_disabled"
    ):
        return STATUS_SUPPRESSED_DISABLED
    return raw if raw in _STATUS_PRIORITY else STATUS_UNKNOWN


def notification_queue_status_label(status_key: object) -> str:
    key = str(status_key or "")
    return _STATUS_LABELS.get(key, key or _STATUS_LABELS[STATUS_UNKNOWN])


def notification_state_label(
    state: object,
    package_missing: object = 0,
    is_supplemental_revision: object = False,
    last_error: object = "",
) -> str:
    return notification_queue_status_label(
        notification_queue_status_key(
            state,
            package_missing,
            is_supplemental_revision,
            last_error,
        )
    )


def notification_queue_priority(
    state: object,
    package_missing: object = 0,
    is_supplemental_revision: object = False,
    last_error: object = "",
    *,
    active: bool = False,
) -> int:
    raw = str(state or "").strip()
    if active and raw in {"AWAITING_REVIEW", "RETRYABLE"}:
        return _STATUS_PRIORITY["QUEUED"]
    return _STATUS_PRIORITY[
        notification_queue_status_key(
            raw,
            package_missing,
            is_supplemental_revision,
            last_error,
        )
    ]


def notification_mapping_priority(
    notification: Mapping[str, object],
    *,
    active: bool = False,
) -> int:
    return notification_queue_priority(
        notification.get("state"),
        notification.get("package_missing"),
        notification.get("is_supplemental_revision"),
        notification.get("last_error"),
        active=active,
    )


def ordered_notification_status_keys(values: Iterable[object]) -> tuple[str, ...]:
    normalized = {
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    }
    return tuple(
        key for key in NOTIFICATION_QUEUE_STATUS_ORDER if key in normalized
    )


__all__ = [
    "NOTIFICATION_QUEUE_STATUS_ORDER",
    "PRODUCT_BLOCK_REASONS",
    "STATUS_CHECK_FAILURE_PREFIXES",
    "notification_has_missing_packages",
    "notification_has_product_block",
    "notification_mapping_priority",
    "notification_queue_priority",
    "notification_queue_status_key",
    "notification_queue_status_label",
    "notification_state_label",
    "notification_status_check_failed",
    "ordered_notification_status_keys",
]
