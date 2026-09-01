"""Health probing and short-lived circuit breaking for visible Chrome lanes."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class BrowserEndpointHealth:
    healthy: bool
    message: str = ""
    cached: bool = False


BrowserEndpointProbe = Callable[[str], Awaitable[BrowserEndpointHealth]]


class BrowserEndpointGuard(Protocol):
    async def check(self, endpoint: str) -> BrowserEndpointHealth: ...


class HttpBrowserEndpointProbe:
    """Verify that an endpoint serves a usable Chrome DevTools descriptor."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        attempts: int = 2,
        retry_delay_seconds: float = 0.2,
    ) -> None:
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._attempts = max(1, int(attempts))
        self._retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    async def __call__(self, endpoint: str) -> BrowserEndpointHealth:
        normalized = str(endpoint or "").strip().rstrip("/")
        if not normalized:
            return BrowserEndpointHealth(False, "没有配置可见 Chrome 通道。")
        last_error = ""
        timeout = httpx.Timeout(self._timeout_seconds)
        for attempt in range(self._attempts):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(f"{normalized}/json/version")
                    response.raise_for_status()
                    payload: Any = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("Chrome 版本接口返回格式无效")
                websocket_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
                if not websocket_url:
                    raise ValueError("Chrome 版本接口缺少调试地址")
                return BrowserEndpointHealth(True)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < self._attempts and self._retry_delay_seconds:
                    await asyncio.sleep(self._retry_delay_seconds)
        return BrowserEndpointHealth(
            False,
            f"可见 Chrome 健康检查失败：{last_error[:500]}",
        )


@dataclass
class _OpenCircuit:
    unavailable_until: float
    health: BrowserEndpointHealth


class BrowserEndpointCircuitBreaker:
    """Share one outage decision across serial tasks without owning task state."""

    def __init__(
        self,
        probe: BrowserEndpointProbe | None = None,
        *,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe or HttpBrowserEndpointProbe()
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._lock = threading.RLock()
        self._open: dict[str, _OpenCircuit] = {}

    async def check(self, endpoint: str) -> BrowserEndpointHealth:
        normalized = str(endpoint or "").strip().rstrip("/")
        now = self._clock()
        with self._lock:
            cached = self._open.get(normalized)
            if cached is not None and now < cached.unavailable_until:
                return replace(cached.health, cached=True)
            self._open.pop(normalized, None)
        health = await self._probe(normalized)
        with self._lock:
            if health.healthy:
                self._open.pop(normalized, None)
            else:
                self._open[normalized] = _OpenCircuit(
                    unavailable_until=self._clock() + self._cooldown_seconds,
                    health=health,
                )
        return health


__all__ = [
    "BrowserEndpointCircuitBreaker",
    "BrowserEndpointGuard",
    "BrowserEndpointHealth",
    "BrowserEndpointProbe",
    "HttpBrowserEndpointProbe",
]
