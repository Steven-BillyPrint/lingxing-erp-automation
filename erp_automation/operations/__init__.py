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
from .scan_audit import (
    SCAN_AUDIT_DIRECTORY,
    SCAN_AUDIT_SCHEMA,
    SCAN_AUDIT_VERSION,
    ScanAuditError,
    ScanAuditWriteResult,
    ScanAuditWriter,
    UnsafeScanAuditPathError,
    build_scan_audit_document,
    redact_audit_text,
    safe_exception_summary,
    safe_query_summary,
    write_scan_audit,
)

__all__ = [
    "DEFAULT_LOG_RETENTION_DAYS",
    "ConfiguredLogCleanupResult",
    "LogRetentionIssue",
    "LogRetentionReport",
    "UnsafeLogPathError",
    "cleanup_configured_log_roots",
    "cleanup_expired_logs",
    "SCAN_AUDIT_DIRECTORY",
    "SCAN_AUDIT_SCHEMA",
    "SCAN_AUDIT_VERSION",
    "ScanAuditError",
    "ScanAuditWriteResult",
    "ScanAuditWriter",
    "UnsafeScanAuditPathError",
    "build_scan_audit_document",
    "redact_audit_text",
    "safe_exception_summary",
    "safe_query_summary",
    "write_scan_audit",
]
