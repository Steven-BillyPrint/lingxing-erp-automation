"""Record SQLite plans and approximate VM work for isolated synthetic pages."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from benchmark_shipment_pages import seed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10_000)
    args = parser.parse_args()
    if not 1 <= args.rows <= 100_000:
        parser.error("rows must be 1..100000")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from shipment_automation.queue_page_query import read_page_index
    from shipment_automation.queue_store import ShipmentWorkflowStore

    with tempfile.TemporaryDirectory(prefix="erp-query-plan-") as temporary:
        path = Path(temporary) / "queue.sqlite3"
        seed(path, args.rows)
        store = ShipmentWorkflowStore(path, read_only=True)
        output = {"rows": args.rows, "sqlite": sqlite3.sqlite_version, "cases": {}}
        for name, options in {
            "cold_first": {}, "cached_first": {"include_facets": False},
            "cold_search": {"search_query": "000", "include_facets": True},
            "cached_search": {"search_query": "000", "include_facets": False},
            "deep": {"page": max(1, args.rows // 100), "include_facets": False},
        }.items():
            with store.connect() as connection:
                connection.execute("BEGIN")
                progress = [0]
                plans = []

                def advance():
                    progress[0] += 1
                    return 0

                class ProfileConnection:
                    create_function = connection.create_function

                    def execute(self, sql, parameters):
                        plans.extend(dict(row) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))
                        connection.set_progress_handler(advance, 1000)
                        return connection.execute(sql, parameters)

                result = read_page_index(
                    ProfileConnection(), job_index_sql=store._queue_index_sql(),
                    **({"page": 1, "page_size": 50, "status": "", "search_field": "platform_order_no",
                        "search_query": "", "product_types": (), "active_statuses": {}, "review_locks": {},
                        "now": datetime.now(timezone.utc)} | options),
                )
                connection.set_progress_handler(None, 0)
                output["cases"][name] = {"approximate_vm_steps": progress[0] * 1000,
                                         "total": result["total"], "page_ids": len(result["selected"]),
                                         "plan": plans}
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
