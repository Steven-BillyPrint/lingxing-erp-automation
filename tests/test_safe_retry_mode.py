import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from lingxing_automation import cli
from lingxing_automation.cli import build_parser, prepare_retry_order_args
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import (
    BatchOrderItem,
    ContactInfo,
    CustomZipFile,
    FolderBuildResult,
    OrderCustomZipBundle,
    OrderFolderLine,
)
from lingxing_automation.pages.order_management import build_batch_candidates_from_rows
from lingxing_automation.products.catalog import PRODUCT_TYPE_TENT
from lingxing_automation.services.custom_attachment_downloader import CUSTOM_ZIP_SKIPPED_NO_FOLDER
from lingxing_automation.services.tent_package_split_planner import TentPackageSplitPlan
from lingxing_automation.services.tent_sku_planner import DestinationRegion, TentSkuAdjustmentPlan, TentSkuPlanAction
from lingxing_automation.storage.dedupe import (
    append_package_split_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    is_instruction_remark_done,
)

ROOT = Path(__file__).resolve().parents[1]


def test_retry_order_args_stay_on_retry_flow():
    """验证安全重测模式中的重测订单参数 stay on 重测 flow场景。"""
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
    assert prepared.allow_package_split is False


def test_retry_order_can_explicitly_allow_sku_adjustment():
    """验证安全重测模式中的重测订单可以显式允许SKU调整场景。"""
    args = build_parser().parse_args(
        ["--retry-order", "112-1234567-1234567", "--allow-sku-adjustment"]
    )

    prepared = prepare_retry_order_args(args)

    assert prepared.no_dedupe_write is True
    assert prepared.no_create_folder is True
    assert prepared.allow_sku_adjustment is True


def test_retry_order_can_explicitly_allow_package_split():
    """验证安全重测模式可以显式允许帐篷拆分包裹。"""
    args = build_parser().parse_args(
        ["--retry-order", "112-1234567-1234567", "--allow-package-split"]
    )

    prepared = prepare_retry_order_args(args)

    assert prepared.no_dedupe_write is True
    assert prepared.no_create_folder is True
    assert prepared.allow_package_split is True


def test_empty_retry_rows_after_confirmed_search_are_lingxing_server_error():
    outcome = contact_sync.retry_no_candidate_outcome(
        {
            "system_order_nos_after_search": ["103700000000000000"],
            "wait_for_visible_rows": {"ok": False, "attempts": []},
        }
    )

    assert outcome["status"] == "lingxing_server_error"
    assert outcome["error_type"] == "lingxing_server_error"
    assert outcome["retryable"] is True
    assert outcome["message"].startswith("领星服务器异常")
    assert "未执行任何订单修改" in outcome["message"]


def test_empty_retry_without_confirmed_order_keeps_no_candidate_result():
    outcome = contact_sync.retry_no_candidate_outcome(
        {
            "system_order_nos_after_search": [],
            "wait_for_visible_rows": {"ok": False, "attempts": []},
        }
    )

    assert outcome == {
        "status": "retry_no_candidate",
        "message": "已按平台单号搜索，但没有从批量表格行构造出可重测候选。",
    }


def test_retry_candidate_reuses_wait_result_without_batch_preparation(monkeypatch):
    platform_order_no = "112-1234567-1234567"
    system_order_no = "103700000000000000"
    calls: list[str] = []

    async def fake_fill(_page, order_no, search_kind):
        calls.append("search")
        assert (order_no, search_kind) == (platform_order_no, "platform")
        return {"search_validation_ok": True}

    async def fake_wait_orders(_page, order_no, search_kind, timeout):
        calls.append("wait_orders")
        assert (order_no, search_kind, timeout) == (
            platform_order_no,
            "platform",
            20,
        )
        return [system_order_no]

    async def fake_wait_rows(_page, _debug):
        calls.append("wait_rows")
        return {
            "ok": True,
            "headers": ["系统单号", "平台单号"],
            "column_indexes": {"system": 0, "platform": 1},
            "rows": [
                {
                    "platform_order_no": platform_order_no,
                    "system_order_no": system_order_no,
                    "row_text": f"{platform_order_no} {system_order_no}",
                }
            ],
        }

    monkeypatch.setattr(contact_sync, "fill_order_search", fake_fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", fake_wait_orders)
    monkeypatch.setattr(
        contact_sync,
        "wait_for_visible_batch_order_rows",
        fake_wait_rows,
    )
    debug: dict[str, object] = {}

    selection = asyncio.run(
        contact_sync.collect_retry_order_candidates(
            object(),
            SimpleNamespace(
                retry_order=platform_order_no,
                batch_payment_hours=96.0,
                search_timeout_sec=20,
            ),
            set(),
            debug,
        )
    )

    assert calls == ["search", "wait_orders", "wait_rows"]
    assert len(selection.candidates) == 1
    assert selection.candidates[0].system_order_no == system_order_no
    assert debug["retry_exact_search_skipped_batch_preparation"] is True
    assert isinstance(debug["retry_scan_duration_ms"], int)


def test_cli_retry_dispatches_to_batch_retry_flow(monkeypatch, capsys):
    """验证安全重测模式中的命令行重测派发到 批量 重测 flow场景。"""
    calls = {"retry": 0, "run_once": 0}

    async def fake_retry(args):
        """模拟重测行为，隔离测试中的外部依赖。"""
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
        """模拟run 一次行为，隔离测试中的外部依赖。"""
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
    """验证安全重测模式中的安全重测 候选覆盖标签已处理并旧付款场景。"""
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


def test_safe_retry_candidate_forces_target_order_without_supported_asin():
    """验证安全重测指定平台单后不再依赖列表 ASIN/SKU 初筛。"""
    raw_row = {
        "platform_order_no": "111-8112209-3174649",
        "system_order_no": "103719401767966430",
        "asin_text": "无商品编码 无商品编码 更多",
        "sku": "10x10-Canopy-Topper 共2 10X10-FRAME-40MM-SQUARE 共2 更多",
        "tag_text": "",
        "paid_at_text": "2026-07-06 19:59:20",
        "row_text": "111-8112209-3174649 无商品编码 10x10-Canopy-Topper 共2",
    }

    normal = build_batch_candidates_from_rows([raw_row], set(), debug={})
    retry = build_batch_candidates_from_rows(
        [raw_row],
        set(),
        debug={},
        ignore_tags=True,
        ignore_processed=True,
        ignore_payment_window=True,
        force_retry_order_no="111-8112209-3174649",
    )

    assert normal == []
    assert len(retry) == 1
    assert retry[0].platform_order_no == "111-8112209-3174649"
    assert retry[0].product_type == PRODUCT_TYPE_TENT


def test_safe_retry_candidate_forces_target_split_order():
    """验证精确安全重测允许已带拆分订单标签的平台单重新进入候选。"""

    raw_row = {
        "platform_order_no": "113-5993563-8330664",
        "system_order_no": "103722091257125376",
        "asin_text": "B0DZ2W2QWK 共1 无商品编码 更多",
        "sku": "Instruction 共1 10X10-FRAME-40MM-SQUARE 共1 更多",
        "tag_text": "拆分订单 合并订单",
        "status_text": "待审核发货 待人工审核",
        "paid_at_text": "2026-07-14 04:39:45",
        "row_text": "113-5993563-8330664 拆分订单 合并订单 B0DZ2W2QWK",
    }

    standard_batch_debug: dict = {}
    retry_debug: dict = {}
    standard_batch = build_batch_candidates_from_rows(
        [raw_row],
        set(),
        debug=standard_batch_debug,
        ignore_tags=True,
    )
    retry = build_batch_candidates_from_rows(
        [raw_row],
        set(),
        debug=retry_debug,
        ignore_tags=True,
        ignore_processed=True,
        ignore_payment_window=True,
        force_retry_order_no="113-5993563-8330664",
    )

    assert standard_batch == []
    assert standard_batch_debug["skip_counts"]["split_order"] == 1
    assert len(retry) == 1
    assert retry[0].platform_order_no == "113-5993563-8330664"
    assert retry_debug.get("skip_counts", {}).get("split_order", 0) == 0


def test_processed_order_is_skipped_by_default(tmp_path):
    """验证安全重测模式中的已处理订单为跳过 by 默认场景。"""
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
    """验证安全重测模式中的重测模式忽略已处理去重场景。"""
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_processed_platform_order(dedupe_path, "112-1234567-1234567", "103700000000000000")

    async def fake_close(_page):
        """模拟关闭行为，隔离测试中的外部依赖。"""
        return None

    async def fake_fill(_page, _order_no, _search_kind):
        """模拟填写行为，隔离测试中的外部依赖。"""
        return {"search_validation_ok": True}

    async def fake_wait(_page, _order_no, _search_kind, _timeout):
        """模拟wait行为，隔离测试中的外部依赖。"""
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


def test_safe_retry_forced_tent_candidate_bypasses_list_asin_and_payment_filters(monkeypatch, tmp_path):
    """验证安全重测强制候选不会在单项处理里再次被列表 ASIN/付款窗口拦截。"""
    calls: list[str] = []

    async def fake_close(_page):
        calls.append("close")

    async def fake_fill(_page, _order_no, _search_kind):
        return {"search_validation_ok": True}

    async def fake_wait_orders(_page, _order_no, _search_kind, _timeout):
        return ["103719401767966430"]

    async def fake_click_system(_page, system_order_no):
        calls.append(f"click:{system_order_no}")

    async def fake_wait_detail(_page, system_order_no):
        calls.append(f"detail:{system_order_no}")

    async def fake_assert_detail(_page, _system_order_no, _platform_order_no, _stage):
        return None

    captured_staging_roots: list[Path] = []

    async def fake_collect_context(*_args, **kwargs):
        captured_staging_roots.append(Path(kwargs["staging_root"]))
        return {
            "recipient_name": "Xander Tams",
            "amazon_quantity_result": None,
            "zip_bundle": None,
            "order_lines": [],
            "order_line_warnings": [],
            "order_line_error": "safe_retry_test_stop",
        }

    async def fake_shipping(_page):
        return "United States of America (USA), MI, PETOSKEY 邮编 12010"

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fake_fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", fake_wait_orders)
    monkeypatch.setattr(contact_sync, "click_system_order", fake_click_system)
    monkeypatch.setattr(contact_sync, "wait_for_detail", fake_wait_detail)
    monkeypatch.setattr(contact_sync, "assert_current_detail_order", fake_assert_detail)
    monkeypatch.setattr(contact_sync, "collect_order_folder_json_context", fake_collect_context)
    monkeypatch.setattr(contact_sync, "read_detail_shipping_address_text", fake_shipping)

    item = BatchOrderItem(
        "103719401767966430",
        "111-8112209-3174649",
        "111-8112209-3174649 无商品编码 10x10-Canopy-Topper 共2",
        paid_at_text="2020-01-01 00:00:00",
        product_type=PRODUCT_TYPE_TENT,
    )

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            item,
            object(),
            dedupe_path=tmp_path / "processed_platform_orders.json",
            payment_window_hours=1,
            log_dir=tmp_path / "runtime-logs",
            ignore_dedupe=True,
            ignore_payment_window=True,
            create_folder=False,
            download_custom_zip=False,
        )
    )

    assert result["status"] == "updated_folder_failed"
    assert result["product_type"] == PRODUCT_TYPE_TENT
    assert "click:103719401767966430" in calls
    assert captured_staging_roots == [
        tmp_path / "runtime-logs" / "custom_zip_staging"
    ]


def test_safe_retry_package_split_continues_after_sku_plan_only(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []

    async def fake_close(_page):
        calls.append(("close", None))

    async def fake_fill(_page, order_no, kind):
        calls.append(("fill", order_no))
        return {"search_validation_ok": True, "kind": kind}

    async def fake_wait_orders(_page, order_no, _kind, _timeout):
        calls.append(("wait_orders", order_no))
        return ["103719401767966430"]

    async def fake_click_system(_page, system_order_no):
        calls.append(("click", system_order_no))

    async def fake_wait_detail(_page, system_order_no):
        calls.append(("detail", system_order_no))

    async def fake_assert_detail(_page, _system_order_no, _platform_order_no, _stage):
        return None

    async def fake_collect_context(*_args, **_kwargs):
        calls.append(("collect_context", None))
        return {
            "recipient_name": "Xander Tams",
            "amazon_quantity_result": None,
            "zip_bundle": OrderCustomZipBundle(platform_order_no="111-8112209-3174649", status="ok"),
            "order_lines": [
                OrderFolderLine(
                    asin="B0D5134SJ3",
                    sku="canopytents",
                    parent_asin=None,
                    product_type=PRODUCT_TYPE_TENT,
                    quantity=1,
                    customization_text="tent",
                )
            ],
            "order_line_warnings": [],
            "order_line_error": None,
        }

    async def fake_shipping(_page):
        return "United States of America (USA), MI, PETOSKEY 12010"

    def fake_build_folder(**_kwargs):
        calls.append(("folder", None))
        return FolderBuildResult(
            status="folder_preview",
            folder_name="111-8112209-3174649+1 tent",
            folder_components=["111-8112209-3174649", "1 tent", "Xander Tams"],
            folder_components_full=["111-8112209-3174649", "1 tent", "Xander Tams"],
        )

    async def fake_sku_stage(*_args, **kwargs):
        calls.append(("sku_stage", kwargs["allow_page_write"]))
        return {
            "sku_adjustment_required": True,
            "sku_adjustment_complete": False,
            "sku_adjustment_status": "write_disabled",
            "sku_adjustment_error": "页面写入已关闭，本次只生成 SKU 调整计划。",
            "sku_adjustment_plan_generated": True,
            "sku_adjustment_plan_only": True,
        }

    async def fake_package_stage(*_args, **kwargs):
        calls.append(("package_stage", kwargs["allow_page_write"]))
        return {
            "package_split_required": True,
            "package_split_complete": True,
            "package_split_status": "package_split_complete",
            "package_split_system_order_nos": ["103719401767966431"],
            "instruction_remark_required": False,
        }

    async def fake_instruction_stage(*_args, **_kwargs):
        calls.append(("instruction_stage", None))
        return {
            "instruction_remark_required": False,
            "instruction_remark_complete": True,
            "instruction_remark_status": "not_required",
        }

    async def fake_warehouse_stage(*_args, **kwargs):
        calls.append(("warehouse_stage", kwargs["allow_page_write"]))
        return {
            "warehouse_logistics_required": True,
            "warehouse_logistics_complete": True,
            "warehouse_logistics_status": "warehouse_logistics_complete",
        }

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fake_fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", fake_wait_orders)
    monkeypatch.setattr(contact_sync, "click_system_order", fake_click_system)
    monkeypatch.setattr(contact_sync, "wait_for_detail", fake_wait_detail)
    monkeypatch.setattr(contact_sync, "assert_current_detail_order", fake_assert_detail)
    monkeypatch.setattr(contact_sync, "collect_order_folder_json_context", fake_collect_context)
    monkeypatch.setattr(contact_sync, "read_detail_shipping_address_text", fake_shipping)
    monkeypatch.setattr(contact_sync, "build_and_create_order_folder_from_lines", fake_build_folder)
    monkeypatch.setattr(contact_sync, "run_tent_sku_adjustment_stage", fake_sku_stage)
    monkeypatch.setattr(contact_sync, "run_tent_package_split_stage", fake_package_stage)
    monkeypatch.setattr(contact_sync, "run_tent_instruction_remark_stage", fake_instruction_stage)
    monkeypatch.setattr(contact_sync, "run_tent_warehouse_logistics_stage", fake_warehouse_stage)

    item = BatchOrderItem(
        "103719401767966430",
        "111-8112209-3174649",
        "111-8112209-3174649 no asin",
        paid_at_text="2020-01-01 00:00:00",
        product_type=PRODUCT_TYPE_TENT,
    )

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            item,
            object(),
            dedupe_path=tmp_path / "processed_platform_orders.json",
            payment_window_hours=1,
            ignore_dedupe=True,
            ignore_payment_window=True,
            create_folder=False,
            download_custom_zip=False,
            allow_sku_adjustment_page_write=False,
            allow_package_split_page_write=True,
            write_dedupe=False,
        )
    )

    assert result["status"] == "updated"
    assert result["sku_adjustment_status"] == "write_disabled"
    assert result["sku_adjustment_complete"] is False
    assert result["sku_adjustment_plan_only"] is True
    assert result["package_split_complete"] is True
    assert ("sku_stage", False) in calls
    assert ("package_stage", True) in calls
    assert ("instruction_stage", None) in calls
    assert ("warehouse_stage", True) in calls


def test_safe_retry_allows_sku_and_package_page_write_only_with_explicit_switch(monkeypatch, tmp_path):
    """验证安全重测只有显式开关才允许 SKU 调整和拆包页面写入。"""
    captured: list[tuple[bool, bool, Path]] = []

    async def fake_process_batch_order_item(*_args, **kwargs):
        """模拟处理 批量 订单行行为，隔离测试中的外部依赖。"""
        captured.append(
            (
                kwargs["allow_sku_adjustment_page_write"],
                kwargs["allow_package_split_page_write"],
                Path(kwargs["log_dir"]),
            )
        )
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
    args.log_dir = str(tmp_path / "runtime-logs")
    asyncio.run(contact_sync.process_batch_candidate_with_policy(object(), item, object(), args, set(), ignore_dedupe=True))

    args = prepare_retry_order_args(
        build_parser().parse_args(
            ["--retry-order", "112-1234567-1234567", "--allow-sku-adjustment", "--allow-package-split"]
        )
    )
    args.log_dir = str(tmp_path / "runtime-logs")
    asyncio.run(contact_sync.process_batch_candidate_with_policy(object(), item, object(), args, set(), ignore_dedupe=True))

    assert captured == [
        (False, False, tmp_path / "runtime-logs"),
        (True, True, tmp_path / "runtime-logs"),
    ]


def test_safe_retry_sku_stage_ignores_stage_dedupe(monkeypatch, tmp_path):
    """验证安全重测会忽略帐篷 SKU 阶段旧完成记录并重新生成调整计划。"""

    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_sku_adjustment_platform_order(
        dedupe_path,
        "112-1234567-1234567",
        "103700000000000000",
        sku_status="auto",
    )
    calls: list[str] = []

    async def fake_close(_page):
        """模拟关闭详情弹窗，隔离页面依赖。"""

        calls.append("close")

    async def fake_read_deadline(_page, *, system_order_no, platform_order_no):
        """模拟读取列表发货时限，确保阶段继续进入 planner。"""

        calls.append(f"deadline:{system_order_no}:{platform_order_no}")
        return "2026-07-07 14:59:59"

    def fake_build_plan(**_kwargs):
        """模拟生成需要调整的帐篷 SKU 计划。"""

        calls.append("build_plan")
        return TentSkuAdjustmentPlan(
            platform_order_no="112-1234567-1234567",
            system_order_no="103700000000000000",
            destination=DestinationRegion(raw_text="United States, NY", country="US", state="NY", category="us_mainland"),
            replace_main_sku="Instruction",
            add_items=[
                TentSkuPlanAction(action="add", sku="10X10-FRAME-40MM-SQUARE", quantity=1, reason="测试支架"),
            ],
        )

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "read_list_shipping_deadline_text", fake_read_deadline)
    captured_plan_kwargs: dict[str, object] = {}

    def fake_build_plan_with_capture(**kwargs):
        captured_plan_kwargs.update(kwargs)
        return fake_build_plan(**kwargs)

    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", fake_build_plan_with_capture)

    result = asyncio.run(
        contact_sync.run_tent_sku_adjustment_stage(
            object(),
            BatchOrderItem(
                "103700000000000000",
                "112-1234567-1234567",
                "",
                paid_at_text="2026-07-03 14:59:59",
                logistics="Expedited",
            ),
            "103700000000000000",
            FolderBuildResult(status="folder_existing_platform_order", folder_components=["1个3x3m帐篷顶"]),
            shipping_address_text="United States, NY",
            dedupe_path=dedupe_path,
            write_dedupe=False,
            allow_page_write=False,
            read_dedupe=False,
        )
    )

    assert result["sku_adjustment_status"] == "write_disabled"
    assert result["sku_adjustment_dedupe_read_enabled"] is False
    assert result["sku_adjustment_replace_main_sku"] == "Instruction"
    assert captured_plan_kwargs["payment_time_text"] == "2026-07-03 14:59:59"
    assert captured_plan_kwargs["logistics_text"] == "Expedited"
    assert calls == ["close", "deadline:103700000000000000:112-1234567-1234567", "build_plan"]


def test_safe_retry_package_split_stage_ignores_stage_dedupe(monkeypatch, tmp_path):
    """验证安全重测会忽略拆包阶段旧完成记录并重新生成拆包计划。"""

    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_package_split_platform_order(
        dedupe_path,
        "112-1234567-1234567",
        "103700000000000000",
        package_status="auto",
        package_required=True,
        system_order_nos=["103700000000000001"],
    )
    calls: list[str] = []

    async def fake_close(_page):
        """模拟关闭详情弹窗，隔离页面依赖。"""

        calls.append("close")

    async def fake_read_deadline(_page, *, system_order_no, platform_order_no):
        """模拟读取列表发货时限，确保阶段继续进入 planner。"""

        calls.append(f"deadline:{system_order_no}:{platform_order_no}")
        return "2026-07-07 14:59:59"

    def fake_build_plan(**_kwargs):
        """模拟生成美国本土帐篷 SKU 计划，使拆包 planner 返回 ready。"""

        calls.append("build_plan")
        return TentSkuAdjustmentPlan(
            platform_order_no="112-1234567-1234567",
            system_order_no="103700000000000000",
            destination=DestinationRegion(raw_text="United States, NY", country="US", state="NY", category="us_mainland"),
            replace_main_sku="Instruction",
            add_items=[
                TentSkuPlanAction(action="add", sku="10X10-FRAME-40MM-SQUARE", quantity=1, reason="测试支架"),
                TentSkuPlanAction(action="add", sku="10x10-Canopy-Topper", quantity=1, reason="测试布料"),
            ],
        )

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "read_list_shipping_deadline_text", fake_read_deadline)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", fake_build_plan)

    result = asyncio.run(
        contact_sync.run_tent_package_split_stage(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            "103700000000000000",
            FolderBuildResult(status="folder_existing_platform_order", folder_components=["1个3x3m帐篷顶"]),
            shipping_address_text="United States, NY",
            dedupe_path=dedupe_path,
            write_dedupe=False,
            allow_page_write=False,
            read_dedupe=False,
        )
    )

    assert result["package_split_status"] == "write_disabled"
    assert result["package_split_plan_status"] == "ready"
    assert result["package_split_dedupe_read_enabled"] is False
    assert [package["package_key"] for package in result["package_split_packages"]] == ["accessory", "frame"]
    assert calls == ["close", "deadline:103700000000000000:112-1234567-1234567", "build_plan"]


def test_safe_retry_instruction_remark_stage_respects_write_disabled(monkeypatch, tmp_path):
    """验证安全重测关闭页面写入时，说明书备注阶段不会真实写入。"""

    dedupe_path = tmp_path / "processed_platform_orders.json"
    calls: list[str] = []

    async def fake_close(_page):
        """模拟关闭详情弹窗。"""

        calls.append("close")

    async def fake_read_deadline(_page, *, system_order_no, platform_order_no):
        """模拟读取发货时限。"""

        calls.append(f"deadline:{system_order_no}:{platform_order_no}")
        return "2026-07-07 14:59:59"

    def fake_build_plan(**_kwargs):
        """模拟需要写说明书备注的 SKU 计划。"""

        calls.append("build_plan")
        return TentSkuAdjustmentPlan(
            platform_order_no="112-1234567-1234567",
            system_order_no="103700000000000000",
            destination=DestinationRegion(raw_text="United States, NY", country="US", state="NY", category="us_mainland"),
            replace_main_sku="Instruction",
            customer_remark="7.3发说明书",
        )

    async def fake_upsert(**_kwargs):
        """如果写入被调用，则测试应失败。"""

        raise AssertionError("页面写入关闭时不应写说明书备注")

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "read_list_shipping_deadline_text", fake_read_deadline)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", fake_build_plan)
    monkeypatch.setattr(contact_sync, "upsert_instruction_customer_remark", fake_upsert)

    result = asyncio.run(
        contact_sync.run_tent_instruction_remark_stage(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            "103700000000000000",
            FolderBuildResult(status="folder_existing_platform_order", folder_components=["1个3x3m帐篷顶"]),
            shipping_address_text="United States, NY",
            package_split_system_order_nos=["103700000000000001"],
            dedupe_path=dedupe_path,
            write_dedupe=False,
            allow_page_write=False,
            read_dedupe=False,
        )
    )

    assert result["instruction_remark_required"] is True
    assert result["instruction_remark_status"] == "write_disabled"
    assert result["instruction_remark_complete"] is False
    assert result["instruction_remark_dedupe_read_enabled"] is False
    assert result["instruction_remark_customer_remark"] == "7.3发说明书"
    assert calls == ["close", "deadline:103700000000000000:112-1234567-1234567", "build_plan"]


def test_instruction_remark_required_checks_all_main_replacements():
    plan = TentSkuAdjustmentPlan(
        platform_order_no="112-1234567-1234567",
        system_order_no="103700000000000000",
        destination=DestinationRegion(raw_text="United States, NY", country="US", state="NY", category="us_mainland"),
        replace_main_sku="10X10-FRAME-40MM-SQUARE",
        replace_main_items=[
            TentSkuPlanAction(action="replace_main", sku="10X10-FRAME-40MM-SQUARE", quantity=1),
            TentSkuPlanAction(action="replace_main", sku="Instruction", quantity=1),
        ],
        customer_remark="7.15发说明书",
    )

    assert contact_sync.tent_instruction_remark_required(plan) is True


def test_instruction_remark_stage_writes_target_system_order(monkeypatch, tmp_path):
    """验证说明书备注写入拆包强响应映射的目标系统单号。"""

    dedupe_path = tmp_path / "processed_platform_orders.json"
    calls: list[tuple] = []

    async def fake_close(_page):
        calls.append(("close",))

    async def fake_read_deadline(_page, *, system_order_no, platform_order_no):
        calls.append(("deadline", system_order_no, platform_order_no))
        return "2026-07-07 14:59:59"

    def fake_build_plan(**_kwargs):
        calls.append(("build_plan",))
        return TentSkuAdjustmentPlan(
            platform_order_no="112-1234567-1234567",
            system_order_no="103700000000000000",
            destination=DestinationRegion(raw_text="United States, NY", country="US", state="NY", category="us_mainland"),
            replace_main_sku="Instruction",
            customer_remark="7.3发说明书",
        )

    async def fake_upsert(_page, *, platform_order_no, system_order_no, remark):
        calls.append(("upsert", platform_order_no, system_order_no, remark))
        return "append"

    async def forbidden_remark_confirm(*_args, **_kwargs):
        raise AssertionError("说明书备注已在拆包弹窗确认，不应再次弹窗")

    async def allow_guard(*_args, **_kwargs):
        return True

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "read_list_shipping_deadline_text", fake_read_deadline)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", fake_build_plan)
    monkeypatch.setattr(contact_sync, "upsert_instruction_customer_remark", fake_upsert)

    result = asyncio.run(
        contact_sync.run_tent_instruction_remark_stage(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            "103700000000000000",
            FolderBuildResult(status="folder_existing_platform_order", folder_components=["1个3x3m帐篷顶"]),
            shipping_address_text="United States, NY",
            package_split_system_order_nos=["103700000000000001", "103700000000000002"],
            package_split_instruction_system_order_no="103700000000000002",
            instruction_remark_confirmation_granted=True,
            dedupe_path=dedupe_path,
            write_dedupe=True,
            allow_page_write=True,
            read_dedupe=True,
            interaction_policy=SimpleNamespace(
                confirm_instruction_remark=forbidden_remark_confirm,
                runtime_write_guard=allow_guard,
            ),
        )
    )

    assert result["instruction_remark_complete"] is True
    assert result["instruction_remark_status"] == "instruction_remark_complete"
    assert result["instruction_remark_action"] == "append"
    assert result["instruction_remark_target_system_order_no"] == "103700000000000002"
    assert result["instruction_remark_recorded"] is True
    assert is_instruction_remark_done(dedupe_path, "112-1234567-1234567") is True
    assert ("upsert", "112-1234567-1234567", "103700000000000002", "7.3发说明书") in calls
    assert not any(call[0] in {"refresh", "find", "fill", "wait"} for call in calls)


def test_runtime_guard_blocks_instruction_write_before_browser_mutation(monkeypatch, tmp_path):
    calls: list[tuple] = []

    async def fake_close(_page):
        calls.append(("close",))

    async def fake_read_deadline(_page, *, system_order_no, platform_order_no):
        return "2026-07-07 14:59:59"

    def fake_build_plan(**_kwargs):
        return TentSkuAdjustmentPlan(
            platform_order_no="112-1234567-1234567",
            system_order_no="103700000000000000",
            destination=DestinationRegion(raw_text="United States, NY", category="us_mainland"),
            replace_main_sku="Instruction",
            customer_remark="7.3发说明书",
        )

    async def forbidden_upsert(*_args, **_kwargs):
        raise AssertionError("runtime guard blocked the stage, so browser write must not run")

    async def approve(*_args, **_kwargs):
        return True

    async def reject(*_args, **_kwargs):
        return False

    async def choose(_platform, _system, contacts):
        return contacts[0] if contacts else None

    async def guard(stage, platform_order_no, system_order_no):
        calls.append(("guard", stage, platform_order_no, system_order_no))
        return False

    policy = contact_sync.CustomOrderInteractionPolicy(
        confirm_writeback=approve,
        confirm_folder_creation=approve,
        confirm_sku_plan=approve,
        confirm_manual_sku_done=reject,
        confirm_package_split_plan=approve,
        confirm_manual_package_split_done=reject,
        choose_contact=choose,
        runtime_write_guard=guard,
    )
    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "read_list_shipping_deadline_text", fake_read_deadline)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", fake_build_plan)
    monkeypatch.setattr(contact_sync, "upsert_instruction_customer_remark", forbidden_upsert)

    result = asyncio.run(
        contact_sync.run_tent_instruction_remark_stage(
            object(),
            BatchOrderItem("103700000000000000", "112-1234567-1234567", ""),
            "103700000000000000",
            FolderBuildResult(
                status="folder_existing_platform_order",
                folder_components=["1个3x3m帐篷顶"],
            ),
            shipping_address_text="United States, NY",
            package_split_system_order_nos=["103700000000000001"],
            dedupe_path=tmp_path / "state.sqlite3",
            write_dedupe=True,
            allow_page_write=True,
            read_dedupe=False,
            interaction_policy=policy,
        )
    )

    assert result["instruction_remark_status"] == "paused_by_emergency_stop"
    assert result["runtime_write_guard_blocked"] is True
    assert result["runtime_write_guard_stage"] == "instruction_remark"
    assert result["manual_review_required"] is False
    assert calls == [
        ("close",),
        (
            "guard",
            "instruction_remark",
            "112-1234567-1234567",
            "103700000000000000",
        ),
    ]


def test_no_dedupe_write_helpers_do_not_create_state_file(tmp_path):
    """验证安全重测模式中的无去重写入 helpers 不会 创建州文件场景。"""
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
    package_recorded = contact_sync.record_package_split_if_allowed(
        dedupe_path,
        "112-1234567-1234567",
        "103700000000000000",
        write_enabled=False,
        package_status="auto",
        package_required=True,
        system_order_nos=[],
    )

    assert contact_recorded is False
    assert final_recorded is False
    assert package_recorded is False
    assert not dedupe_path.exists()


def test_package_split_not_required_notice_names_canada_and_non_mainland(capsys):
    """验证加拿大或美国非本土无需拆包时会在命令行明确提示。"""

    contact_sync.notify_tent_package_split_not_required_in_cmd(
        TentPackageSplitPlan(
            platform_order_no="702-1479942-0568242",
            system_order_no="103718059647957737",
            destination=DestinationRegion(raw_text="Canada", country="CA", category="canada"),
            status="not_required",
            required=False,
            reason="加拿大或美国非本土订单无需拆分包裹。",
        )
    )

    output = capsys.readouterr().out
    assert "加拿大/美国非本土订单不需要拆分包裹" in output
    assert "已记录拆分包裹阶段完成" in output


def test_refresh_order_list_for_package_split_reloads_and_searches(monkeypatch):
    """验证拆包前会刷新列表并重新搜索当前平台单号。"""

    calls: list[tuple[str, object]] = []

    class FakePackageSplitRefreshPage:
        async def reload(self, *, wait_until: str):
            """模拟刷新订单管理页。"""

            calls.append(("reload", wait_until))

        async def goto(self, _url: str, *, wait_until: str):
            """模拟刷新失败后的兜底跳转。"""

            calls.append(("goto", wait_until))

        async def wait_for_timeout(self, timeout_ms: int):
            """模拟刷新后的短等待。"""

            calls.append(("wait", timeout_ms))

    async def fake_close(_page):
        """模拟关闭详情弹窗。"""

        calls.append(("close", None))

    async def fake_fill(_page, order_no, search_kind):
        """模拟重新搜索平台单号。"""

        calls.append(("fill", (order_no, search_kind)))
        return {"search_validation_ok": True}

    async def fake_wait(_page, order_no, search_kind, timeout):
        """模拟等待订单列表重新出现。"""

        calls.append(("wait_orders", (order_no, search_kind, timeout)))
        return ["103718015616447733"]

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fake_fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", fake_wait)

    result = asyncio.run(
        contact_sync.refresh_order_list_for_package_split(
            FakePackageSplitRefreshPage(),
            "114-9069900-8646659",
            "103718015616447733",
        )
    )

    assert result["package_split_refresh_status"] == "refreshed"
    assert result["package_split_refresh_system_order_nos"] == ["103718015616447733"]
    assert calls == [
        ("close", None),
        ("reload", "domcontentloaded"),
        ("wait", 1500),
        ("fill", ("114-9069900-8646659", "platform")),
        ("wait_orders", ("114-9069900-8646659", "platform", 30)),
    ]


def test_finalize_skips_zip_copy_when_folder_write_disabled(tmp_path):
    """验证安全重测模式中的收尾处理跳过zip复制当文件夹写入 disabled场景。"""
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
    """验证安全重测模式中的安全重测 成功消息 不会 声称最终去重写入场景。"""
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
    """验证安全重测模式中的已存在文件夹提示区分 安全重测 来自去重场景。"""
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
    """验证安全重测模式中的规则缺失行 include 原始 定制化 行场景。"""
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
    """验证安全重测模式中的文件夹规则缺失状态使用相同 定制化 行格式场景。"""
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
    """验证安全重测模式中的规则缺失中间状态使用缺失行 details场景。"""
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


def test_folder_failure_reason_includes_rule_missing_detail_immediately():
    """首次失败信息必须直接显示哪个定制选项缺少规则。"""
    result = FolderBuildResult(
        status="vinyl_banners_rule_missing_printed_sides",
        missing_rule_title="Printed Sides",
        missing_rule_value="missing",
        missing_rule_line="1.Printed Sides = missing",
    )

    assert contact_sync.format_folder_failure_reason(result) == (
        "vinyl_banners_rule_missing_printed_sides"
        "（缺少规则：Printed Sides = missing）"
    )


def test_folder_failure_reason_includes_non_rule_error_detail():
    result = FolderBuildResult(
        status="folder_invalid_payment_time",
        error="付款时间无法解析：invalid value",
    )

    assert contact_sync.format_folder_failure_reason(result) == (
        "folder_invalid_payment_time（付款时间无法解析：invalid value）"
    )


def test_rule_missing_lines_fallback_to_field_and_error_when_line_not_found():
    """验证安全重测模式中的规则缺失行兜底到字段并错误当行不 found场景。"""
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
    """验证安全重测模式中的批量 跳过提示打印规则缺失 details场景。"""
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
    """验证安全重测模式中的批量 跳过提示打印规则缺失中间状态 details场景。"""
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


def test_safe_retry_bat_is_removed_from_desktop_refactor():
    assert not (ROOT / "安全重测单个订单.bat").exists()
    assert not (ROOT / "启动领星网页同步.bat").exists()
