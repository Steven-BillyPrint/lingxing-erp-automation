"""Safely prune generated desktop release artifacts from the workspace.

The command is preview-only unless ``--apply`` is supplied.  Historical
business files embedded in obsolete full-release backups are content-addressed,
archived, and verified before any release backup is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


ARCHIVE_SCHEMA = "erp-automation.business-history"
ARCHIVE_VERSION = 1
REPORT_SCHEMA = "erp-automation.workspace-retention"
REPORT_VERSION = 1
DEFAULT_KEEP_FULL_ROLLBACKS = 2

_OUTPUT_PREFIXES = (
    "release",
    "recovery_release",
    "staging",
    "smoke",
    "frozen-smoke",
)
_OUTPUT_KEEP_PREFIXES = ("work-report-",)
_ROLLBACK_EXCLUDED_NAMES = {"business_history", "retention_reports"}
_BROWSER_CACHE_PATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnWebGPUCache",
    "Default/DawnGraphiteCache",
    # Chromium stores rebuildable feature/protobuf records here.  It can also
    # retain very large data-URL copies of downloaded customization ZIPs;
    # login cookies and authenticated site storage live elsewhere.
    "Default/shared_proto_db",
    "BrowserMetrics",
    "GrShaderCache",
    "GraphiteDawnCache",
    "ShaderCache",
    "GPUPersistentCache",
)
_PROTECTED_DATA_NAMES = (
    "automation.sqlite3",
    "shipment_queue.sqlite3",
    "config.enc",
    "config.enc.bak",
)


class WorkspaceRetentionError(RuntimeError):
    """Raised when pruning cannot be proven safe."""


@dataclass(frozen=True)
class CleanupAction:
    category: str
    path: str
    bytes: int


@dataclass(frozen=True)
class CleanupPlan:
    workspace: str
    actions: tuple[CleanupAction, ...]
    kept_full_rollbacks: tuple[str, ...]
    business_file_count: int
    estimated_reclaim_bytes: int


@dataclass(frozen=True)
class CleanupResult:
    applied: bool
    plan: CleanupPlan
    archive_path: str | None = None
    report_path: str | None = None
    reclaimed_bytes: int = 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _ensure_direct_child(parent: Path, child: Path) -> Path:
    parent = parent.resolve(strict=True)
    if _is_link(parent):
        raise WorkspaceRetentionError(f"拒绝使用链接目录：{parent}")
    if child.parent.resolve(strict=True) != parent:
        raise WorkspaceRetentionError(f"清理目标不是允许目录的直接子项：{child}")
    if _is_link(child):
        raise WorkspaceRetentionError(f"拒绝清理符号链接或 Junction：{child}")
    return child


def _ensure_safe_tree(workspace: Path, path: Path, allowed_parent: Path) -> None:
    workspace_resolved = workspace.resolve(strict=True)
    allowed_resolved = allowed_parent.resolve(strict=True)
    path_resolved = path.resolve(strict=True)
    try:
        allowed_resolved.relative_to(workspace_resolved)
        path_resolved.relative_to(allowed_resolved)
    except ValueError as exc:
        raise WorkspaceRetentionError(f"清理路径越界：{path}") from exc
    if path_resolved == allowed_resolved:
        if path.name != "build":
            raise WorkspaceRetentionError(f"拒绝清理整个受保护根目录：{path}")
    if path_resolved == (workspace_resolved / "dist").resolve(strict=False):
        raise WorkspaceRetentionError("禁止清理当前 dist")
    for item in (path, *path.rglob("*")):
        if _is_link(item):
            raise WorkspaceRetentionError(f"清理目标包含链接或 Junction：{item}")


def _contains_executable(path: Path) -> bool:
    return any(item.is_file() for item in path.rglob("*.exe"))


def _is_generated_output(path: Path) -> bool:
    lowered = path.name.lower()
    if any(lowered.startswith(prefix) for prefix in _OUTPUT_KEEP_PREFIXES):
        return False
    return any(lowered.startswith(prefix) for prefix in _OUTPUT_PREFIXES)


def _is_business_history_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith("automation.sqlite3")
        or name.startswith("shipment_queue.sqlite3")
        or (name.startswith("shipment_queue.pre_") and name.endswith(".sqlite3"))
        or name.startswith("processed_platform_orders.json")
        or name in {"config.enc", "config.enc.bak", "china_workdays.json"}
    )


def _business_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    found: list[Path] = []
    for root in paths:
        found.extend(
            item
            for item in root.rglob("*")
            if item.is_file() and not item.is_symlink() and _is_business_history_file(item)
        )
    return tuple(sorted(found, key=lambda item: str(item).lower()))


def _select_kept_rollbacks(
    rollback_root: Path,
    full_rollbacks: Sequence[Path],
    *,
    keep_count: int,
    explicit_names: Sequence[str] = (),
) -> tuple[Path, ...]:
    if keep_count < 1:
        raise WorkspaceRetentionError("至少必须保留一个完整发布回滚")
    by_name = {path.name: path for path in full_rollbacks}
    if explicit_names:
        if len(set(explicit_names)) != len(explicit_names):
            raise WorkspaceRetentionError("显式保留的回滚目录存在重复名称")
        missing = [name for name in explicit_names if name not in by_name]
        if missing:
            raise WorkspaceRetentionError(f"指定保留的完整回滚不存在：{', '.join(missing)}")
        return tuple(by_name[name] for name in explicit_names)
    newest = sorted(full_rollbacks, key=lambda item: item.stat().st_mtime_ns, reverse=True)
    return tuple(newest[:keep_count])


def build_cleanup_plan(
    workspace: str | Path,
    *,
    keep_full_rollbacks: int = DEFAULT_KEEP_FULL_ROLLBACKS,
    keep_rollback_names: Sequence[str] = (),
    include_browser_cache: bool = True,
) -> CleanupPlan:
    root = Path(workspace).resolve(strict=True)
    actions: list[CleanupAction] = []

    build_root = root / "build"
    if build_root.is_dir():
        actions.append(CleanupAction("build", str(build_root), _path_size(build_root)))

    # Historical release/smoke directories were created directly below the
    # workspace before the dedicated ``outputs`` root became the convention.
    # Treat only explicitly generated prefixes as disposable and keep the
    # current ``dist`` plus all ordinary source/business directories outside
    # this rule.
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if child.is_dir() and _is_generated_output(child):
            _ensure_direct_child(root, child)
            actions.append(
                CleanupAction("root_generated_output", str(child), _path_size(child))
            )

    outputs_root = root / "outputs"
    if outputs_root.is_dir():
        for child in sorted(outputs_root.iterdir(), key=lambda item: item.name.lower()):
            if child.is_dir() and _is_generated_output(child):
                _ensure_direct_child(outputs_root, child)
                actions.append(CleanupAction("generated_output", str(child), _path_size(child)))

    rollback_root = root / "rollback_backups"
    full_rollbacks: list[Path] = []
    if rollback_root.is_dir():
        for child in rollback_root.iterdir():
            if (
                child.is_dir()
                and child.name not in _ROLLBACK_EXCLUDED_NAMES
                and not _is_link(child)
                and _contains_executable(child)
            ):
                full_rollbacks.append(child)
    kept = _select_kept_rollbacks(
        rollback_root,
        full_rollbacks,
        keep_count=keep_full_rollbacks,
        explicit_names=keep_rollback_names,
    ) if full_rollbacks else ()
    kept_set = {path.resolve() for path in kept}
    obsolete_rollbacks = tuple(
        sorted(
            (path for path in full_rollbacks if path.resolve() not in kept_set),
            key=lambda item: item.name.lower(),
        )
    )
    for path in obsolete_rollbacks:
        _ensure_direct_child(rollback_root, path)
        actions.append(CleanupAction("obsolete_full_rollback", str(path), _path_size(path)))

    if include_browser_cache:
        profile_root = root / "browser_profile"
        if profile_root.is_dir():
            for relative in _BROWSER_CACHE_PATHS:
                cache = profile_root / Path(relative)
                if cache.is_dir():
                    actions.append(CleanupAction("browser_cache", str(cache), _path_size(cache)))

    deletion_roots = tuple(Path(action.path) for action in actions)
    business_count = len(_business_files(deletion_roots))
    return CleanupPlan(
        workspace=str(root),
        actions=tuple(actions),
        kept_full_rollbacks=tuple(path.name for path in kept),
        business_file_count=business_count,
        estimated_reclaim_bytes=sum(action.bytes for action in actions),
    )


def _snapshot_protected_files(workspace: Path) -> dict[str, dict[str, object]]:
    paths: list[Path] = []
    data_root = workspace / "data"
    if data_root.is_dir():
        paths.extend(item for item in data_root.rglob("*") if item.is_file() and not item.is_symlink())
    exe = workspace / "dist" / "ERP自动化" / "ERP自动化.exe"
    if exe.is_file():
        paths.append(exe)
    return {
        path.relative_to(workspace).as_posix(): {
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(paths, key=lambda item: str(item).lower())
    }


def _check_sqlite_integrity(workspace: Path) -> dict[str, str]:
    results: dict[str, str] = {}
    for name in ("automation.sqlite3", "shipment_queue.sqlite3"):
        path = workspace / "data" / name
        if not path.is_file():
            continue
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        results[name] = result
        if result.lower() != "ok":
            raise WorkspaceRetentionError(f"SQLite 完整性检查失败：{name} / {result}")
    return results


def _find_active_workspace_processes(workspace: Path) -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        payload = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise WorkspaceRetentionError(f"无法确认桌面程序和浏览器是否已退出：{exc}") from exc
    if isinstance(payload, dict):
        payload = [payload]
    dist_prefix = str((workspace / "dist" / "ERP自动化").resolve()).lower()
    profile_token = str((workspace / "browser_profile").resolve()).lower()
    active: list[str] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "")
        executable = str(row.get("ExecutablePath") or "").lower()
        command_line = str(row.get("CommandLine") or "").lower()
        if executable.startswith(dist_prefix) or profile_token in command_line:
            active.append(f"{name} (PID {row.get('ProcessId')})")
    return tuple(active)


def _archive_business_history(
    workspace: Path,
    obsolete_rollbacks: Sequence[Path],
    *,
    timestamp: str,
) -> Path | None:
    files = _business_files(obsolete_rollbacks)
    if not files:
        return None
    rollback_root = workspace / "rollback_backups"
    archive_root = rollback_root / "business_history"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / f"business_history_{timestamp}.zip"
    temporary = archive_path.with_suffix(".zip.tmp")
    entries: list[dict[str, object]] = []
    objects: dict[str, Path] = {}
    rollback_by_path = sorted(obsolete_rollbacks, key=lambda item: len(item.parts), reverse=True)
    for path in files:
        owning = next(root for root in rollback_by_path if path.is_relative_to(root))
        digest = _sha256_file(path)
        object_name = f"objects/{digest[:2]}/{digest}"
        objects.setdefault(digest, path)
        entries.append(
            {
                "backup": owning.name,
                "source_path": path.relative_to(owning).as_posix(),
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": digest,
                "object": object_name,
            }
        )
    index: dict[str, object] = {
        "schema": ARCHIVE_SCHEMA,
        "version": ARCHIVE_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_backups": [path.name for path in obsolete_rollbacks],
        "entries": entries,
    }
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for digest, source in sorted(objects.items()):
                archive.write(source, f"objects/{digest[:2]}/{digest}")
            for backup in obsolete_rollbacks:
                manifest = backup / "manifest.json"
                if manifest.is_file() and not manifest.is_symlink():
                    archive.write(manifest, f"source_manifests/{backup.name}/manifest.json")
            archive.writestr("index.json", json.dumps(index, ensure_ascii=False, indent=2))
        _verify_business_archive(temporary)
        temporary.replace(archive_path)
        _verify_business_archive(archive_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)
        raise
    return archive_path


def _verify_business_archive(path: str | Path) -> dict[str, object]:
    archive_path = Path(path)
    with zipfile.ZipFile(archive_path, "r") as archive:
        try:
            index = json.loads(archive.read("index.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise WorkspaceRetentionError(f"业务历史归档索引无效：{archive_path}") from exc
        if index.get("schema") != ARCHIVE_SCHEMA or index.get("version") != ARCHIVE_VERSION:
            raise WorkspaceRetentionError(f"业务历史归档版本无效：{archive_path}")
        verified: set[str] = set()
        for entry in index.get("entries", []):
            if not isinstance(entry, dict):
                raise WorkspaceRetentionError(f"业务历史归档条目无效：{archive_path}")
            object_name = str(entry.get("object") or "")
            expected_hash = str(entry.get("sha256") or "")
            if object_name in verified:
                continue
            try:
                with archive.open(object_name, "r") as handle:
                    digest, size = _sha256_stream(handle)
            except KeyError as exc:
                raise WorkspaceRetentionError(f"业务历史归档缺少对象：{object_name}") from exc
            expected_size = entry.get("size")
            if not isinstance(expected_size, int):
                raise WorkspaceRetentionError(f"业务历史归档大小字段无效：{object_name}")
            if digest != expected_hash or size != expected_size:
                raise WorkspaceRetentionError(f"业务历史归档校验失败：{object_name}")
            verified.add(object_name)
    return index


def _actions_by_category(plan: CleanupPlan, category: str) -> tuple[Path, ...]:
    return tuple(Path(action.path) for action in plan.actions if action.category == category)


def _validate_actions(plan: CleanupPlan) -> None:
    workspace = Path(plan.workspace).resolve(strict=True)
    allowed = {
        "build": workspace,
        "root_generated_output": workspace,
        "generated_output": workspace / "outputs",
        "obsolete_full_rollback": workspace / "rollback_backups",
        "browser_cache": workspace / "browser_profile",
    }
    for action in plan.actions:
        path = Path(action.path)
        parent = allowed.get(action.category)
        if parent is None or not path.exists():
            raise WorkspaceRetentionError(f"清理计划在执行前发生变化：{path}")
        _ensure_safe_tree(workspace, path, parent)


def apply_cleanup_plan(
    plan: CleanupPlan,
    *,
    expected_exe_sha256: str | None = None,
) -> CleanupResult:
    workspace = Path(plan.workspace).resolve(strict=True)
    active = _find_active_workspace_processes(workspace)
    if active:
        raise WorkspaceRetentionError(f"请先退出桌面程序或自动化浏览器：{', '.join(active)}")
    _validate_actions(plan)
    sqlite_before = _check_sqlite_integrity(workspace)
    # SQLite integrity checks can legitimately create, resize, or remove WAL/SHM
    # sidecars.  Snapshot protected files only after that read has stabilized
    # the database so the cleanup guard does not reject its own housekeeping.
    baseline = _snapshot_protected_files(workspace)
    exe_key = "dist/ERP自动化/ERP自动化.exe"
    if expected_exe_sha256:
        actual = str((baseline.get(exe_key) or {}).get("sha256") or "")
        if actual.upper() != expected_exe_sha256.strip().upper():
            raise WorkspaceRetentionError("当前正式 EXE 哈希与发布期望值不一致，拒绝清理")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    deletion_roots = tuple(Path(action.path) for action in plan.actions)
    archive_path = _archive_business_history(workspace, deletion_roots, timestamp=timestamp)
    if _snapshot_protected_files(workspace) != baseline:
        raise WorkspaceRetentionError("归档期间业务文件或当前 EXE 发生变化，拒绝删除")

    reclaimed = 0
    deleted: list[dict[str, object]] = []
    for action in plan.actions:
        path = Path(action.path)
        if not path.exists():
            continue
        shutil.rmtree(path)
        reclaimed += action.bytes
        deleted.append(asdict(action))

    after = _snapshot_protected_files(workspace)
    if after != baseline:
        raise WorkspaceRetentionError("清理后业务文件或当前 EXE 哈希发生变化")
    sqlite_after = _check_sqlite_integrity(workspace)

    report_root = workspace / "rollback_backups" / "retention_reports"
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"cleanup_{timestamp}.json"
    report = {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "kept_full_rollbacks": list(plan.kept_full_rollbacks),
        "business_archive": str(archive_path) if archive_path else None,
        "protected_files_before": baseline,
        "protected_files_after": after,
        "sqlite_integrity_before": sqlite_before,
        "sqlite_integrity_after": sqlite_after,
        "deleted": deleted,
        "reclaimed_bytes": reclaimed,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return CleanupResult(
        applied=True,
        plan=plan,
        archive_path=str(archive_path) if archive_path else None,
        report_path=str(report_path),
        reclaimed_bytes=reclaimed,
    )


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def _print_plan(plan: CleanupPlan) -> None:
    print("空间维护预览（未执行删除）")
    print(f"工作区：{plan.workspace}")
    print(f"保留完整回滚：{', '.join(plan.kept_full_rollbacks) or '无'}")
    print(f"待归档业务文件：{plan.business_file_count}")
    for action in plan.actions:
        print(f"- [{action.category}] {_human_size(action.bytes):>10}  {action.path}")
    print(f"预计释放：{_human_size(plan.estimated_reclaim_bytes)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全清理 ERP 自动化发布与构建产物")
    parser.add_argument("--workspace", default=".", help="项目工作区，默认当前目录")
    parser.add_argument("--apply", action="store_true", help="执行清理；省略时仅预览")
    parser.add_argument(
        "--keep-full-rollbacks",
        type=int,
        default=DEFAULT_KEEP_FULL_ROLLBACKS,
        help="自动保留的完整发布回滚数量，默认 2",
    )
    parser.add_argument(
        "--keep-rollback",
        action="append",
        default=[],
        help="显式保留的完整回滚目录名，可重复传入",
    )
    parser.add_argument("--skip-browser-cache", action="store_true", help="不清理安全浏览器缓存")
    parser.add_argument("--expected-exe-sha256", help="执行前必须匹配的正式 EXE SHA256")
    args = parser.parse_args(argv)
    try:
        plan = build_cleanup_plan(
            args.workspace,
            keep_full_rollbacks=args.keep_full_rollbacks,
            keep_rollback_names=args.keep_rollback,
            include_browser_cache=not args.skip_browser_cache,
        )
        _print_plan(plan)
        if not args.apply:
            return 0
        result = apply_cleanup_plan(plan, expected_exe_sha256=args.expected_exe_sha256)
        print(f"清理完成，实际释放：{_human_size(result.reclaimed_bytes)}")
        if result.archive_path:
            print(f"业务历史归档：{result.archive_path}")
        print(f"清理报告：{result.report_path}")
        return 0
    except WorkspaceRetentionError as exc:
        print(f"安全清理已停止：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CleanupAction",
    "CleanupPlan",
    "CleanupResult",
    "WorkspaceRetentionError",
    "apply_cleanup_plan",
    "build_cleanup_plan",
]
