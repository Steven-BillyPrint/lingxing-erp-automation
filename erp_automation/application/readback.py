"""Shared eventual-consistency policy for Lingxing write readback.

Lingxing may acknowledge a mutation before the corresponding list/detail API
exposes the new state.  All write workflows use the same bounded backoff so a
slow projection is not mistaken for a failed write and replayed.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


WRITE_READBACK_DELAYS_KEY = "lingxing.write_readback_delays_seconds"

# First read immediately, then remain responsive for ordinary updates while
# allowing almost five minutes for slow order splitting/outbound projections.
DEFAULT_WRITE_READBACK_DELAYS_SECONDS: tuple[float, ...] = (
    0.0,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    30.0,
    45.0,
    60.0,
    60.0,
    60.0,
)

SleepFunc = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ReadbackAttempt:
    number: int
    total: int
    waited_seconds: float

    def details(self) -> dict[str, int | float]:
        return {
            "readback_attempt": self.number,
            "readback_attempts": self.total,
            "readback_waited_seconds": round(self.waited_seconds, 3),
        }


def normalize_readback_delays(value: Any) -> tuple[float, ...]:
    """Validate a configured schedule and fall back to the safe default."""

    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray, str)):
        return DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    try:
        delays = tuple(float(item) for item in raw)
    except (TypeError, ValueError):
        return DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    if (
        not delays
        or len(delays) > 60
        or any(not math.isfinite(delay) or delay < 0 or delay > 300 for delay in delays)
        or sum(delays) > 900
    ):
        return DEFAULT_WRITE_READBACK_DELAYS_SECONDS
    # Every workflow must attempt one immediate read before sleeping.
    return (0.0, *delays) if delays[0] else delays


def readback_delays_from_configuration(
    configuration: Mapping[str, Any],
) -> tuple[float, ...]:
    return normalize_readback_delays(
        configuration.get(
            WRITE_READBACK_DELAYS_KEY,
            DEFAULT_WRITE_READBACK_DELAYS_SECONDS,
        )
    )


async def iter_readback_attempts(
    delays: Sequence[float],
    *,
    sleeper: SleepFunc = asyncio.sleep,
) -> AsyncIterator[ReadbackAttempt]:
    normalized = normalize_readback_delays(delays)
    waited = 0.0
    total = len(normalized)
    for index, delay in enumerate(normalized, start=1):
        if delay:
            await sleeper(delay)
            waited += delay
        yield ReadbackAttempt(index, total, waited)


__all__ = [
    "DEFAULT_WRITE_READBACK_DELAYS_SECONDS",
    "ReadbackAttempt",
    "WRITE_READBACK_DELAYS_KEY",
    "iter_readback_attempts",
    "normalize_readback_delays",
    "readback_delays_from_configuration",
]
