from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_LOG_RETENTION_DAYS = 90.0
_SECONDS_PER_DAY = 24 * 60 * 60


class UnsafeLogPathError(ValueError):
    """Raised when a configured log root could escape through a link or invalid path."""


@dataclass(frozen=True)
class LogRetentionIssue:
    path: Path
    reason: str


@dataclass(frozen=True)
class LogRetentionReport:
    root: Path
    retention_days: float
    cutoff_timestamp: float
    deleted_files: tuple[Path, ...] = ()
    retained_files: tuple[Path, ...] = ()
    skipped_paths: tuple[LogRetentionIssue, ...] = ()
    errors: tuple[LogRetentionIssue, ...] = ()
    deleted_bytes: int = 0
    dry_run: bool = False

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_files)


@dataclass(frozen=True)
class ConfiguredLogCleanupResult:
    reports: tuple[LogRetentionReport, ...] = ()
    errors: tuple[LogRetentionIssue, ...] = ()


def cleanup_expired_logs(
    log_root: str | os.PathLike[str],
    *,
    retention_days: float = DEFAULT_LOG_RETENTION_DAYS,
    now: datetime | float | int | None = None,
    dry_run: bool = False,
) -> LogRetentionReport:
    """Delete regular files older than the retention window under one log root.

    The traversal never follows symbolic links or Windows reparse points, never
    removes directories, and verifies every candidate still resolves beneath the
    selected root immediately before deletion. A missing root is a safe no-op.
    """

    days = float(retention_days)
    if days <= 0:
        raise ValueError("retention_days must be greater than zero")

    root = _absolute_path(log_root)
    cutoff_timestamp = _timestamp(now) - days * _SECONDS_PER_DAY
    if root.is_symlink():
        raise UnsafeLogPathError(f"日志根目录不能是符号链接：{root}")
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return LogRetentionReport(
            root=root,
            retention_days=days,
            cutoff_timestamp=cutoff_timestamp,
            dry_run=dry_run,
        )
    if _is_reparse_point(root_stat):
        raise UnsafeLogPathError(f"日志根目录不能是重解析点：{root}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise UnsafeLogPathError(f"日志根路径不是目录：{root}")
    if root.name.casefold() not in {"log", "logs"}:
        raise UnsafeLogPathError(
            f"为防止误删普通文件，自动清理目录必须命名为 log 或 logs：{root}"
        )

    resolved_root = root.resolve(strict=True)
    deleted: list[Path] = []
    retained: list[Path] = []
    skipped: list[LogRetentionIssue] = []
    errors: list[LogRetentionIssue] = []
    deleted_bytes = 0

    def walk(directory: Path) -> None:
        nonlocal deleted_bytes
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            errors.append(LogRetentionIssue(directory, _error_text(exc)))
            return
        for entry in entries:
            candidate = Path(entry.path)
            try:
                candidate_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append(LogRetentionIssue(candidate, _error_text(exc)))
                continue

            if entry.is_symlink() or _is_reparse_point(candidate_stat):
                skipped.append(LogRetentionIssue(candidate, "symbolic_link_or_reparse_point"))
                continue
            if stat.S_ISDIR(candidate_stat.st_mode):
                try:
                    _resolve_inside(candidate, resolved_root)
                except (OSError, UnsafeLogPathError) as exc:
                    skipped.append(LogRetentionIssue(candidate, str(exc)))
                    continue
                walk(candidate)
                continue
            if not stat.S_ISREG(candidate_stat.st_mode):
                skipped.append(LogRetentionIssue(candidate, "not_a_regular_file"))
                continue
            if candidate_stat.st_mtime >= cutoff_timestamp:
                retained.append(candidate)
                continue

            try:
                _resolve_inside(candidate, resolved_root)
                current_stat = candidate.lstat()
                if candidate.is_symlink() or _is_reparse_point(current_stat):
                    skipped.append(LogRetentionIssue(candidate, "link_created_during_scan"))
                    continue
                if not stat.S_ISREG(current_stat.st_mode):
                    skipped.append(LogRetentionIssue(candidate, "file_type_changed_during_scan"))
                    continue
                scanned_identity = _file_identity(candidate_stat)
                current_identity = _file_identity(current_stat)
                if scanned_identity is not None and scanned_identity != current_identity:
                    skipped.append(LogRetentionIssue(candidate, "file_changed_during_scan"))
                    continue
                if not dry_run:
                    candidate.unlink()
                deleted.append(candidate)
                deleted_bytes += int(current_stat.st_size)
            except FileNotFoundError:
                skipped.append(LogRetentionIssue(candidate, "file_disappeared_during_scan"))
            except (OSError, UnsafeLogPathError) as exc:
                errors.append(LogRetentionIssue(candidate, _error_text(exc)))

    walk(root)
    return LogRetentionReport(
        root=root,
        retention_days=days,
        cutoff_timestamp=cutoff_timestamp,
        deleted_files=tuple(deleted),
        retained_files=tuple(retained),
        skipped_paths=tuple(skipped),
        errors=tuple(errors),
        deleted_bytes=deleted_bytes,
        dry_run=dry_run,
    )


def cleanup_configured_log_roots(
    runtime_config: Any,
    *,
    retention_days: float = DEFAULT_LOG_RETENTION_DAYS,
    now: datetime | float | int | None = None,
    dry_run: bool = False,
) -> ConfiguredLogCleanupResult:
    """Clean ``log_dir``/``debug_log_dir`` attributes without blocking a run.

    Invalid or inaccessible roots are reported to callers and skipped. This
    wrapper is used by command-line entry points so log maintenance can never
    turn an otherwise valid automation run into a startup failure.
    """

    reports: list[LogRetentionReport] = []
    errors: list[LogRetentionIssue] = []
    seen: set[str] = set()
    for attribute in ("log_dir", "debug_log_dir"):
        value = getattr(runtime_config, attribute, None)
        if not value:
            continue
        path = _absolute_path(value)
        identity = os.path.normcase(os.fspath(path))
        if identity in seen:
            continue
        seen.add(identity)
        try:
            reports.append(
                cleanup_expired_logs(
                    path,
                    retention_days=retention_days,
                    now=now,
                    dry_run=dry_run,
                )
            )
        except (OSError, ValueError) as exc:
            errors.append(LogRetentionIssue(path, _error_text(exc)))
    return ConfiguredLogCleanupResult(reports=tuple(reports), errors=tuple(errors))


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    expanded = Path(path).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _timestamp(value: datetime | float | int | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def _resolve_inside(path: Path, resolved_root: Path) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafeLogPathError(f"路径位于日志根目录之外：{path}") from exc
    return resolved


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return bool(flag and attributes & flag)


def _file_identity(value: os.stat_result) -> tuple[int, int] | None:
    """Return a stable identity when the platform exposes one.

    Some Windows/Python combinations return zeroed ``st_dev``/``st_ino`` from
    ``DirEntry.stat(follow_symlinks=False)`` even though ``Path.lstat`` has real
    values. Zero therefore means "identity unavailable", not "file changed".
    """

    device, inode = int(value.st_dev), int(value.st_ino)
    return (device, inode) if device or inode else None


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__
