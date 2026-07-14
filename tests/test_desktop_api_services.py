from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from erp_automation.application.desktop_services import DesktopApiServices
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.integrations.lingxing import APIResponse
from erp_automation.persistence import CustomWorkflowStore
from erp_automation.ui.models import CapabilityPolicy, DesktopSettings
from shipment_automation.config import SHIPMENT_TAG_NAME
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


def _official_order(*, shipment: bool = False) -> dict[str, Any]:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "global_order_no": "103000000000000001",
        "global_payment_time": now,
        "status": 4,
        "remark": f"已建单 ALS01781406025" if shipment else "",
        "order_tag": [{"tag_name": SHIPMENT_TAG_NAME}] if shipment else [],
        "item_info": [
            {
                "id": "item-1",
                "platform_order_no": "111-0000000-0000001",
                "product_no": "B0CRRGTPFH",
                "local_sku": "canopytents",
                "quantity": 1,
            }
        ],
        "platform_info": [
            {
                "platform_code": "10001",
                "platform_order_no": "111-0000000-0000001",
                "payment_time": now,
            }
        ],
        "logistics_info": {},
    }


def _service(tmp_path: Path, client: RecordingClient) -> DesktopApiServices:
    async def factory(_settings: DesktopSettings):
        return client

    return DesktopApiServices(
        tmp_path,
        configuration_store=object(),  # dependency is unused by the injected factory
        policy_provider=lambda: CapabilityPolicy(emergency_stop_writes=True),
        client_factory=factory,
    )


def test_custom_scan_uses_documented_96_hour_filter_and_persists_visible_candidate(tmp_path) -> None:
    client = RecordingClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        custom_state_path="data/custom.sqlite3",
    )

    payload = asyncio.run(service.scan_custom_orders(settings, {}))

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 1
    assert payload["payment_window_hours"] == 96
    assert client.calls[0]["date_type"] == "global_payment_time"
    assert client.calls[0]["order_status"] == 4
    assert client.calls[0]["platform_code"] == [10001]
    assert client.calls[0]["end_time"] - client.calls[0]["start_time"] == 96 * 3600 + 120
    assert client.closed is True
    stored = CustomWorkflowStore(tmp_path / "data/custom.sqlite3").get_workflow(
        "111-0000000-0000001"
    )
    assert stored is not None
    assert stored["workflow_status"] == "pending"


def test_shipment_scan_writes_queue_and_closes_api_client(tmp_path) -> None:
    client = RecordingClient([_official_order(shipment=True)])
    service = _service(tmp_path, client)
    settings = DesktopSettings(
        folder_root=str(tmp_path / "orders"),
        queue_path="data/shipment.sqlite3",
    )

    payload = asyncio.run(service.scan_shipments(settings, {}))

    assert payload["status"] == "completed"
    assert payload["enqueued_count"] == 1
    assert client.closed is True
    stored = ShipmentQueueStore(tmp_path / "data/shipment.sqlite3").get_by_logistics_no(
        "ALS01781406025"
    )
    assert stored is not None
    assert stored["platform_order_no"] == "111-0000000-0000001"


def test_custom_order_factory_owns_client_inside_one_task_loop(tmp_path) -> None:
    client = RecordingClient([_official_order()])
    service = _service(tmp_path, client)
    settings = DesktopSettings(folder_root=str(tmp_path / "orders"))

    async def run() -> None:
        async with service.custom_order_operations(settings, {}) as operations:
            assert isinstance(operations, LingxingCustomOrderApiOperations)
            assert client.closed is False
        assert client.closed is True

    asyncio.run(run())
