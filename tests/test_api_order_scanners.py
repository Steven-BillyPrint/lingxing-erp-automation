from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from erp_automation.application.api_scanners import (
    ApiScanState,
    SHIPMENT_REQUIRED_FIELDS,
    fetch_all_order_pages,
    normalize_api_order_rows,
    redact_sensitive_payload,
    scan_customization_candidates,
    scan_shipment_candidates,
)
from erp_automation.application.lingxing_gateway import OrderPage, OrderRecord
from shipment_automation.models import ManualCompletionItem
from shipment_automation.queue_store import QueueInsertResult, ShipmentQueueStore


class MockGateway:
    def __init__(self, *pages: OrderPage | BaseException) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    async def list_orders(self, **kwargs: Any) -> OrderPage:
        self.calls.append(dict(kwargs))
        if not self.pages:
            raise AssertionError("unexpected extra page request")
        result = self.pages.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ProcessedStore:
    def __init__(self, values: set[str]) -> None:
        self.values = values

    def processed_platform_orders(self) -> set[str]:
        return set(self.values)


class RecordingQueue:
    path = "queue.sqlite3"

    def __init__(self) -> None:
        self.upserts = []
        self.complete_calls: list[tuple[set[str], str, str | None]] = []

    def upsert_candidate(self, candidate, *, run_id=None):
        self.upserts.append((candidate, run_id))
        return QueueInsertResult(True, candidate)

    def complete_missing_pending_orders(
        self,
        visible_system_order_nos,
        *,
        discovered_before,
        run_id=None,
    ):
        self.complete_calls.append((set(visible_system_order_nos), discovered_before, run_id))
        return [
            ManualCompletionItem(
                system_order_no="103000000000000099",
                platform_order_no="111-0000000-0000099",
                logistics_no="ALS00000000099",
            )
        ]


class MixedAuditQueue(RecordingQueue):
    def __init__(self, duplicate_platform_order_no: str) -> None:
        super().__init__()
        self.duplicate_platform_order_no = duplicate_platform_order_no

    def upsert_candidate(self, candidate, *, run_id=None):
        self.upserts.append((candidate, run_id))
        if candidate.platform_order_no == self.duplicate_platform_order_no:
            return QueueInsertResult(
                False,
                candidate,
                existing={
                    "system_order_no": candidate.system_order_no,
                    "platform_order_no": candidate.platform_order_no,
                },
            )
        return QueueInsertResult(True, candidate)


def _record(
    system_order_no: str,
    platform_order_no: str,
    *,
    payload: dict[str, Any] | None = None,
) -> OrderRecord:
    data = dict(payload or {})
    data.setdefault("global_order_no", system_order_no)
    data.setdefault("order_number", platform_order_no)
    return OrderRecord(system_order_no, platform_order_no, data)


def _page(
    records: list[OrderRecord],
    *,
    offset: int,
    length: int,
    total: int | None,
    request_id: str,
) -> OrderPage:
    return OrderPage(
        items=tuple(records),
        offset=offset,
        length=length,
        total=total,
        request_id=request_id,
    )


def _shipment_payload(*, include_remark: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "globalOrderNo": "103000000000000001",
        "orderNumber": "111-0000000-0000001",
        "tags": [{"name": "自动标发"}],
        "orderStatusName": "待审核",
        "paymentTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "detail": {
            "orderItemList": [
                {"amazonAsin": "B0CRRGTPFH", "sellerSku": "canopytents", "quantity": 1}
            ]
        },
    }
    if include_remark:
        payload["customerServiceRemark"] = "已建单 ALS01781406025"
    return payload


def _official_customization_payload(
    system_order_no: str,
    platform_order_no: str,
    *,
    order_tag: list[dict[str, Any]] | None = None,
    pending_order_tag: list[Any] | None = None,
    exception_order_tag: list[Any] | None = None,
    remark: str = "",
) -> dict[str, Any]:
    paid_at = int(datetime.now().timestamp())
    payload: dict[str, Any] = {
        "global_order_no": system_order_no,
        "global_payment_time": paid_at,
        "status": 4,
        "remark": remark,
        "order_tag": list(order_tag or []),
        "item_info": [
            {
                "id": f"item-{system_order_no}",
                "platform_order_no": platform_order_no,
                "product_no": "B0CRRGTPFH",
                "local_sku": "canopytents",
                "quantity": 1,
            }
        ],
        "platform_info": [
            {
                "platform_code": "10001",
                "platform_order_no": platform_order_no,
                "payment_time": paid_at,
            }
        ],
    }
    if pending_order_tag is not None:
        payload["pending_order_tag"] = list(pending_order_tag)
    if exception_order_tag is not None:
        payload["exception_order_tag"] = list(exception_order_tag)
    return payload


def test_pagination_reads_every_page_forwards_filters_and_keeps_request_ids() -> None:
    async def run() -> None:
        gateway = MockGateway(
            _page(
                [
                    _record("103000000000000001", "111-0000000-0000001"),
                    _record("103000000000000002", "111-0000000-0000002"),
                ],
                offset=0,
                length=2,
                total=3,
                request_id="req-1",
            ),
            _page(
                [_record("103000000000000003", "111-0000000-0000003")],
                offset=2,
                length=2,
                total=3,
                request_id="req-2",
            ),
        )

        result = await fetch_all_order_pages(
            gateway,
            filters={"documented_filter": "pending"},
            page_size=2,
        )

        assert result.state is ApiScanState.COMPLETE
        assert len(result.orders) == 3
        assert result.request_ids == ("req-1", "req-2")
        assert gateway.calls == [
            {"offset": 0, "length": 2, "filters": {"documented_filter": "pending"}},
            {"offset": 2, "length": 2, "filters": {"documented_filter": "pending"}},
        ]

    asyncio.run(run())


def test_repeated_page_is_incomplete_and_cannot_loop_forever() -> None:
    async def run() -> None:
        repeated = [
            _record("103000000000000001", "111-0000000-0000001"),
            _record("103000000000000002", "111-0000000-0000002"),
        ]
        gateway = MockGateway(
            _page(repeated, offset=0, length=2, total=4, request_id="req-1"),
            _page(repeated, offset=2, length=2, total=4, request_id="req-2"),
        )

        result = await fetch_all_order_pages(gateway, page_size=2, max_pages=50)

        assert result.state is ApiScanState.INCOMPLETE
        assert len(result.orders) == 2
        assert len(gateway.calls) == 2
        assert result.request_ids == ("req-1", "req-2")
        assert result.diagnostics[-1].code == "repeated_page"

    asyncio.run(run())


def test_maximum_page_guard_marks_unknown_total_snapshot_incomplete() -> None:
    async def run() -> None:
        gateway = MockGateway(
            _page(
                [_record("103000000000000001", "111-0000000-0000001")],
                offset=0,
                length=1,
                total=None,
                request_id="req-1",
            ),
            _page(
                [_record("103000000000000002", "111-0000000-0000002")],
                offset=1,
                length=1,
                total=None,
                request_id="req-2",
            ),
        )

        result = await fetch_all_order_pages(gateway, page_size=1, max_pages=2)

        assert result.state is ApiScanState.INCOMPLETE
        assert result.diagnostics[-1].code == "maximum_pages_reached"
        assert len(gateway.calls) == 2

    asyncio.run(run())


def test_customization_scan_supports_aliases_nested_items_96_hours_and_processed_store() -> None:
    async def run() -> None:
        recent = (datetime.now() - timedelta(hours=95)).strftime("%Y-%m-%d %H:%M:%S")
        old = (datetime.now() - timedelta(hours=97)).strftime("%Y-%m-%d %H:%M:%S")
        payloads = [
            {
                "globalOrderNo": "103000000000000001",
                "orderNumber": "111-0000000-0000001",
                "paymentTime": recent,
                "tagList": [],
                "detail": {"items": [{"amazonAsin": "B0CRRGTPFH", "localSku": "tent", "qty": 1}]},
            },
            {
                "global_order_no": "103000000000000002",
                "order_number": "111-0000000-0000002",
                "paid_at": old,
                "tags": [],
                "order_item_list": [{"asin": "B0CRRGTPFH", "seller_sku": "tent"}],
            },
            {
                "global_order_no": "103000000000000003",
                "order_number": "111-0000000-0000003",
                "paid_at": recent,
                "tags": [],
                "order_item_list": [{"asin": "B0CRRGTPFH", "seller_sku": "tent"}],
            },
        ]
        records = [OrderRecord(None, None, payload) for payload in payloads]
        gateway = MockGateway(
            _page(records, offset=0, length=10, total=3, request_id="custom-req")
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore({"111-0000000-0000003"}),
            filters={"seller_id": 7},
            page_size=10,
        )

        assert result.state is ApiScanState.COMPLETE
        assert result.payment_window_hours == 96.0
        assert [item.platform_order_no for item in result.candidates] == ["111-0000000-0000001"]
        assert result.skip_counts["payment_old"] == 1
        assert result.skip_counts["already_processed_or_duplicate"] == 1
        assert gateway.calls[0]["filters"] == {"seller_id": 7}

    asyncio.run(run())


def test_normalizer_supports_documented_multiplatform_order_response_shape() -> None:
    """Use the field names from Lingxing's official MultiPlatOrderV2 example."""

    async def run() -> None:
        paid_at = int(datetime.now().timestamp())
        payload = {
            "global_order_no": "103000000000000001",
            "global_payment_time": paid_at,
            "status": 4,
            "remark": "已建单 ALS01781406025",
            "order_tag": [{"tag_type": "自定义订单标签", "tag_name": "自动标发"}],
            "item_info": [
                {
                    "id": "8745387",
                    "platform_order_no": "111-0000000-0000001",
                    "product_no": "B0CRRGTPFH",
                    "local_sku": "canopytents",
                    "quantity": 2,
                }
            ],
            "platform_info": [
                {
                    "platform_code": "10001",
                    "platform_order_no": "111-0000000-0000001",
                    "payment_time": paid_at,
                }
            ],
            "logistics_info": {"logistics_type_name": "UPS", "tracking_no": ""},
        }
        pagination = await fetch_all_order_pages(
            MockGateway(
                _page(
                    [OrderRecord("103000000000000001", None, payload)],
                    offset=0,
                    length=20,
                    total=1,
                    request_id="official-shape",
                )
            ),
            page_size=20,
        )

        normalized = normalize_api_order_rows(pagination)

        custom = normalized.customization_rows[0]
        shipment = normalized.shipment_rows[0]
        assert custom["platform_order_no"] == "111-0000000-0000001"
        assert custom["asin"] == "B0CRRGTPFH"
        assert custom["sku"] == "canopytents 共2"
        assert custom["paid_at_text"]
        assert shipment["tag_text"] == "自动标发"
        assert shipment["customer_remark"] == "已建单 ALS01781406025"
        assert normalized.missing_fields(("system", "platform", "paid_at", "tag", "customer_remark")) == ()

    asyncio.run(run())


def test_one_global_order_keeps_each_item_with_its_own_platform_order() -> None:
    async def run() -> None:
        paid_at = int(datetime.now().timestamp())
        payload = {
            "global_order_no": "103000000000000141",
            "global_payment_time": paid_at,
            "status": 4,
            "remark": "",
            "order_tag": [],
            "item_info": [
                {
                    "id": "item-a",
                    "platform_order_no": "112-0000000-0000141",
                    "product_no": "B0CRRGTPFH",
                    "local_sku": "tent-a",
                    "quantity": 2,
                },
                {
                    "id": "item-b",
                    "platform_order_no": "113-0000000-0000142",
                    "product_no": "B0CRRGTPFH",
                    "local_sku": "tent-b",
                    "quantity": 3,
                },
            ],
            "platform_info": [
                {"platform_order_no": "112-0000000-0000141", "payment_time": paid_at},
                {"platform_order_no": "113-0000000-0000142", "payment_time": paid_at},
            ],
        }
        gateway = MockGateway(
            _page(
                [OrderRecord("103000000000000141", None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="two-platform-orders",
            )
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore(set()),
            page_size=20,
        )

        assert result.state is ApiScanState.COMPLETE
        assert [item.platform_order_no for item in result.candidates] == [
            "112-0000000-0000141",
            "113-0000000-0000142",
        ]
        audits = {item["platform_order_no"]: item for item in result.audit_decisions}
        assert audits["112-0000000-0000141"]["items"][0]["sku"] == "tent-a"
        assert audits["112-0000000-0000141"]["items"][0]["quantity_normalized"] == 2
        assert audits["113-0000000-0000142"]["items"][0]["sku"] == "tent-b"
        assert audits["113-0000000-0000142"]["items"][0]["quantity_normalized"] == 3

        normalized = normalize_api_order_rows(result.pagination)
        assert normalized.shipment_rows[0]["platform_order_no"] == ""
        assert normalized.missing_fields(SHIPMENT_REQUIRED_FIELDS)[0].missing_fields == (
            "shipment_platform",
        )

    asyncio.run(run())


def test_normalizer_keeps_typed_tag_sources_separate_for_each_workflow() -> None:
    async def run() -> None:
        payload = _official_customization_payload(
            "103000000000000011",
            "112-0000000-0000011",
            order_tag=[
                {"tag_type": "系统处理类型", "tag_name": "可合并订单"},
                {"tag_type": "自定义订单标签", "tag_name": "直接制作"},
                {"tag_type": "自定义订单标签", "tag_name": "客户确认中"},
            ],
            pending_order_tag=[
                # The live response duplicates system hints as untyped
                # strings; these must not become visible workflow labels.
                "未分配物流",
                {"tag_type": "自定义订单标签", "tag_name": "帐篷标发"},
            ],
            exception_order_tag=[
                "地址异常",
                {"tag_type": "自定义订单标签", "tag_name": "客户确认中"},
            ],
        )
        pagination = await fetch_all_order_pages(
            MockGateway(
                _page(
                    [OrderRecord("103000000000000011", None, payload)],
                    offset=0,
                    length=20,
                    total=1,
                    request_id="typed-mixed-tags",
                )
            ),
            page_size=20,
        )

        normalized = normalize_api_order_rows(pagination)
        custom = normalized.customization_rows[0]
        shipment = normalized.shipment_rows[0]

        assert custom["tag_text"] == "直接制作 | 客户确认中 | 帐篷标发"
        assert shipment["tag_text"] == "直接制作 | 客户确认中 | 帐篷标发"
        for system_tag in ("可合并订单", "未分配物流", "地址异常"):
            assert system_tag not in custom["tag_text"]
            assert system_tag not in shipment["tag_text"]
        assert normalized.missing_fields(("tag",)) == ()

    asyncio.run(run())


def test_customization_scan_allows_system_only_112_but_blocks_custom_tagged_114() -> None:
    async def run() -> None:
        payloads = [
            _official_customization_payload(
                "103000000000000112",
                "112-1999004-7905025",
                order_tag=[
                    {"tag_type": "系统处理类型", "tag_name": "可合并订单"},
                    {"tag_type": "系统订单标签", "tag_name": "未分配物流"},
                ],
            ),
            _official_customization_payload(
                "103000000000000114",
                "114-7667481-5103463",
                order_tag=[
                    {"tag_type": "自定义订单标签", "tag_name": "直接制作"},
                ],
                pending_order_tag=[
                    "未分配物流",
                ],
            ),
            _official_customization_payload(
                "103000000000000119",
                "111-0000000-0000019",
                order_tag=[
                    {"tag_type": "自定义订单标签", "tag_name": "客户确认中"},
                    {"tag_type": "系统订单标签", "tag_name": "未分配物流"},
                ],
            ),
        ]
        gateway = MockGateway(
            _page(
                [OrderRecord(str(payload["global_order_no"]), None, payload) for payload in payloads],
                offset=0,
                length=20,
                total=3,
                request_id="real-tag-regression",
            )
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore(set()),
            page_size=20,
        )

        assert result.state is ApiScanState.COMPLETE
        assert [item.platform_order_no for item in result.candidates] == [
            "112-1999004-7905025",
        ]
        assert result.skip_counts == {"has_tag": 2}
        assert all(item.tag_text is None for item in result.candidates)
        audits = {
            item["platform_order_no"]: item
            for item in result.audit_decisions
        }
        assert audits["112-1999004-7905025"]["decision"] == "candidate"
        assert audits["112-1999004-7905025"]["reason_code"] == "eligible"
        assert audits["112-1999004-7905025"]["custom_tag_text"] == ""
        assert audits["114-7667481-5103463"]["decision"] == "excluded"
        assert audits["114-7667481-5103463"]["reason_code"] == "has_tag"
        assert audits["114-7667481-5103463"]["custom_tag_text"] == "直接制作"
        assert audits["114-7667481-5103463"]["items"] == [
            {
                "asin": "B0CRRGTPFH",
                "sku": "canopytents",
                "quantity_raw": 1,
                "quantity_normalized": 1,
                "quantity_status": "valid",
            }
        ]
        json.dumps(result.audit_decisions, ensure_ascii=False)

    asyncio.run(run())


def test_customization_missing_order_tag_fails_closed_and_returns_no_candidates() -> None:
    async def run() -> None:
        payload = _official_customization_payload(
            "103000000000000131",
            "112-0000000-0000031",
        )
        payload.pop("order_tag")
        gateway = MockGateway(
            _page(
                [OrderRecord("103000000000000131", None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="missing-custom-tag-field",
            )
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore(set()),
            page_size=20,
        )

        assert result.state is ApiScanState.INCOMPLETE
        assert result.candidate_count == 0
        assert result.candidates == ()
        assert result.diagnostics[-1].missing_fields == ("tag",)
        assert result.audit_decisions[0]["decision"] == "manual_review"
        assert result.audit_decisions[0]["reason_code"] == "missing_critical_fields"
        assert result.audit_decisions[0]["missing_fields"] == ["tag"]

    asyncio.run(run())


def test_customization_ignores_item_level_tags_when_order_tag_is_present_and_empty() -> None:
    async def run() -> None:
        payload = _official_customization_payload(
            "103000000000000132",
            "112-0000000-0000032",
        )
        payload["item_info"][0]["tags"] = [
            {"tag_type": "自定义订单标签", "tag_name": "客户确认中"},
            {"name": "商品元数据标签"},
        ]
        gateway = MockGateway(
            _page(
                [OrderRecord("103000000000000132", None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="item-tags-not-order-tags",
            )
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore(set()),
            page_size=20,
        )

        assert result.state is ApiScanState.COMPLETE
        assert [item.platform_order_no for item in result.candidates] == [
            "112-0000000-0000032"
        ]
        assert result.audit_decisions[0]["custom_tag_text"] == ""

    asyncio.run(run())


def test_incomplete_customization_pagination_returns_no_formal_candidates() -> None:
    async def run() -> None:
        payload = _official_customization_payload(
            "103000000000000133",
            "112-0000000-0000033",
        )
        gateway = MockGateway(
            _page(
                [OrderRecord("103000000000000133", None, payload)],
                offset=0,
                length=1,
                total=2,
                request_id="incomplete-customization",
            )
        )

        result = await scan_customization_candidates(
            gateway,
            ProcessedStore(set()),
            page_size=1,
            max_pages=1,
        )

        assert result.state is ApiScanState.INCOMPLETE
        assert result.candidate_count == 0
        assert result.candidates == ()
        assert result.audit_decisions[0]["decision"] == "manual_review"
        assert result.audit_decisions[0]["reason_code"] == "snapshot_incomplete"

    asyncio.run(run())


def test_shipment_scan_retains_typed_custom_tent_shipment_tag() -> None:
    async def run() -> None:
        payload = _official_customization_payload(
            "103000000000000121",
            "112-0000000-0000021",
            order_tag=[
                {"tag_type": "系统处理类型", "tag_name": "可合并订单"},
                {"tag_type": "自定义订单标签", "tag_name": "帐篷标发"},
                {"tag_type": "系统订单标签", "tag_name": "未分配物流"},
            ],
            remark="已建单 ALS01781406025",
        )
        gateway = MockGateway(
            _page(
                [OrderRecord("103000000000000121", None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="typed-shipment-tag",
            )
        )
        store = RecordingQueue()

        result = await scan_shipment_candidates(
            gateway,
            store,
            "帐篷标发",
            page_size=20,
            dry_run=False,
            reconcile_missing=False,
        )

        assert result.state is ApiScanState.COMPLETE
        assert result.candidate_count == 1
        assert result.enqueued_count == 1
        assert store.upserts[0][0].tag_text == "帐篷标发"

    asyncio.run(run())


def test_shipment_audit_covers_exclusions_unknown_missing_manual_and_candidate() -> None:
    async def run() -> None:
        def payload(suffix: int, *, tag_name: str = "帐篷标发", remark: str = ""):
            return _official_customization_payload(
                f"1030000000000002{suffix:02d}",
                f"112-0000000-00002{suffix:02d}",
                order_tag=[
                    {"tag_type": "系统处理类型", "tag_name": "可合并订单"},
                    {"tag_type": "自定义订单标签", "tag_name": tag_name},
                    {"tag_type": "系统订单标签", "tag_name": "未分配物流"},
                ],
                remark=remark,
            )

        unmatched = payload(1, tag_name="其他业务标签", remark="已建单 ALS01781406021")
        old = payload(2, remark="已建单 ALS01781406022")
        old["global_payment_time"] = int((datetime.now() - timedelta(hours=97)).timestamp())
        unknown = payload(3, remark="已建单 ALS01781406023")
        unknown["global_payment_time"] = "not-a-payment-time"
        missing = payload(4, remark="")
        missing.pop("remark")
        manual = payload(5, remark="尚未生成物流单号")
        candidate = payload(6, remark="已建单 ALS01781406026")
        payloads = [unmatched, old, unknown, missing, manual, candidate]
        gateway = MockGateway(
            _page(
                [OrderRecord(str(item["global_order_no"]), None, item) for item in payloads],
                offset=0,
                length=20,
                total=len(payloads),
                request_id="shipment-audit-decisions",
            )
        )

        result = await scan_shipment_candidates(
            gateway,
            RecordingQueue(),
            "帐篷标发",
            page_size=20,
            dry_run=True,
            reconcile_missing=False,
        )

        audits = {item["platform_order_no"]: item for item in result.audit_decisions}
        assert result.state is ApiScanState.INCOMPLETE
        assert (audits["112-0000000-0000201"]["decision"], audits["112-0000000-0000201"]["reason_code"]) == (
            "excluded",
            "shipment_tag_not_matched",
        )
        assert (audits["112-0000000-0000202"]["decision"], audits["112-0000000-0000202"]["reason_code"]) == (
            "excluded",
            "payment_old",
        )
        assert (audits["112-0000000-0000203"]["decision"], audits["112-0000000-0000203"]["reason_code"]) == (
            "manual_review",
            "payment_unknown",
        )
        assert audits["112-0000000-0000204"]["reason_code"] == "missing_critical_fields"
        assert audits["112-0000000-0000204"]["missing_fields"] == ["customer_remark"]
        assert audits["112-0000000-0000205"]["reason_code"] == "missing_valid_logistics"
        assert (audits["112-0000000-0000206"]["decision"], audits["112-0000000-0000206"]["reason_code"]) == (
            "candidate",
            "eligible_dry_run",
        )
        assert audits["112-0000000-0000206"]["custom_tag_text"] == "帐篷标发"
        assert audits["112-0000000-0000206"]["items"][0]["quantity_status"] == "valid"
        encoded = json.dumps(result.audit_decisions, ensure_ascii=False)
        assert "可合并订单" not in encoded
        assert "未分配物流" not in encoded
        assert "尚未生成物流单号" not in encoded
        assert "已建单 ALS01781406026" not in encoded

    asyncio.run(run())


def test_shipment_audit_distinguishes_enqueued_and_duplicate_candidates() -> None:
    async def run() -> None:
        candidate_payload = _official_customization_payload(
            "103000000000000271",
            "112-0000000-0000271",
            order_tag=[{"tag_type": "自定义订单标签", "tag_name": "帐篷标发"}],
            remark="已建单 ALS01781406071",
        )
        duplicate_payload = _official_customization_payload(
            "103000000000000272",
            "112-0000000-0000272",
            order_tag=[{"tag_type": "自定义订单标签", "tag_name": "帐篷标发"}],
            remark="已建单 ALS01781406072",
        )
        gateway = MockGateway(
            _page(
                [
                    OrderRecord("103000000000000271", None, candidate_payload),
                    OrderRecord("103000000000000272", None, duplicate_payload),
                ],
                offset=0,
                length=20,
                total=2,
                request_id="shipment-queue-audit",
            )
        )
        store = MixedAuditQueue("112-0000000-0000272")

        result = await scan_shipment_candidates(
            gateway,
            store,
            "帐篷标发",
            page_size=20,
            dry_run=False,
            reconcile_missing=False,
        )

        audits = {item["platform_order_no"]: item for item in result.audit_decisions}
        assert result.state is ApiScanState.COMPLETE
        assert (audits["112-0000000-0000271"]["decision"], audits["112-0000000-0000271"]["reason_code"]) == (
            "candidate",
            "enqueued",
        )
        assert (audits["112-0000000-0000272"]["decision"], audits["112-0000000-0000272"]["reason_code"]) == (
            "duplicate",
            "queue_duplicate",
        )

    asyncio.run(run())


def test_shipment_scan_extracts_tag_and_remark_then_reconciles_only_complete_snapshot() -> None:
    async def run() -> None:
        payload = _shipment_payload()
        gateway = MockGateway(
            _page(
                [OrderRecord(None, None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="shipment-req",
            )
        )
        store = RecordingQueue()

        result = await scan_shipment_candidates(
            gateway,
            store,
            "自动标发",
            filters={"documented_pending_filter": "value"},
            page_size=20,
            dry_run=False,
        )

        assert result.state is ApiScanState.COMPLETE
        assert result.candidate_count == 1
        assert result.enqueued_count == 1
        assert store.upserts[0][0].logistics_no == "ALS01781406025"
        assert store.complete_calls[0][0] == {"103000000000000001"}
        assert result.manual_completed_count == 1
        assert gateway.calls[0]["filters"] == {"documented_pending_filter": "value"}

    asyncio.run(run())


def test_shipment_scan_excludes_orders_outside_exact_96_hour_window() -> None:
    async def run() -> None:
        payload = _shipment_payload()
        payload["paymentTime"] = (datetime.now() - timedelta(hours=97)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        gateway = MockGateway(
            _page(
                [OrderRecord(None, None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="shipment-old-payment",
            )
        )
        store = RecordingQueue()

        result = await scan_shipment_candidates(
            gateway,
            store,
            "自动标发",
            page_size=20,
            dry_run=False,
            reconcile_missing=False,
        )

        assert result.state is ApiScanState.COMPLETE
        assert result.eligible_row_count == 0
        assert result.candidate_count == 0
        assert store.upserts == []
        assert result.diagnostics[-1].code == "shipment_outside_96h_payment_window"

    asyncio.run(run())


def test_missing_critical_shipment_field_forbids_missing_to_manual_completion() -> None:
    async def run() -> None:
        payload = _shipment_payload(include_remark=False)
        gateway = MockGateway(
            _page(
                [OrderRecord(None, None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="shipment-req",
            )
        )
        store = RecordingQueue()

        result = await scan_shipment_candidates(
            gateway,
            store,
            "自动标发",
            page_size=20,
            dry_run=False,
        )

        assert result.state is ApiScanState.INCOMPLETE
        assert result.missing_critical_field_count == 1
        assert store.complete_calls == []
        assert result.diagnostics[-1].missing_fields == ("customer_remark",)

    asyncio.run(run())


def test_incomplete_pagination_never_writes_or_reconciles() -> None:
    async def run() -> None:
        payload = _shipment_payload()
        gateway = MockGateway(
            _page(
                [OrderRecord(None, None, payload)],
                offset=0,
                length=1,
                total=2,
                request_id="shipment-req",
            )
        )
        store = RecordingQueue()

        result = await scan_shipment_candidates(
            gateway,
            store,
            "自动标发",
            page_size=1,
            max_pages=1,
            dry_run=False,
        )

        assert result.state is ApiScanState.INCOMPLETE
        assert store.upserts == []
        assert store.complete_calls == []
        assert result.audit_decisions[0]["decision"] == "manual_review"
        assert result.audit_decisions[0]["reason_code"] == "snapshot_incomplete_no_write"
        assert result.pagination.diagnostics[-1].code == "maximum_pages_reached"

    asyncio.run(run())


def test_real_shipment_queue_store_receives_api_candidate_transactionally(tmp_path) -> None:
    async def run() -> None:
        payload = _shipment_payload()
        gateway = MockGateway(
            _page(
                [OrderRecord(None, None, payload)],
                offset=0,
                length=20,
                total=1,
                request_id="shipment-req",
            )
        )
        store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")

        result = await scan_shipment_candidates(
            gateway,
            store,
            "自动标发",
            page_size=20,
            dry_run=False,
            reconcile_missing=False,
        )

        assert result.state is ApiScanState.COMPLETE
        row = store.get_by_logistics_no("ALS01781406025")
        assert row is not None
        assert row["system_order_no"] == "103000000000000001"
        assert row["platform_order_no"] == "111-0000000-0000001"

    asyncio.run(run())


def test_results_and_diagnostics_never_repr_raw_payload_or_api_error_secrets() -> None:
    async def run() -> None:
        raw_payload = {
            "global_order_no": "103000000000000001",
            "order_number": "111-0000000-0000001",
            "receiver_tel": "+1 555 123 4567",
            "buyer_email": "buyer@example.com",
            "access_token": "top-secret-token",
            "customer_remark": "phone=15551234567 email=buyer@example.com",
        }
        pagination = await fetch_all_order_pages(
            MockGateway(
                _page(
                    [OrderRecord("103000000000000001", "111-0000000-0000001", raw_payload)],
                    offset=0,
                    length=20,
                    total=1,
                    request_id="safe-request-id",
                )
            ),
            page_size=20,
        )
        normalized = normalize_api_order_rows(pagination)

        safe_payload = redact_sensitive_payload(raw_payload)
        combined_repr = f"{pagination!r} {normalized!r} {safe_payload!r}"
        assert "buyer@example.com" not in combined_repr
        assert "+1 555 123 4567" not in combined_repr
        assert "top-secret-token" not in combined_repr

        failure = await fetch_all_order_pages(
            MockGateway(RuntimeError("token=top-secret-token email=buyer@example.com")),
            page_size=20,
        )
        assert failure.state is ApiScanState.FAILED
        assert "top-secret-token" not in repr(failure)
        assert "buyer@example.com" not in repr(failure)
        assert failure.diagnostics[0].error_type == "RuntimeError"

    asyncio.run(run())
