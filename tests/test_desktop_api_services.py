from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
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
from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState
from erp_automation.ui.models import CapabilityPolicy, DesktopSettings
from lingxing_automation.services.folder_builder import build_daily_folder
from shipment_automation.config import SHIPMENT_TAG_NAME
from shipment_automation.models import ShipmentCandidate
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
    assert client.calls == [
        {
            "offset": 0,
            "length": 200,
            "platform_order_nos": [platform_order_no],
        }
    ]
    assert client.detail_calls == [system_order_no]
    assert client.closed is True


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
    before = store.get_workflow("111-0000000-0000001")
    assert before is not None
    before_stages = [dict(stage) for stage in before["stages"]]

    payload = asyncio.run(
        service.scan_custom_orders(settings, {}, task_id="custom-backfill-001")
    )

    assert payload["status"] == "completed"
    after = store.get_workflow("111-0000000-0000001")
    assert after is not None
    assert after["product_type"] == "tent"
    assert after["workflow_status"] == "folder_pending"
    assert after["stages"] == before_stages
    history_types = [
        row["event_type"] for row in store.history("111-0000000-0000001")[-2:]
    ]
    assert history_types == [
        "workflow_metadata_backfilled",
        "api_candidate_metadata_refreshed",
    ]
    assert after["source_record"]["api_candidate_product_type"] == "tent"


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
            {},
            task_id="mark-task",
            platform_order_nos=platforms,
        )
    )

    assert observed["platform_order_nos"] == platforms
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
