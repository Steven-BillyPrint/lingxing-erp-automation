from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from erp_automation.application.api_scanners import (
    ApiScanState,
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


def test_incomplete_pagination_can_enqueue_seen_candidate_but_never_reconciles_missing() -> None:
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
        assert len(store.upserts) == 1
        assert store.complete_calls == []
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
