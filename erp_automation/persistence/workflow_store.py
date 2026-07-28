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
from typing import Any, Callable, Iterable, Mapping

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
    WAREHOUSE_LOGISTICS_COMPLETE_KEY,
    WAREHOUSE_LOGISTICS_REQUIRED_KEY,
    normalize_bool,
)


STAGE_ORDER = (
    "contact",
    "folder",
    "sku",
    "package_split",
    "instruction_remark",
    "warehouse_logistics",
)
STAGE_PENDING_STATUS = {
    "contact": "pending",
    "folder": "folder_pending",
    "sku": "sku_adjustment_pending",
    "package_split": "package_split_pending",
    "instruction_remark": "instruction_remark_pending",
    "warehouse_logistics": "warehouse_logistics_pending",
}
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
    "warehouse_logistics": (
        WAREHOUSE_LOGISTICS_REQUIRED_KEY,
        WAREHOUSE_LOGISTICS_COMPLETE_KEY,
        "warehouse_logistics_status",
        "warehouse_logistics_completed_at",
    ),
}


class WorkflowStageState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"


class WorkflowPauseKind(StrEnum):
    USER_CANCELLED = "user_cancelled"
    EMERGENCY_STOP = "emergency_stop"
    RETRYABLE_FAILURE = "retryable_failure"
    AMBIGUOUS_WRITE = "ambiguous_write"


class StageRetryReviewResolution(StrEnum):
    RETRY = "not_executed_retry"
    COMPLETED = "already_completed"


@dataclass(frozen=True)
class WorkflowPauseRecord:
    stage: str
    pause_kind: WorkflowPauseKind
    retry_confirmation_required: bool
    workflow_status: str


@dataclass(frozen=True)
class ImportResult:
    source_sha256: str
    source_count: int
    imported_count: int
    skipped: bool = False
    backup_path: Path | None = None


@dataclass(frozen=True)
class ManualCompletionSummary:
    requested_count: int
    completed_count: int
    already_completed_count: int
    changed_stage_count: int


@dataclass(frozen=True)
class BatchWorkflowMutationSummary:
    requested_count: int
    changed_order_count: int
    unchanged_order_count: int
    changed_stage_count: int


@dataclass(frozen=True)
class WorkflowNotRequiredSummary:
    requested_count: int
    changed_order_count: int
    already_terminal_count: int
    missing_count: int
    changed_stage_count: int


@dataclass(frozen=True)
class BuyerCancelReactivationSummary:
    requested_count: int
    clear_observed_order_nos: tuple[str, ...]
    reactivated_order_nos: tuple[str, ...]
    reset_order_nos: tuple[str, ...]

    @property
    def clear_observed_count(self) -> int:
        return len(self.clear_observed_order_nos)

    @property
    def reactivated_count(self) -> int:
        return len(self.reactivated_order_nos)

    @property
    def reset_count(self) -> int:
        return len(self.reset_order_nos)


@dataclass(frozen=True)
class MissingCandidateFolderReconciliationSummary:
    requested_count: int
    completed_count: int
    pending_count: int
    changed_order_count: int
    already_terminal_count: int
    missing_count: int
    changed_stage_count: int
    error_preserved_count: int = 0


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
                    not_required_reason TEXT,
                    buyer_cancel_clear_streak INTEGER NOT NULL DEFAULT 0
                        CHECK (buyer_cancel_clear_streak >= 0),
                    buyer_cancel_clear_last_scan_id TEXT,
                    buyer_cancel_clear_last_seen_at TEXT,
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
                workflow_columns = {
                    str(row["name"])
                    for row in conn.execute("PRAGMA table_info(custom_order_workflows)")
                }
                migrations = (
                    ("not_required_reason", "TEXT"),
                    (
                        "buyer_cancel_clear_streak",
                        "INTEGER NOT NULL DEFAULT 0 CHECK (buyer_cancel_clear_streak >= 0)",
                    ),
                    ("buyer_cancel_clear_last_scan_id", "TEXT"),
                    ("buyer_cancel_clear_last_seen_at", "TEXT"),
                )
                for column, definition in migrations:
                    if column not in workflow_columns:
                        conn.execute(
                            f"ALTER TABLE custom_order_workflows ADD COLUMN {column} {definition}"
                        )
                # v4 adds the warehouse/logistics stage. Existing workflows are
                # deliberately NOT_APPLICABLE so historical completed orders do
                # not reopen merely because the application was upgraded. An
                # unfinished tent workflow promotes this stage to required when
                # its package/instruction record is next mutated.
                conn.execute(
                    """
                    INSERT INTO custom_order_stages(
                        workflow_id, stage, required, state, metadata_json
                    )
                    SELECT id, 'warehouse_logistics', NULL, 'NOT_APPLICABLE', '{}'
                    FROM custom_order_workflows w
                    WHERE NOT EXISTS (
                        SELECT 1 FROM custom_order_stages s
                        WHERE s.workflow_id = w.id
                          AND s.stage = 'warehouse_logistics'
                    )
                    """
                )
                # Older databases recorded the cancellation reason only on the
                # affected stages.  Backfill it once so those terminal records
                # can participate in the conservative automatic reactivation.
                conn.execute(
                    """
                    UPDATE custom_order_workflows
                    SET not_required_reason = 'buyer_cancel_requested'
                    WHERE workflow_status = 'not_required'
                      AND (not_required_reason IS NULL OR TRIM(not_required_reason) = '')
                      AND EXISTS (
                          SELECT 1 FROM custom_order_stages s
                          WHERE s.workflow_id = custom_order_workflows.id
                            AND s.result_status = 'buyer_cancel_requested'
                      )
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

    def list_active_scanned_workflows(self) -> list[dict[str, Any]]:
        """Return non-terminal workflows that originally entered through a scan.

        ``last_seen_at`` is written when an API candidate first enters the
        custom-order queue.  It also anchors the small payment-month folder
        search used when that order disappears from a later complete snapshot.
        """

        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.id AS workflow_id,
                    w.platform_order_no,
                    w.original_system_order_no,
                    w.workflow_status,
                    w.last_seen_at,
                    w.created_at,
                    s.stage,
                    s.state AS stage_state,
                    s.last_error AS stage_last_error,
                    s.metadata_json AS stage_metadata_json
                FROM custom_order_workflows w
                JOIN custom_order_stages s ON s.workflow_id = w.id
                WHERE w.ignored = 0
                  AND w.last_seen_at IS NOT NULL
                  AND TRIM(w.last_seen_at) <> ''
                  AND w.workflow_status NOT IN ('completed', 'not_required', 'cancelled')
                ORDER BY w.id, s.rowid
                """
            ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        stages_by_workflow: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            workflow_id = int(row["workflow_id"])
            grouped.setdefault(
                workflow_id,
                {
                    "platform_order_no": row["platform_order_no"],
                    "original_system_order_no": row["original_system_order_no"],
                    "workflow_status": row["workflow_status"],
                    "last_seen_at": row["last_seen_at"],
                    "created_at": row["created_at"],
                },
            )
            stages_by_workflow.setdefault(workflow_id, []).append(
                {
                    "state": row["stage_state"],
                    "last_error": row["stage_last_error"],
                    "metadata_json": row["stage_metadata_json"],
                }
            )
        output: list[dict[str, Any]] = []
        for workflow_id, workflow in grouped.items():
            protection_codes = self._folder_reconciliation_protection_codes(
                stages_by_workflow.get(workflow_id, ())
            )
            workflow["folder_reconciliation_protected"] = bool(protection_codes)
            workflow["folder_reconciliation_protection_codes"] = protection_codes
            output.append(workflow)
        return output

    def list_workflow_summaries(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        """Return desktop-list fields and stage errors in one SQLite query.

        The desktop refresh used to call ``get_workflow`` once per visible
        order, opening hundreds of SQLite connections every two seconds.  This
        projection keeps the same information while making refresh cost
        independent of the number of orders.
        """

        self.initialize()
        bounded_limit = max(1, min(int(limit), 5000))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.platform_order_no,
                    w.original_system_order_no,
                    w.product_type,
                    w.workflow_status,
                    w.ignored,
                    w.updated_at,
                    w.source_record_json,
                    COALESCE(
                        GROUP_CONCAT(NULLIF(TRIM(s.last_error), ''), '；'),
                        ''
                    ) AS last_error,
                    MAX(
                        CASE
                            WHEN s.metadata_json LIKE '%"retry_confirmation_required":true%'
                            THEN 1
                            ELSE 0
                        END
                    ) AS retry_confirmation_required
                FROM custom_order_workflows w
                LEFT JOIN custom_order_stages s ON s.workflow_id = w.id
                GROUP BY w.id
                ORDER BY w.updated_at DESC, w.id DESC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            summary = dict(row)
            source_record = self._decode_metadata(summary.pop("source_record_json", None))
            summary["result_detail"] = str(
                source_record.get("warehouse_logistics_result_detail") or ""
            ).strip()
            output.append(summary)
        return output

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

    def backfill_workflow_identity(
        self,
        platform_order_no: str,
        *,
        system_order_no: str = "",
        product_type: str = "",
        actor: str = "api_scanner",
    ) -> bool:
        """Fill missing order identity metadata without changing workflow state.

        Legacy JSON snapshots did not always contain ``product_type``.  API
        scans are allowed to repair that omission, but they must never replace
        an operator-confirmed value or mutate any stage checkpoint.
        """

        order_no = str(platform_order_no or "").strip()
        observed_system_order_no = str(system_order_no or "").strip()
        observed_product_type = str(product_type or "").strip()
        if not order_no or not (observed_system_order_no or observed_product_type):
            return False

        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                "SELECT * FROM custom_order_workflows WHERE platform_order_no = ?",
                (order_no,),
            ).fetchone()
            if workflow is None:
                conn.rollback()
                return False

            changed_fields: list[str] = []
            new_system_order_no = str(workflow["original_system_order_no"] or "").strip()
            new_product_type = str(workflow["product_type"] or "").strip()
            if not new_system_order_no and observed_system_order_no:
                new_system_order_no = observed_system_order_no
                changed_fields.append("system_order_no")
            if not new_product_type and observed_product_type:
                new_product_type = observed_product_type
                changed_fields.append("product_type")
            if not changed_fields:
                conn.rollback()
                return False

            source_record = self._decode_metadata(workflow["source_record_json"])
            if "system_order_no" in changed_fields:
                source_record["system_order_no"] = new_system_order_no
                self._insert_system_order(
                    conn,
                    int(workflow["id"]),
                    new_system_order_no,
                    "original",
                    0,
                )
            if "product_type" in changed_fields:
                source_record["product_type"] = new_product_type
            conn.execute(
                """
                UPDATE custom_order_workflows
                SET original_system_order_no = ?, product_type = ?,
                    source_record_json = ?, version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_system_order_no or None,
                    new_product_type or None,
                    _json(source_record),
                    now,
                    int(workflow["id"]),
                ),
            )
            self._insert_event(
                conn,
                int(workflow["id"]),
                stage=None,
                event_type="workflow_metadata_backfilled",
                old_state=str(workflow["workflow_status"]),
                new_state=str(workflow["workflow_status"]),
                actor=actor,
                reason="Fill identity metadata missing from a legacy workflow record.",
                details={"fields": changed_fields, "source": "api_order_snapshot"},
            )
            conn.commit()
        return True

    def processed_platform_orders(self) -> set[str]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT platform_order_no FROM custom_order_workflows
                WHERE workflow_status IN ('completed', 'not_required', 'cancelled') AND ignored = 0
                """
            ).fetchall()
        return {str(row[0]) for row in rows}

    def buyer_cancel_reactivation_order_nos(self) -> set[str]:
        """Return terminal orders eligible for cancellation-clear observation."""

        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT platform_order_no
                FROM custom_order_workflows
                WHERE workflow_status = 'not_required'
                  AND ignored = 0
                  AND not_required_reason = 'buyer_cancel_requested'
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
        self._reject_automatic_block_if_needed(
            [platform_order_no],
            stage=stage,
            state=new_state,
            actor=actor,
            reason=reason,
        )
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                "SELECT id, source_record_json FROM custom_order_workflows "
                "WHERE platform_order_no = ?",
                (platform_order_no,),
            ).fetchone()
            if workflow is None:
                raise KeyError(platform_order_no)
            workflow_id = int(workflow[0])
            current = conn.execute(
                "SELECT state, metadata_json FROM custom_order_stages WHERE workflow_id = ? AND stage = ?",
                (workflow_id, stage),
            ).fetchone()
            old_state = str(current[0]) if current else None
            metadata_json = _json(
                self._clear_pause_metadata(current["metadata_json"] if current else None)
            )
            required = None if new_state == WorkflowStageState.NOT_APPLICABLE else int(
                new_state != WorkflowStageState.NOT_REQUIRED
            )
            completed_at = now if new_state == WorkflowStageState.COMPLETED else None
            conn.execute(
                """
                INSERT INTO custom_order_stages(
                    workflow_id, stage, required, state, result_status, completed_at,
                    last_error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, stage) DO UPDATE SET
                    required = excluded.required,
                    state = excluded.state,
                    result_status = excluded.result_status,
                    completed_at = excluded.completed_at,
                    last_error = excluded.last_error,
                    metadata_json = excluded.metadata_json
                """,
                (
                    workflow_id,
                    stage,
                    required,
                    str(new_state),
                    result_status,
                    completed_at,
                    str(last_error or "").strip() or None,
                    metadata_json,
                ),
            )
            if (
                stage == "warehouse_logistics"
                and new_state == WorkflowStageState.PENDING
            ):
                conn.execute(
                    """
                    UPDATE custom_order_workflows
                    SET source_record_json = ?
                    WHERE id = ?
                    """,
                    (
                        _json(
                            self._clear_warehouse_result_metadata(
                                workflow["source_record_json"]
                            )
                        ),
                        workflow_id,
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

    def record_workflow_paused(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
        result_status: str | None,
        pause_kind: WorkflowPauseKind | str,
        actor: str = "desktop_worker",
    ) -> WorkflowPauseRecord:
        """Persist an interruption without converting the workflow to BLOCKED."""

        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("记录工作流暂停必须填写原因。")
        kind = WorkflowPauseKind(pause_kind)
        review_required = kind is WorkflowPauseKind.AMBIGUOUS_WRITE
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflow = conn.execute(
                """
                SELECT id, source_record_json
                FROM custom_order_workflows
                WHERE platform_order_no = ?
                """,
                (str(platform_order_no or "").strip(),),
            ).fetchone()
            if workflow is None:
                raise KeyError(platform_order_no)
            workflow_id = int(workflow["id"])
            self._reconcile_pending_prior_stage_checkpoints(
                conn,
                workflow_id,
                requested_stage=stage,
                source_record=self._decode_metadata(workflow["source_record_json"]),
                actor=actor,
                now=now,
            )
            rows = {
                str(row["stage"]): row
                for row in conn.execute(
                    "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchall()
            }
            target_stage = self._resolve_pause_stage(rows, stage)
            target = rows[target_stage]
            old_state = str(target["state"])
            metadata = self._decode_metadata(target["metadata_json"])
            metadata.update(
                {
                    "pause_kind": str(kind),
                    "paused_at": now,
                    "paused_by": actor,
                    "retry_confirmation_required": review_required,
                }
            )
            conn.execute(
                """
                UPDATE custom_order_stages
                SET required = CASE WHEN required IS NULL THEN 1 ELSE required END,
                    state = ?, result_status = ?, completed_at = NULL,
                    last_error = ?, metadata_json = ?
                WHERE workflow_id = ? AND stage = ?
                """,
                (
                    str(WorkflowStageState.PENDING),
                    str(result_status or "").strip() or None,
                    audit_reason,
                    _json(metadata),
                    workflow_id,
                    target_stage,
                ),
            )
            self._insert_event(
                conn,
                workflow_id,
                stage=target_stage,
                event_type="workflow_processing_paused",
                old_state=old_state,
                new_state=str(WorkflowStageState.PENDING),
                actor=actor,
                reason=audit_reason,
                details={
                    "pause_kind": str(kind),
                    "requested_stage": stage,
                    "retry_confirmation_required": review_required,
                },
            )
            self._refresh_workflow_status(conn, workflow_id, now=now)
            status_row = conn.execute(
                "SELECT workflow_status FROM custom_order_workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            workflow_status = str(status_row[0])
            conn.commit()
        return WorkflowPauseRecord(
            stage=target_stage,
            pause_kind=kind,
            retry_confirmation_required=review_required,
            workflow_status=workflow_status,
        )

    def get_pending_retry_review(self, platform_order_no: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM custom_order_stages s
                JOIN custom_order_workflows w ON w.id = s.workflow_id
                WHERE w.platform_order_no = ? AND s.state = ?
                """,
                (str(platform_order_no or "").strip(), str(WorkflowStageState.PENDING)),
            ).fetchall()
        by_stage = {str(row["stage"]): dict(row) for row in rows}
        for stage in STAGE_ORDER:
            row = by_stage.get(stage)
            if row is None:
                continue
            metadata = self._decode_metadata(row.get("metadata_json"))
            if metadata.get("retry_confirmation_required") is True:
                row["metadata"] = metadata
                return row
        return None

    def resolve_stage_retry_review(
        self,
        platform_order_no: str,
        stage: str,
        resolution: StageRetryReviewResolution | str,
        *,
        reason: str,
        actor: str = "desktop_user",
    ) -> str:
        """Resolve an ambiguous-write hold atomically before resuming automation."""

        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("解除人工复核锁必须填写原因。")
        choice = StageRetryReviewResolution(resolution)
        self.initialize()
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT w.id AS workflow_id, s.*
                FROM custom_order_workflows w
                JOIN custom_order_stages s ON s.workflow_id = w.id
                WHERE w.platform_order_no = ? AND s.stage = ?
                """,
                (str(platform_order_no or "").strip(), stage),
            ).fetchone()
            if row is None:
                raise KeyError(platform_order_no)
            metadata = self._decode_metadata(row["metadata_json"])
            if (
                str(row["state"]) != str(WorkflowStageState.PENDING)
                or metadata.get("retry_confirmation_required") is not True
            ):
                raise ValueError("该阶段当前没有待解除的人工复核锁。")
            workflow_id = int(row["workflow_id"])
            if choice is StageRetryReviewResolution.COMPLETED:
                new_state = WorkflowStageState.COMPLETED
                result_status = "manual_verified_executed"
                completed_at = now
            else:
                new_state = WorkflowStageState.PENDING
                result_status = "manual_verified_not_executed"
                completed_at = None
            conn.execute(
                """
                UPDATE custom_order_stages
                SET state = ?, result_status = ?, completed_at = ?,
                    last_error = NULL, metadata_json = ?
                WHERE workflow_id = ? AND stage = ?
                """,
                (
                    str(new_state),
                    result_status,
                    completed_at,
                    _json(self._clear_pause_metadata(row["metadata_json"])),
                    workflow_id,
                    stage,
                ),
            )
            self._insert_event(
                conn,
                workflow_id,
                stage=stage,
                event_type="stage_retry_review_resolved",
                old_state=str(row["state"]),
                new_state=str(new_state),
                actor=actor,
                reason=audit_reason,
                details={"resolution": str(choice)},
            )
            self._refresh_workflow_status(conn, workflow_id, now=now)
            refreshed = conn.execute(
                "SELECT workflow_status FROM custom_order_workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            conn.commit()
        return str(refreshed[0])

    def repair_automated_blocked_stages(self) -> int:
        """Convert historical automatic BLOCKED stages to resumable PENDING stages."""

        self.initialize()
        now = utc_now()
        repaired = 0
        automatic_actors = {"desktop_worker", "automation"}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT w.id AS workflow_id, w.platform_order_no, s.*,
                       (
                           SELECT e.actor FROM custom_order_events e
                           WHERE e.workflow_id = w.id AND e.stage = s.stage
                             AND e.new_state = 'BLOCKED'
                           ORDER BY e.id DESC LIMIT 1
                       ) AS blocked_actor
                FROM custom_order_workflows w
                JOIN custom_order_stages s ON s.workflow_id = w.id
                WHERE s.state = 'BLOCKED'
                """
            ).fetchall()
            for row in rows:
                blocked_actor = str(row["blocked_actor"] or "").strip().lower()
                if blocked_actor not in automatic_actors:
                    continue
                diagnostic = " ".join(
                    (str(row["result_status"] or ""), str(row["last_error"] or ""))
                ).lower()
                ambiguous = any(
                    token in diagnostic
                    for token in ("unknown", "manual_review", "manual pending", "无法确认", "不明确")
                )
                metadata = self._decode_metadata(row["metadata_json"])
                metadata.update(
                    {
                        "pause_kind": str(
                            WorkflowPauseKind.AMBIGUOUS_WRITE
                            if ambiguous
                            else WorkflowPauseKind.RETRYABLE_FAILURE
                        ),
                        "paused_at": now,
                        "paused_by": "system_migration",
                        "retry_confirmation_required": ambiguous,
                    }
                )
                conn.execute(
                    """
                    UPDATE custom_order_stages
                    SET state = 'PENDING', completed_at = NULL, metadata_json = ?
                    WHERE workflow_id = ? AND stage = ?
                    """,
                    (_json(metadata), int(row["workflow_id"]), str(row["stage"])),
                )
                self._insert_event(
                    conn,
                    int(row["workflow_id"]),
                    stage=str(row["stage"]),
                    event_type="legacy_automatic_block_repaired",
                    old_state=str(WorkflowStageState.BLOCKED),
                    new_state=str(WorkflowStageState.PENDING),
                    actor="system_migration",
                    reason="自动修复历史自动化阻止状态，恢复为可续作的阶段待处理。",
                    details={
                        "previous_actor": blocked_actor,
                        "retry_confirmation_required": ambiguous,
                    },
                )
                self._refresh_workflow_status(conn, int(row["workflow_id"]), now=now)
                repaired += 1
            conn.commit()
        return repaired

    def set_stage_states_for_workflows(
        self,
        platform_order_nos: Iterable[str],
        stage: str,
        state: WorkflowStageState | str,
        *,
        reason: str,
        actor: str = "user",
    ) -> BatchWorkflowMutationSummary:
        """Atomically set one stage to the same state across selected workflows."""

        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("批量修改工作流状态必须填写原因。")
        new_state = WorkflowStageState(state)
        normalized_order_nos = self._normalize_order_nos(platform_order_nos)
        valid_states = {str(item) for item in WorkflowStageState}
        self.initialize()
        self._reject_automatic_block_if_needed(
            normalized_order_nos,
            stage=stage,
            state=new_state,
            actor=actor,
            reason=audit_reason,
        )
        now = utc_now()
        changed_order_count = 0
        changed_stage_count = 0

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows: list[
                tuple[str, int, dict[str, Any], dict[str, sqlite3.Row]]
            ] = []
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    """
                    SELECT id, source_record_json
                    FROM custom_order_workflows
                    WHERE platform_order_no = ?
                    """,
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    raise ValueError(f"找不到定制订单：{order_no}")
                workflow_id = int(workflow["id"])
                stage_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                if set(stage_rows) != set(STAGE_ORDER):
                    raise ValueError(f"订单 {order_no} 的阶段数据不完整，批量操作已取消。")
                invalid_states = {
                    str(row["state"])
                    for row in stage_rows.values()
                    if str(row["state"]) not in valid_states
                }
                if invalid_states:
                    states = "、".join(sorted(invalid_states))
                    raise ValueError(f"订单 {order_no} 存在未知阶段状态：{states}")
                workflows.append(
                    (
                        order_no,
                        workflow_id,
                        self._decode_metadata(workflow["source_record_json"]),
                        stage_rows,
                    )
                )

            required = None if new_state == WorkflowStageState.NOT_APPLICABLE else int(
                new_state != WorkflowStageState.NOT_REQUIRED
            )
            completed_at = now if new_state == WorkflowStageState.COMPLETED else None
            for _order_no, workflow_id, source_record, stage_rows in workflows:
                old_state = str(stage_rows[stage]["state"])
                if old_state == str(new_state):
                    continue
                conn.execute(
                    """
                    UPDATE custom_order_stages
                    SET required = ?, state = ?, result_status = NULL,
                        completed_at = ?, last_error = NULL, metadata_json = ?
                    WHERE workflow_id = ? AND stage = ?
                    """,
                    (
                        required,
                        str(new_state),
                        completed_at,
                        _json(self._clear_pause_metadata(stage_rows[stage]["metadata_json"])),
                        workflow_id,
                        stage,
                    ),
                )
                if (
                    stage == "warehouse_logistics"
                    and new_state == WorkflowStageState.PENDING
                ):
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET source_record_json = ?
                        WHERE id = ?
                        """,
                        (
                            _json(
                                self._clear_warehouse_result_metadata(source_record)
                            ),
                            workflow_id,
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
                    reason=audit_reason,
                    details={"source": "manual_batch_stage_update"},
                )
                self._refresh_workflow_status(conn, workflow_id, now=now)
                changed_order_count += 1
                changed_stage_count += 1
            conn.commit()

        return BatchWorkflowMutationSummary(
            requested_count=len(normalized_order_nos),
            changed_order_count=changed_order_count,
            unchanged_order_count=len(normalized_order_nos) - changed_order_count,
            changed_stage_count=changed_stage_count,
        )

    def mark_workflows_not_required(
        self,
        platform_order_nos: Iterable[str],
        *,
        reason: str,
        actor: str = "api_scanner",
        result_status: str = "buyer_cancel_requested",
    ) -> WorkflowNotRequiredSummary:
        """Dispose active workflows after an explicit buyer-cancel signal.

        Previously completed stages are preserved for audit.  Pending or
        manually blocked stages become NOT_REQUIRED, and the workflow receives
        a distinct terminal status so it cannot be queued again by a later
        candidate scan.
        """

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("标记订单不需要处理必须填写原因。")
        normalized_order_nos = self._normalize_order_nos(platform_order_nos)
        terminal_states = {
            str(WorkflowStageState.COMPLETED),
            str(WorkflowStageState.NOT_REQUIRED),
            str(WorkflowStageState.NOT_APPLICABLE),
        }
        mutable_states = {
            str(WorkflowStageState.PENDING),
            str(WorkflowStageState.BLOCKED),
        }
        valid_states = terminal_states | mutable_states
        self.initialize()
        now = utc_now()
        changed_order_count = 0
        already_terminal_count = 0
        missing_count = 0
        changed_stage_count = 0

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    """
                    SELECT id, workflow_status, ignored, not_required_reason,
                           buyer_cancel_clear_streak,
                           buyer_cancel_clear_last_scan_id,
                           buyer_cancel_clear_last_seen_at
                    FROM custom_order_workflows
                    WHERE platform_order_no = ?
                    """,
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    missing_count += 1
                    continue
                old_workflow_status = str(workflow["workflow_status"])
                if old_workflow_status == "not_required":
                    already_terminal_count += 1
                    continue
                if old_workflow_status == "completed" or bool(workflow["ignored"]):
                    already_terminal_count += 1
                    continue

                workflow_id = int(workflow["id"])
                stage_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                if set(stage_rows) != set(STAGE_ORDER):
                    raise ValueError(
                        f"订单 {order_no} 的阶段数据不完整，取消状态同步已回滚。"
                    )
                invalid_states = {
                    str(row["state"])
                    for row in stage_rows.values()
                    if str(row["state"]) not in valid_states
                }
                if invalid_states:
                    states = "、".join(sorted(invalid_states))
                    raise ValueError(f"订单 {order_no} 存在未知阶段状态：{states}")

                changed_stages: list[str] = []
                preserved_stage_states: dict[str, str] = {}
                for stage in STAGE_ORDER:
                    row = stage_rows[stage]
                    old_state = str(row["state"])
                    if old_state in terminal_states:
                        preserved_stage_states[stage] = old_state
                        continue
                    conn.execute(
                        """
                        UPDATE custom_order_stages
                        SET required = 0, state = ?, result_status = ?,
                            completed_at = NULL, last_error = NULL, metadata_json = ?
                        WHERE workflow_id = ? AND stage = ?
                        """,
                        (
                            str(WorkflowStageState.NOT_REQUIRED),
                            str(result_status or "buyer_cancel_requested"),
                            _json(self._clear_pause_metadata(row["metadata_json"])),
                            workflow_id,
                            stage,
                        ),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=stage,
                        event_type="stage_state_changed",
                        old_state=old_state,
                        new_state=str(WorkflowStageState.NOT_REQUIRED),
                        actor=actor,
                        reason=audit_reason,
                        details={"source": "buyer_cancel_reconciliation"},
                    )
                    changed_stages.append(stage)
                    changed_stage_count += 1

                conn.execute(
                    """
                    UPDATE custom_order_workflows
                    SET workflow_status = 'not_required', last_seen_at = ?,
                        processed_at = ?, not_required_reason = ?,
                        buyer_cancel_clear_streak = 0,
                        buyer_cancel_clear_last_scan_id = NULL,
                        buyer_cancel_clear_last_seen_at = NULL,
                        version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        now,
                        str(result_status or "buyer_cancel_requested"),
                        now,
                        workflow_id,
                    ),
                )
                self._insert_event(
                    conn,
                    workflow_id,
                    stage=None,
                    event_type="workflow_marked_not_required",
                    old_state=old_workflow_status,
                    new_state="not_required",
                    actor=actor,
                    reason=audit_reason,
                    details={
                        "source": "buyer_cancel_reconciliation",
                        "result_status": str(result_status or "buyer_cancel_requested"),
                        "changed_stages": changed_stages,
                        "preserved_stage_states": preserved_stage_states,
                    },
                )
                changed_order_count += 1
            conn.commit()

        return WorkflowNotRequiredSummary(
            requested_count=len(normalized_order_nos),
            changed_order_count=changed_order_count,
            already_terminal_count=already_terminal_count,
            missing_count=missing_count,
            changed_stage_count=changed_stage_count,
        )

    def reconcile_buyer_cancel_reactivation(
        self,
        *,
        scan_id: str,
        eligible_order_nos: Iterable[str],
        currently_cancelled_order_nos: Iterable[str],
        snapshots_complete: bool,
        actor: str = "api_scanner",
        required_clear_scans: int = 2,
    ) -> BuyerCancelReactivationSummary:
        """Conservatively reopen buyer-cancelled workflows after stable clearance."""

        normalized_scan_id = str(scan_id or "").strip()
        if not normalized_scan_id:
            raise ValueError("买家取消撤销对账必须提供扫描 ID。")
        if int(required_clear_scans) < 2:
            raise ValueError("买家取消撤销至少需要两次完整扫描确认。")
        threshold = int(required_clear_scans)
        # Both sets may legitimately be empty (for example, after an API
        # failure when every outstanding confirmation must be invalidated).
        eligible = {
            str(value or "").strip()
            for value in eligible_order_nos
            if str(value or "").strip()
        }
        cancelled = {
            str(value or "").strip()
            for value in currently_cancelled_order_nos
            if str(value or "").strip()
        }
        self.initialize()
        now = utc_now()
        clear_observed: list[str] = []
        reactivated: list[str] = []
        reset: list[str] = []

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows = conn.execute(
                """
                SELECT id, platform_order_no, workflow_status,
                       buyer_cancel_clear_streak,
                       buyer_cancel_clear_last_scan_id
                FROM custom_order_workflows
                WHERE workflow_status = 'not_required'
                  AND ignored = 0
                  AND not_required_reason = 'buyer_cancel_requested'
                ORDER BY id
                """
            ).fetchall()

            for workflow in workflows:
                workflow_id = int(workflow["id"])
                order_no = str(workflow["platform_order_no"])
                old_streak = max(0, int(workflow["buyer_cancel_clear_streak"] or 0))
                last_scan_id = str(workflow["buyer_cancel_clear_last_scan_id"] or "")
                qualifies = (
                    bool(snapshots_complete)
                    and order_no in eligible
                    and order_no not in cancelled
                )

                if not qualifies:
                    if old_streak or last_scan_id:
                        if not snapshots_complete:
                            reset_reason = "snapshot_incomplete"
                        elif order_no in cancelled:
                            reset_reason = "buyer_cancel_requested"
                        else:
                            reset_reason = "normal_candidate_rules_not_met"
                        conn.execute(
                            """
                            UPDATE custom_order_workflows
                            SET buyer_cancel_clear_streak = 0,
                                buyer_cancel_clear_last_scan_id = NULL,
                                buyer_cancel_clear_last_seen_at = NULL,
                                version = version + 1, updated_at = ?
                            WHERE id = ?
                            """,
                            (now, workflow_id),
                        )
                        self._insert_event(
                            conn,
                            workflow_id,
                            stage=None,
                            event_type="buyer_cancel_clear_reset",
                            old_state="not_required",
                            new_state="not_required",
                            actor=actor,
                            reason="买家取消撤销的连续扫描确认已重置。",
                            details={
                                "source": "buyer_cancel_reactivation",
                                "scan_id": normalized_scan_id,
                                "reset_reason": reset_reason,
                                "previous_clear_streak": old_streak,
                            },
                        )
                        reset.append(order_no)
                    continue

                # A retried application call with the same audit task ID must
                # not count twice toward the two-scan safety threshold.
                if last_scan_id == normalized_scan_id:
                    continue

                new_streak = old_streak + 1
                if new_streak < threshold:
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET buyer_cancel_clear_streak = ?,
                            buyer_cancel_clear_last_scan_id = ?,
                            buyer_cancel_clear_last_seen_at = ?,
                            last_seen_at = ?, version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_streak, normalized_scan_id, now, now, now, workflow_id),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=None,
                        event_type="buyer_cancel_clear_observed",
                        old_state="not_required",
                        new_state="not_required",
                        actor=actor,
                        reason="完整扫描已确认买家取消标签消失，等待再次确认。",
                        details={
                            "source": "buyer_cancel_reactivation",
                            "scan_id": normalized_scan_id,
                            "clear_streak": new_streak,
                            "required_clear_scans": threshold,
                        },
                    )
                    clear_observed.append(order_no)
                    continue

                stages = conn.execute(
                    "SELECT * FROM custom_order_stages WHERE workflow_id = ? ORDER BY rowid",
                    (workflow_id,),
                ).fetchall()
                changed_stages: list[str] = []
                preserved_stage_states: dict[str, str] = {}
                for stage_row in stages:
                    stage = str(stage_row["stage"])
                    old_state = str(stage_row["state"])
                    if (
                        old_state != str(WorkflowStageState.NOT_REQUIRED)
                        or str(stage_row["result_status"] or "")
                        != "buyer_cancel_requested"
                    ):
                        preserved_stage_states[stage] = old_state
                        continue
                    conn.execute(
                        """
                        UPDATE custom_order_stages
                        SET required = 1, state = 'PENDING', result_status = NULL,
                            completed_at = NULL, last_error = NULL, metadata_json = ?
                        WHERE workflow_id = ? AND stage = ?
                        """,
                        (
                            _json(self._clear_pause_metadata(stage_row["metadata_json"])),
                            workflow_id,
                            stage,
                        ),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=stage,
                        event_type="stage_auto_reopened",
                        old_state=old_state,
                        new_state=str(WorkflowStageState.PENDING),
                        actor=actor,
                        reason="连续两次完整扫描确认买家取消申请已撤销。",
                        details={
                            "source": "buyer_cancel_reactivation",
                            "scan_id": normalized_scan_id,
                            "consecutive_clear_scans": new_streak,
                        },
                    )
                    changed_stages.append(stage)

                if not changed_stages:
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET buyer_cancel_clear_streak = 0,
                            buyer_cancel_clear_last_scan_id = NULL,
                            buyer_cancel_clear_last_seen_at = NULL,
                            version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, workflow_id),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=None,
                        event_type="buyer_cancel_clear_reset",
                        old_state="not_required",
                        new_state="not_required",
                        actor=actor,
                        reason="未找到可安全恢复的买家取消阶段，自动恢复已跳过。",
                        details={
                            "source": "buyer_cancel_reactivation",
                            "scan_id": normalized_scan_id,
                            "reset_reason": "no_buyer_cancel_stages",
                            "previous_clear_streak": old_streak,
                        },
                    )
                    reset.append(order_no)
                    continue

                self._refresh_workflow_status(conn, workflow_id, now=now)
                refreshed = conn.execute(
                    "SELECT workflow_status FROM custom_order_workflows WHERE id = ?",
                    (workflow_id,),
                ).fetchone()
                new_workflow_status = str(refreshed["workflow_status"])
                conn.execute(
                    """
                    UPDATE custom_order_workflows
                    SET processed_at = NULL, not_required_reason = NULL,
                        buyer_cancel_clear_streak = 0,
                        buyer_cancel_clear_last_scan_id = NULL,
                        buyer_cancel_clear_last_seen_at = NULL,
                        last_seen_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, workflow_id),
                )
                self._insert_event(
                    conn,
                    workflow_id,
                    stage=None,
                    event_type="workflow_auto_reactivated",
                    old_state="not_required",
                    new_state=new_workflow_status,
                    actor=actor,
                    reason="连续两次完整扫描确认买家取消申请已撤销，自动重新入队。",
                    details={
                        "source": "buyer_cancel_reactivation",
                        "scan_id": normalized_scan_id,
                        "consecutive_clear_scans": new_streak,
                        "changed_stages": changed_stages,
                        "preserved_stage_states": preserved_stage_states,
                    },
                )
                reactivated.append(order_no)

            conn.commit()

        return BuyerCancelReactivationSummary(
            requested_count=len(workflows),
            clear_observed_order_nos=tuple(clear_observed),
            reactivated_order_nos=tuple(reactivated),
            reset_order_nos=tuple(reset),
        )

    def reconcile_missing_candidate_folders(
        self,
        folder_exists_by_order: Mapping[str, bool],
        *,
        reason: str,
        actor: str = "api_scanner",
    ) -> MissingCandidateFolderReconciliationSummary:
        """Reconcile active orders absent from a later complete candidate scan.

        A physical order folder is treated as evidence that a clean local
        workflow was completed outside this application.  Any current error,
        manual-review lock, or blocked stage is preserved without mutation and
        must be resolved by the user.  The whole batch is validated and
        committed atomically.
        """

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("Folder reconciliation requires a reason.")
        normalized_order_nos = self._normalize_order_nos(folder_exists_by_order)
        folder_states = {
            order_no: bool(folder_exists_by_order[order_no])
            for order_no in normalized_order_nos
        }
        terminal_states = {
            str(WorkflowStageState.COMPLETED),
            str(WorkflowStageState.NOT_REQUIRED),
            str(WorkflowStageState.NOT_APPLICABLE),
        }
        mutable_states = {
            str(WorkflowStageState.PENDING),
            str(WorkflowStageState.BLOCKED),
        }
        valid_states = terminal_states | mutable_states
        self.initialize()
        now = utc_now()
        completed_count = 0
        pending_count = 0
        changed_order_count = 0
        already_terminal_count = 0
        missing_count = 0
        changed_stage_count = 0
        error_preserved_count = 0

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows: list[
                tuple[str, sqlite3.Row, dict[str, sqlite3.Row], tuple[str, ...]]
            ] = []
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    """
                    SELECT id, workflow_status, ignored
                    FROM custom_order_workflows
                    WHERE platform_order_no = ?
                    """,
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    missing_count += 1
                    continue
                if (
                    str(workflow["workflow_status"]) in {"completed", "not_required", "cancelled"}
                    or bool(workflow["ignored"])
                ):
                    already_terminal_count += 1
                    continue
                workflow_id = int(workflow["id"])
                stage_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                if set(stage_rows) != set(STAGE_ORDER):
                    raise ValueError(
                        f"Order {order_no} has incomplete stage data; folder reconciliation rolled back."
                    )
                invalid_states = {
                    str(row["state"])
                    for row in stage_rows.values()
                    if str(row["state"]) not in valid_states
                }
                if invalid_states:
                    states = ", ".join(sorted(invalid_states))
                    raise ValueError(
                        f"Order {order_no} has unknown stage states ({states}); "
                        "folder reconciliation rolled back."
                    )
                protection_codes = self._folder_reconciliation_protection_codes(
                    stage_rows.values()
                )
                workflows.append((order_no, workflow, stage_rows, protection_codes))

            for order_no, workflow, stage_rows, protection_codes in workflows:
                workflow_id = int(workflow["id"])
                old_workflow_status = str(workflow["workflow_status"])
                changed_stages: list[str] = []
                folder_found = folder_states[order_no]

                if protection_codes:
                    error_preserved_count += 1
                    continue

                if folder_found:
                    for stage in STAGE_ORDER:
                        row = stage_rows[stage]
                        old_state = str(row["state"])
                        if old_state in terminal_states:
                            continue
                        conn.execute(
                            """
                            UPDATE custom_order_stages
                            SET state = ?, result_status = 'folder_reconciled',
                                completed_at = ?, last_error = NULL, metadata_json = ?
                            WHERE workflow_id = ? AND stage = ?
                            """,
                            (
                                str(WorkflowStageState.COMPLETED),
                                now,
                                _json(self._clear_pause_metadata(row["metadata_json"])),
                                workflow_id,
                                stage,
                            ),
                        )
                        self._insert_event(
                            conn,
                            workflow_id,
                            stage=stage,
                            event_type="stage_state_changed",
                            old_state=old_state,
                            new_state=str(WorkflowStageState.COMPLETED),
                            actor=actor,
                            reason=audit_reason,
                            details={
                                "source": "missing_candidate_folder_reconciliation",
                                "folder_found": True,
                            },
                        )
                        changed_stages.append(stage)
                        changed_stage_count += 1
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET workflow_status = 'completed', processed_at = ?,
                            version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, workflow_id),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=None,
                        event_type="workflow_reconciled_from_folder",
                        old_state=old_workflow_status,
                        new_state="completed",
                        actor=actor,
                        reason=audit_reason,
                        details={
                            "source": "missing_candidate_folder_reconciliation",
                            "folder_found": True,
                            "changed_stages": changed_stages,
                        },
                    )
                    completed_count += 1
                    changed_order_count += 1
                    continue

                # A missing physical folder disproves a stale completed folder
                # checkpoint.  Other completed stages remain valid, preserving
                # the established resume-from-current-stage behavior.
                for stage in STAGE_ORDER:
                    row = stage_rows[stage]
                    old_state = str(row["state"])
                    should_reopen = old_state == str(WorkflowStageState.BLOCKED) or (
                        stage == "folder"
                        and old_state == str(WorkflowStageState.COMPLETED)
                    )
                    if not should_reopen:
                        continue
                    conn.execute(
                        """
                        UPDATE custom_order_stages
                        SET state = ?, result_status = 'folder_missing',
                            completed_at = NULL, last_error = NULL, metadata_json = ?
                        WHERE workflow_id = ? AND stage = ?
                        """,
                        (
                            str(WorkflowStageState.PENDING),
                            _json(self._clear_pause_metadata(row["metadata_json"])),
                            workflow_id,
                            stage,
                        ),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=stage,
                        event_type="stage_state_changed",
                        old_state=old_state,
                        new_state=str(WorkflowStageState.PENDING),
                        actor=actor,
                        reason=audit_reason,
                        details={
                            "source": "missing_candidate_folder_reconciliation",
                            "folder_found": False,
                        },
                    )
                    changed_stages.append(stage)
                    changed_stage_count += 1

                refreshed_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT stage, required, state FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                new_workflow_status = self._workflow_status_from_stages(refreshed_rows)
                if new_workflow_status == "completed":
                    # A non-required folder legitimately needs no physical
                    # directory; retain the terminal aggregate in that edge case.
                    completed_count += 1
                else:
                    pending_count += 1
                if changed_stages or new_workflow_status != old_workflow_status:
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET workflow_status = ?, processed_at = NULL,
                            version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_workflow_status, now, workflow_id),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=None,
                        event_type="workflow_reconciled_from_folder",
                        old_state=old_workflow_status,
                        new_state=new_workflow_status,
                        actor=actor,
                        reason=audit_reason,
                        details={
                            "source": "missing_candidate_folder_reconciliation",
                            "folder_found": False,
                            "changed_stages": changed_stages,
                        },
                    )
                    changed_order_count += 1
            conn.commit()

        return MissingCandidateFolderReconciliationSummary(
            requested_count=len(normalized_order_nos),
            completed_count=completed_count,
            pending_count=pending_count,
            changed_order_count=changed_order_count,
            already_terminal_count=already_terminal_count,
            missing_count=missing_count,
            changed_stage_count=changed_stage_count,
            error_preserved_count=error_preserved_count,
        )

    def mark_workflows_manually_completed(
        self,
        platform_order_nos: Iterable[str],
        *,
        reason: str,
        actor: str = "user",
    ) -> ManualCompletionSummary:
        """Atomically close selected workflows after an operator confirms completion."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("批量标记完成必须填写原因。")
        normalized_order_nos: list[str] = []
        seen: set[str] = set()
        for value in platform_order_nos:
            order_no = str(value or "").strip()
            if not order_no:
                raise ValueError("平台订单号不能为空。")
            if order_no not in seen:
                normalized_order_nos.append(order_no)
                seen.add(order_no)
        if not normalized_order_nos:
            raise ValueError("请至少选择一张定制订单。")

        terminal_states = {
            str(WorkflowStageState.COMPLETED),
            str(WorkflowStageState.NOT_REQUIRED),
            str(WorkflowStageState.NOT_APPLICABLE),
        }
        mutable_states = {
            str(WorkflowStageState.PENDING),
            str(WorkflowStageState.BLOCKED),
        }
        valid_states = terminal_states | mutable_states
        self.initialize()
        now = utc_now()
        completed_count = 0
        already_completed_count = 0
        changed_stage_count = 0

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows: list[tuple[str, int, str, dict[str, sqlite3.Row]]] = []
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    """
                    SELECT id, workflow_status FROM custom_order_workflows
                    WHERE platform_order_no = ?
                    """,
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    raise ValueError(f"找不到定制订单：{order_no}")
                workflow_id = int(workflow["id"])
                stage_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                if set(stage_rows) != set(STAGE_ORDER):
                    raise ValueError(f"订单 {order_no} 的阶段数据不完整，批量操作已取消。")
                invalid_states = {
                    str(row["state"])
                    for row in stage_rows.values()
                    if str(row["state"]) not in valid_states
                }
                if invalid_states:
                    states = "、".join(sorted(invalid_states))
                    raise ValueError(f"订单 {order_no} 存在未知阶段状态：{states}")
                workflows.append(
                    (order_no, workflow_id, str(workflow["workflow_status"]), stage_rows)
                )

            for order_no, workflow_id, old_workflow_status, stage_rows in workflows:
                if old_workflow_status == "completed":
                    already_completed_count += 1
                    continue

                changed_stages: list[str] = []
                preserved_stage_states: dict[str, str] = {}
                for stage in STAGE_ORDER:
                    old_state = str(stage_rows[stage]["state"])
                    if old_state in terminal_states:
                        preserved_stage_states[stage] = old_state
                        continue
                    conn.execute(
                        """
                        UPDATE custom_order_stages
                        SET state = ?, result_status = 'manual', completed_at = ?,
                            last_error = NULL, metadata_json = ?
                        WHERE workflow_id = ? AND stage = ?
                        """,
                        (
                            str(WorkflowStageState.COMPLETED),
                            now,
                            _json(self._clear_pause_metadata(stage_rows[stage]["metadata_json"])),
                            workflow_id,
                            stage,
                        ),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=stage,
                        event_type="stage_state_changed",
                        old_state=old_state,
                        new_state=str(WorkflowStageState.COMPLETED),
                        actor=actor,
                        reason=audit_reason,
                        details={"source": "manual_complete_all"},
                    )
                    changed_stages.append(stage)
                    changed_stage_count += 1

                self._refresh_workflow_status(conn, workflow_id, now=now)
                refreshed = conn.execute(
                    "SELECT workflow_status FROM custom_order_workflows WHERE id = ?",
                    (workflow_id,),
                ).fetchone()
                if refreshed is None or str(refreshed[0]) != "completed":
                    raise RuntimeError(f"订单 {order_no} 未能汇总为 completed")
                self._insert_event(
                    conn,
                    workflow_id,
                    stage=None,
                    event_type="workflow_manually_completed",
                    old_state=old_workflow_status,
                    new_state="completed",
                    actor=actor,
                    reason=audit_reason,
                    details={
                        "source": "manual_complete_all",
                        "changed_stages": changed_stages,
                        "preserved_stage_states": preserved_stage_states,
                    },
                )
                completed_count += 1
            conn.commit()

        return ManualCompletionSummary(
            requested_count=len(normalized_order_nos),
            completed_count=completed_count,
            already_completed_count=already_completed_count,
            changed_stage_count=changed_stage_count,
        )

    def mark_workflows_cancelled(
        self,
        platform_order_nos: Iterable[str],
        *,
        reason: str,
        actor: str = "user",
    ) -> BatchWorkflowMutationSummary:
        """Persistently cancel selected local workflows without changing stages."""

        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("取消定制订单必须填写原因。")
        normalized_order_nos = self._normalize_order_nos(platform_order_nos)
        self.initialize()
        now = utc_now()
        changed_order_count = 0
        unchanged_order_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows: list[sqlite3.Row] = []
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    "SELECT id, platform_order_no, workflow_status "
                    "FROM custom_order_workflows WHERE platform_order_no = ?",
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    raise ValueError(f"找不到定制订单：{order_no}")
                workflows.append(workflow)
            for workflow in workflows:
                old_status = str(workflow["workflow_status"])
                if old_status == "cancelled":
                    unchanged_order_count += 1
                    continue
                conn.execute(
                    """
                    UPDATE custom_order_workflows
                    SET workflow_status = 'cancelled', processed_at = ?,
                        version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, int(workflow["id"])),
                )
                self._insert_event(
                    conn,
                    int(workflow["id"]),
                    stage=None,
                    event_type="workflow_manually_cancelled",
                    old_state=old_status,
                    new_state="cancelled",
                    actor=actor,
                    reason=audit_reason,
                    details={
                        "source": "desktop_user",
                        "stages_preserved": True,
                    },
                )
                changed_order_count += 1
            conn.commit()
        return BatchWorkflowMutationSummary(
            requested_count=len(normalized_order_nos),
            changed_order_count=changed_order_count,
            unchanged_order_count=unchanged_order_count,
            changed_stage_count=0,
        )

    def reopen_from_stage(
        self,
        platform_order_no: str,
        stage: str,
        *,
        reason: str,
        actor: str = "user",
    ) -> None:
        self.reopen_workflows_from_stage(
            [platform_order_no],
            stage,
            reason=reason,
            actor=actor,
        )

    def reopen_workflows_from_stage(
        self,
        platform_order_nos: Iterable[str],
        stage: str,
        *,
        reason: str,
        actor: str = "user",
    ) -> BatchWorkflowMutationSummary:
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知阶段：{stage}")
        audit_reason = str(reason or "").strip()
        if not audit_reason:
            raise ValueError("重新打开工作流必须填写原因。")
        normalized_order_nos = self._normalize_order_nos(platform_order_nos)
        valid_states = {str(item) for item in WorkflowStageState}
        self.initialize()
        now = utc_now()
        start = STAGE_ORDER.index(stage)
        changed_order_count = 0
        changed_stage_count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            workflows: list[
                tuple[str, int, str, dict[str, Any], dict[str, sqlite3.Row]]
            ] = []
            for order_no in normalized_order_nos:
                workflow = conn.execute(
                    """
                    SELECT id, workflow_status, source_record_json
                    FROM custom_order_workflows
                    WHERE platform_order_no = ?
                    """,
                    (order_no,),
                ).fetchone()
                if workflow is None:
                    raise ValueError(f"找不到定制订单：{order_no}")
                workflow_id = int(workflow["id"])
                stage_rows = {
                    str(row["stage"]): row
                    for row in conn.execute(
                        "SELECT * FROM custom_order_stages WHERE workflow_id = ?",
                        (workflow_id,),
                    ).fetchall()
                }
                if set(stage_rows) != set(STAGE_ORDER):
                    raise ValueError(f"订单 {order_no} 的阶段数据不完整，批量操作已取消。")
                invalid_states = {
                    str(row["state"])
                    for row in stage_rows.values()
                    if str(row["state"]) not in valid_states
                }
                if invalid_states:
                    states = "、".join(sorted(invalid_states))
                    raise ValueError(f"订单 {order_no} 存在未知阶段状态：{states}")
                workflows.append(
                    (
                        order_no,
                        workflow_id,
                        str(workflow["workflow_status"]),
                        self._decode_metadata(workflow["source_record_json"]),
                        stage_rows,
                    )
                )

            for (
                _order_no,
                workflow_id,
                old_workflow_status,
                source_record,
                stage_rows,
            ) in workflows:
                order_changed = False
                warehouse_reopened = False
                for current_stage in STAGE_ORDER[start:]:
                    current = stage_rows[current_stage]
                    if current["required"] is None:
                        continue
                    old_state = str(current["state"])
                    if (
                        old_state == str(WorkflowStageState.PENDING)
                        and current["completed_at"] is None
                        and current["last_error"] is None
                        and current["result_status"] is None
                        and self._decode_metadata(current["metadata_json"])
                        == self._clear_pause_metadata(current["metadata_json"])
                    ):
                        continue
                    conn.execute(
                        """
                        UPDATE custom_order_stages
                        SET state = 'PENDING', result_status = NULL, completed_at = NULL,
                            last_error = NULL, metadata_json = ?
                        WHERE workflow_id = ? AND stage = ?
                        """,
                        (
                            _json(self._clear_pause_metadata(current["metadata_json"])),
                            workflow_id,
                            current_stage,
                        ),
                    )
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=current_stage,
                        event_type="stage_reopened",
                        old_state=old_state,
                        new_state=str(WorkflowStageState.PENDING),
                        actor=actor,
                        reason=audit_reason,
                        details={"source": "manual_batch_reopen"},
                    )
                    order_changed = True
                    warehouse_reopened = (
                        warehouse_reopened or current_stage == "warehouse_logistics"
                    )
                    changed_stage_count += 1
                if warehouse_reopened:
                    conn.execute(
                        """
                        UPDATE custom_order_workflows
                        SET source_record_json = ?
                        WHERE id = ?
                        """,
                        (
                            _json(
                                self._clear_warehouse_result_metadata(source_record)
                            ),
                            workflow_id,
                        ),
                    )
                restoring_cancelled = old_workflow_status == "cancelled"
                if not order_changed and not restoring_cancelled:
                    continue
                self._refresh_workflow_status(conn, workflow_id, now=now)
                if restoring_cancelled:
                    refreshed = conn.execute(
                        "SELECT workflow_status FROM custom_order_workflows WHERE id = ?",
                        (workflow_id,),
                    ).fetchone()
                    self._insert_event(
                        conn,
                        workflow_id,
                        stage=None,
                        event_type="workflow_cancelled_reopened",
                        old_state="cancelled",
                        new_state=str(refreshed[0]) if refreshed else "pending",
                        actor=actor,
                        reason=audit_reason,
                        details={"source": "manual_batch_reopen", "stage": stage},
                    )
                changed_order_count += 1
            conn.commit()

        return BatchWorkflowMutationSummary(
            requested_count=len(normalized_order_nos),
            changed_order_count=changed_order_count,
            unchanged_order_count=len(normalized_order_nos) - changed_order_count,
            changed_stage_count=changed_stage_count,
        )

    @staticmethod
    def _decode_metadata(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        try:
            decoded = json.loads(str(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}

    @classmethod
    def _clear_warehouse_result_metadata(cls, value: Any) -> dict[str, Any]:
        source_record = cls._decode_metadata(value)
        for key in (
            "warehouse_logistics_complete",
            "warehouse_logistics_status",
            "warehouse_logistics_completed_at",
            "warehouse_logistics_decisions",
            "warehouse_logistics_write_results",
            "warehouse_logistics_result_detail",
            "warehouse_logistics_postal_code",
            "warehouse_logistics_postal_source",
            "warehouse_logistics_postal_error",
            "warehouse_logistics_postal_diagnostic",
            "shipping_postal_api_error",
            "shipping_postal_api_diagnostic",
            "processed_at",
        ):
            source_record.pop(key, None)
        return source_record

    @classmethod
    def _folder_reconciliation_protection_codes(
        cls,
        stages: Iterable[Any],
    ) -> tuple[str, ...]:
        codes: list[str] = []
        for stage in stages:
            item = dict(stage)
            if str(item.get("last_error") or "").strip():
                codes.append("existing_error")
            if str(item.get("state") or "").strip() == str(WorkflowStageState.BLOCKED):
                codes.append("manual_blocked")
            metadata = cls._decode_metadata(item.get("metadata_json"))
            if metadata.get("retry_confirmation_required") is True:
                codes.append("retry_review_required")
        return tuple(dict.fromkeys(codes))

    @classmethod
    def _clear_pause_metadata(cls, value: Any) -> dict[str, Any]:
        metadata = cls._decode_metadata(value)
        for key in (
            "pause_kind",
            "paused_at",
            "paused_by",
            "retry_confirmation_required",
        ):
            metadata.pop(key, None)
        return metadata

    def _reject_automatic_block_if_needed(
        self,
        platform_order_nos: Iterable[str],
        *,
        stage: str,
        state: WorkflowStageState,
        actor: str,
        reason: str,
    ) -> None:
        normalized_actor = str(actor or "").strip().lower()
        if state is not WorkflowStageState.BLOCKED or normalized_actor in {
            "user",
            "desktop_user",
        }:
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for platform_order_no in platform_order_nos:
                workflow = conn.execute(
                    "SELECT id FROM custom_order_workflows WHERE platform_order_no = ?",
                    (str(platform_order_no or "").strip(),),
                ).fetchone()
                if workflow is None:
                    continue
                current = conn.execute(
                    "SELECT state FROM custom_order_stages WHERE workflow_id = ? AND stage = ?",
                    (int(workflow["id"]), stage),
                ).fetchone()
                self._insert_event(
                    conn,
                    int(workflow["id"]),
                    stage=stage,
                    event_type="automatic_block_rejected",
                    old_state=str(current["state"]) if current else None,
                    new_state=None,
                    actor=actor,
                    reason=str(reason or "").strip(),
                    details={"requested_state": str(state)},
                )
            conn.commit()
        raise PermissionError("已阻止只能由用户手动设置，自动化任务无权写入 BLOCKED。")

    @staticmethod
    def _resolve_pause_stage(rows: dict[str, sqlite3.Row], requested_stage: str) -> str:
        terminal = {
            str(WorkflowStageState.COMPLETED),
            str(WorkflowStageState.NOT_REQUIRED),
            str(WorkflowStageState.NOT_APPLICABLE),
        }
        requested = rows.get(requested_stage)
        if requested is not None and str(requested["state"]) not in terminal:
            return requested_stage
        if requested is not None and str(requested["state"]) != str(WorkflowStageState.COMPLETED):
            return requested_stage
        for stage in STAGE_ORDER:
            row = rows.get(stage)
            if row is not None and str(row["state"]) == str(WorkflowStageState.PENDING):
                return stage
        raise ValueError("订单没有可记录暂停的待处理阶段。")

    @classmethod
    def _reconcile_pending_prior_stage_checkpoints(
        cls,
        conn: sqlite3.Connection,
        workflow_id: int,
        *,
        requested_stage: str,
        source_record: Mapping[str, Any],
        actor: str,
        now: str,
    ) -> None:
        """Restore proven prior checkpoints before persisting a later-stage pause.

        A manual list-state edit can temporarily mark an earlier stage pending
        without erasing the durable write checkpoint in ``source_record_json``.
        If processing subsequently reaches a later stage, that durable
        checkpoint is authoritative proof that the earlier stage already
        completed.  Reconcile only pending prior stages; never override a
        manual BLOCKED/NOT_REQUIRED decision.
        """

        requested_index = STAGE_ORDER.index(requested_stage)
        for stage in STAGE_ORDER[:requested_index]:
            _, complete_key, status_key, completed_key = STAGE_KEYS[stage]
            if not _truth(source_record.get(complete_key)):
                continue
            row = conn.execute(
                """
                SELECT state, metadata_json
                FROM custom_order_stages
                WHERE workflow_id = ? AND stage = ?
                """,
                (workflow_id, stage),
            ).fetchone()
            if (
                row is None
                or str(row["state"]) != str(WorkflowStageState.PENDING)
            ):
                continue
            result_status = str(source_record.get(status_key) or "").strip() or None
            completed_at = (
                str(source_record.get(completed_key) or "").strip() or now
            )
            conn.execute(
                """
                UPDATE custom_order_stages
                SET required = 1, state = ?, result_status = ?,
                    completed_at = ?, last_error = NULL, metadata_json = ?
                WHERE workflow_id = ? AND stage = ?
                """,
                (
                    str(WorkflowStageState.COMPLETED),
                    result_status,
                    completed_at,
                    _json(cls._clear_pause_metadata(row["metadata_json"])),
                    workflow_id,
                    stage,
                ),
            )
            cls._insert_event(
                conn,
                workflow_id,
                stage=stage,
                event_type="stage_checkpoint_reconciled",
                old_state=str(WorkflowStageState.PENDING),
                new_state=str(WorkflowStageState.COMPLETED),
                actor=actor,
                reason="后续阶段处理结果确认此前阶段已经完成。",
                details={
                    "source": "source_record_checkpoint",
                    "requested_stage": requested_stage,
                },
            )

    @staticmethod
    def _normalize_order_nos(platform_order_nos: Iterable[str]) -> list[str]:
        normalized_order_nos: list[str] = []
        seen: set[str] = set()
        for value in platform_order_nos:
            order_no = str(value or "").strip()
            if not order_no:
                raise ValueError("平台订单号不能为空。")
            if order_no not in seen:
                normalized_order_nos.append(order_no)
                seen.add(order_no)
        if not normalized_order_nos:
            raise ValueError("请至少选择一张定制订单。")
        return normalized_order_nos

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

    @staticmethod
    def _workflow_status_from_stages(stages: Mapping[str, Any]) -> str:
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
            return "blocked"
        if all(completed(stage) for stage in STAGE_ORDER):
            return "completed"
        return next(
            (
                STAGE_PENDING_STATUS[stage]
                for stage in STAGE_ORDER
                if not completed(stage)
            ),
            "pending",
        )

    def _refresh_workflow_status(self, conn: sqlite3.Connection, workflow_id: int, *, now: str) -> None:
        stages = {
            str(row["stage"]): row
            for row in conn.execute(
                "SELECT stage, required, state FROM custom_order_stages WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchall()
        }
        status = self._workflow_status_from_stages(stages)
        conn.execute(
            """
            UPDATE custom_order_workflows
            SET workflow_status = ?,
                processed_at = CASE
                    WHEN ? = 'completed' THEN COALESCE(processed_at, ?)
                    ELSE NULL
                END,
                not_required_reason = NULL,
                buyer_cancel_clear_streak = 0,
                buyer_cancel_clear_last_scan_id = NULL,
                buyer_cancel_clear_last_seen_at = NULL,
                version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (status, status, now, now, workflow_id),
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
