"""Compare cold JSONL task-history reads against an isolated source checkout."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc

from benchmark_sqlite_resources import process_resources


def measure(events: int, tasks: int) -> dict:
    from erp_automation.ui.persistent_controller import PersistentBackgroundTaskController

    controller = PersistentBackgroundTaskController(Path.cwd(), recover_interrupted_task_journal=False)
    now = datetime.now(timezone.utc)
    path = Path(controller.log_directory()) / "app_events" / f"{now.astimezone():%Y-%m-%d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = "2026-09-01T00:00:00+00:00"
    with path.open("w", encoding="utf-8") as stream:
        for index in range(events):
            stream.write(json.dumps({"event_type": "task_snapshot", "task": {
                "task_id": f"task-{index % tasks}", "name": "Synthetic task", "area": "shipment",
                "capability": "list_orders", "status": "succeeded", "message": "x" * 256,
                "created_at": stamp, "updated_at": stamp,
            }}) + "\n")
    before = process_resources()
    tracemalloc.start()
    try:
        started = time.perf_counter()
        history = controller._today_task_history()
        cold_seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        started = time.perf_counter()
        cached = controller._today_task_history()
        cached_seconds = time.perf_counter() - started
        assert len(history) == tasks and cached == history
        digest = hashlib.sha256(json.dumps([asdict(task) for task in history], sort_keys=True, default=str).encode()).hexdigest()
        return {"events": events, "tasks": tasks, "file_bytes": path.stat().st_size,
                "python": sys.version, "before": before, "after": process_resources(),
                "cold_seconds_with_tracemalloc": cold_seconds, "cached_seconds": cached_seconds,
                "peak_python_allocation_bytes": peak, "result_sha256": digest}
    finally:
        tracemalloc.stop()
        controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--events", type=int, default=50_000)
    parser.add_argument("--tasks", type=int, default=500)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.tasks <= args.events <= 1_000_000:
        parser.error("expected 1 <= tasks <= events <= 1000000")
    sys.path.insert(0, str(args.source_root.resolve()))
    if args.worker:
        print(json.dumps(measure(args.events, args.tasks), indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="erp-history-benchmark-") as temporary:
            result = subprocess.run([
                sys.executable, str(Path(__file__).resolve()), "--source-root", str(args.source_root.resolve()),
                "--events", str(args.events), "--tasks", str(args.tasks), "--worker",
            ], cwd=temporary, capture_output=True, text=True, check=True)
            print(result.stdout, end="")


if __name__ == "__main__":
    main()
