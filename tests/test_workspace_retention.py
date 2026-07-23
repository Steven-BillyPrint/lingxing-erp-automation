from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from erp_automation.operations import workspace_retention as retention


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    _write(root / "data" / "automation.sqlite3", b"not-used-in-unit-apply")
    _write(root / "data" / "config.enc", b"secret")
    _write(root / "dist" / "ERP自动化" / "ERP自动化.exe", b"current")
    return root


def _release(root: Path, name: str, *, mtime: int, business: bool = False) -> Path:
    path = root / "rollback_backups" / name
    _write(path / "ERP自动化" / "ERP自动化.exe", name.encode())
    if business:
        _write(path / "business_data" / "data" / "automation.sqlite3", b"database-" + name.encode())
        _write(path / "business_data" / "data" / "config.enc", b"same-config")
        _write(path / "business_data" / "data" / "automation.sqlite3-wal", b"")
    path.touch()
    import os

    os.utime(path, (mtime, mtime))
    return path


def test_preview_keeps_latest_two_and_does_not_modify_workspace(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    old = _release(root, "old", mtime=1, business=True)
    middle = _release(root, "middle", mtime=2)
    newest = _release(root, "newest", mtime=3)
    _write(root / "build" / "cache.bin", b"build")
    root_staging = _write(root / "release-staging-old" / "build" / "cache.bin", b"staging")
    _write(root / "outputs" / "release_test" / "app.bin", b"release")
    _write(root / "outputs" / "work-report-2026-07-13" / "report.txt", b"keep")

    plan = retention.build_cleanup_plan(root)

    assert set(plan.kept_full_rollbacks) == {middle.name, newest.name}
    assert old.exists()
    assert (root / "build").exists()
    assert root_staging.exists()
    assert plan.business_file_count == 3
    assert any(action.path == str(old) for action in plan.actions)
    assert any(
        action.category == "root_generated_output"
        and action.path == str(root / "release-staging-old")
        for action in plan.actions
    )
    assert not any("work-report" in action.path for action in plan.actions)


def test_apply_archives_business_data_and_preserves_safe_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    old = _release(root, "old", mtime=1, business=True)
    _release(root, "middle", mtime=2)
    _release(root, "newest", mtime=3)
    _write(root / "build" / "cache.bin", b"build")
    _write(root / "release-staging-old" / "build" / "cache.bin", b"staging")
    _write(root / "outputs" / "release_test" / "app.bin", b"release")
    _write(root / "outputs" / "work-report-2026-07-13" / "report.txt", b"keep")
    _write(root / "browser_profile" / "Default" / "Cache" / "cache.bin", b"cache")
    _write(root / "browser_profile" / "Default" / "shared_proto_db" / "cache.ldb", b"cache")
    _write(root / "browser_profile" / "Default" / "Network" / "Cookies", b"login")
    _write(root / "logs" / "custom_zip_staging" / "order" / "file.zip", b"diagnostic")
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    monkeypatch.setattr(retention, "_check_sqlite_integrity", lambda workspace: {"ok": "ok"})
    before = retention._snapshot_protected_files(root)

    plan = retention.build_cleanup_plan(root)
    result = retention.apply_cleanup_plan(plan)

    assert not old.exists()
    assert not (root / "build").exists()
    assert not (root / "release-staging-old").exists()
    assert not (root / "outputs" / "release_test").exists()
    assert (root / "outputs" / "work-report-2026-07-13").exists()
    assert not (root / "browser_profile" / "Default" / "Cache").exists()
    assert not (root / "browser_profile" / "Default" / "shared_proto_db").exists()
    assert (root / "browser_profile" / "Default" / "Network" / "Cookies").exists()
    assert (root / "logs" / "custom_zip_staging" / "order" / "file.zip").exists()
    assert retention._snapshot_protected_files(root) == before
    assert result.archive_path and Path(result.archive_path).is_file()
    with zipfile.ZipFile(result.archive_path) as archive:
        index = json.loads(archive.read("index.json"))
    assert index["schema"] == retention.ARCHIVE_SCHEMA
    assert {entry["source_path"] for entry in index["entries"]} == {
        "business_data/data/automation.sqlite3",
        "business_data/data/automation.sqlite3-wal",
        "business_data/data/config.enc",
    }
    assert result.report_path and Path(result.report_path).is_file()


def test_root_generated_output_business_files_are_archived_before_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    staging = root / "release-staging-old"
    _write(staging / "dist" / "ERP自动化" / "ERP自动化.exe", b"old")
    _write(staging / "smoke-home" / "data" / "automation.sqlite3", b"staging-db")
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    monkeypatch.setattr(retention, "_check_sqlite_integrity", lambda workspace: {"ok": "ok"})

    result = retention.apply_cleanup_plan(retention.build_cleanup_plan(root))

    assert not staging.exists()
    assert result.archive_path
    with zipfile.ZipFile(result.archive_path) as archive:
        index = json.loads(archive.read("index.json"))
    assert [entry["source_path"] for entry in index["entries"]] == [
        "smoke-home/data/automation.sqlite3"
    ]


def test_root_generated_output_prefix_does_not_match_ordinary_directories(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    keep = _write(root / "reports" / "release-notes.txt", b"keep")

    plan = retention.build_cleanup_plan(root)

    assert keep.exists()
    assert not any(action.path == str(keep.parent) for action in plan.actions)


def test_integrity_sidecar_changes_are_baselined_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    _release(root, "old", mtime=1)
    _release(root, "middle", mtime=2)
    _release(root, "newest", mtime=3)
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    calls = 0

    def check_integrity(workspace: Path) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            _write(workspace / "data" / "automation.sqlite3-shm", b"stabilized")
        return {"ok": "ok"}

    monkeypatch.setattr(retention, "_check_sqlite_integrity", check_integrity)

    result = retention.apply_cleanup_plan(retention.build_cleanup_plan(root))

    assert result.applied is True
    assert calls == 2


def test_archive_failure_prevents_all_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path)
    old = _release(root, "old", mtime=1, business=True)
    _release(root, "middle", mtime=2)
    _release(root, "newest", mtime=3)
    _write(root / "build" / "cache.bin", b"build")
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    monkeypatch.setattr(retention, "_check_sqlite_integrity", lambda workspace: {"ok": "ok"})
    monkeypatch.setattr(
        retention,
        "_archive_business_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(retention.WorkspaceRetentionError("broken")),
    )

    with pytest.raises(retention.WorkspaceRetentionError, match="broken"):
        retention.apply_cleanup_plan(retention.build_cleanup_plan(root))

    assert old.exists()
    assert (root / "build").exists()


def test_second_apply_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)
    _release(root, "old", mtime=1)
    _release(root, "middle", mtime=2)
    _release(root, "newest", mtime=3)
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    monkeypatch.setattr(retention, "_check_sqlite_integrity", lambda workspace: {"ok": "ok"})

    retention.apply_cleanup_plan(retention.build_cleanup_plan(root))
    second = retention.build_cleanup_plan(root)

    assert not second.actions
    assert set(second.kept_full_rollbacks) == {"middle", "newest"}


def test_expected_exe_hash_must_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)
    _release(root, "old", mtime=1)
    _release(root, "middle", mtime=2)
    _release(root, "newest", mtime=3)
    monkeypatch.setattr(retention, "_find_active_workspace_processes", lambda workspace: ())
    monkeypatch.setattr(retention, "_check_sqlite_integrity", lambda workspace: {"ok": "ok"})

    with pytest.raises(retention.WorkspaceRetentionError, match="EXE 哈希"):
        retention.apply_cleanup_plan(
            retention.build_cleanup_plan(root),
            expected_exe_sha256=hashlib.sha256(b"wrong").hexdigest(),
        )


def test_explicit_rollbacks_are_kept(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    first = _release(root, "first", mtime=1)
    second = _release(root, "second", mtime=2)
    _release(root, "third", mtime=3)

    plan = retention.build_cleanup_plan(
        root,
        keep_rollback_names=(first.name, second.name),
    )

    assert plan.kept_full_rollbacks == ("first", "second")


def test_safe_tree_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    (root / "outputs").mkdir()
    outside = _write(tmp_path / "outside" / "file.bin")

    with pytest.raises(retention.WorkspaceRetentionError, match="越界"):
        retention._ensure_safe_tree(root, outside.parent, root / "outputs")
