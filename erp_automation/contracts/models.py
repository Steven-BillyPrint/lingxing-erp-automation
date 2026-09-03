from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4


SERVER_CONFIGURED_SECRET = "__ERP_SERVER_SECRET_CONFIGURED__"


def utc_now() -> datetime:
    """Return an aware timestamp so task history is unambiguous."""

    return datetime.now(timezone.utc)


class CapabilityMode(str, Enum):
    """How one Lingxing capability should be executed."""

    API_FIRST = "api_first"
    BROWSER = "browser"
    DISABLED = "disabled"

    @property
    def label(self) -> str:
        return {
            CapabilityMode.API_FIRST: "API 优先",
            CapabilityMode.BROWSER: "网页",
            CapabilityMode.DISABLED: "禁用",
        }[self]

    @classmethod
    def coerce(cls, value: CapabilityMode | str) -> CapabilityMode:
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower().replace("-", "_")
        aliases = {
            "api": cls.API_FIRST,
            "api_first": cls.API_FIRST,
            "api优先": cls.API_FIRST,
            "browser": cls.BROWSER,
            "web": cls.BROWSER,
            "网页": cls.BROWSER,
            "disabled": cls.DISABLED,
            "off": cls.DISABLED,
            "禁用": cls.DISABLED,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"未知能力模式：{value}") from exc


class Capability(str, Enum):
    LIST_ORDERS = "list_orders"
    GET_ORDER_DETAIL = "get_order_detail"
    UPDATE_CONTACT = "update_contact"
    UPDATE_REMARK = "update_remark"
    DOWNLOAD_CUSTOM_ZIP = "download_custom_zip"
    EDIT_ORDER_ITEMS = "edit_order_items"
    SPLIT_ORDER = "split_order"
    SET_LOGISTICS_CHANNEL = "set_logistics_channel"
    AUDIT_ORDER = "audit_order"
    UPDATE_TRACKING = "update_tracking"
    OUTBOUND_ORDER = "outbound_order"
    REMARK_SHIPMENT = "remark_shipment"
    ALIBABA_LOGISTICS = "alibaba_logistics"
    ALIBABA_ORDER_PREPARE = "alibaba_order_prepare"
    ALIBABA_ORDER_DRAFT = "alibaba_order_draft"
    EMAIL_PREVIEW = "email_preview"
    SEND_NOTIFICATION = "send_notification"

    @property
    def label(self) -> str:
        return {
            Capability.LIST_ORDERS: "读取订单列表",
            Capability.GET_ORDER_DETAIL: "读取订单详情",
            Capability.UPDATE_CONTACT: "写回收件人联系方式",
            Capability.UPDATE_REMARK: "更新客户备注",
            Capability.DOWNLOAD_CUSTOM_ZIP: "下载定制 ZIP",
            Capability.EDIT_ORDER_ITEMS: "调整订单商品",
            Capability.SPLIT_ORDER: "拆分订单/包裹",
            Capability.SET_LOGISTICS_CHANNEL: "设置仓库物流",
            Capability.AUDIT_ORDER: "审核订单",
            Capability.UPDATE_TRACKING: "更新物流信息",
            Capability.OUTBOUND_ORDER: "出库发货",
            Capability.REMARK_SHIPMENT: "更新物流单号并重新标发",
            Capability.ALIBABA_LOGISTICS: "查询阿里国际物流",
            Capability.ALIBABA_ORDER_PREPARE: "准备阿里物流下单",
            Capability.ALIBABA_ORDER_DRAFT: "填写阿里物流草稿",
            Capability.EMAIL_PREVIEW: "生成邮件预览",
            Capability.SEND_NOTIFICATION: "发送客户通知",
        }[self]

    @property
    def is_write(self) -> bool:
        return self in WRITE_CAPABILITIES

    @property
    def default_mode(self) -> CapabilityMode:
        if self is Capability.EMAIL_PREVIEW:
            return CapabilityMode.DISABLED
        if self in {
            Capability.UPDATE_CONTACT,
            Capability.ALIBABA_LOGISTICS,
            Capability.ALIBABA_ORDER_PREPARE,
            Capability.ALIBABA_ORDER_DRAFT,
            Capability.REMARK_SHIPMENT,
        }:
            return CapabilityMode.BROWSER
        return CapabilityMode.API_FIRST


WRITE_CAPABILITIES = frozenset(
    {
        Capability.UPDATE_CONTACT,
        Capability.UPDATE_REMARK,
        Capability.EDIT_ORDER_ITEMS,
        Capability.SPLIT_ORDER,
        Capability.SET_LOGISTICS_CHANNEL,
        Capability.AUDIT_ORDER,
        Capability.UPDATE_TRACKING,
        Capability.OUTBOUND_ORDER,
        Capability.REMARK_SHIPMENT,
        Capability.ALIBABA_ORDER_DRAFT,
        Capability.SEND_NOTIFICATION,
    }
)


@dataclass
class CapabilityPolicy:
    """Per-capability modes plus instance execution and write safety switches."""

    modes: dict[Capability, CapabilityMode] = field(default_factory=dict)
    emergency_stop_writes: bool = True
    execution_paused: bool = False
    execution_pause_reason: str = ""
    instance_execution_paused: bool = False
    instance_execution_pause_state: str = "active"
    instance_pause_target_count: int = 0
    instance_pause_stopped_count: int = 0
    instance_pause_stopping_count: int = 0
    instance_pause_review_count: int = 0

    def __post_init__(self) -> None:
        normalized: dict[Capability, CapabilityMode] = {}
        for key, value in self.modes.items():
            capability = key if isinstance(key, Capability) else Capability(str(key))
            normalized[capability] = CapabilityMode.coerce(value)
        self.modes = normalized
        if self.instance_execution_pause_state not in {"active", "pausing", "paused"}:
            self.instance_execution_pause_state = (
                "paused" if self.instance_execution_paused else "active"
            )

    def configured_mode_for(self, capability: Capability) -> CapabilityMode:
        if capability is Capability.EMAIL_PREVIEW:
            return CapabilityMode.DISABLED
        return self.modes.get(capability, capability.default_mode)

    def effective_mode_for(self, capability: Capability) -> CapabilityMode:
        if self.execution_paused:
            return CapabilityMode.DISABLED
        if self.emergency_stop_writes and capability.is_write:
            return CapabilityMode.DISABLED
        return self.configured_mode_for(capability)

    def set_mode(self, capability: Capability, mode: CapabilityMode | str) -> None:
        self.modes[capability] = (
            CapabilityMode.DISABLED
            if capability is Capability.EMAIL_PREVIEW
            else CapabilityMode.coerce(mode)
        )


class TaskArea(str, Enum):
    CUSTOMIZATION = "customization"
    SHIPMENT = "shipment"
    MAINTENANCE = "maintenance"

    @property
    def label(self) -> str:
        return {
            TaskArea.CUSTOMIZATION: "定制订单",
            TaskArea.SHIPMENT: "自动标发",
            TaskArea.MAINTENANCE: "系统维护",
        }[self]


NOTIFICATION_REVIEW_RESCAN_TRIGGER = "notification_review_rescan"
NOTIFICATION_CONTACT_REFRESH_TRIGGER = "notification_contact_refresh"
SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER = "shipment_notification_compensation"
SHIPMENT_NOTIFICATION_SEND_TRIGGER = "shipment_notification_send"
LINGXING_BROWSER_LOGIN_TRIGGER = "lingxing_browser_login"
NOTIFICATION_SYNC_INCLUDE_DEFERRED_RETRIES_KEY = (
    "_runtime_notification_include_deferred_retries"
)
DESKTOP_CONFIRMATION_PAYLOAD_KEY = "desktop_write_confirmation"
DESKTOP_INSTANCE_ID_PAYLOAD_KEY = "_desktop_instance_id"
DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY = "_desktop_browser_endpoint"
DESKTOP_OPERATOR_NAME_PAYLOAD_KEY = "_desktop_operator_name"
DESKTOP_OPERATOR_EMAIL_PAYLOAD_KEY = "_desktop_operator_email"

LOCAL_BROWSER_ACTION_ALIBABA_ORDER_PREPARE = "alibaba_order_prepare"
LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL = "alibaba_order_fill"


class DesktopWriteAction(str, Enum):
    PROCESS_CUSTOM_ORDER = "process_custom_order"
    EXECUTE_ERP_MARK = "execute_erp_mark"
    EXECUTE_SHIPMENT_REMARK = "execute_shipment_remark"
    FILL_ALIBABA_ORDER_DRAFT = "fill_alibaba_order_draft"
    SEND_SHIPMENT_NOTIFICATION = "send_shipment_notification"


def notification_confirmation_order_no(notification_ids: Iterable[object]) -> str:
    """Return one stable audit identity for a reviewed notification batch."""

    normalized: set[int] = set()
    for value in notification_ids:
        try:
            notification_id = int(value)
        except (TypeError, ValueError):
            continue
        if notification_id > 0:
            normalized.add(notification_id)
    if not normalized:
        raise ValueError("客户通知写入确认缺少有效通知编号。")
    return "shipment-notifications:" + ",".join(
        str(notification_id) for notification_id in sorted(normalized)
    )


@dataclass(frozen=True)
class DesktopWriteConfirmation:
    """Auditable proof that a visible desktop action authorized one write task."""

    confirmation_id: str
    action: DesktopWriteAction
    order_no: str
    confirmed_at: str
    system_order_no: str = ""
    logistics_no: str = ""
    source: str = "qt_message_box"

    @classmethod
    def create(
        cls,
        action: DesktopWriteAction,
        order_no: str,
        *,
        system_order_no: str = "",
        logistics_no: str = "",
        source: str = "qt_message_box",
    ) -> "DesktopWriteConfirmation":
        normalized_order_no = str(order_no or "").strip()
        if not normalized_order_no:
            raise ValueError("桌面写入确认缺少订单号。")
        return cls(
            confirmation_id=uuid4().hex,
            action=DesktopWriteAction(action),
            order_no=normalized_order_no,
            confirmed_at=utc_now().isoformat(),
            system_order_no=str(system_order_no or "").strip(),
            logistics_no=str(logistics_no or "").strip(),
            source=str(source or "").strip(),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "action": self.action.value,
            "order_no": self.order_no,
            "system_order_no": self.system_order_no,
            "logistics_no": self.logistics_no,
            "confirmed_at": self.confirmed_at,
            "source": self.source,
            "confirmed": True,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DesktopWriteConfirmation":
        raw = payload.get(DESKTOP_CONFIRMATION_PAYLOAD_KEY)
        if not isinstance(raw, Mapping) or raw.get("confirmed") is not True:
            raise ValueError("缺少桌面写入确认；请从对应订单页面重新点击执行。")
        confirmation_id = str(raw.get("confirmation_id") or "").strip()
        try:
            UUID(hex=confirmation_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("桌面写入确认编号无效。") from exc
        confirmed_at = str(raw.get("confirmed_at") or "").strip()
        try:
            parsed_at = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("桌面写入确认时间无效。") from exc
        if parsed_at.tzinfo is None:
            raise ValueError("桌面写入确认时间必须包含时区。")
        source = str(raw.get("source") or "").strip()
        if source not in {"qt_message_box", "qt_checked_action"}:
            raise ValueError("桌面写入确认来源无效。")
        return cls(
            confirmation_id=confirmation_id,
            action=DesktopWriteAction(str(raw.get("action") or "")),
            order_no=str(raw.get("order_no") or "").strip(),
            system_order_no=str(raw.get("system_order_no") or "").strip(),
            logistics_no=str(raw.get("logistics_no") or "").strip(),
            confirmed_at=confirmed_at,
            source=source,
        )

    def require_matches(
        self,
        action: DesktopWriteAction,
        order_no: str,
        *,
        system_order_no: str = "",
        logistics_no: str = "",
    ) -> None:
        if self.action is not DesktopWriteAction(action) or self.order_no != str(order_no or "").strip():
            raise ValueError("桌面写入确认与当前操作或订单不匹配。")
        expected_system = str(system_order_no or "").strip()
        expected_logistics = str(logistics_no or "").strip()
        if expected_system and self.system_order_no != expected_system:
            raise ValueError("桌面写入确认与当前系统单号不匹配。")
        if expected_logistics and self.logistics_no != expected_logistics:
            raise ValueError("桌面写入确认与当前物流单号不匹配。")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            TaskStatus.QUEUED: "等待中",
            TaskStatus.RUNNING: "运行中",
            TaskStatus.WAITING_USER: "等待用户确认",
            TaskStatus.STOPPING: "正在停止",
            TaskStatus.SUCCEEDED: "已完成",
            TaskStatus.FAILED: "失败",
            TaskStatus.BLOCKED: "需人工处理",
            TaskStatus.PAUSED: "已暂停",
            TaskStatus.CANCELLED: "已取消",
        }[self]

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.PAUSED,
            TaskStatus.CANCELLED,
        }


@dataclass(frozen=True)
class DesktopInteractionOption:
    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class DesktopInteractionRequest:
    """A desktop decision or targeted local-browser action.

    The controller logs only its identifier, stage and response.  It deliberately
    does not persist ``message``, option descriptions, ``display_data`` or
    ``action_payload`` because those can contain recipient contact information
    or short-lived credentials.
    """

    request_id: str
    task_id: str
    stage: str
    title: str
    message: str
    options: tuple[DesktopInteractionOption, ...] = ()
    display_data: Mapping[str, str] = field(default_factory=dict, repr=False)
    target_instance_id: str = ""
    non_blocking: bool = False
    automatic_action: str = ""
    action_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    approve_label: str = "确认执行"
    reject_label: str = "拒绝 / 停止"
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class DesktopInteractionResponse:
    request_id: str
    accepted: bool
    selected_value: str | None = None
    result_data: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class TaskCommand:
    name: str
    area: TaskArea
    capability: Capability
    payload: Mapping[str, Any] = field(default_factory=dict)
    order_no: str | None = None
    # Assigned by the persistent controller after the task enters the queue.
    # Keeping it on the immutable command lets the same identifier flow into
    # API scan audit files without using thread-local or process-global state.
    execution_id: str | None = None


def task_requires_visible_browser(command: TaskCommand) -> bool:
    """Return whether a task can require an operator-visible browser."""

    if command.area is TaskArea.MAINTENANCE:
        return (
            command.capability is Capability.LIST_ORDERS
            and str(command.payload.get("trigger") or "").strip()
            == LINGXING_BROWSER_LOGIN_TRIGGER
        )
    if command.area is TaskArea.CUSTOMIZATION:
        return command.capability is not Capability.LIST_ORDERS
    if command.area is TaskArea.SHIPMENT:
        # ERP marking is API-first.  Its local Chrome channel is started only
        # for an explicitly approved browser fallback.
        return command.capability in {
            Capability.ALIBABA_LOGISTICS,
            Capability.ALIBABA_ORDER_PREPARE,
            Capability.ALIBABA_ORDER_DRAFT,
            Capability.REMARK_SHIPMENT,
        }
    return False


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    name: str
    area: TaskArea
    capability: Capability
    status: TaskStatus = TaskStatus.QUEUED
    message: str = ""
    order_no: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    progress_percent: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    operator_name: str = ""
    operator_email: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.progress_percent <= 100:
            raise ValueError("任务进度必须在 0 到 100 之间。")


@dataclass(frozen=True)
class CustomOrderRow:
    platform_order_no: str
    system_order_no: str = ""
    product_type: str = ""
    workflow_stage: str = ""
    status_text: str = ""
    last_error: str = ""
    result_detail: str = ""
    retry_confirmation_required: bool = False
    status_updated_at: str = ""
    tag_text: str = ""


@dataclass(frozen=True)
class ShipmentRow:
    platform_order_no: str
    system_order_no: str = ""
    product_type: str = ""
    logistics_no: str = ""
    customer_shipping_service: str = ""
    first_seen_at: str = ""
    tracking_validated: bool | None = None
    international_tracking_no: str = ""
    carrier: str = ""
    alibaba_status: str = ""
    actual_total: str = ""
    chargeable_weight_kg: str = ""
    identity_state: str = ""
    identity_status_text: str = ""
    logistics_state: str = ""
    logistics_next_attempt_at: str = ""
    erp_state: str = ""
    erp_next_attempt_at: str = ""
    checkpoint: str = ""
    lease_owner: str = ""
    lease_stage: str = ""
    lease_until: str = ""
    last_error: str = ""
    updated_at: str = ""
    last_scanned_at: str = ""
    identity_state_changed_at: str = ""
    logistics_state_changed_at: str = ""
    logistics_last_checked_at: str = ""
    erp_state_changed_at: str = ""
    outbounded_at: str = ""
    externally_completed_at: str = ""
    completion_source: str = ""
    erp_last_error: str = ""
    logistics_last_error: str = ""
    email_state: str = ""
    email_last_error: str = ""
    wms_selection_required: bool = False
    sku_text: str = ""
    product_identity_catalog_version: str = ""
    product_identity_checked_at: str = ""
    product_identity_retry_count: int = 0
    product_identity_next_retry_at: str = ""
    product_identity_last_error: str = ""
    product_identity_evidence_json: str = ""
    product_identity_status_text: str = ""
    scan_issue_code: str = ""
    logistics_overdue_at: str = ""
    scan_issue_key: str = ""
    scan_issue_state: str = ""
    scan_issue_reason: str = ""
    scan_issue_state_changed_at: str = ""
    re_mark_cycle_id: int = 0
    re_mark_state: str = ""
    re_mark_checkpoint: str = ""
    re_mark_wo_number: str = ""
    re_mark_old_carrier: str = ""
    re_mark_old_waybill_no: str = ""
    re_mark_new_carrier: str = ""
    re_mark_new_service_line: str = ""
    re_mark_new_waybill_no: str = ""
    re_mark_new_tracking_no: str = ""
    re_mark_new_freight: str = ""
    re_mark_new_currency: str = ""
    re_mark_new_fee_weight_g: str = ""
    re_mark_last_error: str = ""
    re_mark_updated_at: str = ""


@dataclass(frozen=True)
class QueueFacets:
    """Global filter values for one server-side queue query."""

    statuses: tuple[str, ...] = ()
    product_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CustomOrderPage:
    """One deterministic page from the complete custom-order read model."""

    items: tuple[CustomOrderRow, ...] = ()
    page: int = 1
    page_size: int = 50
    total: int = 0
    dataset_revision: str = ""
    facets: QueueFacets = field(default_factory=QueueFacets)

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class ShipmentPage:
    """One deterministic page from the complete shipment read model."""

    items: tuple[ShipmentRow, ...] = ()
    page: int = 1
    page_size: int = 50
    total: int = 0
    dataset_revision: str = ""
    facets: QueueFacets = field(default_factory=QueueFacets)

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class DatasetSummary:
    """Small revision marker transported in place of an unbounded row list."""

    total: int = 0
    revision: str = ""
    latest_updated_at: str = ""


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class LogEntry:
    level: LogLevel
    source: str
    message: str
    task_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    operator_name: str = ""
    operator_email: str = ""


@dataclass(frozen=True)
class LogPage:
    items: tuple[LogEntry, ...] = ()
    page: int = 1
    page_size: int = 100
    total: int = 0

    @property
    def page_count(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class DesktopSettings:
    # Credentials live only in the DPAPI encrypted configuration document.  The
    # desktop model carries them briefly so the user can edit everything in one
    # place; ``repr=False`` prevents accidental disclosure in diagnostics.
    lingxing_app_id: str = ""
    lingxing_app_secret: str = field(default="", repr=False)
    lingxing_api_base_url: str = "https://openapi.lingxing.com"
    lingxing_account: str = ""
    lingxing_password: str = field(default="", repr=False)
    lingxing_remember_login: bool = True
    erp_mark_routes_json: str = "{}"
    erp_mark_outbound_strategy: str = "staged"
    alibaba_account: str = ""
    alibaba_password: str = field(default="", repr=False)
    alibaba_auto_login: bool = True
    alibaba_logistics_query_account: str = ""
    alibaba_logistics_query_password: str = field(default="", repr=False)
    alibaba_logistics_query_auto_login: bool = True
    amazon_lwa_client_id: str = ""
    amazon_lwa_client_secret: str = field(default="", repr=False)
    amazon_refresh_token: str = field(default="", repr=False)
    amazon_sp_api_sandbox: bool = False
    alimail_application_name: str = ""
    alimail_app_id: str = ""
    alimail_app_secret: str = field(default="", repr=False)
    alimail_amazon_sender_email: str = "acs@billyprint.com"
    alimail_independent_sender_email: str = "cs@billyprint.com"
    alimail_sender_display_name: str = "BillyPrint Customer Service"
    clicksend_username: str = field(default="", repr=False)
    clicksend_api_key: str = field(default="", repr=False)
    clicksend_sender_id: str = ""
    notification_virtual_email_domains_json: str = (
        '{"amazon": ["marketplace.amazon.*"], '
        '"10001": ["marketplace.amazon.*"]}'
    )
    folder_root: str = r"Z:\Amazon每日订单汇总"
    custom_state_path: str = "data/automation.sqlite3"
    queue_path: str = "data/shipment_queue.sqlite3"
    browser_profile: str = "browser_profile"
    log_dir: str = "logs"
    api_timeout_seconds: int = 30
    payment_window_hours: int = 96
    high_value_split_weight_kg: int = 4
    high_value_split_longest_side_cm: int = 55
    shipment_tag_name: str = "标发"
    custom_order_review_enabled: bool = False
    shipment_review_enabled: bool = False
    log_retention_days: int = 90
    browser_fallback_enabled: bool = True
    redact_sensitive_logs: bool = False

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.folder_root.strip():
            errors.append("订单文件夹根目录不能为空。")
        if not self.queue_path.strip():
            errors.append("队列数据库路径不能为空。")
        elif self.queue_path.strip().replace("\\", "/").casefold() != "data/shipment_queue.sqlite3":
            errors.append("自动标发队列固定为 data/shipment_queue.sqlite3。")
        if not self.custom_state_path.strip():
            errors.append("定制订单状态数据库路径不能为空。")
        elif self.custom_state_path.strip().replace("\\", "/").casefold() != "data/automation.sqlite3":
            errors.append("定制订单状态库固定为 data/automation.sqlite3。")
        if self.api_timeout_seconds <= 0:
            errors.append("API 超时时间必须大于 0。")
        if self.lingxing_api_base_url.strip().rstrip("/") != "https://openapi.lingxing.com":
            errors.append("领星 API 地址固定为官方 HTTPS 域名。")
        if self.payment_window_hours != 96:
            errors.append("付款时间窗口固定为 96 小时。")
        if self.high_value_split_weight_kg not in {3, 4, 5}:
            errors.append("高金额订单拆单估重阈值必须选择 3、4 或 5kg。")
        if isinstance(self.high_value_split_longest_side_cm, bool) or not (
            1 <= self.high_value_split_longest_side_cm <= 500
        ):
            errors.append("高金额订单拆单最长边阈值必须为 1～500cm。")
        if not self.shipment_tag_name.strip():
            errors.append("自动标发扫描标签不能为空。")
        if self.log_dir.strip().replace("\\", "/").strip("/").casefold() != "logs":
            errors.append("日志目录固定为应用目录下的 logs，以避免误删其他文件。")
        try:
            routes = json.loads(self.erp_mark_routes_json or "{}")
        except json.JSONDecodeError:
            errors.append("ERP 仓库/物流 ID 映射必须是有效 JSON。")
        else:
            if not isinstance(routes, dict):
                errors.append("ERP 仓库/物流 ID 映射必须是 JSON 对象。")
        if self.erp_mark_outbound_strategy not in {"staged", "fast_outbound"}:
            errors.append("ERP 出库策略无效。")
        for label, address in (
            ("Amazon 发件邮箱", self.alimail_amazon_sender_email),
            ("独立站发件邮箱", self.alimail_independent_sender_email),
        ):
            if "@" not in address or address.startswith("@") or address.endswith("@"):
                errors.append(f"{label}格式无效。")
        if not self.alimail_sender_display_name.strip():
            errors.append("发件人显示名称不能为空。")
        try:
            virtual_domains = json.loads(self.notification_virtual_email_domains_json or "{}")
        except json.JSONDecodeError:
            errors.append("平台虚拟邮箱域名映射必须是有效 JSON。")
        else:
            if not isinstance(virtual_domains, dict):
                errors.append("平台虚拟邮箱域名映射必须是 JSON 对象。")
        if self.log_retention_days != 90:
            errors.append("当前版本日志保留期限固定为 90 天。")
        if self.redact_sensitive_logs:
            errors.append("业务日志固定保留原始诊断内容，不能开启脱敏。")
        return tuple(errors)


SENSITIVE_SETTINGS_FIELDS = frozenset(
    item.name for item in fields(DesktopSettings) if not item.repr
)


@dataclass(frozen=True)
class MigrationInfo:
    current_schema_version: int = 0
    target_schema_version: int = 0
    pending_migrations: tuple[str, ...] = ()
    last_result: str = "尚未检查迁移。"

    @property
    def migration_required(self) -> bool:
        return bool(self.pending_migrations) or self.current_schema_version < self.target_schema_version


@dataclass(frozen=True)
class DashboardMetrics:
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    attention: int = 0
    cancelled: int = 0

    @classmethod
    def from_tasks(cls, tasks: list[TaskRecord] | tuple[TaskRecord, ...]) -> DashboardMetrics:
        statuses = [task.status for task in tasks]
        return cls(
            queued=statuses.count(TaskStatus.QUEUED),
            running=(
                statuses.count(TaskStatus.RUNNING)
                + statuses.count(TaskStatus.WAITING_USER)
                + statuses.count(TaskStatus.STOPPING)
            ),
            succeeded=statuses.count(TaskStatus.SUCCEEDED),
            attention=statuses.count(TaskStatus.FAILED) + statuses.count(TaskStatus.BLOCKED),
            cancelled=(
                statuses.count(TaskStatus.CANCELLED)
                + statuses.count(TaskStatus.PAUSED)
            ),
        )


@dataclass
class DesktopSnapshot:
    policy: CapabilityPolicy = field(default_factory=CapabilityPolicy)
    tasks: list[TaskRecord] = field(default_factory=list)
    today_tasks: list[TaskRecord] = field(default_factory=list)
    custom_orders: list[CustomOrderRow] = field(default_factory=list)
    shipments: list[ShipmentRow] = field(default_factory=list)
    custom_orders_summary: DatasetSummary = field(default_factory=DatasetSummary)
    shipments_summary: DatasetSummary = field(default_factory=DatasetSummary)
    notifications_summary: DatasetSummary = field(default_factory=DatasetSummary)
    logs_summary: DatasetSummary = field(default_factory=DatasetSummary)
    server_features: tuple[str, ...] = ()
    settings: DesktopSettings = field(default_factory=DesktopSettings)
    configured_secret_lengths: dict[str, int] = field(
        default_factory=dict,
        repr=False,
    )
    configuration_fingerprint: str = ""
    configuration_key_count: int = 0
    configured_non_sensitive_field_count: int = 0
    configured_secret_field_count: int = 0
    configuration_is_default: bool = True
    migration: MigrationInfo = field(default_factory=MigrationInfo)
    logs: list[LogEntry] = field(default_factory=list)
    operator_name: str = ""
    operator_email: str = ""
    scheduler_leader_instance_id: str = ""
    is_scheduler_leader: bool = True
    scheduled_scan_due_at: dict[str, float] = field(default_factory=dict)
    backend_message: str = "桌面骨架尚未连接实际后台 Worker。"

    @property
    def dashboard(self) -> DashboardMetrics:
        return DashboardMetrics.from_tasks(self.today_tasks)
