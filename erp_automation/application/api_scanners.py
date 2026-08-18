"""API-first order scanners used by the desktop application.

This module is intentionally conservative.  It accepts caller-provided
Lingxing filters verbatim, proves that pagination reached a stable end before
declaring a snapshot complete, and never uses an undocumented numeric status
code.  The existing business-rule functions remain the single source of truth
for customization and shipment candidate selection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
import uuid
from copy import deepcopy
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from lingxing_automation.constants import DEFAULT_PAYMENT_WINDOW_HOURS
from lingxing_automation.models import BatchOrderItem
from lingxing_automation.pages.order_list import build_batch_candidates_from_rows
from lingxing_automation.products.catalog import (
    identify_product_types,
    preferred_product_type,
)
from shipment_automation.candidate_scanner import (
    apply_queue_results,
    build_shipment_scan_report,
    row_has_shipment_tag,
)
from shipment_automation.models import ShipmentScanReport
from shipment_automation.queue_store import (
    QueueInsertResult,
    TagSnapshotReconcileResult,
    utc_now,
)

from .lingxing_gateway import OrderPage, OrderRecord
from .order_status import BUYER_CANCEL_REQUEST_TEXT, has_buyer_cancel_request


DEFAULT_API_PAGE_SIZE = 500
DEFAULT_MAX_API_PAGES = 200
DEFAULT_CUSTOMIZATION_SNAPSHOT_RETRY_DELAYS = (0.0, 1.0, 2.0)
RETRYABLE_SNAPSHOT_DIAGNOSTIC_CODES = frozenset(
    {
        "api_page_failed",
        "overlapping_pages",
        "pagination_exceeds_total",
        "pagination_stopped_early",
        "pagination_total_changed",
        "repeated_page",
    }
)
CUSTOMIZATION_REQUIRED_FIELDS = ("system", "platform", "paid_at", "tag")
SHIPMENT_REQUIRED_FIELDS = (
    "system",
    "shipment_platform",
    "tag",
    "customer_remark",
    "customer_shipping_service",
)


class OrderListGateway(Protocol):
    async def list_orders(
        self,
        *,
        offset: int = 0,
        length: int = DEFAULT_API_PAGE_SIZE,
        filters: Mapping[str, Any] | None = None,
    ) -> OrderPage: ...

    async def get_order_detail(self, order_number: str) -> Any: ...


class ProcessedOrderSource(Protocol):
    def processed_platform_orders(self) -> set[str]: ...


class ShipmentQueueSink(Protocol):
    def upsert_candidate(
        self,
        candidate: Any,
        *,
        run_id: str | None = None,
        allow_tag_restore: bool = False,
    ) -> QueueInsertResult: ...

    def complete_missing_pending_orders(
        self,
        visible_system_order_nos: set[str],
        *,
        discovered_before: str,
        run_id: str | None = None,
    ) -> list[Any]: ...

    def reconcile_shipment_tag_snapshot(
        self,
        tag_states: Mapping[str, bool | None],
        *,
        snapshot_complete: bool,
        run_id: str | None = None,
    ) -> TagSnapshotReconcileResult: ...


class ApiScanState(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True)
class ApiScanDiagnostic:
    """A deliberately payload-free diagnostic safe for logs and UI display."""

    code: str
    message: str
    page_number: int | None = None
    offset: int | None = None
    request_id: str | None = None
    affected_count: int = 0
    missing_fields: tuple[str, ...] = ()
    error_type: str | None = None


@dataclass(frozen=True)
class ApiPageTrace:
    page_number: int
    offset: int
    item_count: int
    request_id: str | None = None
    window_number: int | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class OrderPaginationResult:
    state: ApiScanState
    orders: tuple[OrderRecord, ...] = field(default_factory=tuple, repr=False)
    source_pages: tuple[int, ...] = field(default_factory=tuple, repr=False)
    page_traces: tuple[ApiPageTrace, ...] = ()
    expected_total: int | None = None
    diagnostics: tuple[ApiScanDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state is ApiScanState.COMPLETE

    @property
    def pages_read(self) -> int:
        return len(self.page_traces)

    @property
    def request_ids(self) -> tuple[str, ...]:
        return tuple(trace.request_id for trace in self.page_traces if trace.request_id)


@dataclass(frozen=True)
class MissingFieldNotice:
    """Identifies an incomplete row without including order payload or PII."""

    order_index: int
    source_page: int
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedOrderRows:
    customization_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)
    shipment_rows: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)
    order_field_presence: tuple[Mapping[str, bool], ...] = field(default_factory=tuple, repr=False)
    source_pages: tuple[int, ...] = field(default_factory=tuple, repr=False)

    def missing_fields(self, required_fields: Sequence[str]) -> tuple[MissingFieldNotice, ...]:
        required = tuple(dict.fromkeys(str(value) for value in required_fields))
        notices: list[MissingFieldNotice] = []
        for index, presence in enumerate(self.order_field_presence):
            missing = tuple(field_name for field_name in required if not presence.get(field_name, False))
            if missing:
                source_page = self.source_pages[index] if index < len(self.source_pages) else 0
                notices.append(MissingFieldNotice(index, source_page, missing))
        return tuple(notices)


@dataclass(frozen=True)
class CustomizationApiScanResult:
    state: ApiScanState
    pagination: OrderPaginationResult
    api_raw_order_count: int
    row_count: int
    candidate_count: int
    processed_order_count: int
    payment_window_hours: float
    candidates: tuple[BatchOrderItem, ...] = field(default_factory=tuple, repr=False)
    reactivation_candidates: tuple[BatchOrderItem, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    observed_workflows: tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple,
        repr=False,
    )
    product_identity_observations: tuple["ProductIdentityObservation", ...] = field(
        default_factory=tuple,
        repr=False,
    )
    detail_request_ids: tuple[str, ...] = ()
    skip_counts: Mapping[str, int] = field(default_factory=dict)
    diagnostics: tuple[ApiScanDiagnostic, ...] = ()
    audit_decisions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)

    @property
    def complete(self) -> bool:
        return self.state is ApiScanState.COMPLETE

    @property
    def product_identity_pending_count(self) -> int:
        return len(self.product_identity_observations)


@dataclass(frozen=True)
class ProductIdentityObservation:
    """One customization order retained while Lingxing product data settles."""

    platform_order_no: str
    system_order_no: str
    paid_at_text: str = ""
    sku: str = ""
    tag_text: str = ""
    state: str = "product_identity_pending"
    status_text: str = "等待 ASIN 同步"
    last_error: str = ""
    detail_attempted: bool = False
    observed_asins: tuple[str, ...] = ()
    product_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductTypeBackfillObservation:
    """Exact-detail product identity result for a stored shipment order."""

    platform_order_no: str
    system_order_no: str
    product_types: tuple[str, ...] = ()
    observed_asins: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ShipmentApiScanResult:
    state: ApiScanState
    pagination: OrderPaginationResult
    window_count: int
    api_raw_order_count: int
    row_count: int
    evaluable_row_count: int
    tagged_row_count: int
    candidate_count: int
    enqueued_count: int
    manual_completed_count: int
    missing_critical_field_count: int
    paused_count: int
    resumed_count: int
    immediate_logistics_count: int
    immediate_erp_count: int
    report: ShipmentScanReport = field(repr=False)
    diagnostics: tuple[ApiScanDiagnostic, ...] = ()
    audit_decisions: tuple[Mapping[str, Any], ...] = field(default_factory=tuple, repr=False)

    @property
    def complete(self) -> bool:
        return self.state is ApiScanState.COMPLETE

    @property
    def eligible_row_count(self) -> int:
        """Compatibility alias for callers migrating to ``evaluable_row_count``."""

        return self.evaluable_row_count

    @property
    def manual_review_count(self) -> int:
        return sum(
            1
            for decision in self.audit_decisions
            if decision.get("decision") == "manual_review"
        )


@dataclass(frozen=True)
class _OrderTagViews:
    """Workflow-specific views over Lingxing's heterogeneous order tags.

    The documented order response mixes user-defined order tags with system
    processing/status hints in ``order_tag`` and its sibling fields.  The old
    browser scanner read the dedicated visible "标签" column, so flattening
    every API tag name into one string changes the business meaning.
    """

    custom_field_present: bool
    system_field_present: bool
    customization_text: str
    shipment_text: str


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "refreshtoken",
    "refresh_token",
    "authorization",
    "credential",
    "password",
    "appsecret",
    "app_secret",
    "secret",
    "cookie",
    "email",
    "phone",
    "mobile",
    "telephone",
    "receiver_tel",
    "address",
    "recipient",
    "receiver_name",
    "buyer_name",
    "contact",
    "customer_remark",
    "customerserviceremark",
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PHONE_LIKE_RE = re.compile(r"(?<!\d)\+?\d(?:[\s().-]*\d){6,20}(?!\d)")
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|authorization|cookie|phone|mobile|telephone|email)"
    r"\s*[:=]\s*([^\s,;]+)"
)


def redact_sensitive_text(value: object) -> str:
    """Redact common credentials and contact values from arbitrary text."""

    text = str(value)
    text = _EMAIL_RE.sub("<redacted-email>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _LABELED_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return _PHONE_LIKE_RE.sub("<redacted-phone>", text)


def redact_sensitive_payload(value: Any) -> Any:
    """Return a diagnostics-safe copy; never use this copy for business logic."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            canonical = _canonical_key(key_text)
            if any(_canonical_key(part) in canonical for part in _SENSITIVE_KEY_PARTS):
                output[key_text] = "<redacted>"
            else:
                output[key_text] = redact_sensitive_payload(item)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_sensitive_payload(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


async def fetch_all_order_pages(
    gateway: OrderListGateway,
    *,
    filters: Mapping[str, Any] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
) -> OrderPaginationResult:
    """Read a complete, stable order snapshot without trusting pagination blindly."""

    if page_size <= 0:
        raise ValueError("page_size 必须大于 0。")
    if max_pages <= 0:
        raise ValueError("max_pages 必须大于 0。")

    requested_filters = dict(filters or {})
    orders: list[OrderRecord] = []
    source_pages: list[int] = []
    traces: list[ApiPageTrace] = []
    diagnostics: list[ApiScanDiagnostic] = []
    page_fingerprints: set[str] = set()
    stable_identities: set[str] = set()
    expected_total: int | None = None
    offset = 0

    for page_number in range(1, max_pages + 1):
        try:
            page = await gateway.list_orders(
                offset=offset,
                length=page_size,
                filters=dict(requested_filters),
            )
        except Exception as exc:
            request_id = _optional_text(getattr(exc, "request_id", None))
            diagnostics.append(
                ApiScanDiagnostic(
                    code="api_page_failed",
                    message="读取领星订单分页失败，本轮扫描不完整。",
                    page_number=page_number,
                    offset=offset,
                    request_id=request_id,
                    error_type=type(exc).__name__,
                )
            )
            if request_id:
                traces.append(ApiPageTrace(page_number, offset, 0, request_id))
            return OrderPaginationResult(
                state=ApiScanState.FAILED if not orders else ApiScanState.INCOMPLETE,
                orders=tuple(orders),
                source_pages=tuple(source_pages),
                page_traces=tuple(traces),
                expected_total=expected_total,
                diagnostics=tuple(diagnostics),
            )

        items = tuple(page.items)
        traces.append(ApiPageTrace(page_number, offset, len(items), page.request_id))
        if len(items) > page_size:
            diagnostics.append(
                ApiScanDiagnostic(
                    code="page_larger_than_requested",
                    message="API 返回数量超过请求分页大小，无法证明扫描完整。",
                    page_number=page_number,
                    offset=offset,
                    request_id=page.request_id,
                    affected_count=len(items),
                )
            )
            return _pagination_result(
                ApiScanState.INCOMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )

        if page.total is not None:
            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                diagnostics.append(
                    ApiScanDiagnostic(
                        code="pagination_total_changed",
                        message="API 分页期间订单总数发生变化，本轮不作完整快照。",
                        page_number=page_number,
                        offset=offset,
                        request_id=page.request_id,
                    )
                )
                return _pagination_result(
                    ApiScanState.INCOMPLETE,
                    orders,
                    source_pages,
                    traces,
                    expected_total,
                    diagnostics,
                )

        fingerprint = _page_fingerprint(items)
        if fingerprint in page_fingerprints:
            diagnostics.append(
                ApiScanDiagnostic(
                    code="repeated_page",
                    message="API 重复返回已读取分页，已停止以避免无限循环。",
                    page_number=page_number,
                    offset=offset,
                    request_id=page.request_id,
                    affected_count=len(items),
                )
            )
            return _pagination_result(
                ApiScanState.INCOMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )
        page_fingerprints.add(fingerprint)

        current_identities = [identity for item in items if (identity := _stable_order_identity(item))]
        if len(current_identities) != len(set(current_identities)) or any(
            identity in stable_identities for identity in current_identities
        ):
            diagnostics.append(
                ApiScanDiagnostic(
                    code="overlapping_pages",
                    message="API 分页之间出现重复订单，无法证明快照完整。",
                    page_number=page_number,
                    offset=offset,
                    request_id=page.request_id,
                    affected_count=len(current_identities),
                )
            )
            return _pagination_result(
                ApiScanState.INCOMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )
        stable_identities.update(current_identities)
        orders.extend(items)
        source_pages.extend([page_number] * len(items))

        if expected_total is not None and len(orders) > expected_total:
            diagnostics.append(
                ApiScanDiagnostic(
                    code="pagination_exceeds_total",
                    message="API 累计返回数量超过声明总数，无法证明快照完整。",
                    page_number=page_number,
                    offset=offset,
                    request_id=page.request_id,
                )
            )
            return _pagination_result(
                ApiScanState.INCOMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )

        if expected_total is not None:
            if len(orders) == expected_total:
                return _pagination_result(
                    ApiScanState.COMPLETE,
                    orders,
                    source_pages,
                    traces,
                    expected_total,
                    diagnostics,
                )
            if not items:
                diagnostics.append(
                    ApiScanDiagnostic(
                        code="pagination_stopped_early",
                        message="API 在达到声明总数前返回空页，本轮扫描不完整。",
                        page_number=page_number,
                        offset=offset,
                        request_id=page.request_id,
                    )
                )
                return _pagination_result(
                    ApiScanState.INCOMPLETE,
                    orders,
                    source_pages,
                    traces,
                    expected_total,
                    diagnostics,
                )
        elif len(items) < page_size:
            return _pagination_result(
                ApiScanState.COMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )

        next_offset = offset + len(items)
        if next_offset <= offset:
            diagnostics.append(
                ApiScanDiagnostic(
                    code="pagination_no_progress",
                    message="API 分页游标无法继续前进，已停止以避免无限循环。",
                    page_number=page_number,
                    offset=offset,
                    request_id=page.request_id,
                )
            )
            return _pagination_result(
                ApiScanState.INCOMPLETE,
                orders,
                source_pages,
                traces,
                expected_total,
                diagnostics,
            )
        offset = next_offset

    diagnostics.append(
        ApiScanDiagnostic(
            code="maximum_pages_reached",
            message="API 扫描达到安全分页上限，本轮扫描不完整。",
            page_number=max_pages,
            offset=offset,
        )
    )
    return _pagination_result(
        ApiScanState.INCOMPLETE,
        orders,
        source_pages,
        traces,
        expected_total,
        diagnostics,
    )


async def fetch_stable_order_snapshot(
    gateway: OrderListGateway,
    *,
    filters: Mapping[str, Any] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
    retry_delays_seconds: Sequence[float] = DEFAULT_CUSTOMIZATION_SNAPSHOT_RETRY_DELAYS,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> OrderPaginationResult:
    """Read one complete snapshot, restarting from offset zero after transient drift.

    Every attempt is independent: orders collected from an incomplete attempt are
    discarded and can never be mixed into a later snapshot.  Page traces from all
    attempts are retained for the audit log.
    """

    delays = tuple(float(value) for value in retry_delays_seconds)
    if not delays:
        raise ValueError("retry_delays_seconds 必须至少包含一次读取。")
    if any(value < 0 for value in delays):
        raise ValueError("retry_delays_seconds 不能包含负数。")

    traces: list[ApiPageTrace] = []
    last_result: OrderPaginationResult | None = None
    attempt_count = 0
    for retry_count, delay_seconds in enumerate(delays):
        attempt_count += 1
        if delay_seconds:
            await sleeper(delay_seconds)
        result = await fetch_all_order_pages(
            gateway,
            filters=filters,
            page_size=page_size,
            max_pages=max_pages,
        )
        traces.extend(
            ApiPageTrace(
                page_number=trace.page_number,
                offset=trace.offset,
                item_count=trace.item_count,
                request_id=trace.request_id,
                window_number=trace.window_number,
                retry_count=retry_count,
            )
            for trace in result.page_traces
        )
        last_result = result

        if result.complete:
            diagnostics = list(result.diagnostics)
            if retry_count:
                diagnostics.append(
                    ApiScanDiagnostic(
                        code="snapshot_retry_recovered",
                        message=(
                            f"订单快照在第 {retry_count + 1} 次整轮读取时恢复完整；"
                            "此前不完整结果已全部丢弃。"
                        ),
                        affected_count=retry_count,
                    )
                )
            return OrderPaginationResult(
                state=result.state,
                orders=result.orders,
                source_pages=result.source_pages,
                page_traces=tuple(traces),
                expected_total=result.expected_total,
                diagnostics=tuple(diagnostics),
            )

        terminal_code = result.diagnostics[-1].code if result.diagnostics else ""
        has_next_attempt = retry_count + 1 < len(delays)
        if not has_next_attempt or terminal_code not in RETRYABLE_SNAPSHOT_DIAGNOSTIC_CODES:
            break

    assert last_result is not None
    diagnostics = list(last_result.diagnostics)
    terminal_code = diagnostics[-1].code if diagnostics else ""
    if (
        len(delays) > 1
        and terminal_code in RETRYABLE_SNAPSHOT_DIAGNOSTIC_CODES
        and attempt_count == len(delays)
    ):
        diagnostics.append(
            ApiScanDiagnostic(
                code="snapshot_retry_exhausted",
                message=(
                    f"订单快照连续 {len(delays)} 次整轮读取仍不完整；"
                    "本轮继续禁止候选写入和对账。"
                ),
                affected_count=len(delays),
            )
        )
    return OrderPaginationResult(
        state=last_result.state,
        orders=last_result.orders,
        source_pages=last_result.source_pages,
        page_traces=tuple(traces),
        expected_total=last_result.expected_total,
        diagnostics=tuple(diagnostics),
    )


async def _fetch_shipment_order_windows(
    gateway: OrderListGateway,
    *,
    filters: Mapping[str, Any] | None,
    filter_windows: Sequence[Mapping[str, Any]] | None,
    page_size: int,
    max_pages: int,
) -> tuple[OrderPaginationResult, int]:
    if filter_windows is None:
        pagination = await fetch_all_order_pages(
            gateway,
            filters=filters,
            page_size=page_size,
            max_pages=max_pages,
        )
        return pagination, 1

    windows = tuple(dict(window) for window in filter_windows)
    if not windows:
        return (
            OrderPaginationResult(
                state=ApiScanState.INCOMPLETE,
                diagnostics=(
                    ApiScanDiagnostic(
                        code="shipment_filter_windows_empty",
                        message="自动标发没有可读取的查询窗口，本轮已禁止队列写入。",
                    ),
                ),
            ),
            0,
        )

    window_results = [
        await fetch_all_order_pages(
            gateway,
            filters=window,
            page_size=page_size,
            max_pages=max_pages,
        )
        for window in windows
    ]
    if len(window_results) == 1:
        return window_results[0], 1

    orders: list[OrderRecord] = []
    source_pages: list[int] = []
    page_traces: list[ApiPageTrace] = []
    diagnostics: list[ApiScanDiagnostic] = []
    seen: dict[str, str] = {}
    conflicting_identities: set[str] = set()

    for window_number, result in enumerate(window_results, start=1):
        page_traces.extend(
            ApiPageTrace(
                page_number=trace.page_number,
                offset=trace.offset,
                item_count=trace.item_count,
                request_id=trace.request_id,
                window_number=window_number,
                retry_count=trace.retry_count,
            )
            for trace in result.page_traces
        )
        diagnostics.extend(result.diagnostics)
        for index, record in enumerate(result.orders):
            identity = _cross_window_order_identity(record)
            source_page = result.source_pages[index] if index < len(result.source_pages) else 0
            if not identity:
                orders.append(record)
                source_pages.append(source_page)
                continue
            signature = _shipment_business_signature(record)
            previous_signature = seen.get(identity)
            if previous_signature is None:
                seen[identity] = signature
                orders.append(record)
                source_pages.append(source_page)
                continue
            if previous_signature != signature:
                conflicting_identities.add(identity)

    if conflicting_identities:
        diagnostics.append(
            ApiScanDiagnostic(
                code="shipment_window_snapshot_unstable",
                message="重叠查询窗口中的同一订单业务内容不一致，本轮已禁止队列写入。",
                affected_count=len(conflicting_identities),
            )
        )

    if any(result.state is ApiScanState.FAILED for result in window_results):
        state = ApiScanState.FAILED
    elif conflicting_identities or any(not result.complete for result in window_results):
        state = ApiScanState.INCOMPLETE
    else:
        state = ApiScanState.COMPLETE
    return (
        OrderPaginationResult(
            state=state,
            orders=tuple(orders),
            source_pages=tuple(source_pages),
            page_traces=tuple(page_traces),
            expected_total=len(orders),
            diagnostics=tuple(diagnostics),
        ),
        len(windows),
    )


def _cross_window_order_identity(record: OrderRecord) -> str | None:
    global_order_no = _optional_text(record.global_order_no)
    if not global_order_no:
        mappings = _mapping_tree(dict(record.payload))
        _, value = _lookup(mappings, _SYSTEM_ALIASES)
        global_order_no = _optional_text(value)
    if global_order_no:
        return f"global:{global_order_no}"
    order_number = _optional_text(record.order_number)
    if not order_number:
        mappings = _mapping_tree(dict(record.payload))
        _, value = _lookup(mappings, _PLATFORM_ALIASES)
        order_number = _optional_text(value)
    return f"platform:{order_number}" if order_number else None


def _shipment_business_signature(record: OrderRecord) -> str:
    _, row, presence = _normalize_order(record, source_page=0, source_order_index=0)
    signature = {
        "system_order_no": str(row.get("system_order_no") or "").strip(),
        "platform_order_no": str(row.get("platform_order_no") or "").strip(),
        "tag_text": str(row.get("tag_text") or "").strip(),
        "customer_remark": str(row.get("customer_remark") or "").strip(),
        "customer_shipping_service": str(
            row.get("customer_shipping_service") or ""
        ).strip(),
        "status_text": str(row.get("status_text") or "").strip(),
        "items": row.get("audit_items") or [],
        "presence": {
            field_name: bool(presence.get(field_name))
            for field_name in SHIPMENT_REQUIRED_FIELDS
        },
    }
    encoded = json.dumps(signature, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_api_order_rows(pagination: OrderPaginationResult) -> NormalizedOrderRows:
    """Normalize snake/camel aliases and nested item lists into legacy row shapes."""

    customization_rows: list[dict[str, Any]] = []
    shipment_rows: list[dict[str, Any]] = []
    presence_rows: list[Mapping[str, bool]] = []
    source_pages: list[int] = []

    for index, record in enumerate(pagination.orders):
        source_page = pagination.source_pages[index] if index < len(pagination.source_pages) else 0
        custom_rows, shipment_row, presence = _normalize_order(
            record,
            source_page=source_page,
            source_order_index=index,
        )
        customization_rows.extend(custom_rows)
        shipment_rows.append(shipment_row)
        presence_rows.append(presence)
        source_pages.append(source_page)

    return NormalizedOrderRows(
        customization_rows=tuple(customization_rows),
        shipment_rows=tuple(shipment_rows),
        order_field_presence=tuple(presence_rows),
        source_pages=tuple(source_pages),
    )


@dataclass(frozen=True)
class _ProductIdentityTarget:
    platform_order_no: str
    system_order_no: str
    paid_at_text: str
    sku: str
    tag_text: str
    source_order_index: int | None
    prior_state: str = ""
    backfill_only: bool = False


def _customization_rows_by_platform(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        platform_order_no = str(raw.get("platform_order_no") or "").strip()
        if platform_order_no:
            grouped.setdefault(platform_order_no, []).append(dict(raw))
    return grouped


def _identity_target_from_rows(
    platform_order_no: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    prior_state: str = "",
    backfill_only: bool = False,
) -> _ProductIdentityTarget | None:
    system_order_nos = {
        str(row.get("system_order_no") or "").strip()
        for row in rows
        if str(row.get("system_order_no") or "").strip()
    }
    if len(system_order_nos) != 1:
        return None
    skus = list(
        dict.fromkeys(
            str(row.get("sku") or "").split(" 共", 1)[0].strip()
            for row in rows
            if str(row.get("sku") or "").strip()
        )
    )
    source_indexes = {
        int(row.get("_source_order_index") or 0)
        for row in rows
        if row.get("_source_order_index") is not None
    }
    return _ProductIdentityTarget(
        platform_order_no=platform_order_no,
        system_order_no=next(iter(system_order_nos)),
        paid_at_text=max(
            (str(row.get("paid_at_text") or "").strip() for row in rows),
            default="",
        ),
        sku=" | ".join(skus),
        tag_text=" | ".join(
            dict.fromkeys(
                str(row.get("tag_text") or "").strip()
                for row in rows
                if str(row.get("tag_text") or "").strip()
            )
        ),
        source_order_index=(
            next(iter(source_indexes)) if len(source_indexes) == 1 else None
        ),
        prior_state=str(prior_state or "").strip(),
        backfill_only=backfill_only,
    )


def _identity_target_from_pending(
    raw: Mapping[str, Any],
) -> _ProductIdentityTarget | None:
    platform_order_no = str(raw.get("platform_order_no") or "").strip()
    system_order_no = str(
        raw.get("system_order_no") or raw.get("original_system_order_no") or ""
    ).strip()
    sku = str(raw.get("sku") or raw.get("product_identity_sku") or "").strip()
    if not (platform_order_no and system_order_no):
        return None
    return _ProductIdentityTarget(
        platform_order_no=platform_order_no,
        system_order_no=system_order_no,
        paid_at_text=str(
            raw.get("paid_at_text") or raw.get("product_identity_paid_at") or ""
        ).strip(),
        sku=sku,
        tag_text=str(
            raw.get("tag_text") or raw.get("product_identity_tag_text") or ""
        ).strip(),
        source_order_index=None,
        prior_state=str(raw.get("product_identity_state") or "").strip(),
        backfill_only=bool(raw.get("product_identity_backfill")),
    )


def _detail_identity_error(
    payload: Mapping[str, Any],
    target: _ProductIdentityTarget,
) -> str:
    system_order_nos: set[str] = set()
    platform_order_nos: set[str] = set()

    def visit(value: object, *, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                canonical = _canonical_key(key)
                text = str(child or "").strip() if not isinstance(child, (Mapping, list, tuple)) else ""
                if text and canonical in {
                    "globalorderno",
                    "systemorderno",
                }:
                    system_order_nos.add(text)
                elif text and canonical == "ordernumber" and re.fullmatch(r"\d{15,24}", text):
                    system_order_nos.add(text)
                elif text and canonical in {
                    "platformorderno",
                    "platformorderid",
                    "amazonorderid",
                }:
                    platform_order_nos.add(text)
                visit(child, depth=depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth=depth + 1)

    visit(payload)
    if system_order_nos and target.system_order_no not in system_order_nos:
        return "订单详情返回的系统单号与扫描目标不一致。"
    if platform_order_nos and target.platform_order_no not in platform_order_nos:
        return "订单详情返回的平台单号与扫描目标不一致。"
    return ""


def _detail_rows_for_identity_target(
    payload: Mapping[str, Any],
    target: _ProductIdentityTarget,
    *,
    source_order_index: int,
) -> tuple[list[dict[str, Any]], str]:
    identity_error = _detail_identity_error(payload, target)
    if identity_error:
        return [], identity_error
    detail_rows, _shipment, _presence = _normalize_order(
        OrderRecord(target.system_order_no, target.platform_order_no, payload),
        source_page=0,
        source_order_index=source_order_index,
    )
    matched = [
        row
        for row in detail_rows
        if str(row.get("platform_order_no") or "").strip()
        == target.platform_order_no
    ]
    if not matched:
        return [], "订单详情没有返回目标平台订单的商品行。"
    output: list[dict[str, Any]] = []
    for raw in matched:
        row = dict(raw)
        row.update(
            {
                "system_order_no": target.system_order_no,
                "platform_order_no": target.platform_order_no,
                "paid_at_text": target.paid_at_text,
                "tag_text": target.tag_text,
                "_source_order_index": source_order_index,
            }
        )
        row["row_text"] = _safe_business_row_text(
            target.system_order_no,
            target.platform_order_no,
            target.paid_at_text,
            row.get("asin_text"),
            row.get("sku"),
            row.get("logistics"),
            row.get("status_text"),
            target.tag_text,
        )
        output.append(row)
    return output, ""


async def _read_product_identity_details(
    gateway: OrderListGateway,
    targets: Sequence[_ProductIdentityTarget],
) -> tuple[
    dict[str, tuple[Mapping[str, Any] | None, str, str]],
    tuple[str, ...],
]:
    """Read each Lingxing system order once with bounded concurrency."""

    lookup = getattr(gateway, "get_order_detail", None)
    system_order_nos = tuple(
        dict.fromkeys(target.system_order_no for target in targets if target.system_order_no)
    )
    if not system_order_nos:
        return {}, ()
    if not callable(lookup):
        return {
            order_no: (None, "详情查询能力不可用。", "")
            for order_no in system_order_nos
        }, ()

    semaphore = asyncio.Semaphore(4)

    async def one(order_no: str) -> tuple[str, Mapping[str, Any] | None, str, str]:
        async with semaphore:
            try:
                detail = await lookup(order_no)
            except Exception as exc:
                return order_no, None, f"详情查询失败（{type(exc).__name__}）。", ""
        payload = getattr(detail, "payload", None)
        if not isinstance(payload, Mapping):
            return order_no, None, "订单详情响应缺少可解析商品数据。", ""
        return (
            order_no,
            payload,
            "",
            str(getattr(detail, "request_id", None) or "").strip(),
        )

    results = await asyncio.gather(*(one(order_no) for order_no in system_order_nos))
    by_order = {
        order_no: (payload, error, request_id)
        for order_no, payload, error, request_id in results
    }
    request_ids = tuple(
        dict.fromkeys(request_id for *_rest, request_id in results if request_id)
    )
    return by_order, request_ids


async def _read_platform_sibling_system_orders(
    gateway: OrderListGateway,
    platform_order_nos: Sequence[str],
) -> tuple[
    dict[str, tuple[tuple[str, ...], str]],
    tuple[str, ...],
]:
    """Discover every system order for each platform identity.

    The list API is used only to discover sibling system-order identities.
    Product identity is never inferred from its folded item columns; callers
    must subsequently read the complete detail for every returned system order.
    """

    platforms = tuple(
        dict.fromkeys(str(value or "").strip() for value in platform_order_nos)
    )
    platforms = tuple(value for value in platforms if value)
    if not platforms:
        return {}, ()

    semaphore = asyncio.Semaphore(4)

    async def one(platform_order_no: str) -> tuple[
        str,
        tuple[str, ...],
        str,
        tuple[str, ...],
    ]:
        async with semaphore:
            pagination = await fetch_all_order_pages(
                gateway,
                filters={"platform_order_nos": [platform_order_no]},
                page_size=100,
                max_pages=20,
            )
        if not pagination.complete:
            return (
                platform_order_no,
                (),
                "同平台订单的兄弟系统单列表读取不完整。",
                pagination.request_ids,
            )

        system_order_nos: list[str] = []
        for index, record in enumerate(pagination.orders):
            _custom_rows, shipment_row, _presence = _normalize_order(
                record,
                source_page=0,
                source_order_index=index,
            )
            observed_platform_order_no = str(
                shipment_row.get("platform_order_no")
                or record.order_number
                or ""
            ).strip()
            if (
                observed_platform_order_no
                and observed_platform_order_no != platform_order_no
            ):
                continue
            system_order_no = str(
                shipment_row.get("system_order_no")
                or record.global_order_no
                or ""
            ).strip()
            if not system_order_no:
                return (
                    platform_order_no,
                    (),
                    "同平台订单列表存在无法识别系统单号的记录。",
                    pagination.request_ids,
                )
            if system_order_no not in system_order_nos:
                system_order_nos.append(system_order_no)
        if not system_order_nos:
            return (
                platform_order_no,
                (),
                "同平台订单列表没有返回可核验的系统单号。",
                pagination.request_ids,
            )
        return (
            platform_order_no,
            tuple(system_order_nos),
            "",
            pagination.request_ids,
        )

    results = await asyncio.gather(*(one(platform) for platform in platforms))
    by_platform = {
        platform: (system_order_nos, error)
        for platform, system_order_nos, error, _request_ids in results
    }
    request_ids = tuple(
        dict.fromkeys(
            request_id
            for _platform, _systems, _error, result_request_ids in results
            for request_id in result_request_ids
            if request_id
        )
    )
    return by_platform, request_ids


def _product_identity_rows(
    detail_results: Mapping[
        str,
        tuple[Mapping[str, Any] | None, str, str],
    ],
    target: _ProductIdentityTarget,
    *,
    source_order_index: int,
) -> tuple[list[dict[str, Any]], str]:
    payload, detail_error, _request_id = detail_results.get(
        target.system_order_no,
        (None, "订单详情没有返回结果。", ""),
    )
    if detail_error or payload is None:
        return [], detail_error or "订单详情没有返回可解析商品数据。"
    return _detail_rows_for_identity_target(
        payload,
        target,
        source_order_index=source_order_index,
    )


def _observed_identity_values(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    observed_asins = tuple(
        value
        for value in dict.fromkeys(
            str(row.get("asin") or "").strip() for row in rows
        )
        if value
    )
    selected = preferred_product_type(
        identify_product_types(
            [str(row.get("asin_text") or row.get("asin") or "") for row in rows]
        )
    )
    return observed_asins, ((selected,) if selected else ())


async def read_order_product_type_details(
    gateway: OrderListGateway,
    targets: Sequence[Mapping[str, Any]],
) -> tuple[tuple[ProductTypeBackfillObservation, ...], tuple[str, ...]]:
    """Read complete order details and classify their ASINs without workflow rules.

    This is intentionally a read-only identity operation.  It does not decide
    whether a product is ready for customization automation, and it never
    infers a type from SKU text, list-page item folds, or a retry mode.  When
    the exact system order has no ASIN, all system-order siblings discovered
    by the platform identity are read in full before attribution.
    """

    identity_targets: list[_ProductIdentityTarget] = []
    seen: set[tuple[str, str]] = set()
    for raw in targets:
        platform_order_no = str(raw.get("platform_order_no") or "").strip()
        system_order_no = str(raw.get("system_order_no") or "").strip()
        key = (system_order_no, platform_order_no)
        if not all(key) or key in seen:
            continue
        seen.add(key)
        identity_targets.append(
            _ProductIdentityTarget(
                platform_order_no=platform_order_no,
                system_order_no=system_order_no,
                paid_at_text="",
                sku="",
                tag_text="",
                source_order_index=None,
                backfill_only=True,
            )
        )

    detail_results, exact_detail_request_ids = await _read_product_identity_details(
        gateway,
        identity_targets,
    )
    exact_rows_by_key: dict[tuple[str, str], tuple[list[dict[str, Any]], str]] = {}
    platforms_needing_siblings: list[str] = []
    for index, target in enumerate(identity_targets):
        rows, error = _product_identity_rows(
            detail_results,
            target,
            source_order_index=index,
        )
        exact_rows_by_key[(target.system_order_no, target.platform_order_no)] = (
            rows,
            error,
        )
        observed_asins, _product_types = _observed_identity_values(rows)
        if not error and not observed_asins:
            platforms_needing_siblings.append(target.platform_order_no)

    sibling_results, sibling_list_request_ids = (
        await _read_platform_sibling_system_orders(
            gateway,
            platforms_needing_siblings,
        )
        if platforms_needing_siblings
        else ({}, ())
    )

    target_by_key = {
        (target.system_order_no, target.platform_order_no): target
        for target in identity_targets
    }
    sibling_targets: list[_ProductIdentityTarget] = []
    for platform_order_no in platforms_needing_siblings:
        system_order_nos, discovery_error = sibling_results.get(
            platform_order_no,
            ((), "同平台订单的兄弟系统单列表没有返回结果。"),
        )
        if discovery_error:
            continue
        for system_order_no in system_order_nos:
            key = (system_order_no, platform_order_no)
            if key in target_by_key:
                continue
            sibling_target = _ProductIdentityTarget(
                platform_order_no=platform_order_no,
                system_order_no=system_order_no,
                paid_at_text="",
                sku="",
                tag_text="",
                source_order_index=None,
                backfill_only=True,
            )
            target_by_key[key] = sibling_target
            sibling_targets.append(sibling_target)

    sibling_detail_results, sibling_detail_request_ids = (
        await _read_product_identity_details(gateway, sibling_targets)
        if sibling_targets
        else ({}, ())
    )
    all_detail_results = {**detail_results, **sibling_detail_results}
    request_ids = tuple(
        dict.fromkeys(
            (
                *exact_detail_request_ids,
                *sibling_list_request_ids,
                *sibling_detail_request_ids,
            )
        )
    )

    observations: list[ProductTypeBackfillObservation] = []
    for index, target in enumerate(identity_targets):
        rows, detail_error = exact_rows_by_key.get(
            (target.system_order_no, target.platform_order_no),
            ([], "订单详情没有返回结果。"),
        )
        if detail_error:
            observations.append(
                ProductTypeBackfillObservation(
                    platform_order_no=target.platform_order_no,
                    system_order_no=target.system_order_no,
                    error=detail_error,
                )
            )
            continue

        observed_asins, product_types = _observed_identity_values(rows)
        if not observed_asins:
            sibling_system_order_nos, discovery_error = sibling_results.get(
                target.platform_order_no,
                ((), "同平台订单的兄弟系统单列表没有返回结果。"),
            )
            if discovery_error:
                observations.append(
                    ProductTypeBackfillObservation(
                        platform_order_no=target.platform_order_no,
                        system_order_no=target.system_order_no,
                        error=discovery_error,
                    )
                )
                continue
            if target.system_order_no not in sibling_system_order_nos:
                observations.append(
                    ProductTypeBackfillObservation(
                        platform_order_no=target.platform_order_no,
                        system_order_no=target.system_order_no,
                        error="同平台订单列表未包含目标系统单，无法证明兄弟单汇总完整。",
                    )
                )
                continue

            aggregate_rows: list[dict[str, Any]] = []
            aggregate_error = ""
            for sibling_index, sibling_system_order_no in enumerate(
                sibling_system_order_nos
            ):
                sibling_target = target_by_key[
                    (sibling_system_order_no, target.platform_order_no)
                ]
                sibling_rows, sibling_error = _product_identity_rows(
                    all_detail_results,
                    sibling_target,
                    source_order_index=sibling_index,
                )
                if sibling_error:
                    aggregate_error = sibling_error
                    break
                aggregate_rows.extend(sibling_rows)
            if aggregate_error:
                observations.append(
                    ProductTypeBackfillObservation(
                        platform_order_no=target.platform_order_no,
                        system_order_no=target.system_order_no,
                        error=aggregate_error,
                    )
                )
                continue
            observed_asins, product_types = _observed_identity_values(
                aggregate_rows
            )

        observations.append(
            ProductTypeBackfillObservation(
                platform_order_no=target.platform_order_no,
                system_order_no=target.system_order_no,
                product_types=product_types,
                observed_asins=observed_asins,
            )
        )
    return tuple(observations), request_ids


async def scan_customization_candidates(
    gateway: OrderListGateway,
    processed_orders: ProcessedOrderSource | AbstractSet[str] | Iterable[str],
    *,
    filters: Mapping[str, Any] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
    limit: int = 0,
    reactivation_order_nos: Iterable[str] = (),
    pending_product_identities: Iterable[Mapping[str, Any]] = (),
    snapshot_retry_delays_seconds: Sequence[
        float
    ] = DEFAULT_CUSTOMIZATION_SNAPSHOT_RETRY_DELAYS,
    snapshot_retry_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CustomizationApiScanResult:
    """Build customization candidates with the confirmed 96-hour payment window."""

    pagination = await fetch_stable_order_snapshot(
        gateway,
        filters=filters,
        page_size=page_size,
        max_pages=max_pages,
        retry_delays_seconds=snapshot_retry_delays_seconds,
        sleeper=snapshot_retry_sleeper,
    )
    normalized = normalize_api_order_rows(pagination)
    missing = normalized.missing_fields(CUSTOMIZATION_REQUIRED_FIELDS)
    missing_order_indexes = {item.order_index for item in missing}
    processed = _processed_order_set(processed_orders)
    reactivation_targets = {
        str(value).strip() for value in reactivation_order_nos if str(value).strip()
    }
    evaluable_rows = [
        dict(row)
        for row in normalized.customization_rows
        if row.get("_source_order_index") not in missing_order_indexes
    ]
    initial_debug: dict[str, Any] = {"scan_rows": []}
    build_batch_candidates_from_rows(
        evaluable_rows,
        processed,
        limit=0,
        payment_window_hours=DEFAULT_PAYMENT_WINDOW_HOURS,
        debug=initial_debug,
    )

    prior_targets = {
        target.platform_order_no: target
        for raw in pending_product_identities
        if isinstance(raw, Mapping)
        for target in [_identity_target_from_pending(raw)]
        if target is not None
    }
    rows_by_platform = _customization_rows_by_platform(evaluable_rows)
    targets: dict[str, _ProductIdentityTarget] = {}
    if pagination.complete and not missing:
        initial_groups = {
            str(group.get("platform_order_no") or "").strip(): group
            for group in (initial_debug.get("platform_groups") or ())
            if isinstance(group, Mapping)
        }
        for platform_order_no, group in initial_groups.items():
            if (
                platform_order_no in processed
                or bool(group.get("automation_supported"))
                or str(group.get("payment_status") or "") != "recent"
                or bool(group.get("buyer_cancel_requested"))
                or (
                    str(group.get("tag_text") or "").strip()
                    and platform_order_no not in prior_targets
                )
            ):
                continue
            target = _identity_target_from_rows(
                platform_order_no,
                rows_by_platform.get(platform_order_no, ()),
                prior_state=(
                    prior_targets.get(platform_order_no).prior_state
                    if platform_order_no in prior_targets
                    else ""
                ),
                backfill_only=(
                    prior_targets.get(platform_order_no).backfill_only
                    if platform_order_no in prior_targets
                    else False
                ),
            )
            if target is not None:
                targets[platform_order_no] = target
        for platform_order_no, pending_target in prior_targets.items():
            current_rows = rows_by_platform.get(platform_order_no)
            if current_rows:
                current_asins = {
                    str(row.get("asin") or "").strip().upper()
                    for row in current_rows
                    if str(row.get("asin") or "").strip()
                }
                if current_asins:
                    continue
                current_target = _identity_target_from_rows(
                    platform_order_no,
                    current_rows,
                    prior_state=pending_target.prior_state,
                    backfill_only=pending_target.backfill_only,
                )
                if current_target is not None:
                    targets[platform_order_no] = current_target
            else:
                targets[platform_order_no] = pending_target

    detail_results, detail_request_ids = await _read_product_identity_details(
        gateway,
        tuple(targets.values()),
    )
    detail_errors: dict[str, str] = {}
    enriched_rows_by_platform: dict[str, list[dict[str, Any]]] = {}
    synthetic_source_index = len(normalized.order_field_presence)
    for target in targets.values():
        payload, lookup_error, _request_id = detail_results.get(
            target.system_order_no,
            (None, "详情查询未返回结果。", ""),
        )
        if payload is None:
            detail_errors[target.platform_order_no] = lookup_error
            continue
        source_index = (
            target.source_order_index
            if target.source_order_index is not None
            else synthetic_source_index
        )
        if target.source_order_index is None:
            synthetic_source_index += 1
        detail_rows, detail_error = _detail_rows_for_identity_target(
            payload,
            target,
            source_order_index=source_index,
        )
        if detail_error:
            detail_errors[target.platform_order_no] = detail_error
            continue
        enriched_rows_by_platform[target.platform_order_no] = detail_rows

    final_rows: list[dict[str, Any]] = []
    replaced_platforms: set[str] = set()
    for row in evaluable_rows:
        platform_order_no = str(row.get("platform_order_no") or "").strip()
        replacement = enriched_rows_by_platform.get(platform_order_no)
        if replacement is None:
            final_rows.append(row)
        elif platform_order_no not in replaced_platforms:
            final_rows.extend(replacement)
            replaced_platforms.add(platform_order_no)
    for platform_order_no, replacement in enriched_rows_by_platform.items():
        if platform_order_no not in replaced_platforms:
            final_rows.extend(replacement)

    quarantined_rows = [
        dict(row)
        for row in normalized.customization_rows
        if row.get("_source_order_index") in missing_order_indexes
    ]
    effective_normalized = NormalizedOrderRows(
        customization_rows=tuple([*final_rows, *quarantined_rows]),
        shipment_rows=normalized.shipment_rows,
        order_field_presence=normalized.order_field_presence,
        source_pages=normalized.source_pages,
    )
    debug: dict[str, Any] = {"scan_rows": []}
    evaluated_candidates = build_batch_candidates_from_rows(
        final_rows,
        processed,
        limit=0,
        payment_window_hours=DEFAULT_PAYMENT_WINDOW_HOURS,
        debug=debug,
    )
    if prior_targets:
        recovery_candidates = build_batch_candidates_from_rows(
            [
                row
                for row in final_rows
                if str(row.get("platform_order_no") or "").strip()
                in prior_targets
            ],
            processed - set(prior_targets),
            limit=0,
            payment_window_hours=DEFAULT_PAYMENT_WINDOW_HOURS,
            ignore_payment_window=True,
        )
        candidate_by_platform = {
            candidate.platform_order_no: candidate
            for candidate in evaluated_candidates
        }
        for candidate in recovery_candidates:
            prior_target = prior_targets.get(candidate.platform_order_no)
            if prior_target is not None and prior_target.backfill_only:
                continue
            candidate_by_platform.setdefault(candidate.platform_order_no, candidate)
        evaluated_candidates = list(candidate_by_platform.values())
    if limit:
        evaluated_candidates = evaluated_candidates[:limit]
    diagnostics = list(pagination.diagnostics)
    if missing:
        diagnostics.append(_missing_field_diagnostic("customization", missing))
    backfill_detail_error_count = sum(
        1
        for platform_order_no, target in targets.items()
        if target.backfill_only and platform_order_no in detail_errors
    )
    if backfill_detail_error_count:
        diagnostics.append(
            ApiScanDiagnostic(
                code="customization_product_identity_backfill_incomplete",
                message="部分历史订单商品类型详情读取未完成，后续扫描会安全重试。",
                affected_count=backfill_detail_error_count,
            )
        )
    if pagination.state is ApiScanState.FAILED:
        state = ApiScanState.FAILED
    elif not pagination.complete or missing:
        state = ApiScanState.INCOMPLETE
    else:
        state = ApiScanState.COMPLETE
    skip_counts = {
        str(key): int(value)
        for key, value in (debug.get("skip_counts") or {}).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    audit_decisions = _build_customization_audit_decisions(
        effective_normalized,
        evaluated_candidates,
        debug,
        missing,
        limit=limit,
        snapshot_complete=state is ApiScanState.COMPLETE,
    )
    candidates = tuple(evaluated_candidates) if state is ApiScanState.COMPLETE else ()
    reactivation_candidates: tuple[BatchOrderItem, ...] = ()
    if state is ApiScanState.COMPLETE and reactivation_targets:
        reactivation_debug: dict[str, Any] = {"scan_rows": []}
        evaluated_reactivations = build_batch_candidates_from_rows(
            [
                dict(row)
                for row in effective_normalized.customization_rows
                if row.get("_source_order_index") not in missing_order_indexes
            ],
            processed - reactivation_targets,
            limit=0,
            payment_window_hours=DEFAULT_PAYMENT_WINDOW_HOURS,
            debug=reactivation_debug,
        )
        reactivation_candidates = tuple(
            candidate
            for candidate in evaluated_reactivations
            if candidate.platform_order_no in reactivation_targets
        )
    candidate_platforms = {candidate.platform_order_no for candidate in candidates}
    final_rows_by_platform = _customization_rows_by_platform(final_rows)
    final_groups = {
        str(group.get("platform_order_no") or "").strip(): group
        for group in (debug.get("platform_groups") or ())
        if isinstance(group, Mapping)
    }
    identity_platforms = (
        set(targets)
        | set(prior_targets)
        | {
            platform_order_no
            for platform_order_no, group in final_groups.items()
            if str(group.get("skip_reason") or "")
            in {"product_rules_incomplete", "unrecognized_product"}
        }
    )
    identity_platforms.difference_update(
        platform_order_no
        for platform_order_no, target in prior_targets.items()
        if target.backfill_only
    )
    identity_observations: list[ProductIdentityObservation] = []
    if state is ApiScanState.COMPLETE:
        for platform_order_no in sorted(identity_platforms):
            if platform_order_no in candidate_platforms:
                continue
            rows = final_rows_by_platform.get(platform_order_no, ())
            target = targets.get(platform_order_no) or prior_targets.get(platform_order_no)
            if target is None:
                continue
            asins = tuple(
                dict.fromkeys(
                    str(row.get("asin") or "").strip().upper()
                    for row in rows
                    if str(row.get("asin") or "").strip()
                )
            )
            tag_text = " | ".join(
                dict.fromkeys(
                    str(row.get("tag_text") or "").strip()
                    for row in rows
                    if str(row.get("tag_text") or "").strip()
                )
            ) or target.tag_text
            group = final_groups.get(platform_order_no, {})
            matched_catalogue_product = bool(group.get("matched_product_asins"))
            automation_supported = bool(group.get("automation_supported"))
            product_types = tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in (group.get("product_types") or ())
                    if str(value).strip()
                )
            )
            if tag_text:
                identity_state = "product_identity_tag_conflict"
                identity_status = "ASIN/标签冲突，等待人工复核"
            elif asins and not matched_catalogue_product:
                identity_state = "product_identity_unrecognized"
                identity_status = "ASIN 已同步，但未匹配定制产品"
            elif matched_catalogue_product and not automation_supported:
                identity_state = "product_identity_review"
                identity_status = "商品类型已识别，但自动化处理规则不完整"
            elif asins:
                identity_state = "product_identity_review"
                identity_status = "商品已识别，订单结构需人工复核"
            else:
                identity_state = "product_identity_pending"
                identity_status = "等待 ASIN 同步"
            identity_observations.append(
                ProductIdentityObservation(
                    platform_order_no=platform_order_no,
                    system_order_no=target.system_order_no,
                    paid_at_text=(
                        max(
                            (
                                str(row.get("paid_at_text") or "").strip()
                                for row in rows
                            ),
                            default="",
                        )
                        or target.paid_at_text
                    ),
                    sku=(
                        " | ".join(
                            dict.fromkeys(
                                str(row.get("sku") or "").split(" 共", 1)[0].strip()
                                for row in rows
                                if str(row.get("sku") or "").strip()
                            )
                        )
                        or target.sku
                    ),
                    tag_text=tag_text,
                    state=identity_state,
                    status_text=identity_status,
                    last_error=detail_errors.get(platform_order_no, ""),
                    detail_attempted=platform_order_no in targets,
                    observed_asins=asins,
                    product_types=product_types,
                )
            )

    if identity_observations:
        decision_by_platform = {
            str(item.get("platform_order_no") or "").strip(): dict(item)
            for item in audit_decisions
        }
        for observation in identity_observations:
            decision = decision_by_platform.get(observation.platform_order_no)
            if decision is None:
                decision = {
                    "platform_order_no": observation.platform_order_no,
                    "system_order_no": observation.system_order_no,
                    "paid_at": observation.paid_at_text,
                    "custom_tag_text": observation.tag_text,
                    "items": [
                        {
                            "asin": asin,
                            "sku": observation.sku,
                        }
                        for asin in observation.observed_asins
                    ],
                    "product_types": list(observation.product_types),
                }
                decision_by_platform[observation.platform_order_no] = decision
            decision.update(
                decision="manual_review",
                reason_code=observation.state,
                detail_lookup_attempted=observation.detail_attempted,
            )
        audit_decisions = tuple(decision_by_platform.values())
        diagnostics.append(
            ApiScanDiagnostic(
                code="customization_product_identity_pending",
                message="部分订单商品信息尚未稳定，已保留到待同步/人工复核队列。",
                affected_count=len(identity_observations),
            )
        )

    observed_workflows = tuple(
        {
            "platform_order_no": str(group.get("platform_order_no") or "").strip(),
            "system_order_no": str(
                next(
                    iter(group.get("system_order_nos") or ()),
                    "",
                )
                or ""
            ).strip(),
            "product_type": str(group.get("product_type") or "").strip(),
            "product_types": tuple(
                str(value).strip()
                for value in (group.get("product_types") or ())
                if str(value).strip()
            ),
        }
        for group in (debug.get("platform_groups") or ())
        if str(group.get("platform_order_no") or "").strip()
    )
    return CustomizationApiScanResult(
        state=state,
        pagination=pagination,
        api_raw_order_count=sum(trace.item_count for trace in pagination.page_traces),
        row_count=len(normalized.customization_rows),
        candidate_count=len(candidates),
        processed_order_count=len(processed),
        payment_window_hours=float(DEFAULT_PAYMENT_WINDOW_HOURS),
        candidates=candidates,
        reactivation_candidates=reactivation_candidates,
        observed_workflows=observed_workflows,
        skip_counts=skip_counts,
        diagnostics=tuple(diagnostics),
        audit_decisions=audit_decisions,
        product_identity_observations=tuple(identity_observations),
        detail_request_ids=detail_request_ids,
    )


async def scan_shipment_candidates(
    gateway: OrderListGateway,
    queue_store: ShipmentQueueSink,
    shipment_tag_name: str,
    *,
    filters: Mapping[str, Any] | None = None,
    filter_windows: Sequence[Mapping[str, Any]] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
    dry_run: bool = True,
    reconcile_missing: bool = False,
) -> ShipmentApiScanResult:
    """Scan shipment candidates and update the queue through transactional store calls.

    ``filters`` are forwarded exactly as supplied for compatibility.  When
    ``filter_windows`` is supplied, every window is fully read and merged before
    queue mutation begins.  Callers are responsible for choosing documented
    pending-review filters for their Lingxing account.
    A complete pagination snapshot is required before any queue write.  Missing
    fields quarantine only the affected rows; they do not suppress otherwise
    safe candidates.  Missing-order reconciliation is additionally blocked
    whenever any row lacks a critical identity field.
    """

    scan_started_at = utc_now()
    run_id = uuid.uuid4().hex
    pagination, window_count = await _fetch_shipment_order_windows(
        gateway,
        filters=filters,
        filter_windows=filter_windows,
        page_size=page_size,
        max_pages=max_pages,
    )
    normalized = normalize_api_order_rows(pagination)
    missing = normalized.missing_fields(SHIPMENT_REQUIRED_FIELDS)
    missing_order_indexes = {item.order_index for item in missing}
    evaluable_shipment_rows = [
        dict(row)
        for index, row in enumerate(normalized.shipment_rows)
        if index not in missing_order_indexes
    ]
    report = build_shipment_scan_report(
        evaluable_shipment_rows,
        shipment_tag_name,
        dry_run=dry_run,
        queue_path=str(getattr(queue_store, "path", "") or ""),
    )
    snapshot_complete = pagination.complete
    reconciliation_safe = snapshot_complete and not missing
    report.table_total_count = pagination.expected_total
    report.scan_complete = snapshot_complete
    report.incomplete_field_count = len(missing)
    report.tagged_row_count = sum(
        1
        for row in normalized.shipment_rows
        if row_has_shipment_tag(str(row.get("tag_text") or ""), shipment_tag_name)
    )
    diagnostics = list(pagination.diagnostics)
    if missing:
        diagnostics.append(_shipment_missing_field_diagnostic(missing))
    if not snapshot_complete and report.status != "config_missing":
        report.status = "incomplete"
        report.message = "API 待审核快照不完整，已禁止缺失订单的人工完成判定。"
    elif missing and report.status != "config_missing":
        report.message = f"扫描完成；{len(missing)} 条字段不完整订单已转人工检查。"

    queue_failed = False
    queue_results: list[QueueInsertResult] = []
    tag_reconciliation = TagSnapshotReconcileResult(snapshot_complete=snapshot_complete)
    if not dry_run and report.status != "config_missing" and snapshot_complete:
        try:
            for candidate in report.candidates:
                # ShipmentQueueStore.upsert_candidate uses BEGIN IMMEDIATE and
                # commits each complete candidate state transition atomically.
                # Restoring a tag-paused task requires the separately reconciled
                # complete tag snapshot below; a candidate upsert alone is not
                # sufficient proof that the custom-tag field was fully read.
                queue_results.append(
                    queue_store.upsert_candidate(
                        candidate,
                        run_id=run_id,
                        allow_tag_restore=False,
                    )
                )
        except Exception as exc:
            queue_failed = True
            diagnostics.append(
                ApiScanDiagnostic(
                    code="shipment_queue_write_failed",
                    message="写入发货队列失败；本轮不执行缺失订单结案。",
                    error_type=type(exc).__name__,
                )
            )
        apply_queue_results(report, queue_results)

        if not queue_failed:
            reconciler = getattr(queue_store, "reconcile_shipment_tag_snapshot", None)
            if callable(reconciler):
                tag_states = {
                    str(row.get("system_order_no") or row.get("rowid") or "").strip(): (
                        row_has_shipment_tag(
                            str(row.get("tag_text") or ""), shipment_tag_name
                        )
                    )
                    for row in normalized.shipment_rows
                    if str(row.get("system_order_no") or row.get("rowid") or "").strip()
                    and bool((row.get("field_presence") or {}).get("tag"))
                }
                try:
                    tag_reconciliation = reconciler(
                        tag_states,
                        snapshot_complete=True,
                        run_id=run_id,
                    )
                    report.immediate_logistics_count += (
                        tag_reconciliation.immediate_logistics_count
                    )
                    report.immediate_erp_count += tag_reconciliation.immediate_erp_count
                except Exception as exc:
                    queue_failed = True
                    diagnostics.append(
                        ApiScanDiagnostic(
                            code="shipment_tag_reconciliation_failed",
                            message="根据完整标签快照暂停或恢复发货队列失败。",
                            error_type=type(exc).__name__,
                        )
                    )

        if reconciliation_safe and reconcile_missing and not queue_failed:
            visible_system_orders = {
                str(row.get("system_order_no") or row.get("rowid") or "").strip()
                for row in normalized.shipment_rows
                if str(row.get("system_order_no") or row.get("rowid") or "").strip()
            }
            try:
                report.manual_completed = queue_store.complete_missing_pending_orders(
                    visible_system_orders,
                    discovered_before=scan_started_at,
                    run_id=run_id,
                )
                report.manual_completed_count = len(report.manual_completed)
            except Exception as exc:
                queue_failed = True
                diagnostics.append(
                    ApiScanDiagnostic(
                        code="shipment_reconciliation_failed",
                        message="发货队列的缺失订单结案失败。",
                        error_type=type(exc).__name__,
                    )
                )

    if queue_failed and queue_results:
        diagnostics.append(
            ApiScanDiagnostic(
                code="shipment_queue_partial_update",
                message=(
                    "队列更新在后续错误前已有成功事务；前序变更可能已经提交，"
                    "请根据审计日志核对后安全重试。"
                ),
                affected_count=len(queue_results),
            )
        )

    if queue_failed or pagination.state is ApiScanState.FAILED:
        state = ApiScanState.FAILED
    elif not snapshot_complete or report.status == "config_missing":
        state = ApiScanState.INCOMPLETE
    else:
        state = ApiScanState.COMPLETE
    if queue_failed:
        report.status = "failed"
        report.message = "发货队列更新未完整成功，已停止后续结案。"

    audit_decisions = _build_shipment_audit_decisions(
        normalized,
        missing,
        report,
        queue_results,
        shipment_tag_name,
        dry_run=dry_run,
        queue_failed=queue_failed,
        snapshot_complete=snapshot_complete,
    )
    report.manual_review_count = sum(
        1 for item in audit_decisions if item.get("decision") == "manual_review"
    )
    safe_report = _safe_shipment_report(report)
    return ShipmentApiScanResult(
        state=state,
        pagination=pagination,
        window_count=window_count,
        api_raw_order_count=sum(trace.item_count for trace in pagination.page_traces),
        row_count=len(normalized.shipment_rows),
        evaluable_row_count=len(evaluable_shipment_rows),
        tagged_row_count=report.tagged_row_count,
        candidate_count=len(report.candidates),
        enqueued_count=report.enqueued_count,
        manual_completed_count=report.manual_completed_count,
        missing_critical_field_count=len(missing),
        paused_count=tag_reconciliation.paused_count,
        resumed_count=tag_reconciliation.resumed_count,
        immediate_logistics_count=report.immediate_logistics_count,
        immediate_erp_count=report.immediate_erp_count,
        report=safe_report,
        diagnostics=tuple(diagnostics),
        audit_decisions=audit_decisions,
    )


def _build_customization_audit_decisions(
    normalized: NormalizedOrderRows,
    candidates: Sequence[BatchOrderItem],
    debug: Mapping[str, Any],
    missing: Sequence[MissingFieldNotice],
    *,
    limit: int,
    snapshot_complete: bool,
) -> tuple[Mapping[str, Any], ...]:
    group_logs = {
        str(item.get("platform_order_no") or "").strip(): item
        for item in (debug.get("platform_groups") or [])
        if isinstance(item, Mapping)
    }
    preview_reasons: dict[tuple[str, str], str] = {}
    for item in debug.get("skip_preview") or []:
        if not isinstance(item, Mapping):
            continue
        platform_order_no = str(item.get("platform_order_no") or "").strip()
        reason = str(item.get("reason") or "").strip()
        system_order_nos = item.get("system_order_nos") or [""]
        if isinstance(system_order_nos, (str, bytes)):
            system_order_nos = [system_order_nos]
        for system_order_no in system_order_nos:
            preview_reasons[(platform_order_no, str(system_order_no or "").strip())] = reason

    candidate_platforms = {item.platform_order_no for item in candidates}
    missing_by_index = _missing_fields_by_index(missing)
    grouped_rows: dict[tuple[int, str], dict[str, Any]] = {}
    for row in normalized.customization_rows:
        source_index = int(row.get("_source_order_index") or 0)
        platform_order_no = str(row.get("platform_order_no") or "").strip()
        key = (source_index, platform_order_no)
        grouped = grouped_rows.setdefault(
            key,
            {
                "system_order_no": row.get("system_order_no"),
                "platform_order_no": platform_order_no,
                "paid_at_text": row.get("paid_at_text"),
                "customization_tag_text": row.get("tag_text"),
                "audit_items": [],
                "_source_order_index": source_index,
            },
        )
        grouped["audit_items"].append(
            {
                "asin": row.get("asin"),
                "sku": str(row.get("sku") or "").split(" 共", 1)[0],
                "quantity_raw": row.get("quantity_raw"),
                "quantity_normalized": row.get("quantity_normalized"),
                "quantity_status": row.get("quantity_status"),
                "sales_revenue": row.get("sales_revenue"),
                "sales_revenue_currency": row.get("sales_revenue_currency"),
                "sales_revenue_status": row.get("sales_revenue_status"),
                "order_total": row.get("order_total"),
                "order_total_currency": row.get("order_total_currency"),
                "order_total_status": row.get("order_total_status"),
            }
        )

    decisions: list[Mapping[str, Any]] = []
    for row in grouped_rows.values():
        index = int(row.get("_source_order_index") or 0)
        decision = _audit_order_base(row, tag_key="customization_tag_text")
        platform_order_no = decision["platform_order_no"]
        system_order_no = decision["system_order_no"]
        missing_fields = missing_by_index.get(index)
        if missing_fields:
            decision.update(
                decision="manual_review",
                reason_code="missing_critical_fields",
                missing_fields=list(missing_fields),
            )
        else:
            group = group_logs.get(str(platform_order_no))
            if group is not None:
                decision.update(
                    sales_revenue_total=group.get("sales_revenue_total"),
                    sales_revenue_currency=group.get("sales_revenue_currency"),
                    sales_revenue_status=group.get("sales_revenue_status"),
                    sales_revenue_source=group.get("sales_revenue_source"),
                )
                reason = str(group.get("skip_reason") or "").strip()
                if bool(group.get("hit")) or platform_order_no in candidate_platforms:
                    decision.update(decision="candidate", reason_code="eligible")
                elif reason == "already_processed_or_duplicate":
                    decision.update(decision="duplicate", reason_code=reason)
                else:
                    decision.update(decision="excluded", reason_code=reason or "not_eligible")
            elif platform_order_no in candidate_platforms:
                decision.update(decision="candidate", reason_code="eligible")
            else:
                reason = preview_reasons.get(
                    (str(platform_order_no), str(system_order_no)),
                    "",
                )
                if reason == "already_processed_or_duplicate":
                    decision.update(decision="duplicate", reason_code=reason)
                elif reason:
                    decision.update(decision="excluded", reason_code=reason)
                elif limit and len(candidates) >= limit:
                    decision.update(decision="excluded", reason_code="limit_reached")
                else:
                    decision.update(decision="manual_review", reason_code="not_evaluated")
        if decision.get("decision") == "candidate" and not snapshot_complete:
            decision.update(decision="manual_review", reason_code="snapshot_incomplete")
        decisions.append(decision)
    return tuple(decisions)


def _build_shipment_audit_decisions(
    normalized: NormalizedOrderRows,
    missing: Sequence[MissingFieldNotice],
    report: ShipmentScanReport,
    queue_results: Sequence[QueueInsertResult],
    shipment_tag_name: str,
    *,
    dry_run: bool,
    queue_failed: bool,
    snapshot_complete: bool,
) -> tuple[Mapping[str, Any], ...]:
    missing_by_index = _missing_fields_by_index(missing)
    candidate_keys = {
        _audit_identity(item.system_order_no, item.platform_order_no)
        for item in report.candidates
    }
    manual_reasons: dict[tuple[str, str], str] = {}
    for item in report.manual_reviews:
        manual_reasons.setdefault(
            _audit_identity(item.system_order_no, item.platform_order_no),
            str(item.reason or "manual_review"),
        )
    queue_by_key = {
        _audit_identity(result.candidate.system_order_no, result.candidate.platform_order_no): result
        for result in queue_results
    }
    configured_tag = str(shipment_tag_name or "").strip()
    decisions: list[Mapping[str, Any]] = []

    for index, row in enumerate(normalized.shipment_rows):
        decision = _audit_order_base(row, tag_key="tag_text")
        key = _audit_identity(decision["system_order_no"], decision["platform_order_no"])
        missing_fields = missing_by_index.get(index)
        tag_text = str(decision["custom_tag_text"] or "")

        if missing_fields:
            decision.update(
                decision="manual_review",
                reason_code="missing_critical_fields",
                missing_fields=list(missing_fields),
            )
        elif not configured_tag:
            decision.update(decision="manual_review", reason_code="shipment_tag_config_missing")
        elif not row_has_shipment_tag(tag_text, configured_tag):
            decision.update(decision="excluded", reason_code="shipment_tag_not_matched")
        elif key in manual_reasons:
            decision.update(decision="manual_review", reason_code=manual_reasons[key])
        elif key in queue_by_key:
            queue_result = queue_by_key[key]
            if queue_result.inserted:
                decision.update(decision="candidate", reason_code="enqueued")
            else:
                decision.update(
                    decision="duplicate",
                    reason_code="queue_conflict" if queue_result.conflict else "queue_duplicate",
                )
        elif key in candidate_keys:
            if not snapshot_complete and not dry_run:
                decision.update(
                    decision="manual_review",
                    reason_code="snapshot_incomplete_no_write",
                )
            elif queue_failed and not dry_run:
                decision.update(decision="manual_review", reason_code="queue_write_failed")
            else:
                decision.update(
                    decision="candidate",
                    reason_code="eligible_dry_run" if dry_run else "eligible",
                )
        else:
            decision.update(decision="manual_review", reason_code="not_evaluated")
        decisions.append(decision)

    # Reconciliation decisions refer to queue records that are intentionally
    # absent from the current API snapshot.  Keep the same safe schema without
    # adding logistics numbers or any stored customer data.
    for item in report.manual_completed:
        decisions.append(
            {
                "platform_order_no": str(item.platform_order_no or "").strip(),
                "system_order_no": str(item.system_order_no or "").strip(),
                "paid_at": "",
                "decision": "manual_completed",
                "reason_code": "missing_from_complete_snapshot",
                "custom_tag_text": "",
                "items": [],
            }
        )
    return tuple(decisions)


def _audit_order_base(row: Mapping[str, Any], *, tag_key: str) -> dict[str, Any]:
    products: list[dict[str, Any]] = []
    raw_items = row.get("audit_items")
    if isinstance(raw_items, (list, tuple)):
        for item in raw_items:
            if not isinstance(item, Mapping):
                continue
            products.append(
                {
                    "asin": str(item.get("asin") or "").strip(),
                    "sku": str(item.get("sku") or "").strip(),
                    "quantity_raw": _safe_quantity_raw(item.get("quantity_raw")),
                    "quantity_normalized": _audit_positive_int(item.get("quantity_normalized")),
                    "quantity_status": str(item.get("quantity_status") or "missing").strip(),
                }
            )
    return {
        "platform_order_no": str(row.get("platform_order_no") or "").strip(),
        "system_order_no": str(row.get("system_order_no") or row.get("rowid") or "").strip(),
        "paid_at": str(row.get("paid_at_text") or "").strip(),
        "decision": "",
        "reason_code": "",
        "custom_tag_text": str(row.get(tag_key) or "").strip(),
        "customer_shipping_service": str(
            row.get("customer_shipping_service") or ""
        ).strip(),
        "items": products,
    }


def _audit_identity(system_order_no: object, platform_order_no: object) -> tuple[str, str]:
    return str(system_order_no or "").strip(), str(platform_order_no or "").strip()


def _missing_fields_by_index(
    notices: Sequence[MissingFieldNotice],
) -> dict[int, tuple[str, ...]]:
    output: dict[int, tuple[str, ...]] = {}
    for notice in notices:
        output[notice.order_index] = tuple(str(value) for value in notice.missing_fields)
    return output


def _audit_positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _pagination_result(
    state: ApiScanState,
    orders: Sequence[OrderRecord],
    source_pages: Sequence[int],
    traces: Sequence[ApiPageTrace],
    expected_total: int | None,
    diagnostics: Sequence[ApiScanDiagnostic],
) -> OrderPaginationResult:
    return OrderPaginationResult(
        state=state,
        orders=tuple(orders),
        source_pages=tuple(source_pages),
        page_traces=tuple(traces),
        expected_total=expected_total,
        diagnostics=tuple(diagnostics),
    )


def _safe_shipment_report(report: ShipmentScanReport) -> ShipmentScanReport:
    """Detach and redact the UI-facing report after queue persistence completes."""

    safe_report = deepcopy(report)
    for candidate in [*safe_report.candidates, *safe_report.enqueued_candidates]:
        if candidate.customer_remark:
            candidate.customer_remark = redact_sensitive_text(candidate.customer_remark)
        if candidate.receiver_email:
            candidate.receiver_email = "<redacted-email>"
        if candidate.receiver_phone:
            candidate.receiver_phone = "<redacted-phone>"
        if candidate.receiver_name:
            candidate.receiver_name = "<redacted-name>"
        candidate.warnings = [redact_sensitive_text(value) for value in candidate.warnings]
    for item in safe_report.duplicate_skipped:
        if item.existing_last_error:
            item.existing_last_error = redact_sensitive_text(item.existing_last_error)
    for item in safe_report.manual_reviews:
        item.message = redact_sensitive_text(item.message)
    safe_report.warnings = [redact_sensitive_text(value) for value in safe_report.warnings]
    return safe_report


def _page_fingerprint(items: Sequence[OrderRecord]) -> str:
    normalized: list[Any] = []
    for item in items:
        identity = _stable_order_identity(item)
        if identity:
            normalized.append(("id", identity))
            continue
        try:
            payload_text = json.dumps(item.payload, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            payload_text = type(item.payload).__name__
        normalized.append(("payload", hashlib.sha256(payload_text.encode("utf-8")).hexdigest()))
    encoded = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_order_identity(record: OrderRecord) -> str | None:
    global_order_no = _optional_text(record.global_order_no)
    order_number = _optional_text(record.order_number)
    if not global_order_no and not order_number:
        return None
    return f"{global_order_no or ''}\x1f{order_number or ''}"


def _processed_order_set(
    source: ProcessedOrderSource | AbstractSet[str] | Iterable[str],
) -> set[str]:
    loader = getattr(source, "processed_platform_orders", None)
    values = loader() if callable(loader) else source
    return {str(value).strip() for value in values if str(value).strip()}


def _missing_field_diagnostic(
    scan_kind: str,
    notices: Sequence[MissingFieldNotice],
) -> ApiScanDiagnostic:
    missing_fields = tuple(sorted({field for notice in notices for field in notice.missing_fields}))
    return ApiScanDiagnostic(
        code=f"{scan_kind}_critical_fields_missing",
        message="API 订单缺少扫描所需关键字段，本轮不视为完整快照。",
        affected_count=len(notices),
        missing_fields=missing_fields,
    )


def _shipment_missing_field_diagnostic(
    notices: Sequence[MissingFieldNotice],
) -> ApiScanDiagnostic:
    missing_fields = tuple(sorted({field for notice in notices for field in notice.missing_fields}))
    return ApiScanDiagnostic(
        code="shipment_rows_quarantined",
        message="部分 API 订单缺少自动标发所需字段，仅相关订单已转人工检查。",
        affected_count=len(notices),
        missing_fields=missing_fields,
    )


def _normalize_order(
    record: OrderRecord,
    *,
    source_page: int,
    source_order_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], Mapping[str, bool]]:
    payload = dict(record.payload)
    mappings = _mapping_tree(payload)

    system_present, system_value = _lookup(mappings, _SYSTEM_ALIASES)
    _, platform_value = _lookup(mappings, _PLATFORM_ALIASES)
    paid_present, paid_value = _lookup(mappings, _PAID_AT_ALIASES)
    # Tag containers are order-level API fields.  Restrict lookup to the order
    # mapping so product metadata such as item_info[].tags cannot become a
    # workflow-blocking order tag.
    tag_views = _order_tag_views((payload,))
    remark_present, remark_value = _lookup(mappings, _CUSTOMER_REMARK_ALIASES)
    status_present, status_value = _lookup(mappings, _STATUS_ALIASES)
    logistics_present, logistics_value = _lookup(mappings, _LOGISTICS_ALIASES)
    customer_shipping_service_present, customer_shipping_service_value = (
        _lookup_customer_shipping_service(mappings)
    )
    _, receiver_name_value = _lookup(mappings, _RECEIVER_NAME_ALIASES)
    receiver_email = receiver_email_from_payload(payload) or ""
    _, receiver_phone_value = _lookup(mappings, _RECEIVER_PHONE_ALIASES)
    _, sales_platform_code_value = _lookup(mappings, _SALES_PLATFORM_CODE_ALIASES)
    _, sales_platform_name_value = _lookup(mappings, _SALES_PLATFORM_NAME_ALIASES)
    _, store_name_value = _lookup(mappings, _STORE_NAME_ALIASES)
    _, site_name_value = _lookup(mappings, _SITE_NAME_ALIASES)

    system_order_no = _optional_text(record.global_order_no) or _optional_text(system_value) or ""
    platform_order_no = _optional_text(record.order_number) or _optional_text(platform_value) or ""
    system_present = bool(system_order_no) and (system_present or bool(record.global_order_no))
    paid_at_text = _format_datetime(paid_value)
    customization_tag_text = tag_views.customization_text
    shipment_tag_text = tag_views.shipment_text
    customer_remark = _structured_text(remark_value)
    status_text = _structured_text(status_value)
    buyer_cancel_requested = has_buyer_cancel_request(payload)
    if buyer_cancel_requested and BUYER_CANCEL_REQUEST_TEXT not in status_text:
        status_text = " | ".join(
            value for value in (status_text, BUYER_CANCEL_REQUEST_TEXT) if value
        )
    logistics = _structured_text(logistics_value)
    customer_shipping_service = _structured_text(customer_shipping_service_value)

    items_present, raw_items = _find_item_list(mappings)
    if not items_present:
        top_product_present, _ = _lookup(mappings, (*_ASIN_ALIASES, *_SKU_ALIASES))
        items_present = top_product_present
    item_mappings = [item for item in raw_items if isinstance(item, Mapping)]
    if not item_mappings:
        item_mappings = [{}]

    order_total_currency_present, order_total_currency_value = _lookup(
        (payload,),
        _ORDER_TOTAL_CURRENCY_ALIASES,
    )
    order_total_present = False
    order_total_value: Any = None
    # ``transaction_info[].order_total_amount`` is a system-order aggregate in
    # the documented MultiPlatOrderV2 response.  It is authoritative only when
    # the payload represents at most one platform order; otherwise applying it
    # to every item would duplicate a merged order's total across platform orders.
    if len(_payload_platform_order_nos(payload, item_mappings)) <= 1:
        order_total_present, order_total_value = _lookup(
            (payload,),
            _ORDER_TOTAL_ALIASES,
        )
        if not order_total_present:
            order_total_present, order_total_value = _transaction_order_total(
                payload
            )

    customization_rows: list[dict[str, Any]] = []
    all_skus: list[str] = []
    all_asins: list[str] = []
    audit_items: list[dict[str, Any]] = []
    item_platform_order_nos: list[str] = []
    for raw_item in item_mappings:
        item_only_tree = _mapping_tree(dict(raw_item))
        item_tree = item_only_tree + mappings
        _, item_platform_value = _lookup(item_only_tree, _PLATFORM_ALIASES)
        _, asin_value = _lookup(item_tree, _ASIN_ALIASES)
        _, sku_value = _lookup(item_tree, _SKU_ALIASES)
        _, quantity_value = _lookup(item_tree, _QUANTITY_ALIASES)
        revenue_present, revenue_value = _lookup(item_only_tree, _SALES_REVENUE_ALIASES)
        item_currency_present, item_currency_value = _lookup(
            item_only_tree,
            _SALES_REVENUE_CURRENCY_ALIASES,
        )
        currency_present = item_currency_present or order_total_currency_present
        currency_value = (
            item_currency_value
            if item_currency_present
            else order_total_currency_value
        )
        asin = _optional_text(asin_value) or ""
        sku = _optional_text(sku_value) or ""
        item_platform_order_no = _optional_text(item_platform_value) or platform_order_no
        platform_total_present = order_total_present
        platform_total_value = order_total_value
        platform_total_currency_present = order_total_currency_present
        platform_total_currency_value = order_total_currency_value
        for candidate_mapping in _platform_order_mappings(
            payload,
            item_platform_order_no,
        ):
            candidate_total_present, candidate_total_value = _lookup(
                (candidate_mapping,),
                _ORDER_TOTAL_ALIASES,
            )
            if candidate_total_present:
                platform_total_present = True
                platform_total_value = candidate_total_value
                candidate_currency_present, candidate_currency_value = _lookup(
                    (candidate_mapping,),
                    _ORDER_TOTAL_CURRENCY_ALIASES,
                )
                if candidate_currency_present:
                    platform_total_currency_present = True
                    platform_total_currency_value = candidate_currency_value
                break
        if (
            platform_total_present
            and not platform_total_currency_present
            and currency_present
        ):
            # The documented detail response keeps ``order_price_amount`` at
            # order level while exposing its currency on each ``order_item``.
            platform_total_currency_present = True
            platform_total_currency_value = currency_value
        if item_platform_order_no and item_platform_order_no not in item_platform_order_nos:
            item_platform_order_nos.append(item_platform_order_no)
        quantity_raw, quantity, quantity_status = _normalize_quantity(quantity_value)
        revenue_raw, revenue, revenue_currency, revenue_status = _normalize_sales_revenue(
            revenue_value if revenue_present else None,
            currency_value if currency_present else None,
        )
        (
            order_total_raw,
            order_total,
            order_total_currency,
            order_total_status,
        ) = _normalize_sales_revenue(
            platform_total_value if platform_total_present else None,
            (
                platform_total_currency_value
                if platform_total_currency_present
                else None
            ),
        )
        if asin and asin not in all_asins:
            all_asins.append(asin)
        if sku and sku not in all_skus:
            all_skus.append(sku)
        asin_text = _with_quantity(asin, quantity)
        sku_text = _with_quantity(sku, quantity)
        audit_items.append(
            {
                "asin": asin,
                "sku": sku,
                "quantity_raw": quantity_raw,
                "quantity_normalized": quantity,
                "quantity_status": quantity_status,
                "sales_revenue_raw": revenue_raw,
                "sales_revenue": revenue,
                "sales_revenue_currency": revenue_currency,
                "sales_revenue_status": revenue_status,
                "order_total_raw": order_total_raw,
                "order_total": order_total,
                "order_total_currency": order_total_currency,
                "order_total_status": order_total_status,
            }
        )
        row_text = _safe_business_row_text(
            system_order_no,
            item_platform_order_no,
            paid_at_text,
            asin_text,
            sku_text,
            customer_shipping_service,
            status_text,
            customization_tag_text,
        )
        customization_rows.append(
            {
                "system_order_no": system_order_no,
                "platform_order_no": item_platform_order_no,
                "row_text": row_text,
                "asin_text": asin_text,
                "asin": asin,
                "sku": sku_text,
                "quantity_raw": quantity_raw,
                "quantity_normalized": quantity,
                "quantity_status": quantity_status,
                "sales_revenue_raw": revenue_raw,
                "sales_revenue": revenue,
                "sales_revenue_currency": revenue_currency,
                "sales_revenue_status": revenue_status,
                "order_total_raw": order_total_raw,
                "order_total": order_total,
                "order_total_currency": order_total_currency,
                "order_total_status": order_total_status,
                "status_text": status_text,
                "buyer_cancel_requested": buyer_cancel_requested,
                "tag_text": customization_tag_text,
                "paid_at_text": paid_at_text,
                "logistics": logistics,
                "customer_shipping_service": customer_shipping_service,
                "source_page": source_page,
                "source_scroll_top": 0,
                "_source_order_index": source_order_index,
            }
        )

    shipment_platform_order_no = (
        item_platform_order_nos[0]
        if len(item_platform_order_nos) == 1
        else (platform_order_no if not item_platform_order_nos else "")
    )
    customization_platform_present = bool(customization_rows) and all(
        bool(str(row.get("platform_order_no") or "").strip()) for row in customization_rows
    )
    shipment_platform_present = bool(shipment_platform_order_no)
    shipment_row = {
        "system_order_no": system_order_no,
        "platform_order_no": shipment_platform_order_no,
        "rowid": system_order_no,
        "row_text": _safe_business_row_text(
            system_order_no,
            shipment_platform_order_no,
            paid_at_text,
            " ".join(all_asins),
            " | ".join(all_skus),
            customer_shipping_service,
            status_text,
            shipment_tag_text,
        ),
        "asin_text": " ".join(all_asins),
        "asin": all_asins[0] if all_asins else "",
        "sku": " | ".join(all_skus),
        "status_text": status_text,
        "buyer_cancel_requested": buyer_cancel_requested,
        "tag_text": shipment_tag_text,
        "customization_tag_text": customization_tag_text,
        "audit_items": audit_items,
        "customer_remark": customer_remark,
        "customer_shipping_service": customer_shipping_service,
        "receiver_name": _optional_text(receiver_name_value) or "",
        "receiver_email": receiver_email,
        "receiver_phone": _optional_text(receiver_phone_value) or "",
        "sales_platform_code": _optional_text(sales_platform_code_value) or "",
        "sales_platform_name": _optional_text(sales_platform_name_value) or "",
        "store_name": _optional_text(store_name_value) or "",
        "site_name": _optional_text(site_name_value) or "",
        "paid_at_text": paid_at_text,
        "logistics": logistics,
        "source_page": source_page,
        "source_scroll_top": 0,
        "_source_order_index": source_order_index,
        "field_presence": {
            "system": system_present,
            "platform": shipment_platform_present,
            "tag": tag_views.custom_field_present,
            "customer_remark": remark_present,
            "logistics": logistics_present,
            "customer_shipping_service": customer_shipping_service_present,
        },
    }
    presence = {
        "system": system_present,
        "platform": customization_platform_present,
        "shipment_platform": shipment_platform_present,
        "paid_at": bool(paid_present and paid_at_text),
        "tag": tag_views.custom_field_present,
        "customer_remark": remark_present,
        "items": items_present,
        "status": status_present,
        "logistics": logistics_present,
        "customer_shipping_service": customer_shipping_service_present,
    }
    return customization_rows, shipment_row, presence


def receiver_email_from_payload(payload: Mapping[str, Any]) -> str | None:
    """Read the recipient/buyer email without assuming one platform shape."""

    _, value = _lookup(_mapping_tree(payload), _RECEIVER_EMAIL_ALIASES)
    return _optional_text(value)


def receiver_phone_from_payload(payload: Mapping[str, Any]) -> str | None:
    """Read the recipient/buyer phone without assuming one platform shape."""

    _, value = _lookup(_mapping_tree(payload), _RECEIVER_PHONE_ALIASES)
    return _optional_text(value)


def customer_shipping_service_from_payload(
    payload: Mapping[str, Any],
    *,
    platform_order_no: str | None = None,
) -> tuple[bool, str | None]:
    """读取独立客选配送级别，并优先匹配目标平台订单的详情记录。"""

    platform_text = _optional_text(platform_order_no)
    if platform_text:
        for mapping in _platform_order_mappings(payload, platform_text):
            present, value = _lookup_customer_shipping_service(
                _mapping_tree(dict(mapping), max_depth=2)
            )
            if present:
                return True, _optional_text(_structured_text(value))
    present, value = _lookup_customer_shipping_service(_mapping_tree(payload))
    if not present:
        return False, None
    return True, _optional_text(_structured_text(value))


def _canonical_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _mapping_tree(root: Mapping[str, Any], *, max_depth: int = 4) -> list[Mapping[str, Any]]:
    output: list[Mapping[str, Any]] = []
    queue: list[tuple[Mapping[str, Any], int]] = [(root, 0)]
    seen: set[int] = set()
    while queue:
        current, depth = queue.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(current)
        if depth >= max_depth:
            continue
        for value in current.values():
            if isinstance(value, Mapping):
                queue.append((value, depth + 1))
            elif isinstance(value, (list, tuple)):
                # The documented order-list response keeps both
                # ``platform_info`` and ``item_info`` as arrays of objects.
                # Traverse those wrappers so identifiers/payment fields remain
                # discoverable without assuming one platform-specific shape.
                queue.extend(
                    (item, depth + 1)
                    for item in value
                    if isinstance(item, Mapping)
                )
    return output


def _lookup(
    mappings: Sequence[Mapping[str, Any]],
    aliases: Sequence[str],
) -> tuple[bool, Any]:
    wanted = {_canonical_key(alias) for alias in aliases}
    for mapping in mappings:
        for key, value in mapping.items():
            if _canonical_key(key) in wanted:
                return True, value
    return False, None


def _lookup_customer_shipping_service(
    mappings: Sequence[Mapping[str, Any]],
) -> tuple[bool, Any]:
    """优先读取明确的客选配送类别，再兼容旧的配送级别字段。"""

    for aliases in _CUSTOMER_SHIPPING_SERVICE_ALIAS_GROUPS:
        present, value = _lookup(mappings, aliases)
        if present:
            return True, value
    # 旧接口/网页适配器曾把客选配送级别放在根级 logistics。只接受明确的
    # Standard/Expedited 语义；UPS-全程或包含线路 ID 的物流对象不能进入此字段。
    present, value = _lookup(mappings, ("logistics",))
    if present:
        text = _structured_text(value).strip().casefold()
        if (
            text.startswith(("standard", "expedited"))
            or "标准配送" in text
            or "标准物流" in text
            or "加急" in text
        ):
            return True, value
    return False, None


def _order_tag_views(mappings: Sequence[Mapping[str, Any]]) -> _OrderTagViews:
    """Collect every tag container and retain its API type semantics.

    Typed entries are eligible only when Lingxing identifies them as custom
    order tags.  Untyped legacy aliases are kept for compatibility with older
    response fixtures and browser-shaped adapters.  Every typed custom order
    tag remains visible to both workflows; only Lingxing system hints are
    discarded.
    """

    wanted = {_canonical_key(alias) for alias in _TAG_CONTAINER_ALIASES}
    system_containers = {
        _canonical_key(alias) for alias in _UNTYPED_SYSTEM_TAG_CONTAINER_ALIASES
    }
    values: list[tuple[str, Any]] = []
    custom_field_present = False
    system_field_present = False
    for mapping in mappings:
        for key, value in mapping.items():
            source_key = _canonical_key(key)
            if source_key not in wanted:
                continue
            if source_key in system_containers:
                system_field_present = True
            else:
                # Only the general order-tag containers prove that the API
                # returned the custom-tag field.  pending/exception siblings
                # are system-status hints and cannot authorize pause/resume.
                custom_field_present = True
            values.append((source_key, value))

    # Preserve the former top-level singular aliases without accidentally
    # rediscovering each ``tag_name`` inside the typed container lists.
    if mappings:
        singular_wanted = {_canonical_key(alias) for alias in _SINGULAR_TAG_ALIASES}
        for key, value in mappings[0].items():
            if _canonical_key(key) in singular_wanted:
                custom_field_present = True
                values.append((_canonical_key(key), value))

    customization_names: list[str] = []
    shipment_names: list[str] = []
    for source_key, value in values:
        for tag_type, tag_name in _iter_order_tag_entries(value):
            # Live order-list responses expose pending/exception hints both as
            # typed rows in order_tag and as plain string arrays in sibling
            # fields.  A plain value from those sibling fields is a system
            # hint, not the user-defined label column read by the old scanner.
            if not tag_type and source_key in system_containers:
                continue
            if tag_type and not _is_custom_order_tag_type(tag_type):
                continue
            _append_unique(shipment_names, tag_name)
            _append_unique(customization_names, tag_name)

    return _OrderTagViews(
        custom_field_present=custom_field_present,
        system_field_present=system_field_present,
        customization_text=" | ".join(customization_names),
        shipment_text=" | ".join(shipment_names),
    )


def _iter_order_tag_entries(value: Any) -> Iterable[tuple[str, str]]:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, Mapping):
        tag_name = _mapping_alias_text(value, _TAG_NAME_ALIASES)
        if tag_name:
            yield _mapping_alias_text(value, _TAG_TYPE_ALIASES), tag_name
            return
        for nested in value.values():
            yield from _iter_order_tag_entries(nested)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            yield from _iter_order_tag_entries(nested)
        return
    text = str(value).strip()
    if text:
        yield "", text


def _mapping_alias_text(mapping: Mapping[str, Any], aliases: Sequence[str]) -> str:
    wanted = {_canonical_key(alias) for alias in aliases}
    for key, value in mapping.items():
        if _canonical_key(key) not in wanted:
            continue
        text = _structured_text(value)
        if text:
            return text
    return ""


def _is_custom_order_tag_type(value: str) -> bool:
    normalized = re.sub(r"[\s_\-/]+", "", str(value)).casefold()
    return normalized in _CUSTOM_ORDER_TAG_TYPES


def _append_unique(values: list[str], value: str) -> None:
    normalized = str(value).strip()
    if normalized and normalized not in values:
        values.append(normalized)


def _find_item_list(mappings: Sequence[Mapping[str, Any]]) -> tuple[bool, list[Any]]:
    wanted = {_canonical_key(alias) for alias in _ITEM_LIST_ALIASES}
    for mapping in mappings:
        for key, value in mapping.items():
            if _canonical_key(key) not in wanted:
                continue
            if isinstance(value, list):
                return True, value
            if isinstance(value, tuple):
                return True, list(value)
            return True, []
    return False, []


def _top_level_mapping_records(
    payload: Mapping[str, Any],
    aliases: Sequence[str],
) -> tuple[bool, list[Mapping[str, Any]]]:
    wanted = {_canonical_key(alias) for alias in aliases}
    for key, value in payload.items():
        if _canonical_key(key) not in wanted:
            continue
        if isinstance(value, Mapping):
            return True, [value]
        if isinstance(value, (list, tuple)):
            return True, [item for item in value if isinstance(item, Mapping)]
        return True, []
    return False, []


def _payload_platform_order_nos(
    payload: Mapping[str, Any],
    item_mappings: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return the documented platform identities contained by one system order."""

    _, platform_mappings = _top_level_mapping_records(
        payload,
        _PLATFORM_INFO_LIST_ALIASES,
    )
    values: list[str] = []
    for mapping in (*item_mappings, *platform_mappings):
        present, value = _lookup((mapping,), _PLATFORM_ALIASES)
        text = _optional_text(value) if present else None
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _transaction_order_total(payload: Mapping[str, Any]) -> tuple[bool, Any]:
    """Read the documented ``transaction_info[].order_total_amount`` safely."""

    present, transactions = _top_level_mapping_records(
        payload,
        _TRANSACTION_INFO_LIST_ALIASES,
    )
    if not present or not transactions:
        return False, None

    totals: list[Any] = []
    missing_total = False
    for transaction in transactions:
        total_present, total_value = _lookup((transaction,), _ORDER_TOTAL_ALIASES)
        if total_present:
            totals.append(total_value)
        else:
            missing_total = True
    if not totals:
        return False, None
    if missing_total:
        return True, {"status": "incomplete_transaction_order_totals"}

    fingerprints = {
        json.dumps(
            _safe_money_raw(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for value in totals
    }
    if len(fingerprints) != 1:
        return True, {"status": "conflicting_transaction_order_totals"}
    return True, totals[0]


def _platform_order_mappings(
    payload: Mapping[str, Any],
    platform_order_no: str,
) -> list[Mapping[str, Any]]:
    """Return only order-level platform records for one platform order.

    Product rows can also contain generic amount fields.  Restricting the
    fallback to documented platform-info containers prevents an item price
    from being mistaken for the whole order total.
    """

    wanted_containers = {
        _canonical_key(alias) for alias in _PLATFORM_INFO_LIST_ALIASES
    }
    output: list[Mapping[str, Any]] = []
    for key, value in payload.items():
        if _canonical_key(key) not in wanted_containers:
            continue
        records = value if isinstance(value, (list, tuple)) else (value,)
        for record in records:
            if not isinstance(record, Mapping):
                continue
            present, candidate_order_no = _lookup((record,), _PLATFORM_ALIASES)
            if present and _optional_text(candidate_order_no) == platform_order_no:
                output.append(record)
    return output


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _structured_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        preferred: list[str] = []
        for key in ("name", "label", "value", "text", "tag_name", "tagName"):
            if key in value:
                text = _structured_text(value[key])
                if text:
                    preferred.append(text)
        if preferred:
            return " | ".join(dict.fromkeys(preferred))
        return " | ".join(
            dict.fromkeys(text for item in value.values() if (text := _structured_text(item)))
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return " | ".join(dict.fromkeys(text for item in value if (text := _structured_text(item))))
    return str(value).strip()


def _format_datetime(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) and 0 < float(value) < 100_000_000_000_000:
        timestamp = float(value)
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        try:
            parsed = datetime.fromtimestamp(timestamp).astimezone()
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    text = str(value).strip()
    if not text:
        return ""
    normalized = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_quantity(value: Any) -> tuple[Any, int | None, str]:
    """Return a JSON-safe raw value and a conservative positive quantity."""

    raw = _safe_quantity_raw(value)
    if value is None or (isinstance(value, str) and not value.strip()):
        return raw, None, "missing"
    if isinstance(value, bool):
        return raw, None, "invalid"
    if isinstance(value, int):
        return (raw, value, "valid") if value > 0 else (raw, None, "non_positive")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return raw, None, "invalid"
        parsed = int(value)
        return (raw, parsed, "normalized") if parsed > 0 else (raw, None, "non_positive")
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return raw, None, "invalid"
        parsed = int(text)
        return (raw, parsed, "normalized") if parsed > 0 else (raw, None, "non_positive")
    return raw, None, "invalid"


def _normalize_sales_revenue(
    value: Any,
    currency_value: Any,
) -> tuple[Any, str | None, str | None, str]:
    """Normalize one displayed sales-revenue value without deriving it from price/quantity."""

    raw = _safe_money_raw(value)
    amount_value = value
    currency = _normalize_currency(currency_value)
    if isinstance(value, Mapping):
        value_tree = _mapping_tree(dict(value), max_depth=2)
        amount_present, nested_amount = _lookup(
            value_tree,
            ("amount", "value", "money", "sales_revenue", "salesRevenue"),
        )
        if not amount_present:
            return raw, None, currency, "invalid"
        amount_value = nested_amount
        if not currency:
            nested_currency_present, nested_currency = _lookup(
                value_tree,
                _SALES_REVENUE_CURRENCY_ALIASES,
            )
            if nested_currency_present:
                currency = _normalize_currency(nested_currency)
    if amount_value is None or (
        isinstance(amount_value, str) and not amount_value.strip()
    ):
        return raw, None, currency, "missing"
    if isinstance(amount_value, bool):
        return raw, None, currency, "invalid"

    if isinstance(amount_value, (int, float, Decimal)):
        if isinstance(amount_value, float) and not math.isfinite(amount_value):
            return raw, None, currency, "invalid"
        amount_text = str(amount_value)
    else:
        amount_text = str(amount_value).strip()
        currency_prefixes = {
            "US": "USD",
            "CA": "CAD",
            "C": "CAD",
            "AU": "AUD",
            "A": "AUD",
            "NZ": "NZD",
            "HK": "HKD",
            "SG": "SGD",
        }
        dollar_match = re.match(
            r"(?i)^\s*(US|CA|C|AU|A|NZ|HK|SG)?\s*\$",
            amount_text,
        )
        if dollar_match:
            prefix = str(dollar_match.group(1) or "").upper()
            currency = currency or currency_prefixes.get(prefix, "USD")
            amount_text = amount_text[dollar_match.end() :]
        else:
            code_match = re.match(
                r"(?i)^\s*(USD|CAD|AUD|NZD|HKD|SGD)\b",
                amount_text,
            )
            if code_match:
                currency = currency or code_match.group(1).upper()
                amount_text = amount_text[code_match.end() :]
        # A currency code supplied by the API remains authoritative, but any
        # leftover dollar sign is still formatting rather than part of the
        # numeric value.
        amount_text = amount_text.replace("$", "")
        amount_text = amount_text.replace(",", "").strip()
    try:
        amount = Decimal(amount_text)
    except (InvalidOperation, ValueError):
        return raw, None, currency, "invalid"
    if not amount.is_finite() or amount < 0:
        return raw, None, currency, "invalid"
    if not currency:
        return raw, format(amount, "f"), None, "currency_missing"
    # The high-value workflow intentionally applies the same numeric threshold
    # to USD and CAD.  Other currencies remain blocked from direct comparison.
    if currency not in {"USD", "CAD"}:
        return raw, format(amount, "f"), currency, "non_usd"
    return raw, format(amount, "f"), currency, "valid"


def _normalize_currency(value: Any) -> str | None:
    text = _structured_text(value).strip().upper()
    if not text:
        return None
    if text in {"$", "US$", "USD", "美元"}:
        return "USD"
    return text


def _safe_money_raw(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _safe_money_raw(item)
            for key, item in value.items()
            if isinstance(item, (type(None), bool, int, float, str, Mapping))
        }
    return f"<{type(value).__name__}>"


def _safe_quantity_raw(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return f"<{type(value).__name__}>"


def _with_quantity(value: str, quantity: int | None) -> str:
    if not value:
        return ""
    return f"{value} 共{quantity}" if quantity else value


def _safe_business_row_text(*values: object) -> str:
    return " ".join(str(value).strip() for value in values if str(value or "").strip())


_SYSTEM_ALIASES = (
    "global_order_no",
    "globalOrderNo",
    "system_order_no",
    "systemOrderNo",
    "erp_order_no",
    "erpOrderNo",
)
_PLATFORM_ALIASES = (
    "order_number",
    "orderNumber",
    "platform_order_no",
    "platformOrderNo",
    "platform_order_id",
    "platformOrderId",
    "amazon_order_id",
    "amazonOrderId",
    "source_order_no",
    "sourceOrderNo",
)
_RECEIVER_EMAIL_ALIASES = (
    "buyer_email",
    "buyerEmail",
    "receiver_email",
    "receiverEmail",
    "recipient_email",
    "recipientEmail",
)
_RECEIVER_NAME_ALIASES = (
    "receiver_name",
    "receiverName",
    "recipient_name",
    "recipientName",
    "consignee_name",
    "consigneeName",
    "buyer_name",
    "buyerName",
)
_RECEIVER_PHONE_ALIASES = (
    "receiver_tel",
    "receiverTel",
    "receiver_phone",
    "receiverPhone",
    "recipient_phone",
    "recipientPhone",
    "buyer_phone",
    "buyerPhone",
    "mobile",
)
_SALES_PLATFORM_CODE_ALIASES = (
    "platform_code",
    "platformCode",
    "platform_id",
    "platformId",
)
_SALES_PLATFORM_NAME_ALIASES = (
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
_PAID_AT_ALIASES = (
    "paid_at",
    "paidAt",
    "pay_time",
    "payTime",
    "payment_time",
    "paymentTime",
    "purchase_date",
    "purchaseDate",
    "order_pay_time",
    "orderPayTime",
    "global_payment_time",
    "globalPaymentTime",
)
_TAG_CONTAINER_ALIASES = (
    "tag_text",
    "tagText",
    "tag_names",
    "tagNames",
    "tags",
    "tag_list",
    "tagList",
    "order_tags",
    "orderTags",
    "order_tag",
    "orderTag",
    "pending_order_tag",
    "pendingOrderTag",
    "exception_order_tag",
    "exceptionOrderTag",
)
_SINGULAR_TAG_ALIASES = ("tag_name", "tagName")
_UNTYPED_SYSTEM_TAG_CONTAINER_ALIASES = (
    "pending_order_tag",
    "pendingOrderTag",
    "exception_order_tag",
    "exceptionOrderTag",
)
_TAG_NAME_ALIASES = ("tag_name", "tagName", "name", "label", "value", "text")
_TAG_TYPE_ALIASES = ("tag_type", "tagType", "type", "type_name", "typeName")
_CUSTOM_ORDER_TAG_TYPES = frozenset(
    {
        "2",
        "自定义订单标签",
        "customordertag",
        "customtag",
    }
)
_CUSTOMER_REMARK_ALIASES = (
    "customer_remark",
    "customerRemark",
    "customer_service_remark",
    "customerServiceRemark",
    "service_remark",
    "serviceRemark",
    "remark",
)
_STATUS_ALIASES = (
    "status_text",
    "statusText",
    "order_status_name",
    "orderStatusName",
    "status_name",
    "statusName",
    "order_status",
    "orderStatus",
    "status",
)
_LOGISTICS_ALIASES = (
    "logistics_name",
    "logisticsName",
    "logistics_type_name",
    "logisticsTypeName",
)
_CUSTOMER_SHIPPING_SERVICE_ALIAS_GROUPS = (
    (
        "customer_shipping_service",
        "customerShippingService",
        "shipment_service_level_category",
        "shipmentServiceLevelCategory",
        "ship_service_level_category",
        "shipServiceLevelCategory",
    ),
    ("shipping_service", "shippingService"),
    ("ship_service_level", "shipServiceLevel"),
)
_ITEM_LIST_ALIASES = (
    "order_item",
    "orderItem",
    "order_item_list",
    "orderItemList",
    "item_list",
    "itemList",
    "order_items",
    "orderItems",
    "items",
    "item_info",
    "itemInfo",
)
_PLATFORM_INFO_LIST_ALIASES = (
    "platform_info",
    "platformInfo",
    "platform_order_info",
    "platformOrderInfo",
    "platform_order_list",
    "platformOrderList",
)
_TRANSACTION_INFO_LIST_ALIASES = (
    "transaction_info",
    "transactionInfo",
)
_ASIN_ALIASES = (
    "asin",
    "amazon_asin",
    "amazonAsin",
    "product_id",
    "productId",
    "product_no",
    "productNo",
)
_SKU_ALIASES = (
    "sku",
    "seller_sku",
    "sellerSku",
    "local_sku",
    "localSku",
    "merchant_sku",
    "merchantSku",
    "msku",
)
_QUANTITY_ALIASES = (
    "quantity",
    "quality",
    "qty",
    "item_quantity",
    "itemQuantity",
    "order_quantity",
    "orderQuantity",
)
_SALES_REVENUE_ALIASES = (
    "sales_income",
    "salesIncome",
    "sales_revenue",
    "salesRevenue",
    "sales_revenue_amount",
    "salesRevenueAmount",
    "sales_proceeds",
    "salesProceeds",
    "sale_income",
    "saleIncome",
    "item_income",
    "itemIncome",
    "order_income",
    "orderIncome",
    "item_sales_amount",
    "itemSalesAmount",
    "sales_amount",
    "salesAmount",
    "revenue_amount",
    "revenueAmount",
    "income",
    "revenue",
)
_SALES_REVENUE_CURRENCY_ALIASES = (
    "sales_income_currency",
    "salesIncomeCurrency",
    "sales_revenue_currency",
    "salesRevenueCurrency",
    "amount_currency",
    "amountCurrency",
    "currency_code",
    "currencyCode",
    "currency_name",
    "currencyName",
    "currency",
)
_ORDER_TOTAL_ALIASES = (
    "order_total",
    "orderTotal",
    "order_total_amount",
    "orderTotalAmount",
    "total_order_amount",
    "totalOrderAmount",
    "order_amount",
    "orderAmount",
    "total_amount",
    "totalAmount",
    "amount_total",
    "amountTotal",
    "order_price",
    "orderPrice",
    "order_price_amount",
    "orderPriceAmount",
    "order_total_price",
    "orderTotalPrice",
    "total_order_price",
    "totalOrderPrice",
    "total_price",
    "totalPrice",
    "order_money",
    "orderMoney",
    "order_total_money",
    "orderTotalMoney",
    "total_money",
    "totalMoney",
    "grand_total",
    "grandTotal",
)
_ORDER_TOTAL_CURRENCY_ALIASES = (
    "order_currency",
    "orderCurrency",
    "order_total_currency",
    "orderTotalCurrency",
    "amount_currency",
    "amountCurrency",
    "currency_code",
    "currencyCode",
    "currency_name",
    "currencyName",
    "currency",
)


__all__ = [
    "ApiPageTrace",
    "ApiScanDiagnostic",
    "ApiScanState",
    "CustomizationApiScanResult",
    "MissingFieldNotice",
    "NormalizedOrderRows",
    "OrderPaginationResult",
    "ProductIdentityObservation",
    "ProductTypeBackfillObservation",
    "ShipmentApiScanResult",
    "fetch_all_order_pages",
    "normalize_api_order_rows",
    "redact_sensitive_payload",
    "redact_sensitive_text",
    "receiver_email_from_payload",
    "receiver_phone_from_payload",
    "read_order_product_type_details",
    "scan_customization_candidates",
    "scan_shipment_candidates",
]
