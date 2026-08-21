from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from erp_automation.coordination import local_browser
from erp_automation.coordination import server_main as coordination_server_main
from erp_automation.coordination import service as coordination_service_module
from erp_automation.coordination.access import OperatorIdentity
from erp_automation.configuration import HostKeyAesGcmBackend
from erp_automation.coordination.codec import (
    MAX_CONFIGURED_SECRET_LENGTH,
    decode_interaction_response,
    decode_interactions,
    decode_snapshot,
    to_jsonable,
)
from erp_automation.coordination.http_server import create_http_server
from erp_automation.coordination.local_browser import (
    ALIBABA_QUOTE_URL,
    ALIBABA_SCM_HOME_URL,
    LocalBrowserUnavailable,
    LocalChromeHost,
    _safe_start_url,
)
from erp_automation.coordination.remote_controller import (
    CoordinationAuthenticationRequired,
    CoordinationClientUpdateRequired,
    CoordinationConnectionError,
    RemoteBackgroundTaskController,
)
from erp_automation.coordination.service import (
    ClientUpdateRequiredError,
    CoordinatedControllerService,
    CoordinationSettings,
    MUTATION_METHODS,
    READ_METHODS,
    RPC_METHODS,
)
from erp_automation.coordination.store import CoordinationStore
from erp_automation.ui.controller import (
    BackgroundTaskController,
    ControlResult,
    InMemoryBackgroundTaskController,
)
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    SERVER_CONFIGURED_SECRET,
    DesktopInteractionRequest,
    DesktopInteractionResponse,
    DesktopSnapshot,
    DesktopSettings,
    NOTIFICATION_REVIEW_RESCAN_TRIGGER,
    SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER,
    SHIPMENT_NOTIFICATION_SEND_TRIGGER,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
    LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
)


def _service(
    tmp_path: Path,
) -> tuple[
    InMemoryBackgroundTaskController,
    CoordinationStore,
    CoordinatedControllerService,
]:
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(
            instance_ttl_seconds=30,
            transient_lease_seconds=10,
            task_lease_seconds=30,
            monitor_interval_seconds=0.02,
        ),
    )
    return controller, store, service


def test_scan_resource_scopes_are_separated_by_business_function() -> None:
    custom = coordination_service_module._resource_keys(
        "submit_task",
        [TaskCommand("定制扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS)],
        {},
    )
    shipment = coordination_service_module._resource_keys(
        "submit_task",
        [TaskCommand("标发扫描", TaskArea.SHIPMENT, Capability.LIST_ORDERS)],
        {},
    )
    notification = coordination_service_module._resource_keys(
        "submit_task",
        [
            TaskCommand(
                "客户通知扫描",
                TaskArea.SHIPMENT,
                Capability.LIST_ORDERS,
                payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
            )
        ],
        {},
    )

    assert custom == ("scan:customization",)
    assert shipment == ("scan:shipment",)
    assert notification == ("scan:notification",)
    assert len(set(custom + shipment + notification)) == 3


def test_scan_issue_management_key_resolves_to_the_platform_order_lease() -> None:
    controller = InMemoryBackgroundTaskController()
    controller._state.shipments = [  # noqa: SLF001
        ShipmentRow(
            platform_order_no="39972",
            system_order_no="103000000000009972",
            scan_issue_key="scan-issue:72",
            scan_issue_code="customer_shipping_service_unavailable",
        )
    ]

    assert coordination_service_module._order_resource_keys(
        controller,
        "change_shipment_statuses",
        [["scan-issue:72"], "manual_cancel"],
        {"reason": "人工取消"},
    ) == ("order:39972",)


def test_coordinator_accepts_different_scan_functions_at_the_same_time(
    tmp_path: Path,
) -> None:
    _controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    commands = (
        TaskCommand("定制扫描", TaskArea.CUSTOMIZATION, Capability.LIST_ORDERS),
        TaskCommand(
            "标发扫描",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": "manual_button"},
        ),
        TaskCommand(
            "客户通知扫描",
            TaskArea.SHIPMENT,
            Capability.LIST_ORDERS,
            payload={"trigger": NOTIFICATION_REVIEW_RESCAN_TRIGGER},
        ),
    )
    try:
        results = [
            service.invoke(
                instance_id="one",
                request_id=f"parallel-scan-{index}",
                method="submit_task",
                raw_args=[to_jsonable(command)],
                raw_kwargs={},
            )["result"]
            for index, command in enumerate(commands)
        ]

        assert all(result["accepted"] is True for result in results)
        assert {lease["resource"] for lease in store.active_leases()} == {
            "scan:customization",
            "scan:shipment",
            "scan:notification",
        }
    finally:
        service.close()


def test_task_batch_uses_one_rpc_and_preserves_per_task_coordination(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    service.register(
        "one",
        "Alice",
        browser_endpoint="http://127.0.0.1:24000",
    )
    commands = tuple(
        TaskCommand(
            "处理定制订单",
            TaskArea.CUSTOMIZATION,
            Capability.LIST_ORDERS,
            order_no=f"111-{index}",
        )
        for index in range(3)
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="custom-batch",
            method="submit_tasks",
            raw_args=[to_jsonable(commands)],
            raw_kwargs={},
        )

        assert response["result_type"] == "control_results"
        assert [result["accepted"] for result in response["result"]] == [
            True,
            True,
            True,
        ]
        task_ids = [result["task_id"] for result in response["result"]]
        assert len(set(task_ids)) == 3
        assert {task.order_no for task in controller.snapshot().tasks} == {
            "111-0",
            "111-1",
            "111-2",
        }
        assert {lease["resource"] for lease in store.active_leases()} == {
            "order:111-0",
            "order:111-1",
            "order:111-2",
        }

        repeated = service.invoke(
            instance_id="one",
            request_id="custom-batch",
            method="submit_tasks",
            raw_args=[to_jsonable(commands)],
            raw_kwargs={},
        )
        assert [result["task_id"] for result in repeated["result"]] == task_ids
        assert len(controller.snapshot().tasks) == 3
    finally:
        service.close()


def test_every_controller_operation_is_explicitly_classified_for_remote_audit() -> None:
    public_operations = {
        name
        for name, value in BackgroundTaskController.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert READ_METHODS.isdisjoint(MUTATION_METHODS)
    assert RPC_METHODS == READ_METHODS | MUTATION_METHODS
    assert public_operations == RPC_METHODS | {"snapshot", "prepare_close"}


def test_read_only_outbound_diagnostic_is_exposed_through_coordination_rpc(
    tmp_path: Path,
) -> None:
    class _DiagnosticController(InMemoryBackgroundTaskController):
        def diagnose_shipment_notification_outbound(self, platform_order_no):
            return {
                "platform_order_no": platform_order_no,
                "outbound_state": "WAITING",
                "wms_rows": [{"raw_status": 2}],
                "read_only": True,
            }

    controller = _DiagnosticController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store)
    service.register("one", "Alice")
    try:
        response = service.invoke(
            instance_id="one",
            request_id="diagnose-one",
            method="diagnose_shipment_notification_outbound",
            raw_args=["113-1753631-6040206-1"],
            raw_kwargs={},
        )

        assert response["result_type"] == "json"
        assert response["result"] == {
            "platform_order_no": "113-1753631-6040206-1",
            "outbound_state": "WAITING",
            "wms_rows": [{"raw_status": 2}],
            "read_only": True,
        }
        assert "diagnose_shipment_notification_outbound" in READ_METHODS
        assert "diagnose_shipment_notification_outbound" not in MUTATION_METHODS
        with pytest.raises(ValueError, match="Platform order number is invalid"):
            service.invoke(
                instance_id="one",
                request_id="diagnose-invalid",
                method="diagnose_shipment_notification_outbound",
                raw_args=["../secret"],
                raw_kwargs={},
            )
    finally:
        service.close()


def test_package_logistics_edit_is_validated_and_exposed_as_notification_mutation(
    tmp_path: Path,
) -> None:
    class _PackageEditController(InMemoryBackgroundTaskController):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[int, str, str, str, str]] = []

        def get_shipment_notification_details(self, notification_ids):
            return [
                {
                    "id": int(notification_ids[0]),
                    "platform_order_no": "112-PACKAGE-EDIT",
                }
            ]

        def edit_shipment_notification_package(
            self,
            notification_id,
            *,
            package_key,
            carrier,
            tracking_no,
            reason,
        ):
            self.calls.append(
                (notification_id, package_key, carrier, tracking_no, reason)
            )
            return ControlResult(True, "updated")

    controller = _PackageEditController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store)
    service.register("one", "Alice")
    try:
        response = service.invoke(
            instance_id="one",
            request_id="edit-package-one",
            method="edit_shipment_notification_package",
            raw_args=[17],
            raw_kwargs={
                "package_key": "10001:WO-1",
                "carrier": " USPS ",
                "tracking_no": " 9334610990150195994324 ",
                "reason": " 已在 USPS 官网核对轨迹 ",
            },
        )

        assert response["result"]["accepted"] is True
        assert controller.calls == [
            (
                17,
                "10001:WO-1",
                "USPS",
                "9334610990150195994324",
                "已在 USPS 官网核对轨迹",
            )
        ]
        assert "edit_shipment_notification_package" in MUTATION_METHODS
        assert coordination_service_module._resource_keys(
            "edit_shipment_notification_package",
            [17],
            {},
        ) == ("notification:17",)
        with pytest.raises(ValueError, match="Override reason is invalid"):
            service.invoke(
                instance_id="one",
                request_id="edit-package-invalid",
                method="edit_shipment_notification_package",
                raw_args=[17],
                raw_kwargs={
                    "package_key": "10001:WO-1",
                    "carrier": "USPS",
                    "tracking_no": "9334610990150195994324",
                    "reason": "",
                },
            )
    finally:
        service.close()


def test_read_rpc_responses_are_never_persisted_as_idempotency_cache(
    tmp_path: Path,
) -> None:
    class _CountingController(InMemoryBackgroundTaskController):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def list_shipment_notifications(self, **_kwargs):
            self.calls += 1
            return {
                "items": [{"id": self.calls}],
                "page": 1,
                "page_size": 50,
                "total": 1,
                "total_pages": 1,
                "product_types": [],
            }

    controller = _CountingController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store)
    service.register("one", "Alice")
    try:
        first = service.invoke(
            instance_id="one",
            request_id="same-read-request",
            method="list_shipment_notifications",
            raw_args=[],
            raw_kwargs={"page": 1, "page_size": 50},
        )
        second = service.invoke(
            instance_id="one",
            request_id="same-read-request",
            method="list_shipment_notifications",
            raw_args=[],
            raw_kwargs={"page": 1, "page_size": 50},
        )

        assert controller.calls == 2
        assert first["result"]["items"] == [{"id": 1}]
        assert second["result"]["items"] == [{"id": 2}]
        assert store.cached_response("same-read-request") is None
    finally:
        service.close()


def test_legacy_read_cache_cleanup_backs_up_and_preserves_mutations(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.save_response(
        request_id="large-read",
        instance_id="desktop-one",
        method="list_shipment_notifications",
        response={"result": "x" * 4096},
    )
    store.save_response(
        request_id="mutation",
        instance_id="desktop-one",
        method="save_settings",
        response={"result": {"accepted": True}},
    )

    report = store.compact_legacy_read_responses(
        tuple(READ_METHODS),
        minimum_reclaim_bytes=0,
    )

    assert report["deleted"] == 1
    assert Path(report["backup"]).is_file()
    assert store.cached_response("large-read") is None
    assert store.cached_response("mutation")["result"]["accepted"] is True
    with sqlite3.connect(report["backup"]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT COUNT(*) FROM coordination_requests WHERE request_id = 'large-read'"
        ).fetchone()[0] == 1


def test_legacy_read_cache_cleanup_can_release_pages_without_offline_vacuum(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.save_response(
        request_id="legacy-read",
        instance_id="desktop-one",
        method="list_shipment_notifications",
        response={"result": "cached"},
    )
    store.save_response(
        request_id="mutation",
        instance_id="desktop-one",
        method="save_settings",
        response={"result": {"accepted": True}},
    )

    report = store.compact_legacy_read_responses(
        tuple(READ_METHODS),
        minimum_reclaim_bytes=0,
        create_backup=False,
        vacuum_database=False,
    )

    assert report["deleted"] == 1
    assert report["backup"] == ""
    assert store.cached_response("legacy-read") is None
    assert store.cached_response("mutation")["result"]["accepted"] is True


def test_large_legacy_read_cache_cleanup_does_not_block_service_readiness(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.save_response(
        request_id="legacy-read",
        instance_id="desktop-one",
        method="list_shipment_notifications",
        response={"result": "x" * (17 * 1024 * 1024)},
    )

    started_at = time.monotonic()
    service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        store,
    )
    try:
        assert time.monotonic() - started_at < 2
        assert service._read_cache_maintenance is not None
        service._read_cache_maintenance.join(timeout=10)
        assert not service._read_cache_maintenance.is_alive()
        assert store.cached_response("legacy-read") is None
    finally:
        service.close()


def test_coordinator_runs_receipt_monitor_without_a_desktop_request(tmp_path) -> None:
    class _ReceiptController(InMemoryBackgroundTaskController):
        def __init__(self) -> None:
            super().__init__()
            self.receipt_refresh = threading.Event()
            self.release_receipt_refresh = threading.Event()

        def refresh_due_shipment_notification_receipts(
            self,
            *,
            operator_email: str,
            owner: str,
        ) -> dict[str, int]:
            assert operator_email == ""
            assert owner == "server-receipts:shared"
            self.receipt_refresh.set()
            self.release_receipt_refresh.wait(timeout=1)
            return {"checked": 1, "completed": 1}

    controller = _ReceiptController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(receipt_monitor_interval_seconds=0.01),
    )
    try:
        assert controller.receipt_refresh.wait(timeout=1)
        revision = store.current_revision()
        controller.release_receipt_refresh.set()
        deadline = time.time() + 1
        while store.current_revision() == revision and time.time() < deadline:
            time.sleep(0.01)
        assert store.current_revision() > revision
    finally:
        controller.release_receipt_refresh.set()
        service.close()


class _PortableConfigurationController(InMemoryBackgroundTaskController):
    def __init__(self, package: bytes) -> None:
        super().__init__()
        self.package = package
        self.imported_package = b""
        self.configuration_only = False

    def export_portable_migration(
        self,
        destination: str,
        passphrase: str,
        *,
        include_state: bool,
    ) -> ControlResult:
        assert passphrase == "portable configuration password"
        assert include_state is False
        Path(destination).write_bytes(self.package)
        return ControlResult(True, "exported")

    def import_portable_migration(
        self,
        package_path: str,
        passphrase: str,
        *,
        overwrite: bool,
        configuration_only: bool = False,
    ) -> ControlResult:
        assert passphrase == "portable configuration password"
        assert overwrite is True
        self.imported_package = Path(package_path).read_bytes()
        self.configuration_only = configuration_only
        return ControlResult(True, "imported")


def test_snapshot_codec_round_trip_preserves_controller_models() -> None:
    controller = InMemoryBackgroundTaskController()
    controller.update_capability_mode(
        Capability.DOWNLOAD_CUSTOM_ZIP,
        CapabilityMode.BROWSER,
    )
    snapshot = controller.snapshot()
    snapshot.configuration_fingerprint = "a" * 64
    snapshot.configuration_key_count = 42
    snapshot.configured_non_sensitive_field_count = 4
    snapshot.configured_secret_field_count = 2
    snapshot.configuration_is_default = False

    decoded = decode_snapshot(to_jsonable(snapshot))

    assert (
        decoded.policy.configured_mode_for(Capability.DOWNLOAD_CUSTOM_ZIP)
        is CapabilityMode.BROWSER
    )
    assert decoded.settings == snapshot.settings
    assert decoded.configuration_fingerprint == "a" * 64
    assert decoded.configuration_key_count == 42
    assert decoded.configured_non_sensitive_field_count == 4
    assert decoded.configured_secret_field_count == 2
    assert decoded.configuration_is_default is False
    assert decoded.logs[0].message == snapshot.logs[0].message


def test_snapshot_codec_accepts_only_known_bounded_secret_lengths() -> None:
    decoded = decode_snapshot(
        {
            "settings": {},
            "configured_secret_lengths": {
                "lingxing_app_secret": 17,
                "amazon_refresh_token": 0,
                "alibaba_password": True,
                "unknown_secret": 12,
                "clicksend_api_key": MAX_CONFIGURED_SECRET_LENGTH + 1,
            },
        }
    )

    assert decoded.configured_secret_lengths == {
        "lingxing_app_secret": 17,
        "amazon_refresh_token": 0,
    }


def test_service_skips_snapshot_body_when_revision_is_unchanged(
    tmp_path: Path,
) -> None:
    _controller, _store, service = _service(tmp_path)
    service.register("one", "Alice")
    try:
        initial = service.snapshot_payload("one")
        cached = service.snapshot_payload(
            "one",
            known_revision=int(initial["revision"]),
        )

        assert initial["unchanged"] is False
        assert "snapshot" in initial
        assert initial["interactions"] == []
        assert cached == {
            "revision": initial["revision"],
            "unchanged": True,
        }
    finally:
        service.close()


def test_operator_registration_does_not_wait_for_full_controller_recovery(
    tmp_path: Path,
) -> None:
    created: list[str] = []

    def factory(identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        created.append(identity.email)
        return InMemoryBackgroundTaskController()

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    try:
        allocation = service.allocate_browser_endpoint(
            "alice-pc",
            "PC-A",
            identity=identity,
        )
        registration = service.register(
            "alice-pc",
            "PC-A",
            str(allocation["browser_endpoint"]),
            identity=identity,
        )

        assert created == []
        assert registration["instance_id"] == "alice-pc"

        snapshot = service.snapshot_payload("alice-pc", identity=identity)
        assert snapshot["unchanged"] is False
        assert created == [identity.email]
    finally:
        service.close()


def test_service_requires_the_exact_published_client_version(tmp_path: Path) -> None:
    client_version = (
        Path(__file__).resolve().parents[1] / "CLIENT_VERSION"
    ).read_text(encoding="utf-8").strip()
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        required_client_version=client_version,
    )
    try:
        for supplied in ("", "2026.07.24.2", "9999.12.31.1"):
            try:
                service.allocate_browser_endpoint("one", "Alice", supplied)
            except ClientUpdateRequiredError as exc:
                assert exc.required_version == client_version
            else:
                raise AssertionError(f"Outdated client was accepted: {supplied!r}")

        allocation = service.allocate_browser_endpoint(
            "one",
            "Alice",
            client_version,
        )
        registered = service.register(
            "one",
            "Alice",
            str(allocation["browser_endpoint"]),
            client_version,
        )
        assert registered["instance_id"] == "one"
    finally:
        service.close()


def test_service_rollout_grace_accepts_only_an_older_valid_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(
        coordination_service_module.time,
        "time",
        lambda: now[0],
    )
    required_version = "2026.07.31.2"
    service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        required_client_version=required_version,
        rollout_previous_client_version="2026.07.31.1",
        client_rollout_grace_seconds=900,
    )
    try:
        assert service.client_rollout_grace_remaining_seconds == 900
        allocation = service.allocate_browser_endpoint(
            "old-client",
            "Alice",
            "2026.07.31.1",
        )
        assert allocation["browser_endpoint"].startswith("http://127.0.0.1:")

        for supplied in ("", "invalid", "2026.07.30.9", "2026.07.31.3"):
            with pytest.raises(ClientUpdateRequiredError):
                service.allocate_browser_endpoint(
                    f"rejected-{supplied}",
                    "Alice",
                    supplied,
                )

        now[0] += 901
        assert service.client_rollout_grace_remaining_seconds == 0
        # A process admitted during the rollout remains usable and is not
        # terminated mid-session. Only a new old-version registration fails.
        assert "revision" in service.heartbeat("old-client")
        with pytest.raises(ClientUpdateRequiredError):
            service.allocate_browser_endpoint(
                "expired-old-client",
                "Alice",
                "2026.07.31.1",
            )
    finally:
        service.close()


def test_absolute_rollout_deadline_does_not_reopen_after_server_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_000.0]
    monkeypatch.setattr(
        coordination_service_module.time,
        "time",
        lambda: now[0],
    )
    database = tmp_path / "coordination.sqlite3"
    kwargs = {
        "required_client_version": "2026.07.31.2",
        "rollout_previous_client_version": "2026.07.31.1",
        "client_rollout_grace_seconds": 900,
        "client_rollout_grace_deadline_epoch": 1_900,
    }
    first = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(database),
        **kwargs,
    )
    try:
        assert first.client_rollout_grace_remaining_seconds == 900
        assert first.client_rollout_grace_deadline_epoch == 1_900
    finally:
        first.close()

    now[0] = 1_600.0
    restarted = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(database),
        **kwargs,
    )
    try:
        assert restarted.client_rollout_grace_remaining_seconds == 300
        now[0] = 1_901.0
        assert restarted.client_rollout_grace_remaining_seconds == 0
        with pytest.raises(ClientUpdateRequiredError):
            restarted.allocate_browser_endpoint(
                "late-old-client",
                "Alice",
                "2026.07.31.1",
            )
    finally:
        restarted.close()


def test_pending_rollout_accepts_only_previous_version_until_channel_activation(
    tmp_path: Path,
) -> None:
    service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        required_client_version="2026.07.31.2",
        rollout_previous_client_version="2026.07.31.1",
        client_rollout_grace_seconds=900,
        client_rollout_pending_activation=True,
    )
    try:
        assert service.client_rollout_pending_activation is True
        assert service.client_rollout_grace_remaining_seconds == 0
        assert service.client_rollout_grace_deadline_epoch == 0
        allocation = service.allocate_browser_endpoint(
            "pending-old-client",
            "Alice",
            "2026.07.31.1",
        )
        assert allocation["browser_endpoint"].startswith("http://127.0.0.1:")
        for supplied in ("", "2026.07.30.9", "2026.07.31.3"):
            with pytest.raises(ClientUpdateRequiredError):
                service.allocate_browser_endpoint(
                    f"pending-rejected-{supplied}",
                    "Alice",
                    supplied,
                )
    finally:
        service.close()


def test_server_rollout_marker_distinguishes_pending_from_absolute_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "client-rollout-deadline"
    monkeypatch.delenv("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_EPOCH", raising=False)
    monkeypatch.setenv(
        "ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_FILE",
        str(marker),
    )

    marker.write_text("pending\n", encoding="utf-8")
    assert (
        coordination_server_main._read_client_rollout_pending_activation()
        is True
    )
    assert (
        coordination_server_main._read_client_rollout_grace_deadline_epoch()
        == 0
    )

    marker.write_text("1900\n", encoding="utf-8")
    assert (
        coordination_server_main._read_client_rollout_pending_activation()
        is False
    )
    assert (
        coordination_server_main._read_client_rollout_grace_deadline_epoch()
        == 1_900
    )


def test_missing_optional_rollout_markers_safely_disable_rollout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline_marker = tmp_path / "missing-client-rollout-deadline"
    previous_marker = tmp_path / "missing-previous-client-version"
    monkeypatch.delenv("ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_EPOCH", raising=False)
    monkeypatch.delenv(
        "ERP_ROLLOUT_PREVIOUS_CLIENT_VERSION",
        raising=False,
    )
    monkeypatch.setenv(
        "ERP_CLIENT_ROLLOUT_GRACE_DEADLINE_FILE",
        str(deadline_marker),
    )
    monkeypatch.setenv(
        "ERP_ROLLOUT_PREVIOUS_CLIENT_VERSION_FILE",
        str(previous_marker),
    )

    assert (
        coordination_server_main._read_client_rollout_pending_activation()
        is False
    )
    assert (
        coordination_server_main._read_client_rollout_grace_deadline_epoch()
        == 0
    )
    assert coordination_server_main._read_rollout_previous_client_version() == ""


def test_server_restart_restores_an_active_clients_browser_endpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "coordination.sqlite3"
    old_service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(database),
        required_client_version="2026.07.31.1",
    )
    try:
        allocation = old_service.allocate_browser_endpoint(
            "still-open-client",
            "Alice",
            "2026.07.31.1",
        )
        expected_endpoint = allocation["browser_endpoint"]
        expected_logistics_endpoint = allocation["logistics_browser_endpoint"]
        assert expected_logistics_endpoint != expected_endpoint
    finally:
        old_service.close()

    new_service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(database),
        required_client_version="2026.07.31.2",
        rollout_previous_client_version="2026.07.31.1",
        client_rollout_grace_seconds=900,
    )
    try:
        registered = new_service.register(
            "still-open-client",
            "Alice",
            client_version="2026.07.31.1",
        )
        assert registered["browser_endpoint"] == expected_endpoint
        assert (
            registered["logistics_browser_endpoint"]
            == expected_logistics_endpoint
        )

        other = new_service.allocate_browser_endpoint(
            "another-client",
            "Bob",
            "2026.07.31.2",
        )
        assert other["browser_endpoint"] != expected_endpoint
        assert other["logistics_browser_endpoint"] not in {
            expected_endpoint,
            expected_logistics_endpoint,
            other["browser_endpoint"],
        }
    finally:
        new_service.close()


def test_unregistered_instance_cannot_bypass_client_version_gate(
    tmp_path: Path,
) -> None:
    service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        required_client_version="2026.07.31.2",
    )
    try:
        with pytest.raises(ValueError, match="registration expired"):
            service.snapshot_payload("never-registered")
        with pytest.raises(ValueError, match="registration expired"):
            service.heartbeat("never-registered")
    finally:
        service.close()


@pytest.mark.parametrize("grace_seconds", [-1, 86_401, float("inf")])
def test_service_rejects_invalid_rollout_grace(
    tmp_path: Path,
    grace_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="rollout grace"):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            client_rollout_grace_seconds=grace_seconds,
        )


@pytest.mark.parametrize("deadline", [-1, float("inf"), float("nan")])
def test_service_rejects_invalid_rollout_deadline(
    tmp_path: Path,
    deadline: float,
) -> None:
    with pytest.raises(ValueError, match="rollout grace deadline"):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            rollout_previous_client_version="2026.07.31.1",
            client_rollout_grace_deadline_epoch=deadline,
        )


def test_service_rejects_rollout_deadline_without_previous_version(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires a previous client version"):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            client_rollout_grace_deadline_epoch=1_900,
        )


def test_service_rejects_pending_rollout_without_previous_version(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="Pending client rollout activation requires",
    ):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            client_rollout_pending_activation=True,
        )


def test_service_rejects_pending_rollout_with_deadline(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="cannot already have a deadline",
    ):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            rollout_previous_client_version="2026.07.31.1",
            client_rollout_grace_deadline_epoch=1_900,
            client_rollout_pending_activation=True,
        )


@pytest.mark.parametrize(
    "previous_version",
    ["invalid", "2026.07.31.2", "2026.07.31.3"],
)
def test_service_rejects_invalid_rollout_previous_version(
    tmp_path: Path,
    previous_version: str,
) -> None:
    with pytest.raises(ValueError, match="previous client version"):
        CoordinatedControllerService(
            InMemoryBackgroundTaskController(),
            CoordinationStore(tmp_path / "coordination.sqlite3"),
            required_client_version="2026.07.31.2",
            rollout_previous_client_version=previous_version,
            client_rollout_grace_seconds=900,
        )


def test_http_server_reports_and_enforces_required_client_version(
    tmp_path: Path,
) -> None:
    client_version = (
        Path(__file__).resolve().parents[1] / "CLIENT_VERSION"
    ).read_text(encoding="utf-8").strip()
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        required_client_version=client_version,
    )
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        health = httpx.get(f"{url}/health").json()
        assert health["required_client_version"] == client_version
        assert health["rollout_previous_client_version"] == ""
        assert health["client_rollout_pending_activation"] is False
        assert health["client_rollout_grace_remaining_seconds"] == 0
        assert health["client_rollout_grace_deadline_epoch"] == 0

        rejected = httpx.post(
            f"{url}/v1/instances/browser-endpoint",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "instance_id": "old-client",
                "display_name": "Old",
                "client_version": "2026.07.24.2",
            },
        )
        assert rejected.status_code == 426
        assert rejected.json()["error"] == "client_update_required"
        assert rejected.json()["required_version"] == client_version
        with pytest.raises(CoordinationClientUpdateRequired) as update_error:
            RemoteBackgroundTaskController(
                url,
                token=token,
                display_name="Old",
                client_version="2026.07.24.2",
                strict_registration=True,
            )
        assert update_error.value.required_version == client_version
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


def test_expired_previous_client_drains_owned_task_before_forced_update(
    tmp_path: Path,
) -> None:
    required_version = "2026.07.31.3"
    previous_version = "2026.07.31.2"
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        required_client_version=required_version,
        rollout_previous_client_version=previous_version,
        client_rollout_grace_seconds=900,
    )
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client = RemoteBackgroundTaskController(
        url,
        token=token,
        display_name="Old",
        instance_id="old-client",
        client_version=previous_version,
        strict_registration=True,
    )
    try:
        client.snapshot()
        assert (
            store.acquire(
                resources=("custom-order:A",),
                instance_id=client.instance_id,
                request_id="existing-request",
                operation="submit_task",
                ttl_seconds=120,
            )
            is None
        )
        store.bind_task(
            "existing-request",
            "existing-task",
            ttl_seconds=120,
        )
        service._client_rollout_grace_deadline_epoch = time.time() - 1
        # Simulate a network outage long enough for the desktop registration
        # to expire while its server-owned task remains active.
        store.deregister(client.instance_id)

        draining = client.snapshot()

        assert "只能完成已经开始的任务" in draining.backend_message
        assert store.instance_has_active_tasks(client.instance_id) is True
        rejected = client.submit_task(
            TaskCommand(
                "new task",
                TaskArea.CUSTOMIZATION,
                Capability.LIST_ORDERS,
                order_no="B",
            )
        )
        assert rejected.accepted is False
        assert required_version in rejected.message
        assert store.instance_has_active_tasks(client.instance_id) is True
        stopped = client.set_emergency_stop_writes(True)
        assert stopped.accepted is True
        assert controller.snapshot().policy.emergency_stop_writes is True
        resumed = client.set_emergency_stop_writes(False)
        assert resumed.accepted is False
        assert required_version in resumed.message
        assert controller.snapshot().policy.emergency_stop_writes is True

        store.release_task("existing-task")
        with pytest.raises(CoordinationClientUpdateRequired) as update:
            client.snapshot()
        assert update.value.required_version == required_version
    finally:
        client.prepare_close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


@pytest.mark.parametrize("required_version", ["", "latest", "2026.7.31.3"])
def test_remote_controller_rejects_malformed_required_update_version(
    required_version: str,
) -> None:
    class FakeClient:
        def request(self, method: str, path: str, **_kwargs):
            request = httpx.Request(method, f"https://erp-auth.example{path}")
            return httpx.Response(
                426,
                request=request,
                json={
                    "error": "client_update_required",
                    "required_version": required_version,
                },
            )

    client = object.__new__(RemoteBackgroundTaskController)
    client._client = FakeClient()
    client._lock = threading.RLock()
    client._access_token_provider = None
    client._authentication_required = False
    client._authentication_error = ""
    client._revision = 0
    client._last_error = ""
    client.instance_id = "desktop-one"

    with pytest.raises(
        CoordinationConnectionError,
        match="强制更新版本无效",
    ) as rejected:
        client._request("GET", "/v1/snapshot")

    assert not isinstance(rejected.value, CoordinationClientUpdateRequired)


def test_every_controller_operation_is_exposed_by_coordination_rpc() -> None:
    protocol_methods = {
        name
        for name, value in BackgroundTaskController.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert RPC_METHODS == protocol_methods - {"snapshot", "prepare_close"}


def test_host_key_backend_authenticates_ciphertext() -> None:
    backend = HostKeyAesGcmBackend(b"k" * 32)
    encoded = backend.encrypt(b"secret", purpose=b"test-purpose")

    assert backend.decrypt(encoded, purpose=b"test-purpose") == b"secret"
    damaged = encoded[:-1] + bytes([encoded[-1] ^ 1])
    try:
        backend.decrypt(damaged, purpose=b"test-purpose")
    except Exception as exc:
        assert "authentication failed" in str(exc)
    else:  # pragma: no cover - cryptographic invariant
        raise AssertionError("Damaged ciphertext was accepted")


def test_store_claims_resource_set_atomically_and_reports_owner(tmp_path: Path) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.register_instance("one", "Alice", ttl_seconds=60)
    store.register_instance("two", "Bob", ttl_seconds=60)

    assert (
        store.acquire(
            resources=("custom-order:A", "capability:list_orders"),
            instance_id="one",
            request_id="request-one",
            operation="submit_task",
            ttl_seconds=60,
        )
        is None
    )
    conflict = store.acquire(
        resources=("custom-order:A", "custom-order:B"),
        instance_id="two",
        request_id="request-two",
        operation="set_custom_stage_states",
        ttl_seconds=60,
    )

    assert conflict is not None
    assert conflict.resource == "custom-order:a"
    assert conflict.owner_display_name == "Alice"
    assert {lease["resource"] for lease in store.active_leases()} == {
        "capability:list_orders",
        "custom-order:a",
    }


def test_live_cleanup_preserves_task_leases_until_explicit_release(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    store.register_instance("one", "Alice", ttl_seconds=10)
    assert (
        store.acquire(
            resources=("custom-order:A",),
            instance_id="one",
            request_id="running-request",
            operation="submit_task",
            ttl_seconds=10,
        )
        is None
    )
    store.bind_task("running-request", "running-task", ttl_seconds=10)

    now[0] = 2_000.0
    store.cleanup_expired(include_task_leases=False)

    assert store.instance_has_active_tasks("one") is True
    assert [lease["task_id"] for lease in store.active_leases()] == [
        "running-task"
    ]
    store.release_task("running-task")
    assert store.active_leases() == []


def test_service_startup_clears_orphan_task_leases(tmp_path: Path) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.register_instance("old-process", "Old Process", ttl_seconds=60)
    assert (
        store.acquire(
            resources=("custom-order:A",),
            instance_id="old-process",
            request_id="orphan-request",
            operation="submit_task",
            ttl_seconds=60,
        )
        is None
    )
    store.bind_task("orphan-request", "orphan-task", ttl_seconds=60)
    assert store.instance_has_active_tasks("old-process") is True

    service = CoordinatedControllerService(
        InMemoryBackgroundTaskController(),
        store,
    )
    try:
        assert store.instance_has_active_tasks("old-process") is False
        assert store.active_leases() == []
        assert store.global_execution_paused() is True
        assert service.controller is not None
        assert service.controller.snapshot().policy.execution_paused is True
    finally:
        service.close()


def test_deployment_drain_atomically_blocks_new_write_leases(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    database = tmp_path / "coordination.sqlite3"
    store = CoordinationStore(database, clock=lambda: now[0])
    store.register_instance("one", "Alice", ttl_seconds=60)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE coordination_meta
            SET value = ?
            WHERE key = 'deployment_drain_until'
            """,
            (1_060,),
        )

    conflict = store.acquire(
        resources=("custom-order:A",),
        instance_id="one",
        request_id="blocked-during-deploy",
        operation="submit_task",
        ttl_seconds=60,
    )
    assert conflict is not None
    assert conflict.resource == "server:production-deployment"
    assert conflict.owner_instance_id == "server"
    assert store.active_leases() == []

    assert (
        store.acquire(
            resources=("task:running-task",),
            instance_id="one",
            request_id="cancel-running-task",
            operation="cancel_task",
            ttl_seconds=60,
            allow_during_deployment_drain=True,
        )
        is None
    )
    assert {
        lease["resource"] for lease in store.active_leases()
    } == {"task:running-task"}
    store.release_request("cancel-running-task")

    now[0] = 1_061
    assert (
        store.acquire(
            resources=("custom-order:A",),
            instance_id="one",
            request_id="accepted-after-drain",
            operation="submit_task",
            ttl_seconds=60,
        )
        is None
    )


def test_service_keeps_task_lease_until_task_is_terminal(tmp_path: Path) -> None:
    controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    service.register("two", "Bob")
    command = TaskCommand(
        "scan A",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="A",
    )
    try:
        first = service.invoke(
            instance_id="one",
            request_id="request-one",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )
        second = service.invoke(
            instance_id="two",
            request_id="request-two",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )

        assert first["result"]["accepted"] is True
        assert second["result"]["accepted"] is False
        assert second["result"]["details"]["conflict"] is True
        task_id = str(first["result"]["task_id"])
        assert any(lease["task_id"] == task_id for lease in store.active_leases())

        controller.cancel_task(task_id)
        deadline = time.monotonic() + 2
        while store.active_leases() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert store.active_leases() == []
    finally:
        service.close()


def test_deployment_drain_allows_existing_task_cancel_but_blocks_new_task(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    first_command = TaskCommand(
        "scan A",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="A",
    )
    try:
        first = service.invoke(
            instance_id="one",
            request_id="request-one",
            method="submit_task",
            raw_args=[to_jsonable(first_command)],
            raw_kwargs={},
        )
        task_id = str(first["result"]["task_id"])
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                """
                UPDATE coordination_meta
                SET value = ?
                WHERE key = 'deployment_drain_until'
                """,
                (int(time.time() + 600),),
            )

        blocked = service.invoke(
            instance_id="one",
            request_id="request-two",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "scan B",
                        TaskArea.CUSTOMIZATION,
                        Capability.LIST_ORDERS,
                        order_no="B",
                    )
                )
            ],
            raw_kwargs={},
        )
        assert blocked["result"]["accepted"] is False
        assert (
            blocked["result"]["details"]["resource"]
            == "server:production-deployment"
        )

        cancelled = service.invoke(
            instance_id="one",
            request_id="cancel-one",
            method="cancel_task",
            raw_args=[task_id],
            raw_kwargs={},
        )
        assert cancelled["result"]["accepted"] is True
    finally:
        service.close()


def test_snapshot_failure_conservatively_renews_running_task_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = [1_000.0]
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(
            instance_ttl_seconds=30,
            transient_lease_seconds=10,
            task_lease_seconds=30,
            monitor_interval_seconds=0.01,
        ),
    )
    service.register("one", "Alice")
    command = TaskCommand(
        "scan A",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="A",
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="request-one",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )
        assert response["result"]["accepted"] is True
        task_id = str(response["result"]["task_id"])

        def unavailable_snapshot():
            raise RuntimeError("temporary state database outage")

        monkeypatch.setattr(controller, "snapshot", unavailable_snapshot)
        now[0] = 1_020.0
        service.heartbeat("one")
        deadline = time.monotonic() + 2
        while (
            not any(
                lease["task_id"] == task_id
                and lease["expires_at"] >= 1_050.0
                for lease in store.active_leases()
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        matching = [
            lease
            for lease in store.active_leases()
            if lease["task_id"] == task_id
        ]
        assert matching
        assert all(lease["expires_at"] >= 1_050.0 for lease in matching)
    finally:
        service.close()


def test_expired_task_owner_triggers_global_pause_and_releases_lease(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(
            instance_ttl_seconds=30,
            transient_lease_seconds=10,
            task_lease_seconds=30,
            monitor_interval_seconds=0.01,
        ),
    )
    service.register("lost-owner", "Alice")
    try:
        response = service.invoke(
            instance_id="lost-owner",
            request_id="lost-owner-task",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "scan before outage",
                        TaskArea.CUSTOMIZATION,
                        Capability.LIST_ORDERS,
                        order_no="OUTAGE-A",
                    )
                )
            ],
            raw_kwargs={},
        )
        task_id = str(response["result"]["task_id"])
        now[0] = 2_000.0

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshot = controller.snapshot()
            task = next(item for item in snapshot.tasks if item.task_id == task_id)
            if (
                snapshot.policy.execution_paused
                and task.status is TaskStatus.PAUSED
                and not any(
                    lease["task_id"] == task_id
                    for lease in store.active_leases()
                )
            ):
                break
            time.sleep(0.01)

        snapshot = controller.snapshot()
        task = next(item for item in snapshot.tasks if item.task_id == task_id)
        assert snapshot.policy.execution_paused is True
        assert snapshot.policy.emergency_stop_writes is True
        assert task.status is TaskStatus.PAUSED
        assert not any(
            lease["task_id"] == task_id
            for lease in store.active_leases()
        )
        assert store.global_execution_paused() is True
    finally:
        service.close()


def test_stale_foreign_owner_no_longer_blocks_task_cancellation(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(
            instance_ttl_seconds=30,
            task_lease_seconds=90,
            monitor_interval_seconds=10,
        ),
    )
    service.register("old-computer", "Alice")
    try:
        submitted = service.invoke(
            instance_id="old-computer",
            request_id="old-computer-task",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "stale task",
                        TaskArea.MAINTENANCE,
                        Capability.LIST_ORDERS,
                    )
                )
            ],
            raw_kwargs={},
        )
        task_id = str(submitted["result"]["task_id"])
        now[0] = 1_040.0
        service.register("current-computer", "Alice")

        cancelled = service.invoke(
            instance_id="current-computer",
            request_id="cancel-stale-task",
            method="cancel_task",
            raw_args=[task_id],
            raw_kwargs={},
        )

        assert cancelled["result"]["accepted"] is True
        task = next(
            item for item in controller.snapshot().tasks
            if item.task_id == task_id
        )
        assert task.status is TaskStatus.CANCELLED
    finally:
        service.close()


def test_live_foreign_tasks_are_skipped_without_blocking_local_batch_pause(
    tmp_path: Path,
) -> None:
    controller, _store, service = _service(tmp_path)
    service.register("one", "Alice")
    service.register("two", "Bob")
    try:
        local = service.invoke(
            instance_id="one",
            request_id="local-task",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "local scan",
                        TaskArea.MAINTENANCE,
                        Capability.LIST_ORDERS,
                        order_no="LOCAL-A",
                    )
                )
            ],
            raw_kwargs={},
        )
        foreign = service.invoke(
            instance_id="two",
            request_id="foreign-task",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "foreign scan",
                        TaskArea.MAINTENANCE,
                        Capability.LIST_ORDERS,
                        order_no="FOREIGN-B",
                    )
                )
            ],
            raw_kwargs={},
        )
        local_id = str(local["result"]["task_id"])
        foreign_id = str(foreign["result"]["task_id"])

        result = service.invoke(
            instance_id="one",
            request_id="cancel-mixed-batch",
            method="cancel_tasks",
            raw_args=[[local_id, foreign_id]],
            raw_kwargs={},
        )

        assert result["result"]["accepted"] is True
        assert result["result"]["details"]["partial_success"] is True
        tasks = {task.task_id: task for task in controller.snapshot().tasks}
        assert tasks[local_id].status is TaskStatus.CANCELLED
        assert tasks[foreign_id].status is TaskStatus.QUEUED
    finally:
        service.close()


def test_global_pause_blocks_business_mutations_until_explicit_resume(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    try:
        paused = service.invoke(
            instance_id="one",
            request_id="pause-all",
            method="set_execution_paused",
            raw_args=[True, "safety test"],
            raw_kwargs={},
        )
        assert paused["result"]["accepted"] is True
        assert store.global_execution_paused() is True

        blocked = service.invoke(
            instance_id="one",
            request_id="blocked-mode-change",
            method="update_capability_mode",
            raw_args=[
                Capability.LIST_ORDERS.value,
                CapabilityMode.DISABLED.value,
            ],
            raw_kwargs={},
        )
        assert blocked["result"]["accepted"] is False
        assert blocked["result"]["details"]["execution_paused"] is True

        emergency_lift = service.invoke(
            instance_id="one",
            request_id="blocked-emergency-lift",
            method="set_emergency_stop_writes",
            raw_args=[False],
            raw_kwargs={},
        )
        assert emergency_lift["result"]["accepted"] is False

        resumed = service.invoke(
            instance_id="one",
            request_id="resume-all",
            method="set_execution_paused",
            raw_args=[False],
            raw_kwargs={},
        )
        assert resumed["result"]["accepted"] is True
        assert store.global_execution_paused() is False
        assert controller.snapshot().policy.execution_paused is False
        assert controller.snapshot().policy.emergency_stop_writes is True
    finally:
        service.close()


def test_notification_batch_lease_blocks_overlapping_ids_across_clients(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    controller.set_emergency_stop_writes(False)
    service.register("one", "Alice")
    service.register("two", "Bob")
    first = TaskCommand(
        "发送客户通知（2 条）",
        TaskArea.SHIPMENT,
        Capability.SEND_NOTIFICATION,
        order_no="shipment-notifications:71,72",
        payload={
            "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
            "notification_ids": [71, "072", 72, 0, "invalid"],
        },
    )
    overlapping = TaskCommand(
        "发送客户通知（2 条）",
        TaskArea.SHIPMENT,
        Capability.SEND_NOTIFICATION,
        order_no="shipment-notifications:72,73",
        payload={
            "trigger": SHIPMENT_NOTIFICATION_SEND_TRIGGER,
            "notification_ids": [72, "073"],
        },
    )
    try:
        accepted = service.invoke(
            instance_id="one",
            request_id="notification-batch-one",
            method="submit_task",
            raw_args=[to_jsonable(first)],
            raw_kwargs={},
        )
        conflict = service.invoke(
            instance_id="two",
            request_id="notification-batch-two",
            method="submit_task",
            raw_args=[to_jsonable(overlapping)],
            raw_kwargs={},
        )

        assert accepted["result"]["accepted"] is True
        assert conflict["result"]["accepted"] is False
        assert conflict["result"]["details"]["queue_conflict"] is True
        assert conflict["result"]["details"]["conflict_notification_ids"] == [72]
        assert conflict["result"]["details"]["conflict_operator_name"] == "Alice"
        assert {
            lease["resource"] for lease in store.active_leases()
        } == {
            "notification:71",
            "notification:72",
            "order:shipment-notifications:71,72",
        }
    finally:
        service.close()


def test_browser_tasks_bind_to_the_submitting_desktop_endpoint(tmp_path: Path) -> None:
    controller, _store, service = _service(tmp_path)
    controller.set_emergency_stop_writes(False)
    allocation = service.allocate_browser_endpoint("one", "Alice")
    service.register(
        "one",
        "Alice",
        str(allocation["browser_endpoint"]),
    )
    command = TaskCommand(
        "process order",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="A",
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="browser-task",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )

        assert response["result"]["accepted"] is True
        task = controller.snapshot().tasks[0]
        assert (
            task.payload[DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY]
            == allocation["browser_endpoint"]
        )
    finally:
        service.close()


def test_logistics_query_task_binds_to_dedicated_browser_endpoint(
    tmp_path: Path,
) -> None:
    controller, _store, service = _service(tmp_path)
    allocation = service.allocate_browser_endpoint("one", "Alice")
    service.register(
        "one",
        "Alice",
        str(allocation["browser_endpoint"]),
        logistics_browser_endpoint=str(
            allocation["logistics_browser_endpoint"]
        ),
    )
    command = TaskCommand(
        "query Alibaba logistics",
        TaskArea.SHIPMENT,
        Capability.ALIBABA_LOGISTICS,
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="logistics-browser-task",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )

        assert response["result"]["accepted"] is True
        task = controller.snapshot().tasks[0]
        assert (
            task.payload[DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY]
            == allocation["logistics_browser_endpoint"]
        )
        assert (
            task.payload[DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY]
            != allocation["browser_endpoint"]
        )
    finally:
        service.close()


def test_browser_task_is_rejected_without_desktop_endpoint(tmp_path: Path) -> None:
    _controller, _store, service = _service(tmp_path)
    service.register("one", "Alice")
    command = TaskCommand(
        "process order",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="A",
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="browser-task",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )

        assert response["result"]["accepted"] is False
        assert "可见 Chrome 通道未连接" in response["result"]["message"]
    finally:
        service.close()


def test_interaction_is_visible_only_to_task_owner(tmp_path: Path) -> None:
    class InteractionController(InMemoryBackgroundTaskController):
        def __init__(self):
            super().__init__()
            self.requests: tuple[DesktopInteractionRequest, ...] = ()

        def pending_interactions(self):
            return self.requests

        def respond_interaction(self, response):
            return ControlResult(True, "accepted", self.requests[0].task_id)

    controller = InteractionController()
    controller.set_emergency_stop_writes(False)
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store)
    allocation = service.allocate_browser_endpoint("one", "Alice")
    service.register("one", "Alice", str(allocation["browser_endpoint"]))
    service.register("two", "Bob")
    command = TaskCommand(
        "process order",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="A",
    )
    try:
        submitted = service.invoke(
            instance_id="one",
            request_id="submit-owner-task",
            method="submit_task",
            raw_args=[to_jsonable(command)],
            raw_kwargs={},
        )
        task_id = str(submitted["result"]["task_id"])
        controller.requests = (
            DesktopInteractionRequest(
                request_id="review-one",
                task_id=task_id,
                stage="review",
                title="Review",
                message="Confirm",
                target_instance_id="one",
                non_blocking=True,
            ),
        )
        service._task_owners.pop(task_id, None)

        assert len(service.snapshot_payload("one")["interactions"]) == 1
        assert service.snapshot_payload("two")["interactions"] == []
        rejected = service.invoke(
            instance_id="two",
            request_id="respond-from-wrong-instance",
            method="respond_interaction",
            raw_args=[
                to_jsonable(
                    DesktopInteractionResponse(
                        request_id="review-one",
                        accepted=True,
                    )
                )
            ],
            raw_kwargs={},
        )
        assert rejected["result"]["accepted"] is False
        assert "另一台电脑" in rejected["result"]["message"]
    finally:
        service.close()


def test_local_chrome_host_caches_startup_failure_without_reopening_windows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"test")
    launches: list[list[str]] = []
    clock = 100.0

    class ExitedProcess:
        def poll(self) -> int:
            return 1

    def monotonic() -> float:
        nonlocal clock
        clock += 0.25
        return clock

    def popen(command, **_kwargs):
        launches.append(list(command))
        return ExitedProcess()

    host = LocalChromeHost(
        24000,
        tmp_path / "profile",
        executable=executable,
        startup_timeout_seconds=3,
        startup_failure_cooldown_seconds=30,
    )
    monkeypatch.setattr(host, "_healthy", lambda: False)
    monkeypatch.setattr(local_browser.time, "monotonic", monotonic)
    monkeypatch.setattr(local_browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(local_browser.subprocess, "Popen", popen)

    with pytest.raises(LocalBrowserUnavailable) as first:
        host.ensure_started()
    with pytest.raises(LocalBrowserUnavailable) as second:
        host.ensure_started()

    assert len(launches) == 1
    assert "--new-window" in launches[0]
    assert "系统已停止重复打开 Chrome" in str(first.value)
    assert str(second.value) == str(first.value)


def test_local_chrome_host_open_url_uses_target_for_cold_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host = LocalChromeHost(24000, tmp_path / "profile")
    starts: list[str] = []

    def ensure_started(*, initial_url: str = "about:blank") -> bool:
        starts.append(initial_url)
        return True

    monkeypatch.setattr(host, "ensure_started", ensure_started)
    monkeypatch.setattr(
        local_browser.httpx,
        "get",
        lambda *_args, **_kwargs: pytest.fail(
            "冷启动已直接打开目标页，不应再查询标签页。"
        ),
    )
    monkeypatch.setattr(
        local_browser.httpx,
        "put",
        lambda *_args, **_kwargs: pytest.fail(
            "冷启动已直接打开目标页，不应再新建标签页。"
        ),
    )

    host.open_url(ALIBABA_SCM_HOME_URL)

    assert starts == [ALIBABA_SCM_HOME_URL]


def test_local_chrome_host_open_url_activates_target_for_warm_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host = LocalChromeHost(24000, tmp_path / "profile")
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload=None) -> None:
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        host,
        "ensure_started",
        lambda *, initial_url="about:blank": False,
    )

    def get(url: str, **_kwargs):
        requested_urls.append(url)
        if url.endswith("/json/list"):
            return Response(
                [
                    {
                        "id": "scm-home",
                        "type": "page",
                        "url": ALIBABA_SCM_HOME_URL,
                    }
                ]
            )
        return Response()

    monkeypatch.setattr(local_browser.httpx, "get", get)
    monkeypatch.setattr(
        local_browser.httpx,
        "put",
        lambda *_args, **_kwargs: pytest.fail(
            "热进程已有 SCM 首页时应激活原标签页。"
        ),
    )

    host.open_url(ALIBABA_SCM_HOME_URL)

    assert requested_urls == [
        "http://127.0.0.1:24000/json/list",
        "http://127.0.0.1:24000/json/activate/scm-home",
    ]


def test_local_chrome_host_closes_all_dedicated_profile_pages(
    monkeypatch,
    tmp_path: Path,
) -> None:
    host = LocalChromeHost(24000, tmp_path / "profile")
    closed_urls: list[str] = []

    class Response:
        def __init__(self, payload=None) -> None:
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self) -> None:
            return None

    def get(url: str, **_kwargs):
        if url.endswith("/json/list"):
            return Response(
                [
                    {"id": "page-1", "type": "page"},
                    {"id": "worker-1", "type": "service_worker"},
                    {"id": "page-2", "type": "page"},
                ]
            )
        closed_urls.append(url)
        return Response()

    monkeypatch.setattr(host, "_healthy", lambda: True)
    monkeypatch.setattr(local_browser.httpx, "get", get)

    assert host.close_pages() == 2
    assert closed_urls == [
        "http://127.0.0.1:24000/json/close/page-1",
        "http://127.0.0.1:24000/json/close/page-2",
    ]


def test_remote_browser_pages_close_only_after_tracked_batch_is_terminal() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.close_count = 0

        def close_pages(self) -> None:
            self.close_count += 1

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client.instance_id = "desktop-a"
    client._browser_host = host
    client._browser_cleanup_task_ids = {"task-one", "task-two"}
    client._browser_close_pending = False

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "task-one",
                    "custom one",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
                TaskRecord(
                    "task-two",
                    "custom two",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.RUNNING,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
            ]
        )
    )
    assert host.close_count == 0
    assert client._browser_cleanup_task_ids == {"task-two"}

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "task-two",
                    "custom two",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                )
            ]
        )
    )
    assert host.close_count == 1
    assert client._browser_cleanup_task_ids == set()
    assert client._browser_close_pending is False


def test_remote_browser_close_waits_for_other_same_desktop_task() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.close_count = 0

        def close_pages(self) -> None:
            self.close_count += 1

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client.instance_id = "desktop-a"
    client._browser_host = host
    client._browser_cleanup_task_ids = {"completed-browser-task"}
    client._browser_close_pending = False

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "completed-browser-task",
                    "custom browser task",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
                TaskRecord(
                    "active-alibaba-order",
                    "prepare Alibaba order",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_ORDER_PREPARE,
                    status=TaskStatus.RUNNING,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
            ]
        )
    )

    assert host.close_count == 0
    assert client._browser_cleanup_task_ids == set()
    assert client._browser_close_pending is True

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "active-alibaba-order",
                    "prepare Alibaba order",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_ORDER_PREPARE,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                )
            ]
        )
    )

    assert host.close_count == 1
    assert client._browser_close_pending is False


def test_remote_browser_close_ignores_api_only_and_other_desktop_tasks() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.close_count = 0

        def close_pages(self) -> None:
            self.close_count += 1

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client.instance_id = "desktop-a"
    client._browser_host = host
    client._browser_cleanup_task_ids = {"completed-browser-task"}
    client._browser_close_pending = False

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "completed-browser-task",
                    "custom browser task",
                    TaskArea.CUSTOMIZATION,
                    Capability.UPDATE_CONTACT,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
                TaskRecord(
                    "api-only-task",
                    "API-only notification task",
                    TaskArea.SHIPMENT,
                    Capability.SEND_NOTIFICATION,
                    status=TaskStatus.RUNNING,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
                TaskRecord(
                    "other-desktop-browser-task",
                    "other desktop Alibaba task",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_ORDER_PREPARE,
                    status=TaskStatus.RUNNING,
                    payload={"_desktop_instance_id": "desktop-b"},
                ),
            ]
        )
    )

    assert host.close_count == 1
    assert client._browser_close_pending is False


def test_completed_logistics_query_closes_only_its_profile_while_order_waits() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.close_count = 0

        def close_pages(self) -> None:
            self.close_count += 1

    order_host = BrowserHost()
    logistics_host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client.instance_id = "desktop-a"
    client._browser_host = order_host
    client._logistics_browser_host = logistics_host
    client._browser_cleanup_task_ids = set()
    client._browser_close_pending = False
    client._logistics_browser_cleanup_task_ids = {"completed-logistics"}
    client._logistics_browser_close_pending = False

    client._cleanup_browser_after_terminal_tasks(
        DesktopSnapshot(
            tasks=[
                TaskRecord(
                    "completed-logistics",
                    "query logistics",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_LOGISTICS,
                    status=TaskStatus.SUCCEEDED,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
                TaskRecord(
                    "order-awaiting-operator",
                    "fill Alibaba draft",
                    TaskArea.SHIPMENT,
                    Capability.ALIBABA_ORDER_DRAFT,
                    status=TaskStatus.WAITING_USER,
                    payload={"_desktop_instance_id": "desktop-a"},
                ),
            ]
        )
    )

    assert logistics_host.close_count == 1
    assert order_host.close_count == 0


def test_remote_browser_start_failure_is_marked_for_batch_fuse() -> None:
    class BrowserHost:
        def ensure_started(self) -> None:
            raise LocalBrowserUnavailable("本机专用 Chrome 未就绪。")

    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._browser_host = BrowserHost()
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: pytest.fail(
        "Chrome 启动失败后不应提交远端任务。"
    )
    command = TaskCommand(
        "处理定制订单",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="111-1",
    )

    result = client._rpc("submit_task", command)

    assert result.accepted is False
    assert result.details["local_browser_unavailable"] is True
    assert result.details["retry_suppressed"] is True


def test_remote_task_batch_starts_browser_once_and_decodes_each_result() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.start_count = 0

        def ensure_started(self) -> None:
            self.start_count += 1

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._local_pause_requested = False
    client._browser_host = host
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._browser_cleanup_task_ids = set()
    client._logistics_browser_cleanup_task_ids = set()
    client._request = lambda *_args, **_kwargs: {
        "revision": 3,
        "result_type": "control_results",
        "result": [
            {
                "accepted": True,
                "message": "已提交",
                "task_id": f"task-{index}",
                "details": {},
            }
            for index in range(3)
        ],
    }
    commands = tuple(
        TaskCommand(
            "处理定制订单",
            TaskArea.CUSTOMIZATION,
            Capability.UPDATE_CONTACT,
            order_no=f"111-{index}",
        )
        for index in range(3)
    )

    results = client._rpc("submit_tasks", commands)

    assert [result.task_id for result in results] == [
        "task-0",
        "task-1",
        "task-2",
    ]
    assert host.start_count == 1
    assert client._browser_cleanup_task_ids == {"task-0", "task-1", "task-2"}


def test_alibaba_order_prepare_opens_quote_directly_without_blank_page() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.opened: list[str] = []

        def open_url(self, url: str) -> None:
            self.opened.append(url)

        def ensure_started(self) -> None:
            pytest.fail("阿里物流下单不应通过默认 about:blank 启动 Chrome。")

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._browser_host = host
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: {
        "revision": 1,
        "result_type": "control_result",
        "result": {
            "accepted": True,
            "message": "已提交",
            "task_id": "prepare-one",
            "details": {},
        },
    }
    command = TaskCommand(
        "读取订单并打开阿里查价",
        TaskArea.SHIPMENT,
        Capability.ALIBABA_ORDER_PREPARE,
        order_no="103729824875289685",
    )

    result = client._rpc("submit_task", command)

    assert result.accepted is True
    assert host.opened == [ALIBABA_QUOTE_URL]


def test_alibaba_logistics_query_opens_home_only_in_query_browser_profile() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.opened_urls: list[str] = []

        def open_url(self, url: str) -> None:
            self.opened_urls.append(url)

        def ensure_started(self, *, initial_url: str = "about:blank") -> None:
            pytest.fail(
                "物流查询必须显式打开 SCM 首页，不能只确保 Chrome 已启动。"
            )

    order_host = BrowserHost()
    logistics_host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._browser_host = order_host
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._logistics_browser_host = logistics_host
    client.logistics_browser_endpoint = "http://127.0.0.1:24001"
    client._browser_cleanup_task_ids = set()
    client._logistics_browser_cleanup_task_ids = set()
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: {
        "revision": 1,
        "result_type": "control_result",
        "result": {
            "accepted": True,
            "message": "已提交",
            "task_id": "logistics-one",
            "details": {},
        },
    }
    command = TaskCommand(
        "查询阿里物流号",
        TaskArea.SHIPMENT,
        Capability.ALIBABA_LOGISTICS,
    )

    result = client._rpc("submit_task", command)

    assert result.accepted is True
    assert logistics_host.opened_urls == [local_browser.ALIBABA_SCM_HOME_URL]
    assert order_host.opened_urls == []
    assert client._logistics_browser_cleanup_task_ids == {"logistics-one"}
    assert client._browser_cleanup_task_ids == set()


def test_interaction_codec_preserves_ephemeral_display_data() -> None:
    request = DesktopInteractionRequest(
        request_id="quote-details",
        task_id="quote-task",
        stage="alibaba_order:quote_details",
        title="查价资料",
        message="transient",
        display_data={
            "destination_country_code": "CA",
            "destination_postal_code": "N2R 1A6",
        },
        target_instance_id="desktop-a",
        non_blocking=True,
    )

    decoded = decode_interactions(to_jsonable((request,)))

    assert len(decoded) == 1
    assert decoded[0].display_data == request.display_data
    assert decoded[0].target_instance_id == "desktop-a"
    assert decoded[0].non_blocking is True


def test_interaction_codec_preserves_ephemeral_local_action_data() -> None:
    request = DesktopInteractionRequest(
        request_id="local-fill",
        task_id="draft-task",
        stage="alibaba_order:fill_local_browser",
        title="本机填写",
        message="transient",
        target_instance_id="desktop-a",
        automatic_action=LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
        action_payload={"password": "ephemeral-secret", "detail": {"id": 1}},
    )
    response = DesktopInteractionResponse(
        request_id="local-fill",
        accepted=True,
        result_data={"route_name": "Express"},
    )

    decoded_request = decode_interactions(to_jsonable((request,)))[0]
    decoded_response = decode_interaction_response(to_jsonable(response))

    assert decoded_request.automatic_action == LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL
    assert decoded_request.action_payload == request.action_payload
    assert decoded_response.result_data == {"route_name": "Express"}
    assert "ephemeral-secret" not in repr(decoded_request)


def test_remote_local_action_retries_response_without_reexecuting_page_action() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    deliveries = 0

    class Executor:
        def execute(self, action, payload):
            calls.append((action, dict(payload)))
            return {"route_name": "Express"}

    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._local_action_executor = Executor()
    client._local_action_responses = {}
    client._local_action_inflight = set()
    client._automatic_interactions = {}
    client._snapshot_revision = 7

    def respond(_method, response):
        nonlocal deliveries
        deliveries += 1
        assert response.result_data == {"route_name": "Express"}
        return ControlResult(deliveries > 1, "response")

    client._rpc = respond
    request = DesktopInteractionRequest(
        request_id="local-fill",
        task_id="draft-task",
        stage="alibaba_order:fill_local_browser",
        title="本机填写",
        message="transient",
        automatic_action=LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
        action_payload={"detail": {"id": 1}},
    )

    client._execute_and_respond_local_action(request)
    client._execute_and_respond_local_action(request)

    assert calls == [
        (LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL, {"detail": {"id": 1}})
    ]
    assert deliveries == 2
    assert client._local_action_responses == {}


def test_remote_client_starts_chrome_only_for_approved_erp_fallback() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.starts = 0

        def ensure_started(self) -> None:
            self.starts += 1

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client._browser_host = host
    client.browser_endpoint = "http://127.0.0.1:19001"
    client._last_interactions = (
        DesktopInteractionRequest(
            request_id="fallback-one",
            task_id="task-one",
            stage="erp_mark:browser_fallback",
            title="网页回退",
            message="API 明确拒绝，是否改用网页？",
        ),
        DesktopInteractionRequest(
            request_id="ordinary-review",
            task_id="task-two",
            stage="erp_mark:waybill_review",
            title="审核",
            message="确认运单",
        ),
    )

    assert client._start_browser_for_approved_fallback(
        "respond_interaction",
        (DesktopInteractionResponse("fallback-one", False),),
    ) is None
    assert host.starts == 0

    assert client._start_browser_for_approved_fallback(
        "respond_interaction",
        (DesktopInteractionResponse("ordinary-review", True),),
    ) is None
    assert host.starts == 0

    assert client._start_browser_for_approved_fallback(
        "respond_interaction",
        (DesktopInteractionResponse("fallback-one", True),),
    ) is None
    assert host.starts == 1


def test_remote_client_discards_answered_interaction_before_next_snapshot() -> None:
    request = DesktopInteractionRequest(
        request_id="interaction-one",
        task_id="task-one",
        stage="contact_writeback",
        title="确认",
        message="确认写回",
    )
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._browser_host = None
    client.browser_endpoint = ""
    client.instance_id = "one"
    client._revision = 0
    client._last_interactions = (request,)
    client._request = lambda *_args, **_kwargs: {
        "revision": 1,
        "result_type": "control_result",
        "result": {
            "accepted": True,
            "message": "已提交确认结果。",
            "task_id": "task-one",
            "details": {},
        },
    }

    result = client._rpc(
        "respond_interaction",
        DesktopInteractionResponse("interaction-one", True),
    )

    assert result.accepted is True
    assert client.pending_interactions() == ()


def test_remote_scan_submission_does_not_open_unused_prewarm_page() -> None:
    class BrowserHost:
        def __init__(self) -> None:
            self.opened = []

        def open_url(self, url: str) -> None:
            self.opened.append(url)

    host = BrowserHost()
    client = object.__new__(RemoteBackgroundTaskController)
    client._browser_host = host
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot(
        shipments=[
            ShipmentRow(
                platform_order_no="111-8058023-1865004",
                logistics_no="ALS01829169726",
                identity_state="ACTIVE",
                logistics_state="WAITING",
                erp_state="WAITING",
            )
        ]
    )
    client._lock = threading.RLock()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: {
        "revision": 1,
        "result_type": "control_result",
        "result": {
            "accepted": True,
            "message": "已提交",
            "task_id": "scan-one",
            "details": {},
        },
    }
    command = TaskCommand(
        "扫描候选并查询物流",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={"local_visible_logistics_followup": True},
    )

    result = client._rpc("submit_task", command)

    assert result.accepted is True
    assert host.opened == []


def test_remote_notification_send_rpc_scales_only_the_read_timeout() -> None:
    client = object.__new__(RemoteBackgroundTaskController)
    client._browser_host = None
    client.browser_endpoint = ""
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._lock = threading.RLock()
    client._revision = 0
    client._timeout_seconds = 30.0
    client.instance_id = "desktop-one"
    captured: dict[str, object] = {}

    def request(method: str, path: str, **kwargs):
        captured.update({"method": method, "path": path, **kwargs})
        return {
            "revision": 1,
            "result_type": "control_result",
            "result": {
                "accepted": True,
                "message": "已发送",
                "details": {},
            },
        }

    client._request = request

    result = client._rpc("approve_shipment_notifications", [11, 12, 13])

    assert result.accepted is True
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/rpc"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0
    assert timeout.read == 345.0
    queue_timeout = client._rpc_request_timeout("list_shipment_notifications", ())
    assert isinstance(queue_timeout, httpx.Timeout)
    assert queue_timeout.read == 30.0
    detail_timeout = client._rpc_request_timeout(
        "get_shipment_notification_details", ([11],)
    )
    assert isinstance(detail_timeout, httpx.Timeout)
    assert detail_timeout.read == 30.0


def test_remote_notification_queue_read_failure_is_not_converted_to_empty_list() -> None:
    client = object.__new__(RemoteBackgroundTaskController)
    client._browser_host = None
    client.browser_endpoint = ""
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._lock = threading.RLock()
    client._revision = 0
    client._timeout_seconds = 5.0
    client._authentication_required = False
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        CoordinationConnectionError("queue read timed out")
    )

    with pytest.raises(CoordinationConnectionError, match="queue read timed out"):
        client._rpc(
            "list_shipment_notifications",
            page=1,
            page_size=50,
            search_field="all",
            search_query="",
            product_types=(),
        )


def test_remote_snapshot_clears_local_gate_after_another_client_resumes() -> None:
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._local_pause_requested = True
    client._fail_safe_pause_confirmed = True
    client._last_snapshot = DesktopSnapshot()
    client._last_snapshot.policy.execution_paused = True
    client._last_interactions = ()
    client._snapshot_revision = 1
    client._revision = 1
    client._last_error = ""
    client.instance_id = "desktop-one"
    client._request = lambda *_args, **_kwargs: {
        "revision": 2,
        "unchanged": False,
        "snapshot": to_jsonable(DesktopSnapshot()),
        "interactions": [],
    }

    snapshot = client.snapshot()

    assert snapshot.policy.execution_paused is False
    assert client._local_pause_requested is False
    assert client._fail_safe_pause_confirmed is False


def test_remote_pause_uses_fail_safe_endpoint_when_sso_is_expired() -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def post(self, path: str, **kwargs):
            captured.update({"path": path, **kwargs})
            request = httpx.Request("POST", f"http://127.0.0.1:18765{path}")
            return httpx.Response(
                200,
                request=request,
                json={
                    "ok": True,
                    "result_type": "control_result",
                    "result": {
                        "accepted": True,
                        "message": "server paused",
                        "task_id": None,
                        "details": {"execution_paused": True},
                    },
                },
            )

    client = object.__new__(RemoteBackgroundTaskController)
    client._client = FakeClient()
    client._lock = threading.RLock()
    client._authentication_required = True
    client._authentication_error = "expired"
    client._local_pause_requested = False
    client._fail_safe_pause_confirmed = False
    client._last_snapshot = DesktopSnapshot()

    result = client.set_execution_paused(True, "expired SSO safety test")

    assert result.accepted is True
    assert result.details["fail_safe_endpoint"] is True
    assert captured["path"] == "/v1/safety/pause"
    assert captured["json"] == {"reason": "expired SSO safety test"}
    assert client._last_snapshot.policy.execution_paused is True
    assert client._last_snapshot.policy.emergency_stop_writes is True
    assert client._fail_safe_pause_confirmed is True


def test_expired_access_token_never_opens_login_until_explicit_reauthentication() -> None:
    provider_calls = 0

    class FakeClient:
        def __init__(self) -> None:
            self.headers = {"Cf-Access-Token": "expired"}

        def request(self, method: str, path: str, **_kwargs):
            request = httpx.Request(method, f"https://erp-auth.example{path}")
            if self.headers.get("Cf-Access-Token") == "expired":
                return httpx.Response(403, request=request)
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "revision": 3},
            )

    def provider() -> str:
        nonlocal provider_calls
        provider_calls += 1
        return "renewed.payload.signature"

    client = object.__new__(RemoteBackgroundTaskController)
    client._client = FakeClient()
    client._lock = threading.RLock()
    client._access_token_provider = provider
    client._authentication_required = False
    client._authentication_error = ""
    client._revision = 0
    client._last_error = ""
    client.instance_id = "desktop-one"

    with pytest.raises(CoordinationAuthenticationRequired):
        client._request("GET", "/v1/snapshot")

    assert provider_calls == 0
    assert client.authentication_required is True
    rejected = client._rpc("set_emergency_stop_writes", True)
    assert rejected.accepted is False
    assert rejected.details["authentication_required"] is True
    assert provider_calls == 0

    restored = client.reauthenticate()

    assert restored.accepted is True
    assert restored.details["reauthenticated"] is True
    assert provider_calls == 1
    assert client.authentication_required is False
    assert client._client.headers["Cf-Access-Token"] == "renewed.payload.signature"


def test_logistics_browser_rejects_untrusted_prewarm_url() -> None:
    assert _safe_start_url(ALIBABA_SCM_HOME_URL) == ALIBABA_SCM_HOME_URL
    assert _safe_start_url(ALIBABA_QUOTE_URL) == ALIBABA_QUOTE_URL

    with pytest.raises(ValueError):
        _safe_start_url("https://example.com/")
    with pytest.raises(ValueError):
        _safe_start_url("https://i.alibaba.com/account/settings")


def test_local_alibaba_order_executor_uses_local_chrome_endpoint(
    tmp_path: Path,
) -> None:
    _controller, store, service = _service(tmp_path)
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    remote_endpoint = "http://127.0.0.1:24000"
    local_browser_port = int(server.server_address[1])
    client = RemoteBackgroundTaskController(
        f"http://127.0.0.1:{server.server_address[1]}",
        token=token,
        display_name="Alice",
        instance_id="one",
        browser_endpoint=remote_endpoint,
        browser_local_port=local_browser_port,
        browser_profile_dir=tmp_path / "browser-profile",
    )
    try:
        assert client.browser_endpoint == remote_endpoint
        assert (
            client.local_browser_endpoint
            == f"http://127.0.0.1:{local_browser_port}"
        )
        assert (
            client._local_action_executor.browser_endpoint
            == client.local_browser_endpoint
        )
        assert store.active_browser_endpoints() == {"one": remote_endpoint}
    finally:
        client.prepare_close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


def test_remote_clients_share_state_and_conflict_feedback(tmp_path: Path) -> None:
    _controller, _store, service = _service(tmp_path)
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    first = RemoteBackgroundTaskController(
        url,
        token=token,
        display_name="Alice",
        instance_id="one",
    )
    second = RemoteBackgroundTaskController(
        url,
        token=token,
        display_name="Bob",
        instance_id="two",
    )
    try:
        assert isinstance(first, BackgroundTaskController)
        result = first.update_capability_mode(
            Capability.DOWNLOAD_CUSTOM_ZIP,
            CapabilityMode.BROWSER,
        )
        assert result.accepted is True
        assert (
            second.snapshot().policy.configured_mode_for(
                Capability.DOWNLOAD_CUSTOM_ZIP
            )
            is CapabilityMode.BROWSER
        )

        settings = DesktopSettings(folder_root="D:\\shared")
        assert first.save_settings(settings).accepted is True
        assert second.snapshot().settings.folder_root == "D:\\shared"

        command = TaskCommand(
            "scan A",
            TaskArea.CUSTOMIZATION,
            Capability.LIST_ORDERS,
            order_no="A",
        )
        assert first.submit_task(command).accepted is True
        conflict = second.submit_task(command)
        assert conflict.accepted is False
        assert conflict.details["owner_display_name"] == "Alice"
    finally:
        first.prepare_close()
        second.prepare_close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


def test_emergency_stop_activation_bypasses_configuration_lease(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    controller.set_emergency_stop_writes(False)
    service.register("steven-pc", "Steven")
    service.register("worker-pc", "Worker")
    assert store.acquire(
        resources=("configuration:policy",),
        instance_id="worker-pc",
        request_id="busy-policy-update",
        operation="update_capability_mode",
        ttl_seconds=60,
    ) is None
    try:
        stopped = service.invoke(
            instance_id="steven-pc",
            request_id="priority-emergency-stop",
            method="set_emergency_stop_writes",
            raw_args=[True],
            raw_kwargs={},
        )

        assert stopped["result"]["accepted"] is True
        assert controller.snapshot().policy.emergency_stop_writes is True

        blocked_resume = service.invoke(
            instance_id="steven-pc",
            request_id="ordinary-resume",
            method="set_emergency_stop_writes",
            raw_args=[False],
            raw_kwargs={},
        )
        assert blocked_resume["result"]["accepted"] is False
        assert blocked_resume["result"]["details"]["conflict"] is True
    finally:
        service.close()


def test_emergency_stop_activation_bypasses_busy_rpc_lock(tmp_path: Path) -> None:
    controller, _store, service = _service(tmp_path)
    controller.set_emergency_stop_writes(False)
    service.register("steven-pc", "Steven")
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    invocation_finished = threading.Event()
    response: dict[str, object] = {}

    def hold_ordinary_rpc_lock() -> None:
        with service._call_lock:
            lock_acquired.set()
            assert release_lock.wait(2)

    def activate_stop() -> None:
        response.update(
            service.invoke(
                instance_id="steven-pc",
                request_id="priority-stop-while-busy",
                method="set_emergency_stop_writes",
                raw_args=[True],
                raw_kwargs={},
            )
        )
        invocation_finished.set()

    blocker = threading.Thread(target=hold_ordinary_rpc_lock)
    invocation = threading.Thread(target=activate_stop)
    blocker.start()
    assert lock_acquired.wait(1)
    invocation.start()
    try:
        assert invocation_finished.wait(0.5)
        assert response["result"]["accepted"] is True
        assert controller.snapshot().policy.emergency_stop_writes is True
    finally:
        release_lock.set()
        blocker.join(timeout=2)
        invocation.join(timeout=2)
        service.close()


def test_remote_configuration_export_and_import_transfer_local_files(
    tmp_path: Path,
) -> None:
    package = b"encrypted-portable-configuration"
    controller = _PortableConfigurationController(package)
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(controller, store)
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client = RemoteBackgroundTaskController(
        f"http://127.0.0.1:{server.server_address[1]}",
        token=token,
        display_name="Alice",
        instance_id="one",
    )
    destination = tmp_path / "downloaded.erp-migrate"
    source = tmp_path / "uploaded.erp-migrate"
    source.write_bytes(package + b"-updated")
    try:
        rejected = client.export_portable_migration(
            str(destination),
            "portable configuration password",
            include_state=True,
        )
        assert rejected.accepted is False
        assert not destination.exists()

        exported = client.export_portable_migration(
            str(destination),
            "portable configuration password",
            include_state=False,
        )
        assert exported.accepted is True
        assert destination.read_bytes() == package

        imported = client.import_portable_migration(
            str(source),
            "portable configuration password",
            overwrite=True,
            configuration_only=True,
        )
        assert imported.accepted is True
        assert controller.imported_package == package + b"-updated"
        assert controller.configuration_only is True
    finally:
        client.prepare_close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


def test_portable_configuration_result_names_verified_target_account(
    tmp_path: Path,
) -> None:
    controller = _PortableConfigurationController(b"encrypted-package")
    service = CoordinatedControllerService(
        controller,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
    )
    identity = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    service.register("alice-pc", "Alice PC", identity=identity)
    try:
        exported = service.export_portable_configuration(
            instance_id="alice-pc",
            request_id="export-settings-for-alice",
            passphrase="portable configuration password",
            identity=identity,
        )
        assert exported["result"]["details"]["target_operator_email"] == (
            identity.email
        )

        imported = service.import_portable_configuration(
            instance_id="alice-pc",
            request_id="import-settings-for-alice",
            passphrase="portable configuration password",
            package_base64=base64.b64encode(b"encrypted-package").decode(
                "ascii"
            ),
            identity=identity,
        )
        assert imported["result"]["details"]["target_operator_email"] == (
            identity.email
        )
    finally:
        service.close()


def test_portable_configuration_import_is_isolated_by_verified_email(
    tmp_path: Path,
) -> None:
    controllers: dict[str, _PortableConfigurationController] = {}

    def factory(identity: OperatorIdentity) -> _PortableConfigurationController:
        controller = _PortableConfigurationController(b"encrypted-package")
        controllers[identity.email] = controller
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
    )
    alice = OperatorIdentity(
        "alice@billyprint.com",
        "Alice",
        "alice-subject",
    )
    bob = OperatorIdentity(
        "bob@billyprint.com",
        "Bob",
        "bob-subject",
    )
    service.register("alice-pc", "Alice PC", identity=alice)
    service.register("bob-pc", "Bob PC", identity=bob)
    service.snapshot_payload("alice-pc", identity=alice)
    service.snapshot_payload("bob-pc", identity=bob)
    try:
        response = service.import_portable_configuration(
            instance_id="alice-pc",
            request_id="alice-import-only",
            passphrase="portable configuration password",
            package_base64=base64.b64encode(b"alice-settings").decode("ascii"),
            identity=alice,
        )

        assert response["result"]["accepted"] is True
        assert response["result"]["details"]["target_operator_email"] == (
            alice.email
        )
        assert controllers[alice.email].imported_package == b"alice-settings"
        assert controllers[bob.email].imported_package == b""
    finally:
        service.close()


def test_remote_client_reuses_cached_snapshot_for_unchanged_revision(
    tmp_path: Path,
) -> None:
    _controller, _store, service = _service(tmp_path)
    token = "t" * 48
    server = create_http_server(("127.0.0.1", 0), service, api_token=token)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client = RemoteBackgroundTaskController(
        f"http://127.0.0.1:{server.server_address[1]}",
        token=token,
        display_name="Alice",
        instance_id="one",
    )
    try:
        first = client.snapshot()
        second = client.snapshot()

        assert second is first
        assert client.pending_interactions() == ()
    finally:
        client.prepare_close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


def test_remote_snapshot_redacts_secrets_and_blank_save_preserves_them(
    tmp_path: Path,
) -> None:
    controller, _store, service = _service(tmp_path)
    original = DesktopSettings(
        lingxing_app_secret="server-only-secret",
        amazon_refresh_token="server-only-refresh",
    )
    assert controller.save_settings(original).accepted is True
    service.register("one", "Alice")
    try:
        payload = service.snapshot_payload("one")
        assert (
            payload["snapshot"]["settings"]["lingxing_app_secret"]
            == SERVER_CONFIGURED_SECRET
        )
        assert (
            payload["snapshot"]["settings"]["amazon_refresh_token"]
            == SERVER_CONFIGURED_SECRET
        )
        assert payload["snapshot"]["configured_secret_lengths"] == {
            "amazon_refresh_token": len("server-only-refresh"),
            "lingxing_app_secret": len("server-only-secret"),
        }
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "server-only-secret" not in serialized
        assert "server-only-refresh" not in serialized

        edited = DesktopSettings(folder_root="D:\\edited")
        result = service.invoke(
            instance_id="one",
            request_id="save-settings",
            method="save_settings",
            raw_args=[to_jsonable(edited)],
            raw_kwargs={},
        )
        assert result["result"]["accepted"] is True
        saved = controller.snapshot().settings
        assert saved.folder_root == "D:\\edited"
        assert saved.lingxing_app_secret == "server-only-secret"
        assert saved.amazon_refresh_token == "server-only-refresh"
    finally:
        service.close()


def test_real_coordinator_runs_persistent_notification_followup_after_source_terminal(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    source = TaskCommand(
        "shipment scan",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={
            "trigger": "manual_button",
            "local_visible_logistics_followup": True,
        },
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="persistent-source",
            method="submit_task",
            raw_args=[to_jsonable(source)],
            raw_kwargs={},
        )
        assert response["result"]["accepted"] is True
        source_task_id = str(response["result"]["task_id"])
        followup = store.list_task_followups()[0]
        assert followup["source_task_id"] == source_task_id
        assert followup["state"] == "WAITING_SOURCE"

        controller.set_task_status(
            source_task_id,
            TaskStatus.SUCCEEDED,
            message="source complete",
            progress_percent=100,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            followup = store.list_task_followups()[0]
            if followup["state"] == "SUBMITTED":
                break
            time.sleep(0.01)

        assert followup["state"] == "SUBMITTED", followup
        submitted_task_id = str(followup["submitted_task_id"])
        submitted = next(
            task
            for task in controller.snapshot().tasks
            if task.task_id == submitted_task_id
        )
        assert submitted.payload["trigger"] == (
            SHIPMENT_NOTIFICATION_COMPENSATION_TRIGGER
        )
        assert submitted.payload["source_scan_task_id"] == source_task_id
        assert submitted.payload["persistent_server_followup"] is True

        interaction = DesktopInteractionRequest(
            request_id="persistent-recipient-choice",
            task_id=submitted_task_id,
            stage="notification:recipient_name_select",
            title="选择客户通知收件人姓名",
            message="同一订单存在两个收件人姓名，请选择。",
        )
        controller.pending_interactions = lambda: (interaction,)
        controller.respond_interaction = lambda response: ControlResult(
            bool(response.accepted),
            "已提交姓名选择。",
            submitted_task_id,
        )
        visible = service.snapshot_payload("one")["interactions"]
        assert [item["request_id"] for item in visible] == [
            "persistent-recipient-choice"
        ]
        response = service.invoke(
            instance_id="one",
            request_id="respond-persistent-recipient-choice",
            method="respond_interaction",
            raw_args=[
                to_jsonable(
                    DesktopInteractionResponse(
                        request_id="persistent-recipient-choice",
                        accepted=True,
                    )
                )
            ],
            raw_kwargs={},
        )
        assert response["result"]["accepted"] is True

        controller.set_task_status(
            submitted_task_id,
            TaskStatus.SUCCEEDED,
            message="compensation complete",
            progress_percent=100,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            followup = store.list_task_followups()[0]
            if followup["state"] == "COMPLETED":
                break
            time.sleep(0.01)
        assert followup["state"] == "COMPLETED", followup
        outcomes = [
            item["outcome"]
            for item in store.list_task_followup_attempts(
                str(followup["followup_id"])
            )
        ]
        assert outcomes == ["SUBMITTED", "COMPLETED"]
    finally:
        service.close()


def test_source_scan_is_not_accepted_before_followup_intent_is_durable(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")

    class _IntentCheckingController(InMemoryBackgroundTaskController):
        def submit_task(self, command: TaskCommand) -> ControlResult:
            if command.payload.get("local_visible_logistics_followup"):
                rows = store.list_task_followups()
                assert len(rows) == 1
                assert rows[0]["state"] == "REGISTERING"
                assert rows[0]["source_task_id"] == ""
            return super().submit_task(command)

    controller = _IntentCheckingController()
    service = CoordinatedControllerService(controller, store)
    service.register("one", "Alice")
    try:
        response = service.invoke(
            instance_id="one",
            request_id="intent-before-source",
            method="submit_task",
            raw_args=[
                to_jsonable(
                    TaskCommand(
                        "shipment scan",
                        TaskArea.SHIPMENT,
                        Capability.LIST_ORDERS,
                        payload={
                            "trigger": "manual_button",
                            "local_visible_logistics_followup": True,
                        },
                    )
                )
            ],
            raw_kwargs={},
        )

        assert response["result"]["accepted"] is True
        followup = store.list_task_followups()[0]
        assert followup["state"] == "WAITING_SOURCE"
        assert followup["source_task_id"] == response["result"]["task_id"]
    finally:
        service.close()


def test_real_coordinator_persists_lock_conflict_and_retries_with_backoff(
    tmp_path: Path,
) -> None:
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        controller,
        store,
        settings=CoordinationSettings(
            monitor_interval_seconds=60,
            followup_retry_initial_seconds=0.02,
            followup_retry_max_seconds=0.1,
        ),
    )
    service.register("one", "Alice")
    service.register("busy", "Busy Worker")
    source = TaskCommand(
        "shipment scan",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={
            "trigger": "manual_button",
            "local_visible_logistics_followup": True,
        },
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="retry-source",
            method="submit_task",
            raw_args=[to_jsonable(source)],
            raw_kwargs={},
        )
        source_task_id = str(response["result"]["task_id"])
        controller.set_task_status(
            source_task_id,
            TaskStatus.SUCCEEDED,
            message="source complete",
            progress_percent=100,
        )
        store.release_task(source_task_id)
        assert store.activate_task_followup(
            source_task_id,
            source_status=TaskStatus.SUCCEEDED.value,
        ) == 1
        assert store.acquire(
            resources=("scan:notification",),
            instance_id="busy",
            request_id="busy-list-orders",
            operation="submit_task",
            ttl_seconds=30,
        ) is None

        service._process_persistent_task_followups()

        followup = store.list_task_followups()[0]
        assert followup["state"] == "PENDING"
        assert followup["attempt_count"] == 1
        assert "scan:notification" in followup["last_error"]
        attempts = store.list_task_followup_attempts(
            str(followup["followup_id"])
        )
        assert attempts[-1]["outcome"] == "LEASE_CONFLICT"
        assert attempts[-1]["retry_at"] > attempts[-1]["attempted_at"]

        first_delay = attempts[-1]["retry_at"] - attempts[-1]["attempted_at"]
        time.sleep(0.03)
        service._process_persistent_task_followups()
        followup = store.list_task_followups()[0]
        assert followup["attempt_count"] == 2
        attempts = store.list_task_followup_attempts(
            str(followup["followup_id"])
        )
        second_delay = attempts[-1]["retry_at"] - attempts[-1]["attempted_at"]
        assert second_delay >= first_delay * 1.8

        store.release_request("busy-list-orders")
        time.sleep(0.05)
        service._process_persistent_task_followups()

        followup = store.list_task_followups()[0]
        assert followup["state"] == "SUBMITTED", followup
        assert followup["submitted_task_id"]
        attempts = store.list_task_followup_attempts(
            str(followup["followup_id"])
        )
        assert [item["outcome"] for item in attempts] == [
            "LEASE_CONFLICT",
            "LEASE_CONFLICT",
            "SUBMITTED",
        ]
    finally:
        service.close()


def test_real_coordinator_persists_terminal_followup_failure(
    tmp_path: Path,
) -> None:
    controller, store, service = _service(tmp_path)
    service.register("one", "Alice")
    source = TaskCommand(
        "shipment scan",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={
            "trigger": "manual_button",
            "local_visible_logistics_followup": True,
        },
    )
    try:
        response = service.invoke(
            instance_id="one",
            request_id="failed-followup-source",
            method="submit_task",
            raw_args=[to_jsonable(source)],
            raw_kwargs={},
        )
        source_task_id = str(response["result"]["task_id"])
        controller.set_task_status(
            source_task_id,
            TaskStatus.SUCCEEDED,
            message="source complete",
            progress_percent=100,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            followup = store.list_task_followups()[0]
            if followup["state"] == "SUBMITTED":
                break
            time.sleep(0.01)
        assert followup["state"] == "SUBMITTED", followup

        controller.set_task_status(
            str(followup["submitted_task_id"]),
            TaskStatus.FAILED,
            message="discovery failed safely",
            progress_percent=100,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            followup = store.list_task_followups()[0]
            if followup["state"] == "FAILED":
                break
            time.sleep(0.01)
        assert followup["state"] == "FAILED", followup
        assert followup["last_error"] == "discovery failed safely"
        attempts = store.list_task_followup_attempts(
            str(followup["followup_id"])
        )
        assert attempts[-1]["outcome"] == "TASK_FAILED"
        assert attempts[-1]["error"] == "discovery failed safely"
    finally:
        service.close()


def test_real_coordinator_recovers_unfinished_followup_after_restart(
    tmp_path: Path,
) -> None:
    controller = InMemoryBackgroundTaskController()
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    settings = CoordinationSettings(monitor_interval_seconds=60)
    first = CoordinatedControllerService(controller, store, settings=settings)
    first.register("one", "Alice")
    source = TaskCommand(
        "shipment scan",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={
            "trigger": "manual_button",
            "local_visible_logistics_followup": True,
        },
    )
    response = first.invoke(
        instance_id="one",
        request_id="restart-source",
        method="submit_task",
        raw_args=[to_jsonable(source)],
        raw_kwargs={},
    )
    source_task_id = str(response["result"]["task_id"])
    controller.set_task_status(
        source_task_id,
        TaskStatus.SUCCEEDED,
        message="source complete before restart",
        progress_percent=100,
    )
    assert store.list_task_followups()[0]["state"] == "WAITING_SOURCE"
    first.close()

    second = CoordinatedControllerService(controller, store, settings=settings)
    try:
        recovered = store.list_task_followups()[0]
        assert recovered["state"] == "PENDING"
        assert "协调服务重启" in recovered["last_error"]

        second._process_persistent_task_followups()

        submitted = store.list_task_followups()[0]
        assert submitted["state"] == "SUBMITTED", submitted
        outcomes = [
            item["outcome"]
            for item in store.list_task_followup_attempts(
                str(submitted["followup_id"])
            )
        ]
        assert outcomes == ["RECOVERED", "SUBMITTED"]
    finally:
        second.close()
