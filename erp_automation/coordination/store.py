"""SQLite-backed instance registry, resource leases and revision journal."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class LeaseConflict:
    resource: str
    owner_instance_id: str
    owner_display_name: str
    operation: str
    expires_at: float


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

                    CREATE TABLE IF NOT EXISTS coordination_instances (
                        instance_id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
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

                    CREATE TABLE IF NOT EXISTS coordination_events (
                        revision INTEGER PRIMARY KEY,
                        created_at REAL NOT NULL,
                        instance_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        resources_json TEXT NOT NULL,
                        summary TEXT NOT NULL
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
            self._initialized = True

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
    ) -> None:
        instance = self._validate_identifier(instance_id, label="instance_id")
        display = self._validate_identifier(
            display_name or instance, label="display_name", maximum=200
        )
        now = self._clock()
        expires_at = now + max(15.0, float(ttl_seconds))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO coordination_instances(
                    instance_id, display_name, created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at,
                    expires_at = excluded.expires_at
                """,
                (instance, display, now, now, expires_at),
            )

    def heartbeat(self, instance_id: str, *, ttl_seconds: float) -> None:
        instance = self._validate_identifier(instance_id, label="instance_id")
        now = self._clock()
        expires_at = now + max(15.0, float(ttl_seconds))
        with self._connect() as connection:
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

    def deregister(self, instance_id: str) -> None:
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

    def cleanup_expired(self) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM coordination_leases WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                "DELETE FROM coordination_instances WHERE expires_at <= ?",
                (now,),
            )
            connection.execute(
                "DELETE FROM coordination_requests WHERE created_at <= ?",
                (now - 7 * 24 * 60 * 60,),
            )

    def acquire(
        self,
        *,
        resources: Iterable[str],
        instance_id: str,
        request_id: str,
        operation: str,
        ttl_seconds: float,
    ) -> LeaseConflict | None:
        normalized_resources = self._normalize_resources(resources)
        if not normalized_resources:
            return None
        instance = self._validate_identifier(instance_id, label="instance_id")
        request = self._validate_identifier(request_id, label="request_id")
        operation_name = self._validate_identifier(
            operation, label="operation", maximum=120
        )
        now = self._clock()
        expires_at = now + max(5.0, float(ttl_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM coordination_leases WHERE expires_at <= ?",
                (now,),
            )
            placeholders = ",".join("?" for _ in normalized_resources)
            conflict = connection.execute(
                f"""
                SELECT
                    lease.resource,
                    lease.owner_instance_id,
                    COALESCE(instance.display_name, lease.owner_instance_id)
                        AS owner_display_name,
                    lease.operation,
                    lease.expires_at
                FROM coordination_leases AS lease
                LEFT JOIN coordination_instances AS instance
                    ON instance.instance_id = lease.owner_instance_id
                WHERE lease.resource IN ({placeholders})
                  AND lease.owner_instance_id <> ?
                ORDER BY lease.resource
                LIMIT 1
                """,
                (*normalized_resources, instance),
            ).fetchone()
            if conflict is not None:
                connection.rollback()
                return LeaseConflict(
                    resource=str(conflict["resource"]),
                    owner_instance_id=str(conflict["owner_instance_id"]),
                    owner_display_name=str(conflict["owner_display_name"]),
                    operation=str(conflict["operation"]),
                    expires_at=float(conflict["expires_at"]),
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
                    COALESCE(instance.display_name, lease.owner_instance_id)
                        AS owner_display_name,
                    lease.operation,
                    lease.task_id,
                    lease.acquired_at,
                    lease.expires_at
                FROM coordination_leases AS lease
                LEFT JOIN coordination_instances AS instance
                    ON instance.instance_id = lease.owner_instance_id
                WHERE lease.expires_at > ?
                ORDER BY lease.resource
                """,
                (now,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                    resources_json, summary
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    revision,
                    self._clock(),
                    instance,
                    operation_name,
                    json.dumps(normalized_resources, ensure_ascii=False),
                    str(summary or "")[:500],
                ),
            )
            connection.commit()
        return revision

    def cached_response(self, request_id: str) -> dict[str, Any] | None:
        request = self._validate_identifier(request_id, label="request_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_json
                FROM coordination_requests
                WHERE request_id = ?
                """,
                (request,),
            ).fetchone()
        if row is None:
            return None
        decoded = json.loads(str(row["response_json"]))
        return decoded if isinstance(decoded, dict) else None

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
