"""SQLite queue paging adapter; no controller, transport or UI dependencies.

The SQL projection mirrors queue_policy's precedence and is checked against that
policy across persisted states. Parsing, tracking rules, time and status ordering
reuse the same pure functions. Only a page of identifiers leaves SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .models import shipment_tracking_attention_notice, shipment_tracking_deadline
from .queue_policy import (
    SHIPMENT_STATUS_PRIORITY, actual_total, has_live_lease, product_values,
    shipment_status_bucket, timestamp_value, tracking_validated,
)


def _text(value: object) -> str:
    return str(value or "")


def _register_functions(connection: sqlite3.Connection, now: datetime) -> None:
    observed_at = now.timestamp()

    def overdue(service, first_seen, carrier, tracking, logistics, identity, erp):
        return bool(shipment_tracking_attention_notice(
            customer_shipping_service=service, first_seen_at=first_seen,
            carrier=carrier, international_tracking_no=tracking,
            logistics_state=logistics, identity_state=identity, erp_state=erp,
            tracking_validated=tracking_validated(carrier, tracking, logistics), now=now,
        ))

    def next_transition(service, first_seen, lease_until):
        deadline = shipment_tracking_deadline(customer_shipping_service=service, first_seen_at=first_seen)
        candidates = (timestamp_value(lease_until), deadline.timestamp() if deadline else float("-inf"))
        return min((value for value in candidates if value > observed_at), default=None)

    functions = (
        ("queue_text", 1, _text),
        ("queue_state", 1, lambda value: _text(value).strip().upper()),
        ("queue_casefold", 1, lambda value: _text(value).casefold()),
        ("queue_present", 1, lambda value: bool(_text(value).strip())),
        ("queue_timestamp", 1, timestamp_value),
        ("queue_actual_total", 2, actual_total),
        ("queue_overdue", 7, overdue),
        ("queue_live_lease", 3, lambda owner, stage, until: has_live_lease(_text(owner), _text(stage), _text(until), now=now)),
        ("queue_next_transition", 3, next_transition),
        ("queue_bucket", 1, shipment_status_bucket),
        ("queue_priority", 1, lambda status: SHIPMENT_STATUS_PRIORITY.get(status, 99)),
        ("queue_products", 1, lambda value: json.dumps(product_values(_text(value)), ensure_ascii=False)),
    )
    for name, arity, function in functions:
        # 'now' is frozen for this connection's single read snapshot.
        connection.create_function(name, arity, function, deterministic=True)


def _review_snapshot(locks: Mapping[str, Any]) -> str:
    normalized = {
        key: {"reason": str(lock.get("reason") or "外部写入结果待核对。"),
              "created_at": datetime.fromtimestamp(float(lock["created_at"]), timezone.utc).isoformat()}
        for key, lock in locks.items() if lock
    }
    return json.dumps(normalized, ensure_ascii=False)


# Both sources have the same scalar columns. Issue tie order reproduces the
# former stable Python sort: issues (updated_at DESC, id DESC), then jobs by id.
_COLUMNS = (
    "platform_order_no", "system_order_no", "logistics_no", "product_type",
    "customer_shipping_service", "first_seen_at", "last_scanned_at", "updated_at",
    "identity_state_changed_at", "identity_state", "lease_owner", "lease_stage", "lease_until",
    "logistics_state", "carrier", "international_tracking_no", "actual_total", "chargeable_weight_kg",
    "logistics_state_changed_at", "erp_state", "erp_checkpoint", "erp_state_changed_at",
    "outbounded_at", "externally_completed_at", "re_mark_state", "re_mark_updated_at",
    "scan_issue_code", "scan_issue_state", "scan_issue_state_changed_at",
)
_JOB_EXPRESSIONS = {
    "carrier": "COALESCE(NULLIF(carrier_normalized, ''), carrier_raw)",
    "actual_total": "queue_actual_total(currency, fee_amount)",
    "scan_issue_code": "''", "scan_issue_state": "''", "scan_issue_state_changed_at": "''",
}
_ISSUE_EXPRESSIONS = {
    "platform_order_no": "platform_order_no", "system_order_no": "system_order_no",
    "first_seen_at": "first_seen_at", "last_scanned_at": "last_seen_at", "updated_at": "updated_at",
    "identity_state": "'SCAN_ERROR'", "erp_checkpoint": "'NONE'", "scan_issue_code": "issue_code",
    "scan_issue_state": "COALESCE(NULLIF(management_state, ''), 'ACTIVE')",
    "scan_issue_state_changed_at": "management_updated_at",
}

_STATUS_SQL = """
    CASE
      WHEN manual_reason <> '' AND (erp <> 'DONE' OR remark NOT IN ('', 'COMPLETED', 'CANCELLED')) THEN '标发需人工复核'
      WHEN manual_reason = '' AND override_status <> '' THEN override_status
      WHEN scan_issue_code <> '' THEN CASE issue_state
        WHEN 'MANUAL_REVIEW' THEN '标发需人工复核'
        WHEN 'MANUALLY_COMPLETED' THEN '已完成'
        WHEN 'MANUALLY_CANCELLED' THEN '已取消' ELSE '扫描错误' END
      WHEN remark = 'MANUAL_REVIEW' THEN '重新标发需人工复核'
      WHEN remark = 'COMPLETED' THEN '重新标发完成'
      WHEN remark NOT IN ('', 'CANCELLED') THEN '重新标发'
      WHEN identity = 'CANCELLED' THEN '本轮已取消'
      WHEN identity = 'MANUALLY_CANCELLED' THEN '已取消'
      WHEN identity = 'PAUSED_TAG_REMOVED' THEN '标签已移除'
      WHEN identity NOT IN ('', 'ACTIVE') THEN '订单信息冲突'
      WHEN erp = 'DONE' THEN '已完成'
      WHEN logistics = 'CANCELLED' THEN '已取消'
      WHEN queue_overdue(customer_shipping_service, first_seen_at, carrier, international_tracking_no,
                         logistics, identity, erp) THEN '物流逾期异常'
      WHEN logistics IN ('', 'PENDING') THEN '待查询物流'
      WHEN logistics = 'WAITING' THEN '等待物流就绪'
      WHEN logistics = 'RETRYABLE' THEN '查询失败待重试'
      WHEN logistics <> 'READY' THEN '物流信息需复核'
      WHEN NOT (queue_present(carrier) AND queue_present(international_tracking_no)
                AND queue_present(actual_total) AND queue_present(chargeable_weight_kg)) THEN '物流信息需复核'
      WHEN erp = 'BLOCKED' THEN '标发需人工复核'
      WHEN queue_live_lease(lease_owner, lease_stage, lease_until) THEN '标发处理中'
      WHEN erp = 'RUNNING' OR checkpoint NOT IN ('', 'NONE') THEN '可继续标发'
      WHEN erp = 'RETRYABLE' THEN '标发失败可重试'
      ELSE '可标发'
    END
"""

_TIME_SQL = """
    queue_timestamp(CASE
      WHEN manual_reason <> '' THEN manual_created_at
      WHEN scan_issue_code <> '' THEN COALESCE(NULLIF(scan_issue_state_changed_at, ''), NULLIF(updated_at, ''), last_scanned_at)
      WHEN re_mark_state <> '' THEN COALESCE(NULLIF(re_mark_updated_at, ''), updated_at)
      WHEN erp = 'DONE' THEN COALESCE(NULLIF(outbounded_at, ''), NULLIF(externally_completed_at, ''), NULLIF(erp_state_changed_at, ''), updated_at)
      WHEN identity NOT IN ('', 'ACTIVE') THEN COALESCE(NULLIF(identity_state_changed_at, ''), updated_at)
      WHEN logistics <> 'READY' THEN COALESCE(NULLIF(logistics_state_changed_at, ''), updated_at)
      ELSE COALESCE(NULLIF(erp_state_changed_at, ''), updated_at)
    END)
"""


def read_page_index(
    connection: sqlite3.Connection,
    *,
    job_index_sql: str,
    page: int,
    page_size: int,
    status: str,
    search_field: str,
    search_query: str,
    product_types: Sequence[str],
    active_statuses: Mapping[str, str],
    review_locks: Mapping[str, Any],
    now: datetime,
    include_facets: bool = True,
) -> dict[str, Any]:
    """Read bounded page ids and aggregates from the caller's read transaction."""

    _register_functions(connection, now)
    field = search_field if search_field in {"platform_order_no", "system_order_no"} else "platform_order_no"
    jobs = ", ".join(f"queue_text({_JOB_EXPRESSIONS.get(name, name)}) AS {name}" for name in _COLUMNS)
    issue_expressions = (_ISSUE_EXPRESSIONS.get(name, "''") for name in _COLUMNS)
    issues = ", ".join(f"queue_text({expression}) AS {name}" for name, expression in zip(_COLUMNS, issue_expressions))
    sql = f"""
        WITH raw_jobs AS ({job_index_sql} WHERE j.identity_state <> 'SUPERSEDED'),
        queue_rows AS (
          SELECT 'job' AS kind, id AS record_id, 1 AS source_order,
                 '' AS issue_order_time, id AS source_id, {jobs} FROM raw_jobs
          UNION ALL
          SELECT 'issue', id, 0, updated_at, -id, {issues}
          FROM shipment_scan_issues WHERE resolved_at IS NULL OR management_state <> 'ACTIVE'
        ),
        locks AS MATERIALIZED (SELECT key, value FROM json_each(:locks)),
        overrides AS MATERIALIZED (SELECT key, value FROM json_each(:overrides)),
        normalized AS (
          SELECT q.*, queue_state(identity_state) AS identity, queue_state(logistics_state) AS logistics,
                 queue_state(erp_state) AS erp, queue_state(erp_checkpoint) AS checkpoint,
                 queue_state(re_mark_state) AS remark, queue_state(scan_issue_state) AS issue_state,
                 COALESCE(json_extract(locks.value, '$.reason'), '') AS manual_reason,
                 COALESCE(json_extract(locks.value, '$.created_at'), '') AS manual_created_at,
                 COALESCE(NULLIF(CAST(overrides.value AS TEXT), ''), '') AS override_status
          FROM queue_rows q
          LEFT JOIN locks ON locks.key = queue_casefold(q.platform_order_no)
          LEFT JOIN overrides ON overrides.key = q.logistics_no
          WHERE :include_facets OR (
            (:needle = '' OR instr(queue_casefold(q.{field}), :needle) > 0)
            AND (:products = '[]' OR EXISTS (
              SELECT 1 FROM json_each(queue_products(q.product_type)) product
              JOIN json_each(:products) wanted ON wanted.value = queue_casefold(product.value)
            ))
          )
        ),
        effective AS MATERIALIZED (
          SELECT kind, record_id, source_order, issue_order_time, source_id,
                 platform_order_no, system_order_no, logistics_no, product_type,
                 {_STATUS_SQL} AS display_status, {_TIME_SQL} AS status_time,
                 queue_next_transition(customer_shipping_service, first_seen_at, lease_until) AS valid_until
          FROM normalized
        ),
        filtered AS (
          SELECT * FROM effective
          WHERE (:status = '' OR display_status = :status)
            AND (:needle = '' OR instr(queue_casefold({field}), :needle) > 0)
            AND (:products = '[]' OR EXISTS (
              SELECT 1 FROM json_each(queue_products(product_type)) product
              JOIN json_each(:products) wanted ON wanted.value = queue_casefold(product.value)
            ))
        ),
        counts AS (SELECT count(*) AS total FROM filtered),
        bounds AS (
          SELECT total, min(max(1, :page), max(1, (total + :size - 1) / :size)) AS page FROM counts
        ),
        selected AS (
          SELECT kind, record_id FROM filtered
          ORDER BY queue_bucket(display_status), queue_priority(display_status), status_time DESC,
                   platform_order_no, logistics_no, source_order, issue_order_time DESC, source_id
          LIMIT :size OFFSET (SELECT (page - 1) * :size FROM bounds)
        )
        SELECT total, page,
               (SELECT json_group_array(json_array(kind, record_id)) FROM selected) AS selected_json,
               CASE WHEN :include_facets THEN (
                 SELECT json_group_array(display_status) FROM (
                   SELECT DISTINCT display_status FROM effective WHERE display_status <> '' ORDER BY display_status
                 )
               ) END AS statuses_json,
               CASE WHEN :include_facets THEN (
                 SELECT json_group_array(product) FROM (
                   SELECT DISTINCT p.value AS product FROM effective e, json_each(queue_products(e.product_type)) p
                   ORDER BY product
                 )
               ) END AS products_json,
               (SELECT min(valid_until) FROM effective) AS facets_valid_until
        FROM bounds
    """
    size = max(1, min(int(page_size), 200))
    row = connection.execute(sql, {
        "page": int(page), "size": size, "status": str(status or "").strip(),
        "needle": str(search_query or "").strip().casefold(),
        "products": json.dumps(sorted({str(value).strip().casefold() for value in product_types if str(value).strip()}), ensure_ascii=False),
        "locks": _review_snapshot(review_locks),
        "overrides": json.dumps({key: str(value) for key, value in active_statuses.items() if value}, ensure_ascii=False),
        "include_facets": include_facets,
    }).fetchone()
    return {"page": int(row["page"]), "page_size": size, "total": int(row["total"]),
            "selected": json.loads(row["selected_json"]),
            "statuses": tuple(json.loads(row["statuses_json"])) if include_facets else None,
            "product_types": tuple(json.loads(row["products_json"])) if include_facets else None,
            "facets_valid_until": row["facets_valid_until"]}
