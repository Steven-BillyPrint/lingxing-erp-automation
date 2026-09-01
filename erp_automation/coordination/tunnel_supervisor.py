"""Process supervision for the packaged client's independent SSH tunnels.

This module deliberately knows nothing about the desktop controller or Qt.  It
owns only process lifecycle, bounded reconnect backoff, and diagnostic state.
Consumers receive an immutable health snapshot through ``as_mapping``.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class TunnelProcess(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class SshTunnelSpec:
    key: str
    label: str
    command: tuple[str, ...]
    diagnostic_log: Path | None = None


@dataclass(frozen=True)
class SshTunnelLaneHealth:
    key: str
    label: str
    healthy: bool
    recovering: bool
    restart_count: int
    last_exit_code: int | None = None
    last_error: str = ""

    def as_mapping(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "healthy": self.healthy,
            "recovering": self.recovering,
            "restart_count": self.restart_count,
            "last_exit_code": self.last_exit_code,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class SshTunnelHealth:
    lanes: tuple[SshTunnelLaneHealth, ...]

    @property
    def all_healthy(self) -> bool:
        return bool(self.lanes) and all(lane.healthy for lane in self.lanes)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "all_healthy": self.all_healthy,
            "lanes": {lane.key: lane.as_mapping() for lane in self.lanes},
        }


@dataclass
class _ManagedTunnel:
    spec: SshTunnelSpec
    process: TunnelProcess
    started_at: float
    restart_count: int = 0
    consecutive_failures: int = 0
    next_restart_at: float = 0.0
    exit_reported_for: TunnelProcess | None = None
    last_exit_code: int | None = None
    last_error: str = ""


class SshTunnelSupervisor:
    """Watch and reconnect a fixed set of independent SSH child processes."""

    def __init__(
        self,
        tunnels: Sequence[tuple[SshTunnelSpec, TunnelProcess]],
        *,
        process_factory: Callable[[Sequence[str]], TunnelProcess],
        process_stopper: Callable[[TunnelProcess], None],
        lifecycle_log: Path,
        check_interval_seconds: float = 1.0,
        restart_backoff_seconds: Sequence[float] = (1.0, 2.0, 5.0, 10.0, 30.0),
        stable_after_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not tunnels:
            raise ValueError("At least one SSH tunnel must be supervised.")
        keys = [spec.key for spec, _process in tunnels]
        if len(keys) != len(set(keys)):
            raise ValueError("SSH tunnel keys must be unique.")
        backoff = tuple(max(0.0, float(value)) for value in restart_backoff_seconds)
        if not backoff:
            raise ValueError("SSH tunnel reconnect backoff cannot be empty.")
        now = clock()
        self._tunnels = {
            spec.key: _ManagedTunnel(spec=spec, process=process, started_at=now)
            for spec, process in tunnels
        }
        self._process_factory = process_factory
        self._process_stopper = process_stopper
        self._lifecycle_log = Path(lifecycle_log)
        self._check_interval_seconds = max(0.05, float(check_interval_seconds))
        self._restart_backoff_seconds = backoff
        self._stable_after_seconds = max(0.0, float(stable_after_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def processes(self) -> tuple[TunnelProcess, ...]:
        with self._lock:
            return tuple(item.process for item in self._tunnels.values())

    def start(self) -> None:
        with self._lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._watch_loop,
                name="erp-ssh-tunnel-watchdog",
                daemon=True,
            )
            self._thread.start()
        self._append_event("watchdog_started", message="SSH 通道守护已启动。")

    def stop_monitoring(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self._check_interval_seconds * 2))
        with self._lock:
            self._thread = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop_monitoring()
        for process in reversed(self.processes):
            self._process_stopper(process)
        self._append_event("watchdog_stopped", message="SSH 通道守护已停止。")

    def snapshot(self) -> SshTunnelHealth:
        with self._lock:
            lanes = tuple(
                SshTunnelLaneHealth(
                    key=item.spec.key,
                    label=item.spec.label,
                    healthy=item.process.poll() is None,
                    recovering=(
                        item.process.poll() is not None and not self._closed
                    ),
                    restart_count=item.restart_count,
                    last_exit_code=item.last_exit_code,
                    last_error=item.last_error,
                )
                for item in self._tunnels.values()
            )
        return SshTunnelHealth(lanes)

    def poll_once(self, *, now: float | None = None) -> None:
        """Run one deterministic supervision pass (also useful in unit tests)."""

        current = self._clock() if now is None else float(now)
        with self._lock:
            if self._closed:
                return
            for item in self._tunnels.values():
                exit_code = item.process.poll()
                if exit_code is None:
                    if current - item.started_at >= self._stable_after_seconds:
                        item.consecutive_failures = 0
                    continue
                if item.exit_reported_for is not item.process:
                    item.exit_reported_for = item.process
                    item.last_exit_code = int(exit_code)
                    item.last_error = self._diagnostic_reason(item.spec)
                    delay = self._restart_backoff_seconds[
                        min(
                            item.consecutive_failures,
                            len(self._restart_backoff_seconds) - 1,
                        )
                    ]
                    item.next_restart_at = current + delay
                    self._append_event(
                        "tunnel_exited",
                        tunnel=item.spec,
                        exit_code=item.last_exit_code,
                        reason=item.last_error,
                        retry_in_seconds=delay,
                    )
                if current < item.next_restart_at:
                    continue
                try:
                    replacement = self._process_factory(item.spec.command)
                except Exception as exc:
                    item.consecutive_failures += 1
                    item.last_error = f"启动 SSH 进程失败：{type(exc).__name__}。"
                    delay = self._restart_backoff_seconds[
                        min(
                            item.consecutive_failures,
                            len(self._restart_backoff_seconds) - 1,
                        )
                    ]
                    item.next_restart_at = current + delay
                    self._append_event(
                        "tunnel_restart_failed",
                        tunnel=item.spec,
                        reason=item.last_error,
                        retry_in_seconds=delay,
                    )
                    continue
                item.process = replacement
                item.started_at = current
                item.restart_count += 1
                item.consecutive_failures += 1
                item.exit_reported_for = None
                item.next_restart_at = 0.0
                self._append_event(
                    "tunnel_restarted",
                    tunnel=item.spec,
                    restart_count=item.restart_count,
                )

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self._check_interval_seconds):
            self.poll_once()

    @staticmethod
    def _diagnostic_reason(spec: SshTunnelSpec) -> str:
        path = spec.diagnostic_log
        if path is None or not path.is_file():
            return "OpenSSH 未留下额外诊断。"
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 8192))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return "OpenSSH 诊断日志暂时不可读。"
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        return lines[-1][:1000] if lines else "OpenSSH 未留下额外诊断。"

    def _append_event(
        self,
        event: str,
        *,
        tunnel: SshTunnelSpec | None = None,
        message: str = "",
        exit_code: int | None = None,
        reason: str = "",
        retry_in_seconds: float | None = None,
        restart_count: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if tunnel is not None:
            payload.update({"tunnel": tunnel.key, "label": tunnel.label})
        if message:
            payload["message"] = message
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if reason:
            payload["reason"] = reason
        if retry_in_seconds is not None:
            payload["retry_in_seconds"] = retry_in_seconds
        if restart_count is not None:
            payload["restart_count"] = restart_count
        try:
            self._lifecycle_log.parent.mkdir(parents=True, exist_ok=True)
            with self._lifecycle_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            # Diagnostics must never take down the client or its watchdog.
            return


__all__ = [
    "SshTunnelHealth",
    "SshTunnelLaneHealth",
    "SshTunnelSpec",
    "SshTunnelSupervisor",
    "TunnelProcess",
]
