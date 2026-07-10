from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    LogisticsDetail,
    QUEUE_STATUS_ERP_MARKED,
    QUEUE_STATUS_ERROR,
    QUEUE_STATUS_MANUAL_REVIEW,
    QUEUE_STATUS_NEW,
    QUEUE_STATUS_NOT_READY,
    QUEUE_STATUS_READY_TO_MARK,
    QueueStatusRecord,
    ReadyToMarkItem,
    ShipmentCandidate,
)


SCHEMA_VERSION = 1


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class QueueInsertResult:
    inserted: bool
    candidate: ShipmentCandidate
    existing: dict[str, Any] | None = None


class ShipmentQueueStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shipment_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    system_order_no TEXT NOT NULL,
                    platform_order_no TEXT NOT NULL,
                    als_no TEXT NOT NULL,
                    shipment_tag_name TEXT NOT NULL,
                    tag_text TEXT,
                    sku_text TEXT,
                    customer_remark TEXT,
                    status_text TEXT,
                    receiver_email TEXT,
                    carrier TEXT,
                    international_tracking_no TEXT,
                    logistics_order_no TEXT,
                    actual_total TEXT,
                    chargeable_weight_kg TEXT,
                    package_count INTEGER,
                    queue_status TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT,
                    email_sent_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_queue_als_no ON shipment_queue(als_no)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def get_by_als(self, als_no: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM shipment_queue WHERE als_no = ?",
                (als_no,),
            ).fetchone()
            return dict(row) if row else None

    def list_logistics_check_candidates(
        self,
        *,
        limit: int = 0,
        statuses: tuple[str, ...] = (QUEUE_STATUS_NEW, QUEUE_STATUS_NOT_READY, QUEUE_STATUS_ERROR),
    ) -> list[dict[str, Any]]:
        self.initialize()
        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            "SELECT * FROM shipment_queue "
            f"WHERE queue_status IN ({placeholders}) "
            "ORDER BY updated_at ASC, id ASC"
        )
        params: list[Any] = list(statuses)
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def list_ready_to_mark(self, *, limit: int = 0) -> list[ReadyToMarkItem]:
        self.initialize()
        sql = """
            SELECT system_order_no, platform_order_no, als_no, logistics_order_no,
                   carrier, international_tracking_no, actual_total, chargeable_weight_kg
            FROM shipment_queue
            WHERE queue_status = ?
            ORDER BY updated_at ASC, id ASC
        """
        params: list[Any] = [QUEUE_STATUS_READY_TO_MARK]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            ReadyToMarkItem(
                system_order_no=row["system_order_no"],
                platform_order_no=row["platform_order_no"],
                als_no=row["als_no"],
                logistics_order_no=row["logistics_order_no"],
                carrier=row["carrier"],
                international_tracking_no=row["international_tracking_no"],
                actual_total=row["actual_total"],
                chargeable_weight_kg=row["chargeable_weight_kg"],
            )
            for row in rows
        ]

    def list_erp_mark_candidates(self, *, limit: int = 0) -> list[ReadyToMarkItem]:
        self.initialize()
        sql = """
            SELECT system_order_no, platform_order_no, als_no, logistics_order_no,
                   carrier, international_tracking_no, actual_total, chargeable_weight_kg
            FROM shipment_queue
            WHERE queue_status = ?
               OR (
                    queue_status = ?
                    AND COALESCE(carrier, '') <> ''
                    AND COALESCE(international_tracking_no, '') <> ''
                    AND COALESCE(logistics_order_no, '') <> ''
                    AND COALESCE(actual_total, '') <> ''
                    AND COALESCE(chargeable_weight_kg, '') <> ''
               )
            ORDER BY CASE WHEN queue_status = ? THEN 0 ELSE 1 END, updated_at ASC, id ASC
        """
        params: list[Any] = [QUEUE_STATUS_READY_TO_MARK, QUEUE_STATUS_ERROR, QUEUE_STATUS_READY_TO_MARK]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            ReadyToMarkItem(
                system_order_no=row["system_order_no"],
                platform_order_no=row["platform_order_no"],
                als_no=row["als_no"],
                logistics_order_no=row["logistics_order_no"],
                carrier=row["carrier"],
                international_tracking_no=row["international_tracking_no"],
                actual_total=row["actual_total"],
                chargeable_weight_kg=row["chargeable_weight_kg"],
            )
            for row in rows
        ]

    def list_queue_records_by_statuses(
        self,
        statuses: tuple[str, ...],
        *,
        limit: int = 0,
    ) -> list[QueueStatusRecord]:
        self.initialize()
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        sql = (
            "SELECT system_order_no, platform_order_no, als_no, queue_status, last_error "
            "FROM shipment_queue "
            f"WHERE queue_status IN ({placeholders}) "
            "ORDER BY updated_at ASC, id ASC"
        )
        params: list[Any] = list(statuses)
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            QueueStatusRecord(
                system_order_no=row["system_order_no"],
                platform_order_no=row["platform_order_no"],
                als_no=row["als_no"],
                queue_status=row["queue_status"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def list_logistics_skipped_records(self, *, limit: int = 0) -> list[QueueStatusRecord]:
        return self.list_queue_records_by_statuses(
            (QUEUE_STATUS_READY_TO_MARK, QUEUE_STATUS_MANUAL_REVIEW),
            limit=limit,
        )

    def reset_manual_review_errors_to_error(self, *, keywords: tuple[str, ...], last_error: str) -> int:
        self.initialize()
        normalized_keywords = tuple(keyword for keyword in keywords if keyword)
        if not normalized_keywords:
            return 0
        now = _now_text()
        conditions = " OR ".join("last_error LIKE ?" for _ in normalized_keywords)
        params: list[Any] = [
            QUEUE_STATUS_ERROR,
            last_error,
            now,
            QUEUE_STATUS_MANUAL_REVIEW,
            *[f"%{keyword}%" for keyword in normalized_keywords],
        ]
        with self.connect() as conn:
            result = conn.execute(
                f"""
                UPDATE shipment_queue
                SET queue_status = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE queue_status = ?
                  AND ({conditions})
                """,
                tuple(params),
            )
            return result.rowcount

    def update_erp_mark_by_als(
        self,
        als_no: str,
        *,
        queue_status: str = QUEUE_STATUS_ERP_MARKED,
        last_error: str | None = None,
        processed: bool = True,
    ) -> bool:
        self.initialize()
        now = _now_text()
        processed_at = now if processed else None
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE shipment_queue
                SET queue_status = ?,
                    last_error = ?,
                    updated_at = ?,
                    processed_at = COALESCE(?, processed_at)
                WHERE als_no = ?
                """,
                (queue_status, last_error, now, processed_at, als_no),
            )
            return result.rowcount > 0

    def update_logistics_by_als(
        self,
        als_no: str,
        detail: LogisticsDetail,
        *,
        queue_status: str,
        last_error: str | None,
    ) -> bool:
        self.initialize()
        now = _now_text()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE shipment_queue
                SET carrier = ?,
                    international_tracking_no = ?,
                    logistics_order_no = ?,
                    actual_total = ?,
                    chargeable_weight_kg = ?,
                    package_count = ?,
                    queue_status = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE als_no = ?
                """,
                (
                    detail.carrier,
                    detail.international_tracking_no,
                    detail.logistics_order_no,
                    detail.actual_total,
                    detail.chargeable_weight_kg,
                    detail.package_count,
                    queue_status,
                    last_error,
                    now,
                    als_no,
                ),
            )
            return result.rowcount > 0

    def insert_candidate(self, candidate: ShipmentCandidate) -> QueueInsertResult:
        self.initialize()
        now = _now_text()
        values = {
            "system_order_no": candidate.system_order_no,
            "platform_order_no": candidate.platform_order_no,
            "als_no": candidate.als_no,
            "shipment_tag_name": candidate.shipment_tag_name,
            "tag_text": candidate.tag_text,
            "sku_text": candidate.sku_text,
            "customer_remark": candidate.customer_remark,
            "status_text": candidate.status_text,
            "receiver_email": candidate.receiver_email,
            "carrier": candidate.carrier,
            "international_tracking_no": candidate.international_tracking_no,
            "logistics_order_no": candidate.logistics_order_no,
            "actual_total": candidate.actual_total,
            "chargeable_weight_kg": candidate.chargeable_weight_kg,
            "package_count": candidate.package_count,
            "queue_status": candidate.queue_status,
            "last_error": candidate.last_error,
            "created_at": now,
            "updated_at": now,
        }
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        try:
            with self.connect() as conn:
                conn.execute(
                    f"INSERT INTO shipment_queue ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(values[column] for column in columns),
                )
        except sqlite3.IntegrityError:
            return QueueInsertResult(False, candidate, self.get_by_als(candidate.als_no))
        return QueueInsertResult(True, candidate)

    def insert_candidates(self, candidates: list[ShipmentCandidate]) -> list[QueueInsertResult]:
        return [self.insert_candidate(candidate) for candidate in candidates]
