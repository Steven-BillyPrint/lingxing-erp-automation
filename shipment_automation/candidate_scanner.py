from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .models import (
    DuplicateShipmentItem,
    ManualReviewItem,
    ShipmentCandidate,
    ShipmentScanReport,
)
from .queue_store import QueueInsertResult


ALS_RE = re.compile(r"ALS\s*(\d{11,})", re.I)
INVALID_ALS_CONTEXT_WORDS = ("低申报作废", "附加费作废", "作废", "取消", "无效")


@dataclass
class AlsExtractionResult:
    selected_als_no: str | None
    valid_als_numbers: list[str]
    excluded_als_numbers: list[str]
    duplicate_als_numbers: list[str]
    truncated_als_numbers: list[str]
    warnings: list[str]

    @property
    def needs_manual_review(self) -> bool:
        return bool(self.warnings)


def normalized_shipment_tag(tag_name: str | None) -> str:
    return str(tag_name or "").strip()


def row_has_shipment_tag(tag_text: str | None, shipment_tag_name: str | None) -> bool:
    tag = normalized_shipment_tag(shipment_tag_name)
    if not tag:
        return False
    return tag in str(tag_text or "")


def _context_has_invalid_word(text: str, start: int, end: int) -> bool:
    left = max(text.rfind(separator, 0, start) for separator in "。．.;；,，\n\r") + 1
    right_candidates = [text.find(separator, end) for separator in "。．.;；,，\n\r"]
    right_positions = [position for position in right_candidates if position >= 0]
    right = min(right_positions) if right_positions else len(text)
    context = text[left:right]
    return any(word in context for word in INVALID_ALS_CONTEXT_WORDS)


def _dedupe_keep_order(values: list[str]) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    unique: list[str] = []
    duplicates: list[str] = []
    duplicate_seen: set[str] = set()
    for value in values:
        if value in seen:
            if value not in duplicate_seen:
                duplicate_seen.add(value)
                duplicates.append(value)
            continue
        seen.add(value)
        unique.append(value)
    return unique, duplicates


def extract_als_from_remark(customer_remark: str | None) -> AlsExtractionResult:
    """Extract the first usable ALS number from customer remark text."""

    text = str(customer_remark or "")
    valid_occurrences: list[str] = []
    excluded: list[str] = []
    truncated: list[str] = []
    for match in ALS_RE.finditer(text):
        digits = match.group(1)
        als_no = f"ALS{digits[:11]}"
        if len(digits) > 11:
            truncated.append(als_no)
        if _context_has_invalid_word(text, match.start(), match.end()):
            excluded.append(als_no)
            continue
        valid_occurrences.append(als_no)

    valid_unique, duplicates = _dedupe_keep_order(valid_occurrences)
    warnings: list[str] = []
    if truncated:
        warnings.append(f"物流单号疑似粘连，已按 11 位数字截断：{', '.join(dict.fromkeys(truncated))}")
    if duplicates:
        warnings.append(f"客服备注中重复出现物流单号，已只保留一次：{', '.join(duplicates)}")
    if len(valid_unique) > 1:
        warnings.append(
            f"客服备注出现多个有效物流单号，已优先使用第一个：{valid_unique[0]}；其它：{', '.join(valid_unique[1:])}"
        )
    if excluded:
        warnings.append(f"已排除作废/取消/无效上下文中的物流单号：{', '.join(dict.fromkeys(excluded))}")

    return AlsExtractionResult(
        selected_als_no=valid_unique[0] if valid_unique else None,
        valid_als_numbers=valid_unique,
        excluded_als_numbers=list(dict.fromkeys(excluded)),
        duplicate_als_numbers=duplicates,
        truncated_als_numbers=list(dict.fromkeys(truncated)),
        warnings=warnings,
    )


def build_shipment_scan_report(
    rows: list[dict[str, Any]],
    shipment_tag_name: str,
    *,
    dry_run: bool = True,
    queue_path: str = "",
) -> ShipmentScanReport:
    tag_name = normalized_shipment_tag(shipment_tag_name)
    report = ShipmentScanReport(
        status="completed",
        shipment_tag_name=tag_name,
        queue_path=queue_path,
        dry_run=dry_run,
        scanned_row_count=len(rows),
    )
    if not tag_name:
        report.status = "config_missing"
        report.message = "未配置专属发货标签。"
        return report

    for row in rows:
        tag_text = str(row.get("tag_text") or "").strip()
        if not row_has_shipment_tag(tag_text, tag_name):
            continue
        report.tagged_row_count += 1
        system_order_no = str(row.get("system_order_no") or row.get("rowid") or "").strip()
        platform_order_no = str(row.get("platform_order_no") or "").strip()
        customer_remark = str(row.get("customer_remark") or "").strip()
        extraction = extract_als_from_remark(customer_remark)
        if not extraction.selected_als_no:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    reason="missing_valid_als",
                    als_numbers=extraction.excluded_als_numbers,
                    message="命中专属发货标签，但客服备注中没有可入队的有效物流单号。",
                )
            )
            continue

        report.valid_als_row_count += 1
        candidate = ShipmentCandidate(
            system_order_no=system_order_no,
            platform_order_no=platform_order_no,
            als_no=extraction.selected_als_no,
            shipment_tag_name=tag_name,
            tag_text=tag_text,
            sku_text=str(row.get("sku") or row.get("sku_text") or "").strip(),
            customer_remark=customer_remark,
            status_text=str(row.get("status_text") or "").strip(),
            source_page=_int_or_none(row.get("source_page")),
            source_scroll_top=_int_or_none(row.get("source_scroll_top")),
            rowid=str(row.get("rowid") or "").strip() or None,
            warnings=extraction.warnings,
        )
        report.candidates.append(candidate)
        if extraction.needs_manual_review:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    reason="als_review",
                    als_numbers=extraction.valid_als_numbers,
                    selected_als_no=extraction.selected_als_no,
                    message="；".join(extraction.warnings),
                )
            )

    report.manual_review_count = len(report.manual_reviews)
    report.message = "扫描完成。"
    return report


def apply_queue_results(report: ShipmentScanReport, results: list[QueueInsertResult]) -> ShipmentScanReport:
    for result in results:
        candidate = result.candidate
        if result.inserted:
            report.enqueued_count += 1
            report.enqueued_candidates.append(candidate)
            continue
        existing = result.existing or {}
        report.duplicate_skipped_count += 1
        report.duplicate_skipped.append(
            DuplicateShipmentItem(
                system_order_no=candidate.system_order_no,
                platform_order_no=candidate.platform_order_no,
                als_no=candidate.als_no,
                existing_system_order_no=existing.get("system_order_no"),
                existing_platform_order_no=existing.get("platform_order_no"),
                existing_queue_status=existing.get("queue_status"),
                existing_last_error=existing.get("last_error"),
            )
        )
    return report


def report_to_dict(report: ShipmentScanReport) -> dict[str, Any]:
    return asdict(report)


def _compact_report_for_log(report: ShipmentScanReport) -> dict[str, Any]:
    data = report_to_dict(report)
    for key in ("candidates", "enqueued_candidates"):
        data[key] = [
            {
                "system_order_no": item.get("system_order_no"),
                "platform_order_no": item.get("platform_order_no"),
                "als_no": item.get("als_no"),
                "sku_text": item.get("sku_text"),
                "warnings": item.get("warnings") or [],
            }
            for item in data.get(key, [])
        ]
    return data


def _int_or_none(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed
