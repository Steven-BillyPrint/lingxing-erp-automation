"""Operator-visible local Chrome host for server-coordinated browser tasks."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

ALIBABA_SCM_HOME_URL = "https://scm.alibaba.com/"


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
    if (
        parsed.scheme != "https"
        or str(parsed.hostname or "").casefold() != "scm.alibaba.com"
        or parsed.username
        or parsed.password
    ):
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
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _healthy(self) -> bool:
        try:
            response = httpx.get(f"{self.endpoint}/json/version", timeout=0.5)
            return response.status_code == 200 and "webSocketDebuggerUrl" in response.json()
        except (httpx.HTTPError, TypeError, ValueError):
            return False

    def ensure_started(self, *, initial_url: str = "about:blank") -> bool:
        target_url = _safe_start_url(initial_url)
        if self._healthy():
            return False
        if self.executable is None or not self.executable.is_file():
            raise LocalBrowserUnavailable(
                "没有找到 Google Chrome，无法打开本机可见网页。请先安装 Chrome。"
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt"
            else 0
        )
        self._process = subprocess.Popen(
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
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            if self._healthy():
                return True
            time.sleep(0.1)
        raise LocalBrowserUnavailable(
            "本机 Chrome 已启动但调试通道没有就绪，请关闭专用 Chrome 后重试。"
        )

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
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
