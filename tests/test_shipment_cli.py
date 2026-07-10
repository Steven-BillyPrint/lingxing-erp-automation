import sys

from lingxing_automation import cli as lingxing_cli
from shipment_automation import cli as shipment_cli


def _scan_payload():
    return {
        "status": "completed",
        "message": "扫描完成。",
        "shipment_tag_name": "自动标发",
        "queue_path": "data/shipment_queue.sqlite3",
        "scan_log_file": "logs/shipment_scan_test.json",
        "scanned_row_count": 1,
        "tagged_row_count": 1,
        "valid_als_row_count": 1,
        "enqueued_count": 1,
        "duplicate_skipped_count": 1,
        "manual_review_count": 1,
        "enqueued_candidates": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "als_no": "ALS01781406025",
                "sku_text": "10x10-Canopy 共1",
            }
        ],
        "duplicate_skipped": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "als_no": "ALS01781406025",
                "existing_system_order_no": "103710434633847501",
                "existing_platform_order_no": "112-1165824-9982644",
            }
        ],
        "manual_reviews": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "selected_als_no": "ALS01781406025",
                "als_numbers": ["ALS01781406025"],
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
        "ready_to_mark_count": 1,
        "not_ready_count": 0,
        "manual_review_count": 0,
        "error_count": 0,
        "query_results": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "als_no": "ALS01781406025",
                "status_text": "运输中",
                "queue_status": "READY_TO_MARK",
            },
            {
                "system_order_no": "103710639045926988",
                "platform_order_no": "111-8854282-5961022",
                "als_no": "ALS01789020252",
                "status_text": "运输中",
                "queue_status": "NOT_READY",
                "last_error": "国际物流服务商不是真实海外尾程承运商：YHA，请人工确认。",
            },
            {
                "system_order_no": "103718008850021484",
                "platform_order_no": "701-4375420-3836231",
                "als_no": "ALS01799145331",
                "status_text": "",
                "queue_status": "ERROR",
                "last_error": "浏览器关闭导致本轮查询失败，下轮继续重试。",
            }
        ],
        "ready_to_mark_items": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "als_no": "ALS01781406025",
                "logistics_order_no": "ALS01781406025",
                "carrier": "UPS",
                "international_tracking_no": "1Z999",
                "actual_total": "CNY 123.45",
                "chargeable_weight_kg": "4.500",
            }
        ],
        "skipped_query_records": [
            {
                "system_order_no": "103710639045926988",
                "platform_order_no": "111-8854282-5961022",
                "als_no": "ALS01789020252",
                "queue_status": "MANUAL_REVIEW",
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
        "marked_count": 1,
        "manual_review_count": 0,
        "error_count": 0,
        "results": [
            {
                "system_order_no": "103710434633847501",
                "platform_order_no": "112-1165824-9982644",
                "als_no": "ALS01781406025",
                "erp_step": "ERP_MARKED",
                "queue_status": "ERP_MARKED",
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
    assert "重复跳过数量" not in output
    assert "重复跳过：" not in output
    assert "需要复核：" in output
    assert "ALS01781406025" in output
    assert calls == {"shipment": 1}


def test_shipment_cli_scan_verbose_shows_duplicate_details(monkeypatch, capsys):
    async def fake_run_shipment_scan(args):
        return _scan_payload()

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run", "--verbose"]) == 0
    output = capsys.readouterr().out
    assert "重复跳过数量：1" in output
    assert "重复跳过：" in output
    assert "队列已存在系统单号" in output


def test_shipment_cli_scan_shows_attention_duplicate_status(monkeypatch, capsys):
    payload = _scan_payload()
    payload["duplicate_skipped"][0]["existing_queue_status"] = "ERROR"
    payload["duplicate_skipped"][0]["existing_last_error"] = "上一轮 ERP 标发失败"

    async def fake_run_shipment_scan(args):
        return payload

    monkeypatch.setattr(shipment_cli, "run_shipment_scan", fake_run_shipment_scan)

    assert shipment_cli.main(["scan", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "队列已有待处理/异常记录" in output
    assert "上一轮 ERP 标发失败" in output


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
    assert "本轮未查询的队列记录" in output
    assert output.index("待标发列表") < output.index("本轮未查询的队列记录")
    assert "需要人工复核" in output
    assert "浏览器在阿里物流查询中被关闭，已重启一次重试。" in output
    assert "浏览器关闭导致本轮查询失败，下轮继续重试。" in output
    assert "Browser logs:" not in output
    assert "ALS01789020252 | ALS01789020252" not in output
    assert calls == {"logistics": 1}


def test_shipment_cli_erp_mark_dry_run_dispatches(monkeypatch, capsys):
    calls = {"erp": 0}

    async def fake_run_erp_mark_worker(args):
        calls["erp"] += 1
        assert args.command == "erp-mark"
        assert args.dry_run is True
        assert args.limit == 20
        return {**_erp_mark_payload(), "dry_run": True, "execute": False, "marked_count": 0}

    monkeypatch.setattr(shipment_cli, "run_erp_mark_worker", fake_run_erp_mark_worker)

    assert shipment_cli.main(["erp-mark", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "ERP 自动标发结果" in output
    assert "系统单号 | 平台单号 | 物流单号 | ERP步骤 | 队列状态 | 备注" in output
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
