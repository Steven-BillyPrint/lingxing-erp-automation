"""Non-sensitive hand-off state between quote preparation and draft filling."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class AlibabaOrderSession:
    instance_id: str
    system_order_no: str
    category: str
    baseline_draft_urls: tuple[str, ...]
    prepared_at: datetime


class AlibabaOrderSessionStore:
    """Persist only browser URL baselines; recipient PII is never stored."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            columns = connection.execute(
                "PRAGMA table_info(alibaba_order_sessions)"
            ).fetchall()
            column_names = {str(row["name"]) for row in columns}
            primary_key_columns = tuple(
                str(row["name"])
                for row in sorted(
                    (row for row in columns if int(row["pk"] or 0)),
                    key=lambda row: int(row["pk"]),
                )
            )
            legacy_table = bool(columns) and (
                "instance_id" not in column_names
                or primary_key_columns != ("instance_id", "system_order_no")
            )
            if legacy_table:
                connection.execute(
                    "DROP TABLE IF EXISTS alibaba_order_sessions_legacy"
                )
                connection.execute(
                    """
                    ALTER TABLE alibaba_order_sessions
                    RENAME TO alibaba_order_sessions_legacy
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alibaba_order_sessions (
                    instance_id TEXT NOT NULL,
                    system_order_no TEXT NOT NULL,
                    category TEXT NOT NULL,
                    baseline_draft_urls_json TEXT NOT NULL,
                    prepared_at TEXT NOT NULL,
                    PRIMARY KEY (instance_id, system_order_no)
                )
                """
            )
            if legacy_table:
                connection.execute(
                    """
                    INSERT INTO alibaba_order_sessions (
                        instance_id,
                        system_order_no,
                        category,
                        baseline_draft_urls_json,
                        prepared_at
                    )
                    SELECT
                        '',
                        system_order_no,
                        category,
                        baseline_draft_urls_json,
                        prepared_at
                    FROM alibaba_order_sessions_legacy
                    """
                )
                connection.execute(
                    "DROP TABLE alibaba_order_sessions_legacy"
                )

    def save(
        self,
        *,
        instance_id: str = "",
        system_order_no: str,
        category: str,
        baseline_draft_urls: tuple[str, ...],
    ) -> AlibabaOrderSession:
        normalized_instance = str(instance_id or "").strip()
        normalized = str(system_order_no or "").strip()
        if not normalized:
            raise ValueError("系统单号不能为空。")
        normalized_category = str(category or "").strip()
        if not normalized_category:
            raise ValueError("商品分类不能为空。")
        prepared_at = datetime.now(timezone.utc)
        urls = tuple(dict.fromkeys(str(value) for value in baseline_draft_urls if value))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alibaba_order_sessions (
                    instance_id,
                    system_order_no,
                    category,
                    baseline_draft_urls_json,
                    prepared_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, system_order_no) DO UPDATE SET
                    category = excluded.category,
                    baseline_draft_urls_json = excluded.baseline_draft_urls_json,
                    prepared_at = excluded.prepared_at
                """,
                (
                    normalized_instance,
                    normalized,
                    normalized_category,
                    json.dumps(urls, ensure_ascii=True),
                    prepared_at.isoformat(),
                ),
            )
        return AlibabaOrderSession(
            normalized_instance,
            normalized,
            normalized_category,
            urls,
            prepared_at,
        )

    def get(
        self,
        system_order_no: str,
        *,
        instance_id: str = "",
        max_age: timedelta = timedelta(hours=8),
    ) -> AlibabaOrderSession | None:
        normalized_instance = str(instance_id or "").strip()
        normalized = str(system_order_no or "").strip()
        if not normalized:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    instance_id,
                    system_order_no,
                    category,
                    baseline_draft_urls_json,
                    prepared_at
                FROM alibaba_order_sessions
                WHERE instance_id = ? AND system_order_no = ?
                """,
                (normalized_instance, normalized),
            ).fetchone()
        if row is None:
            return None
        try:
            prepared_at = datetime.fromisoformat(str(row["prepared_at"]).replace("Z", "+00:00"))
            if prepared_at.tzinfo is None:
                prepared_at = prepared_at.replace(tzinfo=timezone.utc)
            urls = tuple(json.loads(str(row["baseline_draft_urls_json"])))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if datetime.now(timezone.utc) - prepared_at.astimezone(timezone.utc) > max_age:
            return None
        return AlibabaOrderSession(
            instance_id=str(row["instance_id"]),
            system_order_no=str(row["system_order_no"]),
            category=str(row["category"]),
            baseline_draft_urls=urls,
            prepared_at=prepared_at,
        )

    def delete(self, system_order_no: str, *, instance_id: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM alibaba_order_sessions
                WHERE instance_id = ? AND system_order_no = ?
                """,
                (
                    str(instance_id or "").strip(),
                    str(system_order_no or "").strip(),
                ),
            )
