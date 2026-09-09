"""Compare shipment paging on synthetic data, in fresh isolated processes.

The same script supports an older checkout through --source-root. No production
database, UI session, network request or credentials are used.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

from benchmark_sqlite_resources import process_resources


STAMP = "2026-09-01T01:00:00+00:00"


def seed(path: Path, count: int) -> None:
    from shipment_automation.models import ShipmentCandidate
    from shipment_automation.queue_store import ShipmentWorkflowStore

    store = ShipmentWorkflowStore(path)
    store.upsert_candidate(ShipmentCandidate(
        system_order_no="SYS-0000000", platform_order_no="ORDER-0000000",
        logistics_no="ALS-0000000", shipment_tag_name="synthetic", sku_text="synthetic details",
    ))
    connection = store.connect()
    try:
        for table, key in (("shipment_jobs", "id"), ("shipment_logistics", "job_id"), ("shipment_erp", "job_id")):
            template = dict(connection.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())
            for column, value in template.items():
                if column.endswith("_at") and value is not None:
                    template[column] = STAMP
            columns = tuple(template)

            def values(index):
                row = dict(template)
                row[key] = index + 1
                if table == "shipment_jobs":
                    row.update(logistics_no=f"ALS-{index:07}", platform_order_no=f"ORDER-{index:07}",
                               system_order_no=f"SYS-{index:07}", product_type=("tent" if index % 2 else "x_stands"),
                               identity_state="CANCELLED" if index % 19 == 0 else "ACTIVE")
                elif table == "shipment_logistics":
                    row.update(state=("READY", "WAITING", "RETRYABLE")[index % 3], carrier_raw="UPS",
                               international_tracking_no="1Z9253126709651051", currency="CNY", fee_amount="10",
                               chargeable_weight_kg="1")
                else:
                    row.update(state="DONE" if index % 7 == 0 else "PENDING")
                return tuple(row[column] for column in columns)

            assignments = ",".join(f"{column}=?" for column in columns)
            connection.execute(f"UPDATE {table} SET {assignments} WHERE {key}=1", values(0))
            connection.executemany(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                (values(index) for index in range(1, count)),
            )
        connection.commit()
    finally:
        connection.close()


def digest_page(items, page, size, total, statuses, products) -> str:
    payload = {"items": [asdict(item) for item in items], "page": page, "page_size": size,
               "total": total, "statuses": statuses, "product_types": products}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def measure(path: Path, *, clients: int, repeats: int, count: int) -> dict:
    from erp_automation.application.queue_queries import paginate_shipment_rows, shipment_row_from_mapping
    from erp_automation.ui.controller import InMemoryBackgroundTaskController
    from shipment_automation.queue_store import ShipmentWorkflowStore

    apply_locks = InMemoryBackgroundTaskController._shipment_rows_with_review_locks
    locks = {f"order-{index:07}": {"reason": "synthetic review", "created_at": 1788224400, "source_area": "shipment"}
             for index in range(0, min(count, 1000), 10)}
    active = {f"ALS-{index:07}": "等待标发" for index in range(1, min(count, 500), 10)}
    cases = {"first": {}, "search": {"search_query": "000"},
             "deep": {"page": max(1, count // 100)}, "status": {"status": "可标发"}}
    before = process_resources()

    def client_run(_client):
        reader = ShipmentWorkflowStore(path, read_only=True)
        sql_paging = callable(getattr(reader, "list_queue_page", None))
        cache = None
        records = []
        for iteration in range(repeats):
            for name, options in cases.items():
                start = time.perf_counter()
                if sql_paging:
                    result = reader.list_queue_page(active_statuses=active, review_locks=locks,
                                                    cached_facets=cache, **options)
                    cache = result["facets_cache"]
                    items = apply_locks(tuple(shipment_row_from_mapping(row) for row in result["items"]), locks)
                    elapsed = time.perf_counter() - start
                    digest = digest_page(items, result["page"], result["page_size"], result["total"],
                                         result["statuses"], result["product_types"])
                else:
                    # Reproduce the original persistent controller algorithm,
                    # including search pushdown, global facets and page hydration.
                    revision = reader.queue_dataset_revision()
                    raw = reader.list_queue_index_rows(search_query=options.get("search_query", ""))
                    rows = apply_locks(tuple(shipment_row_from_mapping(row) for row in raw), locks)
                    result = paginate_shipment_rows(rows, active_statuses=active, **options)
                    if cache is None or cache[0] != revision:
                        all_rows = rows if not options.get("search_query") else apply_locks(tuple(
                            shipment_row_from_mapping(row) for row in reader.list_queue_index_rows()), locks)
                        cache = (revision, paginate_shipment_rows(all_rows, page_size=1, active_statuses=active).facets)
                    jobs = {row["logistics_no"]: shipment_row_from_mapping(row) for row in reader.list_jobs_by_logistics_nos(
                        tuple(row.logistics_no for row in result.items if row.logistics_no and not row.scan_issue_code))}
                    issues = {row.get("scan_issue_key"): shipment_row_from_mapping(row) for row in raw if row.get("scan_issue_key")}
                    items = apply_locks(tuple(issues.get(row.scan_issue_key) if row.scan_issue_code
                                              else jobs.get(row.logistics_no, row) for row in result.items), locks)
                    elapsed = time.perf_counter() - start
                    digest = digest_page(items, result.page, result.page_size, result.total,
                                         cache[1].statuses, cache[1].product_types)
                    # Match per-request ownership; do not carry full queues into
                    # the next request or explicitly invoke garbage collection.
                    del raw, rows, result, jobs, issues
                    if "all_rows" in locals():
                        del all_rows
                records.append({"client": _client, "iteration": iteration, "case": name,
                                "seconds": elapsed, "result_sha256": digest})
        return records

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=clients) as pool:
        measurements = [item for result in pool.map(client_run, range(clients)) for item in result]
    timings = {}
    for name in cases:
        values = sorted(item["seconds"] for item in measurements if item["case"] == name)
        timings[name] = {"p50_seconds": statistics.median(values),
                         "p95_seconds": values[max(0, math.ceil(len(values) * 0.95) - 1)]}
    return {"rows": count, "clients": clients, "repeats": repeats,
            "implementation": "sql" if hasattr(ShipmentWorkflowStore, "list_queue_page") else "all_index",
            "python": sys.version, "platform": sys.platform, "before": before, "after": process_resources(),
            "wall_seconds": time.perf_counter() - started, "timings": timings, "measurements": measurements}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--clients", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", choices=("seed", "measure"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (1 <= args.rows <= 100_000 and 1 <= args.clients <= 10 and 1 <= args.repeats <= 20):
        parser.error("rows must be 1..100000, clients 1..10, repeats 1..20")
    sys.path.insert(0, str(args.source_root.resolve()))
    if args.worker == "seed":
        seed(Path("queue.sqlite3"), args.rows)
    elif args.worker == "measure":
        print(json.dumps(measure(Path("queue.sqlite3"), clients=args.clients, repeats=args.repeats, count=args.rows), indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="erp-page-benchmark-") as temporary:
            command = [sys.executable, str(Path(__file__).resolve()), "--source-root", str(args.source_root.resolve()),
                       "--rows", str(args.rows), "--clients", str(args.clients), "--repeats", str(args.repeats)]
            subprocess.run([*command, "--worker", "seed"], cwd=temporary, check=True, capture_output=True, text=True)
            result = subprocess.run([*command, "--worker", "measure"], cwd=temporary, check=True, capture_output=True, text=True)
            print(result.stdout, end="")


if __name__ == "__main__":
    main()
