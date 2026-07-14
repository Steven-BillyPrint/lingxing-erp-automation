from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from lingxing_automation.storage.dedupe_schema import (
    CONTACT_WRITEBACK_COMPLETE_KEY,
    FOLDER_COMPLETE_KEY,
    INSTRUCTION_REMARK_COMPLETE_KEY,
    INSTRUCTION_REMARK_REQUIRED_KEY,
    ORDERS_KEY,
    PACKAGE_SPLIT_COMPLETE_KEY,
    PACKAGE_SPLIT_REQUIRED_KEY,
    SKU_ADJUSTMENT_COMPLETE_KEY,
    SKU_ADJUSTMENT_REQUIRED_KEY,
    normalize_bool,
)


STAGE_ORDER = ("contact", "folder", "sku", "package_split", "instruction_remark")
STAGE_KEYS = {
    "contact": (None, CONTACT_WRITEBACK_COMPLETE_KEY, "contact_status", "contact_completed_at"),
    "folder": (None, FOLDER_COMPLETE_KEY, "folder_status", "folder_completed_at"),
    "sku": (
        SKU_ADJUSTMENT_REQUIRED_KEY,
        SKU_ADJUSTMENT_COMPLETE_KEY,
        "sku_adjustment_status",
        "sku_adjustment_completed_at",
    ),
    "package_split": (
        PACKAGE_SPLIT_REQUIRED_KEY,
        PACKAGE_SPLIT_COMPLETE_KEY,
        "package_split_status",
        "package_split_completed_at",
    ),
    "instruction_remark": (
        INSTRUCTION_REMARK_REQUIRED_KEY,
        INSTRUCTION_REMARK_COMPLETE_KEY,
        "instruction_remark_status",
        "instruction_remark_completed_at",
    ),
}


class WorkflowStageState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ImportResult:
    source_sha256: str
    source_count: int
    imported_count: int
    skipped: bool = False
    backup_path: Path | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _truth(value: Any) -> bool:
    return normalize_bool(value)


class CustomWorkflowStore:
    """定制订单 SQLite 状态库，并保持旧 JSON 可回退兼容。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(
                    """
                CREATE TABLE IF NOT EXISTS custom_order_workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_order_no TEXT NOT NULL UNIQUE,
                    original_system_order_no TEXT,
                    product_type TEXT,
                    workflow_status TEXT NOT NULL,
                    ignored INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0, 1)),
                    last_seen_at TEXT,
                    processed_at TEXT,
                    source_record_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS custom_order_stages (
                    workflow_id INTEGER NOT NULL REFERENCES custom_order_workflows(id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    required INTEGER CHECK (required IS NULL OR required IN (0, 1)),
                    state TEXT NOT NULL,
                    result_status TEXT,
                    completed_at TEXT,
                    last_error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (workflow_id, stage)
                );

                CREATE TABLE IF NOT EXISTS custom_order_system_orders (
                    workflow_id INTEGER NOT NULL REFERENCES custom_order_workflows(id) ON DELETE CASCADE,
                    system_order_no TEXT NOT NULL,
                    role TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (workflow_id, system_order_no, role)
                );

                CREATE TABLE IF NOT EXISTS custom_order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id INTEGER NOT NULL REFERENCES custom_order_workflows(id) ON DELETE CASCADE,
                    stage TEXT,
                    event_type TEXT NOT NULL,
                    old_state TEXT,
                    new_state TEXT,
                    actor TEXT NOT NULL,
                    reason TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_import_runs (
                    source_sha256 TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_version INTEGER,
                    record_count INTEGER NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_custom_workflow_status
                    ON custom_order_workflows(workflow_status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_custom_events_workflow
                    ON custom_order_events(workflow_id, id);
                    """
                )
            self._initialized = True

    def import_legacy_json(
        self,
        source: str | Path,
        *,
        create_backup: bool = True,
        overwrite_existing: bool = False,
    ) -> ImportResult:
        """事务性导入旧 processed JSON；同一内容重复执行不会重复写入。"""

        self.initialize()
        source_path = Path(source)
        raw = source_path.read_bytes() if source_path.exists() else b""
        digest = hashlib.sha256(raw).hexdigest()
        # Imported lazily so the SQLite runtime has no import cycle back through
        # the public dedupe facade used to select this backend.
        from lingxing_automation.storage import dedupe as legacy_dedupe

        payload = legacy_dedupe._load_raw_payload(source_path)
        orders = payload.get(ORDERS_KEY) or {}
        if not isinstance(orders, dict):
            orders = {}

        with self.connect() as conn:
            prior = conn.execute(
                "SELECT 1 FROM workflow_import_runs WHERE source_sha256 = ?",
                (digest,),
            ).fetchone()
        if prior:
            return ImportResult(digest, len(orders), 0, skipped=True)

        backup_path: Path | None = None
        if create_backup and source_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = source_path.with_name(f"{source_path.name}.pre_sqlite_{timestamp}.bak")
            shutil.copy2(source_path, backup_path)

        imported = 0
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for platform_order_no, value in orders.items():
                if not isinstance(value, dict):
                    continue
                existing = conn.execute(
                    "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
                    (str(platform_order_no),),
                ).fetchone()
                if existing and not overwrite_existing:
                    continue
                self._upsert_legacy_record(conn, str(platform_order_no), value, now=now)
                imported += 1
            conn.execute(
                """
                INSERT INTO workflow_import_runs(
                    source_sha256, source_path, source_version, record_count, imported_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (digest, str(source_path.resolve()), int(payload.get("version") or 0), len(orders), now),
            )
            conn.commit()
        return ImportResult(digest, len(orders), imported, backup_path=backup_path)

    def _upsert_legacy_record(
        self,
        conn: sqlite3.Connection,
        platform_order_no: str,
        record: dict[str, Any],
        *,
        now: str,
    ) -> int:
        status = str(record.get("workflow_status") or "pending")
        conn.execute(
            """
            INSERT INTO custom_order_workflows(
                platform_order_no, original_system_order_no, product_type, workflow_status,
                last_seen_at, processed_at, source_record_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform_order_no) DO UPDATE SET
                original_system_order_no = excluded.original_system_order_no,
                product_type = excluded.product_type,
                workflow_status = excluded.workflow_status,
                last_seen_at = excluded.last_seen_at,
                processed_at = excluded.processed_at,
                source_record_json = excluded.source_record_json,
                version = custom_order_workflows.version + 1,
                updated_at = excluded.updated_at
            """,
            (
                platform_order_no,
                record.get("system_order_no"),
                record.get("product_type"),
                status,
                record.get("last_seen_at"),
                record.get("processed_at"),
                _json(record),
                now,
                now,
            ),
        )
        workflow_id = int(
            conn.execute(
                "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()[0]
        )
        conn.execute("DELETE FROM custom_order_stages WHERE workflow_id = ?", (workflow_id,))
        conn.execute("DELETE FROM custom_order_system_orders WHERE workflow_id = ?", (workflow_id,))
        for stage in STAGE_ORDER:
            required_key, complete_key, status_key, completed_key = STAGE_KEYS[stage]
            required: bool | None
            if required_key is None:
                required = True
            elif required_key in record:
                required = _truth(record.get(required_key))
            else:
                required = None
            complete = _truth(record.get(complete_key))
            if complete:
                state = WorkflowStageState.COMPLETED
            elif required is True:
                state = WorkflowStageState.PENDING
            elif required is False:
                state = WorkflowStageState.NOT_REQUIRED
            else:
                state = WorkflowStageState.NOT_APPLICABLE
            conn.execute(
                """
                INSERT INTO custom_order_stages(
                    workflow_id, stage, required, state, result_status, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    stage,
                    None if required is None else int(required),
                    str(state),
                    record.get(status_key),
                    record.get(completed_key),
                ),
            )

        system_order_no = str(record.get("system_order_no") or "").strip()
        if system_order_no:
            self._insert_system_order(conn, workflow_id, system_order_no, "original", 0)
        for index, value in enumerate(record.get("package_split_system_order_nos") or []):
            text = str(value or "").strip()
            if text:
                self._insert_system_order(conn, workflow_id, text, "package_split", index)
        remark_target = str(record.get("instruction_remark_target_system_order_no") or "").strip()
        if remark_target:
            self._insert_system_order(conn, workflow_id, remark_target, "instruction_remark_target", 0)
        return workflow_id

    @staticmethod
    def _insert_system_order(
        conn: sqlite3.Connection,
        workflow_id: int,
        system_order_no: str,
        role: str,
        sequence_no: int,
    ) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO custom_order_system_orders(
                workflow_id, system_order_no, role, sequence_no
            ) VALUES (?, ?, ?, ?)
            """,
            (workflow_id, system_order_no, role, sequence_no),
        )

    def _legacy_record_from_row(
        self,
        conn: sqlite3.Connection,
        workflow: sqlite3.Row,
    ) -> dict[str, Any]:
        """Rebuild the v3 record represented by one normalized workflow row."""

        try:
            source_record = json.loads(workflow["source_record_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            source_record = {}
        record = dict(source_record) if isinstance(source_record, dict) else {}
        record.update(
            {
                "platform_order_no": workflow["platform_order_no"],
                "system_order_no": workflow["original_system_order_no"],
                "product_type": workflow["product_type"],
                "workflow_status": workflow["workflow_status"],
                "last_seen_at": workflow["last_seen_at"],
                "processed_at": workflow["processed_at"],
            }
        )
        stage_rows = conn.execute(
            "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
            (workflow["id"],),
        ).fetchall()
        for stage_row in stage_rows:
            stage = str(stage_row["stage"])
            required_key, complete_key, status_key, completed_key = STAGE_KEYS[stage]
            if required_key:
                if stage_row["required"] is None:
                    record.pop(required_key, None)
                else:
                    record[required_key] = bool(stage_row["required"])
            record[complete_key] = stage_row["state"] == str(WorkflowStageState.COMPLETED)
            if stage_row["result_status"] is not None:
                record[status_key] = stage_row["result_status"]
            if stage_row["completed_at"] is not None:
                record[completed_key] = stage_row["completed_at"]

        system_rows = conn.execute(
            """
            SELECT * FROM custom_order_system_orders
            WHERE workflow_id = ? ORDER BY role, sequence_no
            """,
            (workflow["id"],),
        ).fetchall()
        split_orders = [
            row["system_order_no"] for row in system_rows if row["role"] == "package_split"
        ]
        if split_orders:
            record["package_split_system_order_nos"] = split_orders
        else:
            record.pop("package_split_system_order_nos", None)
        remark_target = next(
            (
                row["system_order_no"]
                for row in system_rows
                if row["role"] == "instruction_remark_target"
            ),
            None,
        )
        if remark_target:
            record["instruction_remark_target_system_order_no"] = remark_target
        else:
            record.pop("instruction_remark_target_system_order_no", None)
        return record

    def get_legacy_record(self, platform_order_no: str) -> dict[str, Any] | None:
        """Return one workflow using the legacy v3 record shape."""

        self.initialize()
        with self.connect() as conn:
            workflow = conn.execute(
                "SELECT * FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            if workflow is None:
                return None
            return self._legacy_record_from_row(conn, workflow)

    def mutate_legacy_record(
        self,
        platform_order_no: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        event_type: str,
        stage: str | None = None,
        actor: str = "automation",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Atomically read, merge and persist one legacy-shaped workflow record.

        The immediate transaction is important here: several workflow stages can
        finish from different workers, and each update must merge the latest row
        instead of overwriting a stage committed by another worker.
        """

        if stage is not None and stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                "SELECT * FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            old_record = self._legacy_record_from_row(conn, workflow) if workflow else {}
            if stage and workflow:
                old_state_row = conn.execute(
                    """
                    SELECT state FROM custom_order_stages
                    WHERE workflow_id = ? AND stage = ?
                    """,
                    (workflow["id"], stage),
                ).fetchone()
                old_state = str(old_state_row[0]) if old_state_row else None
            else:
                old_state = str(workflow["workflow_status"]) if workflow else None

            updated = mutator(dict(old_record))
            if not isinstance(updated, dict):
                raise TypeError("工作流记录更新器必须返回字典。")
            updated["platform_order_no"] = platform_order_no
            workflow_id = self._upsert_legacy_record(
                conn,
                platform_order_no,
                updated,
                now=now,
            )
            if stage:
                new_state_row = conn.execute(
                    """
                    SELECT state FROM custom_order_stages
                    WHERE workflow_id = ? AND stage = ?
                    """,
                    (workflow_id, stage),
                ).fetchone()
                new_state = str(new_state_row[0]) if new_state_row else None
            else:
                new_state = str(updated.get("workflow_status") or "pending")
            changed_fields = sorted(
                key
                for key in set(old_record) | set(updated)
                if old_record.get(key) != updated.get(key)
            )
            self._insert_event(
                conn,
                workflow_id,
                stage=stage,
                event_type=event_type,
                old_state=old_state,
                new_state=new_state,
                actor=actor,
                reason=reason,
                details={"changed_fields": changed_fields},
            )
            conn.commit()
        return updated

    def completed_platform_orders_for_stages(self, *stages: str) -> set[str]:
        """Return orders whose state is COMPLETED for any requested stage."""

        if not stages:
            return set()
        unknown = set(stages) - set(STAGE_ORDER)
        if unknown:
            raise ValueError(f"未知阶段：{', '.join(sorted(unknown))}")
        self.initialize()
        placeholders = ", ".join("?" for _ in stages)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT w.platform_order_no
                FROM custom_order_workflows w
                JOIN custom_order_stages s ON s.workflow_id = w.id
                WHERE s.stage IN ({placeholders}) AND s.state = ?
                """,
                (*stages, str(WorkflowStageState.COMPLETED)),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def is_stage_completed(self, platform_order_no: str, stage: str) -> bool:
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM custom_order_stages s
                JOIN custom_order_workflows w ON w.id = s.workflow_id
                WHERE w.platform_order_no = ? AND s.stage = ? AND s.state = ?
                """,
                (platform_order_no, stage, str(WorkflowStageState.COMPLETED)),
            ).fetchone()
        return row is not None

    def list_workflows(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("w.workflow_status = ?")
            params.append(status)
        if search:
            clauses.append(
                "(w.platform_order_no LIKE ? OR w.original_system_order_no LIKE ? "
                "OR EXISTS (SELECT 1 FROM custom_order_system_orders s "
                "WHERE s.workflow_id = w.id AND s.system_order_no LIKE ?))"
            )
            pattern = f"%{search.strip()}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT w.* FROM custom_order_workflows w
                {where}
                ORDER BY w.updated_at DESC, w.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_workflow(self, platform_order_no: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            if row is None:
                return None
            stages = conn.execute(
                "SELECT * FROM custom_order_stages WHERE workflow_id = ? ORDER BY rowid",
                (row["id"],),
            ).fetchall()
            system_orders = conn.execute(
                "SELECT * FROM custom_order_system_orders WHERE workflow_id = ? ORDER BY role, sequence_no",
                (row["id"],),
            ).fetchall()
        result = dict(row)
        result["stages"] = [dict(item) for item in stages]
        result["system_orders"] = [dict(item) for item in system_orders]
        return result

    def processed_platform_orders(self) -> set[str]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT platform_order_no FROM custom_order_workflows
                WHERE workflow_status = 'completed' AND ignored = 0
                """
            ).fetchall()
        return {str(row[0]) for row in rows}

    def set_stage_state(
        self,
        platform_order_no: str,
        stage: str,
        state: WorkflowStageState | str,
        *,
        reason: str,
        actor: str = "user",
        result_status: str | None = None,
        last_error: str | None = None,
    ) -> None:
        """修改一个阶段；所有人工修改都要求原因并写入事件。"""

        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        if not str(reason or "").strip():
            raise ValueError("修改工作流状态必须填写原因。")
        new_state = WorkflowStageState(state)
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            if workflow is None:
                raise KeyError(platform_order_no)
            workflow_id = int(workflow[0])
            current = conn.execute(
                "SELECT state FROM custom_order_stages WHERE workflow_id = ? AND stage = ?",
                (workflow_id, stage),
            ).fetchone()
            old_state = str(current[0]) if current else None
            required = None if new_state == WorkflowStageState.NOT_APPLICABLE else int(
                new_state != WorkflowStageState.NOT_REQUIRED
            )
            completed_at = now if new_state == WorkflowStageState.COMPLETED else None
            conn.execute(
                """
                INSERT INTO custom_order_stages(
                    workflow_id, stage, required, state, result_status, completed_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, stage) DO UPDATE SET
                    required = excluded.required,
                    state = excluded.state,
                    result_status = excluded.result_status,
                    completed_at = excluded.completed_at,
                    last_error = excluded.last_error
                """,
                (
                    workflow_id,
                    stage,
                    required,
                    str(new_state),
                    result_status,
                    completed_at,
                    str(last_error or "").strip() or None,
                ),
            )
            self._insert_event(
                conn,
                workflow_id,
                stage=stage,
                event_type="stage_state_changed",
                old_state=old_state,
                new_state=str(new_state),
                actor=actor,
                reason=reason,
            )
            self._refresh_workflow_status(conn, workflow_id, now=now)
            conn.commit()

    def reopen_from_stage(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
        actor: str = "user",
    ) -> None:
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        if not str(reason or "").strip():
            raise ValueError("重新打开工作流必须填写原因。")
        self.initialize()
        now = utc_now()
        start = STAGE_ORDER.index(stage)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            if workflow is None:
                raise KeyError(platform_order_no)
            workflow_id = int(workflow[0])
            for current_stage in STAGE_ORDER[start:]:
                current = conn.execute(
                    "SELECT required, state FROM custom_order_stages WHERE workflow_id = ? AND stage = ?",
                    (workflow_id, current_stage),
                ).fetchone()
                if current is None or current[0] is None:
                    continue
                old_state = str(current[1])
                conn.execute(
                    """
                    UPDATE custom_order_stages
                    SET state = 'PENDING', completed_at = NULL, last_error = NULL
                    WHERE workflow_id = ? AND stage = ?
                    """,
                    (workflow_id, current_stage),
                )
                self._insert_event(
                    conn,
                    workflow_id,
                    stage=current_stage,
                    event_type="stage_reopened",
                    old_state=old_state,
                    new_state=str(WorkflowStageState.PENDING),
                    actor=actor,
                    reason=reason,
                )
            self._refresh_workflow_status(conn, workflow_id, now=now)
            conn.commit()

    def history(self, platform_order_no: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.* FROM custom_order_events e
                JOIN custom_order_workflows w ON w.id = e.workflow_id
                WHERE w.platform_order_no = ? ORDER BY e.id
                """,
                (platform_order_no,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        workflow_id: int,
        *,
        stage: str | None,
        event_type: str,
        old_state: str | None,
        new_state: str | None,
        actor: str,
        reason: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO custom_order_events(
                workflow_id, stage, event_type, old_state, new_state,
                actor, reason, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                stage,
                event_type,
                old_state,
                new_state,
                actor,
                reason,
                _json(details or {}),
                utc_now(),
            ),
        )

    def _refresh_workflow_status(self, conn: sqlite3.Connection, workflow_id: int, *, now: str) -> None:
        stages = {
            str(row["stage"]): row
            for row in conn.execute(
                "SELECT stage, required, state FROM custom_order_stages WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchall()
        }
        def completed(name: str) -> bool:
            row = stages.get(name)
            if not row:
                return False
            return row["state"] in {
                str(WorkflowStageState.COMPLETED),
                str(WorkflowStageState.NOT_REQUIRED),
                str(WorkflowStageState.NOT_APPLICABLE),
            }

        if any(
            row["state"] == str(WorkflowStageState.BLOCKED)
            for row in stages.values()
        ):
            status = "blocked"
        elif all(completed(stage) for stage in STAGE_ORDER):
            status = "completed"
        elif completed("folder") and not completed("sku"):
            status = "sku_adjustment_pending"
        elif completed("folder") and not completed("package_split"):
            status = "package_split_pending"
        elif completed("folder") and not completed("instruction_remark"):
            status = "instruction_remark_pending"
        elif completed("folder"):
            status = "folder_complete"
        elif completed("contact"):
            status = "contact_writeback_complete"
        else:
            status = "pending"
        conn.execute(
            """
            UPDATE custom_order_workflows
            SET workflow_status = ?, version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (status, now, workflow_id),
        )

    def export_legacy_json(self, target: str | Path) -> Path:
        """原子导出 v3 JSON，保证新程序严重故障时旧脚本可继续运行。"""

        self.initialize()
        orders: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            workflows = conn.execute("SELECT * FROM custom_order_workflows ORDER BY id").fetchall()
            for workflow in workflows:
                record = self._legacy_record_from_row(conn, workflow)
                orders[str(workflow["platform_order_no"])] = record
        payload = {"version": 3, "updated_at": utc_now(), "orders": orders}
        target_path = Path(target)
        _atomic_write_json(target_path, payload)
        return target_path


def stage_state_counts(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    output: dict[str, int] = {}
    for record in records:
        status = str(record.get("workflow_status") or "pending")
        output[status] = output.get(status, 0) + 1
    return output
