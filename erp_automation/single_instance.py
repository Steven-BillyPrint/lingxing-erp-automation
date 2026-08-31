from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


_ERROR_ALREADY_EXISTS = 183
_SW_RESTORE = 9
DEFAULT_MUTEX_NAME = r"Local\Billyprint.ERPAutomation.Client"


@dataclass
class DesktopSingleInstance:
    """Own one process-wide Windows mutex without depending on Qt."""

    acquired: bool
    _handle: int = 0
    _kernel32: Any = None

    def close(self) -> None:
        handle = int(self._handle or 0)
        if not handle or self._kernel32 is None:
            return
        self._handle = 0
        if self.acquired:
            self._kernel32.ReleaseMutex(handle)
        self._kernel32.CloseHandle(handle)

    def __enter__(self) -> DesktopSingleInstance:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def acquire_desktop_single_instance(
    name: str = DEFAULT_MUTEX_NAME,
) -> DesktopSingleInstance:
    """Acquire the production desktop mutex.

    Non-Windows platforms return a no-op acquired guard so source tests and
    administrative tooling do not need a second implementation.
    """

    if os.name != "nt":
        return DesktopSingleInstance(acquired=True)

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = int(kernel32.CreateMutexW(None, True, str(name)) or 0)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Unable to create desktop mutex")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return DesktopSingleInstance(acquired=False)
    return DesktopSingleInstance(
        acquired=True,
        _handle=handle,
        _kernel32=kernel32,
    )


def activate_existing_desktop_window() -> bool:
    """Restore the existing ERP window when a second launch is attempted."""

    if os.name != "nt":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[object] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND,
        wintypes.LPWSTR,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    @callback_type
    def visit(window, _parameter):
        if not user32.IsWindowVisible(window):
            return True
        length = int(user32.GetWindowTextLengthW(window))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        if buffer.value.startswith("ERP 自动化控制台"):
            found.append(window)
            return False
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        return False
    window = found[0]
    user32.ShowWindow(window, _SW_RESTORE)
    user32.SetForegroundWindow(window)
    return True


__all__ = [
    "DEFAULT_MUTEX_NAME",
    "DesktopSingleInstance",
    "acquire_desktop_single_instance",
    "activate_existing_desktop_window",
]
