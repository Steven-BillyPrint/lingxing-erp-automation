"""Read-only replay of Lingxing orders through the Alibaba product classifier.

The audit deliberately calls only the Lingxing order-list read endpoint.  It
does not update Lingxing, create Alibaba drafts, or write application state.
Reports contain order identifiers and classification evidence, but no raw
order payload or recipient data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_automation.configuration import EncryptedConfigurationStore
from erp_automation.integrations.lingxing.runtime import (
    create_lingxing_openapi_client,
)
from shipment_automation.alibaba_ordering import (
    AlibabaOrderRuleError,
    AmbiguousProductError,
    UnsupportedProductError,
    extract_order_product_identifier_rows_with_amount,
)
from shipment_automation.alibaba_product_classification import (
    classify_order_product,
)


_CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_WINDOW_DAYS = 30
_PAGE_SIZE = 500


@dataclass(frozen=True)
class AuditRow:
    system_order_no: str
    platform_order_no: str
    state: str
    category: str
    matched_identifiers: str
    unmatched_identifiers: str
    asin_evidence: str
    sku_evidence: str
    selected_sales_amount: str
    selected_sales_currency: str
    selection_reason: str
    reason: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须使用 YYYY-MM-DD。") from exc


def _page_payload(response: object) -> tuple[list[Mapping[str, Any]], int | None]:
    data = getattr(response, "data", None)
    if isinstance(data, list):
        rows = data
        total = None
    elif isinstance(data, Mapping):
        rows = data.get("list")
        raw_total = data.get("total")
        try:
            total = int(raw_total) if raw_total is not None else None
        except (TypeError, ValueError):
            total = None
    else:
        raise RuntimeError("领星订单列表响应格式无效。")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise RuntimeError("领星订单列表响应缺少有效 list。")
    return list(rows), total


def _windows(start: date, end: date) -> tuple[tuple[int, int], ...]:
    start_at = datetime.combine(start, time.min, tzinfo=_CHINA_TIMEZONE)
    end_at = datetime.combine(end, time.max, tzinfo=_CHINA_TIMEZONE).replace(
        microsecond=0
    )
    output: list[tuple[int, int]] = []
    cursor = start_at
    while cursor < end_at:
        window_end = min(cursor + timedelta(days=_WINDOW_DAYS), end_at)
        output.append((int(cursor.timestamp()), int(window_end.timestamp())))
        if window_end >= end_at:
            break
        cursor = window_end - timedelta(seconds=1)
    return tuple(output)


def _text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and (text := str(value).strip()):
            return text
    return ""


def _record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _text(record, "global_order_no", "globalOrderNo"),
        _text(record, "order_number", "orderNumber"),
    )


def _state_for_error(exc: AlibabaOrderRuleError) -> str:
    message = str(exc)
    if isinstance(exc, AmbiguousProductError):
        return "ambiguous"
    if "尚未配置阿里巴巴申报" in message:
        return "known_without_template"
    if "尚未录入商品目录" in message:
        return "unknown_asin"
    if isinstance(exc, UnsupportedProductError):
        return "unmatched"
    return "rule_error"


def _classify(record: Mapping[str, Any]) -> AuditRow:
    evidence_rows = extract_order_product_identifier_rows_with_amount(record)
    asin_evidence = tuple(
        dict.fromkeys(
            item.identifier
            for row in evidence_rows
            for item in row.identifiers
            if item.source_kind == "asin"
        )
    )
    sku_evidence = tuple(
        dict.fromkeys(
            item.identifier
            for row in evidence_rows
            for item in row.identifiers
            if item.source_kind == "sku"
        )
    )
    base = {
        "system_order_no": _record_key(record)[0],
        "platform_order_no": _record_key(record)[1],
        "asin_evidence": " | ".join(asin_evidence),
        "sku_evidence": " | ".join(sku_evidence),
    }
    try:
        classification = classify_order_product(record)
    except AlibabaOrderRuleError as exc:
        return AuditRow(
            **base,
            state=_state_for_error(exc),
            category="",
            matched_identifiers="",
            unmatched_identifiers=" | ".join(
                dict.fromkeys((*asin_evidence, *sku_evidence))
            ),
            selected_sales_amount="",
            selected_sales_currency="",
            selection_reason="",
            reason=str(exc),
        )
    return AuditRow(
        **base,
        state="classified",
        category=str(classification.category),
        matched_identifiers=" | ".join(classification.matched_skus),
        unmatched_identifiers=" | ".join(
            classification.unmatched_identifiers
        ),
        selected_sales_amount=(
            str(classification.selected_sales_amount)
            if classification.selected_sales_amount is not None
            else ""
        ),
        selected_sales_currency=classification.selected_sales_currency,
        selection_reason=classification.selection_reason,
        reason="",
    )


async def _read_orders(
    *,
    workspace: Path,
    start: date,
    end: date,
) -> tuple[list[Mapping[str, Any]], int]:
    config_store = EncryptedConfigurationStore(workspace / "data" / "config.enc")
    client = await create_lingxing_openapi_client(config_store)
    records: dict[tuple[str, str], Mapping[str, Any]] = {}
    request_count = 0
    try:
        for start_time, end_time in _windows(start, end):
            offset = 0
            while True:
                response = await client.list_orders(
                    offset=offset,
                    length=_PAGE_SIZE,
                    date_type="global_purchase_time",
                    start_time=start_time,
                    end_time=end_time,
                    include_delete=False,
                )
                request_count += 1
                page, total = _page_payload(response)
                for record in page:
                    key = _record_key(record)
                    if key == ("", ""):
                        raise RuntimeError("领星订单缺少稳定订单编号，无法安全去重。")
                    records[key] = record
                offset += len(page)
                if not page or len(page) < _PAGE_SIZE:
                    break
                if total is not None and offset >= total:
                    break
    finally:
        await client.aclose()
    return list(records.values()), request_count


def _write_reports(
    *,
    output_prefix: Path,
    start: date,
    end: date,
    rows: Sequence[AuditRow],
    request_count: int,
) -> tuple[Path, Path]:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_prefix.with_suffix(".csv")
    json_path = output_prefix.with_suffix(".summary.json")
    fieldnames = tuple(AuditRow.__dataclass_fields__)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    states = Counter(row.state for row in rows)
    categories = Counter(row.category for row in rows if row.category)
    selection_reasons = Counter(
        row.selection_reason for row in rows if row.selection_reason
    )
    summary = {
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "read_only": True,
        "request_count": request_count,
        "order_count": len(rows),
        "states": dict(sorted(states.items())),
        "categories": dict(sorted(categories.items())),
        "selection_reasons": dict(sorted(selection_reasons.items())),
        "csv": str(csv_path.resolve()),
    }
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return csv_path, json_path


async def _run(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    if args.end < args.start:
        raise ValueError("结束日期不能早于开始日期。")
    records, request_count = await _read_orders(
        workspace=workspace,
        start=args.start,
        end=args.end,
    )
    rows = tuple(_classify(record) for record in records)
    csv_path, json_path = _write_reports(
        output_prefix=Path(args.output).resolve(),
        start=args.start,
        end=args.end,
        rows=rows,
        request_count=request_count,
    )
    summary = json.loads(json_path.read_text(encoding="utf-8"))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV: {csv_path}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--output",
        default="output/alibaba-product-classification-audit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
