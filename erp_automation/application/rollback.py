"""Data snapshot support for an operator-requested Git baseline rollback."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCRIPT_BASELINE_BRANCH = "codex/script-baseline-20260714"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RollbackManager:
    """Create and restore data snapshots; it never launches the old scripts.

    The old code lives only in ``SCRIPT_BASELINE_BRANCH``.  If the user asks
    Codex to roll back, code is restored through Git and this snapshot restores
    mutable SQLite/JSON/rule files.  The normal desktop runtime therefore does
    not maintain or expose two competing execution paths.
    """

    def __init__(self, workspace: str | Path, backup_root: str | Path):
        self.workspace = Path(workspace).resolve()
        self.backup_root = Path(backup_root).resolve()

    def _source(self, value: str | Path) -> tuple[Path, Path]:
        raw = Path(value)
        source = raw.resolve() if raw.is_absolute() else (self.workspace / raw).resolve()
        try:
            relative = source.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("回退快照只能包含工作区内文件。") from exc
        if source.is_symlink():
            raise ValueError("回退快照不接受符号链接。")
        return source, relative

    def create_snapshot(self, paths: Iterable[str | Path], *, reason: str) -> Path:
        if not str(reason or "").strip():
            raise ValueError("回退快照必须填写原因。")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination = self.backup_root / f"rollback_{timestamp}"
        destination.mkdir(parents=True, exist_ok=False)
        files: list[dict[str, object]] = []
        for value in paths:
            source, relative = self._source(value)
            if not source.exists():
                files.append({"path": relative.as_posix(), "exists": False})
                continue
            if not source.is_file():
                raise ValueError(f"回退快照只接受普通文件：{relative.as_posix()}")
            target = destination / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            files.append(
                {
                    "path": relative.as_posix(),
                    "exists": True,
                    "size": source.stat().st_size,
                    "sha256": _sha256(target),
                }
            )
        manifest: dict[str, object] = {
            "schema": "erp-automation.rollback-snapshot",
            "version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "reason": str(reason).strip(),
            "script_baseline_branch": SCRIPT_BASELINE_BRANCH,
            "files": files,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination

    def restore_snapshot(self, snapshot: str | Path) -> tuple[Path, ...]:
        """Validate all files first, then restore with one-generation backups."""

        snapshot_path = Path(snapshot).resolve()
        try:
            snapshot_path.relative_to(self.backup_root)
        except ValueError as exc:
            raise ValueError("快照不属于配置的回退目录。") from exc
        manifest_path = snapshot_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != "erp-automation.rollback-snapshot" or manifest.get("version") != 1:
            raise ValueError("回退快照格式无效。")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ValueError("回退快照文件清单无效。")

        validated: list[tuple[Path, Path | None]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("回退快照文件项无效。")
            relative = Path(str(entry.get("path") or ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("回退快照包含不安全路径。")
            target = (self.workspace / relative).resolve()
            try:
                target.relative_to(self.workspace)
            except ValueError as exc:
                raise ValueError("回退目标路径逃逸工作区。") from exc
            source = snapshot_path / "files" / relative if entry.get("exists") else None
            if source is not None:
                if not source.is_file() or _sha256(source) != entry.get("sha256"):
                    raise ValueError("回退快照校验失败，未修改任何业务文件。")
            validated.append((target, source))

        restored: list[Path] = []
        for target, source in validated:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.is_file():
                backup = target.with_name(f"{target.name}.pre_restore.bak")
                shutil.copy2(target, backup)
            if source is None:
                target.unlink(missing_ok=True)
            else:
                temporary = target.with_name(f".{target.name}.restore.tmp")
                shutil.copy2(source, temporary)
                temporary.replace(target)
            restored.append(target)
        return tuple(restored)


__all__ = ["RollbackManager", "SCRIPT_BASELINE_BRANCH"]
