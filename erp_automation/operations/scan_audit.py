"""Safe, structured, per-task audit files for API order scans.

The module deliberately accepts summaries rather than HTTP request/response
objects. Every section is reduced through an allow-list before serialization,
so accidentally passing a raw Lingxing payload cannot turn the audit file into
a second store for credentials or opaque response bodies. Business diagnostic
values selected by the workflow are retained verbatim.

Typical integration::

    writer = ScanAuditWriter(workspace / "logs")
    result = writer.write(
        task_id=task_id,
        scan_kind="customization",
        started_at=scan_started_at,
        query=query_filters,
        pages=page_traces,
        order_decisions=order_decisions,
        summary=summary,
        error=error,
    )

``result.path`` is safe to show in the desktop task details.  Custom-order and
shipment scans are deliberately separated and named with their local start
time, for example ``logs/custom_order_scan/YYYY-MM-DD/`` and
``logs/shipment_scan/YYYY-MM-DD/``.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from traceback import TracebackException
from typing import Any
from uuid import uuid4


SCAN_AUDIT_SCHEMA = "erp-automation.scan-audit"
SCAN_AUDIT_VERSION = 2
# Kept for reading audit files written by releases before the two scan queues
# received separate directories. New writes use SCAN_AUDIT_DIRECTORIES.
SCAN_AUDIT_DIRECTORY = "api_scan"
SCAN_AUDIT_DIRECTORIES = {
    "customization": "custom_order_scan",
    "shipment": "shipment_scan",
}
SCAN_AUDIT_FILENAME_PREFIXES = {
    "customization": "custom_order_scan",
    "shipment": "shipment_scan",
}

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCAN_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SAFE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,159}$")
_TRUSTED_OPERATOR_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@billyprint\.com$",
    re.IGNORECASE,
)
_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s?]+)\?[^\s]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|token|app[_-]?secret|secret|"
    r"password|authorization|cookie|sign(?:ature)?)\s*[-:=：]\s*[^\s,;；&}\]]+"
)

_QUERY_ALIASES = {
    "datetype": "date_type",
    "starttime": "start_time",
    "endtime": "end_time",
    "platformcode": "platform_code",
    "orderstatus": "order_status",
    "includedelete": "include_delete",
    "offset": "offset",
    "length": "length",
    "pagesize": "page_size",
    "limit": "limit",
    "sellerid": "seller_id",
    "shopid": "shop_id",
    "fulfillmenttype": "fulfillment_type",
}

_PAGE_ALIASES = {
    "windownumber": "window_number",
    "pagenumber": "page_number",
    "page": "page_number",
    "offset": "offset",
    "requestedlength": "requested_length",
    "length": "requested_length",
    "itemcount": "item_count",
    "returnedcount": "item_count",
    "declaredtotal": "declared_total",
    "total": "declared_total",
    "requestid": "request_id",
    "apicode": "api_code",
    "httpstatus": "http_status",
    "responsetime": "response_time",
    "durationms": "duration_ms",
    "retrycount": "retry_count",
    "outcome": "outcome",
    "state": "outcome",
    "errorid": "error_id",
    "errortype": "error_type",
}

_ORDER_ALIASES = {
    "platformorderno": "platform_order_no",
    "systemorderno": "system_order_no",
    "systemordernos": "system_order_nos",
    "sourcepage": "source_page",
    "paidat": "paid_at",
    "paidattext": "paid_at",
    "paymentstatus": "payment_status",
    "paymentagehours": "payment_age_hours",
    "decision": "decision",
    "outcome": "decision",
    "reason": "reason",
    "reasoncode": "reason_code",
    "hit": "matched",
    "matched": "matched",
    "matchedasin": "matched_asin",
    "matchedasins": "matched_asins",
    "unknownasins": "unknown_asins",
    "parentasin": "parent_asin",
    "producttype": "product_type",
    "tagmatched": "tag_matched",
    "customtagtext": "custom_tag_text",
    "tagtext": "custom_tag_text",
    "issplitorder": "is_split_order",
    "processedbefore": "processed_before",
    "duplicate": "duplicate",
    "logisticsno": "logistics_no",
    "internationaltrackingno": "international_tracking_no",
    "carrier": "carrier",
    "alibabastatus": "alibaba_status",
    "logisticsstate": "logistics_state",
    "items": "items",
    "warningcodes": "warning_codes",
    "diagnosticcodes": "diagnostic_codes",
    "missingfields": "missing_fields",
    "errorid": "error_id",
    "salesrevenuetotal": "sales_revenue_total",
    "salesrevenuecurrency": "sales_revenue_currency",
    "salesrevenuestatus": "sales_revenue_status",
    "salesrevenuesource": "sales_revenue_source",
}

_ITEM_ALIASES = {
    "orderitemid": "order_item_id",
    "itemid": "order_item_id",
    "asin": "asin",
    "sku": "sku",
    "msku": "msku",
    "localsku": "local_sku",
    "quantityraw": "quantity_raw",
    "rawquantity": "quantity_raw",
    "quantity": "quantity",
    "quantitynormalized": "quantity_normalized",
    "quantitystatus": "quantity_status",
    "salesrevenue": "sales_revenue",
    "salesrevenuecurrency": "sales_revenue_currency",
    "salesrevenuestatus": "sales_revenue_status",
    "ordertotal": "order_total",
    "ordertotalcurrency": "order_total_currency",
    "ordertotalstatus": "order_total_status",
}

_SUMMARY_SCALARS = {
    "status",
    "state",
    "complete",
    "payment_window_hours",
    "row_count",
    "evaluable_row_count",
    "eligible_row_count",
    "order_count",
    "deduplicated_order_count",
    "candidate_count",
    "processed_order_count",
    "tagged_row_count",
    "enqueued_count",
    "manual_review_count",
    "manual_completed_count",
    "missing_critical_field_count",
    "duplicate_count",
    "refreshed_count",
    "queue_total_count",
    "window_count",
    "scan_start_time",
    "scan_end_time",
    "auto_paused_count",
    "auto_resumed_count",
    "immediate_logistics_count",
    "immediate_erp_count",
    "email_preview_backfill_count",
    "receiver_email_backfill_count",
    "receiver_email_unresolved_count",
    "refreshed_count",
    "queue_total_count",
    "excluded_count",
    "failed_count",
    "pages_read",
    "page_count",
    "order_decision_count",
    "expected_total",
    "buyer_cancel_detected_count",
    "buyer_cancel_reconciled_count",
    "buyer_cancel_clear_observed_count",
    "buyer_cancel_reactivated_count",
    "buyer_cancel_clear_reset_count",
    "buyer_cancel_snapshot_state",
    "missing_candidate_count",
    "folder_reconciled_completed_count",
    "folder_reconciled_pending_count",
    "folder_reconciliation_error_preserved_count",
    "folder_reconciliation_changed_count",
    "folder_reconciliation_state",
    "logistics_query_count",
    "logistics_parsed_count",
    "logistics_ready_count",
    "logistics_waiting_count",
    "logistics_blocked_count",
    "logistics_retryable_count",
    "ready_to_mark_count",
    "alibaba_logistics_execution",
}
_SUMMARY_COUNT_MAPS = {"skip_counts", "reason_counts", "decision_counts"}
_SUMMARY_CODE_LISTS = {"diagnostic_codes", "warning_codes"}

_IDENTIFIER_FIELDS = {
    "task_id",
    "error_id",
    "request_id",
    "platform_order_no",
    "system_order_no",
    "system_order_nos",
    "order_item_id",
    "logistics_no",
    "international_tracking_no",
    "carrier",
    "asin",
    "matched_asin",
    "matched_asins",
    "unknown_asins",
    "parent_asin",
    "sku",
    "msku",
    "local_sku",
}


class ScanAuditError(RuntimeError):
    """Base class for scan-audit validation and persistence errors."""


class UnsafeScanAuditPathError(ScanAuditError):
    """The configured log path could escape or traverse a link/reparse point."""


@dataclass(frozen=True)
class ScanAuditWriteResult:
    """Small integration result that never retains the full audit document."""

    path: Path
    task_id: str
    error_id: str | None = None


def _canonical_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError("扫描审计的分页、订单和汇总项必须是 Mapping 或 dataclass。")


def _sequence(value: object, *, label: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    raise TypeError(f"{label} 必须是序列。")


def _truncate(value: str, limit: int = 500) -> str:
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


def redact_audit_text(value: object, *, redact_phone: bool = True) -> str:
    """Preserve business diagnostics while filtering authentication secrets.

    ``redact_phone`` remains as a compatibility argument. Phone numbers,
    e-mail addresses, names, and addresses are intentionally retained.
    """

    del redact_phone
    text = _truncate(str(value or "").replace("\x00", ""))
    text = _URL_QUERY_RE.sub(r"\1?<redacted-query>", text)
    text = _BEARER_RE.sub("Bearer <redacted-secret>", text)
    text = _LABELED_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text


def _safe_number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _safe_scalar(value: object, *, field: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    number = _safe_number(value)
    if number is not None:
        return number
    if isinstance(value, datetime):
        return _timestamp(value, field)
    if isinstance(value, str):
        return redact_audit_text(value, redact_phone=field not in _IDENTIFIER_FIELDS)
    return f"<unsupported-{type(value).__name__}>"


def _safe_code(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _SAFE_CODE_RE.fullmatch(text):
        return "invalid_code"
    return text


def _safe_identifier_list(value: object, *, field: str) -> list[str]:
    output: list[str] = []
    for item in _sequence(value, label=field):
        text = redact_audit_text(item, redact_phone=False).strip()
        if text:
            output.append(text)
    return output


def _safe_query_value(value: object, *, field: str) -> Any:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_scalar(item, field=field) for item in value]
    return _safe_scalar(value, field=field)


def safe_query_summary(query: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only documented, non-authentication order-query fields."""

    output: dict[str, Any] = {}
    for raw_key, value in (query or {}).items():
        field = _QUERY_ALIASES.get(_canonical_key(raw_key))
        if field is None:
            continue
        output[field] = _safe_query_value(value, field=field)
    return output


def _safe_page(value: object) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for raw_key, item in _mapping(value).items():
        field = _PAGE_ALIASES.get(_canonical_key(raw_key))
        if field is None:
            continue
        output[field] = _safe_scalar(item, field=field)
    return output


def _safe_quantity_raw(value: object) -> int | float | str | None:
    if value is None:
        return None
    number = _safe_number(value)
    if number is not None:
        return number
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
        return value.strip()
    return f"<invalid-{type(value).__name__}>"


def _safe_item(value: object) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for raw_key, item in _mapping(value).items():
        field = _ITEM_ALIASES.get(_canonical_key(raw_key))
        if field is None:
            continue
        if field == "quantity_raw":
            output[field] = _safe_quantity_raw(item)
        elif field in {"quantity", "quantity_normalized"}:
            output[field] = _safe_number(item)
        elif field == "quantity_status":
            output[field] = _safe_code(item)
        else:
            output[field] = _safe_scalar(item, field=field)
    return output


def _safe_order_decision(value: object) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for raw_key, item in _mapping(value).items():
        field = _ORDER_ALIASES.get(_canonical_key(raw_key))
        if field is None:
            continue
        if field == "items":
            output[field] = [_safe_item(entry) for entry in _sequence(item, label="items")]
        elif field in {
            "system_order_nos",
            "matched_asins",
            "unknown_asins",
            "warning_codes",
            "diagnostic_codes",
            "missing_fields",
        }:
            output[field] = _safe_identifier_list(item, field=field)
        elif field in {
            "decision",
            "reason_code",
            "payment_status",
            "product_type",
            "logistics_state",
        }:
            output[field] = _safe_code(item)
        elif field == "reason":
            output[field] = redact_audit_text(item)
        else:
            output[field] = _safe_scalar(item, field=field)
    return output


def _safe_count_mapping(value: object) -> dict[str, int]:
    output: dict[str, int] = {}
    for raw_key, raw_value in _mapping(value).items():
        key = _safe_code(raw_key)
        number = _safe_number(raw_value)
        if key is not None and isinstance(number, int):
            output[key] = number
    return output


def _safe_summary(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for raw_key, value in (summary or {}).items():
        field = str(raw_key).strip().casefold()
        if field in _SUMMARY_SCALARS:
            output[field] = _safe_scalar(value, field=field)
        elif field in _SUMMARY_COUNT_MAPS:
            output[field] = _safe_count_mapping(value)
        elif field in _SUMMARY_CODE_LISTS:
            output[field] = _safe_identifier_list(value, field=field)
    return output


def _timestamp(value: datetime | str, label: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} 不是有效 ISO 时间。") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{label} 必须是 datetime 或 ISO 时间字符串。")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} 必须包含时区。")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def scan_audit_directory_name(scan_kind: str) -> str:
    """Return the dedicated directory for one validated scan kind."""

    normalized = str(scan_kind or "")
    if not _SCAN_KIND_RE.fullmatch(normalized):
        raise ValueError("scan_kind 格式无效。")
    return SCAN_AUDIT_DIRECTORIES.get(normalized, f"{normalized}_scan")


def scan_audit_filename_prefix(scan_kind: str) -> str:
    normalized = str(scan_kind or "")
    if not _SCAN_KIND_RE.fullmatch(normalized):
        raise ValueError("scan_kind 格式无效。")
    return SCAN_AUDIT_FILENAME_PREFIXES.get(normalized, f"{normalized}_scan")


def _traceback_frames(traceback_exception: TracebackException) -> list[dict[str, Any]]:
    # Frame source text and locals are intentionally omitted: either can contain
    # request bodies or credentials embedded in a failing expression.
    return [
        {
            "file": redact_audit_text(frame.filename),
            "line": int(frame.lineno),
            "function": redact_audit_text(frame.name, redact_phone=False),
        }
        for frame in traceback_exception.stack
    ]


def _traceback_chain(traceback_exception: TracebackException) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    current: TracebackException | None = traceback_exception
    relation = "exception"
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # ``exc_type`` is deprecated in Python 3.13; ``exc_type_str`` is the
        # already-normalized public representation and works for chained
        # exceptions too.
        exception_type = current.exc_type_str or "Exception"
        output.append(
            {
                "relation": relation,
                "exception_type": redact_audit_text(exception_type, redact_phone=False),
                "frames": _traceback_frames(current),
                "message": redact_audit_text(
                    " ".join(current.format_exception_only()).strip(),
                    redact_phone=False,
                ),
            }
        )
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            relation = "context"
        else:
            current = None
    return output


def safe_exception_summary(error: BaseException, *, error_id: str | None = None) -> dict[str, Any]:
    """Capture a traceback without locals while retaining useful error text."""

    if not isinstance(error, BaseException):
        raise TypeError("error 必须是 BaseException。")
    normalized_error_id = error_id or uuid4().hex
    if not _TASK_ID_RE.fullmatch(normalized_error_id):
        raise ValueError("error_id 格式无效。")
    captured = TracebackException.from_exception(error, capture_locals=False)
    output: dict[str, Any] = {
        "error_id": normalized_error_id,
        "exception_type": type(error).__name__,
        "traceback": _traceback_chain(captured),
    }
    safe_attributes = (
        ("request_id", "request_id"),
        ("code", "api_code"),
        ("status_code", "http_status"),
        ("operation", "operation"),
    )
    for attribute, field in safe_attributes:
        value = getattr(error, attribute, None)
        if value is not None:
            output[field] = _safe_scalar(value, field=field)
    return output


def build_scan_audit_document(
    *,
    task_id: str,
    scan_kind: str,
    started_at: datetime | str,
    finished_at: datetime | str | None = None,
    query: Mapping[str, Any] | None = None,
    pages: Sequence[object] = (),
    order_decisions: Sequence[object] = (),
    summary: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
    operator_name: str = "",
    operator_email: str = "",
) -> dict[str, Any]:
    """Build the complete diagnostic document without touching the filesystem."""

    if not _TASK_ID_RE.fullmatch(str(task_id or "")) or task_id in {".", ".."}:
        raise ValueError("task_id 只能包含安全的文件名字符。")
    if not _SCAN_KIND_RE.fullmatch(str(scan_kind or "")):
        raise ValueError("scan_kind 格式无效。")
    started_text = _timestamp(started_at, "started_at")
    finished_text = _timestamp(finished_at or datetime.now(timezone.utc), "finished_at")
    started_value = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
    finished_value = datetime.fromisoformat(finished_text.replace("Z", "+00:00"))
    if finished_value < started_value:
        raise ValueError("finished_at 不能早于 started_at。")

    safe_pages = [_safe_page(value) for value in _sequence(pages, label="pages")]
    safe_decisions = [
        _safe_order_decision(value)
        for value in _sequence(order_decisions, label="order_decisions")
    ]
    safe_summary = _safe_summary(summary)
    safe_summary.setdefault("page_count", len(safe_pages))
    safe_summary.setdefault("order_decision_count", len(safe_decisions))
    safe_error = safe_exception_summary(error) if error is not None else None
    normalized_operator_email = str(operator_email or "").strip().casefold()
    if not _TRUSTED_OPERATOR_EMAIL_RE.fullmatch(normalized_operator_email):
        normalized_operator_email = ""
    normalized_operator_name = (
        _truncate(redact_audit_text(operator_name, redact_phone=False), 200)
        if normalized_operator_email
        else ""
    )

    document: dict[str, Any] = {
        "schema": SCAN_AUDIT_SCHEMA,
        "version": SCAN_AUDIT_VERSION,
        "task_id": task_id,
        "scan_kind": scan_kind,
        "started_at": started_text,
        "finished_at": finished_text,
        "operator": {
            "name": normalized_operator_name,
            "email": normalized_operator_email,
        },
        "query_summary": safe_query_summary(query),
        "pagination": {
            "page_count": len(safe_pages),
            "pages": safe_pages,
        },
        "order_decisions": safe_decisions,
        "summary": safe_summary,
        "error_id": safe_error["error_id"] if safe_error is not None else None,
    }
    if safe_error is not None:
        document["error"] = safe_error
    return document


def _absolute_without_resolving(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if not _lexists(path):
        return False
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(reparse_flag and attributes & reparse_flag)


def _existing_path_chain(path: Path) -> list[Path]:
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current == current.parent:
            break
        current = current.parent
    return list(reversed(chain))


def _reject_link_traversal(path: Path) -> None:
    for component in _existing_path_chain(path):
        if _lexists(component) and _is_link_or_reparse(component):
            raise UnsafeScanAuditPathError(
                f"扫描审计路径不允许经过符号链接或重解析点：{component}"
            )


def _ensure_plain_directory(path: Path) -> None:
    _reject_link_traversal(path)
    if _lexists(path):
        if not path.is_dir():
            raise UnsafeScanAuditPathError(f"扫描审计目录不是普通目录：{path}")
    else:
        path.mkdir(parents=True, exist_ok=False)
    _reject_link_traversal(path)


def _confined(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise UnsafeScanAuditPathError("扫描审计目标路径越出固定日志根目录。") from exc


def _atomic_write(path: Path, data: bytes, *, root: Path) -> None:
    if _lexists(path):
        if _is_link_or_reparse(path) or not path.is_file():
            raise UnsafeScanAuditPathError(f"扫描审计目标不是普通文件：{path}")
    _reject_link_traversal(path.parent)
    _confined(path, root)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    _confined(temporary, root)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_link_traversal(path.parent)
        _confined(temporary, root)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _archive_existing_audit(path: Path, *, root: Path) -> None:
    """Preserve an earlier retry attempt before replacing the latest document."""

    if not _lexists(path):
        return
    if _is_link_or_reparse(path) or not path.is_file():
        raise UnsafeScanAuditPathError(f"扫描审计目标不是普通文件：{path}")
    previous = path.read_bytes()
    for attempt in range(1, 10_000):
        archive = path.with_name(f"{path.stem}.attempt-{attempt:03d}{path.suffix}")
        if _lexists(archive):
            continue
        _atomic_write(archive, previous, root=root)
        return
    raise ScanAuditError("同一任务的扫描审计重试次数超过安全上限。")


class ScanAuditWriter:
    """Write one allow-listed audit document below a fixed log root."""

    def __init__(self, log_root: str | os.PathLike[str]) -> None:
        self.log_root = _absolute_without_resolving(log_root)

    def write(
        self,
        *,
        task_id: str,
        scan_kind: str,
        started_at: datetime | str,
        finished_at: datetime | str | None = None,
        query: Mapping[str, Any] | None = None,
        pages: Sequence[object] = (),
        order_decisions: Sequence[object] = (),
        summary: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
        operator_name: str = "",
        operator_email: str = "",
    ) -> ScanAuditWriteResult:
        document = build_scan_audit_document(
            task_id=task_id,
            scan_kind=scan_kind,
            started_at=started_at,
            finished_at=finished_at,
            query=query,
            pages=pages,
            order_decisions=order_decisions,
            summary=summary,
            error=error,
            operator_name=operator_name,
            operator_email=operator_email,
        )
        started_utc = datetime.fromisoformat(
            document["started_at"].replace("Z", "+00:00")
        )
        started_local = started_utc.astimezone()
        date_directory = started_local.strftime("%Y-%m-%d")
        timestamp = started_local.strftime("%Y%m%d_%H%M%S")
        directory_name = scan_audit_directory_name(document["scan_kind"])
        filename_prefix = scan_audit_filename_prefix(document["scan_kind"])
        _ensure_plain_directory(self.log_root)
        audit_root = self.log_root / directory_name
        _ensure_plain_directory(audit_root)
        daily_root = audit_root / date_directory
        _ensure_plain_directory(daily_root)
        _confined(daily_root, self.log_root)
        destination = daily_root / f"{filename_prefix}_{timestamp}_{task_id}.json"
        encoded = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        _archive_existing_audit(destination, root=self.log_root)
        _atomic_write(destination, encoded, root=self.log_root)
        return ScanAuditWriteResult(
            path=destination,
            task_id=task_id,
            error_id=document.get("error_id"),
        )


def write_scan_audit(
    log_root: str | os.PathLike[str],
    **kwargs: Any,
) -> ScanAuditWriteResult:
    """Convenience wrapper for callers that do not need to retain a writer."""

    return ScanAuditWriter(log_root).write(**kwargs)


__all__ = [
    "SCAN_AUDIT_DIRECTORY",
    "SCAN_AUDIT_DIRECTORIES",
    "SCAN_AUDIT_FILENAME_PREFIXES",
    "SCAN_AUDIT_SCHEMA",
    "SCAN_AUDIT_VERSION",
    "ScanAuditError",
    "ScanAuditWriteResult",
    "ScanAuditWriter",
    "UnsafeScanAuditPathError",
    "build_scan_audit_document",
    "redact_audit_text",
    "safe_exception_summary",
    "safe_query_summary",
    "scan_audit_directory_name",
    "scan_audit_filename_prefix",
    "write_scan_audit",
]
