"""Windows packaged-client bootstrap for the shared Aliyun controller.

The installed EXE owns this lifecycle.  PowerShell is retained only as the
signed-package updater; it no longer owns the SSH tunnels or starts the ERP
process.
"""

from __future__ import annotations

import json
import locale
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .remote_controller import RemoteBackgroundTaskController


SERVER_HOST = "8.133.172.100"
SERVER_USER = "admin"
SERVER_PORT = 18765
PREFERRED_LOCAL_PORT = 18765
UPDATE_MANIFEST_URL = (
    "https://github.com/Steven-BillyPrint/lingxing-erp-automation/"
    "releases/latest/download/latest.json"
)
_VERSION_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")


class PackagedClientBootstrapError(RuntimeError):
    """The packaged client could not establish its authenticated session."""


@dataclass(frozen=True)
class PackagedClientPaths:
    executable: Path
    program_root: Path
    version_file: Path
    updater_script: Path
    powershell: Path
    ssh: Path
    state_root: Path
    ssh_key: Path
    known_hosts: Path
    token_file: Path
    browser_profile: Path


@dataclass(frozen=True)
class ClientUpdateResult:
    status: str
    current_version: str
    latest_version: str
    application_path: Path | None = None


@dataclass
class PackagedClientSession:
    controller: RemoteBackgroundTaskController
    tunnel_processes: tuple[subprocess.Popen[bytes], ...]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.controller.prepare_close()
        finally:
            for process in reversed(self.tunnel_processes):
                _stop_process(process)


@dataclass(frozen=True)
class PackagedClientBootstrapOutcome:
    session: PackagedClientSession | None = None
    should_exit: bool = False


def should_bootstrap_packaged_shared_client(
    *,
    frozen: bool | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether this process must create its own shared-server session."""

    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    current_platform = sys.platform if platform is None else platform
    environment = os.environ if environ is None else environ
    return bool(
        is_frozen
        and current_platform == "win32"
        and not str(environment.get("ERP_AUTOMATION_SERVER_URL") or "").strip()
    )


def resolve_packaged_client_paths(
    executable: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> PackagedClientPaths:
    environment = os.environ if environ is None else environ
    executable_path = Path(executable or sys.executable).resolve()
    dist_directory = executable_path.parent.parent
    if dist_directory.name.casefold() != "dist":
        raise PackagedClientBootstrapError(
            "客户端安装结构无效：EXE 不在预期的 dist 目录中。"
        )
    program_root = dist_directory.parent

    local_appdata_value = str(environment.get("LOCALAPPDATA") or "").strip()
    system_root_value = str(
        environment.get("SystemRoot") or environment.get("WINDIR") or ""
    ).strip()
    if not local_appdata_value:
        raise PackagedClientBootstrapError("Windows LOCALAPPDATA 不可用。")
    if not system_root_value:
        raise PackagedClientBootstrapError("Windows SystemRoot 不可用。")

    system_root = Path(system_root_value)
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    ssh_candidate = shutil.which(
        "ssh.exe",
        path=str(environment.get("PATH") or ""),
    )
    ssh = (
        Path(ssh_candidate)
        if ssh_candidate
        else system_root / "System32" / "OpenSSH" / "ssh.exe"
    )
    state_root = Path(local_appdata_value) / "LingxingERP"
    paths = PackagedClientPaths(
        executable=executable_path,
        program_root=program_root,
        version_file=program_root / "VERSION.txt",
        updater_script=program_root / "scripts" / "update_shared_client.ps1",
        powershell=powershell,
        ssh=ssh,
        state_root=state_root,
        ssh_key=state_root / "server-tunnel-ed25519",
        known_hosts=state_root / "known_hosts",
        token_file=state_root / "coordination-token",
        browser_profile=state_root / "browser-profile",
    )
    required = {
        "客户端 EXE": paths.executable,
        "版本文件": paths.version_file,
        "更新器": paths.updater_script,
        "Windows PowerShell": paths.powershell,
        "Windows OpenSSH": paths.ssh,
        "SSH 私钥": paths.ssh_key,
        "服务器主机指纹": paths.known_hosts,
        "协调服务凭据": paths.token_file,
    }
    missing = [f"{label}：{path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise PackagedClientBootstrapError(
            "客户端缺少启动所需文件：\n" + "\n".join(missing)
        )
    return paths


def read_client_version(paths: PackagedClientPaths) -> str:
    version = paths.version_file.read_text(encoding="utf-8-sig").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise PackagedClientBootstrapError(f"客户端版本号无效：{version}")
    return version


def _decode_process_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    encodings = ("utf-8-sig", locale.getpreferredencoding(False), "gb18030")
    for encoding in dict.fromkeys(encodings):
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def run_client_update(
    paths: PackagedClientPaths,
    *,
    manifest_url: str = UPDATE_MANIFEST_URL,
    runner: Callable[..., Any] | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> ClientUpdateResult:
    """Check/install a signed release and return the machine-readable result."""

    command = [
        str(paths.powershell),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(paths.updater_script),
        "-CurrentVersionFile",
        str(paths.version_file),
        "-ManifestUrl",
        manifest_url,
        "-StateRoot",
        str(paths.state_root),
        "-OutputJson",
    ]
    process_options = {
        "cwd": str(paths.program_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "creationflags": _hidden_creation_flags(),
    }
    if runner is not None:
        completed = runner(command, check=False, **process_options)
    else:
        process = subprocess.Popen(command, **process_options)
        while process.poll() is None:
            if progress_callback is not None:
                progress_callback()
            time.sleep(0.05)
        stdout_value, stderr_value = process.communicate()
        completed = subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout_value,
            stderr=stderr_value,
        )
    stdout = _decode_process_output(completed.stdout).strip()
    stderr = _decode_process_output(completed.stderr).strip()
    if completed.returncode:
        detail = stderr or stdout or f"退出代码 {completed.returncode}"
        raise PackagedClientBootstrapError(f"客户端更新检查失败：{detail}")

    payload: dict[str, Any] | None = None
    for line in reversed(stdout.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise PackagedClientBootstrapError("客户端更新器没有返回有效结果。")
    status = str(payload.get("status") or "").strip()
    allowed = {"current", "current_cached", "current_newer", "user_exit", "updated"}
    if status not in allowed:
        raise PackagedClientBootstrapError(f"客户端更新状态无效：{status or '空'}")
    application_value = str(payload.get("application_path") or "").strip()
    application_path = Path(application_value).resolve() if application_value else None
    if status == "updated" and (
        application_path is None or not application_path.is_file()
    ):
        raise PackagedClientBootstrapError("更新完成后找不到新版本 EXE。")
    if status == "updated" and application_path is not None:
        installed_programs = (
            paths.state_root.parent / "Programs" / "LingxingERP"
        ).resolve()
        try:
            application_path.relative_to(installed_programs)
        except ValueError as exc:
            raise PackagedClientBootstrapError(
                "更新器返回的新版本 EXE 不在受控安装目录中。"
            ) from exc
    return ClientUpdateResult(
        status=status,
        current_version=str(payload.get("current_version") or "").strip(),
        latest_version=str(payload.get("latest_version") or "").strip(),
        application_path=application_path,
    )


def start_updated_client(application_path: Path) -> None:
    """Start the newly installed EXE without shortcut-only configuration."""

    command = [str(application_path)]
    subprocess.Popen(
        command,
        cwd=str(application_path.parents[2]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_new_process_group_flag(),
    )


def build_ssh_tunnel_command(
    paths: PackagedClientPaths,
    *,
    forward: str,
) -> list[str]:
    if forward not in {"-L", "-R"}:
        raise ValueError("SSH forwarding mode must be -L or -R.")
    return [
        str(paths.ssh),
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={paths.known_hosts}",
        "-i",
        str(paths.ssh_key),
        forward,
    ]


def _start_tunnel(command: Sequence[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_hidden_creation_flags() | _new_process_group_flag(),
    )


def _available_loopback_port(preferred: int = PREFERRED_LOCAL_PORT) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(listener.getsockname()[1])
    raise PackagedClientBootstrapError("本机没有可用的 SSH 转发端口。")


def _assert_loopback_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            raise PackagedClientBootstrapError(
                f"本机浏览器转发端口 {port} 已被占用。"
            ) from exc


def _wait_for_server(
    process: subprocess.Popen[bytes],
    server_url: str,
    *,
    progress_callback: Callable[[], None] | None = None,
) -> None:
    deadline = time.monotonic() + 15.0
    last_error = ""
    with httpx.Client(base_url=server_url, timeout=2.0) as client:
        while time.monotonic() < deadline:
            if progress_callback is not None:
                progress_callback()
            exit_code = process.poll()
            if exit_code is not None:
                raise PackagedClientBootstrapError(
                    f"SSH 服务隧道已退出（代码 {exit_code}）。"
                )
            try:
                response = client.get("/health")
                response.raise_for_status()
                if response.json().get("ok") is True:
                    return
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
            time.sleep(0.2)
    suffix = f"（{last_error}）" if last_error else ""
    raise PackagedClientBootstrapError(
        f"无法通过 SSH 隧道连接阿里云共享服务{suffix}"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _hidden_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _new_process_group_flag() -> int:
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def bootstrap_packaged_shared_client(
    *,
    instance_name: str = "",
    status_callback: Callable[[str], None] | None = None,
) -> PackagedClientBootstrapOutcome:
    """Update, connect, allocate the local browser, and register this EXE."""

    status = status_callback or (lambda _message: None)
    normalized_name = (
        str(instance_name or "").strip()
        or str(os.environ.get("USERNAME") or "").strip()
        or socket.gethostname()
    )
    paths = resolve_packaged_client_paths()
    status("正在检查客户端更新…")
    update = run_client_update(
        paths,
        progress_callback=lambda: status("正在检查客户端更新…"),
    )
    if update.status == "user_exit":
        return PackagedClientBootstrapOutcome(should_exit=True)
    if update.status == "updated":
        if update.application_path is None:
            raise PackagedClientBootstrapError("更新结果缺少新版本 EXE。")
        start_updated_client(update.application_path)
        return PackagedClientBootstrapOutcome(should_exit=True)

    version = read_client_version(paths)
    token = paths.token_file.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32:
        raise PackagedClientBootstrapError("协调服务凭据为空或无效。")

    tunnel_processes: list[subprocess.Popen[bytes]] = []
    controller: RemoteBackgroundTaskController | None = None
    try:
        status("正在连接阿里云共享服务…")
        local_port = _available_loopback_port()
        server_url = f"http://127.0.0.1:{local_port}"
        api_command = build_ssh_tunnel_command(paths, forward="-L")
        api_command.extend(
            [
                f"127.0.0.1:{local_port}:127.0.0.1:{SERVER_PORT}",
                f"{SERVER_USER}@{SERVER_HOST}",
            ]
        )
        api_tunnel = _start_tunnel(api_command)
        tunnel_processes.append(api_tunnel)
        _wait_for_server(
            api_tunnel,
            server_url,
            progress_callback=lambda: status("正在连接阿里云共享服务…"),
        )

        status("正在注册本机操作实例…")
        instance_id = uuid4().hex
        with httpx.Client(
            base_url=server_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        ) as client:
            response = client.post(
                "/v1/instances/browser-endpoint",
                json={
                    "instance_id": instance_id,
                    "display_name": normalized_name,
                    "client_version": version,
                },
            )
            if response.status_code == 426:
                required = str(
                    response.json().get("required_version") or "最新版本"
                ).strip()
                raise PackagedClientBootstrapError(
                    f"客户端必须更新到 {required} 后才能连接共享服务。"
                )
            response.raise_for_status()
            allocation = response.json()
        if allocation.get("ok") is not True:
            raise PackagedClientBootstrapError("服务器拒绝分配本机浏览器通道。")
        browser_port = int(allocation.get("browser_port") or 0)
        browser_endpoint = str(
            allocation.get("browser_endpoint") or ""
        ).strip()
        if browser_port <= 0 or not browser_endpoint:
            raise PackagedClientBootstrapError("服务器返回了无效的浏览器通道。")
        _assert_loopback_port_available(browser_port)

        status("正在准备本机网页操作环境…")
        browser_command = build_ssh_tunnel_command(paths, forward="-R")
        browser_command.extend(
            [
                (
                    f"127.0.0.1:{browser_port}:"
                    f"127.0.0.1:{browser_port}"
                ),
                f"{SERVER_USER}@{SERVER_HOST}",
            ]
        )
        browser_tunnel = _start_tunnel(browser_command)
        tunnel_processes.append(browser_tunnel)
        browser_deadline = time.monotonic() + 0.5
        while time.monotonic() < browser_deadline:
            status("正在准备本机网页操作环境…")
            time.sleep(0.05)
        if browser_tunnel.poll() is not None:
            raise PackagedClientBootstrapError(
                f"本机浏览器隧道已退出（代码 {browser_tunnel.returncode}）。"
            )

        status("连接成功，正在加载 ERP 控制台…")
        controller = RemoteBackgroundTaskController(
            server_url,
            token=token,
            display_name=normalized_name,
            instance_id=instance_id,
            client_version=version,
            timeout_seconds=5.0,
            browser_endpoint=browser_endpoint,
            browser_local_port=browser_port,
            browser_profile_dir=paths.browser_profile,
            strict_registration=True,
        )
        return PackagedClientBootstrapOutcome(
            session=PackagedClientSession(
                controller=controller,
                tunnel_processes=tuple(tunnel_processes),
            )
        )
    except Exception:
        if controller is not None:
            controller.prepare_close()
        for process in reversed(tunnel_processes):
            _stop_process(process)
        raise


__all__ = [
    "ClientUpdateResult",
    "PackagedClientBootstrapError",
    "PackagedClientBootstrapOutcome",
    "PackagedClientPaths",
    "PackagedClientSession",
    "bootstrap_packaged_shared_client",
    "build_ssh_tunnel_command",
    "read_client_version",
    "resolve_packaged_client_paths",
    "run_client_update",
    "should_bootstrap_packaged_shared_client",
    "start_updated_client",
]
