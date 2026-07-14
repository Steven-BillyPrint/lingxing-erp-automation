"""Desktop UI contracts and pure state models.

Importing this package never requires PySide6. The optional Qt implementation
is loaded only by :mod:`erp_automation.app` when the desktop shell is started.
"""

from .controller import BackgroundTaskController, ControlResult, InMemoryBackgroundTaskController
from .persistent_controller import PersistentBackgroundTaskController
from .models import (
    Capability,
    CapabilityMode,
    CapabilityPolicy,
    CustomOrderRow,
    DashboardMetrics,
    DESKTOP_CONFIRMATION_PAYLOAD_KEY,
    DesktopSettings,
    DesktopSnapshot,
    DesktopWriteAction,
    DesktopWriteConfirmation,
    LogEntry,
    LogLevel,
    MigrationInfo,
    ShipmentRow,
    TaskArea,
    TaskCommand,
    TaskRecord,
    TaskStatus,
)
from .qt_compat import PYSIDE6_AVAILABLE, PySide6RequiredError, require_pyside6

__all__ = [
    "BackgroundTaskController",
    "Capability",
    "CapabilityMode",
    "CapabilityPolicy",
    "ControlResult",
    "CustomOrderRow",
    "DashboardMetrics",
    "DESKTOP_CONFIRMATION_PAYLOAD_KEY",
    "DesktopSettings",
    "DesktopSnapshot",
    "DesktopWriteAction",
    "DesktopWriteConfirmation",
    "InMemoryBackgroundTaskController",
    "LogEntry",
    "LogLevel",
    "MigrationInfo",
    "PYSIDE6_AVAILABLE",
    "PersistentBackgroundTaskController",
    "PySide6RequiredError",
    "ShipmentRow",
    "TaskArea",
    "TaskCommand",
    "TaskRecord",
    "TaskStatus",
    "require_pyside6",
]
