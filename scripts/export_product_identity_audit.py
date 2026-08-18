from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from erp_automation.operations.product_identity_report import (
    build_product_identity_audit_rows,
)
from lingxing_automation.products.catalog import PRODUCT_IDENTITY_CATALOG_VERSION
from shipment_automation.queue_store import ShipmentWorkflowStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export evidence-based product identity audit rows."
    )
    parser.add_argument("queue_path", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument(
        "--catalog-version",
        default=PRODUCT_IDENTITY_CATALOG_VERSION,
    )
    parser.add_argument("--include-resolved", action="store_true")
    args = parser.parse_args()

    rows = build_product_identity_audit_rows(
        ShipmentWorkflowStore(args.queue_path),
        catalog_version=args.catalog_version,
        include_resolved=args.include_resolved,
    )
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else [
        "平台单号",
        "系统单号",
        "物流单号",
        "SKU",
        "商品类型",
        "证据状态",
        "证据范围",
        "证据系统单号",
        "已观察ASIN",
        "目录版本",
        "核验时间",
        "下次重试时间",
        "重试次数",
        "最近错误",
    ]
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
