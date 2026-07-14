"""Runtime maintenance operations shared by the desktop app and legacy CLIs."""

from .log_retention import (
    DEFAULT_LOG_RETENTION_DAYS,
    ConfiguredLogCleanupResult,
    LogRetentionIssue,
    LogRetentionReport,
    UnsafeLogPathError,
    cleanup_configured_log_roots,
    cleanup_expired_logs,
)

__all__ = [
    "DEFAULT_LOG_RETENTION_DAYS",
    "ConfiguredLogCleanupResult",
    "LogRetentionIssue",
    "LogRetentionReport",
    "UnsafeLogPathError",
    "cleanup_configured_log_roots",
    "cleanup_expired_logs",
]
