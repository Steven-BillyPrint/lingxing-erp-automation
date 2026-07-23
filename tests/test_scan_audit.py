from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from erp_automation.operations.scan_audit import (
    SCAN_AUDIT_SCHEMA,
    SCAN_AUDIT_VERSION,
    ScanAuditWriter,
    UnsafeScanAuditPathError,
    build_scan_audit_document,
    safe_query_summary,
    write_scan_audit,
)


STARTED = datetime(2026, 7, 14, 6, 30, tzinfo=timezone.utc)
FINISHED = STARTED + timedelta(seconds=3)


def _pages() -> list[dict[str, object]]:
    return [
        {
            "window_number": 2,
            "page_number": 1,
            "offset": 0,
            "requested_length": 500,
            "returned_count": 2,
            "declared_total": 2,
            "request_id": "safe-request-id",
            "api_code": "0",
            "response_time": "2026-07-14T06:30:01Z",
            "duration_ms": 37.5,
        }
    ]


def _decisions() -> list[dict[str, object]]:
    return [
        {
            "platform_order_no": "111-0000000-0000001",
            "system_order_no": "103000000000000001",
            "source_page": 1,
            "paid_at": "2026-07-14T05:00:00Z",
            "payment_status": "recent",
            "decision": "candidate",
            "reason_code": "matched_supported_product",
            "missing_fields": ["tag"],
            "custom_tag_text": "直接制作",
            "matched_asins": ["B0CRRGTPFH"],
            "items": [
                {
                    "order_item_id": "item-1",
                    "asin": "B0CRRGTPFH",
                    "sku": "canopytents",
                    "quantity_raw": "2",
                    "quantity_normalized": 2,
                    "quantity_status": "valid",
                }
            ],
        }
    ]


def _all_mapping_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key).casefold())
            keys.update(_all_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_all_mapping_keys(item))
    return keys


def test_writer_creates_atomic_per_task_document_with_required_schema(tmp_path: Path) -> None:
    result = write_scan_audit(
        tmp_path / "logs",
        task_id="task-001",
        scan_kind="customization",
        started_at=STARTED,
        finished_at=FINISHED,
        query={
            "date_type": "global_payment_time",
            "start_time": 1,
            "end_time": 2,
            "platform_code": [10001],
            "order_status": 4,
            "include_delete": False,
        },
        pages=_pages(),
        order_decisions=_decisions(),
        summary={
            "status": "complete",
            "row_count": 2,
            "evaluable_row_count": 1,
            "deduplicated_order_count": 1,
            "candidate_count": 1,
            "refreshed_count": 2,
            "queue_total_count": 29,
            "window_count": 2,
            "scan_start_time": 100,
            "scan_end_time": 200,
            "auto_paused_count": 3,
            "auto_resumed_count": 4,
            "immediate_logistics_count": 5,
            "immediate_erp_count": 6,
            "email_preview_backfill_count": 7,
            "skip_counts": {"payment_old": 1},
        },
    )

    local_started = STARTED.astimezone()
    expected = (
        tmp_path
        / "logs"
        / "custom_order_scan"
        / local_started.strftime("%Y-%m-%d")
        / f"custom_order_scan_{local_started.strftime('%Y%m%d_%H%M%S')}_task-001.json"
    )
    assert result.path == expected
    assert result.error_id is None
    document = json.loads(expected.read_text(encoding="utf-8"))
    assert document["schema"] == SCAN_AUDIT_SCHEMA
    assert document["version"] == SCAN_AUDIT_VERSION
    assert document["task_id"] == "task-001"
    assert document["scan_kind"] == "customization"
    assert document["started_at"] == "2026-07-14T06:30:00.000Z"
    assert document["finished_at"] == "2026-07-14T06:30:03.000Z"
    assert document["pagination"]["pages"][0]["item_count"] == 2
    assert document["pagination"]["pages"][0]["window_number"] == 2
    assert document["pagination"]["pages"][0]["request_id"] == "safe-request-id"
    assert document["order_decisions"][0]["custom_tag_text"] == "直接制作"
    assert document["order_decisions"][0]["missing_fields"] == ["tag"]
    item = document["order_decisions"][0]["items"][0]
    assert item["quantity_raw"] == "2"
    assert item["quantity_normalized"] == 2
    assert document["summary"]["skip_counts"] == {"payment_old": 1}
    assert document["summary"]["refreshed_count"] == 2
    assert document["summary"]["queue_total_count"] == 29
    assert document["summary"]["evaluable_row_count"] == 1
    assert document["summary"]["deduplicated_order_count"] == 1
    assert document["summary"]["window_count"] == 2
    assert document["summary"]["scan_start_time"] == 100
    assert document["summary"]["scan_end_time"] == 200
    assert document["summary"]["auto_paused_count"] == 3
    assert document["summary"]["auto_resumed_count"] == 4
    assert document["summary"]["immediate_logistics_count"] == 5
    assert document["summary"]["immediate_erp_count"] == 6
    assert document["summary"]["email_preview_backfill_count"] == 7
    assert list(expected.parent.glob("*.tmp")) == []


def test_writer_separates_scan_kinds_and_puts_local_start_time_in_names(tmp_path: Path) -> None:
    writer = ScanAuditWriter(tmp_path / "logs")
    custom = writer.write(
        task_id="custom-task",
        scan_kind="customization",
        started_at=STARTED,
        finished_at=FINISHED,
    )
    shipment = writer.write(
        task_id="shipment-task",
        scan_kind="shipment",
        started_at=STARTED,
        finished_at=FINISHED,
    )
    local_started = STARTED.astimezone()
    day = local_started.strftime("%Y-%m-%d")
    stamp = local_started.strftime("%Y%m%d_%H%M%S")

    assert custom.path.parent == tmp_path / "logs/custom_order_scan" / day
    assert custom.path.name == f"custom_order_scan_{stamp}_custom-task.json"
    assert shipment.path.parent == tmp_path / "logs/shipment_scan" / day
    assert shipment.path.name == f"shipment_scan_{stamp}_shipment-task.json"


def test_allow_lists_remove_authentication_contacts_addresses_and_raw_responses(
    tmp_path: Path,
) -> None:
    token = "token-value-that-must-not-survive"
    secret = "secret-value-that-must-not-survive"
    email = "buyer@example.com"
    phone = "+1 555 123 4567"
    address = "123 Main Street, Example City"

    try:
        raise RuntimeError(
            f"token={token} secret={secret} email={email} phone={phone} address={address}"
        )
    except RuntimeError as error:
        result = ScanAuditWriter(tmp_path / "logs").write(
            task_id="task-sensitive",
            scan_kind="shipment",
            started_at=STARTED,
            finished_at=FINISHED,
            query={
                "date_type": "global_payment_time",
                "access_token": token,
                "app_secret": secret,
                "buyer_email": email,
                "shipping_address": address,
            },
            pages=[
                {
                    **_pages()[0],
                    "raw_response": {"access_token": token, "buyer_email": email},
                    "response_body": f"{secret} {phone} {address}",
                    "payload": {"data": token},
                }
            ],
            order_decisions=[
                {
                    **_decisions()[0],
                    "reason": f"email={email}; phone={phone}; address={address}",
                    "buyer_email": email,
                    "receiver_phone": phone,
                    "shipping_address": address,
                    "customer_remark": f"{token} {secret}",
                    "raw_response": {"body": secret},
                    "tag_text": "帐篷标发",
                    "raw_tags": [{"buyer_email": email, "value": token}],
                }
            ],
            summary={
                "status": "failed",
                "candidate_count": 0,
                "raw_response": {"secret": secret},
                "message": email,
            },
            error=error,
        )

    encoded = result.path.read_text(encoding="utf-8")
    document = json.loads(encoded)
    lowered = encoded.casefold()
    for forbidden in (token, secret, email, phone, address):
        assert forbidden not in encoded
    forbidden_keys = {
        "access_token",
        "app_secret",
        "buyer_email",
        "shipping_address",
        "receiver_phone",
        "customer_remark",
        "raw_response",
        "response_body",
        "payload",
    }
    assert _all_mapping_keys(document).isdisjoint(forbidden_keys)
    # Structured order identifiers must remain searchable and must not be
    # mistaken for telephone numbers by free-text redaction.
    assert "111-0000000-0000001" in encoded
    assert "103000000000000001" in encoded
    assert document["order_decisions"][0]["custom_tag_text"] == "帐篷标发"
    assert "raw_tags" not in _all_mapping_keys(document)
    assert result.error_id


def test_exception_traceback_has_frames_but_no_locals_source_line_or_message(tmp_path: Path) -> None:
    local_secret = "unlabelled-bearer-material-xyz"

    def fail_inside_named_function() -> None:
        local_email = "hidden@example.com"
        local_phone = "+86 138 0013 8000"
        raise ValueError(f"{local_secret} {local_email} {local_phone}")

    try:
        fail_inside_named_function()
    except ValueError as error:
        result = ScanAuditWriter(tmp_path / "logs").write(
            task_id="task-error",
            scan_kind="customization",
            started_at=STARTED,
            finished_at=FINISHED,
            error=error,
        )

    document = json.loads(result.path.read_text(encoding="utf-8"))
    serialized = json.dumps(document, ensure_ascii=False)
    assert document["error_id"] == result.error_id
    assert document["error"]["exception_type"] == "ValueError"
    frames = document["error"]["traceback"][0]["frames"]
    assert any(frame["function"] == "fail_inside_named_function" for frame in frames)
    assert local_secret not in serialized
    assert "hidden@example.com" not in serialized
    assert "+86 138 0013 8000" not in serialized
    assert all(set(frame) == {"file", "line", "function"} for frame in frames)
    assert "local_email" not in serialized
    assert "raise ValueError" not in serialized


def test_query_summary_is_a_strict_allow_list() -> None:
    summary = safe_query_summary(
        {
            "startTime": 10,
            "end_time": 20,
            "platformCode": [10001],
            "access_token": "never-log-me",
            "url": "https://example.test/path?sign=never-log-me",
            "response": {"data": "never-log-me"},
        }
    )

    assert summary == {
        "start_time": 10,
        "end_time": 20,
        "platform_code": [10001],
    }


@pytest.mark.parametrize(
    "task_id",
    ["../escape", "..", ".", "folder/task", r"folder\task", " space", ""],
)
def test_task_id_cannot_escape_fixed_log_root(tmp_path: Path, task_id: str) -> None:
    with pytest.raises(ValueError, match="task_id"):
        ScanAuditWriter(tmp_path / "logs").write(
            task_id=task_id,
            scan_kind="customization",
            started_at=STARTED,
            finished_at=FINISHED,
        )
    assert not (tmp_path / "escape.json").exists()


def test_writer_rejects_symbolic_log_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked_logs = tmp_path / "logs"
    try:
        linked_logs.symlink_to(actual, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")

    with pytest.raises(UnsafeScanAuditPathError, match="符号链接|重解析点"):
        ScanAuditWriter(linked_logs).write(
            task_id="task-link-root",
            scan_kind="customization",
            started_at=STARTED,
            finished_at=FINISHED,
        )
    assert list(actual.rglob("*.json")) == []


def test_writer_rejects_symbolic_daily_directory(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    audit_root = logs / "shipment_scan"
    outside = tmp_path / "outside"
    audit_root.mkdir(parents=True)
    outside.mkdir()
    linked_day = audit_root / STARTED.astimezone().strftime("%Y-%m-%d")
    try:
        linked_day.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试符号链接：{exc}")

    with pytest.raises(UnsafeScanAuditPathError, match="符号链接|重解析点"):
        ScanAuditWriter(logs).write(
            task_id="task-link-day",
            scan_kind="shipment",
            started_at=STARTED,
            finished_at=FINISHED,
        )
    assert list(outside.rglob("*.json")) == []


def test_rewriting_same_task_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    writer = ScanAuditWriter(tmp_path / "logs")
    first = writer.write(
        task_id="task-retry",
        scan_kind="customization",
        started_at=STARTED,
        finished_at=FINISHED,
        summary={"candidate_count": 1},
    )
    second = writer.write(
        task_id="task-retry",
        scan_kind="customization",
        started_at=STARTED,
        finished_at=FINISHED + timedelta(seconds=1),
        summary={"candidate_count": 2},
    )

    assert first.path == second.path
    assert json.loads(second.path.read_text(encoding="utf-8"))["summary"][
        "candidate_count"
    ] == 2
    attempts = list(second.path.parent.glob(f"{second.path.stem}.attempt-*.json"))
    assert len(attempts) == 1
    assert json.loads(attempts[0].read_text(encoding="utf-8"))["summary"][
        "candidate_count"
    ] == 1
    assert list(second.path.parent.glob("*.tmp")) == []
    assert list(second.path.parent.glob(".*.tmp")) == []


def test_builder_rejects_ambiguous_or_reversed_timestamps() -> None:
    with pytest.raises(ValueError, match="包含时区"):
        build_scan_audit_document(
            task_id="task-time",
            scan_kind="customization",
            started_at=datetime(2026, 7, 14, 6, 30),
            finished_at=FINISHED,
        )
    with pytest.raises(ValueError, match="不能早于"):
        build_scan_audit_document(
            task_id="task-time",
            scan_kind="customization",
            started_at=FINISHED,
            finished_at=STARTED,
        )
