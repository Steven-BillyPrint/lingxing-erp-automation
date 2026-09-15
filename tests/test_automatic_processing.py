from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

import pytest

from erp_automation.application.automatic_processing import (
    automatic_scan_commands, automatic_write_command, dispatch_identity,
    run_automatic_processing,
)
from erp_automation.configuration import EncryptedConfigurationStore, HostKeyAesGcmBackend
from erp_automation.contracts.models import (
    Capability, DESKTOP_CONFIRMATION_PAYLOAD_KEY, DesktopSettings,
    DesktopWriteConfirmation, TaskStatus,
)
from erp_automation.coordination.codec import decode_task_command, decode_settings, to_jsonable
from erp_automation.persistence import CustomWorkflowStore
from erp_automation.persistence.automatic_dispatch import AutomaticDispatchStore
from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController
from shipment_automation.models import ShipmentCandidate, LogisticsDetail, LOGISTICS_READY
from shipment_automation.queue_store import ShipmentQueueStore


def controller_at(path, runner=None):
    return PersistentBackgroundTaskController(path, config_store=EncryptedConfigurationStore(
        path / "data/config.enc", backend=HostKeyAesGcmBackend(b"t" * 32),
    ), task_runner=runner)


def custom(path, platform="P-001", **changes):
    store = CustomWorkflowStore(path / "data/automation.sqlite3")
    store.mutate_legacy_record(platform, lambda old: {
        **old, "system_order_no": "SYS-1", "product_type": "tent",
        "workflow_status": "pending", **changes,
    }, event_type="test_candidate")
    return store


def shipment(path):
    store = ShipmentQueueStore(path / "data/shipment_queue.sqlite3")
    store.upsert_candidate(ShipmentCandidate(
        system_order_no="S-2", platform_order_no="P-2", logistics_no="ALS-2",
        shipment_tag_name="标发", product_type="tent",
    ))
    store.complete_logistics_attempt("ALS-2", LogisticsDetail(
        logistics_no="ALS-2", carrier="UPS", international_tracking_no="1Z9253126709651051",
        actual_total="CNY 100", chargeable_weight_kg="4.5",
    ), state=LOGISTICS_READY, last_error=None)
    return store


def wait_until(condition):
    deadline = time.monotonic() + 3
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_mode_roundtrip_and_invalid_value(tmp_path):
    controller = controller_at(tmp_path)
    try:
        assert controller.automatic_processing_enabled()
        assert controller.set_processing_mode("manual").accepted
        assert controller.get_automatic_processing_tasks() == []
        assert not controller.set_processing_mode("invalid").accepted
        assert decode_settings(to_jsonable(controller.snapshot().settings)).processing_mode == "manual"
    finally:
        controller.close()
    reopened = controller_at(tmp_path)
    try:
        assert not reopened.automatic_processing_enabled()
    finally:
        reopened.close()


def test_candidates_skip_terminal_and_identity_review_before_limiting(tmp_path):
    controller = controller_at(tmp_path, lambda command: {"status": "completed"})
    try:
        controller.set_emergency_stop_writes(False)
        for index in range(110):
            custom(tmp_path, f"A-{index:03}", workflow_status="completed")
        custom(tmp_path, "B-review", product_identity_state="product_identity_review")
        custom(tmp_path, "C-cancelled", workflow_status="cancelled")
        custom(tmp_path, "D-ready")
        commands = [decode_task_command(item) for item in controller.get_automatic_processing_tasks()]
        writes = [command for command in commands if command.capability.is_write]
        assert [command.order_no for command in writes] == ["D-ready"]
        assert len(commands) <= 7
        confirmation = DesktopWriteConfirmation.from_payload(writes[0].payload)
        assert confirmation.source == "automatic_mode"
    finally:
        controller.close()


def test_automatic_write_never_repeats_after_failure_or_restart(tmp_path):
    calls = []
    controller = controller_at(tmp_path, lambda command: calls.append(command) or {"status": "failed"})
    custom(tmp_path)
    command = automatic_write_command("custom", {"platform_order_no": "P-001", "system_order_no": "SYS-1"})
    try:
        controller.set_emergency_stop_writes(False)
        first = controller.submit_task(command)
        assert first.accepted
        wait_until(lambda: any(task.task_id == first.task_id and task.status.terminal for task in controller.task_snapshot()))
        assert not controller.submit_task(command).accepted
        assert len(calls) == 1
    finally:
        controller.close()
    reopened = controller_at(tmp_path, lambda command: calls.append(command) or {"status": "completed"})
    try:
        reopened.set_emergency_stop_writes(False)
        assert not reopened.submit_task(command).accepted
        assert len(calls) == 1
    finally:
        reopened.close()


def test_manual_switch_cancels_queued_automatic_work_only(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def runner(command):
        calls.append(command.order_no)
        started.set()
        release.wait(3)
        return {"status": "completed"}

    controller = controller_at(tmp_path, runner)
    try:
        controller.set_emergency_stop_writes(False)
        for platform in ("P-1", "P-2"):
            custom(tmp_path, platform)
        first = controller.submit_task(automatic_write_command("custom", {"platform_order_no": "P-1", "system_order_no": "SYS-1"}))
        assert first.accepted and started.wait(2)
        second = controller.submit_task(automatic_write_command("custom", {"platform_order_no": "P-2", "system_order_no": "SYS-1"}))
        assert second.accepted
        assert controller.set_processing_mode("manual").accepted
        assert next(task for task in controller.task_snapshot() if task.task_id == second.task_id).status.terminal
        release.set()
        wait_until(lambda: all(task.status.terminal for task in controller.task_snapshot()))
        assert calls == ["P-1"]
        assert controller.get_automatic_processing_tasks() == []
        assert controller.set_processing_mode("automatic").accepted
        writes = [decode_task_command(item) for item in controller.get_automatic_processing_tasks()
                  if item["capability"] == Capability.UPDATE_CONTACT.value]
        assert [command.order_no for command in writes] == ["P-2"]
    finally:
        release.set()
        controller.close()


def test_stale_automatic_command_and_emergency_stop_are_rejected(tmp_path):
    controller = controller_at(tmp_path, lambda command: pytest.fail("must not execute"))
    try:
        custom(tmp_path)
        command = automatic_write_command("custom", {"platform_order_no": "P-001", "system_order_no": "SYS-1"})
        assert not controller.submit_task(command).accepted
        controller.set_emergency_stop_writes(False)
        custom(tmp_path, workflow_status="cancelled")
        assert not controller.submit_task(command).accepted
        controller.set_processing_mode("manual")
        # A stale client cannot disguise the automatic authorization as manual.
        payload = dict(command.payload)
        payload.pop("automatic_processing")
        assert not controller.submit_task(replace(command, payload=payload)).accepted
    finally:
        controller.close()


def test_shared_dispatch_claim_is_atomic_and_survives_reconnect(tmp_path):
    path = tmp_path / "dispatch.sqlite3"
    first, second = AutomaticDispatchStore(path), AutomaticDispatchStore(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda store: store.claim("custom:P-1"), (first, second)))
    assert sorted(results) == [False, True]
    assert AutomaticDispatchStore(path).contains("custom:P-1")


def test_only_fresh_ready_shipments_are_automatically_eligible(tmp_path):
    store = shipment(tmp_path)
    admission = AutomaticDispatchStore(store.path)
    assert [row["logistics_no"] for row in admission.candidates("shipment")] == ["ALS-2"]
    with store.connect() as connection:
        connection.execute("UPDATE shipment_erp SET state = 'RETRYABLE'")
    assert admission.candidates("shipment") == []
    with store.connect() as connection:
        connection.execute("UPDATE shipment_erp SET state = 'PENDING', checkpoint = 'OUTBOUNDED'")
    assert admission.candidates("shipment") == []


def test_dispatch_uses_normal_submission_and_stops_on_unknown_receipt():
    from erp_automation.contracts.controller import ControlResult

    class Controller:
        def get_automatic_processing_tasks(self):
            return [to_jsonable(command) for command in automatic_scan_commands()]

        def submit_task(self, command):
            calls.append(command)
            return ControlResult(False, "unknown", details={"submission_outcome_unknown": True})

    calls = []
    run_automatic_processing(Controller())
    assert len(calls) == 1


def test_coordinator_prefers_automatic_account_and_rejects_other_client(tmp_path):
    from erp_automation.coordination.access import OperatorIdentity
    from erp_automation.coordination.service import CoordinatedControllerService
    from erp_automation.coordination.store import CoordinationStore
    from erp_automation.ui.controller import InMemoryBackgroundTaskController

    controllers = {}

    def factory(identity):
        controller = InMemoryBackgroundTaskController()
        controller.set_processing_mode("manual" if identity.email.startswith("manual") else "automatic")
        controllers[identity.email] = controller
        return controller

    service = CoordinatedControllerService(None, CoordinationStore(tmp_path / "coordination.sqlite3"), controller_factory=factory)
    manual = OperatorIdentity("manual@billyprint.com", "Manual", "manual")
    automatic = OperatorIdentity("automatic@billyprint.com", "Automatic", "automatic")
    try:
        service.register("manual-pc", "Manual", identity=manual)
        service.register("automatic-pc", "Automatic", identity=automatic)
        service._controller_for(manual)
        service._controller_for(automatic)
        assert service._scheduler_status("automatic-pc")["is_leader"]
        rejected = service.invoke(instance_id="manual-pc", request_id="automatic-attempt", method="submit_task",
                                  raw_args=[to_jsonable(automatic_scan_commands()[0])], raw_kwargs={}, identity=manual)
        assert not rejected["result"]["accepted"]
        assert rejected["result"]["details"]["scheduler_rejected"]
        assert controllers[manual.email].task_snapshot() == ()
    finally:
        service.close()


def test_automatic_scan_cadence_is_shared_across_repeated_client_ticks(tmp_path):
    from erp_automation.coordination.service import CoordinatedControllerService
    from erp_automation.coordination.store import CoordinationStore
    from erp_automation.ui.controller import InMemoryBackgroundTaskController

    controller = InMemoryBackgroundTaskController()
    service = CoordinatedControllerService(controller, CoordinationStore(tmp_path / "coordination.sqlite3"))
    try:
        service.register("pc", "PC")
        command = automatic_scan_commands()[0]
        from erp_automation.application.automatic_processing import AUTOMATIC_SCAN_INTERVALS
        service.store.scheduled_job_due_times(AUTOMATIC_SCAN_INTERVALS)
        def submit(request_id):
            return service.invoke(instance_id="pc", request_id=request_id, method="submit_task",
                                  raw_args=[to_jsonable(command)], raw_kwargs={})["result"]
        assert not submit("not-due")["accepted"]
        with service.store._connect() as connection:
            connection.execute("UPDATE coordination_scheduler_jobs SET next_due_at = 0")
            connection.commit()
        first = submit("first")
        assert first["accepted"], first
        rejected = submit("second")
        assert not rejected["accepted"]
        assert rejected["details"]["scheduler_rejected"]
        assert len(controller.task_snapshot()) == 1
    finally:
        service.close()


def test_legacy_client_saving_other_settings_preserves_manual_mode(tmp_path):
    from erp_automation.coordination.service import CoordinatedControllerService
    from erp_automation.coordination.store import CoordinationStore

    controller = controller_at(tmp_path)
    service = CoordinatedControllerService(controller, CoordinationStore(tmp_path / "coordination.sqlite3"))
    try:
        controller.set_processing_mode("manual")
        service.register("legacy-pc", "Legacy")
        settings = to_jsonable(controller.snapshot().settings)
        settings.pop("processing_mode")
        settings["api_timeout_seconds"] = 45
        result = service.invoke(instance_id="legacy-pc", request_id="legacy-settings", method="save_settings",
                                raw_args=[settings], raw_kwargs={})["result"]
        assert result["accepted"]
        assert controller.snapshot().settings.processing_mode == "manual"
        assert controller.snapshot().settings.api_timeout_seconds == 45
    finally:
        service.close()
        controller.close()


def test_new_automatic_client_takes_scheduler_from_legacy_client_during_rollout(tmp_path):
    from erp_automation.coordination.access import OperatorIdentity
    from erp_automation.coordination.service import CoordinatedControllerService
    from erp_automation.coordination.store import CoordinationStore
    from erp_automation.ui.controller import InMemoryBackgroundTaskController

    service = CoordinatedControllerService(
        None, CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=lambda identity: InMemoryBackgroundTaskController(),
        required_client_version="2026.09.15.1", rollout_previous_client_version="2026.09.14.2",
        client_rollout_grace_seconds=3600,
    )
    identity = OperatorIdentity("test@billyprint.com", "Test", "test")
    try:
        assert service.register("old", "Old", client_version="2026.09.14.2", identity=identity)["scheduler"]["is_leader"]
        service._controller_for(identity)
        assert service.register("new", "New", client_version="2026.09.15.1", identity=identity)["scheduler"]["is_leader"]
        assert not service.heartbeat("old", identity=identity)["scheduler"]["is_leader"]
        rejected = service.invoke(instance_id="old", request_id="old-auto", method="submit_task",
            raw_args=[to_jsonable(automatic_scan_commands()[0])], raw_kwargs={}, identity=identity)
        assert rejected["result"]["details"]["scheduler_rejected"]
        service.deregister("new", identity=identity)
        # With only old clients left, ordinary scans can still run during rollout.
        assert service.heartbeat("old", identity=identity)["scheduler"]["is_leader"]
    finally:
        service.close()


def test_automatic_scheduler_handoff_is_atomic_and_recovers_after_pause_and_expiry(tmp_path):
    from erp_automation.application.automatic_processing import AUTOMATIC_PROCESSING_MIN_CLIENT_VERSION
    from erp_automation.coordination.store import CoordinationStore

    now = [100.0]
    store = CoordinationStore(tmp_path / "coordination.sqlite3", clock=lambda: now[0])
    def elect(instance):
        return store.elect_scheduler(instance, ttl_seconds=10,
            automatic_client_min_version=AUTOMATIC_PROCESSING_MIN_CLIENT_VERSION)
    store.register_instance("old", "Old", ttl_seconds=60, client_version="2026.09.14.2")
    assert elect("old")["is_leader"]
    for instance in ("new-a", "new-b"):
        store.register_instance(instance, instance, ttl_seconds=60, client_version="2026.09.15.1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(elect, ("new-a", "new-b")))
    assert sum(item["is_leader"] for item in results) == 1
    leader = next(item["owner_instance_id"] for item in results if item["is_leader"])
    follower = "new-b" if leader == "new-a" else "new-a"
    store.set_instance_execution_paused(leader, enabled=True, state="paused", reason="test")
    assert elect(follower)["is_leader"]
    assert not elect("old")["is_leader"]
    now[0] += 61
    store.register_instance("replacement", "Replacement", ttl_seconds=60, client_version="2026.09.15.1")
    assert elect("replacement")["is_leader"]
