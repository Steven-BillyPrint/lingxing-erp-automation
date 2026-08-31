from __future__ import annotations

import json
import time

from erp_automation.application.queue_queries import (
    paginate_custom_order_rows,
    paginate_shipment_rows,
)
from erp_automation.contracts.models import (
    Capability,
    CustomOrderPage,
    CustomOrderRow,
    DatasetSummary,
    DesktopSnapshot,
    QueueFacets,
    ShipmentPage,
    ShipmentRow,
    TaskArea,
    TaskCommand,
)
from erp_automation.coordination.codec import (
    decode_custom_order_page,
    decode_shipment_page,
    decode_snapshot,
    to_jsonable,
)
from erp_automation.coordination.service import _result_type
from erp_automation.coordination.access import OperatorIdentity
from erp_automation.coordination.service import (
    CoordinatedControllerService,
    CoordinationSettings,
)
from erp_automation.coordination.store import CoordinationStore
from erp_automation.persistence import CustomWorkflowStore
from erp_automation.ui.controller import InMemoryBackgroundTaskController


def test_custom_workflow_store_pages_before_transport_and_applies_task_overlay(
    tmp_path,
) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps(
            {
                "version": 3,
                "orders": {
                    f"111-1111111-111110{index}": {
                        "platform_order_no": f"111-1111111-111110{index}",
                        "system_order_no": f"SYS-{index}",
                        "product_type": "tent" if index % 2 else "x_stands",
                        "workflow_status": "completed",
                    }
                    for index in range(6)
                },
            }
        ),
        encoding="utf-8",
    )
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.import_legacy_json(source, create_backup=False)

    first = store.list_workflow_page(
        page=1,
        page_size=2,
        active_statuses={"111-1111111-1111101": "processing"},
    )
    second = store.list_workflow_page(page=2, page_size=2)
    tents = store.list_workflow_page(
        page=1,
        page_size=50,
        product_types=("tent",),
    )

    assert first["total"] == 6
    assert len(first["items"]) == 2
    assert first["items"][0]["platform_order_no"] == "111-1111111-1111101"
    assert first["items"][0]["display_status"] == "processing"
    assert {item["platform_order_no"] for item in first["items"]}.isdisjoint(
        {item["platform_order_no"] for item in second["items"]}
    )
    assert tents["total"] == 3
    assert tents["product_types"] == ("tent", "x_stands")


def test_custom_order_first_page_stays_under_one_second_with_production_scale(
    tmp_path,
) -> None:
    store = CustomWorkflowStore(tmp_path / "automation.sqlite3")
    store.initialize()
    now = "2026-08-28T00:00:00+00:00"
    with store.connect() as connection:
        connection.executemany(
            """
            INSERT INTO custom_order_workflows (
                platform_order_no, original_system_order_no, product_type,
                workflow_status, source_record_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                (
                    f"ORDER-PERF-{index:04d}",
                    f"SYS-PERF-{index:04d}",
                    "tent" if index % 2 else "x_stands",
                    json.dumps(
                        {
                            "product_types": [
                                "tent" if index % 2 else "x_stands"
                            ]
                        }
                    ),
                    now,
                    now,
                )
                for index in range(2_909)
            ),
        )
        connection.commit()

    started_at = time.perf_counter()
    page = store.list_workflow_page(page=1, page_size=50)
    elapsed_seconds = time.perf_counter() - started_at

    assert page["total"] == 2_909
    assert len(page["items"]) == 50
    assert elapsed_seconds < 1.0


def test_in_memory_pagination_has_no_two_thousand_row_boundary() -> None:
    rows = tuple(
        CustomOrderRow(
            platform_order_no=f"ORDER-{index:04d}",
            product_type="tent",
            workflow_stage="pending",
            status_text="pending",
            status_updated_at=f"2026-08-28T00:{index % 60:02d}:00+00:00",
        )
        for index in range(2_005)
    )

    page = paginate_custom_order_rows(rows, page=11, page_size=200)

    assert page.total == 2_005
    assert page.page == 11
    assert len(page.items) == 5


def test_shipment_page_filters_global_status_and_preserves_total() -> None:
    rows = (
        ShipmentRow(
            "ORDER-READY",
            product_type="tent",
            logistics_no="ALS-READY",
            carrier="UPS",
            international_tracking_no="1Z",
            actual_total="10",
            chargeable_weight_kg="1",
            identity_state="ACTIVE",
            logistics_state="READY",
            erp_state="PENDING",
        ),
        ShipmentRow(
            "ORDER-WAIT",
            product_type="x_stands",
            logistics_no="ALS-WAIT",
            identity_state="ACTIVE",
            logistics_state="WAITING",
            erp_state="PENDING",
        ),
        ShipmentRow(
            "ORDER-CLOSED",
            product_type="tent",
            logistics_no="ALS-CLOSED",
            identity_state="ACTIVE",
            logistics_state="CANCELLED",
            erp_state="PENDING",
        ),
    )

    page = paginate_shipment_rows(
        rows,
        status="等待标发",
        active_statuses={"ALS-READY": "等待标发"},
        dataset_revision="rev-1",
    )

    assert page.total == 1
    assert page.items[0].logistics_no == "ALS-READY"
    assert page.dataset_revision == "rev-1"
    assert set(page.facets.product_types) == {"tent", "x_stands"}

    cancelled = paginate_shipment_rows(rows, status="已取消")
    assert cancelled.total == 1
    assert cancelled.items[0].logistics_no == "ALS-CLOSED"


def test_typed_queue_pages_are_not_misclassified_as_log_pages() -> None:
    custom = CustomOrderPage(
        items=(CustomOrderRow("ORDER-1"),),
        total=1,
        facets=QueueFacets(("pending",), ("tent",)),
    )
    shipment = ShipmentPage(
        items=(ShipmentRow("ORDER-2"),),
        total=1,
        facets=QueueFacets(("可标发",), ("tent",)),
    )

    assert _result_type(custom) == "custom_order_page"
    assert _result_type(shipment) == "shipment_page"
    assert decode_custom_order_page(to_jsonable(custom)) == custom
    assert decode_shipment_page(to_jsonable(shipment)) == shipment


def test_old_snapshot_payload_decodes_without_pagination_capabilities() -> None:
    snapshot = decode_snapshot({"custom_orders": [], "shipments": [], "logs": []})

    assert snapshot.server_features == ()
    assert snapshot.custom_orders_summary.total == 0
    assert snapshot.shipments_summary.total == 0


def test_summary_snapshot_is_opt_in_and_legacy_snapshot_keeps_rows(tmp_path) -> None:
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            custom_orders=[CustomOrderRow("ORDER-1")],
            shipments=[ShipmentRow("ORDER-2")],
            custom_orders_summary=DatasetSummary(1, "custom-rev"),
            shipments_summary=DatasetSummary(1, "shipment-rev"),
            server_features=(
                "custom_order_pagination_v1",
                "shipment_pagination_v1",
                "snapshot_summary_v1",
            ),
        )
    )
    service = CoordinatedControllerService(
        controller,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
    )
    service.register("pc-1", "PC 1")
    try:
        legacy = service.snapshot_payload("pc-1")
        summary = service.snapshot_payload("pc-1", summary_only=True)
    finally:
        service.close()

    assert len(legacy["snapshot"]["custom_orders"]) == 1
    assert len(legacy["snapshot"]["shipments"]) == 1
    assert summary["snapshot"]["custom_orders"] == []
    assert summary["snapshot"]["shipments"] == []
    assert summary["snapshot"]["custom_orders_summary"]["total"] == 1


def test_startup_snapshot_bundles_both_real_first_pages(tmp_path) -> None:
    controller = InMemoryBackgroundTaskController(
        DesktopSnapshot(
            custom_orders=[
                CustomOrderRow(f"CUSTOM-{index:03d}", status_text="pending")
                for index in range(75)
            ],
            shipments=[
                ShipmentRow(
                    f"SHIP-{index:03d}",
                    logistics_no=f"ALS-{index:03d}",
                    identity_state="ACTIVE",
                    logistics_state="WAITING",
                    erp_state="PENDING",
                )
                for index in range(80)
            ],
            custom_orders_summary=DatasetSummary(75, "custom-startup"),
            shipments_summary=DatasetSummary(80, "shipment-startup"),
            server_features=(
                "custom_order_pagination_v1",
                "shipment_pagination_v1",
                "snapshot_summary_v1",
            ),
        )
    )
    service = CoordinatedControllerService(
        controller,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
    )
    service.register("pc-1", "PC 1")
    try:
        payload = service.snapshot_payload(
            "pc-1",
            summary_only=True,
            include_queue_pages=True,
        )
    finally:
        service.close()

    assert payload["snapshot"]["custom_orders"] == []
    assert payload["snapshot"]["shipments"] == []
    assert len(payload["custom_order_page"]["items"]) == 50
    assert payload["custom_order_page"]["total"] == 75
    assert len(payload["shipment_page"]["items"]) == 50
    assert payload["shipment_page"]["total"] == 80
    assert (
        payload["snapshot"]["custom_orders_summary"]["revision"]
        == payload["custom_order_page"]["dataset_revision"]
    )
    assert (
        payload["snapshot"]["shipments_summary"]["revision"]
        == payload["shipment_page"]["dataset_revision"]
    )


def test_idle_operator_controller_is_evicted_after_instance_expires(tmp_path) -> None:
    created: list[InMemoryBackgroundTaskController] = []

    class ClosableController(InMemoryBackgroundTaskController):
        closed = False

        def close(self) -> None:
            self.closed = True

    def factory(_identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = ClosableController()
        created.append(controller)
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
        settings=CoordinationSettings(
            operator_controller_idle_seconds=60,
            monitor_interval_seconds=60,
        ),
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    service.register("alice-pc", "PC", identity=identity)
    service.snapshot_payload("alice-pc", identity=identity)
    service._operator_controller_last_used[identity.email] -= 61
    try:
        assert service._evict_idle_operator_controllers({"alice-pc"}) == 0
        assert created[0].closed is False

        service.store.deregister("alice-pc")
        evicted = service._evict_idle_operator_controllers(set())
        service.register("alice-pc-reconnected", "PC", identity=identity)
        service.snapshot_payload("alice-pc-reconnected", identity=identity)
        assert len(created) == 2
        assert created[1] is not created[0]
        assert created[1].closed is False
    finally:
        service.close()

    assert evicted == 1
    assert created[0].closed is True


def test_idle_operator_controller_is_not_evicted_with_active_or_tracked_work(
    tmp_path,
) -> None:
    created: list[InMemoryBackgroundTaskController] = []

    class ClosableController(InMemoryBackgroundTaskController):
        closed = False

        def close(self) -> None:
            self.closed = True

    def factory(_identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = ClosableController()
        created.append(controller)
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
        settings=CoordinationSettings(
            operator_controller_idle_seconds=60,
            monitor_interval_seconds=60,
        ),
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    service.register("alice-pc", "PC", identity=identity)
    service.snapshot_payload("alice-pc", identity=identity)
    service.store.deregister("alice-pc")
    service._operator_controller_last_used[identity.email] -= 61
    controller = created[0]
    try:
        service._task_controllers["tracked-task"] = controller
        assert service._evict_idle_operator_controllers(set()) == 0
        service._task_controllers.pop("tracked-task")

        submitted = controller.submit_task(
            TaskCommand(
                "后台读取",
                TaskArea.MAINTENANCE,
                Capability.LIST_ORDERS,
            )
        )
        assert submitted.accepted
        assert service._evict_idle_operator_controllers(set()) == 0
        assert controller.closed is False

        assert controller.cancel_task(str(submitted.task_id)).accepted
        assert service._evict_idle_operator_controllers(set()) == 1
        assert controller.closed is True
    finally:
        service.close()


def test_operator_controller_reclamation_can_be_disabled(tmp_path) -> None:
    created: list[InMemoryBackgroundTaskController] = []

    class ClosableController(InMemoryBackgroundTaskController):
        closed = False

        def close(self) -> None:
            self.closed = True

    def factory(_identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = ClosableController()
        created.append(controller)
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
        settings=CoordinationSettings(
            operator_controller_reclamation_enabled=False,
            operator_controller_idle_seconds=60,
            monitor_interval_seconds=60,
        ),
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    service.register("alice-pc", "PC", identity=identity)
    service.snapshot_payload("alice-pc", identity=identity)
    service.store.deregister("alice-pc")
    service._operator_controller_last_used[identity.email] -= 61
    try:
        assert service._evict_idle_operator_controllers(set()) == 0
        assert created[0].closed is False
    finally:
        service.close()
