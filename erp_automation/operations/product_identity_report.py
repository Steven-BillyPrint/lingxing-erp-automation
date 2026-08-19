"""Evidence-based product identity audit rows.

The report deliberately uses only persisted, current-run evidence.  Missing
evidence is labelled ``未复核`` and is never promoted to a claim that sibling
orders or ASINs do not exist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from lingxing_automation.products.catalog import (
    identify_product_types,
    identify_product_types_from_skus,
    preferred_product_type,
)
from shipment_automation.queue_store import ShipmentWorkflowStore


def _texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        source = (value,)
    elif isinstance(value, (list, tuple, set)):
        source = value
    else:
        source = ()
    return tuple(
        text
        for text in dict.fromkeys(str(item or "").strip() for item in source)
        if text
    )


def classify_product_identity_evidence(
    row: Mapping[str, Any],
    event_details: Mapping[str, Any] | None,
    *,
    catalog_version: str,
) -> str:
    """Classify one row without treating absence of evidence as evidence."""

    product_type = str(row.get("product_type") or "").strip()
    if product_type:
        return "已识别"

    next_retry_at = str(row.get("product_identity_next_retry_at") or "").strip()
    last_error = str(row.get("product_identity_last_error") or "").strip()
    if next_retry_at or last_error:
        return "兄弟单或详情读取失败，等待重试"

    normalized_catalog_version = str(catalog_version or "").strip()
    checked_catalog_version = str(
        row.get("product_identity_catalog_version") or ""
    ).strip()
    if (
        not normalized_catalog_version
        or checked_catalog_version != normalized_catalog_version
    ):
        return "未复核"

    details = event_details or {}
    scope = str(details.get("evidence_scope") or "").strip()
    observed_asins = _texts(details.get("observed_asins"))
    observed_skus = _texts(details.get("observed_skus"))
    if scope in {"completed_exact_sku", "notification_full_scan_exact_skus"}:
        return (
            "已完成订单精确 SKU 未收录"
            if observed_skus
            else "已完成订单无可识别 SKU"
        )
    if scope == "supplemental_exact_detail":
        return (
            "补发单精确行已核验，ASIN 未收录"
            if observed_asins
            else "补发单精确行已核验，无 ASIN"
        )
    if scope == "sibling_aggregate":
        return (
            "同平台兄弟单已完整核验，ASIN 未收录"
            if observed_asins
            else "同平台兄弟单已完整核验，无 ASIN"
        )
    if scope == "sibling_list_item":
        return (
            "领星列表兄弟单已完整核验，ASIN 未收录"
            if observed_asins
            else "领星列表兄弟单未返回 ASIN"
        )
    if scope == "supplemental_list_item":
        return (
            "补发单领星列表精确行已核验，ASIN 未收录"
            if observed_asins
            else "补发单领星列表精确行未返回 ASIN"
        )
    if scope == "exact_detail":
        return (
            "精确行已核验，ASIN 未收录"
            if observed_asins
            else "精确行已核验，无 ASIN"
        )
    return "已核验但证据范围不完整"


def build_product_identity_audit_rows(
    store: ShipmentWorkflowStore,
    *,
    catalog_version: str,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """Build auditable rows from the live queue and its identity events."""

    output: list[dict[str, Any]] = []
    platforms_with_jobs: set[str] = set()
    for row in store.list_all_jobs():
        platform_order_no = str(row.get("platform_order_no") or "").strip()
        platforms_with_jobs.add(platform_order_no)
        product_type = str(row.get("product_type") or "").strip()
        if product_type and not include_resolved:
            continue
        identity_events = [
            event
            for event in store.history(str(row.get("logistics_no") or ""))
            if event.event_type
            in {
                "PRODUCT_IDENTITY_BACKFILLED",
                "PRODUCT_IDENTITY_CHECKED",
                "PRODUCT_IDENTITY_RETRY_SCHEDULED",
            }
        ]
        latest_event = identity_events[-1] if identity_events else None
        details = dict(latest_event.details) if latest_event else {}
        output.append(
            {
                "平台单号": platform_order_no,
                "系统单号": str(row.get("system_order_no") or "").strip(),
                "物流单号": str(row.get("logistics_no") or "").strip(),
                "SKU": str(row.get("sku_text") or "").strip(),
                "商品类型": product_type,
                "证据状态": classify_product_identity_evidence(
                    row,
                    details,
                    catalog_version=catalog_version,
                ),
                "证据范围": str(details.get("evidence_scope") or "").strip(),
                "证据系统单号": " | ".join(
                    _texts(details.get("evidence_system_order_nos"))
                ),
                "已观察ASIN": " | ".join(_texts(details.get("observed_asins"))),
                "已观察SKU": " | ".join(_texts(details.get("observed_skus"))),
                "目录版本": str(
                    row.get("product_identity_catalog_version") or ""
                ).strip(),
                "核验时间": str(
                    row.get("product_identity_checked_at") or ""
                ).strip(),
                "下次重试时间": str(
                    row.get("product_identity_next_retry_at") or ""
                ).strip(),
                "重试次数": int(row.get("product_identity_retry_count") or 0),
                "最近错误": str(
                    row.get("product_identity_last_error") or ""
                ).strip(),
            }
        )

    with store.connect() as conn:
        source_rows = conn.execute(
            """
            SELECT platform_order_no, system_order_nos_json
            FROM shipment_notification_order_sources
            WHERE active = 1
            ORDER BY platform_order_no
            """
        ).fetchall()
        snapshot_rows = conn.execute(
            """
            SELECT platform_order_no, system_order_no, marketplace_product_id,
                   local_sku
            FROM shipment_order_product_snapshots
            WHERE active = 1
            ORDER BY platform_order_no, source_sequence, id
            """
        ).fetchall()
    snapshots_by_platform: dict[str, list[Any]] = {}
    for snapshot in snapshot_rows:
        snapshots_by_platform.setdefault(str(snapshot[0]), []).append(snapshot)
    for source in source_rows:
        platform_order_no = str(source[0] or "").strip()
        if not platform_order_no or platform_order_no in platforms_with_jobs:
            continue
        snapshots = snapshots_by_platform.get(platform_order_no, [])
        marketplace_product_ids = _texts(
            [snapshot[2] for snapshot in snapshots]
        )
        local_skus = _texts([snapshot[3] for snapshot in snapshots])
        selected_product_type = preferred_product_type(
            identify_product_types(marketplace_product_ids)
        )
        evidence_scope = "notification_full_scan_siblings"
        if not selected_product_type:
            selected_product_type = preferred_product_type(
                identify_product_types_from_skus(local_skus)
            )
            evidence_scope = "notification_full_scan_exact_skus"
        if selected_product_type and not include_resolved:
            continue
        try:
            source_systems = _texts(json.loads(str(source[1] or "[]")))
        except (TypeError, json.JSONDecodeError):
            source_systems = ()
        evidence_systems = _texts(
            [snapshot[1] for snapshot in snapshots] or source_systems
        )
        if selected_product_type:
            evidence_status = "已识别"
        elif marketplace_product_ids:
            evidence_status = "客户通知全量扫描已核验，ASIN 未收录"
        elif snapshots:
            evidence_status = "客户通知全量扫描已核验，无 ASIN"
        else:
            evidence_status = "未复核"
        output.append(
            {
                "平台单号": platform_order_no,
                "系统单号": " | ".join(source_systems),
                "物流单号": "",
                "SKU": " | ".join(_texts([snapshot[3] for snapshot in snapshots])),
                "商品类型": selected_product_type,
                "证据状态": evidence_status,
                "证据范围": evidence_scope,
                "证据系统单号": " | ".join(evidence_systems),
                "已观察ASIN": " | ".join(marketplace_product_ids),
                "已观察SKU": " | ".join(
                    local_skus
                ),
                "目录版本": str(catalog_version or "").strip(),
                "核验时间": "",
                "下次重试时间": "",
                "重试次数": 0,
                "最近错误": "",
            }
        )
    return output


__all__ = [
    "build_product_identity_audit_rows",
    "classify_product_identity_evidence",
]
