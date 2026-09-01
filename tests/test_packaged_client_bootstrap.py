from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from erp_automation import app
from erp_automation.coordination import client_bootstrap
from erp_automation.ui.controller import ControlResult
from erp_automation.ui.models import DesktopSnapshot


def _packaged_layout(
    tmp_path: Path,
) -> tuple[client_bootstrap.PackagedClientPaths, dict[str, str]]:
    program_root = tmp_path / "Programs" / "LingxingERP" / "2026.07.24.4"
    executable = program_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    updater = program_root / "scripts" / "update_shared_client.ps1"
    version_file = program_root / "VERSION.txt"
    system_root = tmp_path / "Windows"
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    ssh = system_root / "System32" / "OpenSSH" / "ssh.exe"
    cloudflared = executable.parent / "_internal" / "tools" / "cloudflared.exe"
    local_appdata = tmp_path / "LocalAppData"
    state_root = local_appdata / "LingxingERP"
    ssh_key = state_root / "server-tunnel-ed25519"
    known_hosts = state_root / "known_hosts"
    token_file = state_root / "coordination-token"
    for path in (
        executable,
        updater,
        powershell,
        ssh,
        cloudflared,
        ssh_key,
        known_hosts,
        token_file,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    version_file.write_text("2026.07.24.4\n", encoding="utf-8")
    environ = {
        "LOCALAPPDATA": str(local_appdata),
        "SystemRoot": str(system_root),
        "PATH": "",
    }
    paths = client_bootstrap.resolve_packaged_client_paths(
        executable,
        environ=environ,
        embedded_version="2026.07.24.4",
    )
    return paths, environ


def test_only_unconfigured_packaged_windows_exe_bootstraps() -> None:
    assert client_bootstrap.should_bootstrap_packaged_shared_client(
        frozen=True,
        platform="win32",
        environ={},
    )
    assert not client_bootstrap.should_bootstrap_packaged_shared_client(
        frozen=False,
        platform="win32",
        environ={},
    )
    assert not client_bootstrap.should_bootstrap_packaged_shared_client(
        frozen=True,
        platform="linux",
        environ={},
    )
    assert not client_bootstrap.should_bootstrap_packaged_shared_client(
        frozen=True,
        platform="win32",
        environ={"ERP_AUTOMATION_SERVER_URL": "http://127.0.0.1:18765"},
    )


def test_packaged_paths_are_resolved_from_the_exe_itself(tmp_path: Path) -> None:
    paths, _ = _packaged_layout(tmp_path)

    assert paths.program_root == paths.executable.parents[2]
    assert paths.version_file == paths.program_root / "VERSION.txt"
    assert paths.updater_script == (
        paths.program_root / "scripts" / "update_shared_client.ps1"
    )
    assert paths.ssh.name == "ssh.exe"
    assert client_bootstrap.read_client_version(paths) == "2026.07.24.4"


def test_local_test_selects_the_newest_complete_formal_install(
    tmp_path: Path,
) -> None:
    local_appdata = tmp_path / "LocalAppData"
    program_base = local_appdata / "Programs" / "LingxingERP"
    expected = None
    for version in ("2026.08.05.9", "2026.08.06.1"):
        root = program_base / version
        executable = root / "dist" / "ERP自动化" / "ERP自动化.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"formal")
        (root / "VERSION.txt").write_text(version, encoding="utf-8")
        expected = executable.resolve()
    incomplete = program_base / "2026.08.06.2"
    incomplete.mkdir(parents=True)
    (incomplete / "VERSION.txt").write_text("2026.08.06.2", encoding="utf-8")

    selected = client_bootstrap.resolve_installed_formal_client_executable(
        environ={"LOCALAPPDATA": str(local_appdata)}
    )

    assert selected == expected


def test_local_test_bootstrap_skips_the_formal_update_channel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    paths.token_file.write_text("t" * 48, encoding="utf-8")
    monkeypatch.setattr(
        client_bootstrap,
        "resolve_local_test_formal_client_paths",
        lambda: paths,
    )
    monkeypatch.setattr(
        client_bootstrap,
        "run_client_update",
        lambda *_args, **_kwargs: pytest.fail(
            "Local source testing must not invoke the formal update channel."
        ),
    )
    monkeypatch.setattr(
        client_bootstrap,
        "_start_tunnel",
        lambda _command: (_ for _ in ()).throw(RuntimeError("tunnel reached")),
    )

    with pytest.raises(RuntimeError, match="tunnel reached"):
        client_bootstrap.bootstrap_local_test_shared_client()
    assert (
        "ERP_AUTOMATION_LOCAL_TEST_FORMAL_BASELINE_VERSION" not in os.environ
    )


def test_operator_browser_local_port_is_stable_across_erp_restarts() -> None:
    first = client_bootstrap._operator_browser_local_port(
        "Steven@BillyPrint.com"
    )
    second = client_bootstrap._operator_browser_local_port(
        " steven@billyprint.com "
    )

    assert first == second
    assert (
        client_bootstrap.LOCAL_BROWSER_PORT_START
        <= first
        <= client_bootstrap.LOCAL_BROWSER_PORT_END
    )
    assert first != client_bootstrap._operator_browser_local_port(
        "another@billyprint.com"
    )


def test_logistics_browser_port_is_stable_and_isolated_from_order_browser() -> None:
    order_port = client_bootstrap._operator_browser_local_port(
        "steven@billyprint.com"
    )
    first = client_bootstrap._operator_logistics_browser_local_port(
        "Steven@BillyPrint.com"
    )
    second = client_bootstrap._operator_logistics_browser_local_port(
        " steven@billyprint.com "
    )

    assert first == second
    assert first != order_port
    assert (
        client_bootstrap.LOCAL_BROWSER_PORT_START
        <= first
        <= client_bootstrap.LOCAL_BROWSER_PORT_END
    )


def test_local_browser_port_can_be_reused_only_by_healthy_chrome(
    monkeypatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        listener.listen()

        monkeypatch.setattr(
            client_bootstrap,
            "_debugging_endpoint_healthy",
            lambda candidate: candidate == port,
        )
        client_bootstrap._assert_local_browser_port_reusable(port)

        monkeypatch.setattr(
            client_bootstrap,
            "_debugging_endpoint_healthy",
            lambda _candidate: False,
        )
        with pytest.raises(
            client_bootstrap.PackagedClientBootstrapError,
            match="已被其他程序占用",
        ):
            client_bootstrap._assert_local_browser_port_reusable(port)


def test_fresh_package_reports_all_missing_access_material(tmp_path: Path) -> None:
    paths, environ = _packaged_layout(tmp_path)
    paths.ssh_key.unlink()
    paths.known_hosts.unlink()
    paths.token_file.unlink()

    unresolved = client_bootstrap.resolve_packaged_client_paths(
        paths.executable,
        environ=environ,
        require_access_files=False,
    )

    missing = client_bootstrap.missing_client_access_files(unresolved)
    assert {path.name for _label, path in missing} == {
        "server-tunnel-ed25519",
        "known_hosts",
        "coordination-token",
    }
    with pytest.raises(client_bootstrap.PackagedClientBootstrapError):
        client_bootstrap.resolve_packaged_client_paths(
            paths.executable,
            environ=environ,
            require_access_files=True,
        )


def test_packaged_bootstrap_requires_first_run_access_setup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.ssh_key.unlink()
    paths.known_hosts.unlink()
    paths.token_file.unlink()
    setup_calls: list[Path] = []
    update_calls: list[bool] = []
    events: list[str] = []

    def fake_resolve(*_args, require_access_files=True, **_kwargs):
        if require_access_files:
            assert not client_bootstrap.missing_client_access_files(paths)
        return paths

    def fake_setup(candidate):
        events.append("setup")
        setup_calls.append(candidate.state_root)
        candidate.ssh_key.write_bytes(b"private")
        candidate.known_hosts.write_bytes(b"host")
        candidate.token_file.write_text("t" * 48, encoding="utf-8")
        return True

    monkeypatch.setattr(
        client_bootstrap,
        "resolve_packaged_client_paths",
        fake_resolve,
    )
    monkeypatch.setattr(
        client_bootstrap,
        "run_client_update",
        lambda *_args, **_kwargs: (
            events.append("update")
            or
            update_calls.append(True)
            or client_bootstrap.ClientUpdateResult(
                status="current",
                current_version="",
                latest_version="",
            )
        ),
    )
    def stop_after_setup(*_args, **_kwargs):
        raise RuntimeError("stop after first-run setup")

    monkeypatch.setattr(client_bootstrap, "_start_tunnel", stop_after_setup)

    with pytest.raises(RuntimeError, match="stop after first-run setup"):
        client_bootstrap.bootstrap_packaged_shared_client(
            access_setup_callback=fake_setup,
        )

    assert setup_calls == [paths.state_root]
    assert update_calls == [True]
    assert events == ["update", "setup"]


def test_packaged_bootstrap_fails_closed_when_access_setup_is_cancelled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.ssh_key.unlink()
    paths.known_hosts.unlink()
    paths.token_file.unlink()
    monkeypatch.setattr(
        client_bootstrap,
        "resolve_packaged_client_paths",
        lambda *_args, **_kwargs: paths,
    )
    monkeypatch.setattr(
        client_bootstrap,
        "run_client_update",
        lambda *_args, **_kwargs: client_bootstrap.ClientUpdateResult(
            status="current",
            current_version="2026.07.31.2",
            latest_version="2026.07.31.2",
        ),
    )

    with pytest.raises(client_bootstrap.PackagedClientBootstrapError):
        client_bootstrap.bootstrap_packaged_shared_client(
            access_setup_callback=lambda _paths: False,
        )


def test_first_run_configuration_is_imported_once_and_read_back() -> None:
    fingerprint = "a" * 64
    imported_paths: list[Path] = []
    passphrase = "portable configuration password"

    class Controller:
        def import_portable_migration(
            self,
            package_path: str,
            supplied_passphrase: str,
            *,
            overwrite: bool,
            configuration_only: bool,
        ) -> ControlResult:
            path = Path(package_path)
            assert path.read_bytes() == b"encrypted-settings"
            assert supplied_passphrase == passphrase
            assert overwrite is True
            assert configuration_only is True
            imported_paths.append(path)
            return ControlResult(
                True,
                "imported",
                details={
                    "target_operator_email": "alice@billyprint.com",
                    "configuration_fingerprint": fingerprint,
                    "configured_non_sensitive_field_count": 3,
                    "configured_secret_field_count": 2,
                },
            )

        def snapshot(self) -> DesktopSnapshot:
            return DesktopSnapshot(
                configuration_fingerprint=fingerprint,
                configured_non_sensitive_field_count=3,
                configured_secret_field_count=2,
            )

    statuses: list[str] = []
    client_bootstrap._restore_first_run_configuration(
        Controller(),  # type: ignore[arg-type]
        encrypted_package=b"encrypted-settings",
        passphrase=passphrase,
        operator_email="alice@billyprint.com",
        status=statuses.append,
    )

    assert len(imported_paths) == 1
    assert imported_paths[0].exists() is False
    assert "alice@billyprint.com" in statuses[-1]
    assert "3 项非敏感配置" in statuses[-1]
    assert passphrase not in " ".join(statuses)
    setup = client_bootstrap.ClientAccessSetupResult(
        True,
        b"encrypted-settings",
        passphrase,
    )
    assert passphrase not in repr(setup)
    assert "encrypted-settings" not in repr(setup)


def test_first_run_configuration_fails_closed_on_readback_mismatch() -> None:
    class Controller:
        def import_portable_migration(self, *_args, **_kwargs) -> ControlResult:
            return ControlResult(
                True,
                "imported",
                details={
                    "target_operator_email": "alice@billyprint.com",
                    "configuration_fingerprint": "a" * 64,
                },
            )

        def snapshot(self) -> DesktopSnapshot:
            return DesktopSnapshot(configuration_fingerprint="b" * 64)

    with pytest.raises(
        client_bootstrap.PackagedClientBootstrapError,
        match="回读校验失败",
    ):
        client_bootstrap._restore_first_run_configuration(
            Controller(),  # type: ignore[arg-type]
            encrypted_package=b"encrypted-settings",
            passphrase="portable configuration password",
            operator_email="alice@billyprint.com",
            status=lambda _message: None,
        )


def test_outdated_fresh_package_updates_before_requesting_access(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.ssh_key.unlink()
    paths.known_hosts.unlink()
    paths.token_file.unlink()
    updated_application = tmp_path / "installed" / "ERP自动化.exe"
    updated_application.parent.mkdir()
    updated_application.write_bytes(b"updated")
    started: list[Path] = []
    monkeypatch.setattr(
        client_bootstrap,
        "resolve_packaged_client_paths",
        lambda *_args, **_kwargs: paths,
    )
    monkeypatch.setattr(
        client_bootstrap,
        "run_client_update",
        lambda *_args, **_kwargs: client_bootstrap.ClientUpdateResult(
            status="updated",
            current_version="2026.07.31.1",
            latest_version="2026.07.31.2",
            application_path=updated_application,
        ),
    )
    monkeypatch.setattr(
        client_bootstrap,
        "start_updated_client",
        lambda application: started.append(application),
    )

    outcome = client_bootstrap.bootstrap_packaged_shared_client(
        access_setup_callback=lambda _paths: pytest.fail(
            "The new executable must request access after it restarts."
        ),
    )

    assert outcome.should_exit is True
    assert started == [updated_application]


def test_project_dist_exe_ignores_mutable_source_version_file(
    tmp_path: Path,
) -> None:
    program_root = tmp_path / "ERP自动化"
    executable = program_root / "dist" / "ERP自动化" / "ERP自动化.exe"
    updater = program_root / "scripts" / "update_shared_client.ps1"
    version_file = program_root / "VERSION.txt"
    source_version_file = program_root / "CLIENT_VERSION"
    system_root = tmp_path / "Windows"
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    ssh = system_root / "System32" / "OpenSSH" / "ssh.exe"
    cloudflared = executable.parent / "_internal" / "tools" / "cloudflared.exe"
    local_appdata = tmp_path / "LocalAppData"
    state_root = local_appdata / "LingxingERP"
    required_files = (
        executable,
        updater,
        powershell,
        ssh,
        cloudflared,
        state_root / "server-tunnel-ed25519",
        state_root / "known_hosts",
        state_root / "coordination-token",
    )
    for path in required_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test")
    version_file.write_text("2026.07.24.5\n", encoding="utf-8")
    source_version_file.write_text("2099.01.01.1\n", encoding="utf-8")

    paths = client_bootstrap.resolve_packaged_client_paths(
        executable,
        environ={
            "LOCALAPPDATA": str(local_appdata),
            "SystemRoot": str(system_root),
            "PATH": "",
        },
        embedded_version="2026.07.24.5",
    )

    assert paths.program_root == program_root
    assert paths.version_file == version_file
    assert client_bootstrap.read_client_version(paths) == "2026.07.24.5"


def test_packaged_exe_uses_embedded_version_when_external_file_is_stale(
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.version_file.write_text("2026.07.24.5\n", encoding="utf-8")

    assert client_bootstrap.read_client_version(paths) == "2026.07.24.4"


def test_packaged_exe_does_not_require_external_version_file(
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.version_file.unlink()

    resolved = client_bootstrap.resolve_packaged_client_paths(
        paths.executable,
        environ=_environ,
        embedded_version="2026.07.24.4",
    )

    assert client_bootstrap.read_client_version(resolved) == "2026.07.24.4"


def test_exe_invokes_only_the_machine_readable_updater_component(
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    captured: dict[str, object] = {}
    payload = {
        "status": "current",
        "current_version": "2026.07.24.4",
        "latest_version": "2026.07.24.4",
        "launcher_path": "",
        "application_path": "",
    }

    def fake_runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload, ensure_ascii=False).encode(),
            stderr=b"",
        )

    result = client_bootstrap.run_client_update(
        paths,
        runner=fake_runner,
    )

    command = captured["command"]
    assert str(paths.updater_script) in command
    assert "start_shared_desktop.ps1" not in " ".join(command)
    assert command[-1] == "-OutputJson"
    assert "-InstanceName" not in command
    version_index = command.index("-CurrentVersion")
    assert command[version_index + 1] == "2026.07.24.4"
    package_root_index = command.index("-CurrentPackageRoot")
    assert command[package_root_index + 1] == str(paths.program_root)
    process_id_index = command.index("-CurrentProcessId")
    assert int(command[process_id_index + 1]) > 0
    assert "-CurrentVersionFile" not in command
    assert result.status == "current"
    assert captured["kwargs"]["cwd"] == str(paths.program_root)


def test_ssh_forwarding_is_owned_by_the_exe_bootstrap(tmp_path: Path) -> None:
    paths, _ = _packaged_layout(tmp_path)
    diagnostic_log = tmp_path / "logs" / "api-openssh.log"

    local = client_bootstrap.build_ssh_tunnel_command(
        paths,
        forward="-L",
        diagnostic_log=diagnostic_log,
    )
    reverse = client_bootstrap.build_ssh_tunnel_command(paths, forward="-R")

    assert local[0] == str(paths.ssh)
    assert "BatchMode=yes" in local
    assert "ExitOnForwardFailure=yes" in local
    assert "LogLevel=ERROR" in local
    assert f"UserKnownHostsFile={paths.known_hosts}" in local
    assert local[local.index("-E") + 1] == str(diagnostic_log)
    assert diagnostic_log.parent.is_dir()
    assert local[-1] == "-L"
    assert reverse[-1] == "-R"


def test_browser_endpoint_registration_retries_idempotently_after_timeout() -> None:
    requests: list[dict[str, object]] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        requests.append(json.loads(request.content))
        if calls == 1:
            raise httpx.ReadTimeout("slow origin", request=request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "browser_endpoint": "http://127.0.0.1:26001",
                "browser_port": 26001,
            },
        )

    delays: list[float] = []
    statuses: list[str] = []
    with httpx.Client(
        base_url="http://shared.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = client_bootstrap._allocate_browser_endpoint(
            client,
            instance_id="stable-instance",
            display_name="PC-A",
            client_version="2026.08.07.2",
            status_callback=statuses.append,
            retry_sleep=delays.append,
        )

    assert result["ok"] is True
    assert calls == 2
    assert {item["instance_id"] for item in requests} == {"stable-instance"}
    assert delays == [0.5]
    assert any("2/3" in status for status in statuses)


def test_browser_endpoint_registration_explains_access_origin_outage() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"ok": False, "error": "access_verification_unavailable"},
        )

    with httpx.Client(
        base_url="http://shared.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(
            client_bootstrap.PackagedClientBootstrapError,
            match="无需重新安装客户端",
        ) as exc_info:
            client_bootstrap._allocate_browser_endpoint(
                client,
                instance_id="stable-instance",
                display_name="PC-A",
                client_version="2026.08.07.2",
                attempts=2,
                retry_sleep=lambda _seconds: None,
            )

    assert "timed out" not in str(exc_info.value).casefold()
    assert "企业邮箱" in str(exc_info.value)


def test_cloudflare_login_uses_pinned_component_and_returns_only_jwt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    monkeypatch.setattr(
        client_bootstrap,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(b"test").hexdigest(),
    )
    captured: dict[str, object] = {}
    expected = "header.payload.signature"

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return expected.encode(), b""

    def factory(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    token = client_bootstrap.obtain_cloudflare_access_token(
        paths,
        process_factory=factory,
    )

    assert token == expected
    assert captured["command"] == [
        str(paths.cloudflared),
        "access",
        "login",
        "--app",
        client_bootstrap.CLOUDFLARE_ACCESS_APP_URL,
        "--no-verbose",
        "--auto-close",
    ]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


def test_cloudflare_cached_token_read_never_invokes_login_command(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    monkeypatch.setattr(
        client_bootstrap,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(b"test").hexdigest(),
    )
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return b"header.payload.signature", b""

    def factory(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    token = client_bootstrap.read_cached_cloudflare_access_token(
        paths,
        process_factory=factory,
    )

    assert token == "header.payload.signature"
    assert captured["command"] == [
        str(paths.cloudflared),
        "access",
        "token",
        "--app",
        client_bootstrap.CLOUDFLARE_ACCESS_APP_URL,
    ]
    assert "login" not in captured["command"]


def test_cloudflare_missing_cached_token_requires_explicit_login(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    monkeypatch.setattr(
        client_bootstrap,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(b"test").hexdigest(),
    )

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self):
            return b"", b"Unable to find token for provided application."

    with pytest.raises(client_bootstrap.CloudflareAccessLoginRequired):
        client_bootstrap.read_cached_cloudflare_access_token(
            paths,
            process_factory=lambda *_args, **_kwargs: FakeProcess(),
        )


def test_cloudflare_login_retries_transient_app_info_probe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    monkeypatch.setattr(
        client_bootstrap,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(b"test").hexdigest(),
    )
    expected = "header.payload.signature"
    processes = [
        (
            1,
            b"",
            (
                b'failed to get app info: Head "https://login.example/'
                b'?secret=1": i/o timeout'
            ),
        ),
        (0, expected.encode(), b""),
    ]
    commands: list[list[str]] = []
    delays: list[float] = []
    statuses: list[str] = []

    class FakeProcess:
        def __init__(self, returncode, stdout, stderr):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        def poll(self):
            return self.returncode

        def communicate(self):
            return self._stdout, self._stderr

    def factory(command, **_kwargs):
        commands.append(command)
        return FakeProcess(*processes[len(commands) - 1])

    token = client_bootstrap.obtain_cloudflare_access_token(
        paths,
        process_factory=factory,
        status_callback=statuses.append,
        retry_sleep=delays.append,
    )

    assert token == expected
    assert len(commands) == 2
    assert commands[0] == commands[1]
    assert delays == [0.5]
    assert any("2/5" in status for status in statuses)


def test_cloudflare_login_exhaustion_does_not_leak_login_url(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)
    monkeypatch.setattr(
        client_bootstrap,
        "CLOUDFLARED_SHA256",
        hashlib.sha256(b"test").hexdigest(),
    )
    process_count = 0
    leaked_url = (
        "https://login.example/cdn-cgi/access/login/app.example"
        "?secret=one-time-value"
    )

    class FakeProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def communicate(self):
            return b"", f'failed to get app info: Head "{leaked_url}"'.encode()

    def factory(_command, **_kwargs):
        nonlocal process_count
        process_count += 1
        return FakeProcess()

    with pytest.raises(
        client_bootstrap.PackagedClientBootstrapError,
        match="已自动重试 2 次",
    ) as exc_info:
        client_bootstrap.obtain_cloudflare_access_token(
            paths,
            process_factory=factory,
            retry_attempts=2,
            retry_sleep=lambda _seconds: None,
        )

    message = str(exc_info.value)
    assert process_count == 2
    assert "https://" not in message
    assert "cdn-cgi" not in message
    assert "one-time-value" not in message


def test_cloudflare_login_rejects_a_tampered_bundled_component(
    tmp_path: Path,
) -> None:
    paths, _ = _packaged_layout(tmp_path)

    with pytest.raises(
        client_bootstrap.PackagedClientBootstrapError,
        match="integrity verification",
    ):
        client_bootstrap.obtain_cloudflare_access_token(
            paths,
            process_factory=lambda *_args, **_kwargs: pytest.fail(
                "A tampered component must never execute."
            ),
        )


def test_updated_exe_starts_without_shortcut_arguments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    application = (
        tmp_path
        / "Programs"
        / "LingxingERP"
        / "2026.07.24.5"
        / "dist"
        / "ERP自动化"
        / "ERP自动化.exe"
    )
    application.parent.mkdir(parents=True)
    application.write_bytes(b"test")
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(client_bootstrap.subprocess, "Popen", fake_popen)

    client_bootstrap.start_updated_client(application)

    assert captured["command"] == [str(application)]
    assert captured["kwargs"]["cwd"] == str(application.parents[2])


def test_shortcut_instance_option_is_removed_before_qt_arguments() -> None:
    cleaned, instance_name = app.consume_shared_instance_name(
        ["--shared-instance-name", "Workstation A", "--style", "Fusion"]
    )

    assert instance_name == "Workstation A"
    assert cleaned == ["--style", "Fusion"]


def test_main_bootstraps_direct_exe_then_runs_and_closes_same_session(
    monkeypatch,
) -> None:
    controller = object()

    class FakeSession:
        def __init__(self) -> None:
            self.controller = controller
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    class FakeFeedback:
        owns_application = True

        def __init__(self) -> None:
            self.messages: list[str] = []
            self.close_count = 0

        def update(self, message: str) -> None:
            self.messages.append(message)

        def close(self) -> None:
            self.close_count += 1

    session = FakeSession()
    feedback = FakeFeedback()
    captured: dict[str, object] = {}
    monkeypatch.setattr(app, "require_pyside6", lambda: None)
    monkeypatch.setattr(
        app,
        "should_bootstrap_packaged_shared_client",
        lambda: True,
    )
    monkeypatch.setattr(
        app,
        "create_packaged_startup_feedback",
        lambda _argv: feedback,
    )
    monkeypatch.setattr(
        app,
        "bootstrap_packaged_shared_client",
        lambda **kwargs: (
            captured.update(kwargs)
            or client_bootstrap.PackagedClientBootstrapOutcome(session=session)
        ),
    )

    import erp_automation.ui.qt as qt

    def fake_run_desktop(runtime_controller, **kwargs):
        captured["controller"] = runtime_controller
        captured["desktop_kwargs"] = kwargs
        return 17

    monkeypatch.setattr(qt, "run_desktop", fake_run_desktop)

    assert app.main(["--shared-instance-name", "Mayn"]) == 17
    assert captured["instance_name"] == "Mayn"
    assert callable(captured["access_setup_callback"])
    assert callable(captured["access_login_callback"])
    assert captured["controller"] is controller
    desktop_kwargs = captured["desktop_kwargs"]
    assert desktop_kwargs["argv"] == []
    assert desktop_kwargs["execute_existing_application"] is True
    assert callable(desktop_kwargs["required_client_update_handler"])
    assert callable(desktop_kwargs["runtime_restart_callback"])
    assert feedback.close_count == 1
    assert session.close_count == 1


def test_main_runtime_update_restarts_only_after_session_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller = object()
    events: list[object] = []
    application_path = tmp_path / "new" / "ERP自动化.exe"
    application_path.parent.mkdir()
    application_path.write_bytes(b"new")

    class FakeSession:
        def __init__(self) -> None:
            self.controller = controller

        def close(self) -> None:
            events.append("session_closed")

    feedback = SimpleNamespace(
        owns_application=True,
        update=lambda _message: None,
        close=lambda: None,
    )
    monkeypatch.setattr(app, "require_pyside6", lambda: None)
    monkeypatch.setattr(
        app,
        "should_bootstrap_packaged_shared_client",
        lambda: True,
    )
    monkeypatch.setattr(
        app,
        "create_packaged_startup_feedback",
        lambda _argv: feedback,
    )
    monkeypatch.setattr(
        app,
        "bootstrap_packaged_shared_client",
        lambda **_kwargs: client_bootstrap.PackagedClientBootstrapOutcome(
            session=FakeSession()
        ),
    )
    paths = SimpleNamespace()
    monkeypatch.setattr(
        app,
        "resolve_packaged_client_paths",
        lambda **_kwargs: paths,
    )
    monkeypatch.setattr(
        app,
        "run_client_update",
        lambda supplied_paths: (
            events.append(("updated", supplied_paths))
            or client_bootstrap.ClientUpdateResult(
                status="updated",
                current_version="2026.07.31.1",
                latest_version="2026.07.31.2",
                application_path=application_path,
            )
        ),
    )
    monkeypatch.setattr(
        app,
        "start_updated_client",
        lambda path: events.append(("restarted", path)),
    )

    import erp_automation.ui.qt as qt

    def fake_run_desktop(runtime_controller, **kwargs):
        assert runtime_controller is controller
        result = kwargs["required_client_update_handler"]("2026.07.31.2")
        kwargs["runtime_restart_callback"](result.application_path)
        return 17

    monkeypatch.setattr(qt, "run_desktop", fake_run_desktop)

    assert app.main([]) == 17
    assert events == [
        ("updated", paths),
        "session_closed",
        ("restarted", application_path),
    ]


def test_main_rejects_runtime_manifest_that_does_not_match_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller = object()

    class FakeSession:
        def __init__(self) -> None:
            self.controller = controller

        def close(self) -> None:
            pass

    feedback = SimpleNamespace(
        owns_application=True,
        update=lambda _message: None,
        close=lambda: None,
    )
    monkeypatch.setattr(app, "require_pyside6", lambda: None)
    monkeypatch.setattr(app, "should_bootstrap_packaged_shared_client", lambda: True)
    monkeypatch.setattr(app, "create_packaged_startup_feedback", lambda _argv: feedback)
    monkeypatch.setattr(
        app,
        "bootstrap_packaged_shared_client",
        lambda **_kwargs: client_bootstrap.PackagedClientBootstrapOutcome(
            session=FakeSession()
        ),
    )
    monkeypatch.setattr(
        app,
        "resolve_packaged_client_paths",
        lambda **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        app,
        "run_client_update",
        lambda _paths: client_bootstrap.ClientUpdateResult(
            status="updated",
            current_version="2026.07.31.1",
            latest_version="2026.07.31.2",
            application_path=tmp_path / "wrong.exe",
        ),
    )

    import erp_automation.ui.qt as qt

    def fake_run_desktop(_runtime_controller, **kwargs):
        with pytest.raises(RuntimeError, match="版本与服务器要求不一致"):
            kwargs["required_client_update_handler"]("2026.07.31.3")
        return 0

    monkeypatch.setattr(qt, "run_desktop", fake_run_desktop)
    assert app.main([]) == 0


def test_main_exits_old_process_after_updater_starts_new_exe(monkeypatch) -> None:
    feedback = SimpleNamespace(
        owns_application=True,
        update=lambda _message: None,
        close=lambda: None,
    )
    monkeypatch.setattr(app, "require_pyside6", lambda: None)
    monkeypatch.setattr(
        app,
        "should_bootstrap_packaged_shared_client",
        lambda: True,
    )
    monkeypatch.setattr(
        app,
        "create_packaged_startup_feedback",
        lambda _argv: feedback,
    )
    monkeypatch.setattr(
        app,
        "bootstrap_packaged_shared_client",
        lambda **_kwargs: client_bootstrap.PackagedClientBootstrapOutcome(
            should_exit=True
        ),
    )

    assert app.main([]) == 0


def test_main_local_source_uses_formal_shared_bootstrap_without_update_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = object()
    captured: dict[str, object] = {}

    class FakeSession:
        def __init__(self) -> None:
            self.controller = controller
            self.closed = False

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    feedback = SimpleNamespace(
        owns_application=True,
        update=lambda _message: None,
        close=lambda: None,
    )
    monkeypatch.setenv("USERNAME", "Mayn")
    monkeypatch.setattr(app, "require_pyside6", lambda: None)
    monkeypatch.setattr(
        app,
        "should_bootstrap_packaged_shared_client",
        lambda: False,
    )
    monkeypatch.setattr(
        app,
        "is_local_test_shared_server_mode",
        lambda: True,
    )
    monkeypatch.setattr(
        app,
        "create_packaged_startup_feedback",
        lambda _argv: feedback,
    )
    monkeypatch.setattr(
        app,
        "bootstrap_local_test_shared_client",
        lambda **kwargs: (
            captured.update(kwargs)
            or client_bootstrap.PackagedClientBootstrapOutcome(session=session)
        ),
    )

    import erp_automation.ui.qt as qt

    def fake_run_desktop(runtime_controller, **kwargs):
        captured["controller"] = runtime_controller
        captured["desktop_kwargs"] = kwargs
        return 17

    monkeypatch.setattr(qt, "run_desktop", fake_run_desktop)

    assert app.main([]) == 17
    assert captured["instance_name"] == "Mayn（本机测试）"
    assert callable(captured["access_login_callback"])
    assert captured["controller"] is controller
    desktop_kwargs = captured["desktop_kwargs"]
    assert desktop_kwargs["execute_existing_application"] is True
    assert desktop_kwargs["required_client_update_handler"] is None
    assert desktop_kwargs["runtime_restart_callback"] is None
    assert session.closed is True
