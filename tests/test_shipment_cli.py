import sys

from lingxing_automation import cli as lingxing_cli
from shipment_automation import cli as shipment_cli
from shipment_automation.alibaba_logistics import tracking_number_mismatch_reason
from shipment_automation.models import LOGISTICS_BLOCKED, LogisticsDetail, ShipmentCandidate
from shipment_automation.queue_store import ShipmentWorkflowStore


def _scan_payload():
    return {
        "status": "completed",
        "message": "扫描完成。",
        "shipment_tag_name": "自动标发",
        "queue_path": "data/shipment_queue.sqlite3",
        "scan_log_file": "logs/shipment_scan_test.json",
        "scanned_row_count": 1,
        "table_total_count": 1,
        "scan_complete": True,
        "tagged_row_count": 1,
        "valid_logistics_row_count": 1,
        "enqueued_count": 1,
        "refreshed_count": 1,
        "immediate_logistics_count": 1,
        "immediate_erp_count": 0,
        "conflict_count": 0,
        "duplicate_skipped_count": 1,
        "manual_review_count": 1,
        "manual_completed_count": 1,
        "enqueued_candidates": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "logistics_no": "ALS01781406025",
                "sku_text": "10x10-Canopy 共1",
                "tag_text": "自动标发",
            }
        ],
        "manual_completed": [
            {
                "system_order_no": "103717510103539424",
                "platform_order_no": "114-9238856-6341844",
                "logistics_no": "ALS01792557166",
            }
        ],
        "duplicate_skipped": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "logistics_no": "ALS01781406025",
                "existing_system_order_no": "103710434633847501",
                "existing_platform_order_no": "112-1165824-9982644",
                "conflict": False,
            }
        ],
        "manual_reviews": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "selected_logistics_no": "ALS01781406025",
                "logistics_numbers": ["ALS01781406025"],
                "message": "已排除作废/取消/无效上下文中的物流单号：ALS01768041004",
            }
        ],
    }


def _logistics_payload():
    return {
        "status": "completed",
        "queue_path": "data/shipment_queue.sqlite3",
        "scanned_page_count": 1,
        "parsed_count": 1,
        "ready_count": 1,
        "waiting_count": 1,
        "blocked_count": 1,
        "retryable_count": 1,
        "query_results": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "logistics_no": "ALS01781406025",
                "status_text": "运输中",
                "logistics_state": "READY",
            },
            {
                "system_order_no": "103710639045926988",
                "platform_order_no": "111-8854282-5961022",
                "logistics_no": "ALS01789020252",
                "status_text": "运输中",
                "logistics_state": "WAITING",
                "last_error": "国际物流服务商不是真实海外尾程承运商：YHA，请人工确认。",
            },
            {
                "system_order_no": "103718008850021484",
                "platform_order_no": "701-4375420-3836231",
                "logistics_no": "ALS01799145331",
                "status_text": "",
                "logistics_state": "RETRYABLE",
                "last_error": "浏览器关闭导致本轮查询失败，下轮继续重试。",
            }
        ],
        "ready_to_mark_items": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "logistics_no": "ALS01781406025",
                "carrier": "UPS",
                "international_tracking_no": "1Z9253126709651051",
                "actual_total": "CNY 123.45",
                "chargeable_weight_kg": "4.500",
            }
        ],
        "skipped_query_records": [
            {
                "system_order_no": "103710639045926988",
                "platform_order_no": "111-8854282-5961022",
                "logistics_no": "ALS01789020252",
                "logistics_state": "BLOCKED",
                "erp_state": "WAITING",
                "stage_state": "物流/BLOCKED",
                "last_error": "需要人工复核",
            }
        ],
        "warnings": ["浏览器在阿里物流查询中被关闭，已重启一次重试。"],
    }


def _erp_mark_payload():
    return {
        "status": "completed",
        "message": "ERP 标发流程完成。",
        "queue_path": "data/shipment_queue.sqlite3",
        "dry_run": False,
        "execute": True,
        "total_count": 1,
        "done_count": 1,
        "skipped_count": 0,
        "blocked_count": 0,
        "retryable_count": 0,
        "results": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "wc39877",
                "logistics_no": "ALS01781406025",
                "erp_step": "ERP_MARKED",
                "erp_state": "DONE",
                "erp_checkpoint": "OUTBOUNDED",
                "carrier": "UPS",
                "international_tracking_no": "1Z9253126709651051",
                "sales_channel": "INDEPENDENT_SITE",
                "customer_email_required": False,
            }
        ],
        "store_fulfillment_reminders": [
            {
                "independent_order_no": "wc39877",
                "system_order_no": "103710434633847501",
                "logistics_no": "ALS01781406025",
                "carrier": "UPS",
                "international_tracking_no": "1Z9253126709651051",
                "message": "ERP 已标发出库，请在店小秘标发该独立站订单。",
            }
        ],
    }


def test_shipment_cli_scan_dispatches(monkeypatch, capsys):
    calls = {"shipment": 0}

    async def fake_run_shipment_scan(args):
        calls["shipment"] += 1
        assert args.command == "scan"
        assert args.dry_run is True
        return _scan_payload()

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "自动标发候选扫描结果" in output
    assert "物流单号" in output
    assert "ALS：" not in output
    assert "扫描日志：logs/shipment_scan_test.json" in output
    assert "ERP 待审核总数：1" in output
    assert "成功读取行数：1" in output
    assert "本轮新增队列：1" in output
    assert "已有队列刷新：1" in output
    assert "本轮重新命中候选：已安排立即查询/执行 1 条。" in output
    assert "重复跳过数量" not in output
    assert "重复跳过：" not in output
    assert "需要复核：" in output
    assert "ALS01781406025" in output
    assert "10x10-Canopy" not in output
    assert "自动标发 | PENDING" not in output
    assert "人工已完成：1 条，已结案并停止后续处理。详情见扫描日志。" in output
    assert "103717510103539424" not in output
    assert calls == {"shipment": 1}


def test_shipment_cli_incomplete_scan_returns_warning_code_and_reason(monkeypatch, capsys):
    payload = _scan_payload()
    payload.update(
        {
            "status": "incomplete",
            "message": "待审核扫描不完整。",
            "table_total_count": 132,
            "scanned_row_count": 121,
            "scan_complete": False,
            "incomplete_field_count": 2,
        }
    )

    async def fake_run_shipment_scan(_args):
        return payload

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run"]) == shipment_cli.SCAN_INCOMPLETE_EXIT_CODE
    output = capsys.readouterr().out
    assert "ERP 共 132 条，成功读取 121 条，缺少 11 条" in output
    assert "已禁止人工完成判定" in output
    assert "另有 2 条记录的关键列未完整读取" in output


def test_shipment_cli_scan_verbose_shows_duplicate_details(monkeypatch, capsys):
    async def fake_run_shipment_scan(args):
        return _scan_payload()

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run", "--verbose"]) == 0
    output = capsys.readouterr().out
    assert "重复跳过数量：1" in output
    assert "重复跳过：" in output
    assert "队列已存在系统单号" in output
    assert "103717510103539424 | 114-9238856-6341844 | ALS01792557166" in output


def test_shipment_cli_scan_shows_identity_conflict(monkeypatch, capsys):
    payload = _scan_payload()
    payload["duplicate_skipped"][0]["conflict"] = True
    payload["duplicate_skipped"][0]["system_order_no"] = "103710639045926988"

    async def fake_run_shipment_scan(args):
        return payload

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "物流单号归属冲突" in output
    assert "103710639045926988" in output


def test_shipment_cli_scan_json_keeps_duplicate_fields(monkeypatch, capsys):
    async def fake_run_shipment_scan(args):
        return _scan_payload()

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run", "--json"]) == 0
    output = capsys.readouterr().out
    assert '"duplicate_skipped_count": 1' in output
    assert '"duplicate_skipped"' in output


def test_lingxing_cli_shipment_scan_compat_dispatches(monkeypatch, capsys):
    calls = {"shipment": 0}

    async def fake_run_shipment_scan(args):
        calls["shipment"] += 1
        assert args.command == "scan"
        assert args.dry_run is True
        return _scan_payload()

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)
    monkeypatch.setattr(sys, "argv", ["lingxing_web_sync.py", "shipment-scan", "--dry-run"])

    assert lingxing_cli.main() == 0
    output = capsys.readouterr().out
    assert "自动标发候选扫描结果" in output
    assert "物流单号" in output
    assert "ALS：" not in output
    assert "重复跳过数量" not in output
    assert "重复跳过：" not in output
    assert "ALS01781406025" in output
    assert calls == {"shipment": 1}


def test_shipment_cli_logistics_dispatches(monkeypatch, capsys):
    calls = {"logistics": 0}

    async def fake_run_logistics_worker(args):
        calls["logistics"] += 1
        assert args.command == "logistics"
        assert args.from_queue is True
        assert args.update_queue is False
        assert args.env_path == ".env"
        assert args.no_auto_login is False
        assert args.login_timeout_sec == 300
        return _logistics_payload()

    monkeypatch.setattr(shipment_cli, "run_logistics_worker", fake_run_logistics_worker)

    assert shipment_cli.main(["logistics", "--from-queue", "--limit", "20", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "本次物流查询结果" in output
    assert "待标发列表" in output
    assert "物流单号" in output
    assert "ALS：" not in output
    assert "系统单号 | 平台单号 | 物流单号 | 国际物流服务商 | 国际物流单号 | 费用金额 | 计费重KG" in output
    assert "系统单号 | 平台单号 | 物流单号 | 物流订单号" not in output
    assert "ALS01781406025" in output
    assert "国际物流服务商不是真实海外尾程承运商：YHA" in output
    assert "需关注的队列记录" in output
    assert "物流/BLOCKED" in output
    assert output.index("待标发列表") < output.index("需关注的队列记录")
    assert "需要人工复核" in output
    assert "浏览器在阿里物流查询中被关闭，已重启一次重试。" in output
    assert "浏览器关闭导致本轮查询失败，下轮继续重试。" in output
    assert "Browser logs:" not in output
    assert "ALS01789020252 | ALS01789020252" not in output
    assert calls == {"logistics": 1}


def test_logistics_cli_reviews_new_tracking_mismatch_after_query(monkeypatch, tmp_path, capsys):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = ShipmentCandidate(
        system_order_no="103714933869767207",
        platform_order_no="114-1416477-4543451",
        logistics_no="ALS01798551368",
        shipment_tag_name="自动标发",
    )
    store.upsert_candidate(candidate)

    async def fake_run_logistics_worker(_args):
        detail = LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        )
        store.complete_logistics_attempt(
            candidate.logistics_no,
            detail,
            state=LOGISTICS_BLOCKED,
            last_error=tracking_number_mismatch_reason(
                detail.carrier,
                detail.international_tracking_no,
            ),
        )
        return {
            "status": "completed",
            "queue_path": str(path),
            "query_results": [],
            "ready_to_mark_items": [],
            "skipped_query_records": [],
            "warnings": [],
        }

    monkeypatch.setattr(shipment_cli, "run_logistics_worker", fake_run_logistics_worker)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")

    assert shipment_cli.main(
        [
            "logistics",
            "--from-queue",
            "--update-queue",
            "--queue-path",
            str(path),
        ]
    ) == 0

    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["tracking_mismatch_action"] == "AUTO_RECHECK"
    assert row["logistics_next_attempt_at"]
    output = capsys.readouterr().out
    assert "发现承运商与国际物流单号不匹配" in output
    assert "本轮尾程单号已审核：1" in output


def test_shipment_cli_erp_mark_dry_run_dispatches(monkeypatch, capsys):
    calls = {"erp": 0}

    async def fake_run_erp_mark_worker(args):
        calls["erp"] += 1
        assert args.command == "erp-mark"
        assert args.dry_run is True
        assert args.limit == 20
        return {**_erp_mark_payload(), "dry_run": True, "execute": False, "done_count": 0}

    monkeypatch.setattr(shipment_cli, "run_erp_mark_worker", fake_run_erp_mark_worker)

    assert shipment_cli.main(["erp-mark", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "ERP 自动标发结果" in output
    assert "系统单号 | 平台单号 | 物流单号 | ERP步骤 | ERP状态 | 检查点 | 备注" in output
    assert "店小秘待标发提示" in output
    assert "独立站单号 | 系统单号 | 物流单号 | 国际物流服务商 | 国际物流单号 | 备注" in output
    assert "wc39877" in output
    assert "UPS" in output
    assert "1Z9253126709651051" in output
    assert "ALS：" not in output
    assert "ALS01781406025" in output
    assert calls == {"erp": 1}


def test_shipment_cli_erp_mark_execute_dispatches(monkeypatch):
    calls = {"erp": 0}

    async def fake_run_erp_mark_worker(args):
        calls["erp"] += 1
        assert args.command == "erp-mark"
        assert args.dry_run is False
        assert args.limit == 20
        return _erp_mark_payload()

    monkeypatch.setattr(shipment_cli, "run_erp_mark_worker", fake_run_erp_mark_worker)

    assert shipment_cli.main(["erp-mark", "--execute", "--limit", "20"]) == 0
    assert calls == {"erp": 1}


def test_shipment_cli_erp_mark_skips_exit_success_and_print_summary(monkeypatch, capsys):
    async def fake_run_erp_mark_worker(_args):
        return {
            **_erp_mark_payload(),
            "status": "completed_with_skips",
            "total_count": 2,
            "done_count": 1,
            "skipped_count": 1,
        }

    monkeypatch.setattr(shipment_cli, "run_erp_mark_worker", fake_run_erp_mark_worker)

    assert shipment_cli.main(["erp-mark", "--execute"]) == 0
    output = capsys.readouterr().out
    assert "候选数量：2" in output
    assert "完成数量：1" in output
    assert "跳过数量：1" in output


def test_shipment_cli_erp_mark_technical_errors_exit_failure(monkeypatch):
    async def fake_run_erp_mark_worker(_args):
        return {**_erp_mark_payload(), "status": "completed_with_errors", "retryable_count": 1}

    monkeypatch.setattr(shipment_cli, "run_erp_mark_worker", fake_run_erp_mark_worker)

    assert shipment_cli.main(["erp-mark", "--execute"]) == 1


def test_queue_cli_lists_separate_stage_states(tmp_path, capsys):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    store.upsert_candidate(
        ShipmentCandidate(
            system_order_no="103710434633847501",
            platform_order_no="112-1165824-9982644",
            logistics_no="ALS01781406025",
            shipment_tag_name="自动标发",
        )
    )

    assert shipment_cli.main(["queue", "list", "--queue-path", str(path)]) == 0
    output = capsys.readouterr().out
    assert "身份状态 | 物流状态 | ERP状态 | ERP检查点" in output
    assert "ALS01781406025" in output


def test_queue_cli_mutation_requires_execute(tmp_path, capsys):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(path)
    candidate = ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        logistics_no="ALS01781406025",
        shipment_tag_name="自动标发",
    )
    store.upsert_candidate(candidate)
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(logistics_no=candidate.logistics_no, page_error="需要人工处理"),
        state=LOGISTICS_BLOCKED,
        last_error="需要人工处理",
    )

    assert shipment_cli.main(
        ["queue", "retry", "--logistics-no", candidate.logistics_no, "--stage", "logistics", "--queue-path", str(path)]
    ) == 2
    assert "--execute" in capsys.readouterr().out
    assert store.get_by_logistics_no(candidate.logistics_no)["logistics_state"] == LOGISTICS_BLOCKED

    assert shipment_cli.main(
        [
            "queue", "retry", "--logistics-no", candidate.logistics_no, "--stage", "logistics",
            "--queue-path", str(path), "--execute",
        ]
    ) == 0
    assert store.get_by_logistics_no(candidate.logistics_no)["logistics_state"] == "RETRYABLE"


def test_queue_cli_manage_dispatches_interactive_manager(monkeypatch, tmp_path):
    calls = []

    def fake_manager(store):
        calls.append(store.path)
        return 0

    monkeypatch.setattr(shipment_cli, "run_interactive_queue_manager", fake_manager)

    assert shipment_cli.main(
        ["queue", "manage", "--queue-path", str(tmp_path / "shipment_queue.sqlite3")]
    ) == 0
    assert calls == [tmp_path / "shipment_queue.sqlite3"]
