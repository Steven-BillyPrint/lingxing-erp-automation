from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from erp_automation.coordination.remote_controller import (
    CoordinationClientUpdateRequired,
)
from erp_automation.client_version import CLIENT_VERSION
from erp_automation.operations.scan_audit import scan_audit_directory_name
from erp_automation.runtime_mode import (
    is_local_test_mode,
    is_local_test_shared_server_mode,
    local_test_formal_baseline_version,
)
from lingxing_automation.products.catalog import preferred_product_type
from shipment_automation.models import shipment_tracking_attention_notice

from .controller import BackgroundTaskController, ControlResult
from .models import (
    Capability,
    CapabilityMode,
    CustomOrderRow,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopSnapshot,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LogEntry,
    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    SERVER_CONFIGURED_SECRET,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
    notification_confirmation_order_no,
)
from .qt_compat import PYSIDE6_AVAILABLE, require_pyside6


_COMPLETE_ALL_STATE = "__ALL_COMPLETED__"
_CANCEL_WORKFLOW_STATE = "__CANCEL_WORKFLOW__"
_CUSTOM_AUTO_SCAN_INTERVAL_MS = 5 * 60 * 1000
_SHIPMENT_AUTO_SCAN_INTERVAL_MS = 3 * 60 * 60 * 1000
_NOTIFICATION_RECEIPT_UI_REFRESH_INTERVAL_MS = 15 * 1000
_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY = "local_visible_logistics_followup"
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_PARTIAL_DELIVERY_COLOR = "#F58718"
_PRODUCT_BLOCK_REASONS = {
    "product_data_invalid",
    "product_items_missing",
    "product_main_image_missing",
    "product_title_missing",
    "product_sku_missing",
    "instruction_mixed_with_physical",
}


def _scheduled_scan_delay_ms(
    snapshot: DesktopSnapshot,
    *,
    trigger: str,
    default_interval_ms: int,
    now: datetime | None = None,
) -> int | None:
    """Translate the server-owned due time into one local single-shot delay."""

    if (
        not snapshot.is_scheduler_leader
        or snapshot.policy.execution_paused
    ):
        return None
    due_at = float(snapshot.scheduled_scan_due_at.get(trigger) or 0)
    if due_at <= 0:
        return default_interval_ms
    current = now or datetime.now(timezone.utc)
    delay = int((due_at - current.timestamp()) * 1000)
    return max(250, min(default_interval_ms, delay))


def _notification_has_product_block(last_error: object = "") -> bool:
    reasons = {
        value.strip()
        for value in str(last_error or "").split(",")
        if value.strip()
    }
    return bool(reasons & _PRODUCT_BLOCK_REASONS)


def _notification_has_missing_packages(state: object, package_missing: object = 0) -> bool:
    try:
        missing = int(package_missing or 0)
    except (TypeError, ValueError):
        missing = 0
    return str(state or "") == "DELIVERED" and missing > 0


def _notification_state_label(
    state: object,
    package_missing: object = 0,
    is_supplemental_revision: object = False,
    last_error: object = "",
) -> str:
    raw = str(state or "")
    if _notification_has_missing_packages(raw, package_missing):
        return "已发送，待补物流"
    if raw == "AWAITING_REVIEW" and bool(is_supplemental_revision):
        return "待审核补发"
    if raw == "FAILED" and str(last_error or "").startswith(
        ("状态核验超时：", "状态查询失败：")
    ):
        return "状态核验失败"
    if raw == "BLOCKED" and _notification_has_product_block(last_error):
        return "待补商品信息"
    if raw == "BLOCKED" and str(last_error or "") == "recipient_name_conflict_unresolved":
        return "姓名冲突待选择"
    if raw == "SUPPRESSED" and str(last_error or "") == (
        "independent_site_customer_notification_disabled"
    ):
        return "独立站通知已禁用"
    return {
        "DRAFT": "草稿",
        "AWAITING_REVIEW": "待审核",
        "QUEUED": "等待发送",
        "APPROVED": "已审核",
        "REJECTED": "已驳回",
        "SENDING": "发送中",
        "ACCEPTED": "发送服务已接收",
        "DELIVERED": "已完成",
        "DELIVERY_UNCONFIRMED": "24小时未确认送达",
        "MANUALLY_COMPLETED": "人工完成",
        "SUPPRESSED": "已发送（自动去重）",
        "WAITING_CONTACT": "待补联系方式",
        "MANUAL_EMAIL_REQUIRED": "需人工发送邮件",
        "RETRYABLE": "发送失败可重试",
        "BLOCKED": "暂不可发送",
        "FAILED": "发送失败",
        "CANCELLED": "已取消",
    }.get(raw, raw)


def _notification_status_explanation(notification: Mapping[str, object]) -> str:
    error = str(notification.get("last_error") or "").strip()
    if error:
        if error.startswith("outbound_ineligible:"):
            _prefix, _state, reason = (error.split(":", 2) + [""])[:3]
            return {
                "waiting_for_all_customer_visible_packages_outbound": (
                    "尚无客户可见包裹被领星 WMS 明确确认为已出库，"
                    "已保留扫描任务并等待下次同步。"
                ),
                "outbound_confirmed_logistics_incomplete": (
                    "已确认出库，但承运商或最终跟踪号尚不完整，"
                    "暂不可发送。"
                ),
                "unknown_wms_outbound_status": (
                    "领星 WMS 出库状态缺失、无法解析或与状态文字冲突，"
                    "已保守阻止发送。"
                ),
                "conflicting_wms_status": (
                    "同一包裹存在相互冲突的 WMS 出库状态，"
                    "需要人工核对后再同步。"
                ),
                "terminal_wms_outbound_status": (
                    "订单或包裹已取消、截单或关闭，不可发送客户通知。"
                ),
                "previously_outbounded_package_unconfirmed": (
                    "之前已进入通知的包裹本次未能再次确认为已出库，"
                    "原审核已失效，请重新同步并复核。"
                ),
                "tracking_number_source_requires_review": (
                    "面单来源与运单号/跟踪号列无法可靠对应，"
                    "已阻止整单发送；请核对包裹明细后重新同步。"
                ),
            }.get(reason, "WMS 出库资格未能确认，暂不可发送。")
        if error == "superseded":
            return "通知内容已变化，当前版本已失效。"
        if error == "recipient_name_conflict_unresolved":
            return "WMS 返回多个收件人姓名，用户尚未选定；已阻止发送并加入自动重试告警。"
        if error == "recipient_contact_unavailable":
            return (
                "收件人没有可用的邮箱或电话；"
                "请补充至少一种有效联系方式，系统会在下次同步后重新生成待审核通知。"
            )
        if error == "amazon_virtual_email_phone_missing":
            return (
                "检测到 Amazon 虚拟邮箱，且没有可用的真实电话；"
                "系统不会自动发送，请补充真实电话后改用短信。"
            )
        if error == "manual_email_required_virtual_contact":
            return (
                "检测到 Amazon 虚拟邮箱，且没有可用的真实电话；"
                "系统不会自动发送，请人工通知客户，完成后标记“人工完成”。"
            )
        if error == "independent_site_customer_notification_disabled":
            return "独立站订单已禁用客户通知；系统不会发送或重试。"
        if "tracking_number_source_requires_review" in error.split(","):
            return (
                "面单来源与运单号/跟踪号列无法可靠对应，"
                "已阻止整单发送；请核对包裹明细后重新同步。"
            )
        if _notification_has_product_block(error):
            labels = {
                "product_data_invalid": "领星商品数据无法可靠解析",
                "product_items_missing": "部分系统单缺少商品明细",
                "product_main_image_missing": "未找到带主图的商品",
                "product_title_missing": "带主图商品缺少商品标题",
                "product_sku_missing": "未找到可用的商品 SKU",
                "instruction_mixed_with_physical": "同一系统单混有 Instruction 和实物 SKU",
            }
            reasons = [
                labels[value]
                for value in str(error).split(",")
                if value in labels
            ]
            return "；".join(reasons) + "，暂不可审核发送。"
        return error
    state = str(notification.get("state") or "")
    provider_status = str(notification.get("provider_status") or "").strip()
    if state == "DELIVERED":
        return f"供应商确认送达：{provider_status}" if provider_status else "供应商已确认送达。"
    if state == "SUPPRESSED":
        return "检测到该订单已有发送成功记录，系统已阻止重复进入待发队列。"
    if state == "ACCEPTED":
        return (
            f"发送服务已接收，等待确认送达：{provider_status}"
            if provider_status
            else "发送服务已接收，等待确认送达。"
        )
    if state == "DELIVERY_UNCONFIRMED":
        return "发送服务已接收，但 24 小时内未确认送达；这不代表发送失败，系统不会自动重发。"
    if state == "CANCELLED":
        return "已由用户人工取消；未发送，后续扫描不会自动重建。"
    if provider_status:
        return f"供应商状态：{provider_status}"
    return ""


def _notification_status_color(state: object, package_missing: object = 0) -> str:
    raw = str(state or "")
    if _notification_has_missing_packages(raw, package_missing):
        return _PARTIAL_DELIVERY_COLOR
    return {
        "DELIVERED": "#027A48",
        "DELIVERY_UNCONFIRMED": "#B54708",
        "MANUALLY_COMPLETED": "#027A48",
        "SUPPRESSED": "#027A48",
        "RETRYABLE": "#B54708",
        "FAILED": "#B42318",
        "BLOCKED": "#B42318",
        "MANUAL_EMAIL_REQUIRED": "#B54708",
        "CANCELLED": "#667085",
        "QUEUED": "#175CD3",
    }.get(raw, "#344054")


def _tracking_source_label(shipment_type: object) -> str:
    return {
        "MANUAL": "人工填写 · 运单号",
        "SYSTEM_LABEL": "系统面单 · 跟踪号",
        "OVERSEAS_AUTO": "系统面单 · 跟踪号",
        "MATCHED_COLUMNS": "两列一致",
        "UNKNOWN": "来源待复核",
    }.get(str(shipment_type or "").strip().upper(), "来源待复核")


def _notification_status_is_bold(state: object, package_missing: object = 0) -> bool:
    raw = str(state or "")
    return raw in {"DELIVERED", "MANUALLY_COMPLETED", "SUPPRESSED", "QUEUED"} or (
        _notification_has_missing_packages(raw, package_missing)
    )


def _notification_queue_sort_key(
    notification: Mapping[str, object],
    *,
    active: bool = False,
) -> tuple[int, float, int]:
    state = str(notification.get("state") or "")
    missing = notification.get("package_missing")
    if state == "SENDING":
        priority = 0
    elif state == "QUEUED" or (
        active and state in {"AWAITING_REVIEW", "RETRYABLE"}
    ):
        priority = 1
    elif state == "AWAITING_REVIEW":
        priority = 2
    elif _notification_has_missing_packages(state, missing):
        priority = 3
    elif state in {"DELIVERED", "MANUALLY_COMPLETED", "SUPPRESSED"}:
        priority = 4
    elif state == "CANCELLED":
        priority = 5
    else:
        priority = 3
    raw_timestamp = str(
        notification.get("state_changed_at")
        or notification.get("erp_completed_at")
        or notification.get("updated_at")
        or ""
    ).strip()
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_value = timestamp.astimezone(timezone.utc).timestamp()
    except ValueError:
        timestamp_value = 0.0
    try:
        notification_id = int(notification.get("id") or 0)
    except (TypeError, ValueError):
        notification_id = 0
    return priority, -timestamp_value, -notification_id


def _notification_has_full_details(
    notification: Mapping[str, object],
) -> bool:
    """Distinguish send-ready details from lightweight package previews."""

    return bool(
        notification.get("_detail_loaded")
        or notification.get("detail_loaded")
        or ("body" in notification and "reviews" in notification)
        # Keep compatibility with local/legacy controllers that returned full
        # package items directly before the paginated preview contract existed.
        or ("items" in notification and "preview_items" not in notification)
    )


def _scan_countdown_text(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
_CUSTOM_WORKFLOW_STATUS_ORDER = (
    "processing",
    "waiting",
    "pending",
    "folder_pending",
    "sku_adjustment_pending",
    "package_split_pending",
    "instruction_remark_pending",
    "warehouse_logistics_pending",
    "product_identity_tag_conflict",
    "product_identity_unrecognized",
    "product_identity_review",
    "product_identity_pending",
    "blocked",
    "not_required",
    "completed",
    "cancelled",
    "已忽略",
)
_CUSTOM_WORKFLOW_STATUS_LABELS = {
    "processing": "正在处理",
    "waiting": "等待处理",
    "product_identity_pending": "等待 ASIN 同步",
    "product_identity_tag_conflict": "ASIN/标签冲突，待复核",
    "product_identity_unrecognized": "ASIN 未匹配定制产品",
    "product_identity_review": "商品信息待人工复核",
    "pending": "联系方式待处理",
    "folder_pending": "订单文件夹待处理",
    "blocked": "已阻止",
    # Retain labels for older snapshots while startup repair/next mutation
    # normalizes them to the current pending-stage statuses.
    "contact_writeback_complete": "联系方式已完成",
    "folder_complete": "订单文件夹已完成",
    "sku_adjustment_pending": "SKU 调整待处理",
    "package_split_pending": "拆包待处理",
    "instruction_remark_pending": "说明书备注待处理",
    "warehouse_logistics_pending": "仓库物流待处理",
    "not_required": "不需要（买家申请取消）",
    "completed": "已完成",
    "cancelled": "已取消",
    "已忽略": "已忽略",
}
_CUSTOM_WORKFLOW_STATUS_PRIORITY = {
    status: priority for priority, status in enumerate(_CUSTOM_WORKFLOW_STATUS_ORDER)
}
_CUSTOM_QUICK_SELECT_STATUSES = {
    "pending",
    "folder_pending",
    "sku_adjustment_pending",
    "package_split_pending",
    "instruction_remark_pending",
    "warehouse_logistics_pending",
}


def _custom_workflow_status_label(status: object) -> str:
    value = str(status or "")
    return _CUSTOM_WORKFLOW_STATUS_LABELS.get(value, value)


def _custom_order_quick_select_eligibility(
    row: CustomOrderRow,
    *,
    active_order_nos: set[str] | None = None,
) -> tuple[bool, str]:
    status = str(row.status_text or row.workflow_stage or "").strip()
    if row.platform_order_no in (active_order_nos or set()):
        return False, "已有等待或运行中的处理任务"
    if status not in _CUSTOM_QUICK_SELECT_STATUSES:
        return False, _custom_workflow_status_label(status)
    if str(row.last_error or "").strip():
        return False, "存在未处理错误"
    if row.retry_confirmation_required:
        return False, "需要人工复核"
    return True, _custom_workflow_status_label(status)


_INTERACTION_STAGE_LABELS = {
    "contact": "联系方式",
    "contact_writeback": "联系方式修改审核",
    "contact_selection": "选择联系方式候选",
    "folder": "订单文件夹",
    "folder_creation": "创建订单文件夹",
    "sku": "SKU 调整",
    "sku_adjustment": "SKU 调整",
    "manual_sku_completion": "人工确认 SKU 调整完成",
    "package_split": "拆包",
    "manual_package_split_completion": "人工确认拆包完成",
    "instruction_remark": "说明书备注",
    "warehouse_logistics": "仓库物流",
    "buyer_cancelled": "买家申请取消",
    "erp_mark:waybill_review": "自动标发：审核运单填写信息",
    "shipment:alibaba_manual_login": "自动标发：人工登录阿里物流站",
    "notification:recipient_name_select": "客户通知：选择收件人姓名",
    "alibaba_order:quote_details": "阿里查价资料",
}
_INTERACTION_OPERATION_LABELS = {
    "phone_update": "电话写回",
    "contact_writeback": "联系方式写回",
    "package_split": "拆包",
    "sku_adjustment": "SKU 调整",
    "instruction_remark": "说明书备注",
    "warehouse_logistics": "仓库物流",
    "mark_shipped": "标记发货",
}


def _interaction_stage_label(stage: object) -> str:
    """Return a user-facing name while preserving unknown future stages."""

    value = str(stage or "").strip()
    if value in _INTERACTION_STAGE_LABELS:
        return _INTERACTION_STAGE_LABELS[value]
    for prefix, label in (
        ("retry_review:", "重试前人工复核"),
        ("browser_fallback:", "网页回退确认"),
        ("erp_mark:stage_review:", "自动标发"),
        ("erp_mark:", "自动标发"),
    ):
        if value.startswith(prefix):
            operation = value[len(prefix) :]
            operation_label = _INTERACTION_STAGE_LABELS.get(
                operation,
                _INTERACTION_OPERATION_LABELS.get(operation, operation),
            )
            return f"{label}：{operation_label}" if operation_label else label
    return value


def _product_type_label(product_type: object) -> str:
    return preferred_product_type(product_type) or "未识别"


def _shipment_product_type_label(row: ShipmentRow) -> str:
    product_type = preferred_product_type(row.product_type)
    if product_type:
        return product_type
    return "无ASIN"


def _product_type_values(source: object) -> tuple[str, ...]:
    """Return normalized product types from a row or notification mapping."""

    if isinstance(source, Mapping):
        raw_values = source.get("product_types")
        if isinstance(raw_values, Sequence) and not isinstance(
            raw_values,
            (str, bytes),
        ):
            values = raw_values
        else:
            values = (source.get("product_type"),)
    else:
        values = (getattr(source, "product_type", source),)
    normalized: list[str] = []
    for value in values:
        for part in str(value or "").replace("、", "|").split("|"):
            text = part.strip()
            if text and text not in normalized:
                normalized.append(text)
    return tuple(normalized) or ("",)


def _matches_product_type_filter(
    source: object,
    selected_product_types: set[str] | frozenset[str],
) -> bool:
    return not selected_product_types or bool(
        set(_product_type_values(source)) & set(selected_product_types)
    )


def _queue_row_matches_search(row: object, field: str, query: str) -> bool:
    normalized = str(query or "").strip().casefold()
    if not normalized:
        return True
    attribute = {
        "platform_order_no": "platform_order_no",
        "system_order_no": "system_order_no",
        "product_type": "product_type",
    }.get(str(field or ""), "platform_order_no")
    return normalized in str(getattr(row, attribute, "") or "").casefold()


_SHIPMENT_STATUS_LABELS = (
    "扫描错误",
    "物流逾期异常",
    "待查询物流",
    "等待物流就绪",
    "查询失败待重试",
    "可标发",
    "可继续标发",
    "等待标发",
    "等待用户确认",
    "标发处理中",
    "标发失败可重试",
    "物流信息需复核",
    "标发需人工复核",
    "已完成",
    "已取消",
    "本轮已取消",
    "标签已移除",
    "订单信息冲突",
)
_SHIPMENT_OVERDUE_HISTORY_LABEL = "逾期"
_SHIPMENT_NO_OVERDUE_HISTORY_LABEL = "未曾逾期"
_SHIPMENT_OVERDUE_HISTORY_COLOR = "#B54708"
_SHIPMENT_NO_OVERDUE_HISTORY_COLOR = "#047857"
_SHIPMENT_TABLE_DEFAULT_WIDTHS = (
    40,
    160,
    110,
    90,
    115,
    115,
    90,
    110,
    100,
    110,
    110,
    390,
    76,
)
_SHIPMENT_CHECKPOINT_LABELS = {
    "": "尚未开始",
    "NONE": "尚未开始",
    "CHANNEL_SET": "已设置物流渠道",
    "AUDITED": "已审核",
    "LOGISTICS_SAVED": "已保存物流单号",
    "OUTBOUNDED": "已出库完成",
}
_SHIPMENT_STATUS_PRIORITY = {
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


def _shipment_selection_key(row: ShipmentRow) -> str:
    """Return the independent management key used by the shipment checkbox."""

    if str(row.scan_issue_code or "").strip():
        persisted = str(row.scan_issue_key or "").strip()
        if persisted:
            return persisted
        return "scan-row:" + "|".join(
            (
                str(row.system_order_no or "").strip(),
                str(row.platform_order_no or "").strip(),
                str(row.scan_issue_code or "").strip(),
            )
        )
    return str(row.logistics_no or "").strip()


def _queue_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status_timestamp_value(value: object) -> float:
    parsed = _queue_timestamp(value)
    return parsed.timestamp() if parsed is not None else float("-inf")


def _format_status_timestamp(value: object) -> str:
    parsed = _queue_timestamp(value)
    if parsed is None:
        return "-"
    return parsed.astimezone(_CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _shipment_status_timestamp(row: ShipmentRow) -> str:
    if str(row.scan_issue_code or "").strip():
        return (
            row.scan_issue_state_changed_at
            or row.updated_at
            or row.last_scanned_at
        )
    identity = str(row.identity_state or "").strip().upper()
    logistics = str(row.logistics_state or "").strip().upper()
    erp = str(row.erp_state or "").strip().upper()
    if str(row.erp_state or "").strip().upper() == "DONE":
        if str(row.completion_source or "").strip().upper() == "MANUAL_DETECTED":
            return (
                row.externally_completed_at
                or row.outbounded_at
                or row.erp_state_changed_at
                or row.updated_at
            )
        return (
            row.outbounded_at
            or row.externally_completed_at
            or row.erp_state_changed_at
            or row.updated_at
        )
    if identity and identity != "ACTIVE":
        return row.identity_state_changed_at or row.updated_at
    if logistics != "READY" or not all(
        (
            str(row.carrier or "").strip(),
            str(row.international_tracking_no or "").strip(),
            str(row.actual_total or "").strip(),
            str(row.chargeable_weight_kg or "").strip(),
        )
    ):
        return row.logistics_state_changed_at or row.updated_at
    if erp in {"BLOCKED", "RUNNING", "RETRYABLE"} or str(
        row.checkpoint or ""
    ).strip().upper() not in {"", "NONE"}:
        return row.erp_state_changed_at or row.updated_at
    candidates = (
        row.logistics_state_changed_at,
        row.erp_state_changed_at,
    )
    parsed = [
        (timestamp, value)
        for value in candidates
        if (timestamp := _queue_timestamp(value)) is not None
    ]
    if parsed:
        return max(parsed, key=lambda item: item[0])[1]
    return row.updated_at


_LEGACY_EMAIL_ERROR_LABELS = {
    "Missing receiver email.": "邮件预览未生成：缺少收件邮箱（不影响 ERP 标发）。",
    "Conflicting receiver emails for the same platform order.": (
        "邮件预览未生成：同一平台订单存在多个收件邮箱（不影响 ERP 标发）。"
    ),
}


def _email_error_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    translated = _LEGACY_EMAIL_ERROR_LABELS.get(text)
    if translated:
        return translated
    if any(character.isascii() and character.isalpha() for character in text):
        return "邮件预览处理异常，请打开详细日志检查（不影响 ERP 标发）。"
    return text


def _shipment_error_messages(row: ShipmentRow) -> tuple[str, ...]:
    messages: list[str] = []
    for prefix, value in (
        ("ERP", row.erp_last_error),
        ("物流", row.logistics_last_error),
        ("邮件", _email_error_label(row.email_last_error)),
    ):
        text = str(value or "").strip()
        if text and text not in messages:
            messages.append(f"{prefix}：{text}")
    return tuple(messages)


def _shipment_has_live_lease(row: ShipmentRow, *, now: datetime | None = None) -> bool:
    if not (row.lease_owner or row.lease_stage or row.lease_until):
        return False
    expires_at = _queue_timestamp(row.lease_until)
    if expires_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    return expires_at > current.astimezone(timezone.utc)


def _shipment_retry_is_due(value: object, *, now: datetime | None = None) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    retry_at = _queue_timestamp(text)
    if retry_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return retry_at <= current.astimezone(timezone.utc)


def _shipment_checkpoint_label(value: object) -> str:
    raw = str(value or "").strip().upper()
    return _SHIPMENT_CHECKPOINT_LABELS.get(raw, raw or "尚未开始")


def _shipment_business_status(row: ShipmentRow, *, now: datetime | None = None) -> str:
    if str(row.scan_issue_code or "").strip():
        scan_state = str(row.scan_issue_state or "ACTIVE").strip().upper()
        if scan_state == "MANUAL_REVIEW":
            return "标发需人工复核"
        if scan_state == "MANUALLY_COMPLETED":
            return "已完成"
        if scan_state == "MANUALLY_CANCELLED":
            return "已取消"
        return "扫描错误"
    identity = str(row.identity_state or "").strip().upper()
    logistics = str(row.logistics_state or "").strip().upper()
    erp = str(row.erp_state or "").strip().upper()
    checkpoint = str(row.checkpoint or "").strip().upper()
    if identity == "CANCELLED":
        return "本轮已取消"
    if identity == "MANUALLY_CANCELLED":
        return "已取消"
    if identity == "CONFLICT":
        return "订单信息冲突"
    if identity == "PAUSED_TAG_REMOVED":
        return "标签已移除"
    if identity and identity != "ACTIVE":
        return "订单信息冲突"
    # ERP completion is terminal. Historical logistics retry/error fields are
    # retained for audit, but must not make an outbounded order actionable.
    if erp == "DONE":
        return "已完成"
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
    if logistics == "BLOCKED":
        return "物流信息需复核"
    if logistics != "READY":
        return "物流信息需复核"
    if not all(
        (
            str(row.carrier or "").strip(),
            str(row.international_tracking_no or "").strip(),
            str(row.actual_total or "").strip(),
            str(row.chargeable_weight_kg or "").strip(),
        )
    ):
        return "物流信息需复核"
    if erp == "BLOCKED":
        return "标发需人工复核"
    if _shipment_has_live_lease(row, now=now):
        return "标发处理中"
    if erp == "RUNNING" or checkpoint not in {"", "NONE"}:
        return "可继续标发"
    if erp == "RETRYABLE":
        return "标发失败可重试"
    return "可标发"


def _shipment_execution_eligibility(
    row: ShipmentRow,
    *,
    active_logistics_nos: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    status = _shipment_business_status(row, now=now)
    if str(row.scan_issue_code or "").strip():
        return False, "扫描错误记录不能执行标发"
    if row.logistics_no in (active_logistics_nos or set()):
        return False, "已有等待或运行中的标发任务"
    if status not in {"可标发", "可继续标发", "标发失败可重试"}:
        return False, status
    if _shipment_has_live_lease(row, now=now):
        return False, "标发任务正在处理中"
    if not _shipment_retry_is_due(row.erp_next_attempt_at, now=now):
        return False, "尚未到标发重试时间"
    return True, status


def _shipment_status_explanation(row: ShipmentRow, status: str) -> str:
    if str(row.scan_issue_code or "").strip():
        original_error = str(
            row.last_error or "自动标发扫描字段错误，请检查领星订单数据。"
        ).strip()
        reason = str(row.scan_issue_reason or "").strip()
        scan_state = str(row.scan_issue_state or "ACTIVE").strip().upper()
        prefix = {
            "MANUAL_REVIEW": "已转为人工复核",
            "MANUALLY_COMPLETED": "已人工标记完成（未写入 ERP）",
            "MANUALLY_CANCELLED": "已人工取消并永久保留记录",
        }.get(scan_state, "")
        if not prefix:
            return original_error
        reason_text = f"；原因：{reason}" if reason else ""
        return f"{prefix}{reason_text}；原扫描错误：{original_error}"
    if status == "已完成":
        if str(row.completion_source or "").strip().upper() == "MANUAL_DETECTED":
            return "已检测到领星订单在外部完成出库，自动标发任务已结案。"
        return "ERP 标发流程已完成。"
    stage_messages = _shipment_error_messages(row)
    if stage_messages:
        return "；".join(stage_messages)
    if status == "物流逾期异常":
        return shipment_tracking_attention_notice(
            customer_shipping_service=row.customer_shipping_service,
            first_seen_at=row.first_seen_at,
            carrier=row.carrier,
            international_tracking_no=row.international_tracking_no,
            logistics_state=row.logistics_state,
            identity_state=row.identity_state,
            erp_state=row.erp_state,
            tracking_validated=row.tracking_validated,
        ) or "物流资料已超过客选时效，请关注订单情况。"
    if status == "可标发":
        return "物流资料校验通过，勾选后可执行标发。"
    if status == "可继续标发":
        return f"将从“{_shipment_checkpoint_label(row.checkpoint)}”之后继续。"
    if status == "标发处理中":
        return "后台任务正在执行，请勿重复提交。"
    if status == "标发失败可重试" and not _shipment_retry_is_due(row.erp_next_attempt_at):
        return f"将在 {row.erp_next_attempt_at} 后允许重试。"
    if status == "待查询物流":
        return "等待查询阿里国际站物流详情。"
    if status == "等待物流就绪":
        return row.alibaba_status or "阿里物流尚未达到可处理状态。"
    return status


def _operator_display(name: object, email: object) -> str:
    operator_name = str(name or "").strip()
    operator_email = str(email or "").strip()
    if (
        operator_name
        and operator_email
        and operator_name.casefold() != operator_email.casefold()
    ):
        return f"{operator_name}（{operator_email}）"
    return operator_email or operator_name or "-"


if PYSIDE6_AVAILABLE:
    from PySide6.QtCore import (
        QEvent,
        QObject,
        QSize,
        Qt,
        QThread,
        QTimer,
        QUrl,
        Signal,
    )
    from PySide6.QtGui import (
        QColor,
        QDesktopServices,
        QFont,
        QKeySequence,
        QPainter,
        QPainterPath,
        QPen,
        QStandardItem,
        QStandardItemModel,
        QShortcut,
    )
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QPlainTextEdit,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QStyle,
        QStyleOptionButton,
        QStyleOptionViewItem,
        QStyledItemDelegate,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
    from .modern_dialogs import (
        confirm_cloudflare_access_login,
        show_queue_conflict_dialog,
    )

    def _add_proportional_toolbar_widgets(
        layout: QHBoxLayout,
        widgets: Sequence[QWidget],
    ) -> None:
        """Fill a toolbar row while preserving each control's natural proportion."""

        for widget in widgets:
            size_policy = widget.sizePolicy()
            size_policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            widget.setSizePolicy(size_policy)
            layout.addWidget(widget, max(1, widget.sizeHint().width()))

    def _show_log_viewer(
        parent: QWidget,
        title: str,
        content: str,
        *,
        hint: str = "日志内容已脱敏，可直接搜索和复制。",
    ) -> None:
        """Show server-hosted logs inside the shared desktop client."""

        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        dialog.resize(1000, 700)
        layout = QVBoxLayout(dialog)
        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText(content)
        layout.addWidget(viewer, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


    class _ControlResultThread(QThread):
        result_ready = Signal(object)

        def __init__(self, operation: Callable[[], ControlResult], parent=None) -> None:
            super().__init__(parent)
            self._operation = operation

        def run(self) -> None:
            try:
                result = self._operation()
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                result = ControlResult(
                    False,
                    f"后台操作失败：{type(exc).__name__}。",
                )
            self.result_ready.emit(result)


    class _ValueThread(QThread):
        value_ready = Signal(object)
        value_failed = Signal(object)

        def __init__(self, operation: Callable[[], object], parent=None) -> None:
            super().__init__(parent)
            self._operation = operation

        def run(self) -> None:
            try:
                value = self._operation()
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.value_failed.emit(exc)
                return
            self.value_ready.emit(value)


    def _run_control_result_responsive(
        owner: QWidget,
        controller: BackgroundTaskController,
        operation: Callable[[], ControlResult],
        result_handler: Callable[[ControlResult], None],
    ) -> None:
        """Run remote control calls without blocking painting or window dragging."""

        if not (
            getattr(controller, "snapshot_runs_in_background", False)
            or getattr(controller, "control_calls_run_in_background", False)
        ):
            result_handler(operation())
            return
        threads = getattr(owner, "_responsive_control_threads", None)
        if threads is None:
            threads = set()
            setattr(owner, "_responsive_control_threads", threads)
        thread = _ControlResultThread(operation, owner)
        threads.add(thread)

        def cleanup(current: _ControlResultThread = thread) -> None:
            threads.discard(current)
            current.deleteLater()
            window = owner.window()
            if bool(getattr(window, "_close_pending", False)):
                QTimer.singleShot(0, window.close)

        thread.result_ready.connect(result_handler)
        thread.finished.connect(cleanup)
        thread.start()


    def _run_value_responsive(
        owner: QWidget,
        controller: BackgroundTaskController,
        operation: Callable[[], object],
        value_handler: Callable[[object], None],
        error_handler: Callable[[object], None] | None = None,
    ) -> None:
        """Run a potentially remote read without occupying the Qt main loop."""

        if not (
            getattr(controller, "snapshot_runs_in_background", False)
            or getattr(controller, "control_calls_run_in_background", False)
        ):
            try:
                value_handler(operation())
            except Exception as exc:
                if error_handler is not None:
                    error_handler(exc)
            return
        threads = getattr(owner, "_responsive_value_threads", None)
        if threads is None:
            threads = set()
            setattr(owner, "_responsive_value_threads", threads)
        thread = _ValueThread(operation, owner)
        threads.add(thread)

        def cleanup(current: _ValueThread = thread) -> None:
            threads.discard(current)
            current.deleteLater()
            window = owner.window()
            if bool(getattr(window, "_close_pending", False)):
                QTimer.singleShot(0, window.close)

        thread.value_ready.connect(value_handler)
        if error_handler is not None:
            thread.value_failed.connect(error_handler)
        thread.finished.connect(cleanup)
        thread.start()


    def _submit_task_commands(
        controller: BackgroundTaskController,
        commands: Sequence[TaskCommand],
    ) -> tuple[ControlResult, ...]:
        """Use one transport call for a UI batch, with local compatibility."""

        normalized = tuple(commands)
        if not normalized:
            return ()
        submit_batch = getattr(controller, "submit_tasks", None)
        if callable(submit_batch):
            value = submit_batch(normalized)
            if isinstance(value, ControlResult):
                return tuple(value for _command in normalized)
            if (
                isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                and len(value) == len(normalized)
                and all(isinstance(result, ControlResult) for result in value)
            ):
                return tuple(value)
            failure = ControlResult(
                False,
                "后台返回的批量任务结果无效，本批次已停止继续提交。",
            )
            return tuple(failure for _command in normalized)
        return tuple(controller.submit_task(command) for command in normalized)


    class _SnapshotThread(QThread):
        snapshot_ready = Signal(object)
        snapshot_failed = Signal(object)

        def __init__(
            self,
            operation: Callable[[], DesktopSnapshot],
            parent=None,
        ) -> None:
            super().__init__(parent)
            self._operation = operation

        def run(self) -> None:
            try:
                snapshot = self._operation()
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.snapshot_failed.emit(exc)
                return
            self.snapshot_ready.emit(snapshot)


    class _RequiredClientUpdateThread(QThread):
        result_ready = Signal(object)
        update_failed = Signal(object)

        def __init__(
            self,
            operation: Callable[[str], object],
            required_version: str,
            parent=None,
        ) -> None:
            super().__init__(parent)
            self._operation = operation
            self._required_version = required_version

        def run(self) -> None:
            try:
                result = self._operation(self._required_version)
            except Exception as exc:  # pragma: no cover - defensive UI boundary
                self.update_failed.emit(exc)
                return
            self.result_ready.emit(result)

    ResultHandler = Callable[[ControlResult], None]
    ShipmentBatchHandler = Callable[[str, tuple[str, ...]], None]
    ShipmentScanHandler = Callable[[str], None]


    class _ModernComboItemDelegate(QStyledItemDelegate):
        def sizeHint(self, option, index) -> QSize:  # noqa: N802
            size = super().sizeHint(option, index)
            size.setHeight(max(size.height(), 36))
            return size

        def paint(self, painter: QPainter, option, index) -> None:
            styled = QStyleOptionViewItem(option)
            self.initStyleOption(styled, index)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            selected = bool(styled.state & QStyle.StateFlag.State_Selected)
            hovered = bool(styled.state & QStyle.StateFlag.State_MouseOver)
            if selected or hovered:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#EFF6FF" if selected else "#F2F4F7"))
                painter.drawRoundedRect(styled.rect.adjusted(4, 2, -4, -2), 6, 6)
            check_state = index.data(Qt.ItemDataRole.CheckStateRole)
            text_left = 12
            if check_state is not None:
                checkbox = QStyleOptionButton()
                checkbox.state = QStyle.StateFlag.State_Enabled
                checkbox.state |= (
                    QStyle.StateFlag.State_On
                    if check_state
                    in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                    else QStyle.StateFlag.State_Off
                )
                checkbox.rect = self._checkbox_rect(styled)
                style = (
                    styled.widget.style()
                    if styled.widget is not None
                    else QApplication.style()
                )
                style.drawControl(
                    QStyle.ControlElement.CE_CheckBox,
                    checkbox,
                    painter,
                    styled.widget,
                )
                text_left = checkbox.rect.right() - styled.rect.left() + 10
            painter.setPen(QColor("#1D4ED8" if selected else "#344054"))
            text_rect = styled.rect.adjusted(text_left, 0, -10, 0)
            text = styled.fontMetrics.elidedText(
                styled.text,
                Qt.TextElideMode.ElideRight,
                text_rect.width(),
            )
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
            painter.restore()

        @staticmethod
        def _checkbox_rect(option: QStyleOptionViewItem):
            checkbox = QStyleOptionButton()
            style = (
                option.widget.style()
                if option.widget is not None
                else QApplication.style()
            )
            indicator = style.subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator,
                checkbox,
                option.widget,
            )
            indicator.moveLeft(option.rect.left() + 12)
            indicator.moveTop(option.rect.center().y() - indicator.height() // 2)
            return indicator


    class _ModernComboBox(QComboBox):
        """Combo box with a consistent chevron instead of the native Windows arrow."""

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setMaxVisibleItems(12)
            self.setItemDelegate(_ModernComboItemDelegate(self))
            self.view().setSpacing(1)

        def paintEvent(self, event) -> None:  # noqa: N802
            super().paintEvent(event)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = "#98A2B3" if not self.isEnabled() else "#475467"
            if self.hasFocus() or self.view().isVisible():
                color = "#2563EB"
            pen = QPen(QColor(color), 1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            center_x = self.width() - 18
            center_y = self.height() / 2
            path = QPainterPath()
            popup_open = self.view().isVisible()
            outer_y = center_y + 2 if popup_open else center_y - 2
            inner_y = center_y - 2 if popup_open else center_y + 2
            path.moveTo(center_x - 4, outer_y)
            path.lineTo(center_x, inner_y)
            path.lineTo(center_x + 4, outer_y)
            painter.drawPath(path)


    class _ModernSpinBox(QSpinBox):
        """Spin box with the same rounded step area and chevrons as combo boxes."""

        def paintEvent(self, event) -> None:  # noqa: N802
            super().paintEvent(event)
            if bool(self.property("hideStepArrows")):
                return
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = "#98A2B3" if not self.isEnabled() else "#475467"
            if self.hasFocus():
                color = "#2563EB"
            pen = QPen(QColor(color), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            center_x = self.width() - 18
            for center_y, points_up in (
                (self.height() * 0.30, True),
                (self.height() * 0.70, False),
            ):
                path = QPainterPath()
                outer_y = center_y + 1.5 if points_up else center_y - 1.5
                inner_y = center_y - 1.5 if points_up else center_y + 1.5
                path.moveTo(center_x - 3, outer_y)
                path.lineTo(center_x, inner_y)
                path.lineTo(center_x + 3, outer_y)
                painter.drawPath(path)


    class _FullCellCheckDelegate(QStyledItemDelegate):
        """Toggle a checkable item once when any point in its cell is clicked."""

        def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
            flags = model.flags(index)
            is_clickable_check = bool(
                flags & Qt.ItemFlag.ItemIsEnabled
                and flags & Qt.ItemFlag.ItemIsUserCheckable
            )
            if (
                is_clickable_check
                and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
            ):
                current = index.data(Qt.ItemDataRole.CheckStateRole)
                next_state = (
                    Qt.CheckState.Unchecked.value
                    if current == Qt.CheckState.Checked.value
                    else Qt.CheckState.Checked.value
                )
                return bool(
                    model.setData(
                        index,
                        next_state,
                        Qt.ItemDataRole.CheckStateRole,
                    )
                )
            return super().editorEvent(event, model, option, index)


    # Keep all existing constructors and type checks while applying the modern
    # rendering consistently to page, settings and interaction-dialog combos.
    QComboBox = _ModernComboBox
    QSpinBox = _ModernSpinBox


    def _format_time(value) -> str:
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


    def _readonly_item(text: object, *, user_data: object | None = None) -> QTableWidgetItem:
        item = QTableWidgetItem(str(text or ""))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
        return item


    class _WorkflowStatusItem(QTableWidgetItem):
        def __init__(self, text: str, sort_key: tuple[int, float, str] | None) -> None:
            super().__init__(text)
            self._workflow_sort_key = sort_key

        def __lt__(self, other: QTableWidgetItem) -> bool:
            other_key = getattr(other, "_workflow_sort_key", None)
            if self._workflow_sort_key is not None and other_key is not None:
                return self._workflow_sort_key < other_key
            return super().__lt__(other)


    def _workflow_status_item(
        status: object,
        *,
        sort_key: tuple[int, float, str] | None = None,
    ) -> QTableWidgetItem:
        raw = str(status or "")
        item = _WorkflowStatusItem(_custom_workflow_status_label(raw), sort_key)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        color = {
            "processing": "#175CD3",
            "waiting": "#667085",
            "pending": "#B54708",
            "folder_pending": "#B54708",
            "sku_adjustment_pending": "#B54708",
            "package_split_pending": "#B54708",
            "instruction_remark_pending": "#B54708",
            "warehouse_logistics_pending": "#B54708",
            "not_required": "#667085",
            "blocked": "#B42318",
            "completed": "#027A48",
            "已忽略": "#667085",
        }.get(raw, "#344054")
        item.setForeground(QColor(color))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        return item


    def _notification_status_item(
        state: object,
        package_missing: object = 0,
        is_supplemental_revision: object = False,
        last_error: object = "",
    ) -> QTableWidgetItem:
        raw = str(state or "")
        item = _readonly_item(
            _notification_state_label(
                raw,
                package_missing,
                is_supplemental_revision,
                last_error,
            )
        )
        item.setForeground(QColor(_notification_status_color(raw, package_missing)))
        if _notification_status_is_bold(raw, package_missing):
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        return item


    class _AdaptiveTableColumnController(QObject):
        """Keep interactive columns fitted to the current table viewport."""

        def __init__(
            self,
            table: QTableWidget,
            *,
            unscaled_columns: Sequence[int] = (),
        ) -> None:
            super().__init__(table)
            self._table = table
            self._viewport = table.viewport()
            self._unscaled_columns = {
                int(column)
                for column in unscaled_columns
                if 0 <= int(column) < table.columnCount()
            }
            self._preferred_widths = tuple(
                table.horizontalHeader().sectionSize(column)
                for column in range(table.columnCount())
            )
            self._applying = False
            self._fit_pending = False
            self._viewport.installEventFilter(self)
            table.horizontalHeader().sectionResized.connect(
                self._remember_interactive_widths
            )

        @staticmethod
        def _allocate_proportional_widths(
            preferred: Sequence[int],
            available: int,
            minimum: int,
        ) -> tuple[int, ...]:
            count = len(preferred)
            if count == 0:
                return ()
            minimum_total = minimum * count
            if available <= minimum_total:
                return tuple(minimum for _ in preferred)
            remaining = available - minimum_total
            weights = [max(1, int(width) - minimum) for width in preferred]
            weight_total = sum(weights)
            exact_extras = [remaining * weight / weight_total for weight in weights]
            extras = [int(value) for value in exact_extras]
            undistributed = remaining - sum(extras)
            order = sorted(
                range(count),
                key=lambda index: exact_extras[index] - extras[index],
                reverse=True,
            )
            for index in order[:undistributed]:
                extras[index] += 1
            return tuple(minimum + extra for extra in extras)

        def set_preferred_widths(
            self,
            widths: Sequence[int],
            *,
            unscaled_columns: Sequence[int] = (),
        ) -> None:
            if len(widths) != self._table.columnCount():
                raise ValueError("默认列宽数量必须与表格列数一致。")
            self._unscaled_columns.update(
                int(column)
                for column in unscaled_columns
                if 0 <= int(column) < self._table.columnCount()
            )
            header = self._table.horizontalHeader()
            minimum = max(1, header.minimumSectionSize())
            normalized = tuple(max(minimum, int(width)) for width in widths)
            self._applying = True
            try:
                for column, width in enumerate(normalized):
                    header.resizeSection(column, width)
            finally:
                self._applying = False
            self._preferred_widths = normalized
            self._schedule_fit()

        def eventFilter(self, watched, event) -> bool:
            if (
                watched is self._viewport
                and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
            ):
                self._schedule_fit()
            return False

        def _remember_interactive_widths(
            self,
            _logical_index: int,
            _old_size: int,
            _new_size: int,
        ) -> None:
            if self._applying:
                return
            header = self._table.horizontalHeader()
            self._preferred_widths = tuple(
                header.sectionSize(column)
                for column in range(self._table.columnCount())
            )

        def _schedule_fit(self) -> None:
            if self._fit_pending:
                return
            self._fit_pending = True
            QTimer.singleShot(0, self._fit_to_viewport)

        def _fit_to_viewport(self) -> None:
            self._fit_pending = False
            try:
                column_count = self._table.columnCount()
                available = self._table.viewport().width()
                header = self._table.horizontalHeader()
            except RuntimeError:
                return
            if column_count == 0 or available <= 0:
                return
            if len(self._preferred_widths) != column_count:
                self._preferred_widths = tuple(
                    header.sectionSize(column)
                    for column in range(column_count)
                )
            minimum = max(1, header.minimumSectionSize())
            targets = [0] * column_count
            unscaled_total = 0
            flexible_columns: list[int] = []
            flexible_preferred: list[int] = []
            for column, preferred in enumerate(self._preferred_widths):
                if column in self._unscaled_columns:
                    targets[column] = max(minimum, int(preferred))
                    unscaled_total += targets[column]
                else:
                    flexible_columns.append(column)
                    flexible_preferred.append(max(minimum, int(preferred)))
            if flexible_columns:
                flexible_targets = self._allocate_proportional_widths(
                    flexible_preferred,
                    max(0, available - unscaled_total),
                    minimum,
                )
                for column, width in zip(
                    flexible_columns,
                    flexible_targets,
                    strict=True,
                ):
                    targets[column] = width
            elif targets:
                targets[-1] += max(0, available - sum(targets))
            self._applying = True
            try:
                for column, width in enumerate(targets):
                    header.resizeSection(column, width)
            finally:
                self._applying = False


    def _prepare_table(
        table: QTableWidget,
        *,
        full_cell_check_column: int | None = None,
    ) -> None:
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(table.columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.ResizeMode.Interactive,
            )
        adaptive_controller = _AdaptiveTableColumnController(
            table,
            unscaled_columns=(
                (full_cell_check_column,)
                if full_cell_check_column is not None
                else ()
            ),
        )
        setattr(table, "_adaptive_column_controller", adaptive_controller)
        if full_cell_check_column is not None:
            table.setItemDelegateForColumn(
                full_cell_check_column,
                _FullCellCheckDelegate(table),
            )


    def _set_table_default_widths(
        table: QTableWidget,
        widths: Sequence[int],
        *,
        unscaled_columns: Sequence[int] = (),
    ) -> None:
        controller = getattr(table, "_adaptive_column_controller", None)
        if not isinstance(controller, _AdaptiveTableColumnController):
            raise RuntimeError("表格必须先完成统一列宽初始化。")
        controller.set_preferred_widths(
            widths,
            unscaled_columns=unscaled_columns,
        )


    def _table_scroll_state(table: QTableWidget) -> tuple[int, int]:
        return (
            table.verticalScrollBar().value(),
            table.horizontalScrollBar().value(),
        )


    def _restore_table_scroll_state(
        table: QTableWidget,
        state: tuple[int, int],
    ) -> None:
        vertical, horizontal = state
        table.verticalScrollBar().setValue(
            min(vertical, table.verticalScrollBar().maximum())
        )
        table.horizontalScrollBar().setValue(
            min(horizontal, table.horizontalScrollBar().maximum())
        )


    def _copy_table_selection(table: QTableWidget) -> None:
        indexes = sorted(table.selectedIndexes(), key=lambda item: (item.row(), item.column()))
        if not indexes:
            current = table.currentIndex()
            if current.isValid():
                indexes = [current]
        if not indexes:
            return
        rows: dict[int, dict[int, str]] = {}
        for index in indexes:
            rows.setdefault(index.row(), {})[index.column()] = str(index.data() or "")
        lines = [
            "\t".join(columns[column] for column in sorted(columns))
            for _row, columns in sorted(rows.items())
        ]
        QApplication.clipboard().setText("\n".join(lines))


    def _enable_table_copy(table: QTableWidget) -> None:
        shortcut = QShortcut(QKeySequence.StandardKey.Copy, table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda current=table: _copy_table_selection(current))
        table._copy_shortcut = shortcut  # type: ignore[attr-defined]
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def show_copy_menu(position, current: QTableWidget = table) -> None:
            index = current.indexAt(position)
            if index.isValid():
                current.setCurrentCell(index.row(), index.column())
            if not current.currentIndex().isValid():
                return
            menu = QMenu(current)
            copy_action = menu.addAction("复制所选内容\tCtrl+C")
            copy_action.triggered.connect(
                lambda _checked=False, target=current: _copy_table_selection(target)
            )
            menu.exec(current.viewport().mapToGlobal(position))

        table.customContextMenuRequested.connect(show_copy_menu)
        table._copy_context_menu_handler = show_copy_menu  # type: ignore[attr-defined]


    def _modern_stylesheet() -> str:
        return """
            QMainWindow, QWidget {
                background: #F5F7FB;
                color: #172033;
                font-family: "Segoe UI", "Microsoft YaHei UI";
                font-size: 10pt;
            }
            QLabel { background: transparent; }
            QFrame#sidebar {
                background: #111827;
                border: 0;
            }
            QLabel#brandTitle {
                background: transparent;
                color: #FFFFFF;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#brandSubtitle, QLabel#sidebarStatus {
                background: transparent;
                color: #94A3B8;
                font-size: 9pt;
            }
            QFrame#safetyPanel {
                background: #182235;
                border: 1px solid #293548;
                border-radius: 12px;
            }
            QFrame#safetyPanel[emergencyActive="true"] {
                background: #3A1D24;
                border-color: #7F1D1D;
            }
            QLabel#safetyTitle {
                color: #94A3B8;
                font-size: 9pt;
                font-weight: 600;
            }
            QLabel#safetyState {
                color: #86EFAC;
                font-size: 10pt;
                font-weight: 700;
            }
            QLabel#safetyState[emergencyActive="true"] {
                color: #FCA5A5;
            }
            QLabel#safetyDetail {
                color: #64748B;
                font-size: 8.5pt;
            }
            QLabel#emergencyBanner {
                color: #B42318;
                background: #FEF3F2;
                border: 1px solid #FDA29B;
                border-radius: 8px;
                margin: 10px 18px 0 18px;
                padding: 9px 12px;
                font-weight: 700;
            }
            QLabel#localTestBanner {
                color: #9A3412;
                background: #FFF7ED;
                border: 1px solid #FDBA74;
                border-radius: 8px;
                margin: 10px 18px 0 18px;
                padding: 9px 12px;
                font-weight: 700;
            }
            QListWidget#navigation {
                background: transparent;
                color: #CBD5E1;
                border: 0;
                outline: 0;
            }
            QListWidget#navigation::item {
                min-height: 42px;
                padding: 0 14px;
                margin: 3px 0;
                border-radius: 9px;
            }
            QListWidget#navigation::item:hover {
                background: #1F2937;
                color: #FFFFFF;
            }
            QListWidget#navigation::item:selected {
                background: #2563EB;
                color: #FFFFFF;
                font-weight: 600;
            }
            QLabel#pageTitle {
                color: #111827;
                font-size: 23px;
                font-weight: 700;
                padding: 2px 0 8px 0;
            }
            QLabel#sectionHint {
                color: #475467;
                background: #EEF4FF;
                border: 1px solid #D1E0FF;
                border-radius: 8px;
                padding: 9px 12px;
            }
            QLabel#queueStatusBanner {
                color: #344054;
                background: #EEF4FF;
                border: 1px solid #D1E0FF;
                border-radius: 8px;
                padding: 8px 11px;
            }
            QFrame#queueFilterPanel {
                background: #FFFFFF;
                border: 1px solid #E4E7EC;
                border-radius: 9px;
            }
            QLabel#queueFilterLabel {
                color: #667085;
                font-size: 9pt;
            }
            QFrame#queueBatchBar {
                background: #EEF4FF;
                border: 1px solid #B2CCFF;
                border-radius: 9px;
            }
            QLabel#queueSelectionSummary {
                color: #344054;
                font-weight: 600;
            }
            QPushButton {
                min-height: 32px;
                padding: 0 13px;
                background: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 7px;
                font-weight: 500;
            }
            QPushButton:hover { background: #F8FAFC; border-color: #98A2B3; }
            QPushButton:pressed { background: #EEF2F6; }
            QPushButton:disabled { color: #98A2B3; background: #F2F4F7; }
            QPushButton#primaryButton {
                color: #FFFFFF;
                background: #2563EB;
                border-color: #2563EB;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover { background: #1D4ED8; border-color: #1D4ED8; }
            QPushButton#quickSelectButton {
                color: #067647;
                background: #ECFDF3;
                border-color: #ABEFC6;
                font-weight: 600;
            }
            QPushButton#quickSelectButton:hover {
                background: #DCFAE6;
                border-color: #75E0A7;
            }
            QPushButton#quickSelectButton:pressed {
                background: #ABEFC6;
                border-color: #47CD89;
            }
            QWidget#queuePaginationBar {
                background: #FFFFFF;
            }
            QLabel#paginationTotalLabel, QLabel#paginationJumpLabel {
                color: #475467;
            }
            QPushButton#paginationArrowButton {
                min-width: 32px;
                max-width: 32px;
                min-height: 32px;
                max-height: 32px;
                padding: 0;
                border: 0;
                background: transparent;
                color: #667085;
                font-size: 18px;
                font-weight: 600;
            }
            QPushButton#paginationArrowButton:hover {
                color: #2563EB;
                background: #EFF6FF;
            }
            QPushButton#paginationArrowButton:disabled {
                color: #D0D5DD;
                background: transparent;
            }
            QLabel#paginationCurrentPage {
                min-width: 32px;
                max-width: 32px;
                min-height: 32px;
                max-height: 32px;
                color: #2563EB;
                background: #EFF6FF;
                border: 1px solid #DBEAFE;
                border-radius: 5px;
                font-weight: 600;
            }
            QComboBox#paginationPageSize {
                min-width: 96px;
                max-width: 112px;
                min-height: 32px;
                max-height: 32px;
                padding-left: 10px;
                border-radius: 5px;
                font-weight: 400;
            }
            QSpinBox#paginationJumpSpin {
                min-width: 58px;
                max-width: 68px;
                min-height: 32px;
                max-height: 32px;
                padding: 0 7px;
                border-radius: 5px;
            }
            QPushButton#dangerButton { color: #B42318; border-color: #FDA29B; }
            QPushButton#dangerButton:hover { background: #FEF3F2; border-color: #F97066; }
            QPushButton#globalEmergencyButton {
                min-height: 30px;
                padding: 0 10px;
                color: #FECACA;
                background: transparent;
                border: 1px solid #7F1D1D;
                border-radius: 7px;
                font-size: 9pt;
                font-weight: 600;
            }
            QPushButton#globalEmergencyButton:hover {
                color: #FFFFFF;
                background: #7F1D1D;
                border-color: #F87171;
            }
            QPushButton#globalEmergencyButton[emergencyActive="true"] {
                color: #991B1B;
                background: #FEE2E2;
                border-color: #FECACA;
            }
            QPushButton#globalEmergencyButton[emergencyActive="true"]:hover {
                color: #7F1D1D;
                background: #FECACA;
                border-color: #FCA5A5;
            }
            QLineEdit, QPlainTextEdit {
                min-height: 32px;
                padding: 0 9px;
                background: #FFFFFF;
                color: #172033;
                border: 1px solid #D0D5DD;
                border-radius: 7px;
                selection-background-color: #BFDBFE;
            }
            QPlainTextEdit { padding: 8px; }
            QLineEdit:focus, QPlainTextEdit:focus {
                border: 1px solid #3B82F6;
            }
            QSpinBox {
                min-height: 36px;
                padding: 0 42px 0 13px;
                background: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 10px;
                selection-background-color: #BFDBFE;
            }
            QSpinBox:hover { background: #F9FAFB; border-color: #98A2B3; }
            QSpinBox:focus { background: #FFFFFF; border: 1px solid #2563EB; }
            QSpinBox::up-button, QSpinBox::down-button {
                subcontrol-origin: border;
                width: 30px;
                background: #F2F4F7;
                border: 0;
            }
            QSpinBox::up-button {
                subcontrol-position: top right;
                border-top-right-radius: 9px;
            }
            QSpinBox::down-button {
                subcontrol-position: bottom right;
                border-bottom-right-radius: 9px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background: #EAECF0;
            }
            QSpinBox::up-arrow, QSpinBox::down-arrow {
                image: none;
                width: 0;
                height: 0;
            }
            QComboBox {
                min-height: 36px;
                padding: 0 42px 0 13px;
                background: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 10px;
                font-weight: 500;
                selection-background-color: #EFF6FF;
                selection-color: #1D4ED8;
            }
            QComboBox:hover {
                background: #F9FAFB;
                border-color: #98A2B3;
            }
            QComboBox:focus, QComboBox:on {
                background: #FFFFFF;
                border: 1px solid #2563EB;
            }
            QComboBox:disabled {
                background: #F2F4F7;
                color: #98A2B3;
                border-color: #E4E7EC;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 30px;
                margin: 4px 4px 4px 0;
                background: #F2F4F7;
                border: 0;
                border-radius: 7px;
            }
            QComboBox:hover::drop-down {
                background: #EAECF0;
            }
            QComboBox:on::drop-down {
                background: #DBEAFE;
            }
            QComboBox::down-arrow {
                image: none;
                width: 0;
                height: 0;
            }
            QComboBox QAbstractItemView {
                background: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 8px;
                padding: 5px;
                outline: 0;
                selection-background-color: #EFF6FF;
                selection-color: #1D4ED8;
            }
            QComboBox QAbstractItemView::item {
                min-height: 34px;
                padding: 0 10px;
                border-radius: 6px;
            }
            QComboBox QAbstractItemView::item:hover {
                background: #F2F4F7;
            }
            QComboBox QAbstractItemView::item:selected {
                background: #EFF6FF;
                color: #1D4ED8;
            }
            QMenu {
                min-width: 180px;
                padding: 6px;
                background: #FFFFFF;
                color: #344054;
                border: 1px solid #D0D5DD;
                border-radius: 8px;
            }
            QMenu::item {
                min-height: 30px;
                padding: 3px 24px 3px 10px;
                border-radius: 6px;
            }
            QMenu::item:selected { background: #EFF6FF; color: #1D4ED8; }
            QMenu::item:disabled { color: #98A2B3; }
            QMenu::separator {
                height: 1px;
                margin: 5px 8px;
                background: #E4E7EC;
            }
            QTableWidget {
                background: #FFFFFF;
                alternate-background-color: #F8FAFC;
                border: 1px solid #E4E7EC;
                border-radius: 9px;
                outline: 0;
                selection-background-color: #DBEAFE;
                selection-color: #172033;
            }
            QTableWidget::item { padding: 5px 8px; border: 0; }
            QTableWidget::item:selected {
                background: #EAF2FF;
                color: #172033;
                border: 1px solid #93C5FD;
            }
            QHeaderView::section {
                min-height: 34px;
                background: #F2F4F7;
                color: #475467;
                padding: 0 8px;
                border: 0;
                border-bottom: 1px solid #E4E7EC;
                font-weight: 600;
            }
            QGroupBox {
                margin-top: 12px;
                padding-top: 12px;
                background: #FFFFFF;
                border: 1px solid #E4E7EC;
                border-radius: 10px;
                font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QCheckBox { spacing: 7px; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
            QScrollBar::handle:vertical { background: #C7CDD6; border-radius: 4px; min-height: 28px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QStatusBar { background: #FFFFFF; color: #667085; border-top: 1px solid #E4E7EC; }
            QToolTip { background: #111827; color: white; border: 0; padding: 6px; }
        """


    class _CheckableHeaderView(QHeaderView):
        check_state_changed = Signal(int)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(Qt.Orientation.Horizontal, parent)
            self._check_state = Qt.CheckState.Unchecked
            self.setSectionsClickable(True)

        @property
        def check_state(self) -> Qt.CheckState:
            return self._check_state

        def set_check_state(self, state: Qt.CheckState) -> None:
            normalized = Qt.CheckState(state)
            if normalized == self._check_state:
                return
            self._check_state = normalized
            self.viewport().update()

        def paintSection(self, painter, rect, logical_index: int) -> None:  # noqa: N802
            super().paintSection(painter, rect, logical_index)
            if logical_index != 0:
                return
            option = QStyleOptionButton()
            option.rect = rect
            option.state = QStyle.StateFlag.State_Enabled
            option.state |= {
                Qt.CheckState.Unchecked: QStyle.StateFlag.State_Off,
                Qt.CheckState.PartiallyChecked: QStyle.StateFlag.State_NoChange,
                Qt.CheckState.Checked: QStyle.StateFlag.State_On,
            }[self._check_state]
            indicator = self.style().subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator,
                option,
                self,
            )
            indicator.moveCenter(rect.center())
            option.rect = indicator
            self.style().drawControl(
                QStyle.ControlElement.CE_CheckBox,
                option,
                painter,
                self,
            )

        def mousePressEvent(self, event) -> None:  # noqa: N802
            if self.logicalIndexAt(event.position().toPoint()) == 0:
                target = (
                    Qt.CheckState.Unchecked
                    if self._check_state == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                self.check_state_changed.emit(target.value)
                event.accept()
                return
            super().mousePressEvent(event)


    class _ProductTypeFilterCombo(QComboBox):
        """Compact checkable dropdown shared by the three order queues."""

        selection_changed = Signal()
        _ALL_VALUE = "__all_product_types__"

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._selected_values: set[str] = set()
            self._available_values: tuple[str, ...] = ()
            self._skip_next_hide = False
            self.setEditable(True)
            self.lineEdit().setReadOnly(True)
            self.lineEdit().setPlaceholderText("全部商品类型")
            self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            self.setMinimumWidth(180)
            self.setMaximumWidth(320)
            self.setToolTip(
                "可多选商品类型；未选任何类型时显示全部商品类型。"
            )
            self.setModel(QStandardItemModel(self))
            self.view().pressed.connect(self._toggle_index)
            self.activated.connect(
                lambda _index: QTimer.singleShot(0, self._update_summary)
            )
            self._rebuild_items()

        @property
        def selected_values(self) -> frozenset[str]:
            return frozenset(self._selected_values)

        def set_available_values(self, values: Sequence[object]) -> None:
            available = tuple(
                sorted(
                    dict.fromkeys(str(value or "").strip() for value in values),
                    key=lambda value: _product_type_label(value).casefold(),
                )
            )
            if available == self._available_values:
                return
            self._available_values = available
            self._selected_values.intersection_update(available)
            self._rebuild_items()

        def set_selected_values(self, values: Sequence[object]) -> None:
            selected = {
                str(value or "").strip()
                for value in values
                if str(value or "").strip() in self._available_values
                or not str(value or "").strip()
                and "" in self._available_values
            }
            if selected == self._selected_values:
                return
            self._selected_values = selected
            self._sync_item_states()
            self._update_summary()
            self.selection_changed.emit()

        def hidePopup(self) -> None:  # noqa: N802
            if self._skip_next_hide:
                self._skip_next_hide = False
                return
            super().hidePopup()

        def _toggle_index(self, index) -> None:
            value = str(index.data(Qt.ItemDataRole.UserRole) or "")
            if value == self._ALL_VALUE:
                self._selected_values.clear()
            elif value in self._selected_values:
                self._selected_values.remove(value)
            else:
                self._selected_values.add(value)
            self._skip_next_hide = True
            self._sync_item_states()
            self._update_summary()
            self.selection_changed.emit()

        def _rebuild_items(self) -> None:
            model = self.model()
            model.clear()
            all_item = QStandardItem("全部商品类型")
            all_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            all_item.setData(self._ALL_VALUE, Qt.ItemDataRole.UserRole)
            model.appendRow(all_item)
            for value in self._available_values:
                item = QStandardItem(_product_type_label(value))
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                )
                item.setData(value, Qt.ItemDataRole.UserRole)
                model.appendRow(item)
            self._sync_item_states()
            self._update_summary()

        def _sync_item_states(self) -> None:
            model = self.model()
            for row in range(model.rowCount()):
                item = model.item(row)
                value = str(item.data(Qt.ItemDataRole.UserRole) or "")
                checked = (
                    not self._selected_values
                    if value == self._ALL_VALUE
                    else value in self._selected_values
                )
                item.setCheckState(
                    Qt.CheckState.Checked
                    if checked
                    else Qt.CheckState.Unchecked
                )

        def _update_summary(self) -> None:
            labels = [
                _product_type_label(value)
                for value in self._available_values
                if value in self._selected_values
            ]
            if not labels:
                summary = "全部商品类型"
            elif len(labels) <= 2:
                summary = "、".join(labels)
            else:
                summary = f"{'、'.join(labels[:2])} 等 {len(labels)} 类"
            self.setEditText(summary)
            self.lineEdit().setToolTip(
                "全部商品类型" if not labels else "、".join(labels)
            )


    class _PaginationArrowButton(QPushButton):
        def __init__(
            self,
            direction: str,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__("", parent)
            self._direction = direction
            self.setObjectName("paginationArrowButton")

        def paintEvent(self, event) -> None:  # noqa: N802
            super().paintEvent(event)
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(
                QPen(
                    QColor("#667085" if self.isEnabled() else "#D0D5DD"),
                    1.8,
                )
            )
            center_x = self.width() / 2
            center_y = self.height() / 2
            offset = 1 if self._direction == "previous" else -1
            path = QPainterPath()
            path.moveTo(center_x + 3 * offset, center_y - 5)
            path.lineTo(center_x - 2 * offset, center_y)
            path.lineTo(center_x + 3 * offset, center_y + 5)
            painter.drawPath(path)


    class _QueuePaginationBar(QWidget):
        """Shared compact queue pagination matching the ERP list controls."""

        page_requested = Signal(int)
        page_size_changed = Signal(int)
        PAGE_SIZES = (20, 50, 100)

        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setObjectName("queuePaginationBar")
            self._page = 1
            self._page_count = 1
            row = QHBoxLayout(self)
            row.setContentsMargins(0, 2, 0, 0)
            row.setSpacing(6)
            row.addStretch(1)

            self.total_label = QLabel("共 0 条")
            self.total_label.setObjectName("paginationTotalLabel")
            row.addWidget(self.total_label)

            self.previous_button = _PaginationArrowButton("previous")
            self.previous_button.setToolTip("上一页")
            self.previous_button.setAccessibleName("上一页")
            self.previous_button.clicked.connect(
                lambda: self.page_requested.emit(self._page - 1)
            )
            row.addWidget(self.previous_button)

            self.current_page_label = QLabel("1")
            self.current_page_label.setObjectName("paginationCurrentPage")
            self.current_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(self.current_page_label)

            self.next_button = _PaginationArrowButton("next")
            self.next_button.setToolTip("下一页")
            self.next_button.setAccessibleName("下一页")
            self.next_button.clicked.connect(
                lambda: self.page_requested.emit(self._page + 1)
            )
            row.addWidget(self.next_button)

            self.page_size_combo = QComboBox()
            self.page_size_combo.setObjectName("paginationPageSize")
            for size in self.PAGE_SIZES:
                self.page_size_combo.addItem(f"{size} 条/页", size)
            self.page_size_combo.setCurrentIndex(
                self.page_size_combo.findData(50)
            )
            self.page_size_combo.currentIndexChanged.connect(
                self._emit_page_size
            )
            row.addWidget(self.page_size_combo)

            jump_label = QLabel("前往")
            jump_label.setObjectName("paginationJumpLabel")
            row.addWidget(jump_label)
            self.jump_spin = QSpinBox()
            self.jump_spin.setObjectName("paginationJumpSpin")
            self.jump_spin.setProperty("hideStepArrows", True)
            self.jump_spin.setRange(1, 1)
            self.jump_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            self.jump_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.jump_spin.setToolTip("输入页码后按回车或移开焦点即可跳转")
            self.jump_spin.editingFinished.connect(self._request_jump)
            row.addWidget(self.jump_spin)
            page_suffix = QLabel("页")
            page_suffix.setObjectName("paginationJumpLabel")
            row.addWidget(page_suffix)

        def set_state(
            self,
            *,
            total: int,
            page: int,
            page_size: int,
            page_count: int,
        ) -> None:
            self._page_count = max(1, int(page_count))
            self._page = max(1, min(int(page), self._page_count))
            self.total_label.setText(f"共 {max(0, int(total))} 条")
            self.current_page_label.setText(str(self._page))
            self.current_page_label.setToolTip(
                f"第 {self._page} / {self._page_count} 页"
            )
            self.previous_button.setEnabled(self._page > 1)
            self.next_button.setEnabled(self._page < self._page_count)

            previous = self.page_size_combo.blockSignals(True)
            index = self.page_size_combo.findData(int(page_size))
            if index < 0:
                self.page_size_combo.addItem(
                    f"{int(page_size)} 条/页",
                    int(page_size),
                )
                index = self.page_size_combo.findData(int(page_size))
            self.page_size_combo.setCurrentIndex(index)
            self.page_size_combo.blockSignals(previous)

            previous = self.jump_spin.blockSignals(True)
            self.jump_spin.setRange(1, self._page_count)
            self.jump_spin.setValue(self._page)
            self.jump_spin.blockSignals(previous)

        def _emit_page_size(self, _index: int) -> None:
            self.page_size_changed.emit(
                int(self.page_size_combo.currentData() or 50)
            )

        def _request_jump(self) -> None:
            target = int(self.jump_spin.value())
            if target != self._page:
                self.page_requested.emit(target)


    class _MetricCard(QFrame):
        def __init__(self, title: str, color: str) -> None:
            super().__init__()
            self.setObjectName("metricCard")
            self.setStyleSheet(
                "QFrame#metricCard { background: white; border: 1px solid #E4E7EC; "
                "border-radius: 12px; }"
            )
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 14)
            title_label = QLabel(title)
            title_label.setStyleSheet("color: #667085;")
            self.value_label = QLabel("0")
            font = QFont()
            font.setPointSize(22)
            font.setBold(True)
            self.value_label.setFont(font)
            self.value_label.setStyleSheet(f"color: {color};")
            layout.addWidget(title_label)
            layout.addWidget(self.value_label)

        def set_value(self, value: int) -> None:
            self.value_label.setText(str(value))


    class DashboardPage(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._last_signature: object | None = None
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            title = QLabel("仪表盘")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            self.backend_message = QLabel()
            self.backend_message.setWordWrap(True)
            self.backend_message.setStyleSheet(
                "background: #fff8e1; color: #7a5b00; padding: 10px; border-radius: 5px;"
            )
            layout.addWidget(self.backend_message)

            cards = QGridLayout()
            self.queued_card = _MetricCard("等待中", "#3867d6")
            self.running_card = _MetricCard("运行中", "#8854d0")
            self.succeeded_card = _MetricCard("已完成", "#20bf6b")
            self.attention_card = _MetricCard("需要关注", "#eb3b5a")
            cards.addWidget(self.queued_card, 0, 0)
            cards.addWidget(self.running_card, 0, 1)
            cards.addWidget(self.succeeded_card, 0, 2)
            cards.addWidget(self.attention_card, 0, 3)
            layout.addLayout(cards)

            layout.addWidget(QLabel("今日任务"))
            self.tasks = QTableWidget(0, 7)
            self.tasks.setHorizontalHeaderLabels(
                ["时间", "业务", "任务", "订单号", "操作账号", "状态", "说明"]
            )
            _prepare_table(self.tasks)
            _set_table_default_widths(
                self.tasks,
                (150, 100, 180, 160, 160, 100, 360),
            )
            layout.addWidget(self.tasks, 1)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            metrics = snapshot.dashboard
            rows = snapshot.today_tasks
            signature = (
                metrics.queued,
                metrics.running,
                metrics.succeeded,
                metrics.attention,
                snapshot.backend_message,
                tuple(rows),
            )
            if signature == self._last_signature:
                return
            self._last_signature = signature
            self.queued_card.set_value(metrics.queued)
            self.running_card.set_value(metrics.running)
            self.succeeded_card.set_value(metrics.succeeded)
            self.attention_card.set_value(metrics.attention)
            self.backend_message.setText(snapshot.backend_message)
            scroll_state = _table_scroll_state(self.tasks)
            same_task_layout = (
                self.tasks.rowCount() == len(rows)
                and all(
                    (item := self.tasks.item(row_index, 0)) is not None
                    and item.data(Qt.ItemDataRole.UserRole) == task.task_id
                    for row_index, task in enumerate(rows)
                )
            )
            self.tasks.setUpdatesEnabled(False)
            if not same_task_layout:
                self.tasks.setRowCount(len(rows))
            for row_index, task in enumerate(rows):
                values = (
                    _format_time(task.updated_at),
                    task.area.label,
                    task.name,
                    task.order_no or "-",
                    _operator_display(
                        task.operator_name,
                        task.operator_email,
                    ),
                    task.status.label,
                    task.message,
                )
                for column, value in enumerate(values):
                    existing = self.tasks.item(row_index, column)
                    if (
                        same_task_layout
                        and existing is not None
                        and existing.text() == str(value)
                    ):
                        continue
                    item = _readonly_item(value)
                    if column == 0:
                        item.setData(Qt.ItemDataRole.UserRole, task.task_id)
                    if column == 5 and task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
                        item.setForeground(QColor("#EB3B5A"))
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                    self.tasks.setItem(row_index, column, item)
            self.tasks.setUpdatesEnabled(True)
            _restore_table_scroll_state(self.tasks, scroll_state)


    class CustomOrdersPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._all_rows: list[CustomOrderRow] = []
            self._filtered_rows: list[CustomOrderRow] = []
            self._rows: list[CustomOrderRow] = []
            self._page = 1
            self._page_size = 50
            self._page_count = 1
            self._review_enabled = False
            self._visible_order_nos: frozenset[str] = frozenset()
            self._visible_pending_order_nos_cache: frozenset[str] = frozenset()
            self._checked_order_nos: set[str] = set()
            self._active_order_nos: set[str] = set()
            self._active_task_ids_by_order_no: dict[str, tuple[str, ...]] = {}
            self._active_tasks_by_order_no: dict[str, tuple[TaskRecord, ...]] = {}
            self._active_page_task_ids: tuple[str, ...] = ()
            self._optimistic_waiting_order_nos: set[str] = set()
            self._row_index_by_order_no: dict[str, int] = {}
            self._submission_thread: _ControlResultThread | None = None
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(10)
            heading_row = QHBoxLayout()
            heading_row.setSpacing(8)
            title = QLabel("定制订单")
            title.setObjectName("pageTitle")
            heading_row.addWidget(title)
            heading_row.addStretch(1)
            self.scan_button = QPushButton("立即扫描")
            self.scan_button.clicked.connect(self._scan)
            self.scan_logs_button = QPushButton("打开定制订单扫描日志")
            self.scan_logs_button.clicked.connect(self._open_scan_logs)
            for button in (self.scan_button, self.scan_logs_button):
                size_policy = button.sizePolicy()
                size_policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
                button.setSizePolicy(size_policy)
                heading_row.addWidget(button)
            self._page_action_row_layout = heading_row
            layout.addLayout(heading_row)
            self.scan_schedule_label = QLabel()
            self.scan_schedule_label.setObjectName("queueStatusBanner")
            self.scan_schedule_label.setWordWrap(True)
            layout.addWidget(self.scan_schedule_label)
            self.set_scan_countdown(_CUSTOM_AUTO_SCAN_INTERVAL_MS)

            status_filter_label = QLabel("查看状态")
            status_filter_label.setObjectName("queueFilterLabel")
            self.status_filter_combo = QComboBox()
            self.status_filter_combo.setToolTip("只筛选当前表格，不会修改订单状态")
            self.status_filter_combo.addItem("全部状态", "")
            for status in _CUSTOM_WORKFLOW_STATUS_ORDER:
                self.status_filter_combo.addItem(
                    _custom_workflow_status_label(status),
                    status,
                )
            self.status_filter_combo.currentIndexChanged.connect(
                self._apply_status_filter
            )
            self.status_filter_combo.setMinimumWidth(150)
            self.status_filter_combo.setMaximumWidth(220)
            self.product_type_filter_combo = _ProductTypeFilterCombo()
            self.product_type_filter_combo.setMinimumWidth(180)
            self.product_type_filter_combo.setMaximumWidth(300)
            self.product_type_filter_combo.selection_changed.connect(
                self._apply_status_filter
            )
            self.search_field_combo = QComboBox()
            for value, label in (
                ("platform_order_no", "平台单号"),
                ("system_order_no", "系统单号"),
                ("product_type", "商品类型"),
            ):
                self.search_field_combo.addItem(label, value)
            self.search_field_combo.currentIndexChanged.connect(self._apply_status_filter)
            self.search_field_combo.setMinimumWidth(128)
            self.search_field_combo.setMaximumWidth(160)
            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText("输入完整或部分内容搜索当前队列")
            self.search_edit.setClearButtonEnabled(True)
            self.search_edit.setMinimumWidth(180)
            self.search_edit.setMaximumWidth(520)
            search_size_policy = self.search_edit.sizePolicy()
            search_size_policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            self.search_edit.setSizePolicy(search_size_policy)
            self._search_filter_timer = QTimer(self)
            self._search_filter_timer.setSingleShot(True)
            self._search_filter_timer.setInterval(180)
            self._search_filter_timer.timeout.connect(self._apply_status_filter)
            self.search_edit.textChanged.connect(
                self._schedule_status_filter
            )
            self.quick_select_button = QPushButton("勾选待处理（0）")
            self.quick_select_button.setObjectName("quickSelectButton")
            self.quick_select_button.setToolTip(
                "只勾选当前筛选结果中无报错、无人工复核锁且没有运行任务的待处理订单"
            )
            self.quick_select_button.clicked.connect(self._select_visible_pending_orders)

            filter_panel = QFrame()
            filter_panel.setObjectName("queueFilterPanel")
            filter_grid = QGridLayout(filter_panel)
            filter_grid.setContentsMargins(12, 10, 12, 10)
            filter_grid.setHorizontalSpacing(10)
            filter_grid.setVerticalSpacing(7)
            product_filter_label = QLabel("商品类型")
            product_filter_label.setObjectName("queueFilterLabel")
            search_filter_label = QLabel("搜索订单")
            search_filter_label.setObjectName("queueFilterLabel")
            filter_grid.addWidget(status_filter_label, 0, 0)
            filter_grid.addWidget(self.status_filter_combo, 0, 1)
            filter_grid.addWidget(product_filter_label, 0, 2)
            filter_grid.addWidget(self.product_type_filter_combo, 0, 3)
            filter_grid.addWidget(search_filter_label, 1, 0)
            filter_grid.addWidget(self.search_field_combo, 1, 1)
            filter_grid.addWidget(self.search_edit, 1, 2, 1, 3)
            filter_grid.setColumnStretch(4, 1)
            self._filter_row_layout = filter_grid
            layout.addWidget(filter_panel)
            self.stage_combo = QComboBox()
            for value, label in (
                ("contact", "联系方式"),
                ("folder", "订单文件夹"),
                ("sku", "SKU 调整"),
                ("package_split", "拆包"),
                ("instruction_remark", "说明书备注"),
                ("warehouse_logistics", "仓库物流"),
            ):
                self.stage_combo.addItem(label, value)
            self.stage_state_combo = QComboBox()
            for value, label in (
                ("PENDING", "待处理"),
                ("COMPLETED", "已完成"),
                ("NOT_REQUIRED", "不需要"),
                ("NOT_APPLICABLE", "不适用"),
                ("BLOCKED", "已阻止"),
                (_COMPLETE_ALL_STATE, "全部完成"),
                (_CANCEL_WORKFLOW_STATE, "取消订单"),
            ):
                self.stage_state_combo.addItem(label, value)
            # Keep the two combo models as the single source of truth for tests
            # and controller calls, but expose one compact cascading menu to the
            # operator.  "全部完成" and "取消订单" are workflow-level actions,
            # never states nested under an arbitrary stage.
            self.status_action_button = QPushButton("更多批量操作")
            status_menu = QMenu(self.status_action_button)
            self._status_menu = status_menu
            self._status_change_menu = status_menu.addMenu("修改状态")
            self._status_stage_menus: list[QMenu] = []
            complete_action = self._status_change_menu.addAction("全部完成")
            complete_action.triggered.connect(
                lambda: self._run_status_menu_action("", _COMPLETE_ALL_STATE)
            )
            cancel_action = self._status_change_menu.addAction("取消订单")
            cancel_action.triggered.connect(
                lambda: self._run_status_menu_action("", _CANCEL_WORKFLOW_STATE)
            )
            self._status_change_menu.addSeparator()
            for stage_index in range(self.stage_combo.count()):
                stage = str(self.stage_combo.itemData(stage_index) or "")
                stage_label = self.stage_combo.itemText(stage_index)
                stage_menu = QMenu(stage_label, self._status_change_menu)
                self._status_change_menu.addMenu(stage_menu)
                self._status_stage_menus.append(stage_menu)
                for state_index in range(self.stage_state_combo.count()):
                    state = str(self.stage_state_combo.itemData(state_index) or "")
                    if state in {_COMPLETE_ALL_STATE, _CANCEL_WORKFLOW_STATE}:
                        continue
                    state_label = self.stage_state_combo.itemText(state_index)
                    action = stage_menu.addAction(state_label)
                    action.triggered.connect(
                        lambda _checked=False, selected_stage=stage, selected_state=state:
                        self._run_status_menu_action(selected_stage, selected_state)
                    )
                stage_menu.addSeparator()
                reopen_action = stage_menu.addAction("从此阶段重开")
                reopen_action.triggered.connect(
                    lambda _checked=False, selected_stage=stage:
                    self._run_status_menu_action(selected_stage, "__REOPEN__")
                )
            status_menu.addSeparator()
            self.stop_tasks_action = status_menu.addAction("停止当前勾选任务")
            self.stop_tasks_action.triggered.connect(
                lambda _checked=False: self._stop_checked_tasks()
            )
            self.status_action_button.setMenu(status_menu)

            batch_bar = QFrame()
            batch_bar.setObjectName("queueBatchBar")
            batch_actions = QHBoxLayout(batch_bar)
            batch_actions.setContentsMargins(10, 7, 10, 7)
            batch_actions.setSpacing(8)
            self.custom_selection_summary = QLabel("显示 0 · 可处理 0 · 已选 0")
            self.custom_selection_summary.setObjectName("queueSelectionSummary")
            batch_actions.addWidget(self.custom_selection_summary)
            batch_actions.addStretch(1)
            batch_actions.addWidget(self.quick_select_button)
            batch_actions.addWidget(self.status_action_button)
            self.process_button = QPushButton("处理勾选订单")
            self.process_button.setObjectName("primaryButton")
            self.process_button.clicked.connect(self._process_checked_orders)
            self.process_button.setEnabled(False)
            self.status_action_button.setEnabled(False)
            self.quick_select_button.setEnabled(False)
            batch_actions.addWidget(self.process_button)
            self._batch_action_row_layout = batch_actions
            layout.addWidget(batch_bar)

            self.table = QTableWidget(0, 8)
            self._check_header = _CheckableHeaderView(self.table)
            self.table.setHorizontalHeader(self._check_header)
            self.table.setHorizontalHeaderLabels(
                [
                    "",
                    "平台单号",
                    "系统单号",
                    "商品类型",
                    "工作流阶段",
                    "状态",
                    "状态时间",
                    "处理结果/最后错误",
                ]
            )
            _prepare_table(self.table, full_cell_check_column=0)
            _set_table_default_widths(
                self.table,
                (40, 160, 110, 100, 140, 120, 120, 360),
            )
            self._check_header.check_state_changed.connect(self._set_all_checked)
            self.table.itemChanged.connect(self._on_item_changed)
            layout.addWidget(self.table, 1)
            self.pagination_bar = _QueuePaginationBar()
            self.custom_previous_page_button = self.pagination_bar.previous_button
            self.custom_next_page_button = self.pagination_bar.next_button
            self.custom_page_size_combo = self.pagination_bar.page_size_combo
            self.custom_jump_page_spin = self.pagination_bar.jump_spin
            self.pagination_bar.page_requested.connect(self._show_page)
            self.pagination_bar.page_size_changed.connect(self._change_page_size)
            self.pagination_bar.set_state(
                total=0,
                page=1,
                page_size=self._page_size,
                page_count=1,
            )
            layout.addWidget(self.pagination_bar)

        def _scan(self) -> None:
            command = TaskCommand(
                name="扫描定制订单候选",
                area=TaskArea.CUSTOMIZATION,
                capability=Capability.LIST_ORDERS,
            )
            self.scan_button.setEnabled(False)
            self.scan_button.setText("正在提交扫描…")

            def finish(result: ControlResult) -> None:
                self.scan_button.setEnabled(True)
                self.scan_button.setText("立即扫描")
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def set_scan_countdown(self, milliseconds: int) -> None:
            self.scan_schedule_label.setText(
                "后台自动扫描：每 5 分钟 · "
                f"下次扫描 {_scan_countdown_text(milliseconds)} · "
                "范围：Amazon 待审核订单，仅无自定义标签订单进入定制候选。"
                "已入队订单若在下一轮完整快照中不再是候选，将按平台单号核对订单文件夹："
                "无错误订单存在文件夹则完成、不存在则待处理；"
                "报错、待复核或人工阻止订单保留原状态，必须手动处理。"
            )

        def _open_scan_logs(self) -> None:
            def load() -> tuple[str, str, str]:
                root = self._controller.log_directory()
                title, content = self._controller.scan_log_text("customization")
                return root, title, content

            def show(value: object) -> None:
                root, title, content = value
                path = (
                    Path(root) / scan_audit_directory_name("customization")
                    if root
                    else None
                )
                if path is not None and path.is_dir() and QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(path))
                ):
                    return
                _show_log_viewer(
                    self,
                    title,
                    content,
                    hint=(
                        "共享客户端无法直接打开服务器目录，已改为显示服务器上的"
                        "最近定制订单详细扫描日志。"
                    ),
                )

            _run_value_responsive(
                self,
                self._controller,
                load,
                show,
                lambda error: self._result_handler(
                    ControlResult(False, f"读取扫描日志失败：{type(error).__name__}。")
                ),
            )

        def _process_checked_orders(self) -> None:
            if self._submission_thread is not None:
                return
            rows = self._checked_orders()
            unconfirmed_rows = tuple(
                row
                for row in rows
                if row.platform_order_no in self._optimistic_waiting_order_nos
            )
            if unconfirmed_rows:
                self._clear_checked_orders(
                    tuple(row.platform_order_no for row in unconfirmed_rows)
                )
                rows = [
                    row
                    for row in rows
                    if row.platform_order_no
                    not in self._optimistic_waiting_order_nos
                ]
            if not rows:
                self._result_handler(
                    ControlResult(
                        False,
                        (
                            "所选定制订单正在等待服务器确认，请勿重复提交。"
                            if unconfirmed_rows
                            else "请先勾选至少一张定制订单。"
                        ),
                        details={"non_modal": bool(unconfirmed_rows)},
                    )
                )
                return
            if self._review_enabled and not self._confirm_processing_review(rows):
                return
            selected_rows = tuple(rows)
            selected_order_nos = {
                row.platform_order_no for row in selected_rows
            }
            # Re-sort the complete filtered queue before slicing the first
            # page so newly submitted rows become visible at the front.
            self._optimistic_waiting_order_nos.update(selected_order_nos)
            self._apply_status_filter()
            self.process_button.setEnabled(False)
            self.process_button.setText(f"正在提交 {len(selected_rows)} 张…")

            if (
                getattr(self._controller, "snapshot_runs_in_background", False)
                or getattr(
                    self._controller,
                    "control_calls_run_in_background",
                    False,
                )
            ):
                thread = _ControlResultThread(
                    lambda selected=selected_rows: self._submit_checked_order_batch(selected),
                    self,
                )
                thread.result_ready.connect(self._finish_checked_order_submission)
                thread.finished.connect(thread.deleteLater)
                self._submission_thread = thread
                thread.start()
                return
            self._finish_checked_order_submission(
                self._submit_checked_order_batch(selected_rows)
            )

        def _confirm_processing_review(
            self,
            rows: Sequence[CustomOrderRow],
        ) -> bool:
            preview = "\n".join(
                f"• {row.platform_order_no} · {_product_type_label(row.product_type)}"
                for row in rows[:10]
            )
            if len(rows) > 10:
                preview += f"\n• ……另有 {len(rows) - 10} 张"
            return QMessageBox.question(
                self,
                "审核定制订单",
                f"以下 {len(rows)} 张定制订单即将加入处理队列：\n\n"
                f"{preview}\n\n确认无误并继续处理吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes

        def _submit_checked_order_batch(
            self,
            rows: Sequence[CustomOrderRow],
        ) -> ControlResult:
            accepted_rows: list[CustomOrderRow] = []
            accepted_task_ids_by_order_no: dict[str, str] = {}
            rejected: list[tuple[CustomOrderRow, str]] = []
            unconfirmed: list[tuple[CustomOrderRow, str]] = []
            first_task_id: str | None = None
            browser_failure: tuple[str, int] | None = None
            commands: list[TaskCommand] = []
            for row in rows:
                confirmation = DesktopWriteConfirmation.create(
                    DesktopWriteAction.PROCESS_CUSTOM_ORDER,
                    row.platform_order_no,
                    system_order_no=row.system_order_no,
                    source="qt_checked_action",
                )
                commands.append(
                    TaskCommand(
                        name="处理定制订单",
                        area=TaskArea.CUSTOMIZATION,
                        capability=Capability.UPDATE_CONTACT,
                        order_no=row.platform_order_no,
                        payload={
                            "system_order_no": row.system_order_no,
                            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                        },
                    )
                )
            results = _submit_task_commands(self._controller, commands)
            for index, (row, result) in enumerate(zip(rows, results)):
                if result.accepted:
                    accepted_rows.append(row)
                    first_task_id = first_task_id or result.task_id
                    if result.task_id:
                        accepted_task_ids_by_order_no[row.platform_order_no] = (
                            result.task_id
                        )
                elif bool(result.details.get("submission_outcome_unknown")):
                    unconfirmed.append((row, result.message))
                else:
                    rejected.append((row, result.message))
                    if bool(
                        result.details.get("local_browser_unavailable")
                    ):
                        remaining_rows = tuple(rows[index + 1 :])
                        rejected.extend(
                            (pending_row, result.message)
                            for pending_row in remaining_rows
                        )
                        browser_failure = (
                            result.message,
                            1 + len(remaining_rows),
                        )
                        break

            rejected_preview = "\n".join(
                f"• {row.platform_order_no}：{message}"
                for row, message in rejected[:10]
            )
            rejected_remaining = len(rejected) - 10
            if rejected_remaining > 0:
                rejected_preview += f"\n• ……另有 {rejected_remaining} 张订单未排队"

            if browser_failure is not None:
                browser_message, browser_rejected_count = browser_failure
                if accepted_rows:
                    message = (
                        f"已将 {len(accepted_rows)} 张定制订单加入处理队列；"
                        f"本机专用 Chrome 未就绪，后续 "
                        f"{browser_rejected_count} 张未提交并保持勾选。"
                        f"\n{browser_message}"
                    )
                else:
                    message = (
                        f"所选 {len(rows)} 张定制订单均未排队。"
                        "本机专用 Chrome 未就绪，系统已停止本批次的重复启动；"
                        f"{browser_rejected_count} 张订单保持勾选。"
                        f"\n{browser_message}"
                    )
            elif unconfirmed:
                message_parts: list[str] = []
                if accepted_rows:
                    message_parts.append(
                        f"已确认 {len(accepted_rows)} 张定制订单加入处理队列"
                    )
                if rejected:
                    message_parts.append(
                        f"{len(rejected)} 张明确未排队并保持勾选"
                    )
                pending_prefix = "另有 " if message_parts else ""
                message_parts.append(
                    f"{pending_prefix}{len(unconfirmed)} 张提交请求已发送，"
                    "正在等待服务器确认，请勿重复提交"
                )
                message = "；".join(message_parts) + "。"
                if rejected_preview:
                    message += f"\n{rejected_preview}"
            elif accepted_rows and rejected:
                message = (
                    f"已将 {len(accepted_rows)} 张定制订单加入处理队列；"
                    f"{len(rejected)} 张未排队，仍保持勾选。"
                )
            elif accepted_rows:
                message = f"已将 {len(accepted_rows)} 张定制订单按顺序加入处理队列。"
            else:
                message = f"所选 {len(rows)} 张定制订单均未排队：\n{rejected_preview}"
            return ControlResult(
                bool(accepted_rows),
                message,
                first_task_id,
                details={
                    "accepted_order_nos": tuple(
                        row.platform_order_no for row in accepted_rows
                    ),
                    "accepted_task_ids_by_order_no": tuple(
                        accepted_task_ids_by_order_no.items()
                    ),
                    "rejected_orders": tuple(
                        (row.platform_order_no, reason) for row, reason in rejected
                    ),
                    "unconfirmed_order_nos": tuple(
                        row.platform_order_no for row, _reason in unconfirmed
                    ),
                    "submission_outcome_unknown": bool(unconfirmed),
                    "non_modal": bool(unconfirmed) and not rejected,
                    "local_browser_batch_aborted": browser_failure is not None,
                },
            )

        def _finish_checked_order_submission(self, result: ControlResult) -> None:
            self._submission_thread = None
            self.process_button.setText("处理勾选订单")
            accepted_order_nos = tuple(
                result.details.get("accepted_order_nos") or ()
            )
            self._optimistic_waiting_order_nos.difference_update(
                str(order_no) for order_no in result.details.get("accepted_order_nos", ())
            )
            self._optimistic_waiting_order_nos.difference_update(
                str(order_no)
                for order_no, _reason in result.details.get("rejected_orders", ())
            )
            unconfirmed_order_nos = tuple(
                str(order_no)
                for order_no in result.details.get("unconfirmed_order_nos", ())
            )
            accepted_task_ids_by_order_no = dict(
                result.details.get("accepted_task_ids_by_order_no") or ()
            )
            for order_no, task_id in accepted_task_ids_by_order_no.items():
                self._active_order_nos.add(str(order_no))
                self._active_task_ids_by_order_no[str(order_no)] = (str(task_id),)
            if accepted_order_nos or unconfirmed_order_nos:
                self._clear_checked_orders(
                    (*accepted_order_nos, *unconfirmed_order_nos)
                )
            rejected = tuple(result.details.get("rejected_orders") or ())
            if accepted_order_nos and rejected:
                if bool(
                    result.details.get("local_browser_batch_aborted")
                ):
                    rejected_preview = (
                        "本机专用 Chrome 未就绪，系统已停止本批次的"
                        "重复启动；未提交订单仍保持勾选。"
                    )
                else:
                    rejected_preview = "\n".join(
                        f"• {order_no}：{message}"
                        for order_no, message in rejected[:10]
                    )
                    if len(rejected) > 10:
                        rejected_preview += (
                            f"\n• ……另有 {len(rejected) - 10} 张订单未排队"
                        )
                QMessageBox.warning(
                    self,
                    "部分定制订单未排队",
                    f"已排队 {len(accepted_order_nos)} 张，未排队 {len(rejected)} 张。\n\n"
                    f"{rejected_preview}",
                )
            self._apply_status_filter()
            self._result_handler(result)

        def _selected_order(self) -> CustomOrderRow | None:
            index = self.table.currentRow()
            if index < 0:
                selected = self.table.selectedIndexes()
                index = selected[0].row() if selected else -1
            return self._rows[index] if 0 <= index < len(self._rows) else None

        def _checked_orders(self) -> list[CustomOrderRow]:
            row_indexes = sorted(
                self._row_index_by_order_no[order_no]
                for order_no in self._checked_order_nos
                if order_no in self._row_index_by_order_no
            )
            return [self._rows[row_index] for row_index in row_indexes]

        def _visible_pending_order_nos(self) -> set[str]:
            return set(self._visible_pending_order_nos_cache)

        def _update_quick_select_button(self) -> None:
            count = len(self._visible_pending_order_nos_cache)
            self.quick_select_button.setText(f"勾选待处理（{count}）")
            self.quick_select_button.setEnabled(bool(count))

        def _update_selection_summary(self) -> None:
            selected_count = len(self._visible_order_nos & self._checked_order_nos)
            processable_count = len(self._visible_pending_order_nos_cache)
            self.custom_selection_summary.setText(
                f"显示 {len(self._rows)} · 可处理 {processable_count} · 已选 {selected_count}"
            )
            has_selection = selected_count > 0
            self.status_action_button.setEnabled(has_selection)
            self.process_button.setEnabled(
                has_selection and self._submission_thread is None
            )

        def _select_visible_pending_orders(self) -> None:
            visible_order_nos = self._visible_order_nos
            eligible_order_nos = self._visible_pending_order_nos_cache
            self._checked_order_nos.difference_update(visible_order_nos)
            self._checked_order_nos.update(eligible_order_nos)
            self._refresh_visible_checkboxes()
            self._result_handler(
                ControlResult(
                    bool(eligible_order_nos),
                    (
                        f"已勾选当前筛选结果中的 {len(eligible_order_nos)} 张待处理订单。"
                        if eligible_order_nos
                        else "当前筛选结果中没有可直接处理的定制订单。"
                    ),
                    details={"non_modal": True},
                )
            )

        def _status_value(self, row: CustomOrderRow) -> str:
            active_tasks = self._active_tasks_by_order_no.get(
                row.platform_order_no,
                (),
            )
            if any(
                task.status
                in {
                    TaskStatus.RUNNING,
                    TaskStatus.WAITING_USER,
                    TaskStatus.STOPPING,
                }
                for task in active_tasks
            ):
                return "processing"
            if (
                row.platform_order_no in self._active_order_nos
                or row.platform_order_no in self._optimistic_waiting_order_nos
            ):
                return "waiting"
            return str(row.status_text or row.workflow_stage or "")

        def _status_sort_key(
            self,
            row: CustomOrderRow,
        ) -> tuple[int, int, float, str]:
            status = self._status_value(row)
            if status == "processing":
                work_bucket = 0
            elif status == "waiting":
                work_bucket = 1
            elif status in _CUSTOM_QUICK_SELECT_STATUSES:
                work_bucket = 2
            elif status in {"not_required", "completed"}:
                work_bucket = 4
            elif status in {"cancelled", "已忽略"}:
                work_bucket = 5
            else:
                work_bucket = 3
            return (
                work_bucket,
                _CUSTOM_WORKFLOW_STATUS_PRIORITY.get(
                    status,
                    len(_CUSTOM_WORKFLOW_STATUS_PRIORITY),
                ),
                -_status_timestamp_value(row.status_updated_at),
                row.platform_order_no,
            )

        def _update_status_filter_options(self) -> None:
            existing = {
                str(self.status_filter_combo.itemData(index) or "")
                for index in range(self.status_filter_combo.count())
            }
            unknown_statuses = sorted(
                {
                    self._status_value(row)
                    for row in self._all_rows
                    if self._status_value(row)
                }
                - existing,
                key=str.casefold,
            )
            for status in unknown_statuses:
                self.status_filter_combo.addItem(
                    _custom_workflow_status_label(status),
                    status,
                )

        def _apply_status_filter(self, *_args, reset_page: bool = True) -> None:
            selected_order = self._selected_order()
            selected_order_no = selected_order.platform_order_no if selected_order else ""
            selected_status = str(self.status_filter_combo.currentData() or "")
            search_field = str(self.search_field_combo.currentData() or "platform_order_no")
            search_query = self.search_edit.text()
            selected_product_types = self.product_type_filter_combo.selected_values
            ordered_rows = sorted(self._all_rows, key=self._status_sort_key)
            self._filtered_rows = [
                row
                for row in ordered_rows
                if not selected_status or self._status_value(row) == selected_status
                if _matches_product_type_filter(row, selected_product_types)
                if _queue_row_matches_search(row, search_field, search_query)
            ]
            if reset_page:
                self._page = 1
            self._render_filtered_order_page(selected_order_no=selected_order_no)

        def _render_filtered_order_page(
            self,
            *,
            selected_order_no: str = "",
        ) -> None:
            self._page_count = max(
                1,
                (len(self._filtered_rows) + self._page_size - 1)
                // self._page_size,
            )
            self._page = max(1, min(self._page, self._page_count))
            page_start = (self._page - 1) * self._page_size
            self._rows = self._filtered_rows[
                page_start : page_start + self._page_size
            ]
            self.pagination_bar.set_state(
                total=len(self._filtered_rows),
                page=self._page,
                page_size=self._page_size,
                page_count=self._page_count,
            )
            self._visible_order_nos = frozenset(
                row.platform_order_no for row in self._rows
            )
            self._visible_pending_order_nos_cache = frozenset(
                row.platform_order_no
                for row in self._rows
                if _custom_order_quick_select_eligibility(
                    row,
                    active_order_nos=(
                        self._active_order_nos
                        | self._optimistic_waiting_order_nos
                    ),
                )[0]
            )
            self._checked_order_nos.intersection_update(self._visible_order_nos)
            self._update_quick_select_button()
            self._render_rows(selected_order_no=selected_order_no)

        def _show_page(self, page: int) -> None:
            target = max(1, min(int(page), self._page_count))
            if target == self._page:
                return
            selected = self._selected_order()
            selected_order_no = selected.platform_order_no if selected else ""
            self._page = target
            self._render_filtered_order_page(selected_order_no=selected_order_no)

        def _change_page_size(self, page_size: int) -> None:
            normalized = int(page_size)
            if normalized == self._page_size:
                return
            self._page_size = normalized
            self._page = 1
            self._render_filtered_order_page()

        def _schedule_status_filter(self, *_args) -> None:
            if len(self._all_rows) < 250:
                self._search_filter_timer.stop()
                self._apply_status_filter()
            else:
                self._search_filter_timer.start()

        def _render_rows(self, *, selected_order_no: str = "") -> None:
            selected_row_index = -1
            selected_column = self.table.currentColumn()
            scroll_state = _table_scroll_state(self.table)
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            self._row_index_by_order_no = {}
            try:
                self.table.setRowCount(len(self._rows))
                for row_index, row in enumerate(self._rows):
                    self._row_index_by_order_no[row.platform_order_no] = row_index
                    if row.platform_order_no == selected_order_no:
                        selected_row_index = row_index
                    check_item = QTableWidgetItem()
                    check_item.setFlags(
                        (check_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if row.platform_order_no in self._checked_order_nos
                        else Qt.CheckState.Unchecked
                    )
                    check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    check_item.setData(Qt.ItemDataRole.UserRole, row.platform_order_no)
                    self.table.setItem(row_index, 0, check_item)
                    values = (
                        row.platform_order_no,
                        row.system_order_no,
                        _product_type_label(row.product_type),
                        row.last_error or row.result_detail,
                    )
                    for column, value in enumerate(values[:3], start=1):
                        self.table.setItem(row_index, column, _readonly_item(value))
                    self.table.setItem(
                        row_index,
                        4,
                        _workflow_status_item(row.workflow_stage),
                    )
                    self.table.setItem(
                        row_index,
                        5,
                        _workflow_status_item(
                            self._status_value(row),
                            sort_key=self._status_sort_key(row),
                        ),
                    )
                    self.table.setItem(
                        row_index,
                        6,
                        _readonly_item(_format_status_timestamp(row.status_updated_at)),
                    )
                    active_tasks = self._active_tasks_by_order_no.get(
                        row.platform_order_no,
                        (),
                    )
                    if active_tasks:
                        active_task = max(
                            active_tasks,
                            key=lambda task: task.updated_at,
                        )
                        detail = (
                            f"{active_task.status.label} · "
                            f"{active_task.progress_percent}% · {active_task.message}"
                        )
                    elif row.platform_order_no in self._active_order_nos:
                        detail = "已加入处理队列，等待后台任务更新。"
                    elif row.platform_order_no in self._optimistic_waiting_order_nos:
                        detail = "正在提交本批订单，等待服务器确认排队。"
                    else:
                        detail = values[3]
                    self.table.setItem(row_index, 7, _readonly_item(detail))
                if selected_row_index >= 0:
                    column = min(
                        max(selected_column, 0),
                        max(0, self.table.columnCount() - 1),
                    )
                    self.table.setCurrentCell(selected_row_index, column)
                else:
                    self.table.clearSelection()
                    self.table.setCurrentCell(-1, -1)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            _restore_table_scroll_state(self.table, scroll_state)
            self._sync_check_header()

        def _update_active_order_cells(self, order_nos: set[str]) -> None:
            """Patch changing task progress without rebuilding thousands of cells."""

            if not order_nos:
                return
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for order_no in order_nos:
                    row_index = self._row_index_by_order_no.get(order_no)
                    if row_index is None or not 0 <= row_index < len(self._rows):
                        continue
                    row = self._rows[row_index]
                    if row.platform_order_no != order_no:
                        continue
                    self.table.setItem(
                        row_index,
                        5,
                        _workflow_status_item(
                            self._status_value(row),
                            sort_key=self._status_sort_key(row),
                        ),
                    )
                    active_tasks = self._active_tasks_by_order_no.get(order_no, ())
                    if active_tasks:
                        task = max(active_tasks, key=lambda value: value.updated_at)
                        detail = (
                            f"{task.status.label} · {task.progress_percent}% · "
                            f"{task.message}"
                        )
                    elif order_no in self._active_order_nos:
                        detail = "已加入处理队列，等待后台任务更新。"
                    elif order_no in self._optimistic_waiting_order_nos:
                        detail = "正在提交本批订单，等待服务器确认排队。"
                    else:
                        detail = row.last_error or row.result_detail
                    self.table.setItem(row_index, 7, _readonly_item(detail))
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)

        def _sort_visible_rows_in_place(self) -> None:
            """Move changed statuses without reallocating every table cell."""

            if self.table.rowCount() < 2:
                return
            rows_by_order_no = {
                row.platform_order_no: row for row in self._rows
            }
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                self.table.sortItems(5, Qt.SortOrder.AscendingOrder)
                ordered_rows: list[CustomOrderRow] = []
                row_index_by_order_no: dict[str, int] = {}
                for row_index in range(self.table.rowCount()):
                    check_item = self.table.item(row_index, 0)
                    order_no = str(
                        check_item.data(Qt.ItemDataRole.UserRole)
                        if check_item is not None
                        else ""
                    ).strip()
                    row = rows_by_order_no.get(order_no)
                    if row is None:
                        continue
                    row_index_by_order_no[order_no] = len(ordered_rows)
                    ordered_rows.append(row)
                if len(ordered_rows) == len(self._rows):
                    self._rows = ordered_rows
                    self._row_index_by_order_no = row_index_by_order_no
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)

        def _remove_affected_rows_outside_status_filter(
            self,
            order_nos: set[str],
        ) -> None:
            """Keep an active status filter accurate without rebuilding the table."""

            selected_status = str(self.status_filter_combo.currentData() or "")
            if not selected_status:
                return
            removal_indexes = sorted(
                (
                    row_index
                    for order_no in order_nos
                    if (row_index := self._row_index_by_order_no.get(order_no))
                    is not None
                    and 0 <= row_index < len(self._rows)
                    and self._status_value(self._rows[row_index]) != selected_status
                ),
                reverse=True,
            )
            if not removal_indexes:
                return
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for row_index in removal_indexes:
                    self.table.removeRow(row_index)
                    self._rows.pop(row_index)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)

        def _refresh_visible_row_caches(self) -> None:
            self._row_index_by_order_no = {
                row.platform_order_no: row_index
                for row_index, row in enumerate(self._rows)
            }
            self._visible_order_nos = frozenset(self._row_index_by_order_no)
            self._visible_pending_order_nos_cache = frozenset(
                row.platform_order_no
                for row in self._rows
                if _custom_order_quick_select_eligibility(
                    row,
                    active_order_nos=(
                        self._active_order_nos
                        | self._optimistic_waiting_order_nos
                    ),
                )[0]
            )
            self._checked_order_nos.intersection_update(self._visible_order_nos)
            self._update_quick_select_button()

        def _target_orders(self) -> tuple[list[CustomOrderRow], bool]:
            checked_rows = self._checked_orders()
            if checked_rows:
                return checked_rows, True
            selected_row = self._selected_order()
            return ([selected_row] if selected_row is not None else []), False

        def _on_item_changed(self, item: QTableWidgetItem) -> None:
            if item.column() != 0:
                return
            platform_order_no = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not platform_order_no:
                return
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_order_nos.add(platform_order_no)
            else:
                self._checked_order_nos.discard(platform_order_no)
            self._sync_check_header()

        def _refresh_visible_checkboxes(self) -> None:
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for row_index in range(self.table.rowCount()):
                    item = self.table.item(row_index, 0)
                    if item is None:
                        continue
                    order_no = str(
                        item.data(Qt.ItemDataRole.UserRole) or ""
                    ).strip()
                    desired_state = (
                        Qt.CheckState.Checked
                        if order_no in self._checked_order_nos
                        else Qt.CheckState.Unchecked
                    )
                    if item.checkState() != desired_state:
                        item.setCheckState(desired_state)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()

        def _set_all_checked(self, state_value: int) -> None:
            checked = Qt.CheckState(state_value) == Qt.CheckState.Checked
            visible_order_nos = self._visible_order_nos
            if checked:
                self._checked_order_nos.update(visible_order_nos)
            else:
                self._checked_order_nos.difference_update(visible_order_nos)
            self._refresh_visible_checkboxes()

        def _sync_check_header(self) -> None:
            checked_count = len(self._visible_order_nos & self._checked_order_nos)
            if not checked_count:
                state = Qt.CheckState.Unchecked
            elif checked_count == len(self._visible_order_nos):
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            self._check_header.set_check_state(state)
            self._update_selection_summary()

        def _reason(self, title: str) -> str | None:
            reason, accepted = QInputDialog.getText(
                self,
                title,
                "请输入修改原因（会写入审计历史）：",
            )
            value = reason.strip()
            if not accepted:
                return None
            if not value:
                self._result_handler(ControlResult(False, "修改原因不能为空。"))
                return None
            return value

        def _confirm_local_batch(
            self,
            rows: Sequence[CustomOrderRow],
            *,
            title: str,
            action_text: str,
        ) -> bool:
            preview = "\n".join(f"• {row.platform_order_no}" for row in rows[:10])
            remaining = len(rows) - 10
            if remaining > 0:
                preview += f"\n• ……另有 {remaining} 张订单"
            answer = QMessageBox.question(
                self,
                title,
                f"{action_text}\n\n{preview}\n\n"
                "该操作只修改本地状态，不会请求领星 ERP，也不会修改订单文件。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return answer == QMessageBox.StandardButton.Yes

        def _clear_checked_orders(self, order_nos: Sequence[str]) -> None:
            cleared_order_nos = set(order_nos)
            self._checked_order_nos.difference_update(cleared_order_nos)
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for order_no in cleared_order_nos:
                    row_index = self._row_index_by_order_no.get(order_no)
                    if row_index is None:
                        continue
                    item = self.table.item(row_index, 0)
                    if item is not None:
                        item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()

        def _stop_checked_tasks(self) -> None:
            rows = self._checked_orders()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选至少一张定制订单。"))
                return
            task_ids = list(
                dict.fromkeys(
                    task_id
                    for row in rows
                    for task_id in self._active_task_ids_by_order_no.get(
                        row.platform_order_no,
                        (),
                    )
                )
            )
            if not task_ids:
                self._result_handler(
                    ControlResult(
                        False,
                        "勾选订单没有等待中、运行中或等待确认的后台任务。",
                    )
                )
                return
            affected_order_nos = [
                row.platform_order_no
                for row in rows
                if self._active_task_ids_by_order_no.get(row.platform_order_no)
            ]
            preview = "\n".join(
                f"• {order_no}" for order_no in affected_order_nos[:10]
            )
            if len(affected_order_nos) > 10:
                preview += f"\n• ……另有 {len(affected_order_nos) - 10} 张订单"
            answer = QMessageBox.question(
                self,
                "确认停止当前勾选任务",
                f"即将停止 {len(task_ids)} 个后台任务，涉及 "
                f"{len(affected_order_nos)} 张勾选订单：\n\n{preview}\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止；"
                "不会修改订单的业务状态或工作流阶段。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _stop_all_tasks(self) -> None:
            task_ids = list(self._active_page_task_ids)
            if not task_ids:
                self._result_handler(
                    ControlResult(False, "定制订单页当前没有活动任务。")
                )
                return
            answer = QMessageBox.question(
                self,
                "确认停止本页所有任务",
                f"即将停止定制订单页内全部 {len(task_ids)} 个等待中、"
                "运行中或等待确认的后台任务。\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止；"
                "不会修改订单的业务状态或工作流阶段。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _update_stage_state(self) -> None:
            rows, checked_scope = self._target_orders()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选订单或选择一条定制订单。"))
                return
            reason = self._reason("修改订单阶段状态")
            if reason is None:
                return
            stage = str(self.stage_combo.currentData())
            state = str(self.stage_state_combo.currentData())
            order_nos = [row.platform_order_no for row in rows]
            if state == _COMPLETE_ALL_STATE:
                confirmed = self._confirm_local_batch(
                    rows,
                    title="确认全部完成定制订单",
                    action_text=(
                        f"即将把 {len(rows)} 张定制订单的本地工作流标记为 completed。"
                        "当前阶段选择将被忽略："
                    ),
                )
                if not confirmed:
                    return
                operation = lambda: self._controller.complete_custom_workflows(
                    order_nos,
                    reason=reason,
                )
            elif state == _CANCEL_WORKFLOW_STATE:
                confirmed = self._confirm_local_batch(
                    rows,
                    title="确认取消定制订单",
                    action_text=(
                        f"即将把 {len(rows)} 张定制订单设为“已取消”。"
                        "现有各阶段进度和错误会保留，后续扫描不会自动恢复："
                    ),
                )
                if not confirmed:
                    return
                operation = lambda: self._controller.cancel_custom_workflows(
                    order_nos,
                    reason=reason,
                )
            else:
                if checked_scope and not self._confirm_local_batch(
                    rows,
                    title="确认批量修改订单阶段状态",
                    action_text=(
                        f"即将把 {len(rows)} 张定制订单的“{self.stage_combo.currentText()}”阶段"
                        f"修改为“{self.stage_state_combo.currentText()}”："
                    ),
                ):
                    return
                operation = lambda: self._controller.set_custom_stage_states(
                    order_nos,
                    stage,
                    state,
                    reason=reason,
                )

            def finish(result: ControlResult) -> None:
                if result.accepted and checked_scope:
                    self._clear_checked_orders(order_nos)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                operation,
                finish,
            )

        def _run_status_menu_action(self, stage: str, state: str) -> None:
            if state == "__REOPEN__":
                stage_index = self.stage_combo.findData(stage)
                if stage_index >= 0:
                    self.stage_combo.setCurrentIndex(stage_index)
                self._reopen_stage()
                return
            if stage:
                stage_index = self.stage_combo.findData(stage)
                if stage_index >= 0:
                    self.stage_combo.setCurrentIndex(stage_index)
            state_index = self.stage_state_combo.findData(state)
            if state_index >= 0:
                self.stage_state_combo.setCurrentIndex(state_index)
            self._update_stage_state()

        def _reopen_stage(self) -> None:
            rows, checked_scope = self._target_orders()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选订单或选择一条定制订单。"))
                return
            reason = self._reason("重新打开订单工作流")
            if reason is None:
                return
            if checked_scope and not self._confirm_local_batch(
                rows,
                title="确认批量重新打开订单工作流",
                action_text=(
                    f"即将把 {len(rows)} 张定制订单从“{self.stage_combo.currentText()}”阶段起重新打开："
                ),
            ):
                return
            order_nos = [row.platform_order_no for row in rows]
            stage = str(self.stage_combo.currentData())

            def finish(result: ControlResult) -> None:
                if result.accepted and checked_scope:
                    self._clear_checked_orders(order_nos)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.reopen_custom_workflows(
                    order_nos,
                    stage,
                    reason=reason,
                ),
                finish,
            )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._review_enabled = bool(
                snapshot.settings.custom_order_review_enabled
            )
            previous_status_by_order_no = {
                row.platform_order_no: self._status_value(row)
                for row in self._all_rows
            }
            previous_active_tasks = self._active_tasks_by_order_no
            next_rows = list(snapshot.custom_orders)
            next_active_page_task_ids = tuple(
                task.task_id
                for task in snapshot.tasks
                if task.area is TaskArea.CUSTOMIZATION
                and not task.status.terminal
            )
            active_task_ids_by_order_no: dict[str, list[str]] = {}
            active_tasks_by_order_no: dict[str, list[TaskRecord]] = {}
            for task in snapshot.tasks:
                order_no = str(task.order_no or "").strip()
                if (
                    task.area is TaskArea.CUSTOMIZATION
                    and not task.status.terminal
                    and order_no
                ):
                    active_task_ids_by_order_no.setdefault(order_no, []).append(
                        task.task_id
                    )
                    active_tasks_by_order_no.setdefault(order_no, []).append(task)
            next_active_task_ids_by_order_no = {
                order_no: tuple(task_ids)
                for order_no, task_ids in active_task_ids_by_order_no.items()
            }
            next_active_order_nos = set(next_active_task_ids_by_order_no)
            confirmed_optimistic_order_nos = (
                self._optimistic_waiting_order_nos & next_active_order_nos
            )
            self._optimistic_waiting_order_nos.difference_update(
                confirmed_optimistic_order_nos
            )
            next_active_tasks_by_order_no = {
                order_no: tuple(tasks)
                for order_no, tasks in active_tasks_by_order_no.items()
            }
            rows_changed = next_rows != self._all_rows
            active_changed = (
                next_active_task_ids_by_order_no
                != self._active_task_ids_by_order_no
                or next_active_tasks_by_order_no != self._active_tasks_by_order_no
                or next_active_page_task_ids != self._active_page_task_ids
            )
            if (
                not rows_changed
                and not active_changed
                and not confirmed_optimistic_order_nos
            ):
                return
            self._active_order_nos = next_active_order_nos
            self._active_task_ids_by_order_no = next_active_task_ids_by_order_no
            self._active_tasks_by_order_no = next_active_tasks_by_order_no
            self._active_page_task_ids = next_active_page_task_ids
            if rows_changed:
                self._all_rows = next_rows
                self.product_type_filter_combo.set_available_values(
                    [row.product_type for row in self._all_rows]
                )
            all_order_nos = {row.platform_order_no for row in self._all_rows}
            self._checked_order_nos.intersection_update(all_order_nos)
            if rows_changed:
                self._update_status_filter_options()
            next_status_by_order_no = {
                row.platform_order_no: self._status_value(row)
                for row in self._all_rows
            }
            ordering_changed = rows_changed or any(
                previous_status_by_order_no.get(order_no)
                != next_status_by_order_no.get(order_no)
                for order_no in set(previous_status_by_order_no)
                | set(next_status_by_order_no)
            )
            if ordering_changed:
                self._apply_status_filter(reset_page=False)
            else:
                changed_order_nos = {
                    order_no
                    for order_no in set(previous_active_tasks)
                    | set(next_active_tasks_by_order_no)
                    if previous_active_tasks.get(order_no)
                    != next_active_tasks_by_order_no.get(order_no)
                }
                changed_order_nos.update(confirmed_optimistic_order_nos)
                self._update_active_order_cells(changed_order_nos)


    class _ShipmentStatusDialog(QDialog):
        ACTIONS = (
            ("从查询阿里物流重新开始", "reopen:logistics"),
            ("从设置仓库物流重新开始", "reopen:set_channel"),
            ("从审核发货重新开始", "reopen:audit"),
            ("从填写运单信息重新开始", "reopen:tracking"),
            ("从出库发货重新开始", "reopen:outbound"),
            ("转为标发需人工复核", "manual_review"),
            ("标记为人工已完成（不写 ERP）", "mark_manual_done"),
            ("撤销本界面标记的人工完成", "undo_manual_done"),
            ("恢复已取消任务", "restore_cancelled"),
            ("人工取消订单（永久保留）", "manual_cancel"),
            ("恢复人工取消订单", "restore_manual_cancelled"),
            ("停止当前勾选任务", "cancel"),
            ("恢复为扫描错误并允许后续扫描自动处理", "restore_scan_issue"),
        )

        def __init__(
            self,
            order_count: int,
            parent: QWidget | None = None,
            *,
            allowed_actions: set[str] | frozenset[str] | None = None,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle("修改自动标发状态")
            self.setMinimumWidth(430)
            layout = QVBoxLayout(self)
            hint = QLabel(
                f"将对 {order_count} 条勾选任务执行受控状态操作。请选择目标操作："
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self.action_combo = QComboBox()
            for label, value in self.ACTIONS:
                if allowed_actions is not None and value not in allowed_actions:
                    continue
                self.action_combo.addItem(label, value)
            layout.addWidget(self.action_combo)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def selected_action(self) -> str:
            return str(self.action_combo.currentData() or "")

        def selected_label(self) -> str:
            return self.action_combo.currentText()


    class _ManualShipmentDialog(QDialog):
        def __init__(self, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("手动添加自动标发订单")
            self.setMinimumWidth(520)
            layout = QVBoxLayout(self)
            hint = QLabel(
                "这里只向本地队列添加记录，不会立即写入领星 ERP。后续执行标发仍需单独确认。"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            form = QFormLayout()
            self.system_order_no = QLineEdit()
            self.system_order_no.setPlaceholderText("例如：103710434633847501")
            self.platform_order_no = QLineEdit()
            self.platform_order_no.setPlaceholderText("例如：112-1165824-9982644")
            self.logistics_no = QLineEdit()
            self.logistics_no.setPlaceholderText("例如：ALS01781406025")
            self.reason = QLineEdit()
            self.reason.setPlaceholderText("必填；会写入事件历史")
            form.addRow("系统单号", self.system_order_no)
            form.addRow("平台单号", self.platform_order_no)
            form.addRow("物流单号", self.logistics_no)
            form.addRow("添加原因", self.reason)
            layout.addLayout(form)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("加入队列")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def values(self) -> tuple[str, str, str, str]:
            return (
                self.system_order_no.text().strip(),
                self.platform_order_no.text().strip(),
                self.logistics_no.text().strip(),
                self.reason.text().strip(),
            )


    class _ConfirmedShipmentTrackingDialog(QDialog):
        CARRIERS = (
            "UPS",
            "FedEx",
            "DHL",
            "USPS",
            "GOFO",
            "Yanwen",
            "SpeedX",
            "UniUni",
            "1ST",
            "SwiftX",
            "Canada Post",
            "Aramex",
        )

        def __init__(
            self,
            row: ShipmentRow,
            parent: QWidget | None = None,
            *,
            execute_after_save: bool = True,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle(
                "人工核对物流并放行"
                if execute_after_save
                else "修改物流单号和承运商"
            )
            self.setMinimumWidth(520)
            layout = QVBoxLayout(self)
            hint = QLabel(
                f"平台单号：{row.platform_order_no}\n"
                + (
                    "请人工填写并核对承运商和运单号。保存后将使用这一精确组合，"
                    "立即执行 ERP 标发，并在标发完成后发送客户通知。"
                    if execute_after_save
                    else "请填写或更正从物流客服确认到的真实尾程承运商和物流单号。"
                    "此功能不限制物流单号前缀，保存时仍会执行自动标发安全校验。"
                )
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            form = QFormLayout()
            self.carrier_combo = QComboBox()
            for carrier in self.CARRIERS:
                self.carrier_combo.addItem(carrier, carrier)
            current_carrier = str(row.carrier or "").strip().casefold()
            for index, carrier in enumerate(self.CARRIERS):
                if carrier.casefold() == current_carrier:
                    self.carrier_combo.setCurrentIndex(index)
                    break
            self.tracking_edit = QLineEdit(str(row.international_tracking_no or "").strip())
            self.tracking_edit.setClearButtonEnabled(True)
            self.tracking_edit.setPlaceholderText("国际物流单号")
            form.addRow("承运商", self.carrier_combo)
            form.addRow(
                "运单号" if execute_after_save else "物流单号",
                self.tracking_edit,
            )
            layout.addLayout(form)
            warning = QLabel(
                (
                    "这不是仅修改队列状态：该操作会写入 ERP，并会调用邮件或短信供应商；"
                    "只有已人工核对的订单才能放行。"
                    if execute_after_save
                    else "保存后只更新自动标发队列，并将校验通过的订单转为可标发；"
                    "不会立即写入 ERP，也不会发送客户通知。"
                )
            )
            warning.setObjectName("warningText")
            warning.setWordWrap(True)
            layout.addWidget(warning)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
                "保存物流并执行" if execute_after_save else "保存修改"
            )
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def values(self) -> tuple[str, str]:
            return (
                str(self.carrier_combo.currentData() or "").strip(),
                self.tracking_edit.text().strip(),
            )


    class AlibabaOrderPage(QWidget):
        """Two-stage operator flow: manual quote selection, then safe draft fill."""

        def __init__(
            self,
            controller: BackgroundTaskController,
            result_handler: ResultHandler,
        ) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._last_signature: object | None = None
            self._active_task_ids: tuple[str, ...] = ()
            self._quote_postal_code = ""

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(14)
            title = QLabel("阿里物流下单")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            explanation = QLabel(
                "当前版本只处理帐篷类订单。程序可按领星系统单号或平台单号读取订单、"
                "SKU 和完整收货地址；第一步只打开阿里查价页并在本页显示查价资料，"
                "不会自动选择或填写阿里页面任何字段。"
                "进入草稿后，程序填写地址、申报资料和签收服务，但不会点击最终下单。"
            )
            explanation.setWordWrap(True)
            explanation.setObjectName("sectionHint")
            layout.addWidget(explanation)

            form_frame = QFrame()
            form_frame.setObjectName("panel")
            form = QFormLayout(form_frame)
            form.setContentsMargins(18, 18, 18, 18)
            form.setSpacing(12)

            self.system_order_edit = QLineEdit()
            self.system_order_edit.setPlaceholderText("请输入领星系统单号或平台单号")
            self.system_order_edit.setClearButtonEnabled(True)
            self.system_order_edit.textChanged.connect(self._clear_quote_details)
            form.addRow("订单号", self.system_order_edit)

            self.expedited_checkbox = QCheckBox("加急订单")
            self.expedited_checkbox.setToolTip(
                "加急订单必须选择名称含 Expedited/加急的线路；是否需要签收服务请单独选择。"
            )
            self.signature_checkbox = QCheckBox("需要签收服务")
            self.signature_checkbox.setToolTip(
                "仅在本单确实需要签收服务时勾选；此选项与是否加急互不影响。"
            )
            self.heavy_checkbox = QCheckBox("含支架 / 按重量申报")
            self.heavy_checkbox.setToolTip(
                "美国订单勾选后：普通线路按重量×0.2，加急线路按重量×0.4。"
            )
            flags = QHBoxLayout()
            flags.addWidget(self.expedited_checkbox)
            flags.addWidget(self.signature_checkbox)
            flags.addWidget(self.heavy_checkbox)
            flags.addStretch(1)
            form.addRow("订单规则", flags)

            declaration_hint = QLabel(
                "申报价自动规则：美国普通线路 USD 2.5；DDP 线路固定 USD 800；"
                "美国含支架订单按重量计算；加拿大按 15kg 分界使用 USD 13/99。"
            )
            declaration_hint.setWordWrap(True)
            declaration_hint.setObjectName("sectionHint")
            form.addRow("申报价", declaration_hint)
            layout.addWidget(form_frame)

            self.quote_info_frame = QFrame()
            self.quote_info_frame.setObjectName("panel")
            quote_info = QGridLayout(self.quote_info_frame)
            quote_info.setContentsMargins(18, 16, 18, 16)
            quote_info.setHorizontalSpacing(18)
            quote_info.setVerticalSpacing(10)
            quote_title = QLabel("本次查价资料")
            quote_title.setStyleSheet("font-weight: 700; color: #101828;")
            quote_info.addWidget(quote_title, 0, 0, 1, 3)
            self.quote_order_label = QLabel("-")
            self.quote_origin_label = QLabel("-")
            self.quote_destination_label = QLabel("-")
            self.quote_postal_label = QLabel("-")
            self.quote_postal_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.quote_postal_label.setStyleSheet(
                "font-size: 16px; font-weight: 700; color: #175CD3;"
            )
            for row_index, (label, widget) in enumerate(
                (
                    ("订单", self.quote_order_label),
                    ("发货地", self.quote_origin_label),
                    ("目的国家", self.quote_destination_label),
                    ("目的邮编", self.quote_postal_label),
                ),
                start=1,
            ):
                name = QLabel(label)
                name.setStyleSheet("color: #667085;")
                quote_info.addWidget(name, row_index, 0)
                quote_info.addWidget(widget, row_index, 1)
            self.copy_postal_button = QPushButton("复制邮编")
            self.copy_postal_button.setEnabled(False)
            self.copy_postal_button.clicked.connect(self._copy_postal_code)
            quote_info.addWidget(self.copy_postal_button, 4, 2)
            quote_hint = QLabel(
                "请在阿里页面人工选择发货地和目的国家，"
                "再复制邮编粘贴到目的地输入框。"
            )
            quote_hint.setWordWrap(True)
            quote_hint.setObjectName("sectionHint")
            quote_info.addWidget(quote_hint, 5, 0, 1, 3)
            quote_info.setColumnStretch(1, 1)
            self.quote_info_frame.setVisible(False)
            layout.addWidget(self.quote_info_frame)

            button_row = QHBoxLayout()
            self.prepare_button = QPushButton("1. 读取订单并打开阿里查价")
            self.prepare_button.setObjectName("primaryButton")
            self.prepare_button.clicked.connect(self._prepare)
            self.fill_button = QPushButton("2. 填写当前阿里下单草稿")
            self.fill_button.setObjectName("primaryButton")
            self.fill_button.clicked.connect(self._fill_draft)
            button_row.addWidget(self.prepare_button)
            button_row.addWidget(self.fill_button)
            button_row.addStretch(1)
            layout.addLayout(button_row)

            steps = QLabel(
                "操作顺序：① 输入领星系统单号或平台单号并打开查价页；"
                "② 按本页资料在阿里人工选择发货地、目的国家并粘贴邮编；"
                "③ 填写包裹尺寸/重量、选择线路并点击“普通下单”；"
                "④ 回到这里确认选项并填写草稿；"
                "⑤ 在阿里页面最终核对并由人工提交。"
            )
            steps.setWordWrap(True)
            steps.setStyleSheet(
                "background: #F8FAFC; color: #344054; border: 1px solid #EAECF0;"
                "border-radius: 8px; padding: 12px;"
            )
            layout.addWidget(steps)

            self.status_label = QLabel("尚未开始")
            self.status_label.setWordWrap(True)
            self.status_label.setStyleSheet(
                "background: #EFF8FF; color: #175CD3; border: 1px solid #B2DDFF;"
                "border-radius: 8px; padding: 12px;"
            )
            layout.addWidget(self.status_label)
            layout.addStretch(1)

        def _order_identifier(self) -> str:
            return self.system_order_edit.text().strip()

        def _clear_quote_details(self, _text: str = "") -> None:
            self._quote_postal_code = ""
            self.quote_order_label.setText("-")
            self.quote_origin_label.setText("-")
            self.quote_destination_label.setText("-")
            self.quote_postal_label.setText("-")
            self.copy_postal_button.setText("复制邮编")
            self.copy_postal_button.setEnabled(False)
            self.quote_info_frame.setVisible(False)

        def apply_quote_details(self, request: DesktopInteractionRequest) -> bool:
            if request.stage != "alibaba_order:quote_details":
                return False
            values = request.display_data
            requested_order_no = str(values.get("requested_order_no") or "").strip()
            if not requested_order_no or requested_order_no != self._order_identifier():
                return False
            postal_code = str(values.get("destination_postal_code") or "").strip()
            country_code = str(values.get("destination_country_code") or "").strip().upper()
            if not postal_code or not country_code:
                return False
            country_name = {
                "US": "美国",
                "CA": "加拿大",
            }.get(
                country_code,
                str(values.get("destination_country_name") or country_code).strip(),
            )
            system_order_no = str(values.get("system_order_no") or "").strip()
            platform_order_no = str(values.get("platform_order_no") or "").strip()
            order_parts = [
                value
                for value in (
                    f"系统单号 {system_order_no}" if system_order_no else "",
                    f"平台单号 {platform_order_no}" if platform_order_no else "",
                )
                if value
            ]
            self._quote_postal_code = postal_code
            self.quote_order_label.setText("  ·  ".join(order_parts) or requested_order_no)
            self.quote_origin_label.setText(
                f"{str(values.get('origin_country') or '中国大陆').strip()} / "
                f"{str(values.get('origin_city') or '佛山市').strip()}"
            )
            self.quote_destination_label.setText(f"{country_name}（{country_code}）")
            self.quote_postal_label.setText(postal_code)
            self.copy_postal_button.setEnabled(True)
            self.quote_info_frame.setVisible(True)
            return True

        def _copy_postal_code(self) -> None:
            postal_code = self._quote_postal_code
            if not postal_code:
                return
            QApplication.clipboard().setText(postal_code)
            self.copy_postal_button.setText("已复制")

            def restore_copy_label() -> None:
                if self._quote_postal_code == postal_code:
                    self.copy_postal_button.setText("复制邮编")

            QTimer.singleShot(1500, restore_copy_label)

        def _prepare(self) -> None:
            order_identifier = self._order_identifier()
            if not order_identifier:
                QMessageBox.warning(
                    self,
                    "缺少订单号",
                    "请先输入领星系统单号或平台单号。",
                )
                return
            self._clear_quote_details()
            command = TaskCommand(
                name=f"准备阿里物流下单 {order_identifier}",
                area=TaskArea.SHIPMENT,
                capability=Capability.ALIBABA_ORDER_PREPARE,
                order_no=order_identifier,
            )
            self.prepare_button.setEnabled(False)

            def finish(result: ControlResult) -> None:
                if not result.accepted:
                    self.prepare_button.setEnabled(True)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _fill_draft(self) -> None:
            order_identifier = self._order_identifier()
            if not order_identifier:
                QMessageBox.warning(
                    self,
                    "缺少订单号",
                    "请先输入领星系统单号或平台单号。",
                )
                return
            answer = QMessageBox.question(
                self,
                "确认填写阿里草稿",
                (
                    f"订单号：{order_identifier}\n"
                    f"加急订单：{'是' if self.expedited_checkbox.isChecked() else '否'}\n"
                    f"需要签收：{'是' if self.signature_checkbox.isChecked() else '否'}\n"
                    f"含支架/按重量申报：{'是' if self.heavy_checkbox.isChecked() else '否'}\n"
                    "DDP 线路：自动申报 USD 800\n\n"
                    "程序将重新读取领星订单，并填写当前阿里草稿；"
                    "不会点击最终下单。是否继续？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirmation = DesktopWriteConfirmation.create(
                DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT,
                order_identifier,
                system_order_no=order_identifier,
            )
            command = TaskCommand(
                name=f"填写阿里物流草稿 {order_identifier}",
                area=TaskArea.SHIPMENT,
                capability=Capability.ALIBABA_ORDER_DRAFT,
                order_no=order_identifier,
                payload={
                    "expedited": self.expedited_checkbox.isChecked(),
                    "signature_requested": self.signature_checkbox.isChecked(),
                    "heavy_or_frame": self.heavy_checkbox.isChecked(),
                    DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                },
            )
            self.fill_button.setEnabled(False)

            def finish(result: ControlResult) -> None:
                if not result.accepted:
                    self.fill_button.setEnabled(True)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _stop_all_tasks(self) -> None:
            task_ids = list(self._active_task_ids)
            if not task_ids:
                self._result_handler(
                    ControlResult(False, "阿里物流下单页当前没有活动任务。")
                )
                return
            answer = QMessageBox.question(
                self,
                "确认停止本页所有任务",
                f"即将停止阿里物流下单页内全部 {len(task_ids)} 个等待中、"
                "运行中或等待确认的任务。\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止；"
                "不会点击阿里页面的最终下单。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            order_identifier = self._order_identifier()
            active_tasks = [
                task
                for task in snapshot.tasks
                if task.capability
                in {
                    Capability.ALIBABA_ORDER_PREPARE,
                    Capability.ALIBABA_ORDER_DRAFT,
                }
                and not task.status.terminal
            ]
            self._active_task_ids = tuple(task.task_id for task in active_tasks)
            relevant = [
                task
                for task in snapshot.tasks
                if task.capability
                in {
                    Capability.ALIBABA_ORDER_PREPARE,
                    Capability.ALIBABA_ORDER_DRAFT,
                }
                and (not order_identifier or task.order_no == order_identifier)
            ]
            task = max(relevant, key=lambda item: item.updated_at) if relevant else None
            signature = (
                task.task_id,
                task.status,
                task.message,
                task.progress_percent,
            ) if task is not None else None
            if signature == self._last_signature:
                return
            self._last_signature = signature
            if task is None:
                self.status_label.setText("尚未开始")
                self.prepare_button.setEnabled(True)
                self.fill_button.setEnabled(True)
                return
            self.status_label.setText(
                f"{task.status.label} · {task.progress_percent}%\n{task.message}"
            )
            active = not task.status.terminal
            self.prepare_button.setEnabled(not active)
            self.fill_button.setEnabled(not active)


    class ShipmentPage(QWidget):
        def __init__(
            self,
            controller: BackgroundTaskController,
            result_handler: ResultHandler,
            batch_handler: ShipmentBatchHandler | None = None,
            scan_handler: ShipmentScanHandler | None = None,
        ) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._batch_handler = batch_handler
            self._scan_handler = scan_handler
            self._all_rows: list[ShipmentRow] = []
            self._filtered_rows: list[ShipmentRow] = []
            self._rows: list[ShipmentRow] = []
            self._page = 1
            self._page_size = 50
            self._page_count = 1
            self._review_enabled = False
            self._visible_logistics_nos: frozenset[str] = frozenset()
            self._visible_scan_issue_keys: frozenset[str] = frozenset()
            self._visible_ready_logistics_nos_cache: frozenset[str] = frozenset()
            self._ready_shipment_count = 0
            self._checked_logistics_nos: set[str] = set()
            self._checked_scan_issue_keys: set[str] = set()
            self._active_logistics_nos: set[str] = set()
            self._optimistic_waiting_logistics_nos: set[str] = set()
            self._unconfirmed_logistics_nos: set[str] = set()
            self._active_task_ids_by_logistics_no: dict[str, tuple[str, ...]] = {}
            self._active_tasks_by_logistics_no: dict[
                str,
                tuple[TaskRecord, ...],
            ] = {}
            self._active_page_task_ids: tuple[str, ...] = ()
            self._row_index_by_logistics_no: dict[str, int] = {}
            self._row_index_by_selection_key: dict[str, int] = {}
            self._submission_thread: _ControlResultThread | None = None
            self._submission_in_progress = False
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(10)

            heading_row = QHBoxLayout()
            heading_row.setSpacing(8)
            title = QLabel("自动标发")
            title.setObjectName("pageTitle")
            heading_row.addWidget(title)
            heading_row.addStretch(1)

            self.scan_button = QPushButton("扫描并查询物流")
            self.scan_button.clicked.connect(self._scan)
            self.logistics_button = QPushButton("重新查询物流状态")
            self.logistics_button.clicked.connect(self._query_logistics)
            self.scan_logs_button = QPushButton("打开自动标发扫描日志")
            self.scan_logs_button.clicked.connect(self._open_scan_logs)
            for button in (
                self.scan_button,
                self.logistics_button,
                self.scan_logs_button,
            ):
                size_policy = button.sizePolicy()
                size_policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
                button.setSizePolicy(size_policy)
                heading_row.addWidget(button)
            self._page_action_row_layout = heading_row
            layout.addLayout(heading_row)

            self.scan_schedule_label = QLabel()
            self.scan_schedule_label.setObjectName("queueStatusBanner")
            self.scan_schedule_label.setWordWrap(True)
            layout.addWidget(self.scan_schedule_label)
            self.set_scan_countdown(_SHIPMENT_AUTO_SCAN_INTERVAL_MS)

            filter_panel = QFrame()
            filter_panel.setObjectName("queueFilterPanel")
            filter_grid = QGridLayout(filter_panel)
            filter_grid.setContentsMargins(12, 10, 12, 10)
            filter_grid.setHorizontalSpacing(10)
            filter_grid.setVerticalSpacing(7)

            status_filter_label = QLabel("处理状态")
            status_filter_label.setObjectName("queueFilterLabel")
            self.search_field_combo = QComboBox()
            for value, label in (
                ("platform_order_no", "平台单号"),
                ("system_order_no", "系统单号"),
            ):
                self.search_field_combo.addItem(label, value)
            self.search_field_combo.setMinimumWidth(128)
            self.search_field_combo.setMaximumWidth(160)
            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText("输入完整或部分内容搜索自动标发队列")
            self.search_edit.setClearButtonEnabled(True)
            self.search_edit.setMinimumWidth(180)
            self.search_edit.setMaximumWidth(520)
            search_size_policy = self.search_edit.sizePolicy()
            search_size_policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            self.search_edit.setSizePolicy(search_size_policy)
            self.search_field_combo.currentIndexChanged.connect(self._apply_search_filter)
            self._search_filter_timer = QTimer(self)
            self._search_filter_timer.setSingleShot(True)
            self._search_filter_timer.setInterval(180)
            self._search_filter_timer.timeout.connect(self._apply_search_filter)
            self.search_edit.textChanged.connect(
                self._schedule_search_filter
            )
            self.status_filter_combo = QComboBox()
            self.status_filter_combo.addItem("全部状态", "")
            for status_label in _SHIPMENT_STATUS_LABELS:
                self.status_filter_combo.addItem(status_label, status_label)
            self.status_filter_combo.setMinimumWidth(150)
            self.status_filter_combo.setMaximumWidth(220)
            self.status_filter_combo.currentIndexChanged.connect(self._apply_search_filter)
            self.product_type_filter_combo = _ProductTypeFilterCombo()
            self.product_type_filter_combo.setMinimumWidth(180)
            self.product_type_filter_combo.setMaximumWidth(300)
            self.product_type_filter_combo.selection_changed.connect(
                self._apply_search_filter
            )

            product_filter_label = QLabel("商品类型")
            product_filter_label.setObjectName("queueFilterLabel")
            search_filter_label = QLabel("搜索订单")
            search_filter_label.setObjectName("queueFilterLabel")
            filter_grid.addWidget(status_filter_label, 0, 0)
            filter_grid.addWidget(self.status_filter_combo, 0, 1)
            filter_grid.addWidget(
                product_filter_label,
                0,
                2,
                alignment=(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                ),
            )
            filter_grid.addWidget(self.product_type_filter_combo, 0, 3)
            filter_grid.addWidget(search_filter_label, 1, 0)
            filter_grid.addWidget(self.search_field_combo, 1, 1)
            filter_grid.addWidget(self.search_edit, 1, 2, 1, 3)
            filter_grid.setColumnStretch(4, 1)
            self._filter_row_layout = filter_grid
            layout.addWidget(filter_panel)

            batch_bar = QFrame()
            batch_bar.setObjectName("queueBatchBar")
            batch_actions = QHBoxLayout(batch_bar)
            batch_actions.setContentsMargins(10, 7, 10, 7)
            batch_actions.setSpacing(8)
            self.ready_count_label = QLabel("显示 0 · 可标发 0 · 已选 0")
            self.ready_count_label.setObjectName("queueSelectionSummary")
            batch_actions.addWidget(self.ready_count_label)
            batch_actions.addStretch(1)

            self.quick_select_button = QPushButton("一键勾选可标发（0）")
            self.quick_select_button.setObjectName("quickSelectButton")
            self.quick_select_button.setToolTip(
                "只勾选当前筛选结果中物流资料校验通过且当前可以提交的订单"
            )
            self.quick_select_button.clicked.connect(self._select_visible_ready_shipments)
            batch_actions.addWidget(self.quick_select_button)

            self.more_actions_button = QPushButton("更多批量操作")
            self.more_actions_menu = QMenu(self.more_actions_button)
            self.edit_tracking_action = self.more_actions_menu.addAction(
                "修改物流单号和承运商"
            )
            self.edit_tracking_action.triggered.connect(
                lambda _checked=False: self._edit_selected_tracking_pair()
            )
            self.confirm_execute_action = self.more_actions_menu.addAction(
                "人工核对物流并放行"
            )
            self.confirm_execute_action.triggered.connect(
                lambda _checked=False: self._confirm_and_execute()
            )
            self.change_status_action = self.more_actions_menu.addAction("修改状态")
            self.change_status_action.triggered.connect(
                lambda _checked=False: self._change_selected_status()
            )
            self.retry_actions_menu = self.more_actions_menu.addMenu("重试阶段")
            self.retry_logistics_action = self.retry_actions_menu.addAction(
                "重试物流阶段"
            )
            self.retry_logistics_action.triggered.connect(
                lambda _checked=False: self._retry_selected_stage("logistics")
            )
            self.retry_erp_action = self.retry_actions_menu.addAction(
                "核验 ERP 状态并安全继续"
            )
            self.retry_erp_action.triggered.connect(
                lambda _checked=False: self._retry_selected_stage("erp")
            )
            self.more_actions_menu.addSeparator()
            self.stop_tasks_action = self.more_actions_menu.addAction(
                "停止当前勾选任务"
            )
            self.stop_tasks_action.triggered.connect(
                lambda _checked=False: self._stop_checked_tasks()
            )
            self.more_actions_button.setMenu(self.more_actions_menu)
            batch_actions.addWidget(self.more_actions_button)

            self.execute_button = QPushButton("执行标发（0）")
            self.execute_button.setObjectName("primaryButton")
            self.execute_button.clicked.connect(self._execute_selected)
            batch_actions.addWidget(self.execute_button)
            self._batch_action_row_layout = batch_actions
            layout.addWidget(batch_bar)

            self.table = QTableWidget(0, 13)
            self._check_header = _CheckableHeaderView(self.table)
            self.table.setHorizontalHeader(self._check_header)
            self.table.setHorizontalHeaderLabels(
                [
                    "",
                    "平台单号",
                    "系统单号",
                    "商品类型",
                    "阿里物流单号",
                    "国际物流单号",
                    "承运商",
                    "处理状态",
                    "标发进度",
                    "状态时间",
                    "阿里查询时间",
                    "状态说明",
                    "逾期记录",
                ]
            )
            _prepare_table(self.table, full_cell_check_column=0)
            _set_table_default_widths(
                self.table,
                _SHIPMENT_TABLE_DEFAULT_WIDTHS,
                unscaled_columns=(12,),
            )
            self._check_header.check_state_changed.connect(self._set_all_checked)
            self.table.itemChanged.connect(self._on_item_changed)
            layout.addWidget(self.table, 1)
            self.pagination_bar = _QueuePaginationBar()
            self.shipment_previous_page_button = (
                self.pagination_bar.previous_button
            )
            self.shipment_next_page_button = self.pagination_bar.next_button
            self.shipment_page_size_combo = self.pagination_bar.page_size_combo
            self.shipment_jump_page_spin = self.pagination_bar.jump_spin
            self.pagination_bar.page_requested.connect(self._show_page)
            self.pagination_bar.page_size_changed.connect(self._change_page_size)
            self.pagination_bar.set_state(
                total=0,
                page=1,
                page_size=self._page_size,
                page_count=1,
            )
            layout.addWidget(self.pagination_bar)
            self._update_quick_select_button()
            self._update_selection_summary()

        def _scan(self) -> None:
            command = TaskCommand(
                name="扫描候选并查询物流",
                area=TaskArea.SHIPMENT,
                capability=Capability.LIST_ORDERS,
                payload={
                    "trigger": "manual_button",
                    _LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY: True,
                },
            )
            self.scan_button.setEnabled(False)
            self.scan_button.setText("正在提交扫描…")

            def finish(result: ControlResult) -> None:
                self.scan_button.setEnabled(True)
                self.scan_button.setText("扫描并查询物流")
                if result.accepted and result.task_id and self._scan_handler is not None:
                    self._scan_handler(result.task_id)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def set_scan_countdown(self, milliseconds: int) -> None:
            self.scan_schedule_label.setText(
                "● 每 3 小时自动扫描 · "
                f"下次 {_scan_countdown_text(milliseconds)} · "
                "服务器扫描领星待审核订单，本机负责物流查询"
            )
            self.scan_schedule_label.setToolTip(
                "服务器扫描领星待审核订单；在线客户端使用本机可见 Chrome 查询"
                "阿里国际站物流，校验通过后进入“可标发”。遇到登录或安全验证时"
                "请在 Chrome 中人工处理；没有在线客户端时物流记录保持待查询，"
                "不会写入 ERP。"
            )

        def _open_scan_logs(self) -> None:
            def load() -> tuple[str, str, str]:
                root = self._controller.log_directory()
                title, content = self._controller.scan_log_text("shipment")
                return root, title, content

            def show(value: object) -> None:
                root, title, content = value
                path = (
                    Path(root) / scan_audit_directory_name("shipment")
                    if root
                    else None
                )
                if path is not None and path.is_dir() and QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(path))
                ):
                    return
                _show_log_viewer(
                    self,
                    title,
                    content,
                    hint=(
                        "共享客户端无法直接打开服务器目录，已改为显示服务器上的"
                        "最近自动标发详细扫描日志。"
                    ),
                )

            _run_value_responsive(
                self,
                self._controller,
                load,
                show,
                lambda error: self._result_handler(
                    ControlResult(False, f"读取扫描日志失败：{type(error).__name__}。")
                ),
            )

        def _add_manual_order(self) -> None:
            dialog = _ManualShipmentDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            system_order_no, platform_order_no, logistics_no, reason = dialog.values()
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.add_shipment_order(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    logistics_no=logistics_no,
                    reason=reason,
                ),
                self._result_handler,
            )

        def _change_selected_status(self) -> None:
            rows = self._checked_shipment_rows()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选至少一条自动标发任务。"))
                return
            has_scan_issues = any(row.scan_issue_code for row in rows)
            has_queue_rows = any(not row.scan_issue_code for row in rows)
            scan_actions = {
                "manual_review",
                "mark_manual_done",
                "undo_manual_done",
                "manual_cancel",
                "restore_manual_cancelled",
                "restore_scan_issue",
            }
            allowed_actions = None
            if has_scan_issues:
                allowed_actions = (
                    scan_actions - {"restore_scan_issue"}
                    if has_queue_rows
                    else scan_actions
                )
            dialog = _ShipmentStatusDialog(
                len(rows),
                self,
                allowed_actions=allowed_actions,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            action = dialog.selected_action()
            action_label = dialog.selected_label()
            reason = self._reason("修改自动标发状态")
            if reason is None:
                return
            preview = "\n".join(f"• {row.platform_order_no}" for row in rows[:10])
            if len(rows) > 10:
                preview += f"\n• ……另有 {len(rows) - 10} 张"
            if action.startswith("reopen:"):
                warning = (
                    "该操作只重置本地续作检查点，不会撤销 ERP 中已经完成的出库。\n"
                    "目标阶段之前的步骤将被视为已完成并跳过。请先核对 ERP 的真实状态。\n"
                    "重新执行时仍会逐阶段弹窗并进行 ERP 读回；如果 ERP 仍为已出库，"
                    "系统会恢复为已完成而不会重复写入。"
                )
            else:
                warning = "该操作只修改本地管理状态，不会立即向 ERP 发送请求。"
                if has_scan_issues:
                    warning += (
                        " 扫描错误仍会保留原始错误和操作历史，且始终不能直接执行标发。"
                    )
            answer = QMessageBox.question(
                self,
                "确认修改勾选任务状态",
                f"即将对 {len(rows)} 条任务执行“{action_label}”：\n\n{preview}\n\n"
                f"原因：{reason}\n\n{warning}\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            selection_keys = [_shipment_selection_key(row) for row in rows]
            if action.startswith("reopen:"):
                stage = action.split(":", 1)[1]
                operation = lambda: self._controller.reopen_shipments_from_stage(
                    selection_keys, stage, reason=reason
                )
            else:
                operation = lambda: self._controller.change_shipment_statuses(
                    selection_keys,
                    action,
                    reason=reason,
                )
            display_by_key = {
                _shipment_selection_key(row): row.platform_order_no for row in rows
            }

            def finish(result: ControlResult) -> None:
                changed = tuple(
                    result.details.get("changed_logistics_nos") or ()
                )
                if changed:
                    self._clear_checked_shipments(changed)
                skipped = dict(result.details.get("skipped_reasons") or {})
                if skipped:
                    detail = "\n".join(
                        f"• {display_by_key.get(identifier, identifier)}：{message}"
                        for identifier, message in list(skipped.items())[:10]
                    )
                    if len(skipped) > 10:
                        detail += f"\n• ……另有 {len(skipped) - 10} 条"
                    QMessageBox.warning(
                        self,
                        "部分任务未修改",
                        f"以下任务保留勾选：\n\n{detail}",
                    )
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                operation,
                finish,
            )

        def _execute_selected(self) -> None:
            rows = self._checked_shipment_rows()
            if not rows:
                self._result_handler(
                    ControlResult(
                        False,
                        "请先勾选至少一条自动标发订单。",
                        details={"non_modal": True},
                    )
                )
                return
            active_logistics_nos = (
                set(self._active_logistics_nos)
                | self._optimistic_waiting_logistics_nos
            )
            eligible_rows: list[ShipmentRow] = []
            skipped: list[tuple[ShipmentRow, str]] = []
            for row in rows:
                eligible, reason = _shipment_execution_eligibility(
                    row,
                    active_logistics_nos=active_logistics_nos,
                )
                if eligible:
                    eligible_rows.append(row)
                else:
                    skipped.append((row, reason))
            self._review_and_submit_shipment_rows(eligible_rows, skipped=skipped)

        def _review_and_submit_shipment_rows(
            self,
            eligible_rows: Sequence[ShipmentRow],
            *,
            skipped: Sequence[tuple[ShipmentRow, str]] = (),
        ) -> None:
            if self._review_enabled and eligible_rows:
                preview = "\n".join(
                    (
                        f"• {row.platform_order_no} / {row.system_order_no or '-'}\n"
                        f"  品类：{row.product_type or '未识别'}；"
                        f"物流：{row.carrier or '-'} / "
                        f"{row.international_tracking_no or '-'}"
                    )
                    for row in eligible_rows[:10]
                )
                if len(eligible_rows) > 10:
                    preview += f"\n• ……另有 {len(eligible_rows) - 10} 张"
                answer = QMessageBox.question(
                    self,
                    "审核自动标发",
                    (
                        f"以下 {len(eligible_rows)} 张订单即将进入 ERP 自动标发，"
                        "请人工核对品类、承运商和运单号：\n\n"
                        f"{preview}\n\n确认无误并继续标发吗？"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._submit_shipment_rows(eligible_rows, skipped=skipped)

        def _confirm_and_execute(self) -> None:
            rows = self._checked_shipment_rows()
            if len(rows) != 1:
                self._result_handler(
                    ControlResult(False, "请只勾选一条已经人工核对物流信息的订单。")
                )
                return
            row = rows[0]
            dialog = _ConfirmedShipmentTrackingDialog(row, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            carrier, tracking_no = dialog.values()
            if not carrier or not tracking_no:
                self._result_handler(ControlResult(False, "承运商和运单号都不能为空。"))
                return
            def finish(result: ControlResult) -> None:
                if not result.accepted:
                    self._result_handler(result)
                    return
                confirmed_row = replace(
                    row,
                    international_tracking_no=tracking_no,
                    carrier=carrier,
                    logistics_state="READY",
                    erp_state="PENDING",
                    lease_owner="",
                    lease_stage="",
                    lease_until="",
                    last_error="",
                    logistics_last_error="",
                    erp_last_error="",
                )
                self._review_and_submit_shipment_rows([confirmed_row])

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.confirm_shipment_tracking_pair(
                    row.logistics_no,
                    carrier=carrier,
                    tracking_no=tracking_no,
                    reason="桌面用户人工核对承运商和运单号并放行，随后执行标发并同步客户通知草稿",
                ),
                finish,
            )

        def _edit_selected_tracking_pair(self) -> None:
            rows = self._checked_shipment_rows()
            if len(rows) != 1:
                self._result_handler(
                    ControlResult(False, "请只勾选一条需要修改物流信息的订单。")
                )
                return
            row = rows[0]
            dialog = _ConfirmedShipmentTrackingDialog(
                row,
                self,
                execute_after_save=False,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            carrier, tracking_no = dialog.values()
            if not carrier or not tracking_no:
                self._result_handler(ControlResult(False, "承运商和物流单号都不能为空。"))
                return

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.confirm_shipment_tracking_pair(
                    row.logistics_no,
                    carrier=carrier,
                    tracking_no=tracking_no,
                    reason="桌面用户向物流客服核实后手动修改物流单号和承运商",
                ),
                self._result_handler,
            )

        def _submit_shipment_rows(
            self,
            eligible_rows: Sequence[ShipmentRow],
            *,
            skipped: Sequence[tuple[ShipmentRow, str]] = (),
        ) -> None:
            batch_id = uuid4().hex
            self._optimistic_waiting_logistics_nos.update(
                row.logistics_no for row in eligible_rows if row.logistics_no
            )
            self._apply_search_filter()
            if getattr(self._controller, "snapshot_runs_in_background", False):
                self._submission_in_progress = True
                self.execute_button.setEnabled(False)
                self.execute_button.setText(f"正在提交 {len(eligible_rows)} 张…")
                self._update_selection_summary()
                thread = _ControlResultThread(
                    lambda rows=tuple(eligible_rows), excluded=tuple(skipped): (
                        self._submit_shipment_batch(
                            rows,
                            skipped=excluded,
                            batch_id=batch_id,
                        )
                    ),
                    self,
                )
                thread.result_ready.connect(self._finish_shipment_submission)
                thread.finished.connect(thread.deleteLater)
                self._submission_thread = thread
                thread.start()
                return
            self._finish_shipment_submission(
                self._submit_shipment_batch(
                    tuple(eligible_rows),
                    skipped=tuple(skipped),
                    batch_id=batch_id,
                )
            )

        def _submit_shipment_batch(
            self,
            eligible_rows: Sequence[ShipmentRow],
            *,
            skipped: Sequence[tuple[ShipmentRow, str]],
            batch_id: str,
        ) -> ControlResult:
            submitted: list[ShipmentRow] = []
            submitted_task_ids: list[str] = []
            submitted_task_ids_by_logistics_no: dict[str, str] = {}
            rejected: list[tuple[ShipmentRow, str]] = []
            unconfirmed: list[tuple[ShipmentRow, str]] = []
            commands: list[TaskCommand] = []
            for batch_position, row in enumerate(eligible_rows, start=1):
                confirmation = DesktopWriteConfirmation.create(
                    DesktopWriteAction.EXECUTE_ERP_MARK,
                    row.platform_order_no,
                    system_order_no=row.system_order_no,
                    logistics_no=row.logistics_no,
                    source="qt_checked_action",
                )
                commands.append(
                    TaskCommand(
                        name=f"执行自动标发：{row.platform_order_no}",
                        area=TaskArea.SHIPMENT,
                        capability=Capability.OUTBOUND_ORDER,
                        order_no=row.platform_order_no,
                        payload={
                            "system_order_no": row.system_order_no,
                            "logistics_no": row.logistics_no,
                            "shipment_batch_id": batch_id,
                            "shipment_batch_position": batch_position,
                            DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                        },
                    )
                )
            results = _submit_task_commands(self._controller, commands)
            for row, result in zip(eligible_rows, results):
                if result.accepted:
                    submitted.append(row)
                    if result.task_id:
                        submitted_task_ids.append(result.task_id)
                        submitted_task_ids_by_logistics_no[row.logistics_no] = (
                            result.task_id
                        )
                elif bool(result.details.get("submission_outcome_unknown")):
                    unconfirmed.append((row, result.message))
                else:
                    rejected.append((row, result.message))
            details: list[str] = (
                [f"已成功排队 {len(submitted)} 张。"]
                if submitted
                else []
            )
            if skipped:
                summary: dict[str, int] = {}
                for _row, reason in skipped:
                    summary[reason] = summary.get(reason, 0) + 1
                details.append(
                    "跳过并保留勾选："
                    + "、".join(f"{reason} {count} 张" for reason, count in summary.items())
                    + "。"
                )
            if rejected:
                details.append(
                    f"提交失败并保留勾选 {len(rejected)} 张："
                    + "；".join(
                        f"{row.platform_order_no}（{reason}）"
                        for row, reason in rejected[:5]
                    )
                )
            if unconfirmed:
                details.append(
                    f"另有 {len(unconfirmed)} 张提交请求已发送，"
                    "正在等待服务器确认，请勿重复提交。"
                )
            return ControlResult(
                bool(submitted),
                " ".join(details),
                details={
                    "non_modal": True,
                    "shipment_batch_id": batch_id,
                    "submitted_logistics_nos": tuple(
                        row.logistics_no for row in submitted
                    ),
                    "submitted_task_ids": tuple(submitted_task_ids),
                    "submitted_task_ids_by_logistics_no": tuple(
                        submitted_task_ids_by_logistics_no.items()
                    ),
                    "unconfirmed_logistics_nos": tuple(
                        row.logistics_no for row, _reason in unconfirmed
                    ),
                    "submission_outcome_unknown": bool(unconfirmed),
                },
            )

        def _finish_shipment_submission(self, result: ControlResult) -> None:
            self._submission_in_progress = False
            self._submission_thread = None
            self._optimistic_waiting_logistics_nos.clear()
            submitted_logistics_nos = tuple(
                result.details.get("submitted_logistics_nos") or ()
            )
            for logistics_no in submitted_logistics_nos:
                self._checked_logistics_nos.discard(str(logistics_no))
            unconfirmed_logistics_nos = {
                str(logistics_no)
                for logistics_no in result.details.get(
                    "unconfirmed_logistics_nos",
                    (),
                )
            }
            self._checked_logistics_nos.difference_update(
                unconfirmed_logistics_nos
            )
            self._unconfirmed_logistics_nos.update(unconfirmed_logistics_nos)
            self._active_logistics_nos.update(unconfirmed_logistics_nos)
            submitted_task_ids = tuple(
                result.details.get("submitted_task_ids") or ()
            )
            submitted_task_ids_by_logistics_no = dict(
                result.details.get("submitted_task_ids_by_logistics_no") or ()
            )
            for logistics_no, task_id in submitted_task_ids_by_logistics_no.items():
                normalized_logistics_no = str(logistics_no)
                self._active_logistics_nos.add(normalized_logistics_no)
                self._active_task_ids_by_logistics_no[normalized_logistics_no] = (
                    str(task_id),
                )
            batch_id = str(result.details.get("shipment_batch_id") or "")
            if submitted_task_ids and batch_id and self._batch_handler is not None:
                self._batch_handler(batch_id, submitted_task_ids)
            self._apply_search_filter()
            self._result_handler(result)

        def _query_logistics(self) -> None:
            command = TaskCommand(
                name="重新查询到期物流状态",
                area=TaskArea.SHIPMENT,
                capability=Capability.ALIBABA_LOGISTICS,
            )
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                self._result_handler,
            )

        def _selected_row(self) -> ShipmentRow | None:
            index = self.table.currentRow()
            if index < 0:
                selected = self.table.selectedIndexes()
                index = selected[0].row() if selected else -1
            return self._rows[index] if 0 <= index < len(self._rows) else None

        def _checked_shipment_rows(self) -> list[ShipmentRow]:
            return [
                row
                for row in self._rows
                if (
                    _shipment_selection_key(row) in self._checked_scan_issue_keys
                    if row.scan_issue_code
                    else row.logistics_no in self._checked_logistics_nos
                )
            ]

        def _visible_selection_keys(self) -> frozenset[str]:
            return self._visible_logistics_nos | self._visible_scan_issue_keys

        def _checked_selection_keys(self) -> set[str]:
            return self._checked_logistics_nos | self._checked_scan_issue_keys

        def _visible_ready_logistics_nos(self) -> set[str]:
            return set(self._visible_ready_logistics_nos_cache)

        def _update_quick_select_button(self) -> None:
            count = len(self._visible_ready_logistics_nos_cache)
            self.quick_select_button.setText(f"勾选可标发（{count}）")
            self.quick_select_button.setEnabled(bool(count))

        def _update_selection_summary(self) -> None:
            selected_rows = self._checked_shipment_rows()
            selected_count = len(selected_rows)
            selected_queue_count = sum(
                1 for row in selected_rows if not row.scan_issue_code
            )
            has_scan_issues = any(row.scan_issue_code for row in selected_rows)
            self.ready_count_label.setText(
                f"显示 {len(self._rows)} · 可标发 {self._ready_shipment_count} · "
                f"已选 {selected_count}"
            )

            has_selection = selected_count > 0
            batch_actions_enabled = has_selection and not self._submission_in_progress
            self.more_actions_button.setEnabled(batch_actions_enabled)
            queue_actions_enabled = (
                selected_queue_count > 0
                and not has_scan_issues
                and not self._submission_in_progress
            )
            self.execute_button.setEnabled(
                selected_queue_count > 0 and not self._submission_in_progress
            )
            self.confirm_execute_action.setEnabled(
                selected_queue_count == 1
                and not has_scan_issues
                and not self._submission_in_progress
            )
            self.edit_tracking_action.setEnabled(
                selected_queue_count == 1
                and not has_scan_issues
                and not self._submission_in_progress
            )
            self.change_status_action.setEnabled(batch_actions_enabled)
            for action in (
                self.retry_logistics_action,
                self.retry_erp_action,
                self.stop_tasks_action,
            ):
                action.setEnabled(queue_actions_enabled)
            if not self._submission_in_progress:
                self.execute_button.setText(f"执行标发（{selected_queue_count}）")

        def _select_visible_ready_shipments(self) -> None:
            visible_logistics_nos = self._visible_logistics_nos
            eligible_logistics_nos = self._visible_ready_logistics_nos_cache
            self._checked_logistics_nos.difference_update(visible_logistics_nos)
            self._checked_scan_issue_keys.difference_update(
                self._visible_scan_issue_keys
            )
            self._checked_logistics_nos.update(eligible_logistics_nos)
            self._refresh_visible_checkboxes()
            self._result_handler(
                ControlResult(
                    bool(eligible_logistics_nos),
                    (
                        f"已勾选当前筛选结果中的 {len(eligible_logistics_nos)} 张可标发订单。"
                        if eligible_logistics_nos
                        else "当前筛选结果中没有符合标发要求的订单。"
                    ),
                    details={"non_modal": True},
                )
            )

        def _clear_checked_shipments(self, logistics_nos: Sequence[str]) -> None:
            cleared = {str(value) for value in logistics_nos}
            self._checked_logistics_nos.difference_update(cleared)
            self._checked_scan_issue_keys.difference_update(cleared)
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for selection_key in cleared:
                    row_index = self._row_index_by_selection_key.get(selection_key)
                    if row_index is None:
                        continue
                    item = self.table.item(row_index, 0)
                    if item is not None:
                        item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()

        def _reason(self, title: str) -> str | None:
            reason, accepted = QInputDialog.getText(self, title, "请输入原因（会保留在事件历史）：")
            if not accepted:
                return None
            value = reason.strip()
            if not value:
                self._result_handler(ControlResult(False, "原因不能为空。"))
                return None
            return value

        def _retry_selected_stage(self, stage: str = "logistics") -> None:
            rows = self._checked_shipment_rows()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选至少一条自动标发任务。"))
                return
            normalized_stage = stage if stage in {"logistics", "erp"} else "logistics"
            stage_label = {
                "logistics": "物流查询阶段",
                "erp": "ERP 标发阶段",
            }[normalized_stage]
            is_erp_reconciliation = normalized_stage == "erp"
            reason = self._reason(
                "核验并继续 ERP 标发"
                if is_erp_reconciliation
                else "重试自动标发阶段"
            )
            if reason is None:
                return
            preview = "\n".join(f"• {row.platform_order_no}" for row in rows[:10])
            if len(rows) > 10:
                preview += f"\n• ……另有 {len(rows) - 10} 张"
            answer = QMessageBox.question(
                self,
                (
                    "确认核验并继续 ERP 标发"
                    if is_erp_reconciliation
                    else "确认重试勾选阶段"
                ),
                f"即将把 {len(rows)} 条任务放回“{stage_label}”：\n\n"
                f"{preview}\n\n原因：{reason}\n\n"
                + (
                    "该操作只修改本地队列，不立即请求 ERP。后续执行会先只读核验"
                    "领星已有状态，已生效的审核不会重复提交。是否继续？"
                    if is_erp_reconciliation
                    else "该操作只修改本地队列，不立即请求 ERP。是否继续？"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            logistics_nos = [row.logistics_no for row in rows]

            def finish(result: ControlResult) -> None:
                changed = tuple(
                    result.details.get("changed_logistics_nos") or ()
                )
                if changed:
                    self._clear_checked_shipments(changed)
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.retry_shipment_stages(
                    logistics_nos,
                    normalized_stage,
                    reason=reason,
                ),
                finish,
            )

        def _stop_checked_tasks(self) -> None:
            rows = self._checked_shipment_rows()
            if not rows:
                self._result_handler(ControlResult(False, "请先勾选至少一条自动标发任务。"))
                return
            reason = self._reason("停止当前勾选任务")
            if reason is None:
                return
            preview = "\n".join(
                f"• {row.platform_order_no}；{row.system_order_no or '-'}"
                for row in rows[:10]
            )
            if len(rows) > 10:
                preview += f"\n• ……另有 {len(rows) - 10} 条"
            answer = QMessageBox.question(
                self,
                "确认停止当前勾选任务",
                f"即将停止当前勾选的 {len(rows)} 条自动标发任务的本轮处理：\n\n{preview}\n\n"
                "任务会保留在原队列位置；下次完整扫描再次发现后将自动恢复。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                logistics_nos = [row.logistics_no for row in rows]

                def finish(result: ControlResult) -> None:
                    if result.accepted:
                        self._checked_logistics_nos.difference_update(
                            logistics_nos
                        )
                    self._result_handler(result)

                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_shipments(
                        logistics_nos,
                        reason=reason,
                    ),
                    finish,
                )

        def _stop_all_tasks(self) -> None:
            task_ids = list(self._active_page_task_ids)
            if not task_ids:
                self._result_handler(
                    ControlResult(False, "自动标发页当前没有活动任务。")
                )
                return
            answer = QMessageBox.question(
                self,
                "确认停止本页所有任务",
                f"即将停止自动标发页内全部 {len(task_ids)} 个等待中、"
                "运行中或等待确认的后台任务。\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止；"
                "尚未开始且没有后台任务的队列行不会被批量修改。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _on_item_changed(self, item: QTableWidgetItem) -> None:
            if item.column() != 0:
                return
            selection_key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not selection_key:
                return
            target = (
                self._checked_scan_issue_keys
                if selection_key.startswith(("scan-issue:", "scan-row:"))
                else self._checked_logistics_nos
            )
            if item.checkState() == Qt.CheckState.Checked:
                target.add(selection_key)
            else:
                target.discard(selection_key)
            self._sync_check_header()

        def _set_all_checked(self, state_value: int) -> None:
            checked = Qt.CheckState(state_value) == Qt.CheckState.Checked
            if checked:
                self._checked_logistics_nos.update(self._visible_logistics_nos)
                self._checked_scan_issue_keys.update(
                    self._visible_scan_issue_keys
                )
            else:
                self._checked_logistics_nos.difference_update(
                    self._visible_logistics_nos
                )
                self._checked_scan_issue_keys.difference_update(
                    self._visible_scan_issue_keys
                )
            self._refresh_visible_checkboxes()

        def _refresh_visible_checkboxes(self) -> None:
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for row_index in range(self.table.rowCount()):
                    item = self.table.item(row_index, 0)
                    if item is None:
                        continue
                    selection_key = str(
                        item.data(Qt.ItemDataRole.UserRole) or ""
                    ).strip()
                    desired_state = (
                        Qt.CheckState.Checked
                        if selection_key in self._checked_selection_keys()
                        else Qt.CheckState.Unchecked
                    )
                    if item.checkState() != desired_state:
                        item.setCheckState(desired_state)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()

        def _sync_check_header(self) -> None:
            checked_count = len(
                self._visible_selection_keys() & self._checked_selection_keys()
            )
            if not checked_count:
                state = Qt.CheckState.Unchecked
            elif checked_count == len(self._visible_selection_keys()):
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            self._check_header.set_check_state(state)
            self._update_selection_summary()

        def _apply_search_filter(self, *_args, reset_page: bool = True) -> None:
            selected = self._selected_row()
            selected_row_key = _shipment_selection_key(selected) if selected else ""
            field = str(self.search_field_combo.currentData() or "platform_order_no")
            query = self.search_edit.text()
            selected_status = str(self.status_filter_combo.currentData() or "")
            selected_product_types = self.product_type_filter_combo.selected_values

            def shipment_sort_key(row: ShipmentRow) -> tuple[object, ...]:
                status = self._display_business_status(row)
                if status == "标发处理中":
                    work_bucket = 0
                elif status == "等待标发":
                    work_bucket = 1
                elif status in {"可标发", "可继续标发"}:
                    work_bucket = 2
                elif status == "已完成":
                    work_bucket = 4
                elif status in {"已取消", "标签已移除", "本轮已取消"}:
                    work_bucket = 5
                else:
                    work_bucket = 3
                return (
                    work_bucket,
                    _SHIPMENT_STATUS_PRIORITY.get(status, 99),
                    -_status_timestamp_value(_shipment_status_timestamp(row)),
                    row.platform_order_no,
                    row.logistics_no,
                )

            ordered_rows = sorted(
                self._all_rows,
                key=shipment_sort_key,
            )
            self._filtered_rows = [
                row
                for row in ordered_rows
                if _queue_row_matches_search(row, field, query)
                and _matches_product_type_filter(row, selected_product_types)
                and (
                    not selected_status
                    or self._display_business_status(row) == selected_status
                )
            ]
            if reset_page:
                self._page = 1
            self._render_filtered_shipment_page(selected_row_key=selected_row_key)

        def _render_filtered_shipment_page(
            self,
            *,
            selected_row_key: str = "",
        ) -> None:
            self._page_count = max(
                1,
                (len(self._filtered_rows) + self._page_size - 1)
                // self._page_size,
            )
            self._page = max(1, min(self._page, self._page_count))
            page_start = (self._page - 1) * self._page_size
            self._rows = self._filtered_rows[
                page_start : page_start + self._page_size
            ]
            self.pagination_bar.set_state(
                total=len(self._filtered_rows),
                page=self._page,
                page_size=self._page_size,
                page_count=self._page_count,
            )
            self._visible_logistics_nos = frozenset(
                row.logistics_no
                for row in self._rows
                if not row.scan_issue_code and row.logistics_no
            )
            self._visible_scan_issue_keys = frozenset(
                _shipment_selection_key(row)
                for row in self._rows
                if row.scan_issue_code
            )
            self._visible_ready_logistics_nos_cache = frozenset(
                row.logistics_no
                for row in self._rows
                if _shipment_execution_eligibility(
                    row,
                    active_logistics_nos=(
                        self._active_logistics_nos
                        | self._optimistic_waiting_logistics_nos
                    ),
                )[0]
            )
            self._ready_shipment_count = len(
                self._visible_ready_logistics_nos_cache
            )
            self._checked_logistics_nos.intersection_update(
                self._visible_logistics_nos
            )
            self._checked_scan_issue_keys.intersection_update(
                self._visible_scan_issue_keys
            )
            self._update_quick_select_button()
            self._render_rows(selected_row_key=selected_row_key)

        def _show_page(self, page: int) -> None:
            target = max(1, min(int(page), self._page_count))
            if target == self._page:
                return
            selected = self._selected_row()
            selected_row_key = _shipment_selection_key(selected) if selected else ""
            self._page = target
            self._render_filtered_shipment_page(selected_row_key=selected_row_key)

        def _change_page_size(self, page_size: int) -> None:
            normalized = int(page_size)
            if normalized == self._page_size:
                return
            self._page_size = normalized
            self._page = 1
            self._render_filtered_shipment_page()

        def _schedule_search_filter(self, *_args) -> None:
            if len(self._all_rows) < 250:
                self._search_filter_timer.stop()
                self._apply_search_filter()
            else:
                self._search_filter_timer.start()

        def _render_rows(self, *, selected_row_key: str = "") -> None:
            selected_row_index = -1
            selected_column = self.table.currentColumn()
            scroll_state = _table_scroll_state(self.table)
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            self._row_index_by_logistics_no = {}
            self._row_index_by_selection_key = {}
            try:
                self.table.setRowCount(len(self._rows))
                for row_index, row in enumerate(self._rows):
                    selection_key = _shipment_selection_key(row)
                    self._row_index_by_selection_key[selection_key] = row_index
                    if row.logistics_no:
                        self._row_index_by_logistics_no[row.logistics_no] = row_index
                    if selection_key == selected_row_key:
                        selected_row_index = row_index
                    check_item = QTableWidgetItem()
                    check_item.setFlags(
                        (check_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable
                    )
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if selection_key in self._checked_selection_keys()
                        else Qt.CheckState.Unchecked
                    )
                    check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    check_item.setData(Qt.ItemDataRole.UserRole, selection_key)
                    self.table.setItem(row_index, 0, check_item)
                    business_status = self._display_business_status(row)
                    values = (
                        row.platform_order_no,
                        row.system_order_no,
                        _shipment_product_type_label(row),
                        row.logistics_no,
                        row.international_tracking_no or "-",
                        row.carrier or "-",
                        business_status,
                        _shipment_checkpoint_label(row.checkpoint),
                        _format_status_timestamp(_shipment_status_timestamp(row)),
                        _format_status_timestamp(row.logistics_last_checked_at),
                        self._display_status_explanation(row, business_status),
                        (
                            _SHIPMENT_OVERDUE_HISTORY_LABEL
                            if (
                                str(row.logistics_overdue_at or "").strip()
                                or business_status == "物流逾期异常"
                            )
                            else _SHIPMENT_NO_OVERDUE_HISTORY_LABEL
                        ),
                    )
                    for column, value in enumerate(values, start=1):
                        item = _readonly_item(value)
                        if column == 7:
                            color = {
                                "可标发": "#047857",
                                "可继续标发": "#047857",
                                "等待标发": "#1D4ED8",
                                "等待用户确认": "#B45309",
                                "标发处理中": "#1D4ED8",
                                "标发失败可重试": "#B45309",
                                "物流逾期异常": "#B54708",
                                "物流信息需复核": "#B42318",
                                "标发需人工复核": "#B42318",
                                "订单信息冲突": "#B42318",
                                "扫描错误": "#B42318",
                                "已完成": "#047857",
                            }.get(business_status, "#475467")
                            item.setForeground(QColor(color))
                            font = item.font()
                            font.setBold(business_status in {"可标发", "可继续标发"})
                            item.setFont(font)
                        elif column == 12:
                            has_overdue_history = bool(
                                str(row.logistics_overdue_at or "").strip()
                                or business_status == "物流逾期异常"
                            )
                            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                            item.setForeground(
                                QColor(
                                    _SHIPMENT_OVERDUE_HISTORY_COLOR
                                    if has_overdue_history
                                    else _SHIPMENT_NO_OVERDUE_HISTORY_COLOR
                                )
                            )
                        self.table.setItem(row_index, column, item)
                if selected_row_index >= 0:
                    column = min(
                        max(selected_column, 0),
                        max(0, self.table.columnCount() - 1),
                    )
                    self.table.setCurrentCell(selected_row_index, column)
                else:
                    self.table.clearSelection()
                    self.table.setCurrentCell(-1, -1)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            _restore_table_scroll_state(self.table, scroll_state)
            self._sync_check_header()

        def _update_active_shipment_cells(
            self,
            logistics_nos: set[str],
        ) -> None:
            """Patch volatile task progress without rebuilding every row."""

            if not logistics_nos:
                return
            rows_by_logistics_no = {
                row.logistics_no: row for row in self._rows
            }
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for logistics_no in logistics_nos:
                    row_index = self._row_index_by_logistics_no.get(logistics_no)
                    row = rows_by_logistics_no.get(logistics_no)
                    if row_index is None or row is None:
                        continue
                    business_status = self._display_business_status(row)
                    status_item = _readonly_item(business_status)
                    status_item.setForeground(
                        QColor(
                            {
                                "可标发": "#047857",
                                "可继续标发": "#047857",
                                "等待标发": "#1D4ED8",
                                "等待用户确认": "#B45309",
                                "标发处理中": "#1D4ED8",
                                "标发失败可重试": "#B45309",
                                "物流逾期异常": "#B54708",
                                "物流信息需复核": "#B42318",
                                "标发需人工复核": "#B42318",
                                "订单信息冲突": "#B42318",
                                "扫描错误": "#B42318",
                                "已完成": "#047857",
                            }.get(business_status, "#475467")
                        )
                    )
                    self.table.setItem(row_index, 7, status_item)
                    detail = self._display_status_explanation(row, business_status)
                    detail_item = _readonly_item(detail)
                    if detail:
                        detail_item.setToolTip(detail)
                    self.table.setItem(row_index, 11, detail_item)
                    has_overdue_history = bool(
                        str(row.logistics_overdue_at or "").strip()
                        or business_status == "物流逾期异常"
                    )
                    overdue_item = _readonly_item(
                        _SHIPMENT_OVERDUE_HISTORY_LABEL
                        if has_overdue_history
                        else _SHIPMENT_NO_OVERDUE_HISTORY_LABEL
                    )
                    overdue_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    overdue_item.setForeground(
                        QColor(
                            _SHIPMENT_OVERDUE_HISTORY_COLOR
                            if has_overdue_history
                            else _SHIPMENT_NO_OVERDUE_HISTORY_COLOR
                        )
                    )
                    self.table.setItem(row_index, 12, overdue_item)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)

        def _display_business_status(self, row: ShipmentRow) -> str:
            persisted_status = _shipment_business_status(row)
            if persisted_status == "已完成":
                return persisted_status
            active_tasks = self._active_tasks_by_logistics_no.get(
                row.logistics_no,
                (),
            )
            if active_tasks:
                active_task = max(active_tasks, key=lambda task: task.updated_at)
                if active_task.status is TaskStatus.QUEUED:
                    return "等待标发"
                if active_task.status is TaskStatus.WAITING_USER:
                    return "等待用户确认"
                return "标发处理中"
            if row.logistics_no in self._active_logistics_nos:
                return "等待标发"
            if row.logistics_no in self._optimistic_waiting_logistics_nos:
                return "等待标发"
            return persisted_status

        def _display_status_explanation(
            self,
            row: ShipmentRow,
            business_status: str,
        ) -> str:
            if business_status == "已完成":
                return _shipment_status_explanation(row, business_status)
            active_tasks = self._active_tasks_by_logistics_no.get(
                row.logistics_no,
                (),
            )
            if active_tasks:
                active_task = max(active_tasks, key=lambda task: task.updated_at)
                return (
                    f"{active_task.status.label} · "
                    f"{active_task.progress_percent}% · {active_task.message}"
                )
            if row.logistics_no in self._unconfirmed_logistics_nos:
                return "提交请求已发送，正在等待服务器确认，请勿重复提交。"
            if row.logistics_no in self._active_logistics_nos:
                return "已加入标发队列，等待后台任务更新。"
            if row.logistics_no in self._optimistic_waiting_logistics_nos:
                return "正在提交本批订单，等待服务器确认排队。"
            return _shipment_status_explanation(row, business_status)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._review_enabled = bool(snapshot.settings.shipment_review_enabled)
            previous_active_logistics_nos = set(self._active_logistics_nos)
            previous_active_tasks_by_logistics_no = (
                self._active_tasks_by_logistics_no
            )
            next_rows = list(snapshot.shipments)
            next_active_page_task_ids = tuple(
                task.task_id
                for task in snapshot.tasks
                if task.area is TaskArea.SHIPMENT
                and not task.status.terminal
                and (
                    task.capability
                    in {
                        Capability.OUTBOUND_ORDER,
                        Capability.ALIBABA_LOGISTICS,
                    }
                    or (
                        task.capability is Capability.LIST_ORDERS
                        and str(task.payload.get("trigger") or "")
                        not in {
                            NOTIFICATION_REVIEW_RESCAN_TRIGGER,
                            SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
                        }
                    )
                )
            )
            next_active_logistics_nos = {
                str(task.payload.get("logistics_no") or "").strip()
                for task in snapshot.tasks
                if task.area is TaskArea.SHIPMENT
                and task.capability is Capability.OUTBOUND_ORDER
                and not task.status.terminal
                and str(task.payload.get("logistics_no") or "").strip()
            }
            confirmed_unconfirmed_logistics_nos = (
                self._unconfirmed_logistics_nos & next_active_logistics_nos
            )
            self._unconfirmed_logistics_nos.difference_update(
                confirmed_unconfirmed_logistics_nos
            )
            next_active_logistics_nos.update(self._unconfirmed_logistics_nos)
            active_tasks_by_logistics_no: dict[str, list[TaskRecord]] = {}
            for task in snapshot.tasks:
                logistics_no = str(task.payload.get("logistics_no") or "").strip()
                if (
                    task.area is TaskArea.SHIPMENT
                    and task.capability is Capability.OUTBOUND_ORDER
                    and not task.status.terminal
                    and logistics_no
                ):
                    active_tasks_by_logistics_no.setdefault(logistics_no, []).append(
                        task
                    )
            next_active_tasks_by_logistics_no = {
                logistics_no: tuple(tasks)
                for logistics_no, tasks in active_tasks_by_logistics_no.items()
            }
            next_active_task_ids_by_logistics_no = {
                logistics_no: tuple(task.task_id for task in tasks)
                for logistics_no, tasks in next_active_tasks_by_logistics_no.items()
            }
            rows_changed = next_rows != self._all_rows
            active_changed = (
                next_active_logistics_nos != self._active_logistics_nos
                or next_active_task_ids_by_logistics_no
                != self._active_task_ids_by_logistics_no
                or next_active_tasks_by_logistics_no
                != self._active_tasks_by_logistics_no
                or next_active_page_task_ids != self._active_page_task_ids
            )
            if not rows_changed and not active_changed:
                return
            self._active_logistics_nos = next_active_logistics_nos
            self._active_task_ids_by_logistics_no = (
                next_active_task_ids_by_logistics_no
            )
            self._active_tasks_by_logistics_no = next_active_tasks_by_logistics_no
            self._active_page_task_ids = next_active_page_task_ids
            if rows_changed:
                self._all_rows = next_rows
                self.product_type_filter_combo.set_available_values(
                    [row.product_type for row in self._all_rows]
                )
            all_logistics_nos = {
                row.logistics_no
                for row in self._all_rows
                if not row.scan_issue_code and row.logistics_no
            }
            all_scan_issue_keys = {
                _shipment_selection_key(row)
                for row in self._all_rows
                if row.scan_issue_code
            }
            self._checked_logistics_nos.intersection_update(all_logistics_nos)
            self._checked_scan_issue_keys.intersection_update(all_scan_issue_keys)
            if (
                rows_changed
                or previous_active_logistics_nos != next_active_logistics_nos
            ):
                self._apply_search_filter(reset_page=False)
            else:
                changed_logistics_nos = {
                    logistics_no
                    for logistics_no in set(
                        previous_active_tasks_by_logistics_no
                    )
                    | set(next_active_tasks_by_logistics_no)
                    if previous_active_tasks_by_logistics_no.get(logistics_no)
                    != next_active_tasks_by_logistics_no.get(logistics_no)
                }
                self._update_active_shipment_cells(changed_logistics_nos)


    class StateManagementPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._last_policy_signature: object | None = None
            self._last_task_signature: object | None = None
            self._tasks: list[TaskRecord] = []
            self._checked_task_ids: set[str] = set()
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            title = QLabel("状态管理")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            self.emergency_state = QLabel()
            self.emergency_state.setWordWrap(True)
            layout.addWidget(self.emergency_state)

            splitter = QSplitter(Qt.Orientation.Vertical)
            capability_panel = QWidget()
            capability_layout = QVBoxLayout(capability_panel)
            capability_layout.setContentsMargins(0, 0, 0, 0)
            capability_layout.addWidget(QLabel("能力执行模式"))
            self.capabilities = QTableWidget(0, 4)
            self.capabilities.setHorizontalHeaderLabels(["能力", "类型", "配置模式", "实际模式"])
            _prepare_table(self.capabilities)
            self.capabilities.setSelectionMode(
                QAbstractItemView.SelectionMode.NoSelection
            )
            self.capabilities.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            _set_table_default_widths(
                self.capabilities,
                (220, 120, 140, 140),
            )
            capability_layout.addWidget(self.capabilities)
            splitter.addWidget(capability_panel)

            task_panel = QWidget()
            task_layout = QVBoxLayout(task_panel)
            task_layout.setContentsMargins(0, 0, 0, 0)
            action_row = QHBoxLayout()
            action_row.addWidget(QLabel("后台任务"))
            action_row.addStretch(1)
            retry_button = QPushButton("重试")
            retry_button.clicked.connect(self._retry_selected)
            cancel_button = QPushButton("停止当前勾选任务")
            cancel_button.setObjectName("dangerButton")
            cancel_button.clicked.connect(self._cancel_checked)
            cancel_all_button = QPushButton("停止本页所有任务")
            cancel_all_button.setObjectName("dangerButton")
            cancel_all_button.clicked.connect(self._cancel_all)
            action_row.addWidget(retry_button)
            action_row.addWidget(cancel_button)
            action_row.addWidget(cancel_all_button)
            task_layout.addLayout(action_row)
            self.tasks = QTableWidget(0, 8)
            self._task_check_header = _CheckableHeaderView(self.tasks)
            self.tasks.setHorizontalHeader(self._task_check_header)
            self.tasks.setHorizontalHeaderLabels(
                ["", "任务 ID", "业务", "任务", "操作账号", "状态", "进度", "说明"]
            )
            _prepare_table(self.tasks, full_cell_check_column=0)
            _set_table_default_widths(
                self.tasks,
                (40, 220, 110, 180, 160, 110, 90, 360),
            )
            self._task_check_header.check_state_changed.connect(self._set_all_tasks_checked)
            self.tasks.itemChanged.connect(self._on_task_item_changed)
            task_layout.addWidget(self.tasks)
            splitter.addWidget(task_panel)
            splitter.setSizes([330, 330])
            layout.addWidget(splitter, 1)

        def _change_mode(self, capability: Capability, mode_value: str) -> None:
            mode = CapabilityMode(mode_value)
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.update_capability_mode(
                    capability,
                    mode,
                ),
                self._result_handler,
            )

        def _selected_task_id(self) -> str | None:
            row = self.tasks.currentRow()
            if row < 0:
                return None
            item = self.tasks.item(row, 1)
            return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

        def _retry_selected(self) -> None:
            task_id = self._selected_task_id()
            if not task_id:
                self._result_handler(ControlResult(False, "请先选择一个任务。"))
                return
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.retry_task(task_id),
                self._result_handler,
            )

        def _cancel_checked(self) -> None:
            task_ids = [
                task.task_id
                for task in self._tasks
                if task.task_id in self._checked_task_ids and not task.status.terminal
            ]
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.cancel_tasks(task_ids),
                self._result_handler,
            )

        def _cancel_all(self) -> None:
            task_ids = [
                task.task_id for task in self._tasks if not task.status.terminal
            ]
            if not task_ids:
                self._result_handler(
                    ControlResult(False, "状态管理页当前没有活动任务。")
                )
                return
            answer = QMessageBox.question(
                self,
                "确认停止本页所有任务",
                f"即将停止当前显示的全部 {len(task_ids)} 个等待中、"
                "运行中或等待确认的后台任务。\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _on_task_item_changed(self, item: QTableWidgetItem) -> None:
            if item.column() != 0:
                return
            task_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not task_id:
                return
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_task_ids.add(task_id)
            else:
                self._checked_task_ids.discard(task_id)
            self._sync_task_check_header()

        def _set_all_tasks_checked(self, state_value: int) -> None:
            checked = Qt.CheckState(state_value) == Qt.CheckState.Checked
            active_ids = {task.task_id for task in self._tasks if not task.status.terminal}
            if checked:
                self._checked_task_ids.update(active_ids)
            else:
                self._checked_task_ids.difference_update(active_ids)
            previous = self.tasks.blockSignals(True)
            try:
                for row in range(self.tasks.rowCount()):
                    item = self.tasks.item(row, 0)
                    if item is None:
                        continue
                    task_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if checked and task_id in active_ids
                        else Qt.CheckState.Unchecked
                    )
            finally:
                self.tasks.blockSignals(previous)
            self._sync_task_check_header()

        def _sync_task_check_header(self) -> None:
            active_ids = {task.task_id for task in self._tasks if not task.status.terminal}
            checked_count = len(active_ids & self._checked_task_ids)
            if not checked_count:
                state = Qt.CheckState.Unchecked
            elif checked_count == len(active_ids):
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            self._task_check_header.set_check_state(state)

        def _render_policy(
            self,
            snapshot: DesktopSnapshot,
            capabilities: Sequence[Capability],
        ) -> None:
            emergency_active = snapshot.policy.emergency_stop_writes
            self.emergency_state.setText(
                "ERP 写入状态：已紧急停止。所有后续写入均被阻止，请使用左侧全局按钮解除。"
                if emergency_active
                else "ERP 写入状态：允许执行。如需停止所有写入，请使用左侧全局按钮。"
            )
            self.emergency_state.setStyleSheet(
                (
                    "color: #B42318; background: #FEF3F2; border: 1px solid #FDA29B; "
                    "border-radius: 8px; padding: 9px 12px; font-weight: 600;"
                )
                if emergency_active
                else (
                    "color: #027A48; background: #ECFDF3; border: 1px solid #ABEFC6; "
                    "border-radius: 8px; padding: 9px 12px; font-weight: 600;"
                )
            )

            scroll_state = _table_scroll_state(self.capabilities)
            self.capabilities.setUpdatesEnabled(False)
            self.capabilities.setRowCount(len(capabilities))
            for row, capability in enumerate(capabilities):
                self.capabilities.setItem(row, 0, _readonly_item(capability.label))
                self.capabilities.setItem(
                    row,
                    1,
                    _readonly_item("写入" if capability.is_write else "只读"),
                )
                combo = QComboBox()
                if capability in {
                    Capability.UPDATE_CONTACT,
                    Capability.ALIBABA_LOGISTICS,
                    Capability.ALIBABA_ORDER_PREPARE,
                    Capability.ALIBABA_ORDER_DRAFT,
                }:
                    allowed_modes = (CapabilityMode.BROWSER, CapabilityMode.DISABLED)
                elif capability is Capability.EMAIL_PREVIEW:
                    combo.addItem(
                        "禁用（邮件功能尚未接入）",
                        CapabilityMode.DISABLED.value,
                    )
                    combo.setEnabled(False)
                    allowed_modes = ()
                else:
                    # Capabilities fully covered by the official OpenAPI stay
                    # API-first. Contact writeback is handled above because
                    # detail-page verification is safer for both contact fields.
                    allowed_modes = (
                        CapabilityMode.API_FIRST,
                        CapabilityMode.DISABLED,
                    )
                for mode in allowed_modes:
                    combo.addItem(mode.label, mode.value)
                configured = snapshot.policy.configured_mode_for(capability)
                configured_index = combo.findData(configured.value)
                if configured_index < 0:
                    configured_index = 0
                combo.setCurrentIndex(configured_index)
                combo.currentIndexChanged.connect(
                    lambda _index, cap=capability, box=combo: self._change_mode(
                        cap,
                        box.currentData(),
                    )
                )
                self.capabilities.setCellWidget(row, 2, combo)
                effective = snapshot.policy.effective_mode_for(capability)
                effective_item = _readonly_item(effective.label)
                if effective is CapabilityMode.DISABLED:
                    effective_item.setForeground(QColor("#c0392b"))
                self.capabilities.setItem(row, 3, effective_item)
            self.capabilities.setUpdatesEnabled(True)
            _restore_table_scroll_state(self.capabilities, scroll_state)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            capabilities = list(Capability)
            policy_signature = (
                snapshot.policy.emergency_stop_writes,
                tuple(
                    (
                        capability.value,
                        snapshot.policy.configured_mode_for(capability).value,
                        snapshot.policy.effective_mode_for(capability).value,
                    )
                    for capability in capabilities
                ),
            )
            task_signature = tuple(snapshot.tasks)
            policy_changed = policy_signature != self._last_policy_signature
            tasks_changed = task_signature != self._last_task_signature
            if not policy_changed and not tasks_changed:
                return
            self._last_policy_signature = policy_signature
            self._last_task_signature = task_signature
            if policy_changed:
                self._render_policy(snapshot, capabilities)
            if not tasks_changed:
                return

            self._tasks = list(snapshot.tasks)
            active_ids = {task.task_id for task in self._tasks if not task.status.terminal}
            self._checked_task_ids.intersection_update(active_ids)
            scroll_state = _table_scroll_state(self.tasks)
            same_task_layout = (
                self.tasks.rowCount() == len(self._tasks)
                and all(
                    (item := self.tasks.item(row, 0)) is not None
                    and str(item.data(Qt.ItemDataRole.UserRole) or "")
                    == task.task_id
                    for row, task in enumerate(self._tasks)
                )
            )
            previous = self.tasks.blockSignals(True)
            self.tasks.setUpdatesEnabled(False)
            try:
                if not same_task_layout:
                    self.tasks.setRowCount(len(self._tasks))
                for row, task in enumerate(self._tasks):
                    check_item = (
                        self.tasks.item(row, 0)
                        if same_task_layout
                        else QTableWidgetItem()
                    )
                    if check_item is None:
                        check_item = QTableWidgetItem()
                    if not task.status.terminal:
                        check_item.setFlags(
                            (check_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                            & ~Qt.ItemFlag.ItemIsEditable
                        )
                    else:
                        check_item.setFlags(check_item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if task.task_id in self._checked_task_ids
                        else Qt.CheckState.Unchecked
                    )
                    check_item.setData(Qt.ItemDataRole.UserRole, task.task_id)
                    check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if not same_task_layout:
                        self.tasks.setItem(row, 0, check_item)
                    short_id = task.task_id[:10]
                    values = (
                        short_id,
                        task.area.label,
                        task.name,
                        _operator_display(
                            task.operator_name,
                            task.operator_email,
                        ),
                        task.status.label,
                        f"{task.progress_percent}%",
                        task.message,
                    )
                    for column, value in enumerate(values, start=1):
                        user_data = task.task_id if column == 1 else None
                        existing = self.tasks.item(row, column)
                        if (
                            same_task_layout
                            and existing is not None
                            and existing.text() == str(value)
                            and (
                                column != 1
                                or existing.data(Qt.ItemDataRole.UserRole)
                                == user_data
                            )
                        ):
                            continue
                        self.tasks.setItem(
                            row,
                            column,
                            _readonly_item(value, user_data=user_data),
                        )
            finally:
                self.tasks.blockSignals(previous)
                self.tasks.setUpdatesEnabled(True)
            _restore_table_scroll_state(self.tasks, scroll_state)
            self._sync_task_check_header()


    class SettingsPage(QWidget):
        def __init__(self, controller: BackgroundTaskController, result_handler: ResultHandler) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._dirty = False
            self._last_signature: object | None = None
            self._latest_snapshot: DesktopSnapshot | None = None
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            title = QLabel("设置")
            title.setObjectName("pageTitle")
            layout.addWidget(title)
            server_notice = QLabel(
                "当前企业邮箱账号的业务配置与其他账号隔离，并加密保存在阿里云服务器。"
                "密码、Secret 和 Token 不会下发到"
                "桌面；密码框圆点数量等于服务器保存值的字符数，保留圆点保存不会清除原值。"
            )
            server_notice.setObjectName("sectionHint")
            server_notice.setWordWrap(True)
            layout.addWidget(server_notice)

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            body = QWidget()
            body_layout = QVBoxLayout(body)

            def section(label: str) -> QFormLayout:
                frame = QFrame()
                frame.setStyleSheet(
                    "background: white; border: 1px solid #dfe4ea; border-radius: 6px;"
                )
                section_layout = QVBoxLayout(frame)
                heading = QLabel(label)
                heading.setStyleSheet("font-size: 16px; font-weight: bold; border: 0;")
                section_layout.addWidget(heading)
                form_layout = QFormLayout()
                section_layout.addLayout(form_layout)
                body_layout.addWidget(frame)
                return form_layout

            account_form = section("账号与 API（阿里云服务器加密保存）")
            self.app_id = QLineEdit()
            self.app_secret = QLineEdit()
            self.api_base_url = QLineEdit()
            self.api_base_url.setReadOnly(True)
            self.api_base_url.setToolTip("固定为领星官方 HTTPS OpenAPI，防止凭据发送到错误主机。")
            self.lingxing_account = QLineEdit()
            self.lingxing_password = QLineEdit()
            self.lingxing_remember = QCheckBox("记住领星网页登录状态")
            self.erp_mark_routes = QPlainTextEdit()
            self.erp_mark_routes.setMinimumHeight(130)
            self.erp_mark_routes.setPlaceholderText(
                '{\n  "UPS": {"warehouse_id": 1, "logistics_type_id": 2, '
                '"freight_currency_code": "USD"}\n}'
            )
            self.erp_outbound_strategy = QComboBox()
            self.erp_outbound_strategy.addItem("分阶段审核并出库（推荐）", "staged")
            self.erp_outbound_strategy.addItem("快速出库", "fast_outbound")
            self.alibaba_account = QLineEdit()
            self.alibaba_password = QLineEdit()
            self.alibaba_auto_login = QCheckBox("允许自动登录阿里物流下单账号")
            self.alibaba_logistics_query_account = QLineEdit()
            self.alibaba_logistics_query_password = QLineEdit()
            self.alibaba_logistics_query_auto_login = QCheckBox(
                "允许自动登录阿里物流查询账号"
            )
            self.amazon_client_id = QLineEdit()
            self.amazon_client_secret = QLineEdit()
            self.amazon_refresh_token = QLineEdit()
            self.alimail_application_name = QLineEdit()
            self.alimail_app_id = QLineEdit()
            self.alimail_app_secret = QLineEdit()
            self.alimail_amazon_sender = QLineEdit()
            self.alimail_independent_sender = QLineEdit()
            self.alimail_sender_name = QLineEdit()
            self.clicksend_username = QLineEdit()
            self.clicksend_api_key = QLineEdit()
            self.clicksend_sender_id = QLineEdit()
            self.virtual_email_domains = QPlainTextEdit()
            self.virtual_email_domains.setMinimumHeight(90)
            self.amazon_sandbox = QCheckBox("使用 Amazon SP-API 沙箱")
            self._sensitive_editors = (
                self.app_secret,
                self.lingxing_password,
                self.alibaba_password,
                self.alibaba_logistics_query_password,
                self.amazon_client_secret,
                self.amazon_refresh_token,
                self.alimail_app_secret,
                self.clicksend_username,
                self.clicksend_api_key,
            )
            for editor in self._sensitive_editors:
                editor.setEchoMode(QLineEdit.EchoMode.Password)
            account_form.addRow("领星 AppID", self.app_id)
            account_form.addRow("领星 AppSecret", self.app_secret)
            account_form.addRow("领星 OpenAPI 地址", self.api_base_url)
            account_form.addRow("领星网页账号", self.lingxing_account)
            account_form.addRow("领星网页密码", self.lingxing_password)
            account_form.addRow("领星网页登录", self.lingxing_remember)
            account_form.addRow("ERP 仓库/物流 ID 映射", self.erp_mark_routes)
            account_form.addRow("ERP 出库策略", self.erp_outbound_strategy)
            account_form.addRow("阿里物流下单账号", self.alibaba_account)
            account_form.addRow("阿里物流下单密码", self.alibaba_password)
            account_form.addRow("阿里下单网页登录", self.alibaba_auto_login)
            account_form.addRow(
                "阿里物流查询账号",
                self.alibaba_logistics_query_account,
            )
            account_form.addRow(
                "阿里物流查询密码",
                self.alibaba_logistics_query_password,
            )
            account_form.addRow(
                "阿里查询网页登录",
                self.alibaba_logistics_query_auto_login,
            )
            account_form.addRow("Amazon LWA Client ID", self.amazon_client_id)
            account_form.addRow("Amazon LWA Client Secret", self.amazon_client_secret)
            account_form.addRow("Amazon Refresh Token", self.amazon_refresh_token)
            account_form.addRow("阿里邮箱应用名称", self.alimail_application_name)
            account_form.addRow("阿里邮箱 App ID", self.alimail_app_id)
            account_form.addRow("阿里邮箱 App Secret", self.alimail_app_secret)
            account_form.addRow("Amazon 发件邮箱", self.alimail_amazon_sender)
            account_form.addRow("独立站发件邮箱", self.alimail_independent_sender)
            account_form.addRow("发件人显示名称", self.alimail_sender_name)
            account_form.addRow("ClickSend API Username", self.clicksend_username)
            account_form.addRow("ClickSend API Key", self.clicksend_api_key)
            account_form.addRow("ClickSend Sender ID（可选）", self.clicksend_sender_id)
            account_form.addRow("平台虚拟邮箱域名映射", self.virtual_email_domains)
            account_form.addRow("Amazon 环境", self.amazon_sandbox)

            rule_form = section("定制订单规则")
            self.high_value_split_weight = QComboBox()
            self.high_value_split_weight.addItem("3 kg（超过 3000g 才拆）", 3)
            self.high_value_split_weight.addItem("4 kg（超过 4000g 才拆）", 4)
            self.high_value_split_weight.addItem("5 kg（超过 5000g 才拆）", 5)
            self.high_value_split_weight.setToolTip(
                "仅用于金额大于等于 200 USD/CAD 的美国非加急非帐篷定制订单；"
                "读取领星订单管理接口 logistics_info.pre_weight，"
                "即订单详情“实重”行的预估值，不使用预估计费重。"
            )
            rule_form.addRow(
                "非帐篷高金额订单拆单估重阈值",
                self.high_value_split_weight,
            )

            review_form = section("执行审核")
            self.custom_order_review_enabled = QCheckBox(
                "处理勾选订单前显示审核确认"
            )
            self.custom_order_review_enabled.setToolTip(
                "启用后，每次从定制订单页面提交处理前都需要人工确认；"
                "关闭后直接加入处理队列。"
            )
            self.shipment_review_enabled = QCheckBox(
                "执行标发前显示审核确认"
            )
            self.shipment_review_enabled.setToolTip(
                "启用后，帐篷和非帐篷订单执行自动标发前都需要人工确认；"
                "关闭后直接执行，不再单独弹出非帐篷审核。"
            )
            review_form.addRow("定制订单", self.custom_order_review_enabled)
            review_form.addRow("自动标发", self.shipment_review_enabled)

            path_form = section("路径与运行策略")
            self.folder_root = QLineEdit()
            self.custom_state_path = QLineEdit()
            self.queue_path = QLineEdit()
            self.custom_state_path.setReadOnly(True)
            self.queue_path.setReadOnly(True)
            self.custom_state_path.setToolTip("服务器统一管理的定制订单 SQLite 路径。")
            self.queue_path.setToolTip("服务器统一管理的自动标发 SQLite 路径。")
            self.browser_profile = QLineEdit()
            self.log_dir = QLineEdit()
            self.log_dir.setReadOnly(True)
            self.log_dir.setToolTip("固定为程序目录下的 logs，避免日志清理误删其他目录。")
            self.api_timeout = QSpinBox()
            self.api_timeout.setRange(1, 600)
            self.payment_window = QSpinBox()
            self.payment_window.setRange(96, 96)
            self.payment_window.setToolTip(
                "仅用于定制订单扫描；固定处理最近 96 小时付款订单。自动标发不检查付款时间。"
            )
            self.shipment_tag_name = QLineEdit()
            self.shipment_tag_name.setPlaceholderText("标发")
            self.shipment_tag_name.setToolTip(
                "自动标发扫描只处理带有此领星自定义订单标签的订单；"
                "名称必须与领星中的标签完全一致。"
            )
            self.log_retention = QSpinBox()
            self.log_retention.setRange(90, 90)
            self.browser_fallback = QCheckBox("API 失败后允许经确认使用网页补位")
            self.browser_fallback.setText(
                "联系方式固定走网页；其他网页补位每次询问，写入仅限 API 明确未执行"
            )
            self.browser_fallback.setChecked(True)
            self.browser_fallback.setEnabled(False)
            self.redact_logs = QCheckBox("业务日志脱敏")
            self.redact_logs.setEnabled(False)
            self.redact_logs.setToolTip(
                "固定关闭：姓名、电话、邮箱、地址等业务诊断内容按原值写入本地日志；"
                "认证令牌和密码仍不会写入。"
            )
            path_form.addRow("订单文件夹根目录", self.folder_root)
            path_form.addRow("定制订单状态数据库", self.custom_state_path)
            path_form.addRow("自动标发队列数据库", self.queue_path)
            path_form.addRow("浏览器 Profile", self.browser_profile)
            path_form.addRow("日志目录", self.log_dir)
            path_form.addRow("API 超时（秒）", self.api_timeout)
            path_form.addRow("定制订单付款窗口（固定 96 小时）", self.payment_window)
            path_form.addRow("自动标发扫描标签", self.shipment_tag_name)
            path_form.addRow("日志保留（天）", self.log_retention)
            path_form.addRow("网页补位", self.browser_fallback)
            path_form.addRow("日志脱敏", self.redact_logs)
            path_form.addRow("客户通知", QLabel("扫描仅生成审核草稿；审核通过后真实发送"))

            editors = (
                self.app_id,
                self.app_secret,
                self.api_base_url,
                self.lingxing_account,
                self.lingxing_password,
                self.alibaba_account,
                self.alibaba_password,
                self.alibaba_logistics_query_account,
                self.alibaba_logistics_query_password,
                self.amazon_client_id,
                self.amazon_client_secret,
                self.amazon_refresh_token,
                self.alimail_application_name,
                self.alimail_app_id,
                self.alimail_app_secret,
                self.alimail_amazon_sender,
                self.alimail_independent_sender,
                self.alimail_sender_name,
                self.clicksend_username,
                self.clicksend_api_key,
                self.clicksend_sender_id,
                self.folder_root,
                self.custom_state_path,
                self.queue_path,
                self.browser_profile,
                self.log_dir,
                self.shipment_tag_name,
            )
            for editor in editors:
                if editor in self._sensitive_editors:
                    editor.textEdited.connect(
                        lambda text, current=editor: self._sensitive_text_edited(
                            current,
                            text,
                        )
                    )
                else:
                    editor.textEdited.connect(self._mark_dirty)
            self.erp_mark_routes.textChanged.connect(self._mark_dirty)
            self.virtual_email_domains.textChanged.connect(self._mark_dirty)
            self.erp_outbound_strategy.currentIndexChanged.connect(self._mark_dirty)
            self.high_value_split_weight.currentIndexChanged.connect(
                self._mark_dirty
            )
            for widget in (self.api_timeout, self.payment_window):
                widget.valueChanged.connect(self._mark_dirty)
            for widget in (
                self.lingxing_remember,
                self.alibaba_auto_login,
                self.alibaba_logistics_query_auto_login,
                self.amazon_sandbox,
                self.browser_fallback,
                self.redact_logs,
                self.custom_order_review_enabled,
                self.shipment_review_enabled,
            ):
                widget.toggled.connect(self._mark_dirty)

            actions = QHBoxLayout()
            save_button = QPushButton("保存加密配置")
            save_button.setObjectName("primaryButton")
            save_button.clicked.connect(self._save)
            test_api_button = QPushButton("测试领星 API")
            test_api_button.clicked.connect(self._test_api)
            test_alimail_button = QPushButton("测试阿里邮箱 Token")
            test_alimail_button.clicked.connect(
                lambda: self._test_notification_provider("alimail")
            )
            test_clicksend_button = QPushButton("测试 ClickSend 连接")
            test_clicksend_button.clicked.connect(
                lambda: self._test_notification_provider("clicksend")
            )
            export_button = QPushButton("导出设置与授权")
            export_button.clicked.connect(self._export_portable)
            import_button = QPushButton("导入设置与授权")
            import_button.clicked.connect(self._import_portable)
            for button in (
                save_button,
                test_api_button,
                test_alimail_button,
                test_clicksend_button,
                export_button,
                import_button,
            ):
                actions.addWidget(button)
            actions.addStretch(1)
            body_layout.addLayout(actions)

            body_layout.addStretch(1)
            scroll.setWidget(body)
            layout.addWidget(scroll, 1)

        def _mark_dirty(self, *_args) -> None:
            self._dirty = True

        def _sensitive_text_edited(
            self,
            editor: QLineEdit,
            _edited_text: str,
        ) -> None:
            if bool(editor.property("server_secret_configured")):
                editor.setProperty("server_secret_configured", False)
                editor.setProperty("server_secret_length", None)
                editor.setPlaceholderText("")
                editor.setToolTip("")
            self._mark_dirty()

        @staticmethod
        def _secret_value(editor: QLineEdit) -> str:
            if bool(editor.property("server_secret_configured")):
                return ""
            return editor.text()

        def _save(self) -> None:
            settings = DesktopSettings(
                lingxing_app_id=self.app_id.text().strip(),
                lingxing_app_secret=self._secret_value(self.app_secret),
                lingxing_api_base_url=self.api_base_url.text().strip(),
                lingxing_account=self.lingxing_account.text().strip(),
                lingxing_password=self._secret_value(self.lingxing_password),
                lingxing_remember_login=self.lingxing_remember.isChecked(),
                erp_mark_routes_json=self.erp_mark_routes.toPlainText().strip() or "{}",
                erp_mark_outbound_strategy=str(self.erp_outbound_strategy.currentData()),
                alibaba_account=self.alibaba_account.text().strip(),
                alibaba_password=self._secret_value(self.alibaba_password),
                alibaba_auto_login=self.alibaba_auto_login.isChecked(),
                alibaba_logistics_query_account=(
                    self.alibaba_logistics_query_account.text().strip()
                ),
                alibaba_logistics_query_password=self._secret_value(
                    self.alibaba_logistics_query_password
                ),
                alibaba_logistics_query_auto_login=(
                    self.alibaba_logistics_query_auto_login.isChecked()
                ),
                amazon_lwa_client_id=self.amazon_client_id.text().strip(),
                amazon_lwa_client_secret=self._secret_value(
                    self.amazon_client_secret
                ),
                amazon_refresh_token=self._secret_value(
                    self.amazon_refresh_token
                ),
                amazon_sp_api_sandbox=self.amazon_sandbox.isChecked(),
                alimail_application_name=self.alimail_application_name.text().strip(),
                alimail_app_id=self.alimail_app_id.text().strip(),
                alimail_app_secret=self._secret_value(
                    self.alimail_app_secret
                ),
                alimail_amazon_sender_email=self.alimail_amazon_sender.text().strip(),
                alimail_independent_sender_email=self.alimail_independent_sender.text().strip(),
                alimail_sender_display_name=self.alimail_sender_name.text().strip(),
                clicksend_username=self._secret_value(
                    self.clicksend_username
                ).strip(),
                clicksend_api_key=self._secret_value(
                    self.clicksend_api_key
                ),
                clicksend_sender_id=self.clicksend_sender_id.text().strip(),
                notification_virtual_email_domains_json=(
                    self.virtual_email_domains.toPlainText().strip() or "{}"
                ),
                folder_root=self.folder_root.text().strip(),
                custom_state_path=self.custom_state_path.text().strip(),
                queue_path=self.queue_path.text().strip(),
                browser_profile=self.browser_profile.text().strip(),
                log_dir=self.log_dir.text().strip(),
                api_timeout_seconds=self.api_timeout.value(),
                payment_window_hours=self.payment_window.value(),
                high_value_split_weight_kg=int(
                    self.high_value_split_weight.currentData() or 3
                ),
                shipment_tag_name=self.shipment_tag_name.text().strip(),
                custom_order_review_enabled=(
                    self.custom_order_review_enabled.isChecked()
                ),
                shipment_review_enabled=self.shipment_review_enabled.isChecked(),
                log_retention_days=90,
                browser_fallback_enabled=True,
                redact_sensitive_logs=self.redact_logs.isChecked(),
            )
            def finish(result: ControlResult) -> None:
                if result.accepted:
                    self._dirty = False
                    QMessageBox.information(self, "保存成功", result.message)
                    self._result_handler(result)
                    return
                QMessageBox.warning(self, "保存失败", result.message)
                self._result_handler(
                    ControlResult(
                        False,
                        result.message,
                        result.task_id,
                        details={**dict(result.details), "non_modal": True},
                    )
                )

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.save_settings(settings),
                finish,
            )

        def _test_api(self) -> None:
            if self._dirty:
                self._result_handler(ControlResult(False, "请先保存加密配置，再测试领星 API。"))
                return
            command = TaskCommand(
                name="测试领星 OpenAPI 连接",
                area=TaskArea.MAINTENANCE,
                capability=Capability.LIST_ORDERS,
            )
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                self._result_handler,
            )

        def _test_notification_provider(self, provider: str) -> None:
            if self._dirty:
                self._result_handler(
                    ControlResult(False, "请先保存加密配置，再测试供应商连接。")
                )
                return
            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.test_notification_provider(provider),
                self._result_handler,
            )

        def _ask_passphrase(self, *, confirm: bool) -> str | None:
            first, accepted = QInputDialog.getText(
                self,
                "授权与设置文件密码",
                "输入授权与设置文件密码（至少 12 个字符）：",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return None
            if len(first) < 12:
                self._result_handler(
                    ControlResult(False, "授权与设置文件密码至少需要 12 个字符。")
                )
                return None
            if confirm:
                second, accepted = QInputDialog.getText(
                    self,
                    "确认授权与设置文件密码",
                    "再次输入密码：",
                    QLineEdit.EchoMode.Password,
                )
                if not accepted:
                    return None
                if first != second:
                    self._result_handler(
                        ControlResult(False, "两次输入的授权与设置文件密码不一致。")
                    )
                    return None
            return first

        def _export_portable(self) -> None:
            if self._dirty:
                self._result_handler(
                    ControlResult(
                        False,
                        "当前页面有未保存的修改；请先保存加密配置，再导出。",
                    )
                )
                return
            if self._latest_snapshot is None:
                self._result_handler(
                    ControlResult(False, "服务器设置尚未加载完成，请稍后再导出。")
                )
                return
            export_snapshot = self._latest_snapshot
            if export_snapshot.configuration_is_default:
                answer = QMessageBox.question(
                    self,
                    "配置仍为默认值",
                    "当前登录账号没有检测到自定义设置或已配置凭据。"
                    "继续导出将生成一份只含默认设置的授权文件。是否继续？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            destination, _selected_filter = QFileDialog.getSaveFileName(
                self,
                "导出设置与客户端授权",
                "ERP客户端授权与设置.erp-client",
                "ERP 客户端授权文件 (*.erp-client)",
            )
            if not destination:
                return
            passphrase = self._ask_passphrase(confirm=True)
            if passphrase is None:
                return
            def operation() -> ControlResult:
                try:
                    from erp_automation.coordination.access_profile import (
                        export_client_access_profile,
                    )
                    from erp_automation.coordination.client_bootstrap import (
                        SERVER_HOST,
                        SERVER_USER,
                    )

                    with tempfile.TemporaryDirectory(
                        prefix="erp-client-export-"
                    ) as directory:
                        settings_path = Path(directory) / "settings.erp-migrate"
                        result = self._controller.export_portable_migration(
                            str(settings_path),
                            passphrase,
                            include_state=False,
                        )
                        if not result.accepted:
                            return result
                        state_root = (
                            Path(os.environ.get("LOCALAPPDATA") or Path.home())
                            / "LingxingERP"
                        )
                        export_client_access_profile(
                            destination,
                            passphrase,
                            state_root=state_root,
                            server_host=SERVER_HOST,
                            server_user=SERVER_USER,
                            configuration_package=settings_path.read_bytes(),
                        )
                except Exception as exc:
                    return ControlResult(
                        False,
                        f"导出设置与客户端授权失败：{type(exc).__name__}。",
                    )
                details = dict(result.details)
                source_email = str(
                    details.get("target_operator_email")
                    or export_snapshot.operator_email
                    or "当前登录账号"
                ).strip()
                return ControlResult(
                    True,
                    f"已为 {source_email} 加密导出设置与客户端授权："
                    f"{int(details.get('configured_non_sensitive_field_count') or 0)} "
                    "项非敏感配置，"
                    f"{int(details.get('configured_secret_field_count') or 0)} "
                    "项加密凭据。持有该文件和密码即可访问公司系统，"
                    "请分开保管。",
                    details=details,
                )

            _run_control_result_responsive(
                self,
                self._controller,
                operation,
                self._result_handler,
            )

        def _import_portable(self) -> None:
            source, _selected_filter = QFileDialog.getOpenFileName(
                self,
                "选择设置或客户端授权文件",
                "",
                (
                    "ERP 客户端授权文件 (*.erp-client);;"
                    "ERP 设置包 (*.erp-migrate);;所有文件 (*)"
                ),
            )
            if not source:
                return
            passphrase = self._ask_passphrase(confirm=False)
            if passphrase is None:
                return
            dirty_warning = (
                "当前页面的未保存修改也会被放弃；"
                if self._dirty
                else ""
            )
            answer = QMessageBox.question(
                self,
                "确认导入",
                "如果文件包含设置备份，导入会覆盖当前登录企业邮箱账号的服务器设置；"
                "不包含设置备份时只更新本机授权。原配置和本机授权会保留 .bak。"
                + dirty_warning
                + "客户端授权文件的持有人可以访问公司系统。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            verified_snapshots: list[DesktopSnapshot] = []

            def verify_import(result: ControlResult) -> ControlResult:
                if not result.accepted:
                    return result
                snapshot = self._controller.snapshot()
                details = dict(result.details)
                fingerprint = str(
                    details.get("configuration_fingerprint") or ""
                ).strip().casefold()
                target_email = str(
                    details.get("target_operator_email") or ""
                ).strip().casefold()
                snapshot_email = str(
                    snapshot.operator_email or ""
                ).strip().casefold()
                if (
                    not fingerprint
                    or snapshot.configuration_fingerprint != fingerprint
                ):
                    return ControlResult(
                        False,
                        "服务器已处理导入，但客户端回读指纹校验失败；"
                        "请不要继续修改设置，并联系管理员核对备份。",
                        details=details,
                    )
                if snapshot_email and target_email != snapshot_email:
                    return ControlResult(
                        False,
                        "服务器已处理导入，但客户端回读的企业邮箱身份不一致；"
                        "请联系管理员核对账号隔离。",
                        details=details,
                    )
                expected_counts = (
                    int(details.get("configured_non_sensitive_field_count") or 0),
                    int(details.get("configured_secret_field_count") or 0),
                )
                actual_counts = (
                    snapshot.configured_non_sensitive_field_count,
                    snapshot.configured_secret_field_count,
                )
                if expected_counts != actual_counts:
                    return ControlResult(
                        False,
                        "服务器已处理导入，但配置统计回读校验失败；"
                        "请联系管理员核对备份。",
                        details=details,
                    )
                verified_snapshots[:] = [snapshot]
                display_email = target_email or snapshot_email or "当前登录账号"
                return ControlResult(
                    True,
                    f"已导入到 {display_email}，并通过服务器回读："
                    f"{expected_counts[0]} 项非敏感配置，"
                    f"{expected_counts[1]} 项加密凭据。",
                    details={**details, "configuration_readback_verified": True},
                )

            def operation() -> ControlResult:
                try:
                    source_path = Path(source)
                    if source_path.suffix.casefold() == ".erp-client":
                        from erp_automation.coordination.access_profile import (
                            install_client_access_profile,
                            load_client_access_profile,
                            validate_client_access_profile_identity,
                        )
                        from erp_automation.coordination.client_bootstrap import (
                            SERVER_HOST,
                            SERVER_USER,
                        )

                        profile = load_client_access_profile(
                            source_path,
                            passphrase,
                        )
                        validate_client_access_profile_identity(
                            profile,
                            expected_server_host=SERVER_HOST,
                            expected_server_user=SERVER_USER,
                        )
                        imported_configuration = bool(profile.configuration_package)
                        if imported_configuration:
                            with tempfile.TemporaryDirectory(
                                prefix="erp-client-import-"
                            ) as directory:
                                settings_path = Path(directory) / "settings.erp-migrate"
                                settings_path.write_bytes(
                                    profile.configuration_package
                                )
                                result = self._controller.import_portable_migration(
                                    str(settings_path),
                                    passphrase,
                                    overwrite=True,
                                    configuration_only=True,
                                )
                            result = verify_import(result)
                            if not result.accepted:
                                return result
                        state_root = (
                            Path(os.environ.get("LOCALAPPDATA") or Path.home())
                            / "LingxingERP"
                        )
                        install_client_access_profile(
                            profile,
                            state_root=state_root,
                            expected_server_host=SERVER_HOST,
                            expected_server_user=SERVER_USER,
                        )
                        return ControlResult(
                            True,
                            (
                                result.message + "本机授权也已更新；"
                                if imported_configuration
                                else "本机授权已导入；该文件不含设置备份，服务器设置未改动。"
                            )
                            + "重新启动程序后使用导入的授权。",
                            details=(
                                dict(result.details)
                                if imported_configuration
                                else {}
                            ),
                        )
                    return verify_import(
                        self._controller.import_portable_migration(
                            source,
                            passphrase,
                            overwrite=True,
                            configuration_only=True,
                        )
                    )
                except Exception as exc:
                    detail = " ".join(str(exc).split())[:500] or type(exc).__name__
                    return ControlResult(
                        False,
                        f"导入设置与客户端授权失败：{detail}",
                    )

            def finish(result: ControlResult) -> None:
                if result.accepted and verified_snapshots:
                    self._dirty = False
                    self._last_signature = None
                    self.update_snapshot(verified_snapshots[-1])
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                operation,
                finish,
            )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._latest_snapshot = snapshot
            signature = (
                snapshot.settings,
                tuple(sorted(snapshot.configured_secret_lengths.items())),
                snapshot.configuration_fingerprint,
                snapshot.configured_non_sensitive_field_count,
                snapshot.configured_secret_field_count,
                snapshot.operator_email,
            )
            if self._dirty:
                return
            if signature == self._last_signature:
                return
            if not self._dirty:
                settings = snapshot.settings
                widgets = (
                    (self.app_id, settings.lingxing_app_id, ""),
                    (
                        self.app_secret,
                        settings.lingxing_app_secret,
                        "lingxing_app_secret",
                    ),
                    (self.api_base_url, settings.lingxing_api_base_url, ""),
                    (self.lingxing_account, settings.lingxing_account, ""),
                    (
                        self.lingxing_password,
                        settings.lingxing_password,
                        "lingxing_password",
                    ),
                    (self.alibaba_account, settings.alibaba_account, ""),
                    (
                        self.alibaba_password,
                        settings.alibaba_password,
                        "alibaba_password",
                    ),
                    (
                        self.alibaba_logistics_query_account,
                        settings.alibaba_logistics_query_account,
                        "",
                    ),
                    (
                        self.alibaba_logistics_query_password,
                        settings.alibaba_logistics_query_password,
                        "alibaba_logistics_query_password",
                    ),
                    (self.amazon_client_id, settings.amazon_lwa_client_id, ""),
                    (
                        self.amazon_client_secret,
                        settings.amazon_lwa_client_secret,
                        "amazon_lwa_client_secret",
                    ),
                    (
                        self.amazon_refresh_token,
                        settings.amazon_refresh_token,
                        "amazon_refresh_token",
                    ),
                    (
                        self.alimail_application_name,
                        settings.alimail_application_name,
                        "",
                    ),
                    (self.alimail_app_id, settings.alimail_app_id, ""),
                    (
                        self.alimail_app_secret,
                        settings.alimail_app_secret,
                        "alimail_app_secret",
                    ),
                    (
                        self.alimail_amazon_sender,
                        settings.alimail_amazon_sender_email,
                        "",
                    ),
                    (
                        self.alimail_independent_sender,
                        settings.alimail_independent_sender_email,
                        "",
                    ),
                    (
                        self.alimail_sender_name,
                        settings.alimail_sender_display_name,
                        "",
                    ),
                    (
                        self.clicksend_username,
                        settings.clicksend_username,
                        "clicksend_username",
                    ),
                    (
                        self.clicksend_api_key,
                        settings.clicksend_api_key,
                        "clicksend_api_key",
                    ),
                    (self.clicksend_sender_id, settings.clicksend_sender_id, ""),
                    (self.folder_root, settings.folder_root, ""),
                    (self.custom_state_path, settings.custom_state_path, ""),
                    (self.queue_path, settings.queue_path, ""),
                    (self.browser_profile, settings.browser_profile, ""),
                    (self.log_dir, settings.log_dir, ""),
                    (self.shipment_tag_name, settings.shipment_tag_name, ""),
                )
                for widget, value, secret_name in widgets:
                    if (
                        widget in self._sensitive_editors
                        and value == SERVER_CONFIGURED_SECRET
                    ):
                        secret_length = snapshot.configured_secret_lengths.get(
                            secret_name
                        )
                        widget.setProperty(
                            "server_secret_configured",
                            True,
                        )
                        widget.setProperty(
                            "server_secret_length",
                            secret_length,
                        )
                        widget.setText("")
                        if secret_length is None:
                            widget.setPlaceholderText(
                                "已配置（请更新服务端）"
                            )
                            widget.setToolTip(
                                "服务器已加密保存，但当前快照未提供字符数。"
                                "保留空白保存不会修改原值。"
                            )
                        else:
                            widget.setPlaceholderText("●" * secret_length)
                            widget.setToolTip(
                                f"服务器已加密保存，共 {secret_length} 个字符；"
                                "客户端未接收明文。保留圆点保存不会修改原值。"
                            )
                    else:
                        if widget in self._sensitive_editors:
                            widget.setProperty(
                                "server_secret_configured",
                                False,
                            )
                            widget.setProperty("server_secret_length", None)
                            widget.setToolTip("")
                        widget.setText(value)
                        if widget in self._sensitive_editors:
                            widget.setPlaceholderText(
                                "" if value else "尚未配置"
                            )
                self.api_timeout.setValue(settings.api_timeout_seconds)
                self.erp_mark_routes.setPlainText(settings.erp_mark_routes_json)
                self.virtual_email_domains.setPlainText(
                    settings.notification_virtual_email_domains_json
                )
                strategy_index = self.erp_outbound_strategy.findData(
                    settings.erp_mark_outbound_strategy
                )
                self.erp_outbound_strategy.setCurrentIndex(max(0, strategy_index))
                self.payment_window.setValue(settings.payment_window_hours)
                weight_index = self.high_value_split_weight.findData(
                    settings.high_value_split_weight_kg
                )
                self.high_value_split_weight.setCurrentIndex(
                    max(0, weight_index)
                )
                self.log_retention.setValue(90)
                self.lingxing_remember.setChecked(settings.lingxing_remember_login)
                self.alibaba_auto_login.setChecked(settings.alibaba_auto_login)
                self.alibaba_logistics_query_auto_login.setChecked(
                    settings.alibaba_logistics_query_auto_login
                )
                self.amazon_sandbox.setChecked(settings.amazon_sp_api_sandbox)
                self.browser_fallback.setChecked(True)
                self.redact_logs.setChecked(settings.redact_sensitive_logs)
                self.custom_order_review_enabled.setChecked(
                    settings.custom_order_review_enabled
                )
                self.shipment_review_enabled.setChecked(
                    settings.shipment_review_enabled
                )
                self._dirty = False
                self._last_signature = signature


    class _NotificationStatusDialog(QDialog):
        ACTIONS = (
            ("人工完成", "MANUALLY_COMPLETED"),
            ("已取消", "CANCELLED"),
            ("待审核（重新提交）", "AWAITING_REVIEW"),
        )

        def __init__(
            self,
            notification_count: int,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle("修改通知状态")
            self.setMinimumWidth(360)
            layout = QVBoxLayout(self)
            layout.addWidget(
                QLabel(f"请选择 {notification_count} 条通知的新状态：")
            )
            self.status_combo = QComboBox()
            for label, value in self.ACTIONS:
                self.status_combo.addItem(label, value)
            layout.addWidget(self.status_combo)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def selected_value(self) -> str:
            return str(self.status_combo.currentData() or "")


    class _NotificationPackageLogisticsDialog(QDialog):
        CARRIERS = (
            "UPS",
            "FedEx",
            "USPS",
            "DHL",
            "GOFO",
            "Yanwen",
            "SpeedX",
            "UniUni",
            "1ST",
            "SwiftX",
            "Wanb Express",
            "Canada Post",
            "Aramex",
            "4PX",
            "SF International",
            "YunExpress",
            "China Post",
            "J&T Express",
            "Cainiao",
        )

        def __init__(
            self,
            platform_order_no: str,
            packages: Sequence[Mapping[str, object]],
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.setWindowTitle("修改客户通知包裹物流")
            self.setMinimumWidth(620)
            self._packages = [dict(item) for item in packages]
            layout = QVBoxLayout(self)
            hint = QLabel(
                f"平台单号：{platform_order_no or '-'}\n"
                "请选择一个包裹，填写已人工核对的尾程承运商和物流单号。"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            form = QFormLayout()
            self.package_combo = QComboBox()
            for package in self._packages:
                label = str(
                    package.get("display_label")
                    or package.get("stable_label")
                    or package.get("stable_sequence")
                    or "-"
                )
                system_order_no = str(package.get("system_order_no") or "-")
                carrier = str(
                    package.get("carrier")
                    or package.get("carrier_normalized")
                    or package.get("carrier_raw")
                    or "-"
                )
                tracking = str(package.get("final_tracking_no") or "-")
                self.package_combo.addItem(
                    f"Package {label} · {system_order_no} · {carrier} {tracking}",
                    str(package.get("package_key") or ""),
                )
            self.carrier_combo = QComboBox()
            self.carrier_combo.setEditable(True)
            for carrier in self.CARRIERS:
                self.carrier_combo.addItem(carrier, carrier)
            self.tracking_edit = QLineEdit()
            self.tracking_edit.setClearButtonEnabled(True)
            self.tracking_edit.setPlaceholderText("客户可查询的真实尾程物流单号")
            self.reason_edit = QLineEdit()
            self.reason_edit.setClearButtonEnabled(True)
            self.reason_edit.setPlaceholderText("必填；例如：已在 USPS 官网核对轨迹")
            form.addRow("包裹", self.package_combo)
            form.addRow("承运商", self.carrier_combo)
            form.addRow("物流单号", self.tracking_edit)
            form.addRow("修改原因", self.reason_edit)
            layout.addLayout(form)
            warning = QLabel(
                "保存后会保留原始 WMS 值和人工审计，后续自动扫描不会覆盖"
                "这次修正。当前通知会失效并生成新的待审核版本；"
                "不会发送邮件或短信，也不会写入 ERP。"
            )
            warning.setObjectName("warningText")
            warning.setWordWrap(True)
            layout.addWidget(warning)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存并重新生成待审核通知")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)
            self.package_combo.currentIndexChanged.connect(
                self._load_selected_package
            )
            self._load_selected_package(0)

        def _load_selected_package(self, index: int) -> None:
            if not 0 <= index < len(self._packages):
                return
            package = self._packages[index]
            carrier = str(
                package.get("carrier")
                or package.get("carrier_normalized")
                or package.get("carrier_raw")
                or ""
            ).strip()
            carrier_index = self.carrier_combo.findText(
                carrier,
                Qt.MatchFlag.MatchFixedString,
            )
            if carrier_index >= 0:
                self.carrier_combo.setCurrentIndex(carrier_index)
            else:
                self.carrier_combo.setEditText(carrier)
            self.tracking_edit.setText(
                str(package.get("final_tracking_no") or "").strip()
            )

        def values(self) -> tuple[str, str, str, str]:
            return (
                str(self.package_combo.currentData() or "").strip(),
                self.carrier_combo.currentText().strip(),
                self.tracking_edit.text().strip(),
                self.reason_edit.text().strip(),
            )


    class ShipmentNotificationPage(QWidget):
        def __init__(
            self,
            controller: BackgroundTaskController,
            result_handler: ResultHandler,
        ) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler
            self._notifications: list[dict[str, object]] = []
            self._visible_notifications: list[dict[str, object]] = []
            self._notification_ids: frozenset[int] = frozenset()
            self._visible_notification_ids: frozenset[int] = frozenset()
            self._visible_awaiting_review_ids_cache: frozenset[int] = frozenset()
            self._selected_id: int | None = None
            self._checked_notification_ids: set[int] = set()
            self._row_index_by_notification_id: dict[int, int] = {}
            self._batch_send_thread: _ControlResultThread | None = None
            self._notification_reload_thread: _ValueThread | None = None
            self._notification_prefetch_thread: _ValueThread | None = None
            self._notification_reload_queued = False
            self._notification_page_cache: dict[
                tuple[object, ...],
                object,
            ] = {}
            self._notification_detail_thread: _ValueThread | None = None
            self._notification_detail_loading_id: int | None = None
            self._notification_detail_queued_id: int | None = None
            self._notification_detail_failed_ids: set[int] = set()
            self._notification_action_detail_thread: _ValueThread | None = None
            self._notifications_loaded = False
            self._notification_page = 1
            self._notification_page_size = 50
            self._notification_total = 0
            self._notification_total_pages = 1
            self._notification_data_task_states: dict[str, TaskStatus] = {}
            self._notification_send_task_id: str | None = None
            self._optimistic_send_notification_ids: set[int] = set()
            self._active_notification_send_task_ids: tuple[str, ...] = ()
            self._active_task_ids_by_notification_id: dict[
                int,
                tuple[str, ...],
            ] = {}
            self._active_tasks_by_notification_id: dict[
                int,
                tuple[TaskRecord, ...],
            ] = {}
            self._active_notification_ids_by_task_id: dict[
                str,
                tuple[int, ...],
            ] = {}
            self._active_page_task_ids: tuple[str, ...] = ()
            self._contact_refresh_task_id: str | None = None

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(10)
            heading_row = QHBoxLayout()
            heading_row.setSpacing(8)
            title = QLabel("客户通知审核")
            title.setObjectName("pageTitle")
            heading_row.addWidget(title)
            heading_row.addStretch(1)
            self.receipt_button = QPushButton("刷新发送状态")
            self.receipt_button.setToolTip(
                "查询阿里邮箱或 ClickSend 已接收通知的最新发送状态，不会重新发送"
            )
            self.receipt_button.clicked.connect(self._refresh_receipts)
            self.rescan_button = QPushButton("扫描订单并同步物流")
            self.rescan_button.setToolTip(
                "扫描最近 30 天 Amazon 订单，并更新自动标发来源订单的物流；"
                "不会写入 ERP、调用 Alibaba 或直接发送客户通知。"
            )
            self.rescan_button.clicked.connect(self._rescan)
            for button in (self.receipt_button, self.rescan_button):
                size_policy = button.sizePolicy()
                size_policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
                button.setSizePolicy(size_policy)
                heading_row.addWidget(button)
            self._page_action_row_layout = heading_row
            layout.addLayout(heading_row)
            hint = QLabel(
                "自动扫描只采集联系方式和物流并生成草稿。首次发送、补齐物流后的再次发送，"
                "都必须在此页人工审核；只有“审核通过并发送”会调用外部 API。"
            )
            hint.setObjectName("queueStatusBanner")
            hint.setWordWrap(True)
            layout.addWidget(hint)

            self.product_type_filter_combo = _ProductTypeFilterCombo()
            self.product_type_filter_combo.setMinimumWidth(180)
            self.product_type_filter_combo.setMaximumWidth(300)
            self.product_type_filter_combo.selection_changed.connect(
                self._apply_search_filter
            )
            self.search_field_combo = QComboBox()
            for value, label in (
                ("all", "全部字段"),
                ("platform_order_no", "平台单号"),
                ("recipient_name", "收件人"),
                ("recipient_email", "邮箱"),
                ("recipient_phone", "电话"),
                ("state", "状态"),
            ):
                self.search_field_combo.addItem(label, value)
            self.search_field_combo.setMinimumWidth(128)
            self.search_field_combo.setMaximumWidth(160)
            self.search_edit = QLineEdit()
            self.search_edit.setPlaceholderText("输入完整或部分内容搜索客户通知队列")
            self.search_edit.setClearButtonEnabled(True)
            self.search_edit.setMinimumWidth(180)
            self.search_edit.setMaximumWidth(520)
            search_size_policy = self.search_edit.sizePolicy()
            search_size_policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
            self.search_edit.setSizePolicy(search_size_policy)
            self.search_field_combo.currentIndexChanged.connect(
                self._apply_search_filter
            )
            self.search_edit.textChanged.connect(self._apply_search_filter)
            self.approve_button = QPushButton("审核通过并发送")
            self.approve_button.setObjectName("primaryButton")
            self.approve_button.clicked.connect(self._approve)
            self.quick_select_review_button = QPushButton("勾选待审核（0）")
            self.quick_select_review_button.setObjectName("quickSelectButton")
            self.quick_select_review_button.setToolTip(
                "只勾选当前筛选结果中可自动发送的待审核通知；"
                "需人工发送邮件等状态不会被自动勾选"
            )
            self.quick_select_review_button.clicked.connect(
                self._select_visible_awaiting_review
            )

            filter_panel = QFrame()
            filter_panel.setObjectName("queueFilterPanel")
            filter_grid = QGridLayout(filter_panel)
            filter_grid.setContentsMargins(12, 10, 12, 10)
            filter_grid.setHorizontalSpacing(10)
            filter_grid.setVerticalSpacing(7)
            product_filter_label = QLabel("商品类型")
            product_filter_label.setObjectName("queueFilterLabel")
            search_filter_label = QLabel("搜索通知")
            search_filter_label.setObjectName("queueFilterLabel")
            filter_grid.addWidget(product_filter_label, 0, 0)
            filter_grid.addWidget(self.product_type_filter_combo, 0, 1)
            filter_grid.addWidget(search_filter_label, 1, 0)
            filter_grid.addWidget(self.search_field_combo, 1, 1)
            filter_grid.addWidget(self.search_edit, 1, 2, 1, 3)
            filter_grid.setColumnStretch(4, 1)
            self._filter_contact_row_layout = filter_grid
            self._filter_row_layout = filter_grid
            layout.addWidget(filter_panel)

            batch_bar = QFrame()
            batch_bar.setObjectName("queueBatchBar")
            batch_actions = QHBoxLayout(batch_bar)
            batch_actions.setContentsMargins(10, 7, 10, 7)
            batch_actions.setSpacing(8)
            self.notification_selection_summary = QLabel(
                "显示 0 · 待审核 0 · 已选 0"
            )
            self.notification_selection_summary.setObjectName(
                "queueSelectionSummary"
            )
            batch_actions.addWidget(self.notification_selection_summary)
            batch_actions.addStretch(1)
            batch_actions.addWidget(self.quick_select_review_button)
            self.notification_more_actions_button = QPushButton("更多批量操作")
            self.notification_more_actions_menu = QMenu(
                self.notification_more_actions_button
            )
            self.contact_refresh_action = (
                self.notification_more_actions_menu.addAction(
                    "从定制 JSON 获取联系方式"
                )
            )
            self.contact_refresh_action.setToolTip(
                "从订单文件夹内与平台单号匹配的定制 JSON 重新读取邮箱和电话；"
                "没有勾选时处理当前选中行。不会请求领星、写入 ERP 或发送通知。"
            )
            self.contact_refresh_action.triggered.connect(
                lambda _checked=False: self._refresh_contacts()
            )
            self.edit_contact_action = (
                self.notification_more_actions_menu.addAction("修改联系方式")
            )
            self.edit_contact_action.setToolTip(
                "手动补充或修正当前通知的邮箱和电话；人工值不会被后续自动扫描覆盖"
            )
            self.edit_contact_action.triggered.connect(
                lambda _checked=False: self._edit_contact()
            )
            self.edit_package_logistics_action = (
                self.notification_more_actions_menu.addAction(
                    "修改包裹承运商和物流单号"
                )
            )
            self.edit_package_logistics_action.setToolTip(
                "人工修正当前选中订单的任一客户包裹；"
                "保存后生成新的待审核通知，不会立即发送"
            )
            self.edit_package_logistics_action.triggered.connect(
                lambda _checked=False: self._edit_package_logistics()
            )
            self.notification_more_actions_menu.addSeparator()
            self.resubmit_action = self.notification_more_actions_menu.addAction(
                "重新提交审核"
            )
            self.resubmit_action.triggered.connect(
                lambda _checked=False: self._resubmit()
            )
            self.retry_notification_action = (
                self.notification_more_actions_menu.addAction("重试已批准内容")
            )
            self.retry_notification_action.triggered.connect(
                lambda _checked=False: self._retry()
            )
            self.change_notification_status_action = (
                self.notification_more_actions_menu.addAction("修改状态")
            )
            self.change_notification_status_action.triggered.connect(
                lambda _checked=False: self._change_status()
            )
            self.notification_more_actions_menu.addSeparator()
            self.stop_tasks_action = self.notification_more_actions_menu.addAction(
                "停止当前勾选任务"
            )
            self.stop_tasks_action.triggered.connect(
                lambda _checked=False: self._stop_checked_tasks()
            )
            self.notification_more_actions_button.setMenu(
                self.notification_more_actions_menu
            )
            batch_actions.addWidget(self.notification_more_actions_button)
            batch_actions.addWidget(self.approve_button)
            self._notification_action_row_layout = batch_actions
            self._batch_action_row_layout = batch_actions
            layout.addWidget(batch_bar)

            self.pagination_bar = _QueuePaginationBar()
            self.notification_page_status = self.pagination_bar.total_label
            self.notification_previous_page_button = (
                self.pagination_bar.previous_button
            )
            self.notification_next_page_button = self.pagination_bar.next_button
            self.notification_page_size_combo = self.pagination_bar.page_size_combo
            self.notification_jump_page_spin = self.pagination_bar.jump_spin
            self.pagination_bar.page_requested.connect(
                self._show_notification_page
            )
            self.pagination_bar.page_size_changed.connect(
                self._change_notification_page_size
            )

            splitter = QSplitter(Qt.Orientation.Vertical)
            self.table = QTableWidget(0, 10)
            self._check_header = _CheckableHeaderView(self.table)
            self.table.setHorizontalHeader(self._check_header)
            self.table.setHorizontalHeaderLabels(
                [
                    "",
                    "平台单号",
                    "商品类型",
                    "收件人",
                    "邮箱",
                    "电话",
                    "包裹（已有/总数）",
                    "状态时间",
                    "状态",
                    "状态说明",
                ]
            )
            _prepare_table(self.table, full_cell_check_column=0)
            _enable_table_copy(self.table)
            _set_table_default_widths(
                self.table,
                (40, 160, 100, 120, 180, 120, 130, 110, 110, 360),
            )
            self._check_header.check_state_changed.connect(self._set_all_checked)
            self.table.itemChanged.connect(self._on_item_changed)
            self.table.cellClicked.connect(self._on_notification_clicked)
            self.table.itemSelectionChanged.connect(self._show_selected)
            splitter.addWidget(self.table)

            detail = QWidget()
            detail_layout = QVBoxLayout(detail)
            detail_layout.setContentsMargins(0, 8, 0, 0)
            self.summary = QLabel("请选择一条通知。")
            self.summary.setWordWrap(True)
            detail_layout.addWidget(self.summary)
            self.package_table = QTableWidget(0, 7)
            self.package_table.setHorizontalHeaderLabels(
                [
                    "序号",
                    "字母",
                    "系统单号",
                    "面单来源",
                    "承运商",
                    "邮件发送单号",
                    "物流状态",
                ]
            )
            _prepare_table(self.package_table)
            _enable_table_copy(self.package_table)
            _set_table_default_widths(
                self.package_table,
                (60, 60, 150, 120, 100, 180, 140),
                unscaled_columns=(0, 1),
            )
            detail_layout.addWidget(self.package_table)
            splitter.addWidget(detail)
            splitter.setSizes([360, 320])
            layout.addWidget(splitter, 1)
            layout.addWidget(self.pagination_bar)
            self._receipt_ui_refresh_timer = QTimer(self)
            self._receipt_ui_refresh_timer.setInterval(
                _NOTIFICATION_RECEIPT_UI_REFRESH_INTERVAL_MS
            )
            self._receipt_ui_refresh_timer.timeout.connect(
                self._reload_pending_receipt_states
            )
            self._receipt_ui_refresh_timer.start()
            self._notification_filter_timer = QTimer(self)
            self._notification_filter_timer.setSingleShot(True)
            self._notification_filter_timer.setInterval(250)
            self._notification_filter_timer.timeout.connect(self._reload)
            if getattr(self._controller, "snapshot_runs_in_background", False):
                # Warm the coordinator connection and queue page while the
                # user is still on the default page. The first navigation then
                # paints already-fetched rows and refreshes in the background.
                QTimer.singleShot(0, self._reload)

        def showEvent(self, event) -> None:  # noqa: N802 - Qt callback name
            super().showEvent(event)
            QTimer.singleShot(0, self._reload)

        def _selected(self) -> dict[str, object] | None:
            row = self.table.currentRow()
            if row < 0:
                return None
            item = self.table.item(row, 0)
            notification_id = item.data(Qt.ItemDataRole.UserRole) if item else None
            return next(
                (
                    notification
                    for notification in self._notifications
                    if int(notification.get("id") or 0) == int(notification_id or 0)
                ),
                None,
            )

        def _active_task_for_notification(
            self,
            notification_id: int,
        ) -> TaskRecord | None:
            tasks = self._active_tasks_by_notification_id.get(
                int(notification_id),
                (),
            )
            return tasks[0] if tasks else None

        def _notification_sort_key(
            self,
            notification: Mapping[str, object],
        ) -> tuple[int, float, int]:
            notification_id = int(notification.get("id") or 0)
            return _notification_queue_sort_key(
                notification,
                active=(
                    notification_id in self._active_tasks_by_notification_id
                    or notification_id in self._optimistic_send_notification_ids
                ),
            )

        def _show_notification_queue_conflict(
            self,
            notification: Mapping[str, object],
            *,
            task: TaskRecord | None = None,
        ) -> None:
            notification_id = int(notification.get("id") or 0)
            active_task = task or self._active_task_for_notification(
                notification_id
            )
            show_queue_conflict_dialog(
                order_no=str(notification.get("platform_order_no") or "-"),
                task_name=(
                    active_task.name
                    if active_task is not None
                    else "客户通知处理任务"
                ),
                task_status=(
                    active_task.status.label
                    if active_task is not None
                    else "已进入处理队列"
                ),
                operator_name=(
                    active_task.operator_name
                    if active_task is not None
                    else ""
                ),
                operator_email=(
                    active_task.operator_email
                    if active_task is not None
                    else ""
                ),
                parent=self,
            )

        def _show_submission_queue_conflict(
            self,
            result: ControlResult,
            notifications: Sequence[Mapping[str, object]],
        ) -> bool:
            if not bool(result.details.get("queue_conflict")):
                return False
            conflict_ids = {
                int(value)
                for value in result.details.get("conflict_notification_ids", ())
                if str(value).isdigit()
            }
            notification = next(
                (
                    item
                    for item in notifications
                    if int(item.get("id") or 0) in conflict_ids
                ),
                notifications[0] if notifications else {},
            )
            show_queue_conflict_dialog(
                order_no=str(notification.get("platform_order_no") or "-"),
                task_name=str(
                    result.details.get("conflict_task_name")
                    or "客户通知处理任务"
                ),
                task_status=str(
                    result.details.get("conflict_task_status")
                    or "已进入处理队列"
                ),
                operator_name=str(
                    result.details.get("conflict_operator_name") or ""
                ),
                operator_email=str(
                    result.details.get("conflict_operator_email") or ""
                ),
                parent=self,
            )
            self._result_handler(
                ControlResult(
                    False,
                    result.message,
                    result.task_id,
                    details={**dict(result.details), "non_modal": True},
                )
            )
            return True

        def _on_notification_clicked(self, row: int, column: int) -> None:
            if column == 0:
                return
            item = self.table.item(row, 0)
            notification_id = int(
                item.data(Qt.ItemDataRole.UserRole) or 0
            ) if item is not None else 0
            task = self._active_task_for_notification(notification_id)
            if task is None:
                return
            notification = next(
                (
                    value
                    for value in self._notifications
                    if int(value.get("id") or 0) == notification_id
                ),
                None,
            )
            if notification is not None:
                self._show_notification_queue_conflict(
                    notification,
                    task=task,
                )

        def _reload(self) -> None:
            query = self._notification_page_query()
            request_key = self._notification_page_cache_key(query)
            operation = lambda: self._load_notification_page_query(query)
            if getattr(self._controller, "snapshot_runs_in_background", False):
                if (
                    self._notification_reload_thread is not None
                    and self._notification_reload_thread.isRunning()
                ):
                    self._notification_reload_queued = True
                    return
                self._notification_reload_queued = False
                thread = _ValueThread(
                    operation,
                    self,
                )
                thread.value_ready.connect(
                    lambda value, key=request_key: self._apply_notification_reload_for_request(
                        key,
                        value,
                    )
                )
                thread.value_failed.connect(
                    lambda error, key=request_key: self._notification_reload_failed_for_request(
                        key,
                        error,
                    )
                )
                thread.finished.connect(self._notification_reload_finished)
                self._notification_reload_thread = thread
                thread.start()
                return
            value = operation()
            self._cache_notification_page(request_key, value)
            self._apply_notification_reload(value)

        def _active_notification_sort_ids(self) -> tuple[int, ...]:
            return tuple(
                sorted(
                    set(self._active_tasks_by_notification_id)
                    | self._optimistic_send_notification_ids
                )[:100]
            )

        def _notification_page_query(self, page: int | None = None) -> dict[str, object]:
            return {
                "page": self._notification_page if page is None else max(1, int(page)),
                "page_size": self._notification_page_size,
                "search_field": str(
                    self.search_field_combo.currentData() or "all"
                ),
                "search_query": self.search_edit.text().strip(),
                "product_types": tuple(
                    self.product_type_filter_combo.selected_values
                ),
                "active_notification_ids": self._active_notification_sort_ids(),
            }

        @staticmethod
        def _notification_page_cache_key(
            query: Mapping[str, object],
        ) -> tuple[object, ...]:
            return (
                int(query.get("page") or 1),
                int(query.get("page_size") or 50),
                str(query.get("search_field") or "all"),
                str(query.get("search_query") or ""),
                tuple(query.get("product_types") or ()),
                tuple(query.get("active_notification_ids") or ()),
            )

        def _cache_notification_page(
            self,
            key: tuple[object, ...],
            value: object,
        ) -> None:
            if not isinstance(value, Mapping):
                return
            self._notification_page_cache.pop(key, None)
            self._notification_page_cache[key] = value
            while len(self._notification_page_cache) > 8:
                self._notification_page_cache.pop(next(iter(self._notification_page_cache)))

        def _apply_notification_reload_for_request(
            self,
            request_key: tuple[object, ...],
            value: object,
        ) -> None:
            self._cache_notification_page(request_key, value)
            current_key = self._notification_page_cache_key(
                self._notification_page_query()
            )
            if request_key != current_key:
                return
            self._apply_notification_reload(value)

        def _notification_reload_failed_for_request(
            self,
            request_key: tuple[object, ...],
            error: object,
        ) -> None:
            current_key = self._notification_page_cache_key(
                self._notification_page_query()
            )
            if request_key == current_key:
                self._notification_reload_failed(error)

        def _load_notification_page(self) -> object:
            return self._load_notification_page_query(
                self._notification_page_query()
            )

        def _load_notification_page_query(
            self,
            query: Mapping[str, object],
        ) -> object:
            method = self._controller.list_shipment_notifications
            try:
                return method(**dict(query))
            except TypeError as exc:
                # Compatibility for local test doubles and an older in-process
                # controller. Remote RPC validation errors are not TypeError.
                message = str(exc).casefold()
                if "unexpected keyword" not in message and "positional" not in message:
                    raise
                return method()

        def _show_previous_notification_page(self) -> None:
            self._show_notification_page(self._notification_page - 1)

        def _show_next_notification_page(self) -> None:
            self._show_notification_page(self._notification_page + 1)

        def _show_notification_page(self, page: int) -> None:
            target = max(
                1,
                min(int(page), self._notification_total_pages),
            )
            if target == self._notification_page:
                return
            self._notification_page = target
            query = self._notification_page_query()
            cache_key = self._notification_page_cache_key(query)
            cached = self._notification_page_cache.get(cache_key)
            if cached is not None:
                self._apply_notification_reload(cached)
            else:
                self.notification_page_status.setText(f"正在加载第 {target} 页…")
            self._reload()

        def _change_notification_page_size(self, page_size: int) -> None:
            normalized = int(page_size)
            if normalized == self._notification_page_size:
                return
            self._notification_page_size = normalized
            self._notification_page = 1
            self._reload()

        def _update_notification_pagination(self) -> None:
            self.pagination_bar.set_state(
                total=self._notification_total,
                page=self._notification_page,
                page_size=self._notification_page_size,
                page_count=self._notification_total_pages,
            )

        def _reload_pending_receipt_states(self) -> None:
            """Observe coordinator-owned receipt updates without manual refresh."""

            if not self.isVisible():
                return
            receipt_pending_states = {
                "ACCEPTED",
                "FAILED",
                "DELIVERY_UNCONFIRMED",
            }
            if self._active_notification_send_task_ids or any(
                str(item.get("state") or "") in receipt_pending_states
                and str(item.get("provider_message_id") or "").strip()
                for item in self._notifications
            ):
                self._reload()

        def _apply_notification_reload(self, value: object) -> None:
            page_payload = value if isinstance(value, Mapping) else None
            raw_notifications = (
                page_payload.get("items") if page_payload is not None else value
            )
            notifications = (
                list(raw_notifications)
                if isinstance(raw_notifications, Sequence)
                and not isinstance(raw_notifications, (str, bytes))
                else []
            )
            ordered = sorted(
                (
                    dict(item)
                    for item in notifications
                    if isinstance(item, Mapping)
                ),
                key=self._notification_sort_key,
            )
            for item in ordered:
                if "detail_loaded" in item:
                    item["_detail_loaded"] = bool(item.get("detail_loaded"))
            self._notification_detail_failed_ids.clear()
            unchanged = self._notifications_loaded and ordered == self._notifications
            self._notifications_loaded = True
            if page_payload is not None:
                self._notification_page = max(
                    1, int(page_payload.get("page") or self._notification_page)
                )
                self._notification_page_size = min(
                    100,
                    max(
                        1,
                        int(
                            page_payload.get("page_size")
                            or self._notification_page_size
                        ),
                    ),
                )
                self._notification_total = max(
                    0, int(page_payload.get("total") or 0)
                )
                self._notification_total_pages = max(
                    1, int(page_payload.get("total_pages") or 1)
                )
                available_product_types = page_payload.get("product_types") or ()
            else:
                self._notification_page = 1
                self._notification_total = len(ordered)
                self._notification_total_pages = 1
                available_product_types = [
                    product_type
                    for notification in ordered
                    for product_type in _product_type_values(notification)
                ]
            self._update_notification_pagination()
            self._prefetch_next_notification_page()
            if unchanged:
                return
            current_states = {
                int(item.get("id") or 0): str(item.get("state") or "")
                for item in ordered
            }
            self._optimistic_send_notification_ids.intersection_update(
                notification_id
                for notification_id, state in current_states.items()
                if state in {"AWAITING_REVIEW", "RETRYABLE"}
            )
            selected_id = self._selected_id
            selected_column = self.table.currentColumn()
            self._notifications = ordered
            self._notification_ids = frozenset(
                int(item.get("id") or 0)
                for item in ordered
                if int(item.get("id") or 0) > 0
            )
            self.product_type_filter_combo.set_available_values(
                available_product_types
            )
            eligible_ids = self._eligible_notification_ids()
            self._checked_notification_ids.intersection_update(eligible_ids)
            self._render_notifications(
                selected_id=selected_id,
                selected_column=selected_column,
            )

        def _prefetch_next_notification_page(self) -> None:
            if not getattr(self._controller, "snapshot_runs_in_background", False):
                return
            next_page = self._notification_page + 1
            if next_page > self._notification_total_pages:
                return
            query = self._notification_page_query(next_page)
            cache_key = self._notification_page_cache_key(query)
            if cache_key in self._notification_page_cache:
                return
            if (
                self._notification_prefetch_thread is not None
                and self._notification_prefetch_thread.isRunning()
            ):
                return
            thread = _ValueThread(
                lambda: self._load_notification_page_query(query),
                self,
            )
            thread.value_ready.connect(
                lambda value, key=cache_key: self._cache_notification_page(key, value)
            )

            def finished() -> None:
                current = self._notification_prefetch_thread
                self._notification_prefetch_thread = None
                if current is not None:
                    current.deleteLater()

            thread.finished.connect(finished)
            self._notification_prefetch_thread = thread
            thread.start()

        def _notification_reload_failed(self, error: object) -> None:
            self.notification_page_status.setText(
                "加载失败；当前仍显示上次成功读取的数据"
            )
            self._result_handler(
                ControlResult(
                    False,
                    f"客户通知列表刷新失败：{type(error).__name__}。",
                    details={"non_modal": True},
                )
            )

        def _notification_reload_finished(self) -> None:
            thread = self._notification_reload_thread
            self._notification_reload_thread = None
            if thread is not None:
                thread.deleteLater()
            if self._notification_reload_queued:
                self._notification_reload_queued = False
                QTimer.singleShot(0, self._reload)
            window = self.window()
            if bool(getattr(window, "_close_pending", False)):
                QTimer.singleShot(0, window.close)

        def _filtered_notifications(self) -> list[dict[str, object]]:
            query = self.search_edit.text().strip().casefold()
            selected_product_types = self.product_type_filter_combo.selected_values
            candidates = [
                notification
                for notification in self._notifications
                if _matches_product_type_filter(
                    notification,
                    selected_product_types,
                )
            ]
            if not query:
                return candidates
            field = str(self.search_field_combo.currentData() or "all")

            def searchable_values(
                notification: Mapping[str, object],
            ) -> tuple[object, ...]:
                state_values = (
                    notification.get("state"),
                    _notification_state_label(
                        notification.get("state"),
                        notification.get("package_missing"),
                        notification.get("is_supplemental_revision"),
                        notification.get("last_error"),
                    ),
                    _notification_status_explanation(notification),
                )
                values_by_field = {
                    "platform_order_no": (
                        notification.get("platform_order_no"),
                    ),
                    "recipient_name": (notification.get("recipient_name"),),
                    "recipient_email": (notification.get("recipient_email"),),
                    "recipient_phone": (notification.get("recipient_phone"),),
                    "state": state_values,
                }
                if field == "all":
                    return (
                        notification.get("platform_order_no"),
                        *_product_type_values(notification),
                        notification.get("recipient_name"),
                        notification.get("recipient_email"),
                        notification.get("recipient_phone"),
                        *state_values,
                    )
                return values_by_field.get(field, ())

            return [
                notification
                for notification in candidates
                if any(
                    query in str(value or "").casefold()
                    for value in searchable_values(notification)
                )
            ]

        def _apply_search_filter(self, *_args: object) -> None:
            self._notification_page = 1
            # Apply the current page immediately for responsive typing, then
            # refresh from the server so matching rows outside this page are
            # included in the paginated result.
            self._render_notifications(
                selected_id=self._selected_id,
                selected_column=self.table.currentColumn(),
            )
            timer = getattr(self, "_notification_filter_timer", None)
            if timer is not None:
                timer.start()
            else:
                self._reload()

        def _render_notifications(
            self,
            *,
            selected_id: int | None,
            selected_column: int,
        ) -> None:
            scroll_state = _table_scroll_state(self.table)
            self._notifications.sort(key=self._notification_sort_key)
            self._visible_notifications = self._filtered_notifications()
            self._visible_notification_ids = frozenset(
                int(item.get("id") or 0)
                for item in self._visible_notifications
                if int(item.get("id") or 0) > 0
            )
            self._visible_awaiting_review_ids_cache = frozenset(
                int(item.get("id") or 0)
                for item in self._visible_notifications
                if str(item.get("state") or "") == "AWAITING_REVIEW"
                and int(item.get("id") or 0)
                not in self._active_task_ids_by_notification_id
            )
            eligible_ids = self._visible_notification_ids
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            self.table.setRowCount(len(self._visible_notifications))
            self._row_index_by_notification_id = {}
            selected_row = -1
            try:
                for row, notification in enumerate(self._visible_notifications):
                    notification_id = int(notification.get("id") or 0)
                    self._row_index_by_notification_id[notification_id] = row
                    active_task = self._active_task_for_notification(
                        notification_id
                    )
                    stored_state = str(notification.get("state") or "")
                    optimistic_queued = (
                        notification_id in self._optimistic_send_notification_ids
                    )
                    display_state = (
                        "QUEUED"
                        if (active_task is not None or optimistic_queued)
                        and stored_state in {"AWAITING_REVIEW", "RETRYABLE"}
                        else stored_state
                    )
                    if active_task is not None:
                        operator = (
                            active_task.operator_name
                            or active_task.operator_email
                            or "其他在线客户端"
                        )
                        status_explanation = (
                            f"已由 {operator} 加入共享处理队列；"
                            f"后台任务状态：{active_task.status.label}。请勿重复提交。"
                        )
                        status_timestamp = active_task.updated_at
                    elif optimistic_queued:
                        status_explanation = (
                            "审核发送任务已提交，正在等待共享后台领取。请勿重复提交。"
                        )
                        status_timestamp = datetime.now(timezone.utc)
                    else:
                        status_explanation = _notification_status_explanation(
                            notification
                        )
                        status_timestamp = (
                            notification.get("state_changed_at")
                            or notification.get("erp_completed_at")
                            or notification.get("updated_at")
                        )
                    if notification_id == selected_id:
                        selected_row = row
                    check_item = QTableWidgetItem()
                    flags = check_item.flags() & ~Qt.ItemFlag.ItemIsEditable
                    if notification_id in eligible_ids:
                        flags |= Qt.ItemFlag.ItemIsUserCheckable
                    check_item.setFlags(flags)
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if notification_id in self._checked_notification_ids
                        else Qt.CheckState.Unchecked
                    )
                    check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    check_item.setData(Qt.ItemDataRole.UserRole, notification_id)
                    self.table.setItem(row, 0, check_item)
                    values = (
                        notification.get("platform_order_no") or "",
                        "、".join(
                            _product_type_label(value)
                            for value in _product_type_values(notification)
                        ),
                        notification.get("recipient_name") or "-",
                        notification.get("recipient_email") or "-",
                        notification.get("recipient_phone") or "-",
                        f"{notification.get('package_complete') or 0}/{notification.get('package_total') or 0}",
                        _format_status_timestamp(status_timestamp),
                        _notification_state_label(
                            display_state,
                            notification.get("package_missing"),
                            notification.get("is_supplemental_revision"),
                            notification.get("last_error"),
                        ),
                        status_explanation,
                    )
                    for column, value in enumerate(values, start=1):
                        cell = (
                            _notification_status_item(
                                display_state,
                                notification.get("package_missing"),
                                notification.get("is_supplemental_revision"),
                                notification.get("last_error"),
                            )
                            if column == 8
                            else _readonly_item(value)
                        )
                        if column == 9 and str(value or ""):
                            cell.setToolTip(str(value))
                        self.table.setItem(row, column, cell)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()
            self._update_quick_select_review_button()
            if selected_row >= 0:
                column = min(
                    max(selected_column, 0),
                    max(0, self.table.columnCount() - 1),
                )
                self.table.setCurrentCell(selected_row, column)
            elif self._visible_notifications:
                self.table.setCurrentCell(0, 1)
            else:
                self._selected_id = None
                self.summary.setText(
                    "当前筛选没有匹配的客户通知。"
                    if self._notifications
                    else "当前没有客户通知草稿。"
                )
                self.package_table.setRowCount(0)
            _restore_table_scroll_state(self.table, scroll_state)

        def _update_active_notification_cells(
            self,
            notification_ids: set[int],
        ) -> None:
            """Refresh volatile task cells without rebuilding the whole table."""

            if not notification_ids:
                return
            notifications_by_id = {
                int(item.get("id") or 0): item
                for item in self._visible_notifications
            }
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for notification_id in notification_ids:
                    row = self._row_index_by_notification_id.get(notification_id)
                    notification = notifications_by_id.get(notification_id)
                    if row is None or notification is None:
                        continue
                    active_task = self._active_task_for_notification(
                        notification_id
                    )
                    stored_state = str(notification.get("state") or "")
                    optimistic_queued = (
                        notification_id in self._optimistic_send_notification_ids
                    )
                    display_state = (
                        "QUEUED"
                        if (active_task is not None or optimistic_queued)
                        and stored_state in {"AWAITING_REVIEW", "RETRYABLE"}
                        else stored_state
                    )
                    if active_task is not None:
                        operator = (
                            active_task.operator_name
                            or active_task.operator_email
                            or "其他在线客户端"
                        )
                        explanation = (
                            f"已由 {operator} 加入共享处理队列；"
                            f"后台任务状态：{active_task.status.label}。请勿重复提交。"
                        )
                        timestamp = active_task.updated_at
                    elif optimistic_queued:
                        explanation = (
                            "审核发送任务已提交，正在等待共享后台领取。请勿重复提交。"
                        )
                        timestamp = datetime.now(timezone.utc)
                    else:
                        explanation = _notification_status_explanation(
                            notification
                        )
                        timestamp = (
                            notification.get("state_changed_at")
                            or notification.get("erp_completed_at")
                            or notification.get("updated_at")
                        )
                    self.table.setItem(
                        row,
                        7,
                        _readonly_item(_format_status_timestamp(timestamp)),
                    )
                    self.table.setItem(
                        row,
                        8,
                        _notification_status_item(
                            display_state,
                            notification.get("package_missing"),
                            notification.get("is_supplemental_revision"),
                            notification.get("last_error"),
                        ),
                    )
                    explanation_item = _readonly_item(explanation)
                    if explanation:
                        explanation_item.setToolTip(explanation)
                    self.table.setItem(row, 9, explanation_item)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)

        def _eligible_notification_ids(
            self,
            notifications: Sequence[Mapping[str, object]] | None = None,
        ) -> set[int]:
            if notifications is None:
                return set(self._notification_ids)
            source = notifications
            # The header checkbox means exactly "all visible rows".  Business
            # actions validate their own allowed states; a separate quick-select
            # button handles the narrower "send pending review" workflow.
            return {
                int(item.get("id") or 0)
                for item in source
                if int(item.get("id") or 0) > 0
            }

        def _visible_awaiting_review_ids(self) -> set[int]:
            return set(self._visible_awaiting_review_ids_cache)

        def _update_quick_select_review_button(self) -> None:
            count = len(self._visible_awaiting_review_ids_cache)
            self.quick_select_review_button.setText(
                f"勾选待审核（{count}）"
            )
            self.quick_select_review_button.setEnabled(bool(count))

        def _update_notification_selection_summary(self) -> None:
            selected_count = len(
                self._visible_notification_ids & self._checked_notification_ids
            )
            review_count = len(self._visible_awaiting_review_ids_cache)
            self.notification_selection_summary.setText(
                f"显示 {len(self._visible_notifications)} · "
                f"待审核 {review_count} · 已选 {selected_count}"
            )

        def _select_visible_awaiting_review(self) -> None:
            visible_ids = self._visible_notification_ids
            review_ids = self._visible_awaiting_review_ids_cache
            self._checked_notification_ids.difference_update(visible_ids)
            self._checked_notification_ids.update(review_ids)
            self._refresh_visible_checkboxes()
            self._result_handler(
                ControlResult(
                    bool(review_ids),
                    (
                        f"已勾选当前筛选结果中的 {len(review_ids)} 条待审核通知。"
                        if review_ids
                        else "当前筛选结果中没有可自动发送的待审核通知。"
                    ),
                    details={"non_modal": True},
                )
            )

        def _target_notifications(self) -> list[dict[str, object]]:
            if self._checked_notification_ids:
                return [
                    item
                    for item in self._notifications
                    if int(item.get("id") or 0) in self._checked_notification_ids
                ]
            selected = self._selected()
            return [selected] if selected is not None else []

        def _on_item_changed(self, item: QTableWidgetItem) -> None:
            if item.column() != 0:
                return
            notification_id = int(item.data(Qt.ItemDataRole.UserRole) or 0)
            if notification_id not in self._notification_ids:
                return
            if item.checkState() == Qt.CheckState.Checked:
                self._checked_notification_ids.add(notification_id)
            else:
                self._checked_notification_ids.discard(notification_id)
            self._sync_check_header()

        def _set_all_checked(self, state_value: int) -> None:
            eligible_ids = self._visible_notification_ids
            checked = Qt.CheckState(state_value) == Qt.CheckState.Checked
            if checked:
                self._checked_notification_ids.update(eligible_ids)
            else:
                self._checked_notification_ids.difference_update(eligible_ids)
            self._refresh_visible_checkboxes()

        def _refresh_visible_checkboxes(self) -> None:
            previous = self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            try:
                for row in range(self.table.rowCount()):
                    item = self.table.item(row, 0)
                    notification_id = int(
                        item.data(Qt.ItemDataRole.UserRole) or 0
                    ) if item is not None else 0
                    if item is not None and notification_id in self._visible_notification_ids:
                        desired_state = (
                            Qt.CheckState.Checked
                            if notification_id in self._checked_notification_ids
                            else Qt.CheckState.Unchecked
                        )
                        if item.checkState() != desired_state:
                            item.setCheckState(desired_state)
            finally:
                self.table.blockSignals(previous)
                self.table.setUpdatesEnabled(True)
            self._sync_check_header()

        def _sync_check_header(self) -> None:
            checked_count = len(
                self._visible_notification_ids & self._checked_notification_ids
            )
            if not checked_count:
                state = Qt.CheckState.Unchecked
            elif checked_count == len(self._visible_notification_ids):
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
            self._check_header.set_check_state(state)
            self._update_notification_selection_summary()

        def _change_status(self) -> None:
            notifications = self._target_notifications()
            if not notifications:
                self._result_handler(
                    ControlResult(False, "请先勾选或选择至少一条客户通知。")
                )
                return
            dialog = _NotificationStatusDialog(len(notifications), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            selected = dialog.selected_value()
            if selected == "MANUALLY_COMPLETED":
                self._mark_manually_completed(notifications)
            elif selected == "CANCELLED":
                self._cancel_notifications(notifications)
            elif selected == "AWAITING_REVIEW":
                self._reopen_notifications_for_review(notifications)

        def _stop_checked_tasks(self) -> None:
            selected_ids = set(self._checked_notification_ids)
            if not selected_ids:
                self._result_handler(
                    ControlResult(False, "请先勾选至少一条客户通知。")
                )
                return
            task_ids = list(
                dict.fromkeys(
                    task_id
                    for notification_id in sorted(selected_ids)
                    for task_id in self._active_task_ids_by_notification_id.get(
                        notification_id,
                        (),
                    )
                )
            )
            if not task_ids:
                self._result_handler(
                    ControlResult(
                        False,
                        "当前勾选通知没有等待中、运行中或等待确认的后台任务。",
                    )
                )
                return
            unselected_ids = sorted(
                {
                    notification_id
                    for task_id in task_ids
                    for notification_id in self._active_notification_ids_by_task_id.get(
                        task_id,
                        (),
                    )
                    if notification_id not in selected_ids
                }
            )
            if unselected_ids:
                self._result_handler(
                    ControlResult(
                        False,
                        "当前勾选通知与同一批次中的其他通知共用一个后台任务。"
                        f"为避免误停未勾选内容，请同时勾选该批次其余 "
                        f"{len(unselected_ids)} 条通知后再停止。",
                    )
                )
                return
            affected_ids = sorted(
                {
                    notification_id
                    for task_id in task_ids
                    for notification_id in self._active_notification_ids_by_task_id.get(
                        task_id,
                        (),
                    )
                }
            )
            answer = QMessageBox.question(
                self,
                "确认停止当前勾选任务",
                f"即将停止 {len(task_ids)} 个后台任务，涉及当前勾选的 "
                f"{len(affected_ids)} 条客户通知。\n\n"
                "等待用户或只读任务会立即停止；只有已经发出的外部写入请求"
                "会在返回或超时后停止；"
                "不会把通知改为人工完成、已取消或其他业务状态。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _stop_all_tasks(self) -> None:
            task_ids = list(self._active_page_task_ids)
            if not task_ids:
                self._result_handler(
                    ControlResult(False, "客户通知页当前没有活动任务。")
                )
                return
            answer = QMessageBox.question(
                self,
                "确认停止本页所有任务",
                f"即将停止客户通知页内全部 {len(task_ids)} 个等待中、"
                "运行中或等待确认的后台任务。\n\n"
                "这包括发送、联系方式读取和物流同步任务；等待用户或只读任务"
                "会立即停止，只有已经发出的外部发送请求会在返回或超时后停止；"
                "不会修改通知业务状态。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                _run_control_result_responsive(
                    self,
                    self._controller,
                    lambda: self._controller.cancel_tasks(task_ids),
                    self._result_handler,
                )

        def _mark_manually_completed(
            self,
            notifications: Sequence[Mapping[str, object]] | None = None,
        ) -> None:
            notifications = list(notifications or self._target_notifications())
            if not notifications:
                self._result_handler(ControlResult(False, "请先勾选至少一条标发邮件通知。"))
                return
            invalid = [
                item
                for item in notifications
                if str(item.get("state") or "") == "MANUALLY_COMPLETED"
            ]
            if invalid:
                self._result_handler(
                    ControlResult(False, "勾选记录中包含已经是人工完成的通知，无需重复修改。")
                )
                return
            reason, accepted = QInputDialog.getText(
                self,
                "标记人工完成",
                "请输入审计原因：",
                text="历史 ERP 标发已完成，客户通知已人工发送",
            )
            if not accepted:
                return
            reason = reason.strip()
            if not reason:
                self._result_handler(ControlResult(False, "审计原因不能为空。"))
                return
            answer = QMessageBox.question(
                self,
                "确认设为人工完成",
                f"即将把 {len(notifications)} 条标发邮件通知设为人工完成。\n\n"
                "此操作只修改本地状态，不会调用阿里邮箱或 ClickSend，"
                "适用于已经由人工核实并完成标发或通知的订单。"
                "即使发送服务曾接收过，系统也会保留原供应商回执和审计记录；"
                "当前包裹不会重复生成发送草稿，后续若出现新包裹仍会生成新通知。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            notification_ids = [int(item["id"]) for item in notifications]

            def finish(result: ControlResult) -> None:
                if result.accepted:
                    self._checked_notification_ids.difference_update(notification_ids)
                self._result_handler(result)
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.mark_shipment_notifications_manually_completed(
                    notification_ids,
                    reason=reason,
                ),
                finish,
            )

        def _cancel_notifications(
            self,
            notifications: Sequence[Mapping[str, object]] | None = None,
        ) -> None:
            notifications = list(notifications or self._target_notifications())
            if not notifications:
                self._result_handler(ControlResult(False, "请先勾选或选择至少一条客户通知。"))
                return
            allowed_states = {
                "WAITING_CONTACT", "MANUAL_EMAIL_REQUIRED", "AWAITING_REVIEW", "BLOCKED", "REJECTED",
                "RETRYABLE", "FAILED",
            }
            invalid = [
                item for item in notifications
                if str(item.get("state") or "") not in allowed_states
            ]
            if invalid:
                self._result_handler(
                    ControlResult(False, "只有尚未发送的最新通知可以设为已取消。")
                )
                return
            reason, accepted = QInputDialog.getText(
                self,
                "取消客户通知",
                "请输入取消原因（会保留在审核历史）：",
            )
            if not accepted:
                return
            reason = reason.strip()
            if not reason:
                self._result_handler(ControlResult(False, "取消原因不能为空。"))
                return
            answer = QMessageBox.question(
                self,
                "确认取消客户通知",
                f"即将把 {len(notifications)} 条客户通知设为“已取消”。\n\n"
                "不会调用邮件或短信接口；后续扫描也不会自动重新生成草稿。"
                "如需恢复，可使用“重新提交审核”。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            ids = [int(item["id"]) for item in notifications]

            def finish(result: ControlResult) -> None:
                if result.accepted:
                    self._checked_notification_ids.difference_update(ids)
                self._result_handler(result)
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.cancel_shipment_notifications(
                    ids,
                    reason=reason,
                ),
                finish,
            )

        def _merge_notification_details(self, value: object) -> set[int]:
            details = (
                list(value)
                if isinstance(value, Sequence)
                and not isinstance(value, (str, bytes))
                else []
            )
            merged_ids: set[int] = set()
            by_id = {
                int(item.get("id") or 0): dict(item)
                for item in details
                if isinstance(item, Mapping) and int(item.get("id") or 0) > 0
            }
            for notification in self._notifications:
                notification_id = int(notification.get("id") or 0)
                detail = by_id.get(notification_id)
                if detail is None:
                    continue
                notification.update(detail)
                notification["_detail_loaded"] = True
                notification["_detail_error"] = False
                merged_ids.add(notification_id)
            self._notification_detail_failed_ids.difference_update(merged_ids)
            return merged_ids

        def _request_selected_notification_detail(self, notification_id: int) -> None:
            method = getattr(
                self._controller,
                "get_shipment_notification_details",
                None,
            )
            if not callable(method):
                return
            if (
                self._notification_detail_thread is not None
                and self._notification_detail_thread.isRunning()
            ):
                self._notification_detail_queued_id = notification_id
                return
            self._notification_detail_loading_id = notification_id
            thread = _ValueThread(lambda: method((notification_id,)), self)

            def ready(value: object) -> None:
                merged = self._merge_notification_details(value)
                if notification_id not in merged:
                    self._notification_detail_failed_ids.add(notification_id)
                    self._result_handler(
                        ControlResult(
                            False,
                            "客户通知详情已不存在或当前不可读取。",
                            details={"non_modal": True},
                        )
                    )

            def failed(error: object) -> None:
                self._notification_detail_failed_ids.add(notification_id)
                self._result_handler(
                    ControlResult(
                        False,
                        f"客户通知详情加载失败：{type(error).__name__}。",
                        details={"non_modal": True},
                    )
                )

            def finished() -> None:
                current = self._notification_detail_thread
                self._notification_detail_thread = None
                self._notification_detail_loading_id = None
                if current is not None:
                    current.deleteLater()
                queued_id = self._notification_detail_queued_id
                self._notification_detail_queued_id = None
                if queued_id is not None and queued_id != notification_id:
                    self._request_selected_notification_detail(queued_id)
                    return
                self._show_selected()

            thread.value_ready.connect(ready)
            thread.value_failed.connect(failed)
            thread.finished.connect(finished)
            self._notification_detail_thread = thread
            thread.start()

        def _ensure_notification_details(
            self,
            notifications: Sequence[Mapping[str, object]],
            continuation: Callable[[], None],
        ) -> bool:
            missing_ids = tuple(
                int(item.get("id") or 0)
                for item in notifications
                if int(item.get("id") or 0) > 0
                and not _notification_has_full_details(item)
            )
            if not missing_ids:
                return True
            method = getattr(
                self._controller,
                "get_shipment_notification_details",
                None,
            )
            if not callable(method):
                return True
            if (
                self._notification_action_detail_thread is not None
                and self._notification_action_detail_thread.isRunning()
            ):
                return False
            self.approve_button.setEnabled(False)
            self.approve_button.setText("正在加载待审核通知详情…")
            thread = _ValueThread(lambda: method(missing_ids), self)
            load_succeeded = {"value": False}

            def ready(value: object) -> None:
                merged = self._merge_notification_details(value)
                load_succeeded["value"] = all(
                    notification_id in merged for notification_id in missing_ids
                )
                if not load_succeeded["value"]:
                    self._result_handler(
                        ControlResult(
                            False,
                            "部分待审核通知详情已不存在；本次未发送任何通知。",
                        )
                    )

            def failed(error: object) -> None:
                self._result_handler(
                    ControlResult(
                        False,
                        f"发送审核详情加载失败：{type(error).__name__}。未发送任何通知。",
                    )
                )

            def finished() -> None:
                current = self._notification_action_detail_thread
                self._notification_action_detail_thread = None
                if current is not None:
                    current.deleteLater()
                self.approve_button.setEnabled(True)
                self.approve_button.setText("审核通过并发送")
                if load_succeeded["value"]:
                    QTimer.singleShot(0, continuation)

            thread.value_ready.connect(ready)
            thread.value_failed.connect(failed)
            thread.finished.connect(finished)
            self._notification_action_detail_thread = thread
            thread.start()
            return False

        def _show_selected(self) -> None:
            notification = self._selected()
            if notification is None:
                return
            self._selected_id = int(notification.get("id") or 0)
            full_details_loaded = _notification_has_full_details(notification)
            package_preview_loaded = "preview_items" in notification
            if not full_details_loaded and not package_preview_loaded:
                self.package_table.setRowCount(0)
                if self._selected_id in self._notification_detail_failed_ids:
                    self.summary.setText(
                        "通知摘要已保留，但详情加载失败。刷新列表后可重试。"
                    )
                    return
                self.summary.setText("正在加载通知包裹与正文详情…")
                self._request_selected_notification_detail(self._selected_id)
                return
            self.summary.setText(
                f"平台单号：{notification.get('platform_order_no') or '-'}\n"
                f"收件人：{notification.get('recipient_name') or '-'}\n"
                f"邮箱：{notification.get('recipient_email') or '-'}\n"
                f"电话：{notification.get('recipient_phone') or '-'}\n"
                f"包裹：总数 {notification.get('package_total')}，已有物流 "
                f"{notification.get('package_complete')}，待补 {notification.get('package_missing')}"
            )
            raw_items = (
                notification.get("items")
                if full_details_loaded
                else notification.get("preview_items")
            )
            items = list(raw_items or [])
            self.package_table.setRowCount(len(items))
            for row, item in enumerate(items):
                customer_visible = bool(item.get("customer_visible", 1))
                selection_reason = str(item.get("visibility_reason") or "").strip()
                requires_review = selection_reason in {
                    "tracking_source_unresolved",
                }
                final_tracking = str(item.get("final_tracking_no") or "").strip()
                values = (
                    item.get("stable_sequence"),
                    (
                        item.get("display_label") or "待补"
                        if customer_visible
                        else "-"
                    ),
                    item.get("system_order_no") or "-",
                    (
                        "人工修正"
                        if item.get("manual_override")
                        else _tracking_source_label(item.get("shipment_type"))
                    ),
                    item.get("carrier_normalized") or item.get("carrier_raw") or "-",
                    final_tracking or ("待复核" if requires_review else "-"),
                    (
                        (
                            "可发送"
                            if item.get("is_complete")
                            else (
                                "需复核"
                                if requires_review
                                else (
                                "待补物流"
                                if item.get("visibility_reason") == "pending_wms"
                                or selection_reason == "tracking_pending"
                                else "不完整"
                                )
                            )
                        )
                    ) if customer_visible else "Instruction，不通知",
                )
                raw_tracking_tooltip = (
                    f"运单号：{item.get('waybill_no') or '-'}\n"
                    f"跟踪号：{item.get('tracking_no') or '-'}\n"
                    f"选择结果：{final_tracking or '-'}"
                )
                if item.get("manual_override"):
                    raw_tracking_tooltip += (
                        "\n人工修正：是"
                        f"\n修改原因：{item.get('manual_override_reason') or '-'}"
                        f"\n修改时间：{item.get('manual_override_updated_at') or '-'}"
                    )
                for column, value in enumerate(values):
                    cell = _readonly_item(value)
                    if not customer_visible:
                        cell.setForeground(QColor("#98A2B3"))
                        cell.setBackground(QColor("#F2F4F7"))
                    elif column == 3:
                        cell.setForeground(
                            QColor("#B54708" if requires_review else "#175CD3")
                        )
                        cell.setToolTip(raw_tracking_tooltip)
                    elif column == 5:
                        complete = bool(final_tracking)
                        cell.setForeground(
                            QColor(
                                "#175CD3"
                                if complete
                                else "#B42318"
                                if requires_review
                                else "#B54708"
                            )
                        )
                        font = cell.font()
                        font.setBold(True)
                        cell.setFont(font)
                        cell.setToolTip(raw_tracking_tooltip)
                        tracking_url = str(item.get("tracking_url") or "").strip()
                        if tracking_url:
                            cell.setToolTip(
                                f"{raw_tracking_tooltip}\n物流查询链接：{tracking_url}"
                            )
                    elif column == 6 and requires_review:
                        cell.setForeground(QColor("#B42318"))
                    self.package_table.setItem(row, column, cell)

        def _require_selected(self) -> dict[str, object] | None:
            notification = self._selected()
            if notification is None:
                self._result_handler(ControlResult(False, "请先选择一条客户通知。"))
            return notification

        def _refresh_receipts(self) -> None:
            def finish(result: ControlResult) -> None:
                self._result_handler(result)
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                self._controller.refresh_shipment_notification_receipts,
                finish,
            )

        @staticmethod
        def _batch_review_text(notification: Mapping[str, object]) -> str:
            lines = [
                f"平台单号：{notification.get('platform_order_no') or '-'}",
                f"收件人：{notification.get('recipient_name') or '-'}",
                f"邮箱：{notification.get('recipient_email') or '-'}",
                f"电话：{notification.get('recipient_phone') or '-'}",
                f"发送方式：{notification.get('channel') or '-'}",
                "",
                "包裹：",
            ]
            for item in list(notification.get("items") or []):
                if not isinstance(item, Mapping):
                    continue
                if not bool(item.get("customer_visible", 1)):
                    continue
                if not item.get("is_complete"):
                    continue
                label = str(item.get("display_label") or "-")
                carrier = str(
                    item.get("carrier_normalized") or item.get("carrier_raw") or "-"
                )
                tracking = str(item.get("final_tracking_no") or "-")
                lines.append(f"· Package {label}: {carrier} {tracking}")
            subject = str(notification.get("subject") or "").strip()
            body = str(notification.get("body") or "")
            lines.extend(("", *( [f"Subject: {subject}", ""] if subject else [] ), body))
            return "\n".join(lines)

        def _confirm_batch_review(
            self, notifications: Sequence[Mapping[str, object]]
        ) -> bool:
            dialog = QDialog(self)
            dialog.setWindowTitle(f"发送审核确认（{len(notifications)} 条）")
            dialog.resize(900, 700)
            layout = QVBoxLayout(dialog)
            hint = QLabel(
                "请逐条核对收件人、联系方式、平台单号、全部包裹和最终正文。"
                "确认后将并发执行发送；WMS 出库状态已在扫描进入审核列表前完成校验。"
            )
            hint.setWordWrap(True)
            layout.addWidget(hint)
            selector = QComboBox()
            for index, notification in enumerate(notifications, start=1):
                selector.addItem(
                    f"{index}. {notification.get('platform_order_no') or '-'} / "
                    f"{notification.get('recipient_name') or '-'}"
                )
            layout.addWidget(selector)
            viewer = QPlainTextEdit()
            viewer.setReadOnly(True)
            layout.addWidget(viewer, 1)
            confirmed = QCheckBox(f"我已逐条核对上述 {len(notifications)} 条通知")
            layout.addWidget(confirmed)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel
            )
            approve = buttons.button(QDialogButtonBox.StandardButton.Ok)
            approve.setText("审核通过并真实发送")
            approve.setEnabled(False)
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            confirmed.toggled.connect(approve.setEnabled)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)

            def show(index: int) -> None:
                viewer.setPlainText(self._batch_review_text(notifications[index]))

            selector.currentIndexChanged.connect(show)
            show(0)
            return dialog.exec() == QDialog.DialogCode.Accepted

        def _approve(self) -> None:
            notifications = self._target_notifications()
            if not notifications:
                self._result_handler(ControlResult(False, "请先勾选或选择至少一条待审核通知。"))
                return
            if not self._ensure_notification_details(notifications, self._approve):
                return
            if self._active_notification_send_task_ids:
                self._result_handler(
                    ControlResult(
                        False,
                        "已有另一批客户通知正在发送；请等待当前批次完成。",
                    )
                )
                return
            sendable = [
                item
                for item in notifications
                if item.get("state") == "AWAITING_REVIEW"
                and self._active_task_for_notification(
                    int(item.get("id") or 0)
                )
                is None
                and int(item.get("id") or 0)
                not in self._optimistic_send_notification_ids
            ]
            sendable_ids = {
                int(item.get("id") or 0) for item in sendable
            }
            skipped_ids = {
                int(item.get("id") or 0) for item in notifications
            } - sendable_ids
            if skipped_ids:
                self._checked_notification_ids.difference_update(skipped_ids)
                self._render_notifications(
                    selected_id=self._selected_id,
                    selected_column=self.table.currentColumn(),
                )
            if not sendable:
                self._result_handler(
                    ControlResult(
                        False,
                        "没有可发送的待审核通知；已自动取消已发送、非待审核或正在处理记录的历史勾选。",
                    )
                )
                return
            if skipped_ids:
                self._result_handler(
                    ControlResult(
                        True,
                        f"已自动排除 {len(skipped_ids)} 条已发送、非待审核或正在处理的历史勾选；"
                        f"本次将审核 {len(sendable)} 条待审核通知。",
                        details={"non_modal": True},
                    )
                )
            notifications = sendable
            if not self._confirm_batch_review(notifications):
                return
            notification_ids = tuple(int(item["id"]) for item in notifications)
            confirmation_order_no = notification_confirmation_order_no(
                notification_ids
            )
            confirmation = DesktopWriteConfirmation.create(
                DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
                confirmation_order_no,
                source="qt_message_box",
            )
            command = TaskCommand(
                name=f"发送客户通知（{len(notification_ids)} 条）",
                area=TaskArea.SHIPMENT,
                capability=Capability.SEND_NOTIFICATION,
                order_no=confirmation_order_no,
                payload={
                    "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                    "notification_ids": list(notification_ids),
                    "retry": False,
                    DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                },
            )
            self.approve_button.setEnabled(False)
            self.approve_button.setText(f"正在提交 {len(notification_ids)} 条发送任务…")

            def finish(result: ControlResult) -> None:
                if self._show_submission_queue_conflict(result, notifications):
                    self.approve_button.setEnabled(True)
                    self.approve_button.setText("审核通过并发送")
                    return
                if result.accepted:
                    self._notification_send_task_id = result.task_id
                    self._checked_notification_ids.difference_update(
                        notification_ids
                    )
                    self._optimistic_send_notification_ids.update(
                        notification_ids
                    )
                    self._notification_page = 1
                    self._render_notifications(
                        selected_id=self._selected_id,
                        selected_column=self.table.currentColumn(),
                    )
                    self._reload()
                    self.approve_button.setText(
                        f"已提交 {len(notification_ids)} 条发送任务…"
                    )
                else:
                    self.approve_button.setEnabled(True)
                    self.approve_button.setText("审核通过并发送")
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _retry(self) -> None:
            notifications = self._target_notifications()
            if not notifications:
                self._result_handler(
                    ControlResult(False, "请先勾选或选择至少一条客户通知。")
                )
                return
            invalid = [
                notification
                for notification in notifications
                if notification.get("state") != "RETRYABLE"
            ]
            if invalid:
                invalid_states = sorted(
                    {
                        str(notification.get("state") or "-")
                        for notification in invalid
                    }
                )
                self._result_handler(
                    ControlResult(
                        False,
                        "只有“可重试”状态能重试已批准内容；"
                        f"当前有 {len(invalid)} 条状态不符合：{', '.join(invalid_states)}。"
                        "待审核内容请先审核通过，其他状态请先重新提交审核。",
                    )
                )
                return
            for notification in notifications:
                if self._active_task_for_notification(
                    int(notification.get("id") or 0)
                ) is not None:
                    self._show_notification_queue_conflict(notification)
                    return
            notification_ids = tuple(
                sorted(int(notification["id"]) for notification in notifications)
            )
            confirmation_order_no = notification_confirmation_order_no(
                notification_ids
            )
            confirmation = DesktopWriteConfirmation.create(
                DesktopWriteAction.SEND_SHIPMENT_NOTIFICATION,
                confirmation_order_no,
                source="qt_checked_action",
            )
            command = TaskCommand(
                name=f"重试已批准客户通知（{len(notification_ids)} 条）",
                area=TaskArea.SHIPMENT,
                capability=Capability.SEND_NOTIFICATION,
                order_no=confirmation_order_no,
                payload={
                    "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                    "notification_ids": list(notification_ids),
                    "retry": True,
                    DESKTOP_CONFIRMATION_PAYLOAD_KEY: confirmation.to_payload(),
                },
            )

            def finish(result: ControlResult) -> None:
                if self._show_submission_queue_conflict(result, notifications):
                    return
                if result.accepted:
                    self._notification_send_task_id = result.task_id
                    self._checked_notification_ids.difference_update(
                        notification_ids
                    )
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _reject(self) -> None:
            notification = self._require_selected()
            if notification is None:
                return

            def finish(result: ControlResult) -> None:
                self._result_handler(result)
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.reject_shipment_notification(
                    int(notification["id"])
                ),
                finish,
            )

        def _reopen_notifications_for_review(
            self,
            notifications: Sequence[Mapping[str, object]],
        ) -> None:
            if not notifications:
                self._result_handler(
                    ControlResult(False, "请先勾选或选择至少一条客户通知。")
                )
                return
            for notification in notifications:
                if self._active_task_for_notification(
                    int(notification.get("id") or 0)
                ) is not None:
                    self._show_notification_queue_conflict(notification)
                    return
            reason, accepted = QInputDialog.getText(
                self,
                "重新提交审核",
                "请输入重开原因（将保留原发送和审核历史）：",
            )
            if not accepted:
                return
            reason = reason.strip()
            if not reason:
                self._result_handler(ControlResult(False, "重开原因不能为空。"))
                return
            answer = QMessageBox.question(
                self,
                "确认重新提交",
                f"系统将为 {len(notifications)} 条通知保留当前通知及供应商回执，"
                "分别新建待审核版本。\n"
                "本操作不会发送邮件或短信。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            def finish(result: ControlResult) -> None:
                self._result_handler(result)
                self._reload()

            def operation() -> ControlResult:
                return self._controller.resubmit_shipment_notifications(
                    tuple(int(notification["id"]) for notification in notifications),
                    reason=reason,
                )

            _run_control_result_responsive(
                self,
                self._controller,
                operation,
                finish,
            )

        def _resubmit(self) -> None:
            self._reopen_notifications_for_review(self._target_notifications())

        def _edit_contact(self) -> None:
            notification = self._require_selected()
            if notification is None:
                return
            email, accepted = QInputDialog.getText(
                self,
                "修改收件邮箱",
                "邮箱（可留空，留空时将使用有效电话发送短信）：",
                text=str(notification.get("recipient_email") or ""),
            )
            if not accepted:
                return
            phone, accepted = QInputDialog.getText(
                self,
                "修改收件电话",
                "电话：",
                text=str(notification.get("recipient_phone") or ""),
            )
            if not accepted:
                return

            def finish(result: ControlResult) -> None:
                self._result_handler(result)
                self._selected_id = (
                    int(result.details.get("notification_id") or 0) or None
                )
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.edit_shipment_notification_contact(
                    int(notification["id"]),
                    email=email,
                    phone=phone,
                ),
                finish,
            )

        def _edit_package_logistics(self) -> None:
            notification = self._require_selected()
            if notification is None:
                return
            notification_id = int(notification.get("id") or 0)
            if self._active_task_for_notification(notification_id) is not None:
                self._show_notification_queue_conflict(notification)
                return
            if str(notification.get("state") or "") not in {
                "WAITING_CONTACT",
                "MANUAL_EMAIL_REQUIRED",
                "AWAITING_REVIEW",
                "BLOCKED",
                "REJECTED",
                "RETRYABLE",
                "FAILED",
            }:
                self._result_handler(
                    ControlResult(False, "只能修改尚未发送的最新客户通知。")
                )
                return
            if not self._ensure_notification_details(
                [notification],
                self._edit_package_logistics,
            ):
                return
            raw_packages = notification.get("editable_packages")
            if not isinstance(raw_packages, Sequence) or isinstance(
                raw_packages,
                (str, bytes),
            ):
                raw_packages = notification.get("items") or ()
            packages = [
                item
                for item in raw_packages
                if isinstance(item, Mapping)
                and bool(item.get("customer_visible", 1))
                and str(item.get("package_key") or "").strip()
            ]
            if not packages:
                self._result_handler(
                    ControlResult(
                        False,
                        "当前订单没有可人工修改的客户包裹；请先重新同步物流。",
                    )
                )
                return
            dialog = _NotificationPackageLogisticsDialog(
                str(notification.get("platform_order_no") or ""),
                packages,
                self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            package_key, carrier, tracking_no, reason = dialog.values()
            if not carrier or not tracking_no or not reason:
                self._result_handler(
                    ControlResult(False, "承运商、物流单号和修改原因都不能为空。")
                )
                return

            def finish(result: ControlResult) -> None:
                self._result_handler(result)
                if result.accepted:
                    self._selected_id = (
                        int(result.details.get("notification_id") or 0) or None
                    )
                    self._checked_notification_ids.discard(notification_id)
                self._reload()

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.edit_shipment_notification_package(
                    notification_id,
                    package_key=package_key,
                    carrier=carrier,
                    tracking_no=tracking_no,
                    reason=reason,
                ),
                finish,
            )

        def _rescan(self) -> None:
            command = TaskCommand(
                name="扫描订单并同步物流",
                area=TaskArea.SHIPMENT,
                capability=Capability.LIST_ORDERS,
                payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
            )
            self.rescan_button.setEnabled(False)
            self.rescan_button.setText("正在提交扫描…")

            def finish(result: ControlResult) -> None:
                if result.accepted:
                    self.rescan_button.setText("正在扫描订单并同步物流…")
                else:
                    self.rescan_button.setEnabled(True)
                    self.rescan_button.setText("扫描订单并同步物流")
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _refresh_contacts(self) -> None:
            notifications = self._target_notifications()
            if not notifications:
                self._result_handler(
                    ControlResult(False, "请先勾选或选择至少一条客户通知。")
                )
                return
            allowed_states = {
                "WAITING_CONTACT",
                "MANUAL_EMAIL_REQUIRED",
                "AWAITING_REVIEW",
                "BLOCKED",
                "REJECTED",
            }
            invalid = [
                item
                for item in notifications
                if str(item.get("state") or "") not in allowed_states
            ]
            if invalid:
                self._result_handler(
                    ControlResult(
                        False,
                        "只能重新获取尚未发送的待补联系方式、需人工发送邮件、待审核、已阻止或已驳回通知。",
                    )
                )
                return
            command = TaskCommand(
                name="从定制 JSON 重新获取客户通知联系方式",
                area=TaskArea.SHIPMENT,
                capability=Capability.GET_ORDER_DETAIL,
                payload={
                    "trigger": NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                    "notification_ids": [
                        int(item.get("id") or 0) for item in notifications
                    ],
                },
            )
            self.contact_refresh_action.setEnabled(False)
            self.contact_refresh_action.setText("正在提交读取任务…")

            def finish(result: ControlResult) -> None:
                if result.accepted:
                    self._contact_refresh_task_id = result.task_id
                    self.contact_refresh_action.setText("正在读取定制 JSON…")
                else:
                    self.contact_refresh_action.setEnabled(True)
                    self.contact_refresh_action.setText("从定制 JSON 获取联系方式")
                self._result_handler(result)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            notification_data_tasks = [
                task
                for task in snapshot.tasks
                if str(task.payload.get("trigger") or "")
                in {
                    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
                    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
                }
            ]
            next_data_task_states = {
                task.task_id: task.status for task in notification_data_tasks
            }
            notification_data_may_have_changed = (
                not self._notifications_loaded
                or any(
                    task.status.terminal
                    and not self._notification_data_task_states.get(
                        task.task_id,
                        TaskStatus.RUNNING,
                    ).terminal
                    for task in notification_data_tasks
                )
            )
            self._notification_data_task_states = next_data_task_states
            self._active_page_task_ids = tuple(
                task.task_id
                for task in snapshot.tasks
                if not task.status.terminal
                and str(task.payload.get("trigger") or "")
                in {
                    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
                    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
                }
            )
            notification_send_tasks = [
                task
                for task in snapshot.tasks
                if str(task.payload.get("trigger") or "")
                == SHIPMENT_NOTIFICATION_SEND_TRIGGER
            ]
            terminal_send_notification_ids: set[int] = set()
            for task in notification_send_tasks:
                if not task.status.terminal:
                    continue
                raw_ids = task.payload.get("notification_ids")
                if not isinstance(raw_ids, Sequence) or isinstance(
                    raw_ids,
                    (str, bytes),
                ):
                    continue
                for raw_id in raw_ids:
                    try:
                        notification_id = int(raw_id)
                    except (TypeError, ValueError):
                        continue
                    if notification_id > 0:
                        terminal_send_notification_ids.add(notification_id)
            self._optimistic_send_notification_ids.difference_update(
                terminal_send_notification_ids
            )
            self._active_notification_send_task_ids = tuple(
                task.task_id
                for task in notification_send_tasks
                if not task.status.terminal
            )
            active_task_ids_by_notification_id: dict[int, list[str]] = {}
            active_tasks_by_notification_id: dict[
                int,
                list[TaskRecord],
            ] = {}
            active_notification_ids_by_task_id: dict[str, tuple[int, ...]] = {}
            for task in snapshot.tasks:
                if task.status.terminal or str(
                    task.payload.get("trigger") or ""
                ) not in {
                    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
                    NOTIFICATION_CONTACT_REFRESH_TRIGGER,
                }:
                    continue
                raw_notification_ids = task.payload.get("notification_ids")
                if not isinstance(raw_notification_ids, Sequence) or isinstance(
                    raw_notification_ids,
                    (str, bytes),
                ):
                    continue
                notification_ids: list[int] = []
                for raw_notification_id in raw_notification_ids:
                    try:
                        notification_id = int(raw_notification_id)
                    except (TypeError, ValueError):
                        continue
                    if notification_id > 0 and notification_id not in notification_ids:
                        notification_ids.append(notification_id)
                if not notification_ids:
                    continue
                active_notification_ids_by_task_id[task.task_id] = tuple(
                    notification_ids
                )
                for notification_id in notification_ids:
                    active_task_ids_by_notification_id.setdefault(
                        notification_id,
                        [],
                    ).append(task.task_id)
                    active_tasks_by_notification_id.setdefault(
                        notification_id,
                        [],
                    ).append(task)
            next_active_task_ids_by_notification_id = {
                notification_id: tuple(task_ids)
                for notification_id, task_ids in active_task_ids_by_notification_id.items()
            }
            next_active_tasks_by_notification_id = {
                notification_id: tuple(tasks)
                for notification_id, tasks in active_tasks_by_notification_id.items()
            }
            active_mapping_changed = (
                next_active_task_ids_by_notification_id
                != self._active_task_ids_by_notification_id
                or next_active_tasks_by_notification_id
                != self._active_tasks_by_notification_id
                or active_notification_ids_by_task_id
                != self._active_notification_ids_by_task_id
            )
            self._active_task_ids_by_notification_id = (
                next_active_task_ids_by_notification_id
            )
            self._active_tasks_by_notification_id = (
                next_active_tasks_by_notification_id
            )
            self._active_notification_ids_by_task_id = (
                active_notification_ids_by_task_id
            )
            if active_mapping_changed and self._notifications:
                self._checked_notification_ids.intersection_update(
                    self._eligible_notification_ids()
                )
                self._notification_page = 1
                self._render_notifications(
                    selected_id=self._selected_id,
                    selected_column=self.table.currentColumn(),
                )
                self._reload()
            send_active = bool(self._active_notification_send_task_ids)
            self.approve_button.setEnabled(not send_active)
            self.approve_button.setText(
                "正在并发发送客户通知…"
                if send_active
                else "审核通过并发送"
            )
            if self._notification_send_task_id:
                completed_send = next(
                    (
                        task
                        for task in notification_send_tasks
                        if task.task_id == self._notification_send_task_id
                        and task.status.terminal
                    ),
                    None,
                )
                if completed_send is not None:
                    self._notification_send_task_id = None
                    self._reload()
                    self._result_handler(
                        ControlResult(
                            completed_send.status is TaskStatus.SUCCEEDED,
                            completed_send.message,
                            completed_send.task_id,
                            details={"non_modal": True},
                        )
                    )
            rescan_active = any(
                str(task.payload.get("trigger") or "")
                == NOTIFICATION_REVIEW_RESCAN_TRIGGER
                and not task.status.terminal
                for task in snapshot.tasks
            )
            self.rescan_button.setEnabled(not rescan_active)
            self.rescan_button.setText(
                "正在扫描订单并同步物流…" if rescan_active else "扫描订单并同步物流"
            )
            contact_refresh_tasks = [
                task
                for task in snapshot.tasks
                if str(task.payload.get("trigger") or "")
                == NOTIFICATION_CONTACT_REFRESH_TRIGGER
            ]
            contact_refresh_active = any(
                not task.status.terminal for task in contact_refresh_tasks
            )
            self.contact_refresh_action.setEnabled(not contact_refresh_active)
            self.contact_refresh_action.setText(
                "正在读取定制 JSON…"
                if contact_refresh_active
                else "从定制 JSON 获取联系方式"
            )
            if self._contact_refresh_task_id:
                completed = next(
                    (
                        task
                        for task in contact_refresh_tasks
                        if task.task_id == self._contact_refresh_task_id
                        and task.status.terminal
                    ),
                    None,
                )
                if completed is not None:
                    self._contact_refresh_task_id = None
                    self._result_handler(
                        ControlResult(
                            completed.status is TaskStatus.SUCCEEDED,
                            completed.message,
                            completed.task_id,
                            details={"non_modal": True},
                        )
                    )
            if notification_data_may_have_changed:
                self._reload()


    class LogsPage(QWidget):
        def __init__(
            self,
            controller: BackgroundTaskController,
            result_handler: ResultHandler | None = None,
        ) -> None:
            super().__init__()
            self._controller = controller
            self._result_handler = result_handler or (lambda _result: None)
            self._page = 1
            self._page_count = 1
            self._last_snapshot_signature: object | None = None
            self._cleanup_thread: _ControlResultThread | None = None
            self._page_load_thread: _ValueThread | None = None
            self._page_load_queued = False
            self._rendered_log_items: tuple[LogEntry, ...] = ()
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(250)
            self._search_timer.timeout.connect(self._reset_filters)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            title = QLabel("日志")
            title.setObjectName("pageTitle")
            layout.addWidget(title)

            filters = QHBoxLayout()
            self.level_filter = QComboBox()
            self.level_filter.addItem("全部级别", "")
            for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
                self.level_filter.addItem(level, level)
            self.search = QLineEdit()
            self.search.setPlaceholderText("搜索操作者、任务 ID、来源或消息")
            full_log_button = QPushButton("查看所选任务完整日志")
            full_log_button.clicked.connect(self._show_full_log)
            open_log_dir_button = QPushButton("打开日志目录")
            open_log_dir_button.clicked.connect(self._open_log_directory)
            self.cleanup_age_combo = QComboBox()
            self.cleanup_age_combo.addItem("删除 1 个月前", 30)
            self.cleanup_age_combo.addItem("删除 3 个月前", 90)
            self.cleanup_age_combo.setCurrentIndex(1)
            self.cleanup_button = QPushButton("清理旧日志")
            self.cleanup_button.setObjectName("dangerButton")
            self.cleanup_button.clicked.connect(self._delete_old_logs)
            self.level_filter.currentIndexChanged.connect(self._reset_filters)
            self.search.textChanged.connect(self._schedule_search)
            filters.addWidget(self.level_filter)
            filters.addWidget(self.search, 1)
            filters.addWidget(full_log_button)
            filters.addWidget(open_log_dir_button)
            filters.addWidget(self.cleanup_age_combo)
            filters.addWidget(self.cleanup_button)
            layout.addLayout(filters)

            self.table = QTableWidget(0, 6)
            self.table.setHorizontalHeaderLabels(
                ["时间", "级别", "操作者", "任务 ID", "来源", "消息"]
            )
            _prepare_table(self.table)
            _set_table_default_widths(
                self.table,
                (155, 80, 150, 220, 160, 420),
            )
            layout.addWidget(self.table, 1)

            pager = QHBoxLayout()
            self.first_button = QPushButton("首页")
            self.previous_button = QPushButton("上一页")
            self.next_button = QPushButton("下一页")
            self.last_button = QPushButton("末页")
            self.page_label = QLabel("第 1 / 1 页 · 共 0 条")
            self.page_size = QComboBox()
            for size in (50, 100, 200):
                self.page_size.addItem(f"每页 {size} 条", size)
            self.page_size.setCurrentIndex(1)
            self.first_button.clicked.connect(lambda: self._go_to_page(1))
            self.previous_button.clicked.connect(lambda: self._go_to_page(self._page - 1))
            self.next_button.clicked.connect(lambda: self._go_to_page(self._page + 1))
            self.last_button.clicked.connect(lambda: self._go_to_page(self._page_count))
            self.page_size.currentIndexChanged.connect(self._reset_filters)
            pager.addWidget(self.first_button)
            pager.addWidget(self.previous_button)
            pager.addWidget(self.next_button)
            pager.addWidget(self.last_button)
            pager.addWidget(self.page_label)
            pager.addStretch(1)
            pager.addWidget(self.page_size)
            layout.addLayout(pager)

        def update_snapshot(self, snapshot: DesktopSnapshot) -> None:
            signature = (
                len(snapshot.logs),
                snapshot.logs[0].created_at if snapshot.logs else None,
            )
            if signature == self._last_snapshot_signature:
                return
            self._last_snapshot_signature = signature
            self._load_page()

        def _reset_filters(self, *_args) -> None:
            self._page = 1
            self._load_page()

        def _schedule_search(self, *_args) -> None:
            if getattr(self._controller, "snapshot_runs_in_background", False):
                self._search_timer.start()
            else:
                self._search_timer.stop()
                self._reset_filters()

        def _go_to_page(self, page: int) -> None:
            self._page = max(1, min(int(page), self._page_count))
            self._load_page()

        def _load_page(self) -> None:
            level = str(self.level_filter.currentData() or "")
            query = self.search.text().strip()
            page = self._page
            page_size = int(self.page_size.currentData() or 100)

            def load():
                return self._controller.list_log_entries(
                    page=page,
                    page_size=page_size,
                    level=level,
                    query=query,
                )

            if getattr(self._controller, "snapshot_runs_in_background", False):
                if self._page_load_thread is not None and self._page_load_thread.isRunning():
                    self._page_load_queued = True
                    return
                self._page_load_queued = False
                thread = _ValueThread(load, self)
                thread.value_ready.connect(self._apply_log_page)
                thread.finished.connect(self._finish_log_page_load)
                self._page_load_thread = thread
                thread.start()
                return
            self._apply_log_page(load())

        def _apply_log_page(self, result: object) -> None:
            if not all(
                hasattr(result, attribute)
                for attribute in ("page", "page_count", "items", "total")
            ):
                return
            self._page = result.page
            self._page_count = result.page_count
            rows = tuple(result.items)
            if rows != self._rendered_log_items:
                scroll_state = _table_scroll_state(self.table)
                self.table.setUpdatesEnabled(False)
                self.table.setRowCount(len(rows))
                for row, entry in enumerate(rows):
                    values = (
                        _format_time(entry.created_at),
                        entry.level.value,
                        _operator_display(
                            entry.operator_name,
                            entry.operator_email,
                        ),
                        entry.task_id or "-",
                        entry.source,
                        entry.message,
                    )
                    for column, value in enumerate(values):
                        self.table.setItem(row, column, _readonly_item(value))
                self.table.setUpdatesEnabled(True)
                _restore_table_scroll_state(self.table, scroll_state)
                self._rendered_log_items = rows
            self.page_label.setText(
                f"第 {self._page} / {self._page_count} 页 · 共 {result.total} 条"
            )
            self.first_button.setEnabled(self._page > 1)
            self.previous_button.setEnabled(self._page > 1)
            self.next_button.setEnabled(self._page < self._page_count)
            self.last_button.setEnabled(self._page < self._page_count)

        def _finish_log_page_load(self) -> None:
            thread = self._page_load_thread
            self._page_load_thread = None
            if thread is not None:
                thread.deleteLater()
            if self._page_load_queued:
                self._page_load_queued = False
                QTimer.singleShot(0, self._load_page)
            window = self.window()
            if bool(getattr(window, "_close_pending", False)):
                QTimer.singleShot(0, window.close)

        def _selected_task_id(self) -> str | None:
            row = self.table.currentRow()
            if row < 0:
                return None
            item = self.table.item(row, 3)
            value = item.text().strip() if item is not None else ""
            return value if value and value != "-" else None

        def _show_full_log(self) -> None:
            task_id = self._selected_task_id()
            def show(value: object) -> None:
                title, content = value
                _show_log_viewer(
                    self,
                    title,
                    content,
                    hint=(
                        "完整日志已脱敏。扫描任务会显示 API 分页、逐订单匹配或"
                        "排除原因及安全异常堆栈。"
                    ),
                )

            _run_value_responsive(
                self,
                self._controller,
                lambda: self._controller.full_log_text(task_id),
                show,
                lambda error: self._result_handler(
                    ControlResult(False, f"读取完整日志失败：{type(error).__name__}。")
                ),
            )

        def _open_log_directory(self) -> None:
            def load() -> tuple[str, str, str]:
                path = self._controller.log_directory()
                title, content = self._controller.full_log_text()
                return path, title, content

            def show(value: object) -> None:
                path, title, content = value
                if (
                    path
                    and Path(path).is_dir()
                    and QDesktopServices.openUrl(QUrl.fromLocalFile(path))
                ):
                    return
                _show_log_viewer(
                    self,
                    title,
                    content,
                    hint=(
                        "共享客户端无法直接打开服务器日志目录，已改为显示服务器上的"
                        "最近应用日志；选中具体任务可查看对应完整日志。"
                    ),
                )

            _run_value_responsive(
                self,
                self._controller,
                load,
                show,
                lambda error: self._result_handler(
                    ControlResult(False, f"读取应用日志失败：{type(error).__name__}。")
                ),
            )

        def _delete_old_logs(self) -> None:
            if self._cleanup_thread is not None and self._cleanup_thread.isRunning():
                return
            days = int(self.cleanup_age_combo.currentData() or 90)
            months = 1 if days == 30 else 3
            answer = QMessageBox.question(
                self,
                "确认清理旧日志",
                f"即将永久删除工作区 logs 目录中 {months} 个月以前的日志文件。\n\n"
                "不会删除订单数据库、配置、队列或浏览器资料，"
                "但被删日志无法恢复。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.cleanup_button.setEnabled(False)
            self.cleanup_button.setText("正在清理…")
            thread = _ControlResultThread(
                lambda: self._controller.delete_logs_older_than(days),
                self,
            )
            self._cleanup_thread = thread
            thread.result_ready.connect(self._finish_log_cleanup)
            thread.finished.connect(thread.deleteLater)
            thread.start()

        def _finish_log_cleanup(self, result: ControlResult) -> None:
            self.cleanup_button.setEnabled(True)
            self.cleanup_button.setText("清理旧日志")
            self._cleanup_thread = None
            self._page = 1
            self._last_snapshot_signature = None
            self._load_page()
            self._result_handler(result)


    class DesktopMainWindow(QMainWindow):
        def __init__(
            self,
            controller: BackgroundTaskController,
            *,
            required_client_update_handler: Callable[[str], object] | None = None,
            runtime_restart_callback: Callable[[Path], None] | None = None,
        ) -> None:
            super().__init__()
            self._controller = controller
            self._required_client_update_handler = required_client_update_handler
            self._runtime_restart_callback = runtime_restart_callback
            self._client_update_thread: _RequiredClientUpdateThread | None = None
            self._client_update_attempted_version = ""
            self._active_interaction_id: str | None = None
            self._active_interaction_dialog: QDialog | None = None
            self._latest_snapshot: DesktopSnapshot | None = None
            self._api_wait_notice: QMessageBox | None = None
            self._task_status_baseline_ready = False
            self._known_task_statuses: dict[str, TaskStatus] = {}
            self._pending_local_logistics_scan_ids: set[str] = set()
            self._local_logistics_followup_thread: _ControlResultThread | None = None
            self._active_local_logistics_followup_scan_id: str | None = None
            self._local_logistics_followup_retry_delay_ms = 0
            self._notified_shipment_task_ids: set[str] = set()
            self._shipment_batches: dict[str, tuple[str, ...]] = {}
            self._notified_shipment_batch_ids: set[str] = set()
            self._pending_shipment_completion_notices: list[
                tuple[TaskRecord, ...]
            ] = []
            self._authentication_thread: _ControlResultThread | None = None
            self._execution_pause_active = False
            self._execution_pause_state = "active"
            self._execution_pause_thread: _ControlResultThread | None = None
            self._emergency_stop_active = False
            self._emergency_stop_thread: _ControlResultThread | None = None
            self._close_pending = False
            self._close_notice_shown = False
            self._snapshot_thread: _SnapshotThread | None = None
            self._refresh_queued = False
            self._background_snapshots = bool(
                getattr(controller, "snapshot_runs_in_background", False)
            )
            self._local_test_mode = is_local_test_mode()
            self._local_test_shared_server_mode = (
                is_local_test_shared_server_mode()
            )
            self._local_test_formal_baseline_version = (
                local_test_formal_baseline_version() or "未知"
            )
            self.setWindowTitle(
                f"ERP 自动化控制台（本机测试 · 源码 {CLIENT_VERSION}）"
                if self._local_test_mode
                else "ERP 自动化控制台"
            )
            self.resize(1360, 860)
            self.setMinimumSize(1080, 700)

            root = QWidget()
            root_layout = QHBoxLayout(root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            sidebar = QFrame()
            sidebar.setObjectName("sidebar")
            sidebar.setFixedWidth(206)
            sidebar_layout = QVBoxLayout(sidebar)
            sidebar_layout.setContentsMargins(16, 22, 16, 16)
            sidebar_layout.setSpacing(4)
            brand_title = QLabel(
                "ERP 自动化 · 本机测试"
                if self._local_test_mode
                else "ERP 自动化"
            )
            brand_title.setObjectName("brandTitle")
            brand_subtitle = QLabel(
                (
                    (
                        f"源码 {CLIENT_VERSION} / 正式基线 "
                        f"{self._local_test_formal_baseline_version}"
                    )
                    if self._local_test_shared_server_mode
                    else "隔离配置 / 当前分支源码"
                )
                if self._local_test_mode
                else "运营控制台"
            )
            brand_subtitle.setObjectName("brandSubtitle")
            sidebar_layout.addWidget(brand_title)
            sidebar_layout.addWidget(brand_subtitle)
            sidebar_layout.addSpacing(20)
            self.navigation = QListWidget()
            self.navigation.setObjectName("navigation")
            sidebar_layout.addWidget(self.navigation, 1)
            self.safety_panel = QFrame()
            self.safety_panel.setObjectName("safetyPanel")
            safety_layout = QVBoxLayout(self.safety_panel)
            safety_layout.setContentsMargins(11, 9, 11, 10)
            safety_layout.setSpacing(3)
            safety_title = QLabel("写入安全")
            safety_title.setObjectName("safetyTitle")
            safety_layout.addWidget(safety_title)
            self.global_emergency_state = QLabel("●  写入正常")
            self.global_emergency_state.setObjectName("safetyState")
            safety_layout.addWidget(self.global_emergency_state)
            self.local_connection_state = QLabel("本地连接正常")
            self.local_connection_state.setObjectName("safetyDetail")
            safety_layout.addWidget(self.local_connection_state)
            self.operator_identity_state = QLabel("尚未验证企业邮箱")
            self.operator_identity_state.setObjectName("safetyDetail")
            self.operator_identity_state.setWordWrap(True)
            safety_layout.addWidget(self.operator_identity_state)
            self.scheduler_state = QLabel("定时扫描：正在选举")
            self.scheduler_state.setObjectName("safetyDetail")
            safety_layout.addWidget(self.scheduler_state)
            safety_layout.addSpacing(5)
            self.local_pause_button = QPushButton("暂停本机任务")
            self.local_pause_button.setObjectName("localPauseButton")
            self.local_pause_button.setToolTip(
                "只禁止当前电脑提交新任务并停止当前电脑已有任务；"
                "不会改变全局 ERP 写入急停，也不会影响其他电脑。"
            )
            self.local_pause_button.clicked.connect(
                self._toggle_local_execution_pause
            )
            safety_layout.addWidget(self.local_pause_button)
            self.global_emergency_button = QPushButton("紧急停止写入")
            self.global_emergency_button.setObjectName("globalEmergencyButton")
            self.global_emergency_button.setToolTip(
                "全局停止定制订单和自动标发的后续 ERP 写入；"
                "已经发送的请求会先安全返回。"
            )
            self.global_emergency_button.clicked.connect(
                self._toggle_global_emergency_stop
            )
            safety_layout.addWidget(self.global_emergency_button)
            sidebar_layout.addWidget(self.safety_panel)
            self.pages = QStackedWidget()
            content = QWidget()
            content_layout = QVBoxLayout(content)
            content_layout.setContentsMargins(0, 0, 0, 0)
            content_layout.setSpacing(0)
            self.local_test_banner = QLabel(
                (
                    f"本机测试运行：源码目标 {CLIENT_VERSION}，正式连接基线 "
                    f"{self._local_test_formal_baseline_version}。当前窗口执行工作分支"
                    "客户端源码并连接正式共享服务；"
                    "本机文件与正式客户端隔离，但订单来自正式业务环境，任何写入都会影响"
                    "真实数据。本窗口不代表已发布版本。"
                    if self._local_test_shared_server_mode
                    else (
                        "本机测试运行：当前窗口直接执行工作分支源码，"
                        "数据与正式版隔离，不代表已发布版本。"
                    )
                )
            )
            self.local_test_banner.setObjectName("localTestBanner")
            self.local_test_banner.setWordWrap(True)
            self.local_test_banner.setVisible(self._local_test_mode)
            content_layout.addWidget(self.local_test_banner)
            self.execution_pause_banner = QLabel(
                "本机任务已暂停：本机新任务会被直接拒绝，不会静默排队；"
                "其他电脑和 ERP 写入急停不受影响。"
            )
            self.execution_pause_banner.setObjectName("emergencyBanner")
            self.execution_pause_banner.setWordWrap(True)
            self.execution_pause_banner.hide()
            content_layout.addWidget(self.execution_pause_banner)
            self.emergency_banner = QLabel(
                "已紧急停止所有 ERP 写入。只读扫描和日志查看仍可继续；"
                "如需恢复，请使用左侧“解除急停”。"
            )
            self.emergency_banner.setObjectName("emergencyBanner")
            self.emergency_banner.setWordWrap(True)
            self.emergency_banner.hide()
            content_layout.addWidget(self.emergency_banner)
            content_layout.addWidget(self.pages, 1)
            root_layout.addWidget(sidebar)
            root_layout.addWidget(content, 1)
            self.setCentralWidget(root)

            self.dashboard_page = DashboardPage()
            self.custom_orders_page = CustomOrdersPage(controller, self._show_result)
            self.alibaba_order_page = AlibabaOrderPage(
                controller,
                self._show_result,
            )
            self.shipment_page = ShipmentPage(
                controller,
                self._show_result,
                self._register_shipment_batch,
                self._register_shipment_scan_followup,
            )
            self.notification_page = ShipmentNotificationPage(
                controller,
                self._show_result,
            )
            self.state_page = StateManagementPage(controller, self._show_result)
            self.settings_page = SettingsPage(controller, self._show_result)
            self.logs_page = LogsPage(controller, self._show_result)
            pages = (
                ("概览", self.dashboard_page),
                ("定制订单", self.custom_orders_page),
                ("阿里物流下单", self.alibaba_order_page),
                ("自动标发", self.shipment_page),
                ("客户通知审核", self.notification_page),
                ("状态管理", self.state_page),
                ("设置", self.settings_page),
                ("日志", self.logs_page),
            )
            self._page_widgets = tuple(page for _label, page in pages)
            for label, page in pages:
                self.navigation.addItem(QListWidgetItem(label))
                self.pages.addWidget(page)
            self.navigation.currentRowChanged.connect(self._on_navigation_changed)
            self.navigation.setCurrentRow(0)

            self.setStyleSheet(_modern_stylesheet())
            self.statusBar().showMessage("正在读取本地状态…")

            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(1000)
            self._custom_scan_timer = QTimer(self)
            self._custom_scan_timer.setSingleShot(True)
            self._custom_scan_timer.timeout.connect(self._run_automatic_custom_scan)
            self._shipment_scan_timer = QTimer(self)
            self._shipment_scan_timer.setSingleShot(True)
            self._shipment_scan_timer.timeout.connect(self._run_automatic_shipment_scan)
            self.refresh()

        def _on_navigation_changed(self, index: int) -> None:
            if index < 0 or index >= len(self._page_widgets):
                return
            self.pages.setCurrentIndex(index)
            if self._latest_snapshot is not None:
                self._page_widgets[index].update_snapshot(self._latest_snapshot)

        def _toggle_local_execution_pause(self) -> None:
            if (
                self._execution_pause_thread is not None
                and self._execution_pause_thread.isRunning()
            ):
                return
            enabled = not self._execution_pause_active
            if enabled:
                instance_id = str(
                    getattr(self._controller, "instance_id", "") or ""
                ).strip()
                active_count = sum(
                    1
                    for task in (
                        self._latest_snapshot.tasks
                        if self._latest_snapshot is not None
                        else ()
                    )
                    if not task.status.terminal
                    and (
                        not instance_id
                        or str(
                            task.payload.get(DESKTOP_INSTANCE_ID_PAYLOAD_KEY) or ""
                        ).strip()
                        == instance_id
                    )
                )
                answer = QMessageBox.warning(
                    self,
                    "暂停本机任务",
                    f"将立即拒绝本机后续人工任务和定时任务，不会创建记录或静默排队；\n"
                    f"当前本机 {active_count} 个任务将停止：排队/等待/只读任务直接停止，"
                    "已发出的 ERP 写入会等待返回或超时，且不再执行后续步骤。\n\n"
                    "此操作不会改变 ERP 写入急停，也不会影响其他电脑。是否继续？",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.local_pause_button.setEnabled(False)
            self.local_pause_button.setText(
                "正在停止本机任务…" if enabled else "正在恢复本机任务…"
            )
            thread = _ControlResultThread(
                lambda requested=enabled: self._controller.set_execution_paused(
                    requested,
                    "用户从主界面暂停本机任务。" if requested else "",
                ),
                self,
            )
            thread.result_ready.connect(
                lambda result, requested=enabled: self._finish_execution_pause(
                    result,
                    requested,
                )
            )
            thread.finished.connect(thread.deleteLater)
            self._execution_pause_thread = thread
            thread.start()

        def _finish_execution_pause(
            self,
            result: ControlResult,
            requested_enabled: bool,
        ) -> None:
            self._execution_pause_thread = None
            self.local_pause_button.setEnabled(True)
            if result.accepted:
                state = str(
                    result.details.get("instance_execution_pause_state")
                    or ("paused" if requested_enabled else "active")
                )
                self._sync_local_execution_pause(
                    requested_enabled,
                    state=state,
                    target_count=int(result.details.get("target_count") or 0),
                    stopped_count=int(result.details.get("stopped_count") or 0),
                    stopping_count=int(result.details.get("stopping_count") or 0),
                    review_count=int(result.details.get("review_count") or 0),
                )
            else:
                self._sync_local_execution_pause(
                    self._execution_pause_active,
                    state=self._execution_pause_state,
                )
            if result.accepted and requested_enabled:
                self._close_active_interaction_dialog()
                QTimer.singleShot(0, self._show_next_interaction)
            self._show_result(result)

        def _sync_local_execution_pause(
            self,
            active: bool,
            *,
            state: str = "paused",
            target_count: int = 0,
            stopped_count: int = 0,
            stopping_count: int = 0,
            review_count: int = 0,
        ) -> None:
            self._execution_pause_active = bool(active)
            self._execution_pause_state = state if active else "active"
            self.local_pause_button.setText(
                "正在停止本机任务…"
                if self._execution_pause_state == "pausing"
                else "恢复本机任务"
                if self._execution_pause_active
                else "暂停本机任务"
            )
            supported = bool(
                getattr(self._controller, "instance_pause_supported", True)
            )
            if (
                self._execution_pause_thread is not None
                and self._execution_pause_thread.isRunning()
            ):
                self.local_pause_button.setEnabled(False)
            else:
                self.local_pause_button.setEnabled(
                    supported and self._execution_pause_state != "pausing"
                )
            if not supported:
                self.local_pause_button.setToolTip(
                    "当前服务端版本不支持本机级暂停。为避免误停其他电脑，此功能已禁用。"
                )
            self.execution_pause_banner.setVisible(
                self._execution_pause_active
            )
            if self._execution_pause_state == "pausing":
                self.execution_pause_banner.setText(
                    f"正在停止本机任务：已停止 {stopped_count}/{target_count}，"
                    f"仍有 {stopping_count} 个正在等待外部请求返回或到达安全停止点；"
                    "停止完成前不能恢复。"
                )
            elif self._execution_pause_active:
                self.execution_pause_banner.setText(
                    f"本机任务已暂停：已停止 {stopped_count}/{target_count}，"
                    f"人工复核 {review_count} 个。本机新任务会被直接拒绝；"
                    "其他电脑及 ERP 写入急停不受影响。"
                )
            self.scheduler_state.setText(
                "定时扫描：本机已退出调度"
                if self._execution_pause_active
                else self.scheduler_state.text()
            )

        def _toggle_global_emergency_stop(self) -> None:
            if (
                self._emergency_stop_thread is not None
                and self._emergency_stop_thread.isRunning()
            ):
                return
            enabled = not self._emergency_stop_active
            self.global_emergency_button.setEnabled(False)
            self.global_emergency_button.setText(
                "正在紧急停止…" if enabled else "正在解除急停…"
            )
            thread = _ControlResultThread(
                lambda requested=enabled: self._controller.set_emergency_stop_writes(
                    requested
                ),
                self,
            )
            thread.result_ready.connect(
                lambda result, requested=enabled: self._finish_emergency_stop(
                    result,
                    requested,
                )
            )
            self._emergency_stop_thread = thread
            thread.start()

        def _finish_emergency_stop(
            self,
            result: ControlResult,
            requested_enabled: bool,
        ) -> None:
            thread = self._emergency_stop_thread
            self._emergency_stop_thread = None
            self.global_emergency_button.setEnabled(True)
            if result.accepted:
                self._sync_global_emergency_stop(requested_enabled)
            else:
                self._sync_global_emergency_stop(self._emergency_stop_active)
            self._show_result(result)
            if thread is not None:
                thread.deleteLater()

        def _sync_global_emergency_stop(self, active: bool) -> None:
            self._emergency_stop_active = bool(active)
            self.global_emergency_button.setText(
                "解除急停"
                if self._emergency_stop_active
                else "紧急停止"
            )
            if (
                self._emergency_stop_thread is not None
                and self._emergency_stop_thread.isRunning()
            ):
                self.global_emergency_button.setEnabled(False)
                self.global_emergency_button.setText(
                    "正在解除急停…"
                    if self._emergency_stop_active
                    else "正在紧急停止…"
                )
            else:
                self.global_emergency_button.setEnabled(True)
            self.global_emergency_button.setToolTip(
                "全局停止定制订单和自动标发的后续 ERP 写入；"
                "已经发送的请求会先安全返回。"
            )
            for widget in (
                self.safety_panel,
                self.global_emergency_state,
                self.global_emergency_button,
            ):
                widget.setProperty("emergencyActive", self._emergency_stop_active)
                style = widget.style()
                style.unpolish(widget)
                style.polish(widget)
            self.global_emergency_state.setText(
                "●  已紧急停止"
                if self._emergency_stop_active
                else "●  写入正常"
            )
            self.local_connection_state.setText(
                "只读功能仍可使用"
                if self._emergency_stop_active
                else "本地连接正常"
            )
            self.emergency_banner.setVisible(self._emergency_stop_active)

        def refresh(self) -> None:
            if self._background_snapshots:
                if self._snapshot_thread is not None:
                    self._refresh_queued = True
                    return
                self._refresh_queued = False
                thread = _SnapshotThread(self._controller.snapshot, self)
                thread.snapshot_ready.connect(self._apply_snapshot)
                thread.snapshot_failed.connect(self._snapshot_failed)
                thread.finished.connect(self._snapshot_finished)
                self._snapshot_thread = thread
                thread.start()
                return
            self._apply_snapshot(self._controller.snapshot())

        def _snapshot_failed(self, error: object) -> None:
            if isinstance(error, CoordinationClientUpdateRequired):
                self._begin_required_client_update(error.required_version)
                return
            message = (
                str(error)
                if isinstance(error, str)
                else f"后台状态同步失败：{type(error).__name__}。"
            )
            self.statusBar().showMessage(message, 8000)

        def _begin_required_client_update(self, required_version: str) -> None:
            if self._client_update_thread is not None:
                return
            normalized_version = str(required_version or "").strip()
            if normalized_version == self._client_update_attempted_version:
                self.statusBar().showMessage(
                    "客户端更新尚未完成，请关闭并重新打开程序后重试。",
                    15000,
                )
                return
            if self._required_client_update_handler is None:
                self.statusBar().showMessage(
                    "服务器要求更新客户端，请退出后安装最新版。",
                    15000,
                )
                return
            self._client_update_attempted_version = normalized_version
            self.statusBar().showMessage(
                "服务器要求更新客户端，正在打开安全更新窗口…"
            )
            thread = _RequiredClientUpdateThread(
                self._required_client_update_handler,
                normalized_version,
                self,
            )
            self._client_update_thread = thread
            thread.result_ready.connect(self._required_client_update_finished)
            thread.update_failed.connect(self._required_client_update_failed)
            thread.finished.connect(self._required_client_update_thread_finished)
            thread.start()

        def _required_client_update_finished(self, result: object) -> None:
            status = str(getattr(result, "status", "") or "").strip()
            application_path = getattr(result, "application_path", None)
            if status == "updated" and isinstance(application_path, Path):
                if self._runtime_restart_callback is not None:
                    self._runtime_restart_callback(application_path)
                self.statusBar().showMessage(
                    "新版本已校验并安装，当前任务安全结束后将自动重启。"
                )
                self.close()
                return
            if status in {"user_exit", "repair_scheduled"}:
                self.statusBar().showMessage(
                    "当前版本已不能继续连接，正在安全退出。"
                )
                self.close()
                return
            self._required_client_update_failed(
                RuntimeError(
                    "服务器要求更新客户端，但正式更新通道尚未提供匹配版本。"
                    "请稍后重新检查更新，或联系管理员完成发布激活。"
                )
            )

        def _required_client_update_failed(self, error: object) -> None:
            self.statusBar().showMessage("客户端更新未完成。", 15000)
            from .modern_dialogs import show_packaged_client_error_dialog

            show_packaged_client_error_dialog(
                "服务器已停止接受当前客户端版本，但自动更新未完成。\n\n"
                f"{error}\n\n"
                "程序没有执行新的业务写入；服务器中已经开始的任务仍会继续运行。",
                parent=self,
            )

        def _required_client_update_thread_finished(self) -> None:
            thread = self._client_update_thread
            self._client_update_thread = None
            if thread is not None:
                thread.deleteLater()
            if self._close_pending:
                QTimer.singleShot(0, self.close)

        def _snapshot_finished(self) -> None:
            thread = self._snapshot_thread
            self._snapshot_thread = None
            if thread is not None:
                thread.deleteLater()
            if self._close_pending:
                QTimer.singleShot(0, self.close)
                return
            if self._refresh_queued:
                self._refresh_queued = False
                QTimer.singleShot(0, self.refresh)

        def _apply_snapshot(self, snapshot: DesktopSnapshot) -> None:
            self._sync_scheduled_scan_timers(snapshot)
            unchanged = snapshot is self._latest_snapshot
            self.custom_orders_page.set_scan_countdown(
                self._custom_scan_timer.remainingTime()
            )
            self.shipment_page.set_scan_countdown(
                self._shipment_scan_timer.remainingTime()
            )
            if unchanged:
                return
            self._sync_api_wait_notice(snapshot)
            self._capture_shipment_completion_notices(snapshot)
            self._capture_local_logistics_followups(snapshot)
            self._latest_snapshot = snapshot
            instance_pause_supported = bool(
                getattr(self._controller, "instance_pause_supported", True)
            )
            local_pause_active = bool(
                snapshot.policy.instance_execution_paused
                or (
                    instance_pause_supported
                    and snapshot.policy.execution_paused
                )
            )
            self._sync_local_execution_pause(
                local_pause_active,
                state=snapshot.policy.instance_execution_pause_state,
                target_count=snapshot.policy.instance_pause_target_count,
                stopped_count=snapshot.policy.instance_pause_stopped_count,
                stopping_count=snapshot.policy.instance_pause_stopping_count,
                review_count=snapshot.policy.instance_pause_review_count,
            )
            self._sync_global_emergency_stop(
                snapshot.policy.emergency_stop_writes
            )
            if bool(
                getattr(self._controller, "authentication_required", False)
            ):
                self.local_connection_state.setText("企业邮箱登录已过期")
            self.operator_identity_state.setText(
                (
                    f"操作者：{snapshot.operator_name}\n{snapshot.operator_email}"
                    if snapshot.operator_name
                    and snapshot.operator_email
                    and snapshot.operator_name != snapshot.operator_email
                    else f"操作者：{snapshot.operator_email or snapshot.operator_name}"
                )
                if snapshot.operator_email or snapshot.operator_name
                else "尚未验证企业邮箱"
            )
            self.scheduler_state.setText(
                "定时扫描：已暂停"
                if snapshot.policy.execution_paused
                else (
                    "定时扫描：本机负责"
                    if snapshot.is_scheduler_leader
                    else "定时扫描：其他在线客户端负责"
                )
            )
            index = self.navigation.currentRow()
            if 0 <= index < len(self._page_widgets):
                self._page_widgets[index].update_snapshot(snapshot)
            self.statusBar().showMessage(
                f"状态已同步  ·  定制订单 {len(snapshot.custom_orders)}  ·  "
                f"自动标发 {len(snapshot.shipments)}  ·  后台任务 {len(snapshot.tasks)}"
            )

        def _sync_api_wait_notice(self, snapshot: DesktopSnapshot) -> None:
            active_api_scans = [
                task
                for task in snapshot.tasks
                if not task.status.terminal
                and task.capability is Capability.LIST_ORDERS
                and task.area is TaskArea.SHIPMENT
                and task.payload.get(
                    _LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY
                ) is True
            ]
            if active_api_scans:
                if self._api_wait_notice is None:
                    notice = QMessageBox(self)
                    notice.setIcon(QMessageBox.Icon.Information)
                    notice.setWindowTitle("正在通过 API 查询")
                    notice.setText(
                        "程序正在通过官方 API 查询订单，请稍候。\n\n"
                        "不需要点击确认，也不会为了等待 API 而预先打开"
                        "阿里巴巴国际物流网页；查询结束后本提示会自动关闭。"
                    )
                    notice.setStandardButtons(QMessageBox.StandardButton.NoButton)
                    notice.setModal(False)
                    notice.show()
                    self._api_wait_notice = notice
                return
            if self._api_wait_notice is not None:
                self._api_wait_notice.close()
                self._api_wait_notice.deleteLater()
                self._api_wait_notice = None

        def _sync_scheduled_scan_timers(
            self,
            snapshot: DesktopSnapshot,
        ) -> None:
            schedules = (
                (
                    self._custom_scan_timer,
                    "five_minute_timer",
                    _CUSTOM_AUTO_SCAN_INTERVAL_MS,
                ),
                (
                    self._shipment_scan_timer,
                    "three_hour_timer",
                    _SHIPMENT_AUTO_SCAN_INTERVAL_MS,
                ),
            )
            now = datetime.now(timezone.utc)
            for timer, trigger, interval_ms in schedules:
                desired = _scheduled_scan_delay_ms(
                    snapshot,
                    trigger=trigger,
                    default_interval_ms=interval_ms,
                    now=now,
                )
                if desired is None:
                    timer.stop()
                    continue
                remaining = timer.remainingTime()
                if remaining < 0 or abs(remaining - desired) > 1500:
                    timer.start(desired)

            if self._close_pending:
                prepare_close = getattr(self._controller, "prepare_close", None)
                result = (
                    prepare_close()
                    if callable(prepare_close)
                    else ControlResult(not any(not task.status.terminal for task in snapshot.tasks), "")
                )
                self.statusBar().showMessage(result.message)
                if result.accepted:
                    self._close_pending = False
                    QTimer.singleShot(0, self.close)
                return
            self._show_next_interaction()
            self._show_next_shipment_completion_notice()

        def _capture_shipment_completion_notices(
            self,
            snapshot: DesktopSnapshot,
        ) -> None:
            current_statuses = {task.task_id: task.status for task in snapshot.tasks}
            shipment_tasks = [
                task
                for task in snapshot.tasks
                if task.area is TaskArea.SHIPMENT
                and task.capability is Capability.OUTBOUND_ORDER
            ]
            tasks_by_id = {task.task_id: task for task in shipment_tasks}
            discovered_batches: dict[str, list[TaskRecord]] = {}
            for task in shipment_tasks:
                batch_id = str(task.payload.get("shipment_batch_id") or "").strip()
                if batch_id:
                    discovered_batches.setdefault(batch_id, []).append(task)

            if not self._task_status_baseline_ready:
                # Do not replay old successful tasks every time the desktop is
                # opened.  Active batches are retained so a completion that
                # happens after startup still earns one batch notification.
                self._notified_shipment_task_ids.update(
                    task.task_id
                    for task in shipment_tasks
                    if task.status.terminal
                )
                for batch_id, tasks in discovered_batches.items():
                    if any(not task.status.terminal for task in tasks):
                        ordered = sorted(
                            tasks,
                            key=lambda task: int(
                                task.payload.get("shipment_batch_position") or 0
                            ),
                        )
                        self._shipment_batches.setdefault(
                            batch_id,
                            tuple(task.task_id for task in ordered),
                        )
                    else:
                        self._notified_shipment_batch_ids.add(batch_id)
                self._task_status_baseline_ready = True
                self._known_task_statuses = current_statuses
                return

            for batch_id, tasks in discovered_batches.items():
                if (
                    batch_id not in self._shipment_batches
                    and batch_id not in self._notified_shipment_batch_ids
                    and any(not task.status.terminal for task in tasks)
                ):
                    ordered = sorted(
                        tasks,
                        key=lambda task: int(
                            task.payload.get("shipment_batch_position") or 0
                        ),
                    )
                    self._shipment_batches[batch_id] = tuple(
                        task.task_id for task in ordered
                    )

            for task in reversed(shipment_tasks):
                batch_id = str(task.payload.get("shipment_batch_id") or "").strip()
                previous = self._known_task_statuses.get(task.task_id)
                if (
                    not batch_id
                    and task.status.terminal
                    and (previous is None or not previous.terminal)
                    and task.task_id not in self._notified_shipment_task_ids
                ):
                    self._notified_shipment_task_ids.add(task.task_id)
                    self._pending_shipment_completion_notices.append((task,))

            for batch_id, task_ids in tuple(self._shipment_batches.items()):
                if batch_id in self._notified_shipment_batch_ids:
                    self._shipment_batches.pop(batch_id, None)
                    continue
                tasks = [tasks_by_id[task_id] for task_id in task_ids if task_id in tasks_by_id]
                if len(tasks) != len(task_ids) or not all(task.status.terminal for task in tasks):
                    continue
                tasks.sort(
                    key=lambda task: int(task.payload.get("shipment_batch_position") or 0)
                )
                self._notified_shipment_batch_ids.add(batch_id)
                self._notified_shipment_task_ids.update(task_ids)
                self._pending_shipment_completion_notices.append(tuple(tasks))
                self._shipment_batches.pop(batch_id, None)
            self._known_task_statuses = current_statuses

        def _register_shipment_batch(
            self,
            batch_id: str,
            task_ids: tuple[str, ...],
        ) -> None:
            normalized_batch_id = str(batch_id or "").strip()
            normalized_task_ids = tuple(
                task_id for task_id in (str(value or "").strip() for value in task_ids) if task_id
            )
            if normalized_batch_id and normalized_task_ids:
                self._shipment_batches[normalized_batch_id] = normalized_task_ids

        def _register_shipment_scan_followup(self, task_id: str) -> None:
            normalized_task_id = str(task_id or "").strip()
            if normalized_task_id:
                self._pending_local_logistics_scan_ids.add(normalized_task_id)

        def _capture_local_logistics_followups(
            self,
            snapshot: DesktopSnapshot,
        ) -> None:
            if snapshot.policy.execution_paused:
                return
            if (
                self._local_logistics_followup_thread is not None
                and self._local_logistics_followup_thread.isRunning()
            ):
                return
            if not self._pending_local_logistics_scan_ids:
                return
            active_query = any(
                task.area is TaskArea.SHIPMENT
                and task.capability is Capability.ALIBABA_LOGISTICS
                and not task.status.terminal
                for task in snapshot.tasks
            )
            if active_query:
                return
            tasks_by_id = {task.task_id: task for task in snapshot.tasks}
            for scan_task_id in tuple(self._pending_local_logistics_scan_ids):
                scan_task = tasks_by_id.get(scan_task_id)
                if scan_task is None or not scan_task.status.terminal:
                    continue
                if scan_task.status in {TaskStatus.CANCELLED, TaskStatus.PAUSED}:
                    self._pending_local_logistics_scan_ids.discard(scan_task_id)
                    continue
                command = TaskCommand(
                    name="领星扫描后在本机查询阿里物流",
                    area=TaskArea.SHIPMENT,
                    capability=Capability.ALIBABA_LOGISTICS,
                    payload={
                        "trigger": "after_shipment_scan",
                        "source_scan_task_id": scan_task_id,
                    },
                )
                self.statusBar().showMessage(
                    "领星扫描已结束，正在启动本机可见 Chrome 查询阿里物流；"
                    "如出现登录或安全验证，请直接在打开的网页中处理。",
                    15000,
                )

                def submit_followup(
                    logistics_command: TaskCommand = command,
                ) -> ControlResult:
                    return self._controller.submit_task(
                        logistics_command
                    )

                thread = _ControlResultThread(
                    submit_followup,
                    self,
                )
                thread.result_ready.connect(
                    self._handle_local_logistics_followup_result
                )
                thread.finished.connect(
                    self._finish_local_logistics_followup_thread
                )
                self._active_local_logistics_followup_scan_id = scan_task_id
                self._local_logistics_followup_thread = thread
                thread.start()
                return

        def _handle_local_logistics_followup_result(
            self,
            result: ControlResult,
        ) -> None:
            if result.accepted:
                if self._active_local_logistics_followup_scan_id:
                    self._pending_local_logistics_scan_ids.discard(
                        self._active_local_logistics_followup_scan_id
                    )
                self._local_logistics_followup_retry_delay_ms = 0
                self.statusBar().showMessage(
                    "阿里物流查询已交给本机可见 Chrome；"
                    "遇到登录、验证码或安全验证时请在该窗口完成操作。",
                    15000,
                )
            else:
                self._local_logistics_followup_retry_delay_ms = min(
                    60_000,
                    max(
                        2_000,
                        self._local_logistics_followup_retry_delay_ms * 2,
                    ),
                )
                self.statusBar().showMessage(
                    "本机阿里物流查询未启动，将自动重试；"
                    "客户通知补偿已由服务端持久队列接管："
                    + result.message,
                    15000,
                )

        def _finish_local_logistics_followup_thread(self) -> None:
            thread = self._local_logistics_followup_thread
            self._local_logistics_followup_thread = None
            self._active_local_logistics_followup_scan_id = None
            if thread is not None:
                thread.deleteLater()
            if self._close_pending:
                QTimer.singleShot(0, self.close)
            elif self._latest_snapshot is not None:
                QTimer.singleShot(
                    self._local_logistics_followup_retry_delay_ms,
                    lambda: self._capture_local_logistics_followups(
                        self._latest_snapshot
                    ),
                )

        def _show_next_shipment_completion_notice(self) -> None:
            if self._active_interaction_id is not None:
                return
            if self._controller.pending_interactions():
                return
            if not self._pending_shipment_completion_notices:
                return
            tasks = self._pending_shipment_completion_notices.pop(0)
            succeeded = [task for task in tasks if task.status is TaskStatus.SUCCEEDED]
            failed = [task for task in tasks if task.status is TaskStatus.FAILED]
            review = [task for task in tasks if task.status is TaskStatus.BLOCKED]
            paused = [
                task
                for task in tasks
                if task.status in {TaskStatus.CANCELLED, TaskStatus.PAUSED}
            ]

            def task_lines(items: Sequence[TaskRecord], *, include_result: bool) -> str:
                lines: list[str] = []
                for task in items[:10]:
                    platform = str(task.order_no or "").strip() or "-"
                    system = str(task.payload.get("system_order_no") or "").strip() or "-"
                    logistics = str(task.payload.get("logistics_no") or "").strip() or "-"
                    line = f"• {platform} / {system} / {logistics}"
                    if include_result:
                        line += f" — {task.status.label}：{task.message or '-'}"
                    lines.append(line)
                if len(items) > 10:
                    lines.append(f"• ……另有 {len(items) - 10} 张")
                return "\n".join(lines)

            sections = [
                "本批自动标发任务已全部结束。",
                (
                    f"结果：成功 {len(succeeded)}，失败 {len(failed)}，"
                    f"需人工复核 {len(review)}，暂停/取消 {len(paused)}。"
                ),
            ]
            if succeeded:
                sections.extend(("成功订单：", task_lines(succeeded, include_result=False)))
            problems = failed + review + paused
            if problems:
                sections.extend(("未成功订单：", task_lines(problems, include_result=True)))
            QMessageBox.information(
                self,
                "自动标发完成",
                "\n\n".join(sections),
            )

        def _run_automatic_custom_scan(self) -> None:
            self._custom_scan_timer.start(_CUSTOM_AUTO_SCAN_INTERVAL_MS)
            self._submit_automatic_scan(
                area=TaskArea.CUSTOMIZATION,
                name="定制订单五分钟自动扫描",
                trigger="five_minute_timer",
            )

        def _run_automatic_shipment_scan(self) -> None:
            self._shipment_scan_timer.start(_SHIPMENT_AUTO_SCAN_INTERVAL_MS)
            self._submit_automatic_scan(
                area=TaskArea.SHIPMENT,
                name="自动标发三小时自动扫描",
                trigger="three_hour_timer",
            )

        def _submit_automatic_scan(
            self,
            *,
            area: TaskArea,
            name: str,
            trigger: str,
        ) -> None:
            snapshot = self._latest_snapshot
            if snapshot is None:
                # Persistent and coordinated controllers load SQLite/network
                # state on a worker. Do not make an automatic timer turn that
                # cold read back into a GUI-thread stall.
                if self._background_snapshots:
                    self.refresh()
                    return
                snapshot = self._controller.snapshot()
            if (
                not snapshot.is_scheduler_leader
                or snapshot.policy.execution_paused
            ):
                return
            scan_active = any(
                task.area is area
                and (
                    task.capability is Capability.LIST_ORDERS
                    or (
                        area is TaskArea.SHIPMENT
                        and task.capability is Capability.ALIBABA_LOGISTICS
                    )
                )
                and not task.status.terminal
                for task in snapshot.tasks
            )
            if scan_active:
                return
            command = TaskCommand(
                name=name,
                area=area,
                capability=Capability.LIST_ORDERS,
                payload={
                    "trigger": trigger,
                    **(
                        {_LOCAL_LOGISTICS_FOLLOWUP_PAYLOAD_KEY: True}
                        if area is TaskArea.SHIPMENT
                        else {}
                    ),
                },
            )

            def finish(result: ControlResult) -> None:
                if (
                    area is TaskArea.SHIPMENT
                    and result.accepted
                    and result.task_id
                ):
                    self._register_shipment_scan_followup(result.task_id)
                self.statusBar().showMessage(
                    result.message
                    if result.accepted
                    else f"后台扫描未提交：{result.message}",
                    8000,
                )

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.submit_task(command),
                finish,
            )

        def _show_next_interaction(self) -> None:
            requests = self._controller.pending_interactions()
            if self._active_interaction_id is not None:
                if any(
                    request.request_id == self._active_interaction_id
                    for request in requests
                ):
                    return
                self._close_active_interaction_dialog()
                self._active_interaction_id = None
            if not requests:
                return
            request = requests[0]
            self._active_interaction_id = request.request_id
            if request.stage == "alibaba_order:quote_details":
                displayed = self.alibaba_order_page.apply_quote_details(request)
                response = DesktopInteractionResponse(
                    request_id=request.request_id,
                    accepted=True,
                )
                if displayed:
                    self.statusBar().showMessage(
                        "阿里查价资料已显示，可一键复制邮编。",
                        10000,
                    )
                self._submit_interaction_response(response)
            else:
                self._interaction_dialog(request)

        def _submit_interaction_response(
            self,
            response: DesktopInteractionResponse,
        ) -> None:
            def finish(result: ControlResult) -> None:
                if not result.accepted:
                    self._show_result(result)
                if self._active_interaction_id == response.request_id:
                    self._active_interaction_id = None
                self._close_active_interaction_dialog()
                QTimer.singleShot(0, self._show_next_interaction)

            _run_control_result_responsive(
                self,
                self._controller,
                lambda: self._controller.respond_interaction(response),
                finish,
            )

        def _interaction_dialog(
            self,
            request: DesktopInteractionRequest,
        ) -> None:
            dialog = QDialog(self)
            dialog.setWindowTitle(request.title)
            dialog.setModal(False)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.resize(760, 520)
            layout = QVBoxLayout(dialog)

            stage = QLabel(f"当前阶段：{_interaction_stage_label(request.stage)}")
            stage.setStyleSheet("font-weight: bold;")
            layout.addWidget(stage)
            viewer = QPlainTextEdit()
            viewer.setReadOnly(True)
            viewer.setPlainText(request.message)
            layout.addWidget(viewer, 1)

            option_box: QComboBox | None = None
            if request.options:
                layout.addWidget(QLabel("请选择："))
                option_box = QComboBox()
                if request.stage in {
                    "erp_mark:wms_outbound_select",
                    "notification:recipient_name_select",
                }:
                    option_box.addItem("请选择一个候选项（必须明确选择）", None)
                for option in request.options:
                    label = option.label
                    if option.description:
                        label = f"{label} — {option.description}"
                    option_box.addItem(label, option.value)
                layout.addWidget(option_box)

            informational = request.stage == "buyer_cancelled"
            if not informational:
                warning = QLabel(
                    (
                        "请先在已打开的 Chrome 中完成验证和登录，再点击“已完成登录，继续读取”。"
                        if request.stage == "shipment:alibaba_manual_login"
                        else (
                            "暂不选择会生成审核页可见的异常记录，并保留自动重试与失败告警。"
                            if request.stage == "notification:recipient_name_select"
                            else "拒绝会安全停止当前阶段。只有 API 明确证明尚未执行时，才会出现网页回退确认。"
                        )
                    )
                )
                warning.setWordWrap(True)
                warning.setStyleSheet("color: #9a6700;")
                layout.addWidget(warning)

            if informational:
                buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
                buttons.button(QDialogButtonBox.StandardButton.Ok).setText(request.approve_label)
                buttons.accepted.connect(dialog.accept)
            else:
                buttons = QDialogButtonBox(
                    QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No
                )
                approve = buttons.button(QDialogButtonBox.StandardButton.Yes)
                reject = buttons.button(QDialogButtonBox.StandardButton.No)
                approve.setText(request.approve_label)
                reject.setText(request.reject_label)
                reject.setDefault(True)
                if request.stage in {
                    "erp_mark:wms_outbound_select",
                    "notification:recipient_name_select",
                } and option_box is not None:
                    approve.setEnabled(False)
                    option_box.currentIndexChanged.connect(
                        lambda _index: approve.setEnabled(option_box.currentData() is not None)
                    )
                buttons.accepted.connect(dialog.accept)
                buttons.rejected.connect(dialog.reject)
            pause_button = buttons.addButton(
                "暂停本机任务",
                QDialogButtonBox.ButtonRole.ActionRole,
            )
            pause_button.setObjectName("interactionLocalPauseButton")
            pause_button.setToolTip(
                "暂停当前电脑的新任务，并安全停止当前电脑已有任务。"
            )
            pause_button.clicked.connect(self._toggle_local_execution_pause)
            layout.addWidget(buttons)

            self._active_interaction_dialog = dialog
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dialog.finished.connect(
                lambda result: self._finish_interaction_dialog(
                    request,
                    option_box,
                    dialog,
                    result,
                )
            )
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        def _finish_interaction_dialog(
            self,
            request: DesktopInteractionRequest,
            option_box: QComboBox | None,
            dialog: QDialog,
            result: int,
        ) -> None:
            if self._active_interaction_dialog is dialog:
                self._active_interaction_dialog = None
            if self._active_interaction_id != request.request_id:
                return
            accepted = result == QDialog.DialogCode.Accepted
            selected = option_box.currentData() if accepted and option_box is not None else None
            self._submit_interaction_response(
                DesktopInteractionResponse(
                    request_id=request.request_id,
                    accepted=accepted,
                    selected_value=str(selected) if selected is not None else None,
                )
            )

        def _close_active_interaction_dialog(self) -> None:
            dialog = self._active_interaction_dialog
            self._active_interaction_dialog = None
            if dialog is None:
                return
            dialog.blockSignals(True)
            dialog.close()

        def _show_result(self, result: ControlResult) -> None:
            self.statusBar().showMessage(result.message, 8000)
            if bool(result.details.get("authentication_required")):
                self._request_cloudflare_reauthentication(result.message)
                return
            if not result.accepted and not bool(result.details.get("non_modal")):
                QMessageBox.warning(self, "操作未执行", result.message)
            self.refresh()

        def _request_cloudflare_reauthentication(self, reason: str) -> None:
            if (
                self._authentication_thread is not None
                and self._authentication_thread.isRunning()
            ):
                self.statusBar().showMessage(
                    "企业邮箱登录正在进行，请在浏览器完成验证。",
                    8000,
                )
                return
            reauthenticate = getattr(self._controller, "reauthenticate", None)
            if not callable(reauthenticate):
                QMessageBox.warning(
                    self,
                    "需要企业邮箱登录",
                    "当前客户端无法恢复企业邮箱登录，请安装最新版本。",
                )
                return

            if not confirm_cloudflare_access_login(reason, parent=self):
                self.statusBar().showMessage(
                    "本次操作未执行；企业邮箱登录页没有打开。",
                    8000,
                )
                return

            self.local_connection_state.setText("正在等待企业邮箱登录")
            self.statusBar().showMessage(
                "已按你的确认打开网页登录，请在浏览器完成邮箱验证。",
            )
            thread = _ControlResultThread(reauthenticate, self)
            thread.result_ready.connect(self._finish_cloudflare_reauthentication)
            thread.finished.connect(
                self._clear_cloudflare_reauthentication_thread
            )
            self._authentication_thread = thread
            thread.start()

        def _finish_cloudflare_reauthentication(
            self,
            result: ControlResult,
        ) -> None:
            self.statusBar().showMessage(result.message, 10000)
            if result.accepted:
                self.local_connection_state.setText("本地连接正常")
                QMessageBox.information(
                    self,
                    "企业邮箱登录已恢复",
                    result.message,
                )
                self.refresh()
                return
            self.local_connection_state.setText("企业邮箱登录仍未恢复")
            QMessageBox.warning(
                self,
                "企业邮箱登录未恢复",
                result.message,
            )

        def _clear_cloudflare_reauthentication_thread(self) -> None:
            thread = self._authentication_thread
            self._authentication_thread = None
            if thread is not None:
                thread.deleteLater()

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt callback name
            self._timer.stop()
            self._custom_scan_timer.stop()
            self._shipment_scan_timer.stop()
            if (
                self._client_update_thread is not None
                and self._client_update_thread.isRunning()
            ):
                self._close_pending = True
                event.ignore()
                return
            if (
                self._local_logistics_followup_thread is not None
                and self._local_logistics_followup_thread.isRunning()
            ):
                self._close_pending = True
                event.ignore()
                return
            if (
                self._authentication_thread is not None
                and self._authentication_thread.isRunning()
            ):
                QMessageBox.information(
                    self,
                    "正在等待企业邮箱登录",
                    "请先完成或关闭浏览器中的企业邮箱登录，再关闭程序。",
                )
                event.ignore()
                return
            responsive_threads = [
                thread
                for owner in (self, *self._page_widgets)
                for thread in tuple(
                    getattr(owner, "_responsive_control_threads", ())
                )
                if thread.isRunning()
            ]
            value_threads = [
                thread
                for thread in (
                    getattr(
                        self.notification_page,
                        "_notification_reload_thread",
                        None,
                    ),
                    getattr(
                        self.notification_page,
                        "_notification_prefetch_thread",
                        None,
                    ),
                    getattr(self.logs_page, "_page_load_thread", None),
                )
                if thread is not None and thread.isRunning()
            ]
            value_threads.extend(
                thread
                for owner in (self, *self._page_widgets)
                for thread in tuple(
                    getattr(owner, "_responsive_value_threads", ())
                )
                if thread.isRunning()
            )
            if responsive_threads or value_threads:
                self._close_pending = True
                event.ignore()
                return
            if self._snapshot_thread is not None:
                self._close_pending = True
                self._refresh_queued = False
                event.ignore()
                return
            prepare_close = getattr(self._controller, "prepare_close", None)
            result = prepare_close() if callable(prepare_close) else ControlResult(True, "")
            if not result.accepted:
                self._close_pending = True
                if not self._close_notice_shown:
                    self._close_notice_shown = True
                    QMessageBox.information(
                        self,
                        "正在安全关闭",
                        result.message
                        + "\n\n窗口会在任务安全结束后自动关闭，无需再次点击关闭。",
                    )
                event.ignore()
                return
            close_controller = getattr(self._controller, "close", None)
            if callable(close_controller):
                close_controller()
            event.accept()


    def run_desktop(
        controller: BackgroundTaskController,
        *,
        argv: Sequence[str] | None = None,
        execute_existing_application: bool = False,
        required_client_update_handler: Callable[[str], object] | None = None,
        runtime_restart_callback: Callable[[Path], None] | None = None,
    ) -> int:
        require_pyside6()
        application = QApplication.instance()
        owns_application = application is None
        if application is None:
            qt_argv = list(argv) if argv is not None else list(sys.argv)
            if not qt_argv:
                qt_argv = ["erp-automation"]
            application = QApplication(qt_argv)
            # Pin a Windows CJK-capable UI font.  Qt's off-screen backend and
            # some freshly installed Windows accounts do not always select a
            # Chinese fallback for Segoe UI, which otherwise renders labels as
            # empty squares even though the source text is valid UTF-8.
            application.setFont(QFont("Microsoft YaHei UI", 9))
            application.setApplicationName(
                "ERP 自动化控制台（本机测试）"
                if is_local_test_mode()
                else "ERP 自动化控制台"
            )
            application.setOrganizationName("ERP Automation")
        window = DesktopMainWindow(
            controller,
            required_client_update_handler=required_client_update_handler,
            runtime_restart_callback=runtime_restart_callback,
        )
        window.show()
        # Keep a strong reference when embedded in an already-running Qt host.
        setattr(application, "_erp_automation_window", window)
        return (
            application.exec()
            if owns_application or execute_existing_application
            else 0
        )


else:

    class _QtUnavailable:
        def __init__(self, *_args, **_kwargs) -> None:
            require_pyside6()


    DashboardPage = _QtUnavailable
    CustomOrdersPage = _QtUnavailable
    ShipmentPage = _QtUnavailable
    StateManagementPage = _QtUnavailable
    SettingsPage = _QtUnavailable
    ShipmentNotificationPage = _QtUnavailable
    LogsPage = _QtUnavailable
    DesktopMainWindow = _QtUnavailable

    def run_desktop(
        controller: BackgroundTaskController,
        *,
        argv: Sequence[str] | None = None,
        execute_existing_application: bool = False,
        required_client_update_handler: Callable[[str], object] | None = None,
        runtime_restart_callback: Callable[[Path], None] | None = None,
    ) -> int:
        del (
            controller,
            argv,
            execute_existing_application,
            required_client_update_handler,
            runtime_restart_callback,
        )
        require_pyside6()
        return 2


__all__ = [
    "DashboardPage",
    "CustomOrdersPage",
    "DesktopMainWindow",
    "LogsPage",
    "SettingsPage",
    "ShipmentNotificationPage",
    "ShipmentPage",
    "StateManagementPage",
    "run_desktop",
]
