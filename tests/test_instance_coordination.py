from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest

from erp_automation.configuration import HostKeyAesGcmBackend
from erp_automation.coordination.codec import decode_snapshot, to_jsonable
from erp_automation.coordination.http_server import create_http_server
from erp_automation.coordination.local_browser import (
    ALIBABA_SCM_HOME_URL,
    _safe_start_url,
)
from erp_automation.coordination.remote_controller import (
    RemoteBackgroundTaskController,
)
from erp_automation.coordination.service import (
    ClientUpdateRequiredError,
    CoordinatedControllerService,
    CoordinationSettings,
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
    ShipmentRow,
    TaskArea,
    TaskCommand,
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

    decoded = decode_snapshot(to_jsonable(snapshot))

    assert (
        decoded.policy.configured_mode_for(Capability.DOWNLOAD_CUSTOM_ZIP)
        is CapabilityMode.BROWSER
    )
    assert decoded.settings == snapshot.settings
    assert decoded.logs[0].message == snapshot.logs[0].message


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
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        service.close()


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
            ),
        )

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


def test_shipment_scan_prewarms_first_due_alibaba_logistics_page() -> None:
    client = object.__new__(RemoteBackgroundTaskController)
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

    command = TaskCommand(
        "扫描候选并查询物流",
        TaskArea.SHIPMENT,
        Capability.LIST_ORDERS,
        payload={"local_visible_logistics_followup": True},
    )

    assert client._prewarms_local_logistics(command) is True
    assert (
        client._logistics_prewarm_url()
        == "https://scm.alibaba.com/luyou/express/detail.htm?id=1829169726"
    )


def test_remote_scan_submission_opens_prewarm_page_before_rpc() -> None:
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
    assert host.opened == [
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1829169726"
    ]


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
    assert client._rpc_request_timeout("list_shipment_notifications", ()) is None


def test_logistics_browser_rejects_untrusted_prewarm_url() -> None:
    assert _safe_start_url(ALIBABA_SCM_HOME_URL) == ALIBABA_SCM_HOME_URL

    with pytest.raises(ValueError):
        _safe_start_url("https://example.com/")


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
