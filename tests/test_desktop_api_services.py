from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import erp_automation.application.desktop_services as desktop_services_module
from erp_automation.application.capabilities import CapabilityUnavailable
from erp_automation.application.desktop_services import DesktopApiServices
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.configuration import (
    EncryptedConfigurationStore,
    HostKeyAesGcmBackend,
)
from erp_automation.integrations.lingxing import APIResponse
from erp_automation.persistence import (
    CustomWorkflowStore,
    WorkflowPauseKind,
    WorkflowStageState,
)
from erp_automation.ui.models import (
    CapabilityPolicy,
    DesktopSettings,
    NOTIFICATION_SYNC_INCLUDE_DEFERRED_RETRIES_KEY,
)
from lingxing_automation.services.folder_builder import build_daily_folder
from lingxing_automation.products.catalog import PRODUCT_IDENTITY_CATALOG_VERSION
from shipment_automation.config import SHIPMENT_TAG_NAME
from shipment_automation.models import (
    LOGISTICS_CANCELLED,
    LOGISTICS_PENDING,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.alibaba_ordering import ProductCategory
from shipment_automation.alibaba_product_classification import (
    classify_order_product,
)
from shipment_automation.notification_store import ShipmentNotificationStore
from shipment_automation.queue_store import ShipmentQueueStore


class RecordingClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def list_orders(self, *, offset=0, length=500, **filters):
        self.calls.append({"offset": offset, "length": length, **filters})
        return APIResponse(
            code="0",
            message="操作成功",
            data={"total": len(self.rows), "list": self.rows[offset : offset + length]},
            request_id="request-safe-id",
            response_time=None,
            raw={},
        )

    async def aclose(self) -> None:
        self.closed = True


class IncompleteClient(RecordingClient):
    """Return one usable row while declaring a larger, incomplete snapshot."""

    async def list_orders(self, *, offset=0, length=500, **filters):
        self.calls.append({"offset": offset, "length": length, **filters})
        return APIResponse(
            code="0",
            message="操作成功",
            data={
                "total": len(self.rows) + 1,
                "list": self.rows[offset : offset + length],
            },
            request_id="request-incomplete-safe-id",
            response_time=None,
            raw={},
        )


class CancellationOverlapClient(RecordingClient):
    """Return one overlapping cancellation snapshot, then a stable retry."""

    def __init__(
        self,
        primary_rows: list[dict[str, Any]],
        cancellation_rows: list[dict[str, Any]],
    ) -> None:
        super().__init__(primary_rows)
        self.cancellation_rows = cancellation_rows
        self.cancellation_calls = 0

    async def list_orders(self, *, offset=0, length=500, **filters):
        self.calls.append({"offset": offset, "length": length, **filters})
        if "order_status" in filters:
            rows = self.rows[offset : offset + length]
            total = len(self.rows)
            request_id = "primary-stable"
        else:
            self.cancellation_calls += 1
            total = len(self.cancellation_rows)
            if self.cancellation_calls == 1:
                rows = self.cancellation_rows[:2]
            elif self.cancellation_calls == 2:
                rows = [self.cancellation_rows[1]]
            else:
                rows = self.cancellation_rows
            request_id = f"cancellation-{self.cancellation_calls}"
        return APIResponse(
            code="0",
            message="操作成功",
            data={"total": total, "list": rows},
            request_id=request_id,
            response_time=None,
            raw={},
        )


class DetailRecordingClient(RecordingClient):
    def __init__(self, rows: list[dict[str, Any]], detail_payload: dict[str, Any]) -> None:
        super().__init__(rows)
        self.detail_payload = detail_payload
        self.detail_calls: list[str] = []

    async def get_fbm_order_detail(self, order_number: str):
        self.detail_calls.append(order_number)
        return APIResponse(
            code="0",
            message="操作成功",
            data=dict(self.detail_payload),
            request_id="detail-request-safe-id",
            response_time=None,
            raw={},
        )


def _official_order(
    *,
    shipment: bool = False,
    platform_code: str = "10001",
    platform_order_no: str = "111-0000000-0000001",
) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "global_order_no": "103000000000000001",
        "global_payment_time": now,
        "status": 4,
        "logistics": "Standard",
        "customer_shipping_list": ["Standard"],
        "remark": f"已建单 ALS01781406025" if shipment else "",
        "order_tag": [{"tag_name": SHIPMENT_TAG_NAME}] if shipment else [],
        "item_info": [
            {
                "id": "item-1",
                "platform_order_no": platform_order_no,
                "product_no": "B0CRRGTPFH",
                "local_sku": "canopytents",
                "quantity": 1,
            }
        ],
        "platform_info": [
            {
                "platform_code": platform_code,
                "platform_order_no": platform_order_no,
                "payment_time": now,
            }
        ],
        "logistics_info": {},
    }


def _service(
    tmp_path: Path,
    client: RecordingClient,
) -> DesktopApiServices:
    async def factory(_settings: DesktopSettings):
        return client

    return DesktopApiServices(
        tmp_path,
        configuration_store=object(),  # dependency is unused by the injected factory
        policy_provider=lambda: CapabilityPolicy(emergency_stop_writes=True),
        client_factory=factory,
    )


def test_host_key_configuration_uses_workspace_token_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend = HostKeyAesGcmBackend(b"k" * 32)
    configuration_store = EncryptedConfigurationStore(
        tmp_path / "data" / "config.enc",
        backend=backend,
    )
    captured: dict[str, Any] = {}
    sentinel_client = object()

    async def fake_create_client(store, **kwargs):
        captured["store"] = store
        captured.update(kwargs)
        return sentinel_client

    monkeypatch.setattr(
        desktop_services_module,
        "create_lingxing_openapi_client",
        fake_create_client,
    )
    service = DesktopApiServices(
        tmp_path,
        configuration_store=configuration_store,
        policy_provider=CapabilityPolicy,
    )

    _gateway, client = asyncio.run(service.create_gateway(DesktopSettings()))

    assert client is sentinel_client
    assert captured["store"] is configuration_store
    assert captured["token_backend"] is backend
    assert captured["token_path"] == tmp_path / "data" / "local" / "lingxing-token.enc"
    assert captured["lock_path"] == tmp_path / "data" / "local" / "lingxing-token.lock"


def test_order_detail_lookup_resolves_platform_number_to_one_system_order(
    tmp_path,
) -> None:
    platform_order_no = "112-1537898-9215412"
    system_order_no = "103729383039790228"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    client = DetailRecordingClient(
        [row],
        {
            "order_number": system_order_no,
            "order_item": [{"platform_order_id": platform_order_no}],
        },
    )

    result = asyncio.run(
        _service(tmp_path, client).get_order_detail_payload(
            DesktopSettings(),
            platform_order_no,
        )
    )

    assert result.requested_order_no == platform_order_no
    assert result.system_order_no == system_order_no
    assert result.platform_order_no == platform_order_no
    assert classify_order_product(result.payload).category is ProductCategory.TENT
    assert client.calls == [
        {
            "offset": 0,
            "length": 200,
            "platform_order_nos": [platform_order_no],
        }
    ]
    assert client.detail_calls == [system_order_no]
    assert client.closed is True


def test_order_detail_lookup_enriches_direct_system_order_with_list_asin(
    tmp_path,
) -> None:
    platform_order_no = "112-1537898-9215412"
    system_order_no = "103729383039790228"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    row["item_info"][0]["product_no"] = "B0D6KZ7G88"
    client = DetailRecordingClient(
        [row],
        {
            "order_number": system_order_no,
            "order_item": [
                {
                    "platform_order_id": platform_order_no,
                    "sku": "10ft-Full-Wall",
                }
            ],
        },
    )

    result = asyncio.run(
        _service(tmp_path, client).get_order_detail_payload(
            DesktopSettings(),
            system_order_no,
        )
    )

    assert classify_order_product(result.payload).category is ProductCategory.TENT
    assert client.calls == [
        {
            "offset": 0,
            "length": 200,
            "platform_order_nos": [platform_order_no],
        }
    ]
    assert client.detail_calls == [system_order_no]
    assert client.closed is True


def test_order_detail_lookup_uses_list_amount_for_mixed_non_tent_category(
    tmp_path,
) -> None:
    platform_order_no = "112-1537898-9215499"
    system_order_no = "103729383039790299"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    row["amount_currency"] = "USD"
    row["item_info"] = [
        {
            "platform_order_no": platform_order_no,
            "product_no": "B0D1FZKVV7",
            "local_sku": "x-banner-24x63in",
            "sales_revenue_amount": "57.15",
        },
        {
            "platform_order_no": platform_order_no,
            "product_no": "B0DS22NHGT",
            "local_sku": "Feather-Flag-0.5x2m",
            "sales_revenue_amount": "69.54",
        },
    ]
    client = DetailRecordingClient(
        [row],
        {
            "order_number": system_order_no,
            "order_item": [
                {
                    "platform_order_id": platform_order_no,
                    "sku": "x-banner-24x63in",
                },
                {
                    "platform_order_id": platform_order_no,
                    "sku": "Feather-Flag-0.5x2m",
                },
            ],
        },
    )

    result = asyncio.run(
        _service(tmp_path, client).get_order_detail_payload(
            DesktopSettings(),
            system_order_no,
        )
    )
    classification = classify_order_product(result.payload)

    assert classification.category is ProductCategory.VINYL_BANNER
    assert classification.selected_sales_amount == Decimal("69.54")
    assert classification.selected_sales_currency == "USD"
    assert classification.selection_reason == "highest_sales_amount"
    assert client.closed is True


def test_customer_shipping_list_probe_reads_exact_sanitized_evidence(
    tmp_path,
) -> None:
    platform_order_no = "wc39715"
    system_order_no = "103728494714573824"
    row = _official_order(
        platform_code="10010",
        platform_order_no=platform_order_no,
    )
    row["global_order_no"] = system_order_no
    row["customer_shipping_list"] = ["Expedited"]
    row["logistics_info"] = {"logistics_type_name": "UPS-全程"}
    client = RecordingClient([row])

    result = asyncio.run(
        _service(tmp_path, client).probe_customer_shipping_list(
            DesktopSettings(),
            platform_order_no,
            system_order_no,
        )
    )

    assert result["status"] == "completed"
    assert result["matched_record_count"] == 1
    assert result["customer_shipping_service_present"] is True
    assert result["customer_shipping_service"] == "expedited"
    assert result["customer_shipping_service_raw_values"] == ["Expedited"]
    assert result["authoritative_field"] == "customer_shipping_list"
    assert result["external_write_calls"] == 0
    assert {
        "field": "customer_shipping_list",
        "value": "Expedited",
    } in result["shipping_field_candidates"]
    assert {
        "field": "logistics_type_name",
        "value": "UPS-全程",
    } in result["shipping_field_candidates"]
    assert client.closed is True


def test_customer_shipping_list_probe_reports_route_without_inference(
    tmp_path,
) -> None:
    platform_order_no = "wc40256"
    system_order_no = "103730129849888506"
    row = _official_order(
        platform_code="10010",
        platform_order_no=platform_order_no,
    )
    row["global_order_no"] = system_order_no
    row.pop("customer_shipping_list")
    row["logistics_info"] = {"logistics_type_name": "Fedex-专线尾程"}
    client = RecordingClient([row])

    result = asyncio.run(
        _service(tmp_path, client).probe_customer_shipping_list(
            DesktopSettings(),
            platform_order_no,
            system_order_no,
        )
    )

    assert result["status"] == "completed"
    assert result["customer_shipping_service_present"] is False
    assert result["customer_shipping_service"] == ""
    assert result["authoritative_field"] == ""
    assert result["external_write_calls"] == 0
    assert {
        "field": "logistics_type_name",
        "value": "Fedex-专线尾程",
    } in result["shipping_field_candidates"]


def test_order_detail_lookup_blocks_ambiguous_platform_number(tmp_path) -> None:
    platform_order_no = "112-1537898-9215412"
    first = _official_order(platform_order_no=platform_order_no)
    second = _official_order(platform_order_no=platform_order_no)
    first["global_order_no"] = "103729383039790228"
    second["global_order_no"] = "103729383039790229"
    client = DetailRecordingClient([first, second], {})

    with pytest.raises(CapabilityUnavailable, match="对应多个领星系统单号"):
        asyncio.run(
            _service(tmp_path, client).get_order_detail_payload(
                DesktopSettings(),
                platform_order_no,
            )
        )

    assert client.detail_calls == []
    assert client.closed is True


def test_order_detail_lookup_resolves_digits_only_platform_number(tmp_path) -> None:
    platform_order_no = "420630849235990416420600935898"
    system_order_no = "103729383039790228"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no

    class NumericPlatformClient(DetailRecordingClient):
        async def get_fbm_order_detail(self, order_number: str):
            self.detail_calls.append(order_number)
            if order_number == platform_order_no:
                from erp_automation.integrations.lingxing import LingxingAPIError

                raise LingxingAPIError(
                    "get_fbm_order_detail",
                    "1005001",
                    "order not found",
                    request_id="direct-request-safe-id",
                )
            return APIResponse(
                code="0",
                message="操作成功",
                data={
                    "order_number": system_order_no,
                    "order_item": [{"platform_order_id": platform_order_no}],
                },
                request_id="detail-request-safe-id",
                response_time=None,
                raw={},
            )

    client = NumericPlatformClient([row], {})

    result = asyncio.run(
        _service(tmp_path, client).get_order_detail_payload(
            DesktopSettings(),
            platform_order_no,
        )
    )

    assert result.system_order_no == system_order_no
    assert result.platform_order_no == platform_order_no
    assert client.detail_calls == [platform_order_no, system_order_no]


def test_order_detail_lookup_blocks_mismatched_system_identity(tmp_path) -> None:
    client = DetailRecordingClient(
        [],
        {"order_number": "103729383039790229"},
    )

    with pytest.raises(CapabilityUnavailable, match="系统单号与请求不一致"):
        asyncio.run(
            _service(tmp_path, client).get_order_detail_payload(
                DesktopSettings(),
                "103729383039790228",
            )
        )

    assert client.detail_calls == ["103729383039790228"]
    assert client.closed is True


def test_shipment_filter_windows_cover_thirty_china_calendar_days_without_payment_filter() -> None:
    china = timezone(timedelta(hours=8))
    current = datetime(2026, 7, 14, 20, 30, tzinfo=china)

    windows = DesktopApiServices._shipment_order_filters(current)

    assert len(windows) == 2
    assert windows[0]["start_time"] == int(
        datetime(2026, 6, 14, 0, 0, 0, tzinfo=china).timestamp()
    )
    assert windows[-1]["end_time"] == int(
        datetime(2026, 7, 14, 23, 59, 59, tzinfo=china).timestamp()
    )
    assert windows[1]["start_time"] == windows[0]["end_time"] - 1
    assert all(
        window["end_time"] - window["start_time"] <= 30 * 24 * 60 * 60
        for window in windows
    )
    assert all(window["date_type"] == "global_purchase_time" for window in windows)
    assert all(window["order_status"] == 4 for window in windows)
    assert all(window["include_delete"] is False for window in windows)
    assert all("platform_code" not in window for window in windows)
    assert all("global_payment_time" not in window.values() for window in windows)


def test_custom_scan_uses_documented_96_hour_filter_and_persists_visible_candidate(tmp_path) -> None:
    client = RecordingClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom.sqlite3",
    )

    payload = asyncio.run(
        service.scan_custom_orders(
            settings,
            {},
            task_id="desktop-custom-001",
            operator_name="Steven",
            operator_email="steven@billyprint.com",
        )
    )

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 1
    assert payload["payment_window_hours"] == 96
    assert payload["task_id"] == "desktop-custom-001"
    assert payload["error_id"] is None
    assert "跳过统计" in payload["message"]
    audit_path = Path(payload["audit_log_path"])
    assert audit_path == next(
        (tmp_path / "logs" / "custom_order_scan").rglob(
            "custom_order_scan_*_desktop-custom-001.json"
        )
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["scan_kind"] == "customization"
    assert audit["operator"] == {
        "name": "Steven",
        "email": "steven@billyprint.com",
    }
    assert audit["query_summary"]["date_type"] == "global_payment_time"
    assert audit["pagination"]["pages"][0]["request_id"] == "request-safe-id"
    assert audit["summary"]["candidate_count"] == 1
    assert audit["order_decisions"]
    assert client.calls[0]["date_type"] == "global_payment_time"
    assert client.calls[0]["order_status"] == 4
    assert client.calls[0]["platform_code"] == [10001]
    assert client.calls[0]["end_time"] - client.calls[0]["start_time"] == 96 * 3600 + 120
    assert len(client.calls) == 2
    assert "order_status" not in client.calls[1]
    assert client.calls[1]["platform_code"] == [10001]
    assert client.closed is True
    stored = CustomWorkflowStore(tmp_path / "data/custom.sqlite3").get_workflow(
        "111-0000000-0000001"
    )
    assert stored is not None
    assert stored["workflow_status"] == "pending"


def test_custom_scan_retains_missing_asin_and_promotes_it_after_detail_sync(
    tmp_path,
) -> None:
    platform_order_no = "111-9378399-8373017"
    system_order_no = "103000000000000117"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    row["item_info"][0].pop("product_no")
    row["item_info"][0]["local_sku"] = "Custom-Tent-Package-10x10"
    detail_payload = {
        "order_number": system_order_no,
        "order_item": [
            {
                "platform_order_id": platform_order_no,
                "MSKU": "Custom-Tent-Package-10x10",
                "quality": 2,
            }
        ],
    }
    client = DetailRecordingClient([row], detail_payload)
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-asin-sync.sqlite3",
    )

    first = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-asin-sync-001")
    )

    assert first["status"] == "completed"
    assert first["candidate_count"] == 0
    assert first["product_identity_pending_count"] == 1
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    pending = store.get_workflow(platform_order_no)
    assert pending is not None
    assert pending["workflow_status"] == "product_identity_pending"
    assert pending["product_identity_state"] == "product_identity_pending"
    assert store.list_active_scanned_workflows() == []
    assert [
        item["platform_order_no"]
        for item in store.list_product_identity_pending_workflows()
    ] == [platform_order_no]

    detail_payload["order_item"][0]["product_no"] = "B0DZ2W2QWK"
    second = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-asin-sync-002")
    )

    assert second["status"] == "completed"
    assert second["candidate_count"] == 1
    assert second["product_identity_pending_count"] == 0
    resolved = store.get_workflow(platform_order_no)
    assert resolved is not None
    assert resolved["workflow_status"] == "pending"
    assert resolved["product_type"] == "tent"
    assert resolved["product_identity_state"] == ""
    assert store.list_product_identity_pending_workflows() == []
    assert [item["event_type"] for item in store.history(platform_order_no)][-1] == (
        "api_product_identity_resolved"
    )


def test_custom_scan_removes_retained_identity_when_custom_tag_appears(
    tmp_path,
) -> None:
    platform_order_no = "111-9378399-8373019"
    system_order_no = "103000000000000119"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    row["item_info"][0].pop("product_no")
    row["item_info"][0]["local_sku"] = "Custom-Tent-Package-10x10"
    detail_payload = {
        "order_number": system_order_no,
        "order_item": [
            {
                "platform_order_id": platform_order_no,
                "MSKU": "Custom-Tent-Package-10x10",
                "quality": 1,
            }
        ],
    }
    client = DetailRecordingClient([row], detail_payload)
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-tag-exclusion.sqlite3",
    )

    first = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-tag-exclusion-001")
    )
    assert first["product_identity_pending_count"] == 1
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    assert [
        item["platform_order_no"]
        for item in store.list_product_identity_pending_workflows()
    ] == [platform_order_no]

    # Reproduce the state already written by the previous implementation for
    # the two production rows that were displayed as ASIN/tag conflicts.
    store.mutate_legacy_record(
        platform_order_no,
        lambda current: {
            **current,
            "workflow_status": "product_identity_tag_conflict",
            "product_identity_state": "product_identity_tag_conflict",
            "product_identity_status_text": "ASIN/标签冲突，等待人工复核",
            "product_identity_tag_text": "客户确认中",
        },
        event_type="test_seed_existing_tag_conflict",
        actor="test",
    )
    row["order_tag"] = [
        {
            "tag_type": "自定义订单标签",
            "tag_name": "客户确认中",
        }
    ]
    second = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-tag-exclusion-002")
    )

    assert second["status"] == "completed"
    assert second["candidate_count"] == 0
    assert second["product_identity_pending_count"] == 0
    assert second["custom_tag_excluded_count"] == 1
    assert second["custom_orders"] == []
    workflow = store.get_workflow(platform_order_no)
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert workflow["not_required_reason"] == "custom_order_tag_present"
    assert store.list_product_identity_pending_workflows() == []
    assert store.list_active_scanned_workflows() == []
    history = store.history(platform_order_no)
    assert history[-1]["event_type"] == "workflow_marked_not_required"
    assert json.loads(history[-1]["details_json"])["source"] == (
        "custom_tag_reconciliation"
    )
    assert client.detail_calls == [system_order_no]
    audit = json.loads(Path(second["audit_log_path"]).read_text(encoding="utf-8"))
    decision = next(
        item
        for item in audit["order_decisions"]
        if item["platform_order_no"] == platform_order_no
    )
    assert decision["decision"] == "not_required"
    assert decision["reason_code"] == "has_tag"


def test_custom_scan_persists_known_type_when_automation_rules_are_incomplete(
    tmp_path,
) -> None:
    platform_order_no = "111-9378399-8373118"
    system_order_no = "103000000000000218"
    row = _official_order(platform_order_no=platform_order_no)
    row["global_order_no"] = system_order_no
    row["item_info"][0]["product_no"] = "B0H36GPHVH"
    row["item_info"][0]["local_sku"] = "Custom-Pop-Up-Display"
    client = DetailRecordingClient(
        [row],
        {
            "order_number": system_order_no,
            "order_item": [
                {
                    "platform_order_id": platform_order_no,
                    "product_no": "B0H36GPHVH",
                    "MSKU": "Custom-Pop-Up-Display",
                    "quality": 1,
                }
            ],
        },
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-known-type.sqlite3",
    )

    result = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-known-type-001")
    )

    assert result["status"] == "completed"
    assert result["candidate_count"] == 0
    assert result["product_identity_pending_count"] == 1
    workflow = CustomWorkflowStore(
        tmp_path / settings.custom_state_path
    ).get_workflow(platform_order_no)
    assert workflow is not None
    assert workflow["product_type"] == "pop_up_displays"
    assert workflow["product_types"] == ["pop_up_displays"]
    assert workflow["product_identity_state"] == "product_identity_review"
    assert "规则不完整" in workflow["source_record"][
        "product_identity_status_text"
    ]


def test_custom_scan_retries_overlapping_buyer_cancel_snapshot_and_audits_attempts(
    tmp_path,
) -> None:
    primary = _official_order(platform_order_no="111-0000000-0000001")
    cancellation_rows: list[dict[str, Any]] = []
    for index in range(1, 4):
        row = _official_order(platform_order_no=f"112-0000000-000000{index}")
        row["global_order_no"] = f"10300000000000000{index}"
        row["item_info"][0]["id"] = f"item-{index}"
        cancellation_rows.append(row)
    client = CancellationOverlapClient([primary], cancellation_rows)
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-cancellation-retry.sqlite3",
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-cancellation-retry")
    )

    assert payload["status"] == "completed"
    assert payload["diagnostic_codes"] == ["snapshot_retry_recovered"]
    assert client.cancellation_calls == 3
    assert [
        call["offset"] for call in client.calls if "order_status" not in call
    ] == [0, 2, 0]
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["diagnostic_codes"] == ["snapshot_retry_recovered"]
    assert [page["retry_count"] for page in audit["pagination"]["pages"]] == [
        0,
        0,
        0,
        1,
    ]


def test_custom_scan_backfills_missing_product_type_without_resetting_workflow(tmp_path) -> None:
    client = RecordingClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-backfill.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        "111-0000000-0000001",
        lambda _old: {
            "platform_order_no": "111-0000000-0000001",
            "system_order_no": "103000000000000001",
            "contact_writeback_complete": True,
            "folder_complete": False,
            "workflow_status": "folder_pending",
        },
        event_type="legacy_imported",
        actor="migration",
    )
    error = (
        "文件夹生成失败：vinyl_banners_rule_missing_printed_sides"
        "（缺少规则：Printed Sides = missing）"
    )
    store.record_workflow_paused(
        "111-0000000-0000001",
        "folder",
        reason=error,
        result_status="vinyl_banners_rule_missing_printed_sides",
        pause_kind=WorkflowPauseKind.RETRYABLE_FAILURE,
        actor="desktop_worker",
    )
    before = store.get_workflow("111-0000000-0000001")
    assert before is not None
    before_stages = [dict(stage) for stage in before["stages"]]

    payloads = [
        asyncio.run(
            service.scan_custom_orders(
                settings,
                {},
                task_id=f"custom-backfill-{index:03d}",
            )
        )
        for index in (1, 2)
    ]

    assert [payload["status"] for payload in payloads] == ["completed", "completed"]
    after = store.get_workflow("111-0000000-0000001")
    assert after is not None
    assert after["product_type"] == "tent"
    assert after["workflow_status"] == "folder_pending"
    assert after["stages"] == before_stages
    summary = next(
        item
        for item in store.list_workflow_summaries()
        if item["platform_order_no"] == "111-0000000-0000001"
    )
    assert summary["last_error"] == error
    history = store.history("111-0000000-0000001")
    history_types = [row["event_type"] for row in history]
    assert "workflow_metadata_backfilled" in history_types
    assert history_types.count("api_candidate_metadata_refreshed") == 2
    refresh_events = [
        row for row in history if row["event_type"] == "api_candidate_metadata_refreshed"
    ]
    assert all(
        json.loads(row["details_json"])["stages_preserved"]
        for row in refresh_events
    )
    assert after["source_record"]["api_candidate_product_type"] == "tent"


def test_custom_scan_backfills_completed_historical_order_from_exact_detail(
    tmp_path,
) -> None:
    platform_order_no = "111-0000000-0000991"
    system_order_no = "103000000000000991"
    client = DetailRecordingClient(
        [],
        {
            "order_number": system_order_no,
            "order_item": [
                {
                    "platform_order_id": platform_order_no,
                    "product_no": "B0CRRGTPFH",
                    "MSKU": "Custom-Tent-Package-10x10",
                    "quality": 1,
                }
            ],
        },
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-history-backfill.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        platform_order_no,
        lambda _old: {
            "platform_order_no": platform_order_no,
            "system_order_no": system_order_no,
            "workflow_status": "completed",
            "contact_writeback_complete": True,
            "folder_complete": True,
        },
        event_type="legacy_imported",
        actor="migration",
    )
    before = store.get_workflow(platform_order_no)
    assert before is not None
    before_stages = [dict(stage) for stage in before["stages"]]

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-history-backfill-001")
    )

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 0
    assert payload["product_identity_pending_count"] == 0
    assert client.detail_calls == [system_order_no]
    after = store.get_workflow(platform_order_no)
    assert after is not None
    assert after["product_type"] == "tent"
    assert after["product_types"] == ["tent"]
    assert after["workflow_status"] == "completed"
    assert after["stages"] == before_stages
    assert after["source_record"]["product_identity_catalog_version"]


def test_failed_custom_history_detail_is_not_checkpointed(tmp_path) -> None:
    platform_order_no = "111-0000000-0000992"
    system_order_no = "103000000000000992"
    client = DetailRecordingClient(
        [],
        {
            "order_number": "103000000000009999",
            "order_item": [
                {
                    "platform_order_id": platform_order_no,
                    "product_no": "B0CRRGTPFH",
                    "MSKU": "Custom-Tent-Package-10x10",
                    "quality": 1,
                }
            ],
        },
    )
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-history-backfill-retry.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        platform_order_no,
        lambda _old: {
            "platform_order_no": platform_order_no,
            "system_order_no": system_order_no,
            "workflow_status": "completed",
            "contact_writeback_complete": True,
            "folder_complete": True,
        },
        event_type="legacy_imported",
        actor="migration",
    )

    payload = asyncio.run(
        _service(tmp_path, client).scan_custom_orders(
            settings,
            {},
            task_id="custom-history-backfill-retry",
        )
    )

    assert client.detail_calls == [system_order_no]
    assert "customization_product_identity_backfill_incomplete" in payload[
        "diagnostic_codes"
    ]
    after = store.get_workflow(platform_order_no)
    assert after is not None
    assert not after["product_type"]
    assert "product_identity_catalog_version" not in after["source_record"]
    assert "product_identity_backfill_checked" not in {
        event["event_type"] for event in store.history(platform_order_no)
    }


def test_custom_scan_reconciles_queued_buyer_cancel_order_to_not_required(tmp_path) -> None:
    order = _official_order(platform_order_no="114-9578255-9785802")
    order["global_order_no"] = "103722237001371149"
    order["order_tag"] = [
        {
            "tag_type": "系统处理类型",
            "tag_no": "3-33",
            "tag_name": "买家申请取消",
        }
    ]
    client = RecordingClient([order])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-cancel.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        "114-9578255-9785802",
        lambda _old: {
            "platform_order_no": "114-9578255-9785802",
            "system_order_no": "103722237001371149",
            "product_type": "car_magnet",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-cancel-001")
    )

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 0
    assert payload["buyer_cancel_detected_count"] == 1
    assert payload["buyer_cancel_reconciled_count"] == 1
    assert "改为不需要" in payload["message"]
    workflow = store.get_workflow("114-9578255-9785802")
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert "114-9578255-9785802" in store.processed_platform_orders()
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    decisions = [
        item
        for item in audit["order_decisions"]
        if item["platform_order_no"] == "114-9578255-9785802"
    ]
    assert any(item["decision"] == "not_required" for item in decisions)
    assert all(item["reason_code"] == "buyer_cancel_requested" for item in decisions)
    assert len(client.calls) == 2
    assert client.calls[0]["order_status"] == 4
    assert "order_status" not in client.calls[1]


def test_custom_scan_reactivates_cleared_buyer_cancel_after_two_complete_tasks(
    tmp_path,
) -> None:
    order_no = "701-4689510-2891447"
    client = RecordingClient([_official_order(platform_order_no=order_no)])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-cancel-reactivation.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103000000000000001",
            "product_type": "tent",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.mark_workflows_not_required(
        [order_no],
        reason="领星订单状态显示买家申请取消。",
        actor="api_scanner",
    )

    first = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-reactivate-001")
    )

    assert first["status"] == "completed"
    assert first["candidate_count"] == 0
    assert first["buyer_cancel_clear_observed_count"] == 1
    assert first["buyer_cancel_reactivated_count"] == 0
    assert "等待下一次完整扫描确认" in first["message"]
    waiting = store.get_workflow(order_no)
    assert waiting is not None
    assert waiting["workflow_status"] == "not_required"
    assert waiting["buyer_cancel_clear_streak"] == 1
    first_audit = json.loads(
        Path(first["audit_log_path"]).read_text(encoding="utf-8")
    )
    assert first_audit["summary"]["buyer_cancel_clear_observed_count"] == 1
    assert any(
        decision["reason_code"] == "buyer_cancel_clear_observed"
        for decision in first_audit["order_decisions"]
    )

    second = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-reactivate-002")
    )

    assert second["status"] == "completed"
    assert second["candidate_count"] == 0
    assert second["buyer_cancel_clear_observed_count"] == 0
    assert second["buyer_cancel_reactivated_count"] == 1
    assert "取消申请已撤销，1 张订单已重新入队" in second["message"]
    restored = store.get_workflow(order_no)
    assert restored is not None
    assert restored["workflow_status"] == "pending"
    assert restored["processed_at"] is None
    assert order_no not in store.processed_platform_orders()
    second_audit = json.loads(
        Path(second["audit_log_path"]).read_text(encoding="utf-8")
    )
    assert second_audit["summary"]["buyer_cancel_reactivated_count"] == 1
    assert any(
        decision["reason_code"] == "buyer_cancel_request_cleared_reactivated"
        for decision in second_audit["order_decisions"]
    )
    assert len(client.calls) == 4


def test_incomplete_custom_scan_invalidates_buyer_cancel_clear_confirmation(
    tmp_path,
) -> None:
    order_no = "701-4689510-2891447"
    client = IncompleteClient([_official_order(platform_order_no=order_no)])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-cancel-reset.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103000000000000001",
            "product_type": "tent",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.mark_workflows_not_required(
        [order_no],
        reason="领星订单状态显示买家申请取消。",
        actor="api_scanner",
    )
    store.reconcile_buyer_cancel_reactivation(
        scan_id="complete-before-failure",
        eligible_order_nos=[order_no],
        currently_cancelled_order_nos=[],
        snapshots_complete=True,
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-reactivate-incomplete")
    )

    assert payload["status"] == "incomplete"
    assert payload["buyer_cancel_clear_observed_count"] == 0
    assert payload["buyer_cancel_reactivated_count"] == 0
    assert payload["buyer_cancel_clear_reset_count"] == 1
    workflow = store.get_workflow(order_no)
    assert workflow is not None
    assert workflow["workflow_status"] == "not_required"
    assert workflow["buyer_cancel_clear_streak"] == 0
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["buyer_cancel_clear_reset_count"] == 1
    assert any(
        decision["reason_code"] == "buyer_cancel_clear_reset"
        for decision in audit["order_decisions"]
    )


def test_custom_scan_reconciles_missing_candidates_from_order_folders(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        desktop_services_module.os,
        "walk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network order folders must not be recursively walked")
        ),
    )
    client = RecordingClient([])
    service = _service(tmp_path, client)
    folder_root = tmp_path / "orders"
    found_order = "111-0000000-0000201"
    absent_order = "112-0000000-0000202"
    build_daily_folder(folder_root, datetime.now(timezone.utc).date()).joinpath(
        f"{found_order}+1个测试产品"
    ).mkdir(parents=True)
    settings = DesktopSettings(
        folder_root=str(folder_root),
        custom_state_path="data/custom-folder-reconcile.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    seen_at = datetime.now(timezone.utc).isoformat()
    for order_no, system_order_no in (
        (found_order, "103700000000000201"),
        (absent_order, "103700000000000202"),
    ):
        store.mutate_legacy_record(
            order_no,
            lambda _old, order_no=order_no, system_order_no=system_order_no: {
                "platform_order_no": order_no,
                "system_order_no": system_order_no,
                "last_seen_at": seen_at,
                "workflow_status": "pending",
            },
            event_type="api_candidate_seen",
            actor="api_scanner",
        )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-folder-reconcile-001")
    )

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 0
    assert payload["missing_candidate_count"] == 2
    assert payload["folder_reconciled_completed_count"] == 1
    assert payload["folder_reconciled_pending_count"] == 1
    assert payload["folder_reconciliation_changed_count"] == 1
    assert payload["folder_reconciliation_state"] == "complete"
    assert store.get_workflow(found_order)["workflow_status"] == "completed"
    assert store.get_workflow(absent_order)["workflow_status"] == "pending"

    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["missing_candidate_count"] == 2
    assert audit["summary"]["folder_reconciled_completed_count"] == 1
    decisions = {
        item["platform_order_no"]: item
        for item in audit["order_decisions"]
        if item["reason_code"].startswith("missing_candidate_folder_")
    }
    assert decisions[found_order]["decision"] == "completed"
    assert decisions[found_order]["matched"] is True
    assert decisions[absent_order]["decision"] == "pending"
    assert decisions[absent_order]["matched"] is False


def test_custom_scan_preserves_error_order_without_querying_folder(tmp_path) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "disconnected-order-drive"),
        custom_state_path="data/custom-folder-error-preserved.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    order_no = "113-0000000-0000205"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000205",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )
    store.set_stage_state(
        order_no,
        "contact",
        WorkflowStageState.PENDING,
        reason="联系方式保存后读回失败",
        actor="desktop_worker",
        result_status="readback_failed",
        last_error="必须由用户处理的原始错误",
    )
    before = store.get_workflow(order_no)
    history_before = store.history(order_no)

    first = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-error-preserved-001")
    )
    second = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-error-preserved-002")
    )

    for payload in (first, second):
        assert payload["status"] == "completed"
        assert payload["missing_candidate_count"] == 1
        assert payload["folder_reconciled_completed_count"] == 0
        assert payload["folder_reconciled_pending_count"] == 0
        assert payload["folder_reconciliation_error_preserved_count"] == 1
        assert payload["folder_reconciliation_state"] == "complete"
        assert "保留报错 1" in payload["message"]
        audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
        assert audit["summary"]["folder_reconciliation_error_preserved_count"] == 1
        decisions = [
            item
            for item in audit["order_decisions"]
            if item["reason_code"] == "missing_candidate_existing_error_preserved"
        ]
        assert len(decisions) == 1
        assert decisions[0]["platform_order_no"] == order_no
        assert "必须由用户处理的原始错误" not in json.dumps(audit, ensure_ascii=False)

    assert store.get_workflow(order_no) == before
    assert store.history(order_no) == history_before


def test_custom_scan_does_not_treat_unavailable_folder_root_as_absent(tmp_path) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "disconnected-order-drive"),
        custom_state_path="data/custom-folder-unavailable.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    order_no = "113-0000000-0000203"
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000203",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-folder-unavailable-001")
    )

    assert payload["status"] == "incomplete"
    assert payload["missing_candidate_count"] == 1
    assert payload["folder_reconciliation_state"] == "unavailable"
    assert "missing_candidate_folder_root_unavailable" in payload["diagnostic_codes"]
    assert store.get_workflow(order_no)["workflow_status"] == "pending"
    assert not any(
        event["event_type"] == "workflow_reconciled_from_folder"
        for event in store.history(order_no)
    )


def test_custom_scan_requires_complete_snapshots_before_folder_reconciliation(tmp_path) -> None:
    client = IncompleteClient([])
    service = _service(tmp_path, client)
    folder_root = tmp_path / "orders"
    order_no = "114-0000000-0000204"
    build_daily_folder(folder_root, datetime.now(timezone.utc).date()).joinpath(
        f"{order_no}+1个测试产品"
    ).mkdir(parents=True)
    settings = DesktopSettings(
        folder_root=str(folder_root),
        custom_state_path="data/custom-folder-incomplete.sqlite3",
    )
    store = CustomWorkflowStore(tmp_path / settings.custom_state_path)
    store.mutate_legacy_record(
        order_no,
        lambda _old: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000204",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "workflow_status": "pending",
        },
        event_type="api_candidate_seen",
        actor="api_scanner",
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-folder-incomplete-001")
    )

    assert payload["status"] == "incomplete"
    assert payload["folder_reconciliation_state"] == "skipped_incomplete_snapshot"
    assert (
        "missing_candidate_folder_reconciliation_snapshot_incomplete"
        in payload["diagnostic_codes"]
    )
    assert store.get_workflow(order_no)["workflow_status"] == "pending"


def test_shipment_scan_writes_queue_and_closes_api_client(tmp_path) -> None:
    client = RecordingClient(
        [
            _official_order(
                shipment=True,
                platform_code="10010",
                platform_order_no="wc39877",
            )
        ]
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment.sqlite3",
    )

    payload = asyncio.run(
        service.scan_shipments(
            settings,
            {},
            operator_name="Steven",
            operator_email="steven@billyprint.com",
        )
    )

    assert payload["status"] == "completed"
    assert payload["enqueued_count"] == 1
    assert payload["task_id"].startswith("shipment-")
    assert payload["queue_total_count"] == 1
    assert payload["api_order_count"] == 2
    assert payload["deduplicated_order_count"] == 1
    assert payload["evaluable_row_count"] == 1
    assert payload["eligible_row_count"] == 1
    assert payload["tagged_row_count"] == 1
    assert payload["window_count"] == 2
    assert payload["email_preview_backfill_count"] == 0
    assert payload["alibaba_logistics_execution"] == "client_visible_browser_required"
    assert payload["alibaba_logistics_followup_pending"] is True
    assert payload["logistics_query_count"] == 0
    assert "本机可见 Chrome" in payload["message"]
    assert "当前队列共 1 个" in payload["message"]
    assert "96 小时" not in payload["message"]
    assert "新增为 0" not in payload["message"]
    shipment_audit_path = Path(payload["audit_log_path"])
    assert shipment_audit_path.parent.parent.name == "shipment_scan"
    assert shipment_audit_path.name.startswith("shipment_scan_")
    shipment_audit = json.loads(shipment_audit_path.read_text(encoding="utf-8"))
    assert shipment_audit["scan_kind"] == "shipment"
    assert shipment_audit["operator"] == {
        "name": "Steven",
        "email": "steven@billyprint.com",
    }
    assert shipment_audit["summary"]["queue_total_count"] == 1
    assert shipment_audit["summary"]["enqueued_count"] == 1
    assert shipment_audit["summary"]["order_count"] == 2
    assert shipment_audit["summary"]["deduplicated_order_count"] == 1
    assert shipment_audit["summary"]["evaluable_row_count"] == 1
    assert shipment_audit["summary"]["window_count"] == 2
    assert (
        shipment_audit["summary"]["alibaba_logistics_execution"]
        == "client_visible_browser_required"
    )
    assert shipment_audit["summary"]["logistics_query_count"] == 0
    assert "payment_window_hours" not in shipment_audit["summary"]
    assert shipment_audit["pagination"]["page_count"] == 2
    assert len(client.calls) == 2
    assert all(call["date_type"] == "global_purchase_time" for call in client.calls)
    assert all(call["order_status"] == 4 for call in client.calls)
    assert all(call["include_delete"] is False for call in client.calls)
    assert all("platform_code" not in call for call in client.calls)
    assert all(
        call["end_time"] - call["start_time"] <= 30 * 24 * 60 * 60
        for call in client.calls
    )
    assert client.calls[1]["start_time"] == client.calls[0]["end_time"] - 1
    assert client.closed is True
    stored = ShipmentQueueStore(tmp_path / "data/shipment.sqlite3").get_by_logistics_no(
        "ALS01781406025"
    )
    assert stored is not None
    assert stored["platform_order_no"] == "wc39877"


def test_shipment_scan_exactly_refreshes_new_als_after_old_logistics_closed(
    tmp_path,
) -> None:
    platform_order_no = "113-2813644-2307447"
    system_order_no = "103735738141701904"
    old_logistics_no = "ALS01920294001"
    new_logistics_no = "ALS01922616942"

    queue_path = tmp_path / "data/shipment-closed-replacement.sqlite3"
    queue = ShipmentQueueStore(queue_path)
    queue.upsert_candidate(
        ShipmentCandidate(
            system_order_no=system_order_no,
            platform_order_no=platform_order_no,
            logistics_no=old_logistics_no,
            shipment_tag_name=SHIPMENT_TAG_NAME,
            tag_text=SHIPMENT_TAG_NAME,
            customer_remark=f"已建单 {old_logistics_no}",
            customer_shipping_service="standard",
        )
    )
    queue.complete_logistics_attempt(
        old_logistics_no,
        LogisticsDetail(
            logistics_no=old_logistics_no,
            status_text="订单关闭",
        ),
        state=LOGISTICS_CANCELLED,
        last_error="阿里物流订单已取消：订单关闭",
    )

    replacement_row = _official_order(
        shipment=True,
        platform_order_no=platform_order_no,
    )
    replacement_row["global_order_no"] = system_order_no
    replacement_row["remark"] = f"{new_logistics_no} 8.25 发出"
    replacement_row["status"] = 7

    class ClosedReplacementClient(RecordingClient):
        async def list_orders(self, *, offset=0, length=500, **filters):
            self.calls.append({"offset": offset, "length": length, **filters})
            rows = (
                [replacement_row]
                if filters.get("platform_order_nos") == [platform_order_no]
                else []
            )
            return APIResponse(
                code="0",
                message="操作成功",
                data={
                    "total": len(rows),
                    "list": rows[offset : offset + length],
                },
                request_id=(
                    "closed-replacement-exact"
                    if rows
                    else "pending-review-empty"
                ),
                response_time=None,
                raw={},
            )

    client = ClosedReplacementClient([])
    settings = DesktopSettings(
        queue_path="data/shipment-closed-replacement.sqlite3",
    )

    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))

    assert payload["status"] == "completed"
    assert payload["cancelled_logistics_refresh"]["target_count"] == 1
    assert payload["cancelled_logistics_refresh"]["updated_target_count"] == 1
    assert "切换到备注中的新 ALS" in payload["message"]
    refreshed = ShipmentQueueStore(queue_path)
    assert refreshed.get_by_logistics_no(old_logistics_no) is None
    current = refreshed.get_by_logistics_no(new_logistics_no)
    assert current["logistics_state"] == LOGISTICS_PENDING
    assert current["logistics_last_error"] is None
    assert any(
        call.get("platform_order_nos") == [platform_order_no]
        for call in client.calls
    )
    assert client.closed is True


def test_shipment_scan_uses_the_tag_saved_in_desktop_settings(tmp_path) -> None:
    configured_tag = "客户待标发"
    row = _official_order(shipment=False)
    row["order_tag"] = [{"tag_name": configured_tag}]
    row["remark"] = "已建单 ALS01781406025"
    client = RecordingClient([row])
    settings = DesktopSettings(
        queue_path="data/shipment-custom-tag.sqlite3",
        shipment_tag_name=configured_tag,
    )

    payload = asyncio.run(
        _service(tmp_path, client).scan_shipments(settings, {})
    )

    assert payload["enqueued_count"] == 1
    stored = ShipmentQueueStore(
        tmp_path / settings.queue_path
    ).get_by_logistics_no("ALS01781406025")
    assert stored is not None
    assert stored["shipment_tag_name"] == configured_tag


def test_shipment_scan_repairs_historical_customer_shipping_service_pollution(
    tmp_path,
) -> None:
    queue_path = tmp_path / "data/shipment-service-backfill.sqlite3"
    store = ShipmentQueueStore(queue_path)
    candidate = ShipmentCandidate(
        system_order_no="103734710136652579",
        platform_order_no="112-4851688-6178611",
        logistics_no="ALS-SERVICE-BACKFILL-001",
        shipment_tag_name=SHIPMENT_TAG_NAME,
        tag_text=SHIPMENT_TAG_NAME,
        product_type="pop_up_displays",
        customer_shipping_service="UPS-全程",
    )
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = '2020-01-01T00:00:00Z' "
            "WHERE logistics_no = ?",
            (candidate.logistics_no,),
        )
        conn.commit()
    client = DetailRecordingClient(
        [],
        {
            "global_order_no": candidate.system_order_no,
            "buyer_choose_express": "Standard",
            "platform_info": [
                {"platform_order_no": candidate.platform_order_no}
            ],
            "logistics_info": {"logistics_type_name": "UPS-全程"},
        },
    )
    settings = DesktopSettings(
        queue_path="data/shipment-service-backfill.sqlite3"
    )

    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))

    assert payload["status"] == "completed"
    assert payload["customer_shipping_service_backfill_target_count"] == 1
    assert payload["customer_shipping_service_backfill_updated_job_count"] == 1
    assert payload["customer_shipping_service_backfill_remaining_target_count"] == 0
    assert client.detail_calls == [candidate.system_order_no]
    repaired = store.get_by_logistics_no(candidate.logistics_no)
    assert repaired["customer_shipping_service"] == "standard"
    assert repaired["shipping_attention_notice"]


def test_shipment_scan_repairs_historical_service_from_exact_list_field(
    tmp_path,
) -> None:
    queue_path = tmp_path / "data/shipment-service-list-backfill.sqlite3"
    store = ShipmentQueueStore(queue_path)
    candidate = ShipmentCandidate(
        system_order_no="103000000000000001",
        platform_order_no="112-0000000-0000001",
        logistics_no="ALS-SERVICE-LIST-BACKFILL-001",
        shipment_tag_name=SHIPMENT_TAG_NAME,
        tag_text=SHIPMENT_TAG_NAME,
        product_type="pop_up_displays",
        customer_shipping_service="Fedex-专线尾程",
    )
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_jobs SET first_seen_at = '2020-01-01T00:00:00Z' "
            "WHERE logistics_no = ?",
            (candidate.logistics_no,),
        )
        conn.commit()
    list_row = _official_order(
        shipment=False,
        platform_order_no=candidate.platform_order_no,
    )
    list_row["customer_shipping_list"] = ["Expedited"]
    list_row["order_tag"] = [{"tag_name": SHIPMENT_TAG_NAME}]
    client = DetailRecordingClient(
        [list_row],
        {"logistics_type_name": "Fedex-专线尾程"},
    )
    settings = DesktopSettings(
        queue_path="data/shipment-service-list-backfill.sqlite3"
    )

    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))

    assert payload["status"] == "completed"
    assert payload["customer_shipping_service_backfill_target_count"] == 1
    assert payload["customer_shipping_service_backfill_updated_job_count"] == 1
    assert payload["customer_shipping_service_backfill_remaining_target_count"] == 0
    assert client.detail_calls == []
    assert any(
        call.get("platform_order_nos") == [candidate.platform_order_no]
        for call in client.calls
    )
    repaired = store.get_by_logistics_no(candidate.logistics_no)
    assert repaired["customer_shipping_service"] == "expedited"
    assert repaired["shipping_attention_notice"]


def test_shipment_scan_backfills_historical_product_type_without_changing_state(
    tmp_path,
) -> None:
    historical_system_order_no = "103000000000000099"
    historical_platform_order_no = "112-0000000-0000099"
    historical_logistics_no = "ALS01781406099"
    settings = DesktopSettings(queue_path="data/shipment-product-type.sqlite3")
    store = ShipmentQueueStore(tmp_path / settings.queue_path)
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no=historical_system_order_no,
            platform_order_no=historical_platform_order_no,
            logistics_no=historical_logistics_no,
            shipment_tag_name=SHIPMENT_TAG_NAME,
            tag_text=SHIPMENT_TAG_NAME,
            product_type="",
        )
    )
    historical = store.get_by_logistics_no(historical_logistics_no)
    assert historical is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_logistics
            SET state = 'READY', updated_at = '2026-08-01T00:00:00Z'
            WHERE job_id = ?
            """,
            (historical["job_id"],),
        )
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = 'DONE', checkpoint = 'OUTBOUNDED',
                updated_at = '2026-08-01T00:00:00Z'
            WHERE job_id = ?
            """,
            (historical["job_id"],),
        )
        conn.commit()
    before = store.get_by_logistics_no(historical_logistics_no)

    detail_payload = {
        "global_order_no": historical_system_order_no,
        "item_info": [
            {
                "platform_order_no": historical_platform_order_no,
                "product_no": "B0H36GPHVH",
                "local_sku": "display-parent",
                "quantity": 1,
            }
        ],
        "platform_info": [
            {"platform_order_no": historical_platform_order_no}
        ],
    }
    current_row = _official_order(
        shipment=True,
        platform_order_no="111-0000000-0000001",
    )
    client = DetailRecordingClient([current_row], detail_payload)

    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))
    after = ShipmentQueueStore(tmp_path / settings.queue_path).get_by_logistics_no(
        historical_logistics_no
    )

    assert client.detail_calls == [historical_system_order_no]
    assert payload["product_type_backfill_target_count"] == 1
    assert payload["product_type_backfill_resolved_job_count"] == 1
    assert payload["product_type_backfill_failed_target_count"] == 0
    assert after is not None
    assert after["product_type"] == "pop_up_displays"
    assert after["product_identity_catalog_version"]
    for field in (
        "identity_state",
        "logistics_state",
        "erp_state",
        "erp_checkpoint",
    ):
        assert after[field] == before[field]


def test_shipment_scan_automatically_rechecks_old_no_asin_history_from_list_api(
    tmp_path,
) -> None:
    platform_order_no = "112-8004970-0417042"
    target_system_order_no = "103731890217881093"
    asin_system_order_no = "103731985375571456"
    logistics_no = "ALS-HISTORICAL-LIST-ASIN"
    settings = DesktopSettings(queue_path="data/shipment-list-identity-history.sqlite3")
    store = ShipmentQueueStore(tmp_path / settings.queue_path)
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no=target_system_order_no,
            platform_order_no=platform_order_no,
            logistics_no=logistics_no,
            shipment_tag_name=SHIPMENT_TAG_NAME,
            tag_text=SHIPMENT_TAG_NAME,
            product_type="",
            customer_shipping_service="standard",
        )
    )
    store.apply_product_identity_backfill(
        [
            {
                "system_order_no": target_system_order_no,
                "platform_order_no": platform_order_no,
                "product_types": (),
                "observed_asins": (),
                "evidence_scope": "sibling_aggregate",
                "evidence_system_order_nos": (target_system_order_no,),
            }
        ],
        catalog_version="2026-08-18.5",
        run_id="old-no-asin-checkpoint",
    )
    historical = store.get_by_logistics_no(logistics_no)
    assert historical is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_logistics SET state = 'READY' WHERE job_id = ?",
            (historical["job_id"],),
        )
        conn.execute(
            "UPDATE shipment_erp SET state = 'DONE', checkpoint = 'OUTBOUNDED' "
            "WHERE job_id = ?",
            (historical["job_id"],),
        )
        conn.commit()
    before = store.get_by_logistics_no(logistics_no)

    target_row = _official_order(
        shipment=False,
        platform_order_no=platform_order_no,
    )
    target_row["global_order_no"] = target_system_order_no
    target_row["item_info"][0].pop("product_no")
    target_row["item_info"][0]["local_sku"] = "split-part-without-asin"
    asin_row = _official_order(
        shipment=False,
        platform_order_no=platform_order_no,
    )
    asin_row["global_order_no"] = asin_system_order_no
    asin_row["item_info"][0]["product_no"] = "B0DZ2W2QWK"
    asin_row["item_info"][0]["local_sku"] = "TENT-ROLLER-BAG-10X10-50MM"

    class HistoricalListIdentityClient(RecordingClient):
        async def list_orders(self, *, offset=0, length=500, **filters):
            self.calls.append({"offset": offset, "length": length, **filters})
            rows = (
                [target_row, asin_row]
                if filters.get("platform_order_nos") == [platform_order_no]
                else []
            )
            return APIResponse(
                code="0",
                message="操作成功",
                data={
                    "total": len(rows),
                    "list": rows[offset : offset + length],
                },
                request_id="historical-list-product-id",
                response_time=None,
                raw={},
            )

        async def get_fbm_order_detail(self, order_number: str):
            raise AssertionError(
                f"real list product_no must avoid detail fallback: {order_number}"
            )

    client = HistoricalListIdentityClient([])
    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))
    after = ShipmentQueueStore(tmp_path / settings.queue_path).get_by_logistics_no(
        logistics_no
    )

    assert payload["product_type_backfill_target_count"] == 1
    assert payload["product_type_backfill_resolved_job_count"] == 1
    assert after is not None
    assert after["product_type"] == "tent"
    assert after["product_identity_catalog_version"] == (
        PRODUCT_IDENTITY_CATALOG_VERSION
    )
    evidence = json.loads(after["product_identity_evidence_json"])
    assert evidence["observed_asins"] == ["B0DZ2W2QWK"]
    assert evidence["evidence_scope"] == "sibling_list_item"
    for field in (
        "identity_state",
        "logistics_state",
        "erp_state",
        "erp_checkpoint",
    ):
        assert after[field] == before[field]


def test_shipment_scan_backfills_completed_exact_sku_without_detail_api(
    tmp_path,
) -> None:
    class NoHistoricalDetailClient(RecordingClient):
        async def get_fbm_order_detail(self, order_number: str):
            raise AssertionError(
                f"completed exact SKU must not call detail API: {order_number}"
            )

    settings = DesktopSettings(queue_path="data/shipment-sku-backfill.sqlite3")
    store = ShipmentQueueStore(tmp_path / settings.queue_path)
    candidate = ShipmentCandidate(
        system_order_no="103000000000200001",
        platform_order_no="112-0703089-1217824",
        logistics_no="ALS-SKU-BACKFILL",
        shipment_tag_name=SHIPMENT_TAG_NAME,
        tag_text=SHIPMENT_TAG_NAME,
        sku_text="Car-Magnet-12x18in-2pcs",
        product_type="",
    )
    store.upsert_candidate(candidate)
    with store.connect() as conn:
        conn.execute(
            "UPDATE shipment_erp SET state = 'DONE', checkpoint = 'OUTBOUNDED'"
        )
        conn.commit()

    client = NoHistoricalDetailClient([_official_order(shipment=True)])
    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))
    repaired = ShipmentQueueStore(
        tmp_path / settings.queue_path
    ).get_by_logistics_no(candidate.logistics_no)

    assert payload["product_type_backfill_sku_target_count"] == 1
    assert payload["product_type_backfill_sku_resolved_job_count"] == 1
    assert payload["product_type_backfill_resolved_job_count"] == 1
    assert repaired is not None
    assert repaired["product_type"] == "car_magnet"
    assert "精确 SKU" in payload["message"]


def test_shipment_scan_drains_more_than_one_product_identity_batch(tmp_path) -> None:
    class BatchIdentityClient(RecordingClient):
        def __init__(self, rows, platforms_by_system):
            super().__init__(rows)
            self.platforms_by_system = platforms_by_system
            self.detail_calls: list[str] = []

        async def list_orders(self, *, offset=0, length=500, **filters):
            if "platform_order_nos" in filters:
                self.calls.append({"offset": offset, "length": length, **filters})
                return APIResponse(
                    code="0",
                    message="操作成功",
                    data={"total": 0, "list": []},
                    request_id="empty-exact-platform-list",
                    response_time=None,
                    raw={},
                )
            return await super().list_orders(
                offset=offset,
                length=length,
                **filters,
            )

        async def get_fbm_order_detail(self, order_number: str):
            self.detail_calls.append(order_number)
            platform_order_no = self.platforms_by_system[order_number]
            return APIResponse(
                code="0",
                message="操作成功",
                data={
                    "global_order_no": order_number,
                    "item_info": [
                        {
                            "platform_order_no": platform_order_no,
                            "product_no": "B0CRRGTPFH",
                            "local_sku": "known-tent",
                        }
                    ],
                    "platform_info": [
                        {"platform_order_no": platform_order_no}
                    ],
                },
                request_id=f"detail-{order_number}",
                response_time=None,
                raw={},
            )

    settings = DesktopSettings(queue_path="data/shipment-product-drain.sqlite3")
    store = ShipmentQueueStore(tmp_path / settings.queue_path)
    platforms_by_system: dict[str, str] = {}
    for index in range(30):
        system_order_no = f"1030000000001{index:03d}"
        platform_order_no = f"111-0000000-{index:07d}"
        platforms_by_system[system_order_no] = platform_order_no
        store.upsert_candidate(
            ShipmentCandidate(
                system_order_no=system_order_no,
                platform_order_no=platform_order_no,
                logistics_no=f"ALS-DRAIN-{index:03d}",
                shipment_tag_name=SHIPMENT_TAG_NAME,
                tag_text=SHIPMENT_TAG_NAME,
                product_type="",
            )
        )

    client = BatchIdentityClient(
        [_official_order(shipment=True)],
        platforms_by_system,
    )
    payload = asyncio.run(_service(tmp_path, client).scan_shipments(settings, {}))

    assert len(client.detail_calls) == 30
    assert payload["product_type_backfill_batch_count"] == 2
    assert payload["product_type_backfill_target_count"] == 30
    assert payload["product_type_backfill_resolved_job_count"] == 30
    assert payload["product_type_backfill_remaining_target_count"] == 0
    assert payload["product_type_backfill_deferred_target_count"] == 0
    assert client.closed is True
    assert all(
        store.get_by_logistics_no(f"ALS-DRAIN-{index:03d}")["product_type"]
        == "tent"
        for index in range(30)
    )


def test_notification_rescan_never_runs_alibaba_logistics(tmp_path) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-notifications.sqlite3",
    )
    ShipmentQueueStore(tmp_path / settings.queue_path).initialize()

    payload = asyncio.run(service.sync_shipment_notifications(settings, {}))

    assert payload["status"] == "completed"
    assert payload["alibaba_logistics_query_count"] == 0
    assert payload["external_provider_calls"] == 0
    assert payload["erp_write_calls"] == 0
    assert payload["notification_sync"]["eligible_order_count"] == 0
    assert "新增草稿 0" in payload["message"]
    assert "未发送邮件或短信" in payload["message"]
    assert client.calls
    assert all(call["date_type"] == "global_purchase_time" for call in client.calls)
    assert all(call["include_delete"] is False for call in client.calls)
    assert all("order_status" not in call for call in client.calls)
    assert client.closed is True


def test_notification_rescan_retains_discovery_error_detail_in_result_and_message(
    tmp_path,
) -> None:
    class _DiscoveryError(RuntimeError):
        request_id = "notification-discovery-request"
        status_code = 502
        code = "UPSTREAM_UNAVAILABLE"
        operation = "list_orders"

    class _DiscoveryErrorClient(RecordingClient):
        async def list_orders(self, *, offset=0, length=500, **filters):
            self.calls.append({"offset": offset, "length": length, **filters})
            raise _DiscoveryError("raw upstream response is sensitive")

    client = _DiscoveryErrorClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-notifications-discovery-error.sqlite3",
    )

    payload = asyncio.run(service.sync_shipment_notifications(settings, {}))

    report = payload["notification_sync"]
    assert payload["status"] == "completed_with_warnings"
    assert report["discovery_error_count"] == 1
    assert report["discovery_error_type"] == "_DiscoveryError"
    assert report["discovery_error_http_status"] == 502
    assert report["discovery_error_api_code"] == "UPSTREAM_UNAVAILABLE"
    assert report["discovery_error_request_id"] == (
        "notification-discovery-request"
    )
    assert report["discovery_error_id"] in payload["message"]
    assert "_DiscoveryError" in payload["message"]
    assert "HTTP 502" in payload["message"]
    assert "raw upstream response is sensitive" in str(payload)
    assert client.closed is True


def test_notification_rescan_skips_when_single_instance_lock_is_held(tmp_path) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-notifications-locked.sqlite3",
    )
    path = tmp_path / settings.queue_path
    ShipmentQueueStore(path).initialize()
    lock_store = ShipmentNotificationStore(path)
    assert lock_store.try_acquire_scan_lock("other-scan") is True

    try:
        payload = asyncio.run(
            service.sync_shipment_notifications(settings, {}, task_id="this-scan")
        )
    finally:
        lock_store.release_scan_lock("other-scan")

    assert payload["status"] == "completed_with_warnings"
    assert payload["notification_sync"]["scan_lock_busy_count"] == 1
    assert payload["external_provider_calls"] == 0
    assert payload["erp_write_calls"] == 0
    assert client.calls == []
    assert client.closed is False


def test_shipment_scan_defers_notification_compensation_and_server_alibaba(
    tmp_path,
    monkeypatch,
) -> None:
    phases: list[str] = []

    service = _service(tmp_path, RecordingClient([]))

    async def notification_sync(_settings, _configuration, **_kwargs):
        phases.append("notification_compensation")
        return {
            "status": "completed",
            "message": "notification sync complete",
            "notification_sync": {
                "eligible_order_count": 4,
                "new_draft_count": 1,
                "partial_logistics_order_count": 2,
                "waiting_logistics_order_count": 1,
                "unchanged_order_count": 1,
                "failed_order_count": 0,
            },
            "external_provider_calls": 0,
        }

    monkeypatch.setattr(service, "sync_shipment_notifications", notification_sync)
    settings = DesktopSettings(queue_path="data/shipment-manual-compensation.sqlite3")

    payload = asyncio.run(
        service.scan_shipments(settings, {}, task_id="shipment-manual-compensation")
    )

    assert phases == []
    assert payload["alibaba_logistics_execution"] == "client_visible_browser_required"
    assert payload["alibaba_logistics_followup_pending"] is True
    assert payload["notification_compensation_followup_pending"] is True
    assert payload["logistics_query_count"] == 0
    assert "notification_sync" not in payload
    assert "独立后台任务中增量执行" in payload["message"]


def test_notification_sync_passes_targeted_platform_scope(tmp_path, monkeypatch) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-notifications-targeted.sqlite3",
    )
    ShipmentQueueStore(tmp_path / settings.queue_path).initialize()
    observed: dict[str, Any] = {}

    async def fake_sync(_gateway, _store, _configuration, **kwargs):
        observed.update(kwargs)
        return {
            "eligible_order_count": 0,
            "package_update_count": 0,
            "notification_count": 0,
        }

    monkeypatch.setattr(
        "shipment_automation.notification_sync.sync_notification_drafts",
        fake_sync,
    )
    platforms = ("111-1234567-1234567",)

    payload = asyncio.run(
        service.sync_shipment_notifications(
            settings,
            {NOTIFICATION_SYNC_INCLUDE_DEFERRED_RETRIES_KEY: True},
            task_id="mark-task",
            platform_order_nos=platforms,
        )
    )

    assert observed["platform_order_nos"] == platforms
    assert observed["include_deferred_retries"] is True
    assert payload["external_provider_calls"] == 0
    assert client.closed is True


def test_shipment_zero_new_message_distinguishes_scan_from_existing_queue(tmp_path) -> None:
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-empty.sqlite3",
    )

    payload = asyncio.run(service.scan_shipments(settings, {}, task_id="shipment-zero"))

    assert payload["status"] == "completed"
    assert payload["enqueued_count"] == 0
    assert payload["evaluable_row_count"] == 0
    assert payload["eligible_row_count"] == 0
    assert "本次新增为 0" in payload["message"]
    assert "不代表当前队列为空" in payload["message"]


def test_tagged_missing_customer_shipping_service_defaults_when_detail_is_empty(
    tmp_path,
) -> None:
    row = _official_order(shipment=True)
    row.pop("customer_shipping_list")
    row.pop("logistics")
    client = DetailRecordingClient(
        [row],
        {
            "global_order_no": row["global_order_no"],
        },
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        queue_path="data/shipment-required-service.sqlite3",
    )

    payload = asyncio.run(
        service.scan_shipments(
            settings,
            {},
            task_id="shipment-required-service-missing",
        )
    )

    assert payload["status"] == "completed"
    assert payload["api_order_count"] == 2
    assert payload["deduplicated_order_count"] == 1
    assert payload["tagged_row_count"] == 1
    assert payload["evaluable_row_count"] == 1
    assert payload["missing_critical_field_count"] == 0
    assert payload["critical_error_count"] == 0
    assert payload["candidate_count"] == 1
    assert payload["enqueued_count"] == 1
    assert payload["customer_shipping_service_detail_target_count"] == 1
    assert payload["customer_shipping_service_detail_resolved_count"] == 1
    assert payload["customer_shipping_service_detail_unresolved_count"] == 0
    assert client.detail_calls == [row["global_order_no"]]

    queue_rows = ShipmentQueueStore(
        tmp_path / "data/shipment-required-service.sqlite3"
    ).list_all_jobs()
    assert len(queue_rows) == 1
    assert queue_rows[0]["platform_order_no"] == (
        row["platform_info"][0]["platform_order_no"]
    )
    assert queue_rows[0]["logistics_no"] == "ALS01781406025"
    assert queue_rows[0]["customer_shipping_service"] == "standard"
    assert not queue_rows[0].get("scan_issue_code")

    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["status"] == "completed"
    assert audit["summary"]["critical_error_count"] == 0
    assert audit["summary"]["customer_shipping_service_detail_target_count"] == 1
    assert audit["summary"]["customer_shipping_service_detail_resolved_count"] == 1
    assert audit["order_decisions"][0]["decision"] != "error"


def test_unknown_tagged_customer_shipping_service_is_queue_error_after_detail(
    tmp_path,
) -> None:
    row = _official_order(shipment=True)
    row["customer_shipping_list"] = ["UPS-全程"]
    row.pop("logistics")
    client = DetailRecordingClient(
        [row],
        {
            "global_order_no": row["global_order_no"],
            "logistics_info": {"logistics_type_name": "UPS-全程"},
        },
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        queue_path="data/shipment-unresolved-service.sqlite3",
    )

    payload = asyncio.run(
        service.scan_shipments(
            settings,
            {},
            task_id="shipment-required-service-unresolved",
        )
    )

    assert payload["status"] == "completed_with_warnings"
    assert payload["deduplicated_order_count"] == 1
    assert payload["tagged_row_count"] == 1
    assert payload["evaluable_row_count"] == 0
    assert payload["candidate_count"] == 0
    assert payload["enqueued_count"] == 0
    assert payload["missing_critical_field_count"] == 1
    assert payload["critical_error_count"] == 1
    assert payload["customer_shipping_service_detail_target_count"] == 1
    assert payload["customer_shipping_service_detail_resolved_count"] == 0
    assert payload["customer_shipping_service_detail_unresolved_count"] == 1
    assert client.detail_calls == [row["global_order_no"]]
    assert "shipment_required_fields_unavailable" in payload["diagnostic_codes"]
    assert "已直接显示在队列中" in payload["message"]
    assert "已执行精确列表重读" in payload["message"]
    assert "Amazon 必要时再执行详情补读" in payload["message"]
    assert "其他订单继续处理" in payload["message"]

    queue_rows = ShipmentQueueStore(
        tmp_path / "data/shipment-unresolved-service.sqlite3"
    ).list_all_jobs()
    assert len(queue_rows) == 1
    assert "未返回明确的客选物流字段" in queue_rows[0]["last_error"]

    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["status"] == "completed_with_warnings"
    assert audit["summary"]["critical_error_count"] == 1
    assert audit["summary"][
        "customer_shipping_service_detail_unresolved_count"
    ] == 1
    assert audit["order_decisions"][0]["decision"] == "error"
    assert audit["order_decisions"][0]["reason_code"] == (
        "required_customer_shipping_service_unavailable"
    )


def test_shipment_scan_keeps_due_logistics_for_local_client_when_lingxing_fails(
    tmp_path,
    monkeypatch,
) -> None:
    async def explode(*_args, **_kwargs):
        raise RuntimeError("temporary Lingxing failure")

    monkeypatch.setattr(desktop_services_module, "scan_shipment_candidates", explode)
    service = _service(tmp_path, RecordingClient([]))
    settings = DesktopSettings(queue_path="data/shipment-partial.sqlite3")

    payload = asyncio.run(
        service.scan_shipments(
            settings,
            {"alibaba.account": "configured"},
            task_id="shipment-partial",
        )
    )

    assert payload["status"] == "failed"
    assert payload["logistics_query_count"] == 0
    assert payload["logistics_ready_count"] == 0
    assert payload["alibaba_logistics_execution"] == "client_visible_browser_required"
    assert payload["alibaba_logistics_followup_pending"] is True
    assert "等待本机 Chrome 查询" in payload["message"]
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["logistics_query_count"] == 0
    assert audit["summary"]["logistics_ready_count"] == 0
    assert (
        audit["summary"]["alibaba_logistics_execution"]
        == "client_visible_browser_required"
    )
    assert audit["summary"]["diagnostic_codes"] == [
        "lingxing_scan_runtime_failure"
    ]
    assert audit["order_decisions"] == []


def test_successful_shipment_scan_does_not_build_email_previews_while_mail_is_disabled(
    tmp_path,
) -> None:
    queue_path = tmp_path / "data/shipment-email.sqlite3"
    store = ShipmentQueueStore(queue_path)
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no="103000000000000099",
            platform_order_no="112-0000000-0000099",
            logistics_no="ALS01781406099",
                shipment_tag_name=SHIPMENT_TAG_NAME,
                tag_text=SHIPMENT_TAG_NAME,
                receiver_email="buyer@example.com",
                product_type="tent",
        )
    )
    job = store.get_by_logistics_no("ALS01781406099")
    assert job is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_logistics
            SET state = 'READY', carrier_normalized = 'UPS',
                international_tracking_no = '1Z999', updated_at = '2026-07-14T00:00:00Z'
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = 'DONE', checkpoint = 'OUTBOUNDED',
                completion_source = 'AUTOMATION', updated_at = '2026-07-14T00:00:00Z'
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )

    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-email.sqlite3",
    )
    first = asyncio.run(
        _service(tmp_path, RecordingClient([])).scan_shipments(
            settings,
            {},
            task_id="shipment-email-backfill-1",
        )
    )
    second = asyncio.run(
        _service(tmp_path, RecordingClient([])).scan_shipments(
            settings,
            {},
            task_id="shipment-email-backfill-2",
        )
    )

    # Notification compensation is now independent from the scan's critical
    # path; mail-preview behavior remains disabled and idempotent.
    assert first["status"] == "completed"
    assert first["email_preview_backfill_count"] == 0
    assert second["email_preview_backfill_count"] == 0
    assert store.list_email_batches() == []


def test_shipment_scan_does_not_query_receiver_email_while_mail_is_disabled(
    tmp_path,
) -> None:
    queue_path = tmp_path / "data/shipment-email-repair.sqlite3"
    store = ShipmentQueueStore(queue_path)
    candidate = ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        shipment_tag_name=SHIPMENT_TAG_NAME,
        tag_text=SHIPMENT_TAG_NAME,
        receiver_email=None,
        product_type="tent",
    )
    store.upsert_candidate(candidate)
    job = store.get_by_logistics_no(candidate.logistics_no)
    assert job is not None
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_erp
            SET state = 'DONE', checkpoint = 'OUTBOUNDED',
                completion_source = 'AUTOMATION', updated_at = '2026-07-16T00:00:00Z'
            WHERE job_id = ?
            """,
            (job["job_id"],),
        )
    assert store.prepare_email_batches_with_count() == 1
    assert store.list_email_batches()[0].state == "BLOCKED"

    client = DetailRecordingClient(
        [],
        {
            "global_order_no": candidate.system_order_no,
            "buyer_email": "buyer@example.com",
        },
    )
    settings = DesktopSettings(queue_path="data/shipment-email-repair.sqlite3")
    payload = asyncio.run(
        _service(tmp_path, client).scan_shipments(
            settings,
            {},
            task_id="shipment-email-repair",
        )
    )

    assert client.detail_calls == []
    assert payload["receiver_email_backfill_count"] == 0
    assert payload["receiver_email_unresolved_count"] == 0
    assert payload["email_preview_backfill_count"] == 0
    batch = store.list_email_batches()[0]
    assert batch.state == "BLOCKED"
    assert batch.recipient_email is None
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["receiver_email_backfill_count"] == 0
    assert "buyer@example.com" not in json.dumps(audit, ensure_ascii=False)


def test_custom_order_factory_owns_client_inside_one_task_loop(tmp_path) -> None:
    client = RecordingClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        high_value_split_weight_kg=5,
    )

    async def run() -> None:
        async with service.custom_order_operations(settings, {}) as operations:
            assert isinstance(operations, LingxingCustomOrderApiOperations)
            assert operations.high_value_split_weight_threshold_g == 5000
            assert client.closed is False
        assert client.closed is True

    asyncio.run(run())


def test_incomplete_custom_snapshot_is_audited_but_not_persisted_or_actionable(tmp_path) -> None:
    client = IncompleteClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-incomplete.sqlite3",
    )

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-incomplete-001")
    )

    assert payload["status"] == "incomplete"
    assert payload["api_order_count"] == 1
    assert payload["custom_orders"] == []
    assert "未更新候选数据库" in payload["message"]
    assert (
        CustomWorkflowStore(tmp_path / "data/custom-incomplete.sqlite3").get_workflow(
            "111-0000000-0000001"
        )
        is None
    )
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["status"] == "incomplete"
    assert audit["summary"]["candidate_count"] == payload["candidate_count"]


def test_incomplete_shipment_snapshot_never_writes_queue(tmp_path) -> None:
    client = IncompleteClient(
        [
            _official_order(
                shipment=True,
                platform_code="10010",
                platform_order_no="wc-incomplete",
            )
        ]
    )
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment-incomplete.sqlite3",
    )

    payload = asyncio.run(
        service.scan_shipments(settings, {}, task_id="shipment-incomplete-001")
    )

    assert payload["status"] == "incomplete"
    assert payload["candidate_count"] == 1
    assert payload["enqueued_count"] == 0
    assert payload["queue_total_count"] == 0
    assert "未写入不完整快照中的候选" in payload["message"]
    assert (
        ShipmentQueueStore(
            tmp_path / "data/shipment-incomplete.sqlite3"
        ).get_by_logistics_no("ALS01781406025")
        is None
    )
    audit = json.loads(Path(payload["audit_log_path"]).read_text(encoding="utf-8"))
    assert audit["summary"]["status"] == "incomplete"
    assert audit["summary"]["enqueued_count"] == 0


@pytest.mark.parametrize(
    ("method_name", "scanner_name", "scan_kind"),
    [
        ("scan_custom_orders", "scan_customization_candidates", "customization"),
        ("scan_shipments", "scan_shipment_candidates", "shipment"),
    ],
)
def test_scan_runtime_exception_keeps_business_detail_only_in_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    scanner_name: str,
    scan_kind: str,
) -> None:
    token = "unlabelled-token-material-that-must-never-leak"
    email = "private-buyer@example.com"

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"token={token}; email={email}; address=123 Main Street")

    monkeypatch.setattr(desktop_services_module, scanner_name, explode)
    client = RecordingClient([])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom-failure.sqlite3",
        queue_path="data/shipment-failure.sqlite3",
    )
    task_id = f"desktop-{scan_kind}-failure"

    payload = asyncio.run(getattr(service, method_name)(settings, {}, task_id=task_id))

    serialized_payload = json.dumps(payload, ensure_ascii=False)
    assert payload["status"] == "failed"
    assert payload["task_id"] == task_id
    assert payload["error_id"]
    assert Path(payload["audit_log_path"]).is_file()
    assert Path(payload["audit_log_path"]).parent.parent.name == {
        "customization": "custom_order_scan",
        "shipment": "shipment_scan",
    }[scan_kind]
    if scan_kind == "shipment":
        assert "领星阶段失败" in payload["message"]
        assert "等待本机 Chrome 查询" in payload["message"]
        assert payload["alibaba_logistics_followup_pending"] is True
    else:
        assert "原始错误信息已隐藏" in payload["message"]
    assert token not in serialized_payload
    assert email not in serialized_payload
    assert "123 Main Street" not in serialized_payload
    assert client.closed is True

    audit_text = Path(payload["audit_log_path"]).read_text(encoding="utf-8")
    audit = json.loads(audit_text)
    assert audit["scan_kind"] == scan_kind
    assert audit["error_id"] == payload["error_id"]
    assert token not in audit_text
    assert email in audit_text
    assert "123 Main Street" in audit_text
    assert "token=<redacted>" in audit_text
