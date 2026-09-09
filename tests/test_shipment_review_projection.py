import sqlite3

from erp_automation.contracts.models import DesktopSnapshot, ShipmentRow
from erp_automation.coordination.service import CoordinatedControllerService, CoordinationSettings
from erp_automation.coordination.store import CoordinationStore
from erp_automation.ui.controller import InMemoryBackgroundTaskController
from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController
from shipment_automation.queue_store import ShipmentWorkflowStore
from shipment_automation.models import ShipmentCandidate


def ready_row():
    return ShipmentRow(
        "ORDER", logistics_no="ALS", carrier="FedEx", international_tracking_no="123",
        actual_total="10", chargeable_weight_kg="1", identity_state="ACTIVE",
        logistics_state="READY", erp_state="PENDING",
    )


def test_historical_lock_migration_preserves_date_and_does_not_expire(tmp_path):
    path = tmp_path / "coordination.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE coordination_manual_review_locks (resource TEXT PRIMARY KEY, task_id TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)")
        c.execute("INSERT INTO coordination_manual_review_locks VALUES ('order:order', 'old-custom', '外部写入未确认', 100)")
    store = CoordinationStore(path, clock=lambda: 1_000_000)
    store.set_manual_review_locks(("order:ORDER",), task_id="old-custom", reason="外部写入未确认", source_area="customization")
    store.cleanup_expired()
    lock = store.order_review_locks()["order"]
    assert lock["created_at"] == 100
    assert lock["source_area"] == "customization"
    assert "task_id" not in lock
    store.set_manual_review_locks(("order:ORDER",), task_id="new-unknown", reason="另一次写入")
    lock = store.order_review_locks()["order"]
    assert lock["created_at"] == 1_000_000
    assert lock["source_area"] == ""


def test_shared_lock_invalidates_snapshot_and_filters_before_pagination(tmp_path):
    controller = InMemoryBackgroundTaskController(DesktopSnapshot(shipments=[ready_row()]))
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store, settings=CoordinationSettings(monitor_interval_seconds=3600))
    service.register("desktop", "Operator")
    try:
        initial = service.snapshot_payload("desktop", summary_only=True)
        original_revision = controller.list_shipment_page().dataset_revision
        store.set_manual_review_locks(("order:ORDER",), task_id="old-custom", reason="心跳丢失，外部写入未确认", source_area="customization")
        fresh = service.snapshot_payload("desktop", known_revision=initial["revision"], summary_only=True)
        assert not fresh["unchanged"]
        page = controller.list_shipment_page(status="标发需人工复核")
        assert page.total == 1
        assert page.items[0].manual_review_reason == "心跳丢失，外部写入未确认"
        assert page.items[0].manual_review_source == "customization"
        assert controller.list_shipment_page(status="可标发").total == 0
        assert page.dataset_revision != original_revision
        assert controller.snapshot().shipments[0].manual_review_reason
        store.clear_manual_review_locks(("order:ORDER",))
        cleared = service.snapshot_payload("desktop", known_revision=fresh["revision"], summary_only=True)
        assert not cleared["unchanged"]
        assert controller.list_shipment_page(status="可标发").total == 1
        assert not controller.snapshot().shipments[0].manual_review_reason
    finally:
        service.close()


def test_persistent_hydration_retains_lock_and_summary_revision(tmp_path):
    controller = PersistentBackgroundTaskController(tmp_path / "runtime", recover_interrupted_task_journal=False)
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    controller.set_shipment_review_lock_provider(store.order_review_locks)
    path = controller._shipment_state_path()
    queue = ShipmentWorkflowStore(path)
    queue.upsert_candidate(ShipmentCandidate(
        platform_order_no="ORDER", system_order_no="SYS", logistics_no="ALS",
        shipment_tag_name="自动标发", sku_text="detail",
    ))
    with queue.connect() as conn:
        conn.execute("UPDATE shipment_logistics SET state = 'READY', carrier_raw = 'FedEx', "
                     "international_tracking_no = '123', currency = 'CNY', fee_amount = '10', "
                     "chargeable_weight_kg = '1'")
    try:
        initial = controller.summary_snapshot().shipments_summary.revision
        store.set_manual_review_locks(("order:ORDER",), task_id="old", reason="需核对")
        page = controller.list_shipment_page(status="标发需人工复核")
        assert page.total == 1
        assert page.items[0].sku_text == "detail"
        assert page.items[0].manual_review_reason == "需核对"
        assert page.items[0].manual_review_source == "历史任务"
        assert controller.summary_snapshot().shipments_summary.revision == page.dataset_revision
        assert page.dataset_revision != initial
    finally:
        controller.close()
