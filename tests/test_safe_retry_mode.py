import asyncio
import sys
from pathlib import Path

from lingxing_automation import cli
from lingxing_automation.cli import build_parser, prepare_retry_order_args
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import BatchOrderItem, ContactInfo, CustomZipFile, FolderBuildResult, OrderCustomZipBundle
from lingxing_automation.pages.order_management import build_batch_candidates_from_rows
from lingxing_automation.services.custom_attachment_downloader import CUSTOM_ZIP_SKIPPED_NO_FOLDER
from lingxing_automation.storage.dedupe import append_processed_platform_order

ROOT = Path(__file__).resolve().parents[1]


def test_retry_order_args_stay_on_retry_flow():
    args = build_parser().parse_args(
        ["--batch", "--loop", "--retry-order", "112-1234567-1234567", "--no-dedupe-write"]
    )

    prepared = prepare_retry_order_args(args)

    assert prepared.retry_order == "112-1234567-1234567"
    assert prepared.order_no is None
    assert prepared.batch is False
    assert prepared.loop is False
    assert prepared.no_dedupe_write is True
    assert prepared.no_create_folder is True
    assert prepared.allow_sku_adjustment is False


def test_retry_order_can_explicitly_allow_sku_adjustment():
    args = build_parser().parse_args(
        ["--retry-order", "112-1234567-1234567", "--allow-sku-adjustment"]
    )

    prepared = prepare_retry_order_args(args)

    assert prepared.no_dedupe_write is True
    assert prepared.no_create_folder is True
    assert prepared.allow_sku_adjustment is True


def test_cli_retry_dispatches_to_batch_retry_flow(monkeypatch, capsys):
    calls = {"retry": 0, "run_once": 0}

    async def fake_retry(args):
        calls["retry"] += 1
        assert args.retry_order == "112-1234567-1234567"
        return {
            "status": "completed",
            "retry_order": args.retry_order,
            "candidate_count": 1,
            "updated_count": 1,
            "skipped_count": 0,
            "dedupe_write_enabled": False,
        }

    async def fake_run_once(_args):
        calls["run_once"] += 1
        raise AssertionError("安全重测不能再走旧 run_once 流程")

    monkeypatch.setattr(cli, "run_retry_order", fake_retry)
    monkeypatch.setattr(cli, "run_once", fake_run_once)
    monkeypatch.setattr(sys, "argv", ["lingxing_web_sync.py", "--retry-order", "112-1234567-1234567", "--no-dedupe-write"])

    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "安全重测结果" in output
    assert calls == {"retry": 1, "run_once": 0}


def test_safe_retry_candidate_overrides_tag_processed_and_old_payment(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_processed_platform_order(dedupe_path, "112-1234567-1234567", "103700000000000000")
    raw_row = {
        "platform_order_no": "112-1234567-1234567",
        "system_order_no": "103700000000000000",
        "asin_text": "B0D5134SJ3",
        "sku": "canopytents",
        "tag_text": "客户确认中",
        "paid_at_text": "2020-01-01 00:00:00",
        "row_text": "112-1234567-1234567 103700000000000000 B0D5134SJ3 canopytents 客户确认中",
    }

    normal = build_batch_candidates_from_rows(
        [raw_row],
        {"112-1234567-1234567"},
        payment_window_hours=1,
        debug={},
    )
    retry = build_batch_candidates_from_rows(
        [raw_row],
        {"112-1234567-1234567"},
        payment_window_hours=1,
        debug={},
        ignore_tags=True,
        ignore_processed=True,
        ignore_payment_window=True,
    )

    assert normal == []
    assert len(retry) == 1
    assert retry[0].platform_order_no == "112-1234567-1234567"


def test_processed_order_is_skipped_by_default(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_processed_platform_order(dedupe_path, "112-1234567-1234567", "103700000000000000")

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            object(),
            dedupe_path=dedupe_path,
        )
    )

    assert result["status"] == "already_processed"


def test_retry_mode_ignores_processed_dedupe(monkeypatch, tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_processed_platform_order(dedupe_path, "112-1234567-1234567", "103700000000000000")

    async def fake_close(_page):
        return None

    async def fake_fill(_page, _order_no, _search_kind):
        return {"search_validation_ok": True}

    async def fake_wait(_page, _order_no, _search_kind, _timeout):
        return []

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fake_fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", fake_wait)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            object(),
            dedupe_path=dedupe_path,
            ignore_dedupe=True,
        )
    )

    assert result["status"] == "search_no_results"


def test_safe_retry_allows_sku_page_write_only_with_explicit_switch(monkeypatch):
    captured: list[bool] = []

    async def fake_process_batch_order_item(*_args, **kwargs):
        captured.append(kwargs["allow_sku_adjustment_page_write"])
        return {"status": "updated_folder_created_sku_failed"}

    monkeypatch.setattr(contact_sync, "process_batch_order_item", fake_process_batch_order_item)
    item = BatchOrderItem(
        "103700000000000000",
        "112-1234567-1234567",
        "112-1234567-1234567 B0D5134SJ3 canopytents",
    )

    args = prepare_retry_order_args(
        build_parser().parse_args(["--retry-order", "112-1234567-1234567"])
    )
    asyncio.run(contact_sync.process_batch_candidate_with_policy(object(), item, object(), args, set(), ignore_dedupe=True))

    args = prepare_retry_order_args(
        build_parser().parse_args(["--retry-order", "112-1234567-1234567", "--allow-sku-adjustment"])
    )
    asyncio.run(contact_sync.process_batch_candidate_with_policy(object(), item, object(), args, set(), ignore_dedupe=True))

    assert captured == [False, True]


def test_no_dedupe_write_helpers_do_not_create_state_file(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"

    contact_recorded = contact_sync.record_contact_writeback_if_allowed(
        dedupe_path,
        "112-1234567-1234567",
        "103700000000000000",
        contact_status="written",
        write_enabled=False,
    )
    final_recorded = contact_sync.append_final_processed_if_allowed(
        dedupe_path,
        "112-1234567-1234567",
        "103700000000000000",
        write_enabled=False,
    )

    assert contact_recorded is False
    assert final_recorded is False
    assert not dedupe_path.exists()


def test_finalize_skips_zip_copy_when_folder_write_disabled(tmp_path):
    final_folder = tmp_path / "final"
    final_folder.mkdir()
    staging_dir = tmp_path / "custom_zip_staging" / "112-1234567-1234567"
    staging_dir.mkdir(parents=True)
    zip_path = staging_dir / "B0TEST0000_CustomizedInfo.zip"
    zip_path.write_bytes(b"zip bytes")

    result = contact_sync.finalize_custom_zip_files_for_folder(
        FolderBuildResult(status="folder_existing_platform_order", folder_path=str(final_folder)),
        {
            "zip_bundle": OrderCustomZipBundle(
                platform_order_no="112-1234567-1234567",
                zip_files=[
                    CustomZipFile(
                        row_index=1,
                        asin="B0TEST0000",
                        sku=None,
                        msku=None,
                        platform_order_no="112-1234567-1234567",
                        trigger_text="download",
                        zip_filename=zip_path.name,
                        zip_path=str(zip_path),
                    )
                ],
            ),
            "custom_zip_staging_dir": str(staging_dir),
        },
        allow_folder_write=False,
    )

    assert result["custom_zip_status"] == CUSTOM_ZIP_SKIPPED_NO_FOLDER
    assert "文件夹写入已关闭" in result["custom_zip_error"]
    assert not (final_folder / zip_path.name).exists()
    assert staging_dir.exists()


def test_safe_retry_success_message_does_not_claim_final_dedupe_write():
    contact = ContactInfo(
        phone=None,
        email="buyer@example.com",
        source_excerpt="buyer@example.com",
        source_count=1,
    )
    formal_message = contact_sync.build_writeback_success_message(contact)

    safe_message = contact_sync.adapt_completion_message_for_runtime(
        formal_message,
        folder_write_enabled=False,
        dedupe_write_enabled=False,
    )

    assert "已加入最终完成列表" not in safe_message
    assert "不写入最终完成列表" in safe_message


def test_existing_folder_notice_distinguishes_safe_retry_from_dedupe(capsys, tmp_path):
    contact_sync.notify_existing_folder_in_cmd(
        "112-1234567-1234567",
        "103700000000000000",
        FolderBuildResult(
            status="folder_existing_platform_order",
            folder_path=str(tmp_path / "112-1234567-1234567"),
        ),
        folder_write_enabled=False,
        dedupe_write_enabled=False,
    )

    output = capsys.readouterr().out
    assert "不是查重跳过" in output
    assert "不会把订单加入 data/processed_platform_orders.json" in output


def test_rule_missing_lines_include_original_customization_line():
    result = FolderBuildResult(
        status="feather_flags_rule_missing",
        missing_rule_title="Pole Type",
        missing_rule_value="Aluminum-Pole",
        customization_pairs={"1.Pole Type": "Aluminum-Pole"},
        error="缺少刀旗文件夹规则：Pole Type = Aluminum-Pole",
    )

    assert result.missing_rule_lines() == [
        "缺少规则：Pole Type = Aluminum-Pole",
    ]
    assert result.to_log_dict()["folder_missing_rule_line"] == "1.Pole Type = Aluminum-Pole"


def test_folder_rule_missing_status_uses_same_customization_line_format():
    result = FolderBuildResult(
        status="folder_rule_missing",
        missing_rule_title="Fabric Material Options",
        missing_rule_value="Mystery Fabric",
        customization_pairs={"2.Fabric Material Options": "Mystery Fabric"},
    )

    assert result.missing_rule_lines() == [
        "缺少规则：Fabric Material Options = Mystery Fabric",
    ]


def test_rule_missing_middle_status_uses_missing_line_details():
    result = FolderBuildResult(
        status="vinyl_banners_rule_missing_printed_sides",
        missing_rule_title="Printed Sides",
        missing_rule_value="missing",
        missing_rule_line="1.Printed Sides = missing",
        error="喷绘缺少 Printed Sides 定制选项",
    )

    assert result.missing_rule_lines() == [
        "缺少规则：Printed Sides = missing",
    ]
    assert result.to_log_dict()["folder_missing_rule_line"] == "1.Printed Sides = missing"


def test_folder_failure_reason_omits_rule_missing_detail():
    result = FolderBuildResult(
        status="vinyl_banners_rule_missing_printed_sides",
        missing_rule_title="Printed Sides",
        missing_rule_value="missing",
        missing_rule_line="1.Printed Sides = missing",
    )

    assert contact_sync.format_folder_failure_reason(result) == "vinyl_banners_rule_missing_printed_sides"


def test_rule_missing_lines_fallback_to_field_and_error_when_line_not_found():
    result = FolderBuildResult(
        status="folder_rule_missing",
        missing_rule_title="Printed Sides",
        missing_rule_value="Mystery Side",
        customization_pairs={"1.Other Option": "Mystery Side"},
        error="缺少文件夹规则：Printed Sides = Mystery Side",
    )

    assert result.missing_rule_lines() == [
        "缺少规则：Printed Sides = Mystery Side",
    ]
    assert contact_sync.folder_rule_missing_lines_from_log(result.to_log_dict()) == [
        "缺少规则：Printed Sides = Mystery Side",
    ]


def test_batch_skip_notice_prints_rule_missing_details(capsys):
    contact_sync.print_batch_item_skip_notice(
        {
            "platform_order_no": "702-9402546-2859420",
            "system_order_no": "103715344751344187",
            "status": "updated_folder_failed",
            "message": "文件夹生成失败：feather_flags_rule_missing",
            "folder_status": "feather_flags_rule_missing",
            "folder_missing_rule_title": "Pole Type",
            "folder_missing_rule_value": "Aluminum-Pole",
            "customization_pairs": {"1.Pole Type": "Aluminum-Pole"},
        }
    )

    output = capsys.readouterr().out
    assert "缺少规则：Pole Type = Aluminum-Pole" in output
    assert "定制行：" not in output


def test_batch_skip_notice_prints_rule_missing_middle_status_details(capsys):
    contact_sync.print_batch_item_skip_notice(
        {
            "platform_order_no": "114-8706887-0057811",
            "system_order_no": "103716678622178622",
            "status": "updated_folder_failed",
            "message": "文件夹生成失败：vinyl_banners_rule_missing_printed_sides",
            "folder_status": "vinyl_banners_rule_missing_printed_sides",
            "folder_missing_rule_title": "Printed Sides",
            "folder_missing_rule_value": "missing",
            "folder_missing_rule_line": "1.Printed Sides = missing",
            "custom_zip_status": "ok",
        }
    )

    output = capsys.readouterr().out
    assert "缺少规则：Printed Sides = missing" in output
    assert "定制行：" not in output


def test_safe_retry_bat_is_the_only_single_retry_bat_entrypoint():
    safe_retry = (ROOT / "安全重测单个订单.bat").read_text(encoding="utf-8")

    assert "--retry-order" in safe_retry
    assert "--no-dedupe-write" in safe_retry
    assert "--no-create-folder" in safe_retry
    assert "--allow-sku-adjustment" in safe_retry
    assert "\\u8bf7\\u8f93\\u5165\\u8981\\u5b89\\u5168\\u91cd\\u6d4b\\u7684\\u5e73\\u53f0\\u5355\\u53f7" in safe_retry
    assert "\\u662f\\u5426\\u5141\\u8bb8\\u672c\\u6b21\\u771f\\u5b9e\\u8c03\\u6574\\u5e10\\u7bf7 SKU" in safe_retry
    assert not (ROOT / "启动领星网页同步.bat").exists()
