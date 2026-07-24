from __future__ import annotations

import threading
import time
from pathlib import Path

from erp_automation.configuration import HostKeyAesGcmBackend
from erp_automation.coordination.codec import decode_snapshot, to_jsonable
from erp_automation.coordination.http_server import create_http_server
from erp_automation.coordination.remote_controller import (
    RemoteBackgroundTaskController,
)
from erp_automation.coordination.service import (
    CoordinatedControllerService,
    CoordinationSettings,
    RPC_METHODS,
)
from erp_automation.coordination.store import CoordinationStore
from erp_automation.ui.controller import (
    BackgroundTaskController,
    InMemoryBackgroundTaskController,
)
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    DesktopSettings,
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
        assert payload["snapshot"]["settings"]["lingxing_app_secret"] == ""
        assert payload["snapshot"]["settings"]["amazon_refresh_token"] == ""

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
