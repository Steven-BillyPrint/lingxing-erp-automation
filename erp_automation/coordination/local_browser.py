"""Operator-visible local Chrome host for server-coordinated browser tasks."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

ALIBABA_SCM_HOME_URL = "https://scm.alibaba.com/"
ALIBABA_QUOTE_URL = "https://i.alibaba.com/logistics/web/shipping/query"


class LocalBrowserUnavailable(RuntimeError):
    """The desktop could not provide a visible Chrome debugging endpoint."""


def _find_chrome_executable() -> Path | None:
    candidates = (
        Path(os.environ.get("PROGRAMFILES") or "")
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)") or "")
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA") or "")
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def _safe_start_url(value: str) -> str:
    normalized = str(value or "").strip() or "about:blank"
    if normalized == "about:blank":
        return normalized
    parsed = urlparse(normalized)
    hostname = str(parsed.hostname or "").casefold()
    allowed = hostname == "scm.alibaba.com" or (
        hostname == "i.alibaba.com"
        and parsed.path == "/logistics/web/shipping/query"
        and not parsed.query
        and not parsed.fragment
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise ValueError("本机物流浏览器只允许打开阿里国际站 SCM 页面。")
    return normalized


class LocalChromeHost:
    """Start one dedicated, visible Chrome profile on an allocated loopback port."""

    def __init__(
        self,
        port: int,
        profile_dir: str | Path,
        *,
        executable: str | Path | None = None,
        startup_timeout_seconds: float = 15.0,
        startup_failure_cooldown_seconds: float = 30.0,
    ) -> None:
        if not 1024 <= int(port) <= 65535:
            raise ValueError("Local browser port is invalid.")
        self.port = int(port)
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.executable = (
            Path(executable).expanduser().resolve()
            if executable
            else _find_chrome_executable()
        )
        self.startup_timeout_seconds = max(3.0, float(startup_timeout_seconds))
        self.startup_failure_cooldown_seconds = max(
            1.0,
            float(startup_failure_cooldown_seconds),
        )
        self._process: subprocess.Popen[bytes] | None = None
        self._start_lock = threading.RLock()
        self._last_start_failure_at = 0.0
        self._last_start_failure_message = ""

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self) -> bool:
        try:
            response = httpx.get(f"{self.endpoint}/json/version", timeout=0.5)
            return response.status_code == 200 and "webSocketDebuggerUrl" in response.json()
        except (httpx.HTTPError, TypeError, ValueError):
            return False

    def _clear_start_failure(self) -> None:
        self._last_start_failure_at = 0.0
        self._last_start_failure_message = ""

    def _remember_start_failure(self, message: str) -> LocalBrowserUnavailable:
        self._last_start_failure_at = time.monotonic()
        self._last_start_failure_message = message
        return LocalBrowserUnavailable(message)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def ensure_started(self, *, initial_url: str = "about:blank") -> bool:
        target_url = _safe_start_url(initial_url)
        with self._start_lock:
            if self._healthy():
                self._clear_start_failure()
                return False
            now = time.monotonic()
            if (
                self._last_start_failure_message
                and now - self._last_start_failure_at
                < self.startup_failure_cooldown_seconds
            ):
                raise LocalBrowserUnavailable(self._last_start_failure_message)
            if self.executable is None or not self.executable.is_file():
                raise self._remember_start_failure(
                    "没有找到 Google Chrome，无法打开本机可见网页。请先安装 Chrome。"
                )

            existing_process = self._process
            process_is_running = (
                existing_process is not None
                and existing_process.poll() is None
            )
            launched_here = not process_is_running
            if launched_here:
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                creationflags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                )
                try:
                    existing_process = subprocess.Popen(
                        [
                            str(self.executable),
                            f"--remote-debugging-port={self.port}",
                            f"--user-data-dir={self.profile_dir}",
                            "--remote-allow-origins=*",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--new-window",
                            target_url,
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                except OSError as exc:
                    raise self._remember_start_failure(
                        "本机专用 Chrome 启动失败，系统已停止重复启动。"
                        "请检查 Chrome 是否可用后重试。"
                    ) from exc
                self._process = existing_process

            deadline = time.monotonic() + self.startup_timeout_seconds
            launcher_exited_at = 0.0
            while time.monotonic() < deadline:
                # Check health before the launcher process.  Chrome forwards a
                # second launch to the process already owning the profile and
                # exits the second process immediately; the shared endpoint can
                # still become healthy moments later.
                if self._healthy():
                    self._clear_start_failure()
                    return launched_here
                if (
                    existing_process is not None
                    and existing_process.poll() is not None
                ):
                    launcher_exited_at = launcher_exited_at or time.monotonic()
                    if time.monotonic() - launcher_exited_at >= 2.0:
                        break
                time.sleep(0.1)

            if (
                launched_here
                and existing_process is not None
                and existing_process.poll() is None
            ):
                self._stop_process(existing_process)
                self._process = None
            if launcher_exited_at:
                message = (
                    "检测到专用 Chrome 资料正被另一个 ERP 窗口或旧进程占用，"
                    "但它使用的调试端口与当前窗口不同。系统已停止重复打开 Chrome；"
                    "请关闭旧的专用 Chrome 后重试。"
                )
            else:
                message = (
                    "本机专用 Chrome 调试通道未能就绪，系统已停止重复打开 Chrome。"
                    "请关闭专用 Chrome 后重试。"
                )
            raise self._remember_start_failure(message)

    def open_url(self, url: str) -> None:
        """Open or activate one trusted SCM page in the dedicated Chrome."""

        target_url = _safe_start_url(url)
        if self.ensure_started(initial_url=target_url):
            return
        try:
            targets = httpx.get(f"{self.endpoint}/json/list", timeout=1.0).json()
            for target in targets if isinstance(targets, list) else ():
                if (
                    isinstance(target, dict)
                    and str(target.get("type") or "") == "page"
                    and str(target.get("url") or "") == target_url
                    and str(target.get("id") or "")
                ):
                    httpx.get(
                        f"{self.endpoint}/json/activate/{target['id']}",
                        timeout=1.0,
                    ).raise_for_status()
                    return
            response = httpx.put(
                f"{self.endpoint}/json/new?{quote(target_url, safe='')}",
                timeout=2.0,
            )
            response.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise LocalBrowserUnavailable(
                "本机 Chrome 已启动，但无法预先打开阿里物流页面。"
            ) from exc

    def close(self) -> None:
        with self._start_lock:
            process = self._process
            self._process = None
            self._clear_start_failure()
        self._stop_process(process)
