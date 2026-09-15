"""Durable, shared admission records: a failed or unknown write is never looped."""
from pathlib import Path

from lingxing_automation.storage.sqlite_connection import connect_database


class AutomaticDispatchStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect_database(path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS automatic_dispatches (
                    identity TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
            """)

    def claim(self, identity: str) -> bool:
        with connect_database(self.path) as connection:
            return connection.execute(
                "INSERT OR IGNORE INTO automatic_dispatches(identity) VALUES (?)", (identity,),
            ).rowcount == 1

    def contains(self, identity: str) -> bool:
        with connect_database(self.path) as connection:
            return connection.execute(
                "SELECT 1 FROM automatic_dispatches WHERE identity = ?", (identity,),
            ).fetchone() is not None

    def release_unstarted(self, identity: str) -> None:
        with connect_database(self.path) as connection:
            connection.execute("DELETE FROM automatic_dispatches WHERE identity = ?", (identity,))

    def candidates(self, kind: str, *, identity: str = "", limit: int = 1,
                   include_claimed: bool = False, excluded_orders: tuple[str, ...] = ()) -> list[dict]:
        if kind == "custom":
            source = """
                SELECT w.platform_order_no, w.original_system_order_no AS system_order_no,
                       'custom:' || w.platform_order_no AS dispatch_identity
                FROM custom_order_workflows w
                WHERE w.ignored = 0
                  AND w.workflow_status IN ('pending', 'folder_pending', 'sku_adjustment_pending',
                      'package_split_pending', 'instruction_remark_pending', 'warehouse_logistics_pending')
                  AND COALESCE(json_extract(w.source_record_json, '$.product_identity_state'), '') = ''
                  AND TRIM(COALESCE(w.original_system_order_no, '')) <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM custom_order_stages s WHERE s.workflow_id = w.id
                      AND (s.state = 'BLOCKED' OR COALESCE(s.last_error, '') <> ''
                           OR COALESCE(json_extract(s.metadata_json, '$.pause_kind'), '') <> ''
                           OR json_extract(s.metadata_json, '$.retry_confirmation_required') = 1)
                  )
            """
        elif kind == "shipment":
            source = """
                SELECT j.platform_order_no, j.system_order_no, j.logistics_no,
                       'shipment:' || j.logistics_no AS dispatch_identity
                FROM shipment_jobs j
                JOIN shipment_logistics l ON l.job_id = j.id
                JOIN shipment_erp e ON e.job_id = j.id
                WHERE j.identity_state = 'ACTIVE' AND l.state = 'READY' AND e.state = 'PENDING'
                  AND e.checkpoint = 'NONE' AND e.policy_block_code IS NULL
                  AND COALESCE(j.lease_owner, '') = '' AND COALESCE(e.last_error, '') = ''
                  AND TRIM(COALESCE(l.international_tracking_no, '')) <> ''
                  AND TRIM(COALESCE(l.carrier_normalized, l.carrier_raw, '')) <> ''
            """
        elif kind == "notification":
            # Follow the existing notification workflow: send available outbound
            # packages first, then only genuinely new customer-visible packages.
            # A full source snapshot is still required; full shipment is not.
            # Compare package keys across confirmed history, so tracking/text
            # corrections and manually reopened identical drafts do not resend.
            source = """
                SELECT n.id, n.platform_order_no, 'notification:' || n.id AS dispatch_identity
                FROM shipment_notifications n
                JOIN shipment_notification_outbound_eligibility e
                  ON e.platform_order_no = n.platform_order_no
                WHERE n.state = 'AWAITING_REVIEW' AND n.legacy_email_batch_id IS NULL
                  AND e.outbound_state = 'OUTBOUNDED' AND e.snapshot_complete = 1
                  AND TRIM(e.package_set_hash) <> ''
                  AND n.package_total > 0 AND n.package_complete > 0
                  AND COALESCE(n.last_error, '') = '' AND n.attempt_count = 0
                  AND COALESCE(n.provider_message_id, '') = ''
                  AND COALESCE(n.provider_status, '') = ''
                  AND COALESCE(n.approved_at, '') = ''
                  AND COALESCE(n.approved_content_hash, '') = ''
                  AND COALESCE(n.sent_at, '') = '' AND COALESCE(n.delivered_at, '') = ''
                  AND n.id = (SELECT MAX(latest.id) FROM shipment_notifications latest
                      WHERE latest.platform_order_no = n.platform_order_no
                        AND latest.legacy_email_batch_id IS NULL)
                  AND NOT EXISTS (SELECT 1 FROM shipment_notifications prior
                      WHERE prior.platform_order_no = n.platform_order_no AND prior.id <> n.id
                        AND (prior.state IN ('SENDING', 'DELIVERY_UNCONFIRMED', 'RETRYABLE', 'FAILED')
                          OR (prior.state NOT IN ('ACCEPTED', 'DELIVERED', 'MANUALLY_COMPLETED')
                            AND (prior.attempt_count > 0 OR COALESCE(prior.provider_message_id, '') <> ''
                              OR COALESCE(prior.sent_at, '') <> '' OR COALESCE(prior.delivered_at, '') <> ''))))
                  AND EXISTS (
                      SELECT 1 FROM shipment_notification_items current_item
                      WHERE current_item.notification_id = n.id
                        AND current_item.customer_visible = 1 AND current_item.is_complete = 1
                        AND TRIM(COALESCE(current_item.final_tracking_no, '')) <> ''
                        AND NOT EXISTS (
                            SELECT 1 FROM shipment_notification_items sent_item
                            JOIN shipment_notifications sent ON sent.id = sent_item.notification_id
                            WHERE sent.platform_order_no = n.platform_order_no AND sent.id <> n.id
                              AND sent.state IN ('ACCEPTED', 'DELIVERED', 'MANUALLY_COMPLETED')
                              AND sent_item.customer_visible = 1 AND sent_item.is_complete = 1
                              AND TRIM(COALESCE(sent_item.final_tracking_no, '')) <> ''
                              AND sent_item.package_key = current_item.package_key
                        )
                  )
            """
        else:
            raise ValueError(kind)
        sql = f"SELECT * FROM ({source}) candidate WHERE 1 = 1"
        if not include_claimed:
            sql += " AND NOT EXISTS (SELECT 1 FROM automatic_dispatches a WHERE a.identity = candidate.dispatch_identity)"
        params: list[object] = []
        if identity:
            sql += " AND candidate.dispatch_identity = ?"
            params.append(identity)
        if excluded_orders:
            sql += f" AND lower(candidate.platform_order_no) NOT IN ({','.join('?' for _ in excluded_orders)})"
            params.extend(order.casefold() for order in excluded_orders)
        sql += " ORDER BY candidate.dispatch_identity LIMIT ?"
        params.append(max(1, min(limit, 100)))
        with connect_database(self.path) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]
