from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from erp_automation.operations.log_retention import (
    DEFAULT_LOG_RETENTION_DAYS,
    UnsafeLogPathError,
    cleanup_configured_log_roots,
    cleanup_expired_logs,
)


NOW = 2_000_000_000.0
DAY = 24 * 60 * 60


def _write_with_age(path: Path, *, days: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name, encoding="utf-8")
    timestamp = NOW - days * DAY
    os.utime(path, (timestamp, timestamp))


def test_default_retention_deletes_only_files_older_than_90_days(tmp_path: Path):
    root = tmp_path / "logs"
    expired = root / "nested" / "expired.json"
    retained = root / "retained.png"
    boundary = root / "boundary.log"
    sibling = tmp_path / "not-logs" / "expired.json"
    _write_with_age(expired, days=91)
    _write_with_age(retained, days=89)
    _write_with_age(boundary, days=90)
    _write_with_age(sibling, days=365)

    report = cleanup_expired_logs(root, now=NOW)

    assert report.retention_days == DEFAULT_LOG_RETENTION_DAYS
    assert report.deleted_files == (expired,)
    assert not expired.exists()
    assert retained.exists()
    assert boundary.exists()
    assert sibling.exists()
    assert (root / "nested").is_dir()


def test_explicit_retention_and_dry_run_remain_supported(tmp_path: Path):
    root = tmp_path / "logs"
    expired = root / "eight-days-old.log"
    _write_with_age(expired, days=8)

    preview = cleanup_expired_logs(root, retention_days=7, now=NOW, dry_run=True)

    assert preview.deleted_files == (expired,)
    assert preview.dry_run is True
    assert expired.exists()

    result = cleanup_expired_logs(root, retention_days=7, now=NOW)
    assert result.deleted_count == 1
    assert not expired.exists()


def test_missing_root_is_a_safe_noop_and_file_root_is_rejected(tmp_path: Path):
    missing = tmp_path / "missing"
    report = cleanup_expired_logs(missing, now=NOW)
    assert report.root == missing
    assert report.deleted_count == 0

    file_root = tmp_path / "not-a-directory"
    file_root.write_text("data", encoding="utf-8")
    with pytest.raises(UnsafeLogPathError, match="不是目录"):
        cleanup_expired_logs(file_root, now=NOW)


def test_broad_non_log_directory_is_rejected_without_deleting_files(tmp_path: Path):
    broad_root = tmp_path / "Documents"
    old_file = broad_root / "important.json"
    _write_with_age(old_file, days=365)

    with pytest.raises(UnsafeLogPathError, match="必须命名为 log 或 logs"):
        cleanup_expired_logs(broad_root, now=NOW)

    assert old_file.exists()


def test_configured_cleanup_deduplicates_roots_and_never_blocks_on_bad_path(tmp_path: Path):
    good_root = tmp_path / "logs"
    expired = good_root / "expired.log"
    _write_with_age(expired, days=91)
    bad_root = tmp_path / "file"
    bad_root.write_text("not a directory", encoding="utf-8")
    config = argparse.Namespace(log_dir=good_root, debug_log_dir=bad_root)

    result = cleanup_configured_log_roots(config, now=NOW)

    assert len(result.reports) == 1
    assert result.reports[0].deleted_count == 1
    assert len(result.errors) == 1
    assert bad_root.exists()


def test_symbolic_links_are_never_followed_or_deleted(tmp_path: Path):
    root = tmp_path / "logs"
    root.mkdir()
    outside = tmp_path / "outside"
    target_file = outside / "old.log"
    target_dir_file = outside / "folder" / "old.json"
    _write_with_age(target_file, days=365)
    _write_with_age(target_dir_file, days=365)
    file_link = root / "linked-file.log"
    directory_link = root / "linked-directory"
    try:
        file_link.symlink_to(target_file)
        directory_link.symlink_to(target_dir_file.parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")

    report = cleanup_expired_logs(root, now=NOW)

    assert target_file.exists()
    assert target_dir_file.exists()
    assert file_link.is_symlink()
    assert directory_link.is_symlink()
    assert {issue.path for issue in report.skipped_paths} == {file_link, directory_link}


def test_symbolic_link_cannot_be_used_as_the_log_root(tmp_path: Path):
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(actual_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")

    with pytest.raises(UnsafeLogPathError, match="符号链接"):
        cleanup_expired_logs(linked_root, now=NOW)
