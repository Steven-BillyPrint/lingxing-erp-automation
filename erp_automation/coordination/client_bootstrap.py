"""Windows packaged-client bootstrap for the shared Aliyun controller.

The installed EXE owns this lifecycle.  PowerShell is retained only as the
signed-package updater; it no longer owns the SSH tunnels or starts the ERP
process.
"""

from __future__ import annotations

import json
import hashlib
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

from erp_automation.client_version import CLIENT_VERSION
from erp_automation.runtime_mode import (
    LOCAL_TEST_FORMAL_BASELINE_VERSION_ENVIRONMENT_VARIABLE,
)

from .remote_controller import RemoteBackgroundTaskController


SERVER_HOST = "8.133.172.100"
SERVER_USER = "admin"
SERVER_PORT = 18765
PREFERRED_LOCAL_PORT = 18765
LOCAL_BROWSER_PORT_START = 26000
LOCAL_BROWSER_PORT_END = 46999
CLOUDFLARE_ACCESS_APP_URL = "https://erp-auth.billyprint.net"
CLOUDFLARED_SHA256 = "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841"
UPDATE_MANIFEST_URL = (
    "https://github.com/Steven-BillyPrint/lingxing-erp-automation/"
    "releases/latest/download/latest.json"
)
_VERSION_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d+$")
_CLOUDFLARE_APP_INFO_RETRY_ATTEMPTS = 5
_CLOUDFLARE_APP_INFO_TRANSIENT_MARKERS = (
    "failed to get app info",
    "context deadline exceeded",
    "client.timeout exceeded",
    "connection reset",
    "i/o timeout",
)


class PackagedClientBootstrapError(RuntimeError):
    """The packaged client could not establish its authenticated session."""


class CloudflareAccessLoginRequired(PackagedClientBootstrapError):
    """No unexpired cached Cloudflare Access session is available."""


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
    client_version: str
    cloudflared: Path | None = None


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
    require_access_files: bool = True,
    embedded_version: str | None = None,
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
    version_file = program_root / "VERSION.txt"
    cloudflared_candidates = (
        executable_path.parent / "_internal" / "tools" / "cloudflared.exe",
        program_root / "tools" / "cloudflared.exe",
    )
    cloudflared = next(
        (candidate for candidate in cloudflared_candidates if candidate.is_file()),
        cloudflared_candidates[0],
    )
    paths = PackagedClientPaths(
        executable=executable_path,
        program_root=program_root,
        version_file=version_file,
        updater_script=program_root / "scripts" / "update_shared_client.ps1",
        powershell=powershell,
        ssh=ssh,
        state_root=state_root,
        ssh_key=state_root / "server-tunnel-ed25519",
        known_hosts=state_root / "known_hosts",
        token_file=state_root / "coordination-token",
        cloudflared=cloudflared,
        browser_profile=state_root / "browser-profile",
        client_version=str(
            embedded_version
            if embedded_version is not None
            else CLIENT_VERSION
        ).strip(),
    )
    required = {
        "客户端 EXE": paths.executable,
        "更新器": paths.updater_script,
        "Windows PowerShell": paths.powershell,
        "Windows OpenSSH": paths.ssh,
        "Cloudflare 登录组件": paths.cloudflared,
    }
    if require_access_files:
        required.update(
            {
                "SSH 私钥": paths.ssh_key,
                "服务器主机指纹": paths.known_hosts,
                "协调服务凭据": paths.token_file,
            }
        )
    missing = [f"{label}：{path}" for label, path in required.items() if not path.is_file()]
    if missing:
        raise PackagedClientBootstrapError(
            "客户端缺少启动所需文件：\n" + "\n".join(missing)
        )
    return paths


def resolve_installed_formal_client_executable(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the newest complete formally installed client executable."""

    environment = os.environ if environ is None else environ
    local_appdata_value = str(environment.get("LOCALAPPDATA") or "").strip()
    if not local_appdata_value:
        raise PackagedClientBootstrapError("Windows LOCALAPPDATA 不可用。")
    program_base = Path(local_appdata_value) / "Programs" / "LingxingERP"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    if program_base.is_dir():
        for version_root in program_base.iterdir():
            if not version_root.is_dir() or not _VERSION_PATTERN.fullmatch(
                version_root.name
            ):
                continue
            version_file = version_root / "VERSION.txt"
            executable = (
                version_root / "dist" / "ERP自动化" / "ERP自动化.exe"
            )
            if not version_file.is_file() or not executable.is_file():
                continue
            try:
                recorded_version = version_file.read_text(
                    encoding="utf-8-sig"
                ).strip()
            except OSError:
                continue
            if recorded_version != version_root.name:
                continue
            candidates.append(
                (
                    tuple(int(part) for part in version_root.name.split(".")),
                    executable.resolve(),
                )
            )
    if not candidates:
        raise PackagedClientBootstrapError(
            "没有找到可复用访问配置的正式客户端安装。"
        )
    return max(candidates, key=lambda item: item[0])[1]


def resolve_local_test_formal_client_paths(
    *,
    environ: Mapping[str, str] | None = None,
) -> PackagedClientPaths:
    """Use formal access tooling and its server-compatible registration version."""

    environment = os.environ if environ is None else environ
    executable = resolve_installed_formal_client_executable(environ=environment)
    formal_version = executable.parents[2].name
    return resolve_packaged_client_paths(
        executable,
        environ=environment,
        require_access_files=False,
        embedded_version=formal_version,
    )


def missing_client_access_files(
    paths: PackagedClientPaths,
) -> tuple[tuple[str, Path], ...]:
    required = (
        ("SSH 私钥", paths.ssh_key),
        ("服务器主机指纹", paths.known_hosts),
        ("协调服务凭据", paths.token_file),
    )
    return tuple((label, path) for label, path in required if not path.is_file())


def read_client_version(paths: PackagedClientPaths) -> str:
    version = str(paths.client_version or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise PackagedClientBootstrapError(f"EXE 内置客户端版本号无效：{version}")
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
    """Check and install a verified release, returning its machine-readable result."""

    command = [
        str(paths.powershell),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(paths.updater_script),
        "-CurrentVersion",
        read_client_version(paths),
        "-CurrentPackageRoot",
        str(paths.program_root),
        "-CurrentProcessId",
        str(os.getpid()),
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
    allowed = {
        "current",
        "current_cached",
        "user_exit",
        "updated",
        "repair_scheduled",
    }
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


def _operator_browser_local_port(operator_email: str) -> int:
    """Return one stable local CDP port for the operator on this Windows PC."""

    normalized = str(operator_email or "").strip().casefold()
    if not normalized.endswith("@billyprint.com"):
        raise PackagedClientBootstrapError("无法为无效的企业邮箱分配浏览器端口。")
    span = LOCAL_BROWSER_PORT_END - LOCAL_BROWSER_PORT_START + 1
    digest = hashlib.sha256(
        f"lingxing-erp-browser:{normalized}".encode("utf-8")
    ).digest()
    return LOCAL_BROWSER_PORT_START + int.from_bytes(digest[:4], "big") % span


def _debugging_endpoint_healthy(port: int) -> bool:
    try:
        response = httpx.get(
            f"http://127.0.0.1:{int(port)}/json/version",
            timeout=0.5,
        )
        return (
            response.status_code == 200
            and "webSocketDebuggerUrl" in response.json()
        )
    except (httpx.HTTPError, TypeError, ValueError):
        return False


def _assert_local_browser_port_reusable(port: int) -> None:
    """Allow a free port or the healthy Chrome shared by another ERP window."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
            return
        except OSError as exc:
            if _debugging_endpoint_healthy(port):
                return
            raise PackagedClientBootstrapError(
                f"本机专用 Chrome 端口 {port} 已被其他程序占用。"
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


def _verify_cloudflared_binary(path: Path) -> None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PackagedClientBootstrapError(
            "Cloudflare login component could not be verified."
        ) from exc
    actual = digest.hexdigest()
    if actual != CLOUDFLARED_SHA256:
        raise PackagedClientBootstrapError(
            "Cloudflare login component failed integrity verification. "
            "Please reinstall the latest official client."
        )


def _validated_cloudflare_jwt(value: bytes | str) -> str:
    token = (
        _decode_process_output(value).strip()
        if isinstance(value, bytes)
        else str(value).strip()
    )
    if (
        len(token) > 32 * 1024
        or token.count(".") != 2
        or any(not segment for segment in token.split("."))
    ):
        raise CloudflareAccessLoginRequired(
            "企业邮箱登录会话不存在或已经过期。"
        )
    return token


def read_cached_cloudflare_access_token(
    paths: PackagedClientPaths,
    *,
    app_url: str = CLOUDFLARE_ACCESS_APP_URL,
    status_callback: Callable[[str], None] | None = None,
    process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    timeout_seconds: float = 30.0,
    retry_attempts: int = _CLOUDFLARE_APP_INFO_RETRY_ATTEMPTS,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Read an unexpired cached Access JWT without ever opening a browser."""

    cloudflared = paths.cloudflared
    if cloudflared is None or not cloudflared.is_file():
        raise PackagedClientBootstrapError("客户端缺少 Cloudflare 登录组件。")
    _verify_cloudflared_binary(cloudflared)
    status = status_callback or (lambda _message: None)
    status("正在读取已保存的企业邮箱登录会话……")
    attempts = max(1, min(int(retry_attempts), 10))
    command = [
        str(cloudflared),
        "access",
        "token",
        "--app",
        str(app_url).strip(),
    ]
    for attempt in range(1, attempts + 1):
        process = process_factory(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            creationflags=_hidden_creation_flags() | _new_process_group_flag(),
        )
        deadline = time.monotonic() + max(5.0, float(timeout_seconds))
        while process.poll() is None and time.monotonic() < deadline:
            status("正在读取已保存的企业邮箱登录会话……")
            time.sleep(0.1)
        if process.poll() is None:
            _stop_process(process)
            raise PackagedClientBootstrapError(
                "读取企业邮箱登录会话超时，请检查网络后重试。"
            )
        stdout, stderr = process.communicate()
        if process.returncode == 0 and _decode_process_output(stdout).strip():
            return _validated_cloudflare_jwt(stdout)

        detail = _decode_process_output(stderr).strip().casefold()
        transient_app_info_failure = any(
            marker in detail
            for marker in _CLOUDFLARE_APP_INFO_TRANSIENT_MARKERS
        )
        if transient_app_info_failure and attempt < attempts:
            status(
                "Cloudflare 登录入口暂时无响应，"
                f"正在自动重试（{attempt + 1}/{attempts}）……"
            )
            retry_sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))
            continue
        if transient_app_info_failure:
            raise PackagedClientBootstrapError(
                "Cloudflare 登录入口暂时无法连接，"
                f"已自动重试 {attempts} 次。请检查网络后重试。"
            )
        raise CloudflareAccessLoginRequired(
            "企业邮箱登录会话不存在或已经过期。"
        )
    raise AssertionError("Cloudflare cached-token retry loop ended unexpectedly.")


def obtain_cloudflare_access_token(
    paths: PackagedClientPaths,
    *,
    app_url: str = CLOUDFLARE_ACCESS_APP_URL,
    status_callback: Callable[[str], None] | None = None,
    process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    timeout_seconds: float = 5 * 60,
    retry_attempts: int = _CLOUDFLARE_APP_INFO_RETRY_ATTEMPTS,
    retry_sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Open the browser after explicit consent and return the Access JWT."""

    cloudflared = paths.cloudflared
    if cloudflared is None or not cloudflared.is_file():
        raise PackagedClientBootstrapError("客户端缺少 Cloudflare 登录组件。")
    _verify_cloudflared_binary(cloudflared)
    status = status_callback or (lambda _message: None)
    status(
        "正在确认个人 @billyprint.com 企业身份；"
        "已有有效登录会话会直接复用，否则将打开浏览器验证码登录……"
    )
    attempts = max(1, min(int(retry_attempts), 10))
    command = [
        str(cloudflared),
        "access",
        "login",
        "--app",
        str(app_url).strip(),
        "--no-verbose",
        "--auto-close",
    ]
    for attempt in range(1, attempts + 1):
        process = process_factory(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            creationflags=_hidden_creation_flags() | _new_process_group_flag(),
        )
        deadline = time.monotonic() + max(30.0, float(timeout_seconds))
        while process.poll() is None and time.monotonic() < deadline:
            status("正在确认企业邮箱登录会话……")
            time.sleep(0.2)
        if process.poll() is None:
            _stop_process(process)
            raise PackagedClientBootstrapError(
                "企业邮箱登录超时，请重新打开客户端。"
            )
        stdout, stderr = process.communicate()
        if process.returncode == 0:
            try:
                return _validated_cloudflare_jwt(stdout)
            except CloudflareAccessLoginRequired as exc:
                raise PackagedClientBootstrapError(
                    "Cloudflare 企业邮箱登录未返回有效凭据。"
                ) from exc

        detail = _decode_process_output(stderr).strip().casefold()
        transient_app_info_failure = any(
            marker in detail
            for marker in _CLOUDFLARE_APP_INFO_TRANSIENT_MARKERS
        )
        if transient_app_info_failure and attempt < attempts:
            status(
                "Cloudflare 登录入口暂时无响应，"
                f"正在自动重试（{attempt + 1}/{attempts}）……"
            )
            retry_sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))
            continue
        if transient_app_info_failure:
            raise PackagedClientBootstrapError(
                "Cloudflare 登录入口暂时无法连接，"
                f"已自动重试 {attempts} 次。请检查网络后重试。"
            )
        raise PackagedClientBootstrapError(
            "Cloudflare 企业邮箱登录失败，请重新打开客户端重试。"
        )
    raise AssertionError("Cloudflare login retry loop ended unexpectedly.")


def bootstrap_packaged_shared_client(
    *,
    instance_name: str = "",
    status_callback: Callable[[str], None] | None = None,
    access_setup_callback: Callable[[PackagedClientPaths], bool] | None = None,
    access_login_callback: Callable[[str], bool] | None = None,
    _paths: PackagedClientPaths | None = None,
    _check_for_updates: bool = True,
) -> PackagedClientBootstrapOutcome:
    """Update, connect, allocate the local browser, and register this EXE."""

    status = status_callback or (lambda _message: None)
    normalized_name = (
        str(instance_name or "").strip()
        or str(os.environ.get("USERNAME") or "").strip()
        or socket.gethostname()
    )
    paths = _paths or resolve_packaged_client_paths(require_access_files=False)
    # The client package and its signed hashes are public release artifacts.
    # Converge on the current program before showing any access/setup UI so a
    # freshly downloaded older installer cannot keep presenting obsolete
    # authorization or security logic. Company data remains inaccessible until
    # the separate local access profile below is complete.
    if _check_for_updates:
        status("正在检查客户端更新…")
        update = run_client_update(
            paths,
            progress_callback=lambda: status("正在检查客户端更新…"),
        )
        if update.status in {"user_exit", "repair_scheduled"}:
            return PackagedClientBootstrapOutcome(should_exit=True)
        if update.status == "updated":
            if update.application_path is None:
                raise PackagedClientBootstrapError("更新结果缺少新版本 EXE。")
            start_updated_client(update.application_path)
            return PackagedClientBootstrapOutcome(should_exit=True)

    missing_access = missing_client_access_files(paths)
    if missing_access:
        status("当前电脑尚未授权，等待导入客户端授权文件…")
        if access_setup_callback is None or not access_setup_callback(paths):
            missing_text = "\n".join(
                f"{label}：{path}" for label, path in missing_access
            )
            raise PackagedClientBootstrapError(
                "当前电脑尚未获得公司系统访问授权。\n" + missing_text
            )
        paths = resolve_packaged_client_paths(require_access_files=True)

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

        try:
            access_token = read_cached_cloudflare_access_token(
                paths,
                status_callback=status,
            )
        except CloudflareAccessLoginRequired as exc:
            if access_login_callback is None or not access_login_callback(str(exc)):
                raise PackagedClientBootstrapError(
                    "企业邮箱登录尚未完成；程序没有打开任何网页。"
                ) from exc
            access_token = obtain_cloudflare_access_token(
                paths,
                status_callback=status,
            )
        status("正在注册本机操作实例…")
        instance_id = uuid4().hex
        with httpx.Client(
            base_url=server_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Cf-Access-Token": access_token,
            },
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
        browser_remote_port = int(allocation.get("browser_port") or 0)
        browser_endpoint = str(
            allocation.get("browser_endpoint") or ""
        ).strip()
        operator_payload = allocation.get("operator")
        operator_email = (
            str(operator_payload.get("email") or "").strip().casefold()
            if isinstance(operator_payload, Mapping)
            else ""
        )
        if not operator_email.endswith("@billyprint.com"):
            raise PackagedClientBootstrapError(
                "服务器没有返回有效的企业邮箱身份。"
            )
        operator_browser_profile = (
            paths.browser_profile
            / hashlib.sha256(operator_email.encode("utf-8")).hexdigest()[:32]
        )
        if browser_remote_port <= 0 or not browser_endpoint:
            raise PackagedClientBootstrapError("服务器返回了无效的浏览器通道。")
        browser_local_port = _operator_browser_local_port(operator_email)
        _assert_local_browser_port_reusable(browser_local_port)

        status("正在准备本机网页操作环境…")
        browser_command = build_ssh_tunnel_command(paths, forward="-R")
        browser_command.extend(
            [
                (
                    f"127.0.0.1:{browser_remote_port}:"
                    f"127.0.0.1:{browser_local_port}"
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
            browser_local_port=browser_local_port,
            browser_profile_dir=operator_browser_profile,
            strict_registration=True,
            access_token=access_token,
            access_token_provider=lambda: obtain_cloudflare_access_token(paths),
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


def bootstrap_local_test_shared_client(
    *,
    instance_name: str = "",
    status_callback: Callable[[str], None] | None = None,
    access_login_callback: Callable[[str], bool] | None = None,
) -> PackagedClientBootstrapOutcome:
    """Connect source code with the formal access profile without updating EXEs."""

    paths = resolve_local_test_formal_client_paths()
    previous_baseline = os.environ.get(
        LOCAL_TEST_FORMAL_BASELINE_VERSION_ENVIRONMENT_VARIABLE
    )
    os.environ[
        LOCAL_TEST_FORMAL_BASELINE_VERSION_ENVIRONMENT_VARIABLE
    ] = paths.client_version
    try:
        return bootstrap_packaged_shared_client(
            instance_name=instance_name,
            status_callback=status_callback,
            access_setup_callback=None,
            access_login_callback=access_login_callback,
            _paths=paths,
            _check_for_updates=False,
        )
    except Exception:
        if previous_baseline is None:
            os.environ.pop(
                LOCAL_TEST_FORMAL_BASELINE_VERSION_ENVIRONMENT_VARIABLE,
                None,
            )
        else:
            os.environ[
                LOCAL_TEST_FORMAL_BASELINE_VERSION_ENVIRONMENT_VARIABLE
            ] = previous_baseline
        raise


__all__ = [
    "ClientUpdateResult",
    "CloudflareAccessLoginRequired",
    "PackagedClientBootstrapError",
    "PackagedClientBootstrapOutcome",
    "PackagedClientPaths",
    "PackagedClientSession",
    "bootstrap_local_test_shared_client",
    "bootstrap_packaged_shared_client",
    "build_ssh_tunnel_command",
    "missing_client_access_files",
    "obtain_cloudflare_access_token",
    "read_cached_cloudflare_access_token",
    "read_client_version",
    "resolve_installed_formal_client_executable",
    "resolve_local_test_formal_client_paths",
    "resolve_packaged_client_paths",
    "run_client_update",
    "should_bootstrap_packaged_shared_client",
    "start_updated_client",
]
