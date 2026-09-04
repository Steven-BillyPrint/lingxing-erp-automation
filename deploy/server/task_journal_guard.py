"""Deployment backstop for live tasks missing a coordination lease.

The coordination database remains the primary deployment guard.  This module
cross-checks the append-only task journal for non-terminal tasks created by the
currently running coordinator process, so a delayed monitor cannot make an
active worker invisible to a production restart.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "blocked",
    "paused",
    "cancelled",
}
_START_CLOCK_SKEW_SECONDS = 5.0


def _timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def service_active_enter_epoch(
    service_name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    clock: Callable[[], float] = time.time,
    uptime_path: Path = Path("/proc/uptime"),
) -> float:
    """Return the service activation time on the Unix epoch clock."""

    completed = run(
        [
            "systemctl",
            "show",
            "--property=ActiveEnterTimestampMonotonic",
            "--value",
            service_name,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        active_monotonic = int(completed.stdout.strip()) / 1_000_000
        uptime = float(uptime_path.read_text(encoding="utf-8").split()[0])
    except (IndexError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("Could not determine coordinator start time.") from exc
    if active_monotonic <= 0 or uptime <= 0:
        raise RuntimeError("Coordinator start time is unavailable.")
    return clock() - uptime + active_monotonic


def nonterminal_task_ids_started_since(
    app_events_root: Path,
    *,
    started_at: float,
) -> set[str]:
    """Find latest non-terminal task snapshots created after ``started_at``.

    Files older than the current process are skipped.  A malformed individual
    line is ignored because a process crash can leave a partial final JSONL
    record; valid later snapshots remain authoritative.
    """

    root = Path(app_events_root)
    latest_status: dict[str, str] = {}
    threshold = float(started_at) - _START_CLOCK_SKEW_SECONDS
    try:
        paths = sorted(root.glob("*.jsonl"))
    except OSError as exc:
        raise RuntimeError("Could not enumerate the task journal.") from exc
    for path in paths:
        try:
            if path.stat().st_mtime < threshold:
                continue
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if (
                        not isinstance(event, dict)
                        or event.get("event_type") != "task_snapshot"
                    ):
                        continue
                    task = event.get("task")
                    if not isinstance(task, dict):
                        continue
                    task_id = str(task.get("task_id") or "").strip()
                    if not task_id:
                        continue
                    created_at = _timestamp(task.get("created_at"))
                    if created_at is None:
                        created_at = _timestamp(event.get("timestamp"))
                    if created_at is None or created_at < threshold:
                        continue
                    latest_status[task_id] = (
                        str(task.get("status") or "").strip().casefold()
                    )
        except OSError as exc:
            raise RuntimeError(f"Could not read task journal: {path.name}") from exc
    return {
        task_id
        for task_id, status in latest_status.items()
        if status not in _TERMINAL_STATUSES
    }
