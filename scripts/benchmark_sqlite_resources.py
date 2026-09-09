"""Measure synthetic SQLite request cycles in an isolated temporary workspace.

Run each source revision in a fresh process. No runtime database, network service
or credentials are used; normal Python garbage collection remains enabled.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time


def process_resources() -> dict[str, int]:
    if sys.platform == "win32":
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        process = kernel.GetCurrentProcess()
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        handles = wintypes.DWORD()
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        if not kernel.GetProcessHandleCount(process, ctypes.byref(handles)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"rss_bytes": counters.WorkingSetSize, "peak_rss_bytes": counters.PeakWorkingSetSize,
                "process_handles": handles.value}

    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"VmRSS", "VmHWM"}:
            fields[key] = int(value.split()[0]) * 1024
    result = {"rss_bytes": fields["VmRSS"], "peak_rss_bytes": fields["VmHWM"],
              "process_handles": len(list(Path("/proc/self/fd").iterdir()))}
    rollup = Path("/proc/self/smaps_rollup")
    if rollup.exists():
        for line in rollup.read_text().splitlines():
            if line.startswith("Pss:"):
                result["pss_bytes"] = int(line.split()[1]) * 1024
    return result


def benchmark(root: Path, iterations: int) -> dict:
    from erp_automation.coordination.store import CoordinationStore
    from erp_automation.persistence.workflow_store import CustomWorkflowStore
    from shipment_automation.notification_store import ShipmentNotificationStore
    from shipment_automation.queue_store import ShipmentWorkflowStore

    stores = [CoordinationStore(root / "coordination.sqlite3"),
              CustomWorkflowStore(root / "automation.sqlite3"),
              ShipmentWorkflowStore(root / "shipment.sqlite3"),
              ShipmentNotificationStore(root / "notification.sqlite3")]
    for store in stores[1:]:
        store.initialize()
    samples = [{"cycle": 0, **process_resources()}]
    started = time.perf_counter()
    for index in range(iterations):
        for store in stores:
            connect = getattr(store, "connect", None) or store._connect
            with connect() as connection:
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        if (index + 1) % max(1, iterations // 20) == 0:
            samples.append({"cycle": index + 1, **process_resources()})
    return {"iterations": iterations, "connections_per_cycle": len(stores),
            "elapsed_seconds": round(time.perf_counter() - started, 4), "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100_000:
        parser.error("iterations must be between 1 and 100000")
    if args.worker:
        sys.path.insert(0, str(args.source_root.resolve()))
        report = benchmark(Path.cwd(), args.iterations)
        report.update(python=sys.version, platform=sys.platform,
                      source_root=str(args.source_root.resolve()), pid=os.getpid())
        print(json.dumps(report, indent=2))
        return
    # Exit the measured process before cleanup: the old implementation can still
    # hold SQLite handles until process exit. Do not force GC to improve samples.
    with tempfile.TemporaryDirectory(prefix="erp-sqlite-benchmark-") as temporary:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker", "--source-root",
             str(args.source_root.resolve()), "--iterations", str(args.iterations)],
            cwd=temporary, check=True, capture_output=True, text=True,
        )
        print(result.stdout, end="")


if __name__ == "__main__":
    main()
