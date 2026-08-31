from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


_MAX_LOG_BYTES = 2 * 1024 * 1024
_installed: CrashDiagnostics | None = None


def default_crash_log_path() -> Path:
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    root = Path(local_app_data) if local_app_data else Path.cwd()
    return root / "LingxingERP" / "logs" / "client-crash.log"


class CrashDiagnostics:
    """Capture Python and native fault evidence without business payloads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stream: IO[str] | None = None
        self._previous_sys_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook
        self._installed_sys_hook = None
        self._installed_thread_hook = None
        self._faulthandler_was_enabled = faulthandler.is_enabled()

    def install(self) -> CrashDiagnostics:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_file() and self.path.stat().st_size > _MAX_LOG_BYTES:
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            self.path.replace(rotated)
        self._stream = self.path.open("a", encoding="utf-8", buffering=1)
        self._write_marker("client_start")
        if not self._faulthandler_was_enabled:
            faulthandler.enable(file=self._stream, all_threads=True)

        def sys_hook(error_type, error, error_traceback) -> None:
            self._write_exception("main_thread_exception", error_type, error, error_traceback)
            self._previous_sys_hook(error_type, error, error_traceback)

        def thread_hook(arguments: threading.ExceptHookArgs) -> None:
            self._write_exception(
                "worker_thread_exception",
                arguments.exc_type,
                arguments.exc_value,
                arguments.exc_traceback,
            )
            self._previous_thread_hook(arguments)

        self._installed_sys_hook = sys_hook
        self._installed_thread_hook = thread_hook
        sys.excepthook = sys_hook
        threading.excepthook = thread_hook
        return self

    def _write_marker(self, event: str) -> None:
        stream = self._stream
        if stream is None:
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stream.write(f"\n[{timestamp}] {event} pid={os.getpid()}\n")

    def _write_exception(
        self,
        event: str,
        error_type,
        error,
        error_traceback,
    ) -> None:
        self._write_marker(event)
        stream = self._stream
        if stream is not None:
            del error
            stream.write(
                "exception_type="
                f"{getattr(error_type, '__module__', 'builtins')}."
                f"{getattr(error_type, '__name__', 'Exception')}\n"
            )
            for frame in traceback.extract_tb(error_traceback):
                # File/function/line identify the failing code path. Omitting
                # the rendered source line and exception message prevents a
                # runtime value (order number, address or token) from entering
                # this low-level crash file.
                stream.write(
                    f'  File "{frame.filename}", line {frame.lineno}, '
                    f"in {frame.name}\n"
                )

    def close(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._write_marker("client_stop")
        self._stream = None
        if sys.excepthook is self._installed_sys_hook:
            sys.excepthook = self._previous_sys_hook
        if threading.excepthook is self._installed_thread_hook:
            threading.excepthook = self._previous_thread_hook
        if faulthandler.is_enabled() and not self._faulthandler_was_enabled:
            faulthandler.disable()
        stream.close()


def install_crash_diagnostics(
    path: str | Path | None = None,
) -> CrashDiagnostics:
    global _installed
    if _installed is not None:
        return _installed
    diagnostics = CrashDiagnostics(path or default_crash_log_path()).install()
    _installed = diagnostics
    atexit.register(diagnostics.close)
    return diagnostics


__all__ = [
    "CrashDiagnostics",
    "default_crash_log_path",
    "install_crash_diagnostics",
]
