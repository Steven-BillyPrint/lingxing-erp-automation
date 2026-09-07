import time

import pytest

from erp_automation.contracts.models import (
    Capability, DesktopInteractionRequest, DesktopSnapshot, TaskArea, TaskRecord, TaskStatus,
)
from erp_automation.coordination.service import (
    CoordinatedControllerService, CoordinationSettings,
)
from erp_automation.coordination.store import CoordinationStore
from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController


def controller_with_queue(tmp_path, count=100):
    return PersistentBackgroundTaskController(
        tmp_path / "runtime",
        initial=DesktopSnapshot(tasks=[
            TaskRecord(
                f"task-{index}", "Process order", TaskArea.CUSTOMIZATION,
                Capability.UPDATE_CONTACT, order_no=f"ORDER-{index}",
                status=TaskStatus.RUNNING if index == 0 else TaskStatus.QUEUED,
                progress_percent=82 if index == 0 else 0,
            ) for index in range(count)
        ]),
        recover_interrupted_task_journal=False,
    )


@pytest.mark.parametrize("count", [40, 100])
def test_cutover_reaches_poll_without_waiting_for_monitor_or_full_queue(tmp_path, monkeypatch, count):
    controller = controller_with_queue(tmp_path, count)
    service = CoordinatedControllerService(
        controller, CoordinationStore(tmp_path / "coordination.sqlite3"),
        settings=CoordinationSettings(monitor_interval_seconds=3600),
    )
    service.register("desktop", "Operator")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(controller, "snapshot", lambda: pytest.fail("full queues must not be read"))
            initial = service.snapshot_payload("desktop", summary_only=True)
            assert service.snapshot_payload(
                "desktop", known_revision=initial["revision"], summary_only=True,
            )["unchanged"]
            started = time.monotonic()
            controller.set_task_status("task-0", TaskStatus.SUCCEEDED, progress_percent=100)
            controller.set_task_status("task-1", TaskStatus.RUNNING, progress_percent=10)
            fresh = service.snapshot_payload(
                "desktop", known_revision=initial["revision"], summary_only=True,
            )
            assert time.monotonic() - started < 2
            assert fresh["revision"] > initial["revision"]
            tasks = {task["task_id"]: task for task in fresh["snapshot"]["tasks"]}
            assert tasks["task-0"]["status"] == "succeeded"
            assert tasks["task-1"]["status"] == "running"
            assert fresh["snapshot"]["custom_orders"] == []
    finally:
        service.close()
        controller.close()


def test_cache_age_forces_verification_even_if_external_marker_was_missed(tmp_path):
    controller = controller_with_queue(tmp_path, 0)
    service = CoordinatedControllerService(
        controller, CoordinationStore(tmp_path / "coordination.sqlite3"),
        settings=CoordinationSettings(monitor_interval_seconds=3600),
    )
    service.register("desktop", "Operator")
    try:
        initial = service.snapshot_payload("desktop", summary_only=True)
        service._snapshot_body_times[("desktop", True)] = time.monotonic() - 60
        fresh = service.snapshot_payload(
            "desktop", known_revision=initial["revision"], summary_only=True,
        )
        assert fresh["unchanged"] is False
        assert "snapshot" in fresh
    finally:
        service.close()
        controller.close()


def test_database_write_invalidates_poll_without_monitor(tmp_path):
    controller = controller_with_queue(tmp_path, 0)
    store = controller._get_custom_store()
    store.initialize()
    service = CoordinatedControllerService(
        controller, CoordinationStore(tmp_path / "coordination.sqlite3"),
        settings=CoordinationSettings(monitor_interval_seconds=3600),
    )
    service.register("desktop", "Operator")
    try:
        initial = service.snapshot_payload("desktop", summary_only=True)
        with store.connect() as connection:
            connection.execute("CREATE TABLE state_refresh_probe (value INTEGER)")
        fresh = service.snapshot_payload(
            "desktop", known_revision=initial["revision"], summary_only=True,
        )
        assert fresh["unchanged"] is False
        assert fresh["revision"] > initial["revision"]
    finally:
        service.close()
        controller.close()


def test_interaction_changes_are_delivered_without_monitor_and_only_to_owner(tmp_path):
    controller = controller_with_queue(tmp_path, 1)
    service = CoordinatedControllerService(
        controller, CoordinationStore(tmp_path / "coordination.sqlite3"),
        settings=CoordinationSettings(monitor_interval_seconds=3600),
    )
    service.register("owner", "Owner")
    service.register("other", "Other")
    try:
        initial = service.snapshot_payload("owner", summary_only=True)
        controller._pending_interactions["review"] = DesktopInteractionRequest(
            "review", "task-0", "review", "Review", "Confirm",
            target_instance_id="owner", non_blocking=True,
        )
        fresh = service.snapshot_payload(
            "owner", known_revision=initial["revision"], summary_only=True,
        )
        assert fresh["unchanged"] is False
        assert len(fresh["interactions"]) == 1
        assert service.snapshot_payload("other", summary_only=True)["interactions"] == []
        controller._pending_interactions.clear()
        cleared = service.snapshot_payload(
            "owner", known_revision=fresh["revision"], summary_only=True,
        )
        assert cleared["revision"] > fresh["revision"]
        assert cleared["interactions"] == []
    finally:
        service.close()
        controller.close()


def test_monitor_reads_task_snapshot_once_for_a_hundred_tracked_tasks(tmp_path, monkeypatch):
    controller = controller_with_queue(tmp_path)
    service = CoordinatedControllerService(
        controller, CoordinationStore(tmp_path / "coordination.sqlite3"),
        settings=CoordinationSettings(monitor_interval_seconds=3600),
    )
    service.register("desktop", "Operator")
    # Execute one real monitor iteration deterministically after stopping its
    # scheduling threads. No business workers or browser sessions are started.
    service._closed.set()
    service._monitor.join(2)
    service._receipt_monitor.join(2)
    for task in controller.task_snapshot():
        service._tracked_tasks.add(task.task_id)
        service._task_owners[task.task_id] = "desktop"
        service._task_controllers[task.task_id] = controller
    reads = []
    original = controller.task_snapshot

    class OneIteration:
        def __init__(self):
            self.calls = 0
        def wait(self, _timeout):
            self.calls += 1
            return self.calls > 1

    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, "_closed", OneIteration())
            patch.setattr(controller, "task_snapshot", lambda: (reads.append(1), original())[1])
            patch.setattr(controller, "snapshot", lambda: pytest.fail("monitor read full queue"))
            service._monitor_loop()
            assert len(reads) == 1
            assert len(service._tracked_tasks) == 100
    finally:
        service.close()
        controller.close()
