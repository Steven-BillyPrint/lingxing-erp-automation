from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import erp_automation.application.desktop_services as desktop_services_module
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

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="desktop-custom-001")
    )

    assert payload["status"] == "completed"
    assert payload["candidate_count"] == 1
    assert payload["payment_window_hours"] == 96
    assert payload["task_id"] == "desktop-custom-001"
    assert payload["error_id"] is None
    assert "跳过统计" in payload["message"]
    audit_path = Path(payload["audit_log_path"])
    assert audit_path == next((tmp_path / "logs" / "api_scan").rglob("desktop-custom-001.json"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["scan_kind"] == "customization"
    assert audit["query_summary"]["date_type"] == "global_payment_time"
    assert audit["pagination"]["pages"][0]["request_id"] == "request-safe-id"
    assert audit["summary"]["candidate_count"] == 1
    assert audit["order_decisions"]
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

    payload = asyncio.run(service.scan_shipments(settings, {}))

    assert payload["status"] == "completed"
    assert payload["enqueued_count"] == 1
    assert payload["task_id"].startswith("shipment-")
    assert payload["queue_total_count"] == 1
    assert payload["api_order_count"] == 1
    assert payload["eligible_row_count"] == 1
    assert payload["tagged_row_count"] == 1
    assert "当前队列共 1 个" in payload["message"]
    assert "新增为 0" not in payload["message"]
    shipment_audit_path = Path(payload["audit_log_path"])
    shipment_audit = json.loads(shipment_audit_path.read_text(encoding="utf-8"))
    assert shipment_audit["scan_kind"] == "shipment"
    assert shipment_audit["summary"]["queue_total_count"] == 1
    assert shipment_audit["summary"]["enqueued_count"] == 1
    assert shipment_audit["summary"]["eligible_row_count"] == 1
    assert shipment_audit["pagination"]["page_count"] == 1
    assert client.calls[0]["date_type"] == "global_payment_time"
    assert client.calls[0]["order_status"] == 4
    assert client.calls[0]["include_delete"] is False
    assert "platform_code" not in client.calls[0]
    assert client.calls[0]["end_time"] - client.calls[0]["start_time"] == 96 * 3600 + 120
    assert client.closed is True
    stored = ShipmentQueueStore(tmp_path / "data/shipment.sqlite3").get_by_logistics_no(
        "ALS01781406025"
    )
    assert stored is not None
    assert stored["platform_order_no"] == "wc39877"


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
    assert payload["eligible_row_count"] == 0
    assert "本次新增为 0" in payload["message"]
    assert "不代表当前队列为空" in payload["message"]


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
def test_scan_runtime_exception_returns_only_safe_payload_and_audit(
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
    assert email not in audit_text
    assert "123 Main Street" not in audit_text
