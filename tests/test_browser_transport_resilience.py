from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from erp_automation.application.browser_preflight import (
    BrowserEndpointCircuitBreaker,
    BrowserEndpointHealth,
)
from erp_automation.application.desktop_tasks import DesktopTaskRunner
from erp_automation.contracts.models import (
    Capability,
    DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY,
    TaskArea,
    TaskCommand,
)
from erp_automation.coordination.remote_controller import (
    RemoteBackgroundTaskController,
)
from erp_automation.coordination.tunnel_supervisor import (
    SshTunnelSpec,
    SshTunnelSupervisor,
)
from erp_automation.ui.models import DesktopSnapshot
from erp_automation.ui.qt import _format_local_transport_health


class _FakeProcess:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_tunnel_supervisor_records_exit_reason_and_reconnects(tmp_path: Path) -> None:
    diagnostic = tmp_path / "main-browser-openssh.log"
    diagnostic.write_text("client_loop: send disconnect: Connection reset\n", encoding="utf-8")
    lifecycle = tmp_path / "lifecycle.jsonl"
    dead = _FakeProcess(255)
    replacement = _FakeProcess(None)
    starts: list[tuple[str, ...]] = []
    stopped: list[_FakeProcess] = []
    spec = SshTunnelSpec(
        key="main_browser",
        label="主浏览器",
        command=("ssh.exe", "-N"),
        diagnostic_log=diagnostic,
    )
    supervisor = SshTunnelSupervisor(
        ((spec, dead),),
        process_factory=lambda command: starts.append(tuple(command)) or replacement,
        process_stopper=stopped.append,
        lifecycle_log=lifecycle,
        restart_backoff_seconds=(1.0,),
        clock=lambda: 0.0,
    )

    supervisor.poll_once(now=0.0)
    failed = supervisor.snapshot().lanes[0]
    assert failed.healthy is False
    assert failed.last_exit_code == 255
    assert "Connection reset" in failed.last_error
    assert starts == []

    supervisor.poll_once(now=1.0)
    recovered = supervisor.snapshot().lanes[0]
    assert recovered.healthy is True
    assert recovered.restart_count == 1
    assert starts == [("ssh.exe", "-N")]

    events = [json.loads(line) for line in lifecycle.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "tunnel_exited"
    assert events[0]["exit_code"] == 255
    assert "Connection reset" in events[0]["reason"]
    assert events[1]["event"] == "tunnel_restarted"
    supervisor.close()
    assert stopped == [replacement]


def test_browser_preflight_circuit_suppresses_repeated_failed_probes() -> None:
    calls: list[str] = []
    now = [10.0]

    async def failed_probe(endpoint: str) -> BrowserEndpointHealth:
        calls.append(endpoint)
        return BrowserEndpointHealth(False, "connection refused")

    guard = BrowserEndpointCircuitBreaker(
        failed_probe,
        cooldown_seconds=30.0,
        clock=lambda: now[0],
    )

    first = asyncio.run(guard.check("http://127.0.0.1:24000"))
    second = asyncio.run(guard.check("http://127.0.0.1:24000"))
    now[0] = 41.0
    third = asyncio.run(guard.check("http://127.0.0.1:24000"))

    assert first.healthy is False and first.cached is False
    assert second.healthy is False and second.cached is True
    assert third.cached is False
    assert calls == ["http://127.0.0.1:24000"] * 2


def test_server_runner_blocks_before_business_code_when_browser_lane_is_down(
    tmp_path: Path,
) -> None:
    class Guard:
        async def check(self, endpoint: str) -> BrowserEndpointHealth:
            assert endpoint == "http://127.0.0.1:24000"
            return BrowserEndpointHealth(False, "connection refused")

    runner = DesktopTaskRunner(
        tmp_path,
        settings_provider=lambda: (_ for _ in ()).throw(
            AssertionError("business settings must not be loaded")
        ),
        configuration_provider=lambda: {},
        browser_endpoint_guard=Guard(),
    )
    command = TaskCommand(
        "处理定制订单",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="111-222",
        payload={
            DESKTOP_BROWSER_ENDPOINT_PAYLOAD_KEY: "http://127.0.0.1:24000",
        },
    )

    result = runner(command)

    assert result.succeeded is False
    assert result.blocked is True
    assert result.payload["browser_tunnel_unavailable"] is True
    assert result.payload["retryable"] is True
    assert result.payload["preserve_order_pending"] is True
    assert result.payload["shared_prerequisite_error"].startswith(
        "visible_browser_tunnel:"
    )


def test_remote_submission_is_rejected_while_its_browser_tunnel_recovers() -> None:
    class BrowserHost:
        def ensure_started(self) -> None:
            raise AssertionError("Chrome must not start while the tunnel is down")

    client = object.__new__(RemoteBackgroundTaskController)
    client._metadata_lock = threading.RLock()
    client._transport_health_provider = lambda: {
        "all_healthy": False,
        "lanes": {
            "main_browser": {
                "label": "主浏览器",
                "healthy": False,
                "recovering": True,
            }
        },
    }
    client._lock = threading.RLock()
    client._authentication_required = False
    client._authentication_error = ""
    client._local_pause_requested = False
    client._browser_host = BrowserHost()
    client.browser_endpoint = "http://127.0.0.1:24000"
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "desktop-one"
    client._browser_cleanup_task_ids = set()
    client._logistics_browser_cleanup_task_ids = set()
    client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("down-lane task must not reach the server")
    )
    command = TaskCommand(
        "处理定制订单",
        TaskArea.CUSTOMIZATION,
        Capability.UPDATE_CONTACT,
        order_no="111-222",
    )

    result = client._rpc("submit_task", command)

    assert result.accepted is False
    assert result.details["browser_tunnel_recovering"] is True
    assert result.details["browser_lane"] == "main_browser"


def test_ui_transport_text_requires_all_three_lanes() -> None:
    health = {
        "all_healthy": False,
        "lanes": {
            "api": {"label": "API", "healthy": True},
            "main_browser": {
                "label": "主浏览器",
                "healthy": False,
                "last_error": "Connection reset",
            },
            "logistics_browser": {"label": "物流浏览器", "healthy": True},
        },
    }

    text, tooltip = _format_local_transport_health(health)

    assert text == "本地通道重连中：主浏览器"
    assert "API：正常" in tooltip
    assert "主浏览器：自动重连中" in tooltip
    assert "物流浏览器：正常" in tooltip
