from __future__ import annotations

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
from erp_automation.configuration import HostKeyAesGcmBackend
from erp_automation.coordination.codec import (
    MAX_CONFIGURED_SECRET_LENGTH,
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


def test_every_controller_operation_is_explicitly_classified_for_remote_audit() -> None:
    public_operations = {
        name
        for name, value in BackgroundTaskController.__dict__.items()
        if callable(value) and not name.startswith("_")
    }

    assert READ_METHODS.isdisjoint(MUTATION_METHODS)
    assert RPC_METHODS == READ_METHODS | MUTATION_METHODS
    assert public_operations == RPC_METHODS | {"snapshot", "prepare_close"}


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

        other = new_service.allocate_browser_endpoint(
            "another-client",
            "Bob",
            "2026.07.31.2",
        )
        assert other["browser_endpoint"] != expected_endpoint
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
        now[0] = 2_000.0
        deadline = time.monotonic() + 2
        while (
            not any(
                lease["task_id"] == task_id
                and lease["expires_at"] >= 2_030.0
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
        assert all(lease["expires_at"] >= 2_030.0 for lease in matching)
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
