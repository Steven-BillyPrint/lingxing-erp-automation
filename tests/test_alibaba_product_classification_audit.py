from __future__ import annotations

import json
from datetime import date

from scripts.audit_alibaba_product_classification import (
    _classify,
    _windows,
    _write_reports,
)


def test_audit_windows_cover_six_months_in_bounded_ranges() -> None:
    windows = _windows(date(2026, 2, 27), date(2026, 8, 27))

    assert len(windows) == 7
    assert windows[0][0] < windows[0][1]
    assert windows[-1][0] < windows[-1][1]
    assert all(
        current_end - next_start == 1
        for (_current_start, current_end), (next_start, _next_end)
        in zip(windows, windows[1:])
    )


def test_audit_classifies_lingxing_product_no_without_raw_payload_output() -> None:
    row = _classify(
        {
            "global_order_no": "system-1",
            "order_number": "platform-1",
            "item_info": [
                {
                    "product_no": "B0D6KZ7G88",
                    "sku": "10ft-Full-Wall",
                }
            ],
            "receive_info": {"receiver_name": "must-not-leak"},
        }
    )

    assert row.state == "classified"
    assert row.category == "tent"
    assert row.matched_identifiers == "B0D6KZ7G88"
    assert "must-not-leak" not in repr(row)


def test_audit_reports_are_summary_only_plus_bounded_evidence_csv(tmp_path) -> None:
    row = _classify(
        {
            "global_order_no": "system-1",
            "order_number": "platform-1",
            "item_info": [{"product_no": "B0CQLN5GNL"}],
        }
    )

    csv_path, json_path = _write_reports(
        output_prefix=tmp_path / "audit",
        start=date(2026, 2, 27),
        end=date(2026, 8, 27),
        rows=(row,),
        request_count=7,
    )

    summary = json.loads(json_path.read_text(encoding="utf-8"))
    assert csv_path.exists()
    assert summary["read_only"] is True
    assert summary["order_count"] == 1
    assert summary["states"] == {"classified": 1}
    assert summary["categories"] == {"wall_decal": 1}
