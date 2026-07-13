from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_default_shipment_bat_runs_scan_logistics_then_erp_mark():
    text = (ROOT / "启动自动标发候选扫描.bat").read_text(encoding="utf-8")

    scan_command = "-m shipment_automation.cli scan --dry-run"
    logistics_command = "-m shipment_automation.cli logistics --from-queue --limit 20 --update-queue"
    erp_mark_command = "-m shipment_automation.cli erp-mark --execute --limit 20"

    assert scan_command in text
    assert logistics_command in text
    assert erp_mark_command in text
    assert text.index(scan_command) < text.index(logistics_command)
    assert text.index(logistics_command) < text.index(erp_mark_command)
    assert "Step 3 will operate Lingxing ERP for real" in text
    assert "Shipment candidate scan failed. Later steps will not start." in text
    assert 'if "%SCAN_EXIT_CODE%"=="3"' in text
    assert "Existing queued orders will continue" in text
    assert "failed or was aborted" not in text
    assert "batch completed with technical errors" in text
    assert "set \"INTERVAL_SECONDS=10800\"" in text
    assert "timeout /t %INTERVAL_SECONDS% /nobreak" in text
    assert ":bootstrap" in text
    assert "goto setup_wait" in text
    assert ":main_menu" in text
    assert 'set "PYTHONIOENCODING=utf-8"' in text
    assert "\\u81ea\\u52a8\\u6807\\u53d1\\u4e3b\\u83dc\\u5355" in text
    assert "\\u542f\\u52a8\\u81ea\\u52a8\\u6807\\u53d1\\u5de1\\u68c0" in text
    assert "\\u7ba1\\u7406\\u963b\\u6b62\\u548c\\u5f85\\u5904\\u7406" in text
    assert "\\u8bf7\\u8f93\\u5165 1\\u30012 \\u6216 0\\uff1a" in text
    assert "自动标发主菜单" not in text
    assert "请输入 1、2 或 0：" not in text
    assert 'if "%MENU_CHOICE%"=="1" goto run_loop' in text
    assert 'if "%MENU_CHOICE%"=="2" goto queue_manage' in text
    assert 'if "%MENU_CHOICE%"=="0" goto end' in text
    assert "-m shipment_automation.cli queue manage" in text
    assert "goto main_menu" in text
    assert "goto run_loop" in text
    assert "pause" not in text.lower()
