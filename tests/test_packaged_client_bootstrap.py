from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from erp_automation import app
from erp_automation.coordination import client_bootstrap


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

    def fake_resolve(*_args, require_access_files=True, **_kwargs):
        if require_access_files:
            assert not client_bootstrap.missing_client_access_files(paths)
        return paths

    def fake_setup(candidate):
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
            update_calls.append(True)
            or client_bootstrap.ClientUpdateResult(
                status="user_exit",
                current_version="",
                latest_version="",
            )
        ),
    )

    outcome = client_bootstrap.bootstrap_packaged_shared_client(
        access_setup_callback=fake_setup,
    )

    assert outcome.should_exit is True
    assert setup_calls == [paths.state_root]
    assert update_calls == [True]


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
        lambda *_args, **_kwargs: pytest.fail(
            "Updater must not run before access is configured."
        ),
    )

    with pytest.raises(client_bootstrap.PackagedClientBootstrapError):
        client_bootstrap.bootstrap_packaged_shared_client(
            access_setup_callback=lambda _paths: False,
        )


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


def test_packaged_exe_rejects_external_version_mismatch(
    tmp_path: Path,
) -> None:
    paths, _environ = _packaged_layout(tmp_path)
    paths.version_file.write_text("2026.07.24.5\n", encoding="utf-8")

    with pytest.raises(
        client_bootstrap.PackagedClientBootstrapError,
        match="内置版本与客户端包版本不一致",
    ):
        client_bootstrap.read_client_version(paths)


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
    assert "-CurrentVersionFile" not in command
    assert result.status == "current"
    assert captured["kwargs"]["cwd"] == str(paths.program_root)


def test_ssh_forwarding_is_owned_by_the_exe_bootstrap(tmp_path: Path) -> None:
    paths, _ = _packaged_layout(tmp_path)

    local = client_bootstrap.build_ssh_tunnel_command(paths, forward="-L")
    reverse = client_bootstrap.build_ssh_tunnel_command(paths, forward="-R")

    assert local[0] == str(paths.ssh)
    assert "BatchMode=yes" in local
    assert "ExitOnForwardFailure=yes" in local
    assert f"UserKnownHostsFile={paths.known_hosts}" in local
    assert local[-1] == "-L"
    assert reverse[-1] == "-R"


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
    assert captured["controller"] is controller
    assert captured["desktop_kwargs"] == {
        "argv": [],
        "execute_existing_application": True,
    }
    assert feedback.close_count == 1
    assert session.close_count == 1


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
