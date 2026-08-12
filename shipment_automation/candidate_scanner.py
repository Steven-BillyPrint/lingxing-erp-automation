from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from lingxing_automation.products.catalog import extract_asins, match_supported_product

from .models import (
    DuplicateShipmentItem,
    ManualReviewItem,
    ShipmentCandidate,
    ShipmentScanReport,
    normalize_customer_shipping_service,
)
from .queue_store import QueueInsertResult


LOGISTICS_NO_RE = re.compile(r"ALS\s*(\d{11,})", re.I)
INVALID_LOGISTICS_CONTEXT_WORDS = ("低申报作废", "附加费作废", "作废", "取消", "无效")


def _shipment_product_types(row: dict[str, Any]) -> str:
    product_types: list[str] = []
    source = str(row.get("asin_text") or row.get("asin") or "")
    for asin in extract_asins(source):
        match = match_supported_product(asin)
        if match is not None and match.product_type not in product_types:
            product_types.append(match.product_type)
    return " | ".join(product_types)


@dataclass
class LogisticsNumberExtractionResult:
    selected_logistics_no: str | None
    valid_logistics_numbers: list[str]
    excluded_logistics_numbers: list[str]
    duplicate_logistics_numbers: list[str]
    truncated_logistics_numbers: list[str]
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
    # Treat tags as labels, not arbitrary substrings.  Substring matching can
    # incorrectly admit labels such as "非帐篷标发" or "帐篷标发已取消".
    # Current API normalization joins labels with ``|``; whitespace and common
    # punctuation remain supported for legacy browser snapshots.
    labels = {
        value.strip()
        for value in re.split(r"\s*[|,，;；]\s*", str(tag_text or "").strip())
        if value.strip()
    }
    if tag in labels:
        return True
    # Legacy browser snapshots joined Chinese label chips with spaces.  Only
    # use whitespace as a separator when the configured label itself has no
    # whitespace, so labels such as "Ready To Ship" remain exact.
    if not re.search(r"\s", tag):
        return tag in str(tag_text or "").split()
    return False


def _context_has_invalid_word(text: str, start: int, end: int) -> bool:
    left = max(text.rfind(separator, 0, start) for separator in "。．.;；,，\n\r") + 1
    right_candidates = [text.find(separator, end) for separator in "。．.;；,，\n\r"]
    right_positions = [position for position in right_candidates if position >= 0]
    right = min(right_positions) if right_positions else len(text)
    context = text[left:right]
    return any(word in context for word in INVALID_LOGISTICS_CONTEXT_WORDS)


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


def extract_logistics_numbers_from_remark(customer_remark: str | None) -> LogisticsNumberExtractionResult:
    """Extract the first usable logistics number from customer remark text."""

    text = str(customer_remark or "")
    valid_occurrences: list[str] = []
    excluded: list[str] = []
    truncated: list[str] = []
    for match in LOGISTICS_NO_RE.finditer(text):
        digits = match.group(1)
        logistics_no = f"ALS{digits[:11]}"
        if len(digits) > 11:
            truncated.append(logistics_no)
        if _context_has_invalid_word(text, match.start(), match.end()):
            excluded.append(logistics_no)
            continue
        valid_occurrences.append(logistics_no)

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

    return LogisticsNumberExtractionResult(
        selected_logistics_no=valid_unique[0] if valid_unique else None,
        valid_logistics_numbers=valid_unique,
        excluded_logistics_numbers=list(dict.fromkeys(excluded)),
        duplicate_logistics_numbers=duplicates,
        truncated_logistics_numbers=list(dict.fromkeys(truncated)),
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

    candidate_by_logistics_no: dict[str, ShipmentCandidate] = {}
    for row in rows:
        tag_text = str(row.get("tag_text") or "").strip()
        if not row_has_shipment_tag(tag_text, tag_name):
            continue
        report.tagged_row_count += 1
        system_order_no = str(row.get("system_order_no") or row.get("rowid") or "").strip()
        platform_order_no = str(row.get("platform_order_no") or "").strip()
        customer_remark = str(row.get("customer_remark") or "").strip()
        extraction = extract_logistics_numbers_from_remark(customer_remark)
        if not extraction.selected_logistics_no:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    reason="missing_valid_logistics",
                    logistics_numbers=extraction.excluded_logistics_numbers,
                    message="命中专属发货标签，但客服备注中没有可入队的有效物流单号。",
                )
            )
            continue

        report.valid_logistics_row_count += 1
        customer_shipping_service = normalize_customer_shipping_service(
            row.get("customer_shipping_service") or row.get("logistics")
        )
        if not customer_shipping_service:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    reason="missing_customer_shipping_service",
                    logistics_numbers=extraction.valid_logistics_numbers,
                    selected_logistics_no=extraction.selected_logistics_no,
                    message="命中专属发货标签且物流单号有效，但没有读取到客选物流，未自动入队。",
                )
            )
            continue
        if not platform_order_no:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no="",
                    reason="missing_platform_order_no",
                    logistics_numbers=extraction.valid_logistics_numbers,
                    selected_logistics_no=extraction.selected_logistics_no,
                    message="命中专属发货标签且物流单号有效，但没有读取到平台单号，未自动入队。",
                )
            )
            continue
        candidate = ShipmentCandidate(
            system_order_no=system_order_no,
            platform_order_no=platform_order_no,
            logistics_no=extraction.selected_logistics_no,
            shipment_tag_name=tag_name,
            tag_text=tag_text,
            sku_text=str(row.get("sku") or row.get("sku_text") or "").strip(),
            product_type=_shipment_product_types(row),
            customer_remark=customer_remark,
            status_text=str(row.get("status_text") or "").strip(),
            source_page=_int_or_none(row.get("source_page")),
            source_scroll_top=_int_or_none(row.get("source_scroll_top")),
            rowid=str(row.get("rowid") or "").strip() or None,
            receiver_name=str(row.get("receiver_name") or "").strip() or None,
            receiver_email=str(row.get("receiver_email") or "").strip() or None,
            receiver_phone=str(row.get("receiver_phone") or "").strip() or None,
            sales_platform_code=str(row.get("sales_platform_code") or "").strip() or None,
            sales_platform_name=str(row.get("sales_platform_name") or "").strip() or None,
            store_name=str(row.get("store_name") or "").strip() or None,
            site_name=str(row.get("site_name") or "").strip() or None,
            customer_shipping_service=customer_shipping_service,
            warnings=extraction.warnings,
        )
        # One Alibaba logistics number represents one physical logistics
        # order.  Split ERP rows can repeat the same customer remark, so the
        # same *first* ALS number may appear on several rows.  Preserve the
        # first row in source order instead of turning the later row into an
        # identity conflict or a second queue entry.
        existing_candidate = candidate_by_logistics_no.get(candidate.logistics_no)
        if existing_candidate is None:
            candidate_by_logistics_no[candidate.logistics_no] = candidate
            report.candidates.append(candidate)
        elif existing_candidate.platform_order_no == candidate.platform_order_no:
            if candidate.receiver_email and not existing_candidate.receiver_email:
                existing_candidate.receiver_email = candidate.receiver_email
            if candidate.receiver_phone and not existing_candidate.receiver_phone:
                existing_candidate.receiver_phone = candidate.receiver_phone
            if candidate.receiver_name and not existing_candidate.receiver_name:
                existing_candidate.receiver_name = candidate.receiver_name
            if candidate.product_type and not existing_candidate.product_type:
                existing_candidate.product_type = candidate.product_type
            existing_candidate.warnings = list(
                dict.fromkeys(
                    [
                        *existing_candidate.warnings,
                        "同一平台订单的多行命中了同一个首个 ALS 单号，已合并为一条队列记录。",
                    ]
                )
            )
        else:
            # Keep the later candidate so the store can freeze the globally
            # reused logistics number as a real cross-order conflict.
            report.candidates.append(candidate)
        if extraction.needs_manual_review:
            report.manual_reviews.append(
                ManualReviewItem(
                    system_order_no=system_order_no,
                    platform_order_no=platform_order_no,
                    reason="logistics_number_review",
                    logistics_numbers=extraction.valid_logistics_numbers,
                    selected_logistics_no=extraction.selected_logistics_no,
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
        if result.conflict:
            report.conflict_count += 1
        else:
            report.refreshed_count += 1
        if result.immediate_logistics:
            report.immediate_logistics_count += 1
        if result.immediate_erp:
            report.immediate_erp_count += 1
        report.duplicate_skipped.append(
            DuplicateShipmentItem(
                system_order_no=candidate.system_order_no,
                platform_order_no=candidate.platform_order_no,
                logistics_no=candidate.logistics_no,
                existing_system_order_no=existing.get("system_order_no"),
                existing_platform_order_no=existing.get("platform_order_no"),
                existing_identity_state=existing.get("identity_state"),
                existing_logistics_state=existing.get("logistics_state"),
                existing_erp_state=existing.get("erp_state"),
                existing_last_error=existing.get("last_error"),
                conflict=result.conflict,
                immediate_logistics=result.immediate_logistics,
                immediate_erp=result.immediate_erp,
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
                "logistics_no": item.get("logistics_no"),
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
