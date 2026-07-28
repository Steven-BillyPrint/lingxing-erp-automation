from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from erp_automation.coordination.access import (
    CloudflareAccessError,
    CloudflareAccessVerifier,
    OperatorIdentity,
)
from erp_automation.coordination.codec import decode_snapshot, to_jsonable
from erp_automation.coordination.http_server import create_http_server
from erp_automation.coordination.service import CoordinatedControllerService
from erp_automation.coordination.server_main import _bootstrap_legacy_operator_config
from erp_automation.coordination.store import CoordinationStore
from erp_automation.configuration import (
    EncryptedConfigurationStore,
    HostKeyAesGcmBackend,
)
from erp_automation.persistence import CustomWorkflowStore, WorkflowStageState
from erp_automation.ui.controller import InMemoryBackgroundTaskController
from erp_automation.ui.models import (
    Capability,
    CapabilityMode,
    DesktopSettings,
    TaskArea,
    TaskCommand,
)
from erp_automation.ui.persistent_controller import (
    PersistentBackgroundTaskController,
)


TEAM_DOMAIN = "morning-leaf-e9e2.cloudflareaccess.com"
ISSUER = f"https://{TEAM_DOMAIN}"
AUDIENCE = "erp-test-audience"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _key_material() -> tuple[rsa.RSAPrivateKey, dict[str, str]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    return private_key, {
        "kty": "RSA",
        "kid": "test-key",
        "alg": "RS256",
        "use": "sig",
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    now: float,
    email: str = "alice@billyprint.com",
    name: str | None = None,
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: float = 3600,
) -> str:
    header = _b64(
        json.dumps(
            {"alg": "RS256", "kid": "test-key", "typ": "JWT"},
            separators=(",", ":"),
        ).encode()
    )
    payload = _b64(
        json.dumps(
            {
                "iss": issuer,
                "aud": [audience],
                "email": email,
                "name": name,
                "sub": f"subject:{email}",
                "iat": now,
                "nbf": now - 1,
                "exp": now + expires_in,
            },
            separators=(",", ":"),
        ).encode()
    )
    signed = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _verifier(
    *,
    now: float,
) -> tuple[CloudflareAccessVerifier, rsa.RSAPrivateKey]:
    private_key, jwk = _key_material()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{ISSUER}/cdn-cgi/access/certs"
        return httpx.Response(200, json={"keys": [jwk]})

    verifier = CloudflareAccessVerifier(
        team_domain=TEAM_DOMAIN,
        audience=AUDIENCE,
        allowed_email_domain="billyprint.com",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now,
    )
    return verifier, private_key


def test_cloudflare_access_verifies_signature_audience_and_company_email() -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)

    identity = verifier.verify(_token(private_key, now=now))

    assert identity == OperatorIdentity(
        email="alice@billyprint.com",
        name="alice",
        subject="subject:alice@billyprint.com",
    )


def test_cloudflare_access_sanitizes_operator_display_name() -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)

    identity = verifier.verify(
        _token(
            private_key,
            now=now,
            name="\u202e  Alice\r\nAdmin\x00 ",
        )
    )

    assert identity.name == "Alice Admin"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"email": "alice@example.com"}, "Only @billyprint.com"),
        ({"audience": "wrong-audience"}, "audience"),
        ({"issuer": "https://wrong.cloudflareaccess.com"}, "issuer"),
        ({"expires_in": -120}, "expired"),
    ],
)
def test_cloudflare_access_rejects_wrong_identity_claims(
    overrides: dict[str, object],
    message: str,
) -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)

    with pytest.raises(CloudflareAccessError, match=message):
        verifier.verify(_token(private_key, now=now, **overrides))


def test_cloudflare_access_rejects_tampered_signature() -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)
    token = _token(private_key, now=now)
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    tampered = f"{header}.{payload}.{replacement}{signature[1:]}"

    with pytest.raises(CloudflareAccessError, match="signature"):
        verifier.verify(tampered)


@pytest.mark.parametrize("expires_in", [float("nan"), float("inf")])
def test_cloudflare_access_rejects_non_finite_timestamps(
    expires_in: float,
) -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)

    with pytest.raises(CloudflareAccessError, match="timestamps"):
        verifier.verify(
            _token(private_key, now=now, expires_in=expires_in)
        )


@pytest.mark.parametrize("invalid_prefix", ["\u00e9", "+", "/"])
def test_cloudflare_access_rejects_non_base64url_jwt_segments_cleanly(
    invalid_prefix: str,
) -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)
    token = _token(private_key, now=now)

    with pytest.raises(CloudflareAccessError, match="base64url"):
        verifier.verify(invalid_prefix + token)


def test_http_origin_requires_cloudflare_identity_and_binds_operator(
    tmp_path: Path,
) -> None:
    now = 1_800_000_000.0
    verifier, private_key = _verifier(now=now)
    controller = InMemoryBackgroundTaskController()
    service = CoordinatedControllerService(
        controller,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
    )
    api_token = "shared-api-token-with-at-least-32-characters"
    server = create_http_server(
        ("127.0.0.1", 0),
        service,
        api_token=api_token,
        access_verifier=verifier,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(base_url=base_url) as client:
            missing = client.post(
                "/v1/instances/register",
                headers={"Authorization": f"Bearer {api_token}"},
                json={
                    "instance_id": "desktop-a",
                    "display_name": "untrusted-name",
                },
            )
            assert missing.status_code == 401

            registered = client.post(
                "/v1/instances/register",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Cf-Access-Token": _token(private_key, now=now),
                },
                json={
                    "instance_id": "desktop-a",
                    "display_name": "workstation-a",
                },
            )
            assert registered.status_code == 200
            assert registered.json()["operator"] == {
                "name": "alice",
                "email": "alice@billyprint.com",
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        service.close()


def test_operator_controllers_isolate_settings_but_share_revision(
    tmp_path: Path,
) -> None:
    controllers: dict[str, InMemoryBackgroundTaskController] = {}

    def factory(identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = InMemoryBackgroundTaskController()
        controllers[identity.email] = controller
        return controller

    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    service = CoordinatedControllerService(
        None,
        store,
        controller_factory=factory,
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    try:
        service.register("alice-pc", "PC-A", identity=alice)
        service.register("bob-pc", "PC-B", identity=bob)
        service.invoke(
            instance_id="alice-pc",
            request_id="alice-save",
            method="save_settings",
            raw_args=[
                to_jsonable(
                    DesktopSettings(
                        lingxing_account="alice-erp",
                        lingxing_app_secret="alice-secret",
                    )
                )
            ],
            raw_kwargs={},
            identity=alice,
        )
        service.invoke(
            instance_id="bob-pc",
            request_id="bob-save",
            method="save_settings",
            raw_args=[
                to_jsonable(
                    DesktopSettings(
                        lingxing_account="bob-erp",
                        lingxing_app_secret="bob-secret",
                    )
                )
            ],
            raw_kwargs={},
            identity=bob,
        )

        alice_snapshot = decode_snapshot(
            service.snapshot_payload("alice-pc", identity=alice)["snapshot"]
        )
        bob_snapshot = decode_snapshot(
            service.snapshot_payload("bob-pc", identity=bob)["snapshot"]
        )

        assert alice_snapshot.settings.lingxing_account == "alice-erp"
        assert bob_snapshot.settings.lingxing_account == "bob-erp"
        assert alice_snapshot.configured_secret_lengths["lingxing_app_secret"] == 12
        assert bob_snapshot.configured_secret_lengths["lingxing_app_secret"] == 10
        assert alice_snapshot.operator_email == alice.email
        assert bob_snapshot.operator_email == bob.email
        assert store.current_revision() >= 3
    finally:
        service.close()


def test_legacy_configuration_is_assigned_to_only_one_operator(
    tmp_path: Path,
) -> None:
    operator_root = tmp_path / "operator-config"
    operator_root.mkdir()
    legacy = tmp_path / "config.enc"
    legacy.write_bytes(b"encrypted-legacy-config")

    alice_path = _bootstrap_legacy_operator_config(
        operator_config_root=operator_root,
        legacy_config_path=legacy,
        operator_email="alice@billyprint.com",
    )
    marker = operator_root / ".legacy-config-owner.sha256"

    assert alice_path.read_bytes() == legacy.read_bytes()
    assert len(marker.read_text(encoding="ascii").strip()) == 64
    assert "@" not in marker.read_text(encoding="ascii")

    alice_path.unlink()
    recovered = _bootstrap_legacy_operator_config(
        operator_config_root=operator_root,
        legacy_config_path=legacy,
        operator_email="alice@billyprint.com",
    )
    assert recovered.read_bytes() == legacy.read_bytes()

    with pytest.raises(RuntimeError, match="already assigned"):
        _bootstrap_legacy_operator_config(
            operator_config_root=operator_root,
            legacy_config_path=legacy,
            operator_email="bob@billyprint.com",
        )
    assert list(operator_root.glob("*.enc")) == [alice_path]


@pytest.mark.parametrize(
    "email",
    ["@billyprint.com", "alice @billyprint.com", "alice@@billyprint.com"],
)
def test_legacy_configuration_rejects_malformed_bootstrap_email(
    tmp_path: Path,
    email: str,
) -> None:
    operator_root = tmp_path / "operator-config"
    operator_root.mkdir()

    with pytest.raises(ValueError, match="@billyprint.com"):
        _bootstrap_legacy_operator_config(
            operator_config_root=operator_root,
            legacy_config_path=tmp_path / "config.enc",
            operator_email=email,
        )


def test_cross_user_order_lock_operator_log_and_realtime_task_refresh(
    tmp_path: Path,
) -> None:
    controllers: dict[str, InMemoryBackgroundTaskController] = {}

    def factory(identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = InMemoryBackgroundTaskController()
        controllers[identity.email] = controller
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    alice_command = TaskCommand(
        "读取订单",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="ORDER-1001",
    )
    bob_command = TaskCommand(
        "更新物流",
        TaskArea.SHIPMENT,
        Capability.UPDATE_TRACKING,
        order_no="ORDER-1001",
    )
    try:
        service.register("alice-pc", "PC-A", identity=alice)
        service.register("bob-pc", "PC-B", identity=bob)
        bob_initial = service.snapshot_payload("bob-pc", identity=bob)

        accepted = service.invoke(
            instance_id="alice-pc",
            request_id="alice-order-request",
            method="submit_task",
            raw_args=[to_jsonable(alice_command)],
            raw_kwargs={},
            identity=alice,
        )
        conflict = service.invoke(
            instance_id="bob-pc",
            request_id="bob-order-request",
            method="submit_task",
            raw_args=[to_jsonable(bob_command)],
            raw_kwargs={},
            identity=bob,
        )
        bob_after = service.snapshot_payload(
            "bob-pc",
            known_revision=int(bob_initial["revision"]),
            identity=bob,
        )
        bob_snapshot = decode_snapshot(bob_after["snapshot"])

        assert accepted["result"]["accepted"] is True
        assert conflict["result"]["accepted"] is False
        assert conflict["result"]["details"]["conflict"] is True
        assert conflict["result"]["details"]["owner_email"] == alice.email
        assert bob_after["unchanged"] is False
        assert any(task.order_no == "ORDER-1001" for task in bob_snapshot.tasks)
        assert any(
            entry.operator_email == alice.email
            and "submit_task" in entry.message
            for entry in bob_snapshot.logs
        )
        assert any(
            entry.operator_email == bob.email
            and "submit_task" in entry.message
            for entry in bob_snapshot.logs
        )
    finally:
        service.close()


def test_different_orders_with_same_capability_do_not_conflict(
    tmp_path: Path,
) -> None:
    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=lambda _identity: InMemoryBackgroundTaskController(),
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    first = TaskCommand(
        "读取订单 A",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="ORDER-1001",
    )
    second = TaskCommand(
        "读取订单 B",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        order_no="ORDER-1002",
    )
    try:
        service.register("alice-pc", "PC-A", identity=alice)
        service.register("bob-pc", "PC-B", identity=bob)

        first_result = service.invoke(
            instance_id="alice-pc",
            request_id="alice-order-1001",
            method="submit_task",
            raw_args=[to_jsonable(first)],
            raw_kwargs={},
            identity=alice,
        )
        second_result = service.invoke(
            instance_id="bob-pc",
            request_id="bob-order-1002",
            method="submit_task",
            raw_args=[to_jsonable(second)],
            raw_kwargs={},
            identity=bob,
        )

        assert first_result["result"]["accepted"] is True
        assert second_result["result"]["accepted"] is True
        active_resources = {
            lease["resource"] for lease in service.store.active_leases()
        }
        assert "order:order-1001" in active_resources
        assert "order:order-1002" in active_resources
    finally:
        service.close()


def test_persistent_cross_user_order_state_refreshes_from_shared_sqlite(
    tmp_path: Path,
) -> None:
    order_no = "111-2222222-3333333"
    workflow_store = CustomWorkflowStore(tmp_path / "data/automation.sqlite3")
    workflow_store.mutate_legacy_record(
        order_no,
        lambda _current: {
            "platform_order_no": order_no,
            "system_order_no": "103700000000000001",
            "product_type": "tablecloths",
            "contact_writeback_complete": False,
            "folder_complete": False,
        },
        event_type="test_initialized",
        actor="test",
    )
    controllers: dict[str, PersistentBackgroundTaskController] = {}

    def factory(identity: OperatorIdentity) -> PersistentBackgroundTaskController:
        key = (identity.email.encode("utf-8") + b"\0" * 32)[:32]
        controller = PersistentBackgroundTaskController(
            tmp_path,
            config_store=EncryptedConfigurationStore(
                tmp_path / "data/operator-config" / f"{identity.name}.enc",
                backend=HostKeyAesGcmBackend(key),
            ),
        )
        controllers[identity.email] = controller
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    try:
        service.register("alice-pc", "PC-A", identity=alice)
        service.register("bob-pc", "PC-B", identity=bob)
        bob_initial_payload = service.snapshot_payload("bob-pc", identity=bob)
        bob_initial = decode_snapshot(bob_initial_payload["snapshot"])
        initial_row = next(
            row for row in bob_initial.custom_orders if row.platform_order_no == order_no
        )

        changed = service.invoke(
            instance_id="alice-pc",
            request_id="alice-stage-change",
            method="set_custom_stage_state",
            raw_args=[order_no, "contact", str(WorkflowStageState.BLOCKED)],
            raw_kwargs={"reason": "verified cross-user refresh"},
            identity=alice,
        )
        bob_after_payload = service.snapshot_payload(
            "bob-pc",
            known_revision=int(bob_initial_payload["revision"]),
            identity=bob,
        )
        bob_after = decode_snapshot(bob_after_payload["snapshot"])
        refreshed_row = next(
            row for row in bob_after.custom_orders if row.platform_order_no == order_no
        )

        assert changed["result"]["accepted"] is True
        assert bob_after_payload["unchanged"] is False
        assert refreshed_row == next(
            row
            for row in controllers[alice.email].snapshot().custom_orders
            if row.platform_order_no == order_no
        )
        assert refreshed_row.workflow_stage == "blocked"
        assert refreshed_row.workflow_stage != initial_row.workflow_stage
    finally:
        service.close()


def test_scheduler_leader_lease_fails_over_between_online_clients(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    store.register_instance("alice-pc", "PC-A", ttl_seconds=60, identity=alice)
    store.register_instance("bob-pc", "PC-B", ttl_seconds=60, identity=bob)

    first = store.elect_scheduler("alice-pc", ttl_seconds=5)
    second = store.elect_scheduler("bob-pc", ttl_seconds=5)

    assert first["is_leader"] is True
    assert second["is_leader"] is False
    assert second["owner_instance_id"] == "alice-pc"

    now[0] += 6
    takeover = store.elect_scheduler("bob-pc", ttl_seconds=5)

    assert takeover["changed"] is True
    assert takeover["is_leader"] is True
    assert takeover["owner_instance_id"] == "bob-pc"

    assert store.deregister("bob-pc") is True
    immediate = store.elect_scheduler("alice-pc", ttl_seconds=5)
    assert immediate["is_leader"] is True


def test_scheduler_job_cadence_survives_leader_failover(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    store.register_instance("alice-pc", "PC-A", ttl_seconds=10_000, identity=alice)
    store.register_instance("bob-pc", "PC-B", ttl_seconds=10_000, identity=bob)
    store.elect_scheduler("alice-pc", ttl_seconds=60)

    due_times = store.scheduled_job_due_times({"five_minute_timer": 300})
    assert due_times == {"five_minute_timer": 1300.0}

    follower = store.claim_scheduled_job(
        job_key="five_minute_timer",
        interval_seconds=300,
        instance_id="bob-pc",
        request_id="bob-early",
    )
    assert follower["claimed"] is False
    assert follower["reason"] == "not_scheduler_leader"

    now[0] = 1301.0
    store.elect_scheduler("alice-pc", ttl_seconds=60)
    first_run = store.claim_scheduled_job(
        job_key="five_minute_timer",
        interval_seconds=300,
        instance_id="alice-pc",
        request_id="alice-run",
    )
    duplicate = store.claim_scheduled_job(
        job_key="five_minute_timer",
        interval_seconds=300,
        instance_id="alice-pc",
        request_id="alice-duplicate",
    )
    assert first_run["claimed"] is True
    assert first_run["next_due_at"] == 1601.0
    assert duplicate["claimed"] is False
    assert duplicate["reason"] == "not_due"

    assert store.deregister("alice-pc") is True
    store.elect_scheduler("bob-pc", ttl_seconds=60)
    before_due = store.claim_scheduled_job(
        job_key="five_minute_timer",
        interval_seconds=300,
        instance_id="bob-pc",
        request_id="bob-before-due",
    )
    assert before_due["claimed"] is False
    assert before_due["next_due_at"] == 1601.0

    now[0] = 1602.0
    store.elect_scheduler("bob-pc", ttl_seconds=60)
    takeover = store.claim_scheduled_job(
        job_key="five_minute_timer",
        interval_seconds=300,
        instance_id="bob-pc",
        request_id="bob-run",
    )
    assert takeover["claimed"] is True
    assert takeover["next_due_at"] == 1902.0


def test_same_instance_cannot_replace_an_active_request_lease(
    tmp_path: Path,
) -> None:
    store = CoordinationStore(tmp_path / "coordination.sqlite3")
    store.register_instance("desktop-a", "PC-A", ttl_seconds=60)

    assert store.acquire(
        resources=("order:ORDER-1001",),
        instance_id="desktop-a",
        request_id="request-one",
        operation="submit_task",
        ttl_seconds=60,
    ) is None
    replay_conflict = store.acquire(
        resources=("order:ORDER-1001",),
        instance_id="desktop-a",
        request_id="request-one",
        operation="submit_task",
        ttl_seconds=60,
    )
    conflict = store.acquire(
        resources=("order:ORDER-1001",),
        instance_id="desktop-a",
        request_id="request-two",
        operation="submit_task",
        ttl_seconds=60,
    )

    assert replay_conflict is not None
    assert replay_conflict.owner_instance_id == "desktop-a"
    assert conflict is not None
    assert conflict.owner_instance_id == "desktop-a"
    assert conflict.resource == "order:order-1001"


def test_service_rejects_scheduled_scan_from_follower_or_before_due(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = CoordinationStore(
        tmp_path / "coordination.sqlite3",
        clock=lambda: now[0],
    )
    controller = InMemoryBackgroundTaskController()
    service = CoordinatedControllerService(controller, store)
    automatic = TaskCommand(
        "scheduled scan",
        TaskArea.CUSTOMIZATION,
        Capability.LIST_ORDERS,
        payload={"trigger": "five_minute_timer"},
    )
    try:
        service.register("leader-pc", "Leader")
        service.register("follower-pc", "Follower")
        initial = service.snapshot_payload("leader-pc")
        assert initial["snapshot"]["scheduled_scan_due_at"] == {
            "five_minute_timer": 1300.0,
            "three_hour_timer": 11800.0,
        }

        follower = service.invoke(
            instance_id="follower-pc",
            request_id="follower-scan",
            method="submit_task",
            raw_args=[to_jsonable(automatic)],
            raw_kwargs={},
        )
        early = service.invoke(
            instance_id="leader-pc",
            request_id="leader-early",
            method="submit_task",
            raw_args=[to_jsonable(automatic)],
            raw_kwargs={},
        )
        assert follower["result"]["accepted"] is False
        assert follower["result"]["details"]["reason"] == "not_scheduler_leader"
        assert early["result"]["accepted"] is False
        assert early["result"]["details"]["reason"] == "not_due"

        now[0] = 1301.0
        due = service.invoke(
            instance_id="leader-pc",
            request_id="leader-due",
            method="submit_task",
            raw_args=[to_jsonable(automatic)],
            raw_kwargs={},
        )
        assert due["result"]["accepted"] is True
    finally:
        service.close()


def test_emergency_stop_and_capability_policy_remain_global(
    tmp_path: Path,
) -> None:
    controllers: dict[str, InMemoryBackgroundTaskController] = {}

    def factory(identity: OperatorIdentity) -> InMemoryBackgroundTaskController:
        controller = InMemoryBackgroundTaskController()
        controllers[identity.email] = controller
        return controller

    service = CoordinatedControllerService(
        None,
        CoordinationStore(tmp_path / "coordination.sqlite3"),
        controller_factory=factory,
    )
    alice = OperatorIdentity("alice@billyprint.com", "Alice", "alice-subject")
    bob = OperatorIdentity("bob@billyprint.com", "Bob", "bob-subject")
    try:
        service.register("alice-pc", "PC-A", identity=alice)
        service.register("bob-pc", "PC-B", identity=bob)
        service.invoke(
            instance_id="alice-pc",
            request_id="global-stop",
            method="set_emergency_stop_writes",
            raw_args=[True],
            raw_kwargs={},
            identity=alice,
        )
        service.invoke(
            instance_id="alice-pc",
            request_id="global-mode",
            method="update_capability_mode",
            raw_args=[Capability.LIST_ORDERS.value, CapabilityMode.BROWSER.value],
            raw_kwargs={},
            identity=alice,
        )

        assert controllers[alice.email].snapshot().policy.emergency_stop_writes is True
        assert controllers[bob.email].snapshot().policy.emergency_stop_writes is True
        assert (
            controllers[bob.email]
            .snapshot()
            .policy.configured_mode_for(Capability.LIST_ORDERS)
            is CapabilityMode.BROWSER
        )
    finally:
        service.close()
