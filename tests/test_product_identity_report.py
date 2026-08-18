from __future__ import annotations

from erp_automation.operations.product_identity_report import (
    build_product_identity_audit_rows,
    classify_product_identity_evidence,
)
from shipment_automation.models import ShipmentCandidate
from shipment_automation.notification_domain import OrderProductSnapshot
from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.queue_store import ShipmentWorkflowStore


def test_report_never_claims_missing_siblings_without_current_evidence() -> None:
    row = {
        "product_type": "",
        "product_identity_catalog_version": "old-version",
    }

    assert classify_product_identity_evidence(
        row,
        {},
        catalog_version="current-version",
    ) == "未复核"


def test_report_distinguishes_sibling_evidence_retry_and_supplements(tmp_path) -> None:
    store = ShipmentWorkflowStore(tmp_path / "shipment.sqlite3")
    candidates = (
        ShipmentCandidate(
            system_order_no="SYS-SIBLING",
            platform_order_no="111-0000000-0000001",
            logistics_no="ALS-SIBLING",
            shipment_tag_name="自动标发",
            product_type="",
        ),
        ShipmentCandidate(
            system_order_no="SYS-RETRY",
            platform_order_no="111-0000000-0000002",
            logistics_no="ALS-RETRY",
            shipment_tag_name="自动标发",
            product_type="",
        ),
        ShipmentCandidate(
            system_order_no="SYS-SUPPLEMENT",
            platform_order_no="111-0000000-0000003-1",
            logistics_no="ALS-SUPPLEMENT",
            shipment_tag_name="自动标发",
            product_type="",
        ),
    )
    store.insert_candidates(candidates)
    store.apply_product_identity_backfill(
        [
            {
                "system_order_no": "SYS-SIBLING",
                "platform_order_no": "111-0000000-0000001",
                "observed_asins": ("B0UNKNOWN",),
                "evidence_scope": "sibling_aggregate",
                "evidence_system_order_nos": ("SYS-SIBLING", "SYS-BAG"),
            },
            {
                "system_order_no": "SYS-RETRY",
                "platform_order_no": "111-0000000-0000002",
                "error": "兄弟单列表读取不完整。",
                "evidence_scope": "sibling_discovery",
            },
            {
                "system_order_no": "SYS-SUPPLEMENT",
                "platform_order_no": "111-0000000-0000003-1",
                "evidence_scope": "supplemental_exact_detail",
                "evidence_system_order_nos": ("SYS-SUPPLEMENT",),
            },
        ],
        catalog_version="current-version",
    )

    rows = {
        row["平台单号"]: row
        for row in build_product_identity_audit_rows(
            store,
            catalog_version="current-version",
        )
    }
    assert rows["111-0000000-0000001"]["证据状态"] == (
        "同平台兄弟单已完整核验，ASIN 未收录"
    )
    assert rows["111-0000000-0000001"]["证据系统单号"] == (
        "SYS-SIBLING | SYS-BAG"
    )
    assert rows["111-0000000-0000002"]["证据状态"] == (
        "兄弟单或详情读取失败，等待重试"
    )
    assert rows["111-0000000-0000003-1"]["证据状态"] == (
        "补发单精确行已核验，无 ASIN"
    )


def test_report_includes_notification_only_sibling_asin_evidence(tmp_path) -> None:
    path = tmp_path / "notification-only.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    notification_store = ShipmentNotificationStore(path)
    platform = "112-0703089-1217824"
    notification_store.merge_full_scan_sources(
        [
            {
                "platform_order_no": platform,
                "system_order_nos": ("SYS-FRAME", "SYS-INSTRUCTION"),
                "purchased_at": "2026-08-18T00:00:00Z",
            }
        ]
    )
    notification_store.replace_product_scan(
        platform,
        [
            OrderProductSnapshot(
                platform_order_no=platform,
                system_order_no="SYS-FRAME",
                item_key="FRAME",
                local_sku="10X10-FRAME",
            ),
            OrderProductSnapshot(
                platform_order_no=platform,
                system_order_no="SYS-INSTRUCTION",
                item_key="INSTRUCTION",
                marketplace_product_id="B0CNVLXTWB",
                local_sku="Instruction",
            ),
        ],
        ("SYS-FRAME", "SYS-INSTRUCTION"),
    )

    assert build_product_identity_audit_rows(
        ShipmentWorkflowStore(path),
        catalog_version="current-version",
    ) == []
    rows = build_product_identity_audit_rows(
        ShipmentWorkflowStore(path),
        catalog_version="current-version",
        include_resolved=True,
    )
    assert len(rows) == 1
    assert rows[0]["平台单号"] == platform
    assert rows[0]["商品类型"] == "car_magnet"
    assert rows[0]["证据状态"] == "已识别"
    assert rows[0]["证据范围"] == "notification_full_scan_siblings"
    assert rows[0]["已观察ASIN"] == "B0CNVLXTWB"


def test_report_includes_notification_only_exact_sku_evidence(tmp_path) -> None:
    path = tmp_path / "notification-only-sku.sqlite3"
    ShipmentWorkflowStore(path).initialize()
    notification_store = ShipmentNotificationStore(path)
    platform = "114-4659879-3804266"
    notification_store.merge_full_scan_sources(
        [
            {
                "platform_order_no": platform,
                "system_order_nos": ("SYS-MAGNET",),
                "purchased_at": "2026-08-18T00:00:00Z",
            }
        ]
    )
    notification_store.replace_product_scan(
        platform,
        [
            OrderProductSnapshot(
                platform_order_no=platform,
                system_order_no="SYS-MAGNET",
                item_key="MAGNET",
                local_sku="Car-Magnet-10x20in-2pcs",
            )
        ],
        ("SYS-MAGNET",),
    )

    rows = build_product_identity_audit_rows(
        ShipmentWorkflowStore(path),
        catalog_version="current-version",
        include_resolved=True,
    )

    assert rows[0]["商品类型"] == "car_magnet"
    assert rows[0]["证据状态"] == "已识别"
    assert rows[0]["证据范围"] == "notification_full_scan_exact_skus"
    assert rows[0]["已观察ASIN"] == ""
    assert rows[0]["已观察SKU"] == "Car-Magnet-10x20in-2pcs"
