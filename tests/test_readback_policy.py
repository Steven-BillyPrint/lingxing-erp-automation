from __future__ import annotations

import asyncio

from erp_automation.application.readback import (
    DEFAULT_WRITE_READBACK_DELAYS_SECONDS,
    WRITE_READBACK_DELAYS_KEY,
    iter_readback_attempts,
    normalize_readback_delays,
    readback_delays_from_configuration,
)
from erp_automation.configuration import with_configuration_defaults


def test_default_write_readback_window_covers_slow_erp_projection() -> None:
    values = with_configuration_defaults({})
    delays = readback_delays_from_configuration(values)

    assert values[WRITE_READBACK_DELAYS_KEY] == list(
        DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    )
    assert delays[0] == 0
    assert len(delays) >= 10
    assert sum(delays) >= 4 * 60


def test_invalid_or_unsafe_readback_schedule_falls_back_to_safe_default() -> None:
    assert normalize_readback_delays([]) == DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    assert normalize_readback_delays([-1, 1]) == DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    assert normalize_readback_delays([float("nan")]) == DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    assert normalize_readback_delays([901]) == DEFAULT_WRITE_READBACK_DELAYS_SECONDS


def test_readback_attempts_use_configured_backoff_without_sleeping_after_success() -> None:
    async def run() -> None:
        sleeps: list[float] = []
        attempts = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        async for attempt in iter_readback_attempts([0, 3, 7], sleeper=sleeper):
            attempts.append(attempt)
            if attempt.number == 2:
                break

        assert sleeps == [3]
        assert attempts[-1].details() == {
            "readback_attempt": 2,
            "readback_attempts": 3,
            "readback_waited_seconds": 3.0,
        }

    asyncio.run(run())
