"""API-first order scanners used by the desktop application.

This module is intentionally conservative.  It accepts caller-provided
Lingxing filters verbatim, proves that pagination reached a stable end before
declaring a snapshot complete, and never uses an undocumented numeric status
code.  The existing business-rule functions remain the single source of truth
for customization and shipment candidate selection.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from collections.abc import Iterable, Mapping, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from lingxing_automation.constants import DEFAULT_PAYMENT_WINDOW_HOURS
from lingxing_automation.models import BatchOrderItem
from lingxing_automation.parsers.dates import classify_recent_payment_window
from lingxing_automation.pages.order_list import build_batch_candidates_from_rows
from shipment_automation.candidate_scanner import apply_queue_results, build_shipment_scan_report
from shipment_automation.models import ShipmentScanReport
from shipment_automation.queue_store import QueueInsertResult, utc_now

from .lingxing_gateway import OrderPage, OrderRecord


DEFAULT_API_PAGE_SIZE = 500
DEFAULT_MAX_API_PAGES = 200
CUSTOMIZATION_REQUIRED_FIELDS = ("system", "platform", "paid_at")
SHIPMENT_REQUIRED_FIELDS = ("system", "platform", "paid_at", "tag", "customer_remark")


class OrderListGateway(Protocol):
    async def list_orders(
        self,
        *,
        offset: int = 0,
        length: int = DEFAULT_API_PAGE_SIZE,
        filters: Mapping[str, Any] | None = None,
    ) -> OrderPage: ...


class ProcessedOrderSource(Protocol):
    def processed_platform_orders(self) -> set[str]: ...


class ShipmentQueueSink(Protocol):
    def upsert_candidate(
        self,
        candidate: Any,
        *,
        run_id: str | None = None,
    ) -> QueueInsertResult: ...

    def complete_missing_pending_orders(
        self,
        visible_system_order_nos: set[str],
        *,
        discovered_before: str,
        run_id: str | None = None,
    ) -> list[Any]: ...


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
    row_count: int
    candidate_count: int
    processed_order_count: int
    payment_window_hours: float
    candidates: tuple[BatchOrderItem, ...] = field(default_factory=tuple, repr=False)
    skip_counts: Mapping[str, int] = field(default_factory=dict)
    diagnostics: tuple[ApiScanDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state is ApiScanState.COMPLETE


@dataclass(frozen=True)
class ShipmentApiScanResult:
    state: ApiScanState
    pagination: OrderPaginationResult
    row_count: int
    tagged_row_count: int
    candidate_count: int
    enqueued_count: int
    manual_completed_count: int
    missing_critical_field_count: int
    report: ShipmentScanReport = field(repr=False)
    diagnostics: tuple[ApiScanDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        return self.state is ApiScanState.COMPLETE


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


def normalize_api_order_rows(pagination: OrderPaginationResult) -> NormalizedOrderRows:
    """Normalize snake/camel aliases and nested item lists into legacy row shapes."""

    customization_rows: list[dict[str, Any]] = []
    shipment_rows: list[dict[str, Any]] = []
    presence_rows: list[Mapping[str, bool]] = []
    source_pages: list[int] = []

    for index, record in enumerate(pagination.orders):
        source_page = pagination.source_pages[index] if index < len(pagination.source_pages) else 0
        custom_rows, shipment_row, presence = _normalize_order(record, source_page=source_page)
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


async def scan_customization_candidates(
    gateway: OrderListGateway,
    processed_orders: ProcessedOrderSource | AbstractSet[str] | Iterable[str],
    *,
    filters: Mapping[str, Any] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
    limit: int = 0,
) -> CustomizationApiScanResult:
    """Build customization candidates with the confirmed 96-hour payment window."""

    pagination = await fetch_all_order_pages(
        gateway,
        filters=filters,
        page_size=page_size,
        max_pages=max_pages,
    )
    normalized = normalize_api_order_rows(pagination)
    missing = normalized.missing_fields(CUSTOMIZATION_REQUIRED_FIELDS)
    processed = _processed_order_set(processed_orders)
    debug: dict[str, Any] = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [dict(row) for row in normalized.customization_rows],
        processed,
        limit=limit,
        payment_window_hours=DEFAULT_PAYMENT_WINDOW_HOURS,
        debug=debug,
    )
    diagnostics = list(pagination.diagnostics)
    if missing:
        diagnostics.append(_missing_field_diagnostic("customization", missing))
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
    return CustomizationApiScanResult(
        state=state,
        pagination=pagination,
        row_count=len(normalized.customization_rows),
        candidate_count=len(candidates),
        processed_order_count=len(processed),
        payment_window_hours=float(DEFAULT_PAYMENT_WINDOW_HOURS),
        candidates=tuple(candidates),
        skip_counts=skip_counts,
        diagnostics=tuple(diagnostics),
    )


async def scan_shipment_candidates(
    gateway: OrderListGateway,
    queue_store: ShipmentQueueSink,
    shipment_tag_name: str,
    *,
    filters: Mapping[str, Any] | None = None,
    page_size: int = DEFAULT_API_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_API_PAGES,
    dry_run: bool = True,
    reconcile_missing: bool = True,
) -> ShipmentApiScanResult:
    """Scan shipment candidates and update the queue through transactional store calls.

    ``filters`` are forwarded exactly as supplied.  Callers are responsible for
    choosing the documented pending-review filter for their Lingxing account.
    Missing-order reconciliation is allowed only after the entire pagination
    snapshot and every critical field have been verified.
    """

    scan_started_at = utc_now()
    run_id = uuid.uuid4().hex
    pagination = await fetch_all_order_pages(
        gateway,
        filters=filters,
        page_size=page_size,
        max_pages=max_pages,
    )
    normalized = normalize_api_order_rows(pagination)
    missing = normalized.missing_fields(SHIPMENT_REQUIRED_FIELDS)
    eligible_shipment_rows = [
        dict(row)
        for row in normalized.shipment_rows
        if classify_recent_payment_window(
            f"付款时间 {str(row.get('paid_at_text') or '').strip()}",
            hours=DEFAULT_PAYMENT_WINDOW_HOURS,
        )
        == "recent"
    ]
    report = build_shipment_scan_report(
        eligible_shipment_rows,
        shipment_tag_name,
        dry_run=dry_run,
        queue_path=str(getattr(queue_store, "path", "") or ""),
    )
    snapshot_complete = pagination.complete and not missing
    report.table_total_count = pagination.expected_total
    report.scan_complete = snapshot_complete
    report.incomplete_field_count = len(missing)
    diagnostics = list(pagination.diagnostics)
    if missing:
        diagnostics.append(_missing_field_diagnostic("shipment", missing))
    excluded_payment_rows = len(normalized.shipment_rows) - len(eligible_shipment_rows)
    if excluded_payment_rows:
        diagnostics.append(
            ApiScanDiagnostic(
                code="shipment_outside_96h_payment_window",
                message="已排除付款时间不在最近 96 小时内的自动标发订单。",
                affected_count=excluded_payment_rows,
            )
        )
    if not snapshot_complete and report.status != "config_missing":
        report.status = "incomplete"
        report.message = "API 待审核快照不完整，已禁止缺失订单的人工完成判定。"

    queue_failed = False
    if not dry_run and report.status != "config_missing":
        queue_results: list[QueueInsertResult] = []
        try:
            for candidate in report.candidates:
                # ShipmentQueueStore.upsert_candidate uses BEGIN IMMEDIATE and
                # commits each complete candidate state transition atomically.
                queue_results.append(queue_store.upsert_candidate(candidate, run_id=run_id))
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

        if snapshot_complete and reconcile_missing and not queue_failed:
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

    if queue_failed or pagination.state is ApiScanState.FAILED:
        state = ApiScanState.FAILED
    elif not snapshot_complete or report.status == "config_missing":
        state = ApiScanState.INCOMPLETE
    else:
        state = ApiScanState.COMPLETE
    if queue_failed:
        report.status = "failed"
        report.message = "发货队列更新未完整成功，已停止后续结案。"

    safe_report = _safe_shipment_report(report)
    return ShipmentApiScanResult(
        state=state,
        pagination=pagination,
        row_count=len(normalized.shipment_rows),
        tagged_row_count=report.tagged_row_count,
        candidate_count=len(report.candidates),
        enqueued_count=report.enqueued_count,
        manual_completed_count=report.manual_completed_count,
        missing_critical_field_count=len(missing),
        report=safe_report,
        diagnostics=tuple(diagnostics),
    )


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


def _normalize_order(
    record: OrderRecord,
    *,
    source_page: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], Mapping[str, bool]]:
    payload = dict(record.payload)
    mappings = _mapping_tree(payload)

    system_present, system_value = _lookup(mappings, _SYSTEM_ALIASES)
    platform_present, platform_value = _lookup(mappings, _PLATFORM_ALIASES)
    paid_present, paid_value = _lookup(mappings, _PAID_AT_ALIASES)
    tag_present, tag_value = _lookup(mappings, _TAG_ALIASES)
    remark_present, remark_value = _lookup(mappings, _CUSTOMER_REMARK_ALIASES)
    status_present, status_value = _lookup(mappings, _STATUS_ALIASES)
    logistics_present, logistics_value = _lookup(mappings, _LOGISTICS_ALIASES)

    system_order_no = _optional_text(record.global_order_no) or _optional_text(system_value) or ""
    platform_order_no = _optional_text(record.order_number) or _optional_text(platform_value) or ""
    system_present = bool(system_order_no) and (system_present or bool(record.global_order_no))
    platform_present = bool(platform_order_no) and (platform_present or bool(record.order_number))
    paid_at_text = _format_datetime(paid_value)
    tag_text = _structured_text(tag_value)
    customer_remark = _structured_text(remark_value)
    status_text = _structured_text(status_value)
    logistics = _structured_text(logistics_value)

    items_present, raw_items = _find_item_list(mappings)
    if not items_present:
        top_product_present, _ = _lookup(mappings, (*_ASIN_ALIASES, *_SKU_ALIASES))
        items_present = top_product_present
    item_mappings = [item for item in raw_items if isinstance(item, Mapping)]
    if not item_mappings:
        item_mappings = [{}]

    customization_rows: list[dict[str, Any]] = []
    all_skus: list[str] = []
    all_asins: list[str] = []
    for raw_item in item_mappings:
        item_tree = _mapping_tree(dict(raw_item)) + mappings
        _, asin_value = _lookup(item_tree, _ASIN_ALIASES)
        _, sku_value = _lookup(item_tree, _SKU_ALIASES)
        _, quantity_value = _lookup(item_tree, _QUANTITY_ALIASES)
        asin = _optional_text(asin_value) or ""
        sku = _optional_text(sku_value) or ""
        quantity = _positive_int(quantity_value)
        if asin and asin not in all_asins:
            all_asins.append(asin)
        if sku and sku not in all_skus:
            all_skus.append(sku)
        asin_text = _with_quantity(asin, quantity)
        sku_text = _with_quantity(sku, quantity)
        row_text = _safe_business_row_text(
            system_order_no,
            platform_order_no,
            paid_at_text,
            asin_text,
            sku_text,
            logistics,
            status_text,
            tag_text,
        )
        customization_rows.append(
            {
                "system_order_no": system_order_no,
                "platform_order_no": platform_order_no,
                "row_text": row_text,
                "asin_text": asin_text,
                "asin": asin,
                "sku": sku_text,
                "status_text": status_text,
                "tag_text": tag_text,
                "paid_at_text": paid_at_text,
                "logistics": logistics,
                "source_page": source_page,
                "source_scroll_top": 0,
            }
        )

    shipment_row = {
        "system_order_no": system_order_no,
        "platform_order_no": platform_order_no,
        "rowid": system_order_no,
        "row_text": _safe_business_row_text(
            system_order_no,
            platform_order_no,
            paid_at_text,
            " ".join(all_asins),
            " | ".join(all_skus),
            logistics,
            status_text,
            tag_text,
        ),
        "asin_text": " ".join(all_asins),
        "asin": all_asins[0] if all_asins else "",
        "sku": " | ".join(all_skus),
        "status_text": status_text,
        "tag_text": tag_text,
        "customer_remark": customer_remark,
        "paid_at_text": paid_at_text,
        "logistics": logistics,
        "source_page": source_page,
        "source_scroll_top": 0,
        "field_presence": {
            "system": system_present,
            "platform": platform_present,
            "tag": tag_present,
            "customer_remark": remark_present,
        },
    }
    presence = {
        "system": system_present,
        "platform": platform_present,
        "paid_at": bool(paid_present and paid_at_text),
        "tag": tag_present,
        "customer_remark": remark_present,
        "items": items_present,
        "status": status_present,
        "logistics": logistics_present,
    }
    return customization_rows, shipment_row, presence


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


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
    "amazon_order_id",
    "amazonOrderId",
    "source_order_no",
    "sourceOrderNo",
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
_TAG_ALIASES = (
    "tag_text",
    "tagText",
    "tag_name",
    "tagName",
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
    "logistics",
    "logistics_name",
    "logisticsName",
    "logistics_type_name",
    "logisticsTypeName",
    "shipping_service",
    "shippingService",
    "ship_service_level",
    "shipServiceLevel",
)
_ITEM_LIST_ALIASES = (
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
    "qty",
    "item_quantity",
    "itemQuantity",
    "order_quantity",
    "orderQuantity",
)


__all__ = [
    "ApiPageTrace",
    "ApiScanDiagnostic",
    "ApiScanState",
    "CustomizationApiScanResult",
    "MissingFieldNotice",
    "NormalizedOrderRows",
    "OrderPaginationResult",
    "ShipmentApiScanResult",
    "fetch_all_order_pages",
    "normalize_api_order_rows",
    "redact_sensitive_payload",
    "redact_sensitive_text",
    "scan_customization_candidates",
    "scan_shipment_candidates",
]
