"""SQLite-backed instance registry, resource leases and revision journal."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .access import OperatorIdentity


@dataclass(frozen=True)
class LeaseConflict:
    resource: str
    owner_instance_id: str
    owner_display_name: str
    operation: str
    expires_at: float
    owner_email: str = ""


class CoordinationStore:
    """Authoritative multi-instance coordination state.

    Every lease operation uses ``BEGIN IMMEDIATE`` so checking and claiming a
    set of resources is atomic even if the API is later served by more than one
    request thread.
    """

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._initialize_lock = threading.Lock()
        self._initialized = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS coordination_meta (
                        key TEXT PRIMARY KEY,
                        value INTEGER NOT NULL
                    );
                    INSERT OR IGNORE INTO coordination_meta(key, value)
                    VALUES ('revision', 0);
                    INSERT OR IGNORE INTO coordination_meta(key, value)
                    VALUES ('deployment_drain_until', 0);
                    INSERT OR IGNORE INTO coordination_meta(key, value)
                    VALUES ('global_execution_paused', 0);

                    CREATE TABLE IF NOT EXISTS coordination_instances (
                        instance_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        operator_email TEXT NOT NULL DEFAULT '',
                        operator_name TEXT NOT NULL DEFAULT '',
                        identity_subject TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS coordination_leases (
                        resource TEXT PRIMARY KEY,
                        owner_instance_id TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        task_id TEXT NOT NULL DEFAULT '',
                        acquired_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_coordination_leases_owner
                    ON coordination_leases(owner_instance_id);
                    CREATE INDEX IF NOT EXISTS idx_coordination_leases_task
                    ON coordination_leases(task_id);

                    CREATE TABLE IF NOT EXISTS coordination_scheduler_slots (
                        slot TEXT PRIMARY KEY,
                        owner_instance_id TEXT NOT NULL,
                        acquired_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS coordination_scheduler_jobs (
                        job_key TEXT PRIMARY KEY,
                        next_due_at REAL NOT NULL,
                        last_started_at REAL NOT NULL DEFAULT 0,
                        last_owner_instance_id TEXT NOT NULL DEFAULT '',
                        last_request_id TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS coordination_task_followups (
                        followup_id TEXT PRIMARY KEY,
                        followup_kind TEXT NOT NULL,
                        source_request_id TEXT NOT NULL UNIQUE,
                        source_task_id TEXT NOT NULL DEFAULT '',
                        source_instance_id TEXT NOT NULL,
                        operator_email TEXT NOT NULL DEFAULT '',
                        operator_name TEXT NOT NULL DEFAULT '',
                        identity_subject TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0,
                        submitted_task_id TEXT NOT NULL DEFAULT '',
                        claim_until REAL NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_coordination_followups_due
                    ON coordination_task_followups(state, next_attempt_at);
                    CREATE INDEX IF NOT EXISTS idx_coordination_followups_source
                    ON coordination_task_followups(source_task_id);
                    CREATE INDEX IF NOT EXISTS idx_coordination_followups_submitted
                    ON coordination_task_followups(submitted_task_id);

                    CREATE TABLE IF NOT EXISTS coordination_task_followup_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        followup_id TEXT NOT NULL,
                        attempted_at REAL NOT NULL,
                        outcome TEXT NOT NULL,
                        error TEXT NOT NULL DEFAULT '',
                        retry_at REAL NOT NULL DEFAULT 0,
                        submitted_task_id TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_coordination_followup_attempts
                    ON coordination_task_followup_attempts(followup_id, id);

                    CREATE TABLE IF NOT EXISTS coordination_events (
                        revision INTEGER PRIMARY KEY,
                        created_at REAL NOT NULL,
                        instance_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        resources_json TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        operator_email TEXT NOT NULL DEFAULT '',
                        operator_name TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS coordination_requests (
                        request_id TEXT PRIMARY KEY,
                        instance_id TEXT NOT NULL,
                        method TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_coordination_requests_created
                    ON coordination_requests(created_at);
                    """
                )
                self._ensure_column(
                    connection,
                    "coordination_instances",
                    "operator_email",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_instances",
                    "operator_name",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_instances",
                    "identity_subject",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_instances",
                    "browser_endpoint",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_instances",
                    "logistics_browser_endpoint",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_events",
                    "operator_email",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    connection,
                    "coordination_events",
                    "operator_name",
                    "TEXT NOT NULL DEFAULT ''",
                )
            self._initialized = True

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @staticmethod
    def _normalize_resources(resources: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    str(resource or "").strip().casefold()
                    for resource in resources
                    if str(resource or "").strip()
                }
            )
        )

    @staticmethod
    def _validate_identifier(value: str, *, label: str, maximum: int = 160) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{label} is invalid.")
        return normalized

    def register_instance(
        self,
        instance_id: str,
        display_name: str,
        *,
        ttl_seconds: float,
        identity: OperatorIdentity | None = None,
    ) -> None:
        instance = self._validate_identifier(instance_id, label="instance_id")
        operator_email = str(identity.email if identity else "").strip().casefold()
        operator_name = str(identity.name if identity else "").strip()
        identity_subject = str(identity.subject if identity else "").strip()
        display = self._validate_identifier(
            (
                f"{identity.display_name} / {display_name}"
                if identity and str(display_name or "").strip()
                else identity.display_name
                if identity
                else display_name or instance
            ),
            label="display_name",
            maximum=400,
        )
        now = self._clock()
        expires_at = now + max(15.0, float(ttl_seconds))
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT operator_email, identity_subject
                FROM coordination_instances
                WHERE instance_id = ?
                """,
                (instance,),
            ).fetchone()
            if existing is not None:
                existing_email = str(existing["operator_email"] or "").casefold()
                existing_subject = str(existing["identity_subject"] or "")
                if (
                    existing_email
                    and operator_email
                    and existing_email != operator_email
                ) or (
                    existing_subject
                    and identity_subject
                    and existing_subject != identity_subject
                ):
                    raise ValueError(
                        "Desktop instance is already bound to another Cloudflare user."
                    )
            connection.execute(
                """
                INSERT INTO coordination_instances(
                    instance_id, display_name, operator_email, operator_name,
                    identity_subject, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    operator_email = CASE
                        WHEN excluded.operator_email <> ''
                        THEN excluded.operator_email
                        ELSE coordination_instances.operator_email
                    END,
                    operator_name = CASE
                        WHEN excluded.operator_name <> ''
                        THEN excluded.operator_name
                        ELSE coordination_instances.operator_name
                    END,
                    identity_subject = CASE
                        WHEN excluded.identity_subject <> ''
                        THEN excluded.identity_subject
                        ELSE coordination_instances.identity_subject
                    END,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at
                """,
                (
                    instance,
                    display,
                    operator_email,
                    operator_name,
                    identity_subject,
                    now,
                    now,
                    expires_at,
                ),
            )

    def _set_browser_endpoint_column(
        self,
        instance_id: str,
        endpoint: str,
        *,
        column: str,
    ) -> None:
        if column not in {"browser_endpoint", "logistics_browser_endpoint"}:
            raise ValueError("Unsupported browser endpoint column.")
        instance = self._validate_identifier(instance_id, label="instance_id")
        normalized_endpoint = self._validate_identifier(
            endpoint,
            label=column,
            maximum=256,
        )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conflict = connection.execute(
                f"""
                SELECT instance_id
                FROM coordination_instances
                WHERE (browser_endpoint = ? OR logistics_browser_endpoint = ?)
                  AND NOT (instance_id = ? AND {column} = ?)
                  AND expires_at > ?
                LIMIT 1
                """,
                (
                    normalized_endpoint,
                    normalized_endpoint,
                    instance,
                    normalized_endpoint,
                    now,
                ),
            ).fetchone()
            if conflict is not None:
                raise ValueError("Desktop browser endpoint is already assigned.")
            updated = connection.execute(
                f"""
                UPDATE coordination_instances
                SET {column} = ?
                WHERE instance_id = ?
                """,
                (normalized_endpoint, instance),
            )
            if updated.rowcount != 1:
                raise KeyError(instance)

    def set_browser_endpoint(self, instance_id: str, endpoint: str) -> None:
        self._set_browser_endpoint_column(
            instance_id,
            endpoint,
            column="browser_endpoint",
        )

    def set_logistics_browser_endpoint(self, instance_id: str, endpoint: str) -> None:
        self._set_browser_endpoint_column(
            instance_id,
            endpoint,
            column="logistics_browser_endpoint",
        )

    def active_browser_endpoints(self) -> dict[str, str]:
        now = self._clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT instance_id, browser_endpoint
                FROM coordination_instances
                WHERE expires_at > ? AND browser_endpoint <> ''
                ORDER BY instance_id
                """,
                (now,),
            ).fetchall()
        return {
            str(row["instance_id"]): str(row["browser_endpoint"])
            for row in rows
        }

    def active_logistics_browser_endpoints(self) -> dict[str, str]:
        now = self._clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT instance_id, logistics_browser_endpoint
                FROM coordination_instances
                WHERE expires_at > ? AND logistics_browser_endpoint <> ''
                ORDER BY instance_id
                """,
                (now,),
            ).fetchall()
        return {
            str(row["instance_id"]): str(row["logistics_browser_endpoint"])
            for row in rows
        }

    def heartbeat(
        self,
        instance_id: str,
        *,
        ttl_seconds: float,
        identity: OperatorIdentity | None = None,
    ) -> None:
        instance = self._validate_identifier(instance_id, label="instance_id")
        now = self._clock()
        expires_at = now + max(15.0, float(ttl_seconds))
        with self._connect() as connection:
            if identity is not None:
                registered = connection.execute(
                    """
                    SELECT operator_email, identity_subject
                    FROM coordination_instances
                    WHERE instance_id = ?
                    """,
                    (instance,),
                ).fetchone()
                if registered is not None and (
                    str(registered["operator_email"] or "").casefold()
                    not in {"", identity.email.casefold()}
                    or str(registered["identity_subject"] or "")
                    not in {"", identity.subject}
                ):
                    raise ValueError(
                        "Desktop instance identity does not match its registration."
                    )
            updated = connection.execute(
                """
                UPDATE coordination_instances
                SET last_seen_at = ?, expires_at = ?
                WHERE instance_id = ?
                """,
                (now, expires_at, instance),
            )
            if updated.rowcount != 1:
                raise KeyError(instance)

    def deregister(self, instance_id: str) -> bool:
        instance = self._validate_identifier(instance_id, label="instance_id")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM coordination_leases WHERE owner_instance_id = ? AND task_id = ''",
                (instance,),
            )
            connection.execute(
                "DELETE FROM coordination_instances WHERE instance_id = ?",
                (instance,),
            )
            released_scheduler = connection.execute(
                """
                DELETE FROM coordination_scheduler_slots
                WHERE owner_instance_id = ?
                """,
                (instance,),
            ).rowcount > 0
        return released_scheduler

    def clear_task_leases(self) -> list[dict[str, Any]]:
        """Discard stale task leases and return them for crash recovery."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource, owner_instance_id, operation, task_id, expires_at
                FROM coordination_leases
                WHERE task_id <> ''
                ORDER BY task_id, resource
                """
            ).fetchall()
            connection.execute(
                "DELETE FROM coordination_leases WHERE task_id <> ''"
            )
        return [dict(row) for row in rows]

    def global_execution_paused(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM coordination_meta WHERE key = 'global_execution_paused'"
            ).fetchone()
        return bool(int(row["value"] or 0)) if row is not None else False

    def set_global_execution_paused(self, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO coordination_meta(key, value)
                VALUES ('global_execution_paused', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (1 if enabled else 0,),
            )

    def active_instance_ids(self) -> set[str]:
        now = self._clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT instance_id
                FROM coordination_instances
                WHERE expires_at > ?
                """,
                (now,),
            ).fetchall()
        return {str(row["instance_id"]) for row in rows}

    def cleanup_expired(self, *, include_task_leases: bool = True) -> None:
        now = self._clock()
        with self._connect() as connection:
            if include_task_leases:
                connection.execute(
                    "DELETE FROM coordination_leases WHERE expires_at <= ?",
                    (now,),
                )
            else:
                # A task lease is released only after the live coordinator
                # observes a terminal task. The service clears prior-process
                # task leases once at startup, so ordinary TTL cleanup must not
                # make a running task disappear during a snapshot outage.
                connection.execute(
                    """
                    DELETE FROM coordination_leases
                    WHERE task_id = '' AND expires_at <= ?
                    """,
                    (now,),
                )
            connection.execute(
                "DELETE FROM coordination_instances WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                "DELETE FROM coordination_scheduler_slots WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                "DELETE FROM coordination_requests WHERE created_at <= ?",
                (now - 7 * 24 * 60 * 60,),
            )

    def compact_legacy_read_responses(
        self,
        read_methods: Sequence[str],
        *,
        minimum_reclaim_bytes: int = 16 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Back up, delete, and vacuum obsolete cached read RPC responses.

        Callers must invoke this only during offline startup maintenance, before
        the coordination HTTP server accepts requests and after confirming no
        task was recovered. Mutation responses remain untouched because they
        provide idempotency for external writes.
        """

        methods = tuple(
            dict.fromkeys(str(value or "").strip() for value in read_methods)
        )
        methods = tuple(value for value in methods if value)
        if not methods:
            return {"deleted": 0, "reclaimed_candidate_bytes": 0, "backup": ""}
        placeholders = ",".join("?" for _ in methods)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(response_json)), 0) "
                f"FROM coordination_requests WHERE method IN ({placeholders})",
                methods,
            ).fetchone()
        count = int(row[0] or 0)
        reclaim_bytes = int(row[1] or 0)
        if count <= 0 or reclaim_bytes < max(0, int(minimum_reclaim_bytes)):
            return {
                "deleted": 0,
                "candidate_count": count,
                "reclaimed_candidate_bytes": reclaim_bytes,
                "backup": "",
            }

        backup_root = self.path.parent / "coordination-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_path = backup_root / (
            f"{self.path.stem}-before-read-cache-cleanup-{timestamp}.sqlite3"
        )
        suffix = 1
        while backup_path.exists():
            backup_path = backup_root / (
                f"{self.path.stem}-before-read-cache-cleanup-{timestamp}-{suffix}.sqlite3"
            )
            suffix += 1
        with self._connect() as source, sqlite3.connect(backup_path) as target:
            source.backup(target)
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.casefold() != "ok":
                raise RuntimeError("Coordination database backup integrity check failed.")

        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM coordination_requests WHERE method IN ({placeholders})",
                methods,
            )
            connection.commit()
            connection.execute("VACUUM")
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.casefold() != "ok":
                raise RuntimeError("Coordination database integrity check failed after VACUUM.")
        return {
            "deleted": count,
            "candidate_count": count,
            "reclaimed_candidate_bytes": reclaim_bytes,
            "backup": str(backup_path),
        }

    def elect_scheduler(
        self,
        instance_id: str,
        *,
        ttl_seconds: float,
        slot: str = "automatic_scans",
    ) -> dict[str, Any]:
        """Atomically renew or elect one online client as scheduler leader."""

        instance = self._validate_identifier(instance_id, label="instance_id")
        normalized_slot = self._validate_identifier(
            slot,
            label="scheduler slot",
            maximum=120,
        )
        now = self._clock()
        expires_at = now + max(5.0, float(ttl_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT slot.owner_instance_id, slot.expires_at,
                       instance.expires_at AS instance_expires_at
                FROM coordination_scheduler_slots AS slot
                LEFT JOIN coordination_instances AS instance
                  ON instance.instance_id = slot.owner_instance_id
                WHERE slot.slot = ?
                """,
                (normalized_slot,),
            ).fetchone()
            previous_owner = (
                str(previous["owner_instance_id"]) if previous is not None else ""
            )
            previous_valid = bool(
                previous is not None
                and float(previous["expires_at"]) > now
                and previous["instance_expires_at"] is not None
                and float(previous["instance_expires_at"]) > now
            )
            if previous_valid and previous_owner != instance:
                owner = previous_owner
                leader_expires_at = float(previous["expires_at"])
            else:
                acquired_at = (
                    now
                    if previous_owner != instance
                    else connection.execute(
                        """
                        SELECT acquired_at
                        FROM coordination_scheduler_slots
                        WHERE slot = ?
                        """,
                        (normalized_slot,),
                    ).fetchone()["acquired_at"]
                )
                connection.execute(
                    """
                    INSERT INTO coordination_scheduler_slots(
                        slot, owner_instance_id, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(slot) DO UPDATE SET
                        owner_instance_id = excluded.owner_instance_id,
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at
                    """,
                    (normalized_slot, instance, acquired_at, expires_at),
                )
                owner = instance
                leader_expires_at = expires_at
            connection.commit()
        return {
            "slot": normalized_slot,
            "owner_instance_id": owner,
            "is_leader": owner == instance,
            "expires_at": leader_expires_at,
            "changed": owner != previous_owner,
        }

    def scheduled_job_due_times(
        self,
        intervals: dict[str, float],
    ) -> dict[str, float]:
        """Return persistent due times, creating each job on its first use."""

        normalized: dict[str, float] = {}
        for job_key, interval_seconds in intervals.items():
            job = self._validate_identifier(
                job_key,
                label="scheduler job",
                maximum=120,
            )
            normalized[job] = max(5.0, float(interval_seconds))
        if not normalized:
            return {}
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for job, interval_seconds in normalized.items():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO coordination_scheduler_jobs(
                        job_key, next_due_at
                    ) VALUES (?, ?)
                    """,
                    (job, now + interval_seconds),
                )
            placeholders = ",".join("?" for _ in normalized)
            rows = connection.execute(
                f"""
                SELECT job_key, next_due_at
                FROM coordination_scheduler_jobs
                WHERE job_key IN ({placeholders})
                """,
                tuple(normalized),
            ).fetchall()
            connection.commit()
        return {
            str(row["job_key"]): float(row["next_due_at"])
            for row in rows
        }

    def claim_scheduled_job(
        self,
        *,
        job_key: str,
        interval_seconds: float,
        instance_id: str,
        request_id: str,
        slot: str = "automatic_scans",
    ) -> dict[str, Any]:
        """Atomically authorize one due run by the current scheduler leader."""

        job = self._validate_identifier(
            job_key,
            label="scheduler job",
            maximum=120,
        )
        instance = self._validate_identifier(instance_id, label="instance_id")
        request = self._validate_identifier(request_id, label="request_id")
        normalized_slot = self._validate_identifier(
            slot,
            label="scheduler slot",
            maximum=120,
        )
        interval = max(5.0, float(interval_seconds))
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            leader = connection.execute(
                """
                SELECT slot.owner_instance_id, slot.expires_at,
                       instance.expires_at AS instance_expires_at
                FROM coordination_scheduler_slots AS slot
                LEFT JOIN coordination_instances AS instance
                  ON instance.instance_id = slot.owner_instance_id
                WHERE slot.slot = ?
                """,
                (normalized_slot,),
            ).fetchone()
            leader_instance_id = (
                str(leader["owner_instance_id"]) if leader is not None else ""
            )
            is_leader = bool(
                leader is not None
                and leader_instance_id == instance
                and float(leader["expires_at"]) > now
                and leader["instance_expires_at"] is not None
                and float(leader["instance_expires_at"]) > now
            )
            if not is_leader:
                connection.rollback()
                return {
                    "claimed": False,
                    "reason": "not_scheduler_leader",
                    "owner_instance_id": leader_instance_id,
                    "next_due_at": 0.0,
                }

            connection.execute(
                """
                INSERT OR IGNORE INTO coordination_scheduler_jobs(
                    job_key, next_due_at
                ) VALUES (?, ?)
                """,
                (job, now + interval),
            )
            row = connection.execute(
                """
                SELECT next_due_at, last_request_id
                FROM coordination_scheduler_jobs
                WHERE job_key = ?
                """,
                (job,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("Scheduler job was not created.")
            next_due_at = float(row["next_due_at"])
            if str(row["last_request_id"] or "") == request:
                connection.commit()
                return {
                    "claimed": True,
                    "reason": "request_replay",
                    "owner_instance_id": instance,
                    "next_due_at": next_due_at,
                }
            if next_due_at > now:
                connection.rollback()
                return {
                    "claimed": False,
                    "reason": "not_due",
                    "owner_instance_id": instance,
                    "next_due_at": next_due_at,
                }
            next_due_at = now + interval
            connection.execute(
                """
                UPDATE coordination_scheduler_jobs
                SET next_due_at = ?,
                    last_started_at = ?,
                    last_owner_instance_id = ?,
                    last_request_id = ?
                WHERE job_key = ?
                """,
                (next_due_at, now, instance, request, job),
            )
            connection.commit()
        return {
            "claimed": True,
            "reason": "due",
            "owner_instance_id": instance,
            "next_due_at": next_due_at,
        }

    def defer_scheduled_job(
        self,
        request_id: str,
        *,
        retry_seconds: float = 60.0,
    ) -> None:
        """Allow a rejected claimed run to retry without creating a hot loop."""

        request = self._validate_identifier(request_id, label="request_id")
        retry_at = self._clock() + max(5.0, float(retry_seconds))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE coordination_scheduler_jobs
                SET next_due_at = ?,
                    last_request_id = ''
                WHERE last_request_id = ?
                """,
                (retry_at, request),
            )

    def register_task_followup_intent(
        self,
        *,
        source_request_id: str,
        source_instance_id: str,
        followup_kind: str,
        operator_email: str = "",
        operator_name: str = "",
        identity_subject: str = "",
    ) -> dict[str, Any]:
        """Durably register a follow-up before its source task is accepted."""

        request = self._validate_identifier(
            source_request_id,
            label="source request",
        )
        instance = self._validate_identifier(
            source_instance_id,
            label="source instance",
        )
        kind = self._validate_identifier(
            followup_kind,
            label="follow-up kind",
            maximum=80,
        )
        followup_id = self._validate_identifier(
            f"{kind}:{request}",
            label="follow-up id",
        )
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO coordination_task_followups(
                    followup_id, followup_kind, source_request_id,
                    source_instance_id, operator_email, operator_name,
                    identity_subject, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'REGISTERING', ?, ?)
                ON CONFLICT(source_request_id) DO NOTHING
                """,
                (
                    followup_id,
                    kind,
                    request,
                    instance,
                    str(operator_email or "")[:320],
                    str(operator_name or "")[:200],
                    str(identity_subject or "")[:512],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM coordination_task_followups "
                "WHERE source_request_id = ?",
                (request,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("Persistent follow-up intent was not created.")
        return dict(row)

    def bind_task_followup_source(
        self,
        source_request_id: str,
        source_task_id: str,
    ) -> dict[str, Any]:
        """Attach the accepted source task to its durable follow-up intent."""

        request = self._validate_identifier(
            source_request_id,
            label="source request",
        )
        task = self._validate_identifier(source_task_id, label="source task")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE coordination_task_followups
                SET source_task_id = ?, state = 'WAITING_SOURCE',
                    next_attempt_at = 0, claim_until = 0,
                    last_error = '', updated_at = ?
                WHERE source_request_id = ?
                  AND state IN ('REGISTERING', 'WAITING_SOURCE')
                """,
                (task, now, request),
            )
            row = connection.execute(
                "SELECT * FROM coordination_task_followups "
                "WHERE source_request_id = ?",
                (request,),
            ).fetchone()
            connection.commit()
        if updated.rowcount != 1 or row is None:
            raise RuntimeError("Persistent follow-up source could not be bound.")
        return dict(row)

    def cancel_task_followup_intent(
        self,
        source_request_id: str,
        error: str,
    ) -> None:
        request = self._validate_identifier(
            source_request_id,
            label="source request",
        )
        now = self._clock()
        message = str(error or "source task was not accepted")[:1000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT followup_id FROM coordination_task_followups "
                "WHERE source_request_id = ?",
                (request,),
            ).fetchone()
            connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = 'CANCELLED', last_error = ?, claim_until = 0,
                    updated_at = ?
                WHERE source_request_id = ? AND state = 'REGISTERING'
                """,
                (message, now, request),
            )
            if row is not None:
                connection.execute(
                    """
                    INSERT INTO coordination_task_followup_attempts(
                        followup_id, attempted_at, outcome, error
                    ) VALUES (?, ?, 'SOURCE_REJECTED', ?)
                    """,
                    (str(row["followup_id"]), now, message),
                )
            connection.commit()

    def activate_task_followup(
        self,
        source_task_id: str,
        *,
        source_status: str,
        source_message: str = "",
    ) -> int:
        """Make a source task's durable follow-up due after terminal status."""

        task = self._validate_identifier(source_task_id, label="source task")
        normalized_status = str(source_status or "").strip().lower()
        now = self._clock()
        cancelled = normalized_status in {"cancelled", "paused"}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = ?, next_attempt_at = ?, claim_until = 0,
                    last_error = ?, updated_at = ?
                WHERE source_task_id = ? AND state = 'WAITING_SOURCE'
                """,
                (
                    "CANCELLED" if cancelled else "PENDING",
                    0 if cancelled else now,
                    str(source_message or "")[:1000] if cancelled else "",
                    now,
                    task,
                ),
            )
            connection.commit()
        return int(updated.rowcount)

    def recover_task_followups(self) -> int:
        """Requeue unfinished follow-ups after a coordinator process restart."""

        now = self._clock()
        message = "协调服务重启，未完成的客户通知补偿已恢复等待提交。"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT followup_id
                FROM coordination_task_followups
                WHERE state IN (
                    'REGISTERING', 'WAITING_SOURCE', 'CLAIMED', 'SUBMITTED'
                )
                """
            ).fetchall()
            updated = connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = 'PENDING', next_attempt_at = ?, claim_until = 0,
                    submitted_task_id = '', last_error = ?, updated_at = ?
                WHERE state IN (
                    'REGISTERING', 'WAITING_SOURCE', 'CLAIMED', 'SUBMITTED'
                )
                """,
                (now, message, now),
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO coordination_task_followup_attempts(
                        followup_id, attempted_at, outcome, error, retry_at
                    ) VALUES (?, ?, 'RECOVERED', ?, ?)
                    """,
                    (str(row["followup_id"]), now, message, now),
                )
            connection.commit()
        return int(updated.rowcount)

    def claim_due_task_followups(
        self,
        *,
        limit: int = 10,
        claim_seconds: float = 30.0,
    ) -> list[dict[str, Any]]:
        """Atomically claim due persistent follow-ups for one monitor pass."""

        now = self._clock()
        claim_until = now + max(1.0, float(claim_seconds))
        normalized_limit = max(1, min(100, int(limit)))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT followup_id
                FROM coordination_task_followups
                WHERE (state = 'PENDING' AND next_attempt_at <= ?)
                   OR (state = 'CLAIMED' AND claim_until <= ?)
                ORDER BY next_attempt_at, created_at
                LIMIT ?
                """,
                (now, now, normalized_limit),
            ).fetchall()
            ids = [str(row["followup_id"]) for row in rows]
            claimed: list[dict[str, Any]] = []
            for followup_id in ids:
                connection.execute(
                    """
                    UPDATE coordination_task_followups
                    SET state = 'CLAIMED', claim_until = ?, updated_at = ?
                    WHERE followup_id = ?
                    """,
                    (claim_until, now, followup_id),
                )
                row = connection.execute(
                    "SELECT * FROM coordination_task_followups "
                    "WHERE followup_id = ?",
                    (followup_id,),
                ).fetchone()
                if row is not None:
                    claimed.append(dict(row))
            connection.commit()
        return claimed

    def retry_task_followup(
        self,
        followup_id: str,
        *,
        error: str,
        initial_seconds: float,
        maximum_seconds: float,
        outcome: str = "RETRY",
    ) -> dict[str, Any]:
        followup = self._validate_identifier(
            followup_id,
            label="follow-up id",
        )
        now = self._clock()
        message = str(error or "follow-up submission failed")[:1000]
        initial = max(0.01, float(initial_seconds))
        maximum = max(initial, float(maximum_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt_count FROM coordination_task_followups "
                "WHERE followup_id = ?",
                (followup,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(followup)
            attempt_count = int(row["attempt_count"] or 0) + 1
            delay = min(maximum, initial * (2 ** min(20, attempt_count - 1)))
            retry_at = now + delay
            connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = 'PENDING', attempt_count = ?,
                    next_attempt_at = ?, claim_until = 0,
                    last_error = ?, updated_at = ?
                WHERE followup_id = ?
                """,
                (attempt_count, retry_at, message, now, followup),
            )
            connection.execute(
                """
                INSERT INTO coordination_task_followup_attempts(
                    followup_id, attempted_at, outcome, error, retry_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (followup, now, str(outcome or "RETRY")[:80], message, retry_at),
            )
            updated = connection.execute(
                "SELECT * FROM coordination_task_followups "
                "WHERE followup_id = ?",
                (followup,),
            ).fetchone()
            connection.commit()
        return dict(updated) if updated is not None else {}

    def mark_task_followup_submitted(
        self,
        followup_id: str,
        submitted_task_id: str,
    ) -> None:
        followup = self._validate_identifier(
            followup_id,
            label="follow-up id",
        )
        task = self._validate_identifier(submitted_task_id, label="submitted task")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = 'SUBMITTED', submitted_task_id = ?,
                    claim_until = 0, last_error = '', updated_at = ?
                WHERE followup_id = ?
                """,
                (task, now, followup),
            )
            connection.execute(
                """
                INSERT INTO coordination_task_followup_attempts(
                    followup_id, attempted_at, outcome, submitted_task_id
                ) VALUES (?, ?, 'SUBMITTED', ?)
                """,
                (followup, now, task),
            )
            connection.commit()

    def mark_task_followup_failed(
        self,
        followup_id: str,
        error: str,
        *,
        outcome: str = "FAILED",
    ) -> None:
        followup = self._validate_identifier(
            followup_id,
            label="follow-up id",
        )
        now = self._clock()
        message = str(error or "persistent follow-up failed")[:1000]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = 'FAILED', claim_until = 0,
                    last_error = ?, updated_at = ?
                WHERE followup_id = ?
                """,
                (message, now, followup),
            )
            connection.execute(
                """
                INSERT INTO coordination_task_followup_attempts(
                    followup_id, attempted_at, outcome, error
                ) VALUES (?, ?, ?, ?)
                """,
                (followup, now, str(outcome or "FAILED")[:80], message),
            )
            connection.commit()

    def complete_task_followup(
        self,
        submitted_task_id: str,
        *,
        succeeded: bool,
        message: str = "",
    ) -> int:
        task = self._validate_identifier(submitted_task_id, label="submitted task")
        now = self._clock()
        error = "" if succeeded else str(message or "补偿任务未成功完成")[:1000]
        outcome = "COMPLETED" if succeeded else "TASK_FAILED"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT followup_id FROM coordination_task_followups "
                "WHERE submitted_task_id = ? AND state = 'SUBMITTED'",
                (task,),
            ).fetchall()
            updated = connection.execute(
                """
                UPDATE coordination_task_followups
                SET state = ?, last_error = ?, updated_at = ?
                WHERE submitted_task_id = ? AND state = 'SUBMITTED'
                """,
                ("COMPLETED" if succeeded else "FAILED", error, now, task),
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO coordination_task_followup_attempts(
                        followup_id, attempted_at, outcome, error,
                        submitted_task_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(row["followup_id"]), now, outcome, error, task),
                )
            connection.commit()
        return int(updated.rowcount)

    def list_task_followups(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM coordination_task_followups "
                "ORDER BY created_at, followup_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_task_followup_attempts(
        self,
        followup_id: str,
    ) -> list[dict[str, Any]]:
        followup = self._validate_identifier(
            followup_id,
            label="follow-up id",
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM coordination_task_followup_attempts "
                "WHERE followup_id = ? ORDER BY id",
                (followup,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire(
        self,
        *,
        resources: Iterable[str],
        instance_id: str,
        request_id: str,
        operation: str,
        ttl_seconds: float,
        allow_during_deployment_drain: bool = False,
    ) -> LeaseConflict | None:
        normalized_resources = self._normalize_resources(resources)
        instance = self._validate_identifier(instance_id, label="instance_id")
        request = self._validate_identifier(request_id, label="request_id")
        operation_name = self._validate_identifier(
            operation, label="operation", maximum=120
        )
        now = self._clock()
        expires_at = now + max(5.0, float(ttl_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            drain_row = connection.execute(
                """
                SELECT value
                FROM coordination_meta
                WHERE key = 'deployment_drain_until'
                """
            ).fetchone()
            if (
                not allow_during_deployment_drain
                and drain_row is not None
                and int(drain_row["value"] or 0) > int(now)
            ):
                connection.rollback()
                return LeaseConflict(
                    resource="server:production-deployment",
                    owner_instance_id="server",
                    owner_display_name="ERP 服务器更新",
                    operation="production_deployment",
                    expires_at=float(drain_row["value"]),
                )
            if not normalized_resources:
                connection.rollback()
                return None
            connection.execute(
                """
                DELETE FROM coordination_leases
                WHERE task_id = '' AND expires_at <= ?
                """,
                (now,),
            )
            placeholders = ",".join("?" for _ in normalized_resources)
            conflict = connection.execute(
                f"""
                SELECT
                    lease.resource,
                    lease.owner_instance_id,
                    COALESCE(
                        NULLIF(instance.operator_name, ''),
                        instance.display_name,
                        lease.owner_instance_id
                    )
                        AS owner_display_name,
                    COALESCE(instance.operator_email, '') AS owner_email,
                    lease.operation,
                    lease.expires_at
                FROM coordination_leases AS lease
                LEFT JOIN coordination_instances AS instance
                    ON instance.instance_id = lease.owner_instance_id
                WHERE lease.resource IN ({placeholders})
                ORDER BY lease.resource
                LIMIT 1
                """,
                normalized_resources,
            ).fetchone()
            if conflict is not None:
                connection.rollback()
                return LeaseConflict(
                    resource=str(conflict["resource"]),
                    owner_instance_id=str(conflict["owner_instance_id"]),
                    owner_display_name=str(conflict["owner_display_name"]),
                    operation=str(conflict["operation"]),
                    expires_at=float(conflict["expires_at"]),
                    owner_email=str(conflict["owner_email"] or ""),
                )
            for resource in normalized_resources:
                connection.execute(
                    """
                    INSERT INTO coordination_leases(
                        resource, owner_instance_id, request_id, operation,
                        task_id, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, '', ?, ?)
                    ON CONFLICT(resource) DO UPDATE SET
                        owner_instance_id = excluded.owner_instance_id,
                        request_id = excluded.request_id,
                        operation = excluded.operation,
                        task_id = '',
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        resource,
                        instance,
                        request,
                        operation_name,
                        now,
                        expires_at,
                    ),
                )
            connection.commit()
        return None

    def bind_task(self, request_id: str, task_id: str, *, ttl_seconds: float) -> None:
        request = self._validate_identifier(request_id, label="request_id")
        task = self._validate_identifier(task_id, label="task_id")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE coordination_leases
                SET task_id = ?, expires_at = ?
                WHERE request_id = ?
                """,
                (task, self._clock() + max(30.0, float(ttl_seconds)), request),
            )

    def renew_task(self, task_id: str, *, ttl_seconds: float) -> None:
        task = self._validate_identifier(task_id, label="task_id")
        with self._connect() as connection:
            connection.execute(
                "UPDATE coordination_leases SET expires_at = ? WHERE task_id = ?",
                (self._clock() + max(30.0, float(ttl_seconds)), task),
            )

    def release_request(self, request_id: str) -> None:
        request = self._validate_identifier(request_id, label="request_id")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM coordination_leases WHERE request_id = ? AND task_id = ''",
                (request,),
            )

    def release_task(self, task_id: str) -> None:
        task = self._validate_identifier(task_id, label="task_id")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM coordination_leases WHERE task_id = ?",
                (task,),
            )

    def active_leases(self) -> list[dict[str, Any]]:
        now = self._clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    lease.resource,
                    lease.owner_instance_id,
                    COALESCE(
                        NULLIF(instance.operator_name, ''),
                        instance.display_name,
                        lease.owner_instance_id
                    )
                        AS owner_display_name,
                    COALESCE(instance.operator_email, '') AS owner_email,
                    lease.operation,
                    lease.task_id,
                    lease.acquired_at,
                    lease.expires_at
                FROM coordination_leases AS lease
                LEFT JOIN coordination_instances AS instance
                    ON instance.instance_id = lease.owner_instance_id
                WHERE lease.task_id <> '' OR lease.expires_at > ?
                ORDER BY lease.resource
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

    def instance_has_active_tasks(self, instance_id: str) -> bool:
        instance = self._validate_identifier(instance_id, label="instance_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM coordination_leases
                WHERE owner_instance_id = ?
                  AND task_id <> ''
                LIMIT 1
                """,
                (instance,),
            ).fetchone()
        return row is not None

    def current_revision(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM coordination_meta WHERE key = 'revision'"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def publish_event(
        self,
        *,
        instance_id: str,
        operation: str,
        resources: Iterable[str] = (),
        summary: str = "",
        identity: OperatorIdentity | None = None,
    ) -> int:
        instance = str(instance_id or "server")[:160]
        operation_name = str(operation or "state_changed")[:120]
        normalized_resources = self._normalize_resources(resources)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM coordination_meta WHERE key = 'revision'"
            ).fetchone()
            revision = int(row[0]) + 1
            connection.execute(
                "UPDATE coordination_meta SET value = ? WHERE key = 'revision'",
                (revision,),
            )
            connection.execute(
                """
                INSERT INTO coordination_events(
                    revision, created_at, instance_id, operation,
                    resources_json, summary, operator_email, operator_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision,
                    self._clock(),
                    instance,
                    operation_name,
                    json.dumps(normalized_resources, ensure_ascii=False),
                    str(summary or "")[:500],
                    str(identity.email if identity else "")[:320],
                    str(identity.name if identity else "")[:200],
                ),
            )
            connection.commit()
        return revision

    def cached_response(
        self,
        request_id: str,
        *,
        instance_id: str | None = None,
        method: str | None = None,
    ) -> dict[str, Any] | None:
        request = self._validate_identifier(request_id, label="request_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT instance_id, method, response_json
                FROM coordination_requests
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
        if row is None:
            return None
        if instance_id is not None and str(row["instance_id"]) != str(instance_id):
            raise ValueError("Request ID is already owned by another desktop instance.")
        if method is not None and str(row["method"]) != str(method):
            raise ValueError("Request ID is already bound to another operation.")
        decoded = json.loads(str(row["response_json"]))
        return decoded if isinstance(decoded, dict) else None

    def instance_identity(self, instance_id: str) -> OperatorIdentity | None:
        instance = self._validate_identifier(instance_id, label="instance_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT operator_email, operator_name, identity_subject
                FROM coordination_instances
                WHERE instance_id = ?
                """,
                (instance,),
            ).fetchone()
        if row is None or not str(row["operator_email"] or ""):
            return None
        return OperatorIdentity(
            email=str(row["operator_email"]),
            name=str(row["operator_name"] or row["operator_email"]),
            subject=str(row["identity_subject"] or row["operator_email"]),
        )

    def save_response(
        self,
        *,
        request_id: str,
        instance_id: str,
        method: str,
        response: dict[str, Any],
    ) -> None:
        request = self._validate_identifier(request_id, label="request_id")
        instance = self._validate_identifier(instance_id, label="instance_id")
        method_name = self._validate_identifier(method, label="method", maximum=120)
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO coordination_requests(
                    request_id, instance_id, method, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (request, instance, method_name, encoded, self._clock()),
            )
