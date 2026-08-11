from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

import pytest

from erp_automation.application.custom_order_api import (
    CustomOrderApiPlanError,
    LingxingCustomOrderApiOperations,
    _ApiOrderItem,
    _ApiOrderSnapshot,
)
from erp_automation.application.api_scanners import _normalize_order
from erp_automation.application.lingxing_gateway import OrderRecord
from lingxing_automation.models import BatchOrderItem, OrderFolderLine
from lingxing_automation.models import FolderBuildResult
from lingxing_automation.flows import contact_sync
from lingxing_automation.services.custom_order_api import InstructionRemarkOutcome
from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_ORDER_SUMMARY_RESOLVED,
    AmazonOrderSummaryResult,
)
from lingxing_automation.services.tent_package_split_adjuster import TentPackageSplitResult
from lingxing_automation.services.tent_sku_adjuster import TentSkuAdjustmentResult
from lingxing_automation.pages.order_list import build_batch_candidates_from_rows
from lingxing_automation.services.china_workday import (
    CHINA_TIMEZONE,
    build_processing_instruction_customer_remark,
)
from lingxing_automation.services.high_value_custom_order import (
    HIGH_VALUE_WORKFLOW_KIND,
    build_high_value_package_split_plan,
    build_high_value_sku_plan,
    evaluate_high_value_split,
)
from lingxing_automation.storage.dedupe import (
    append_folder_complete_platform_order,
    append_sku_adjustment_platform_order,
    append_warehouse_logistics_platform_order,
    load_order_workflow_record,
)


def _item(**overrides) -> BatchOrderItem:
    values = {
        "system_order_no": "103731847759327937",
        "platform_order_no": "114-2218890-7377033",
        "row_text": "B0DBGBDHL7 Tablecloth-Spandex-6ft Standard",
        "asin": "B0DBGBDHL7",
        "sku": "Tablecloth-Spandex-6ft",
        "product_type": "tablecloths",
        "logistics": "Standard",
        "sales_revenue_total": "352.27",
        "sales_revenue_currency": "USD",
        "sales_revenue_status": "complete",
    }
    values.update(overrides)
    return BatchOrderItem(**values)


def _lines() -> list[OrderFolderLine]:
    return [
        OrderFolderLine(
            asin="B0DBGBDHL7",
            sku="Tablecloth-Spandex-6ft",
            parent_asin="B0DBG9JWYS",
            product_type="tablecloths",
            quantity=quantity,
            customization_text="custom",
            order_item_id=f"item-{index}",
        )
        for index, quantity in enumerate((2, 1, 2), start=1)
    ]


def test_api_rows_sum_displayed_sales_revenue_without_multiplying_quantity() -> None:
    payload = {
        "global_order_no": "103731847759327937",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "item_info": [
            {
                "platform_order_no": "114-2218890-7377033",
                "product_no": "B0DBGBDHL7",
                "local_sku": "Tablecloth-Spandex-6ft",
                "quantity": quantity,
                "sales_income": revenue,
            }
            for quantity, revenue in ((2, "$140.91"), (1, "$70.45"), (2, "$140.91"))
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103731847759327937", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == "352.27"
    assert candidates[0].sales_revenue_currency == "USD"
    assert candidates[0].sales_revenue_status == "complete"
    assert candidates[0].sales_revenue_source == "item_sales_revenue"


def test_api_rows_use_order_total_when_item_sales_revenue_is_missing() -> None:
    payload = {
        "global_order_no": "103732045296813606",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "order_total_amount": "$50.00",
        "item_info": [
            {
                "platform_order_no": "111-1262035-7672262",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103732045296813606", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == "50.00"
    assert candidates[0].sales_revenue_currency == "USD"
    assert candidates[0].sales_revenue_status == "complete"
    assert candidates[0].sales_revenue_source == "order_total"
    evaluation = evaluate_high_value_split(
        candidates[0],
        [],
        shipping_address_text=None,
    )
    assert evaluation.status == "below_threshold"
    assert evaluation.requires_stage is False


def test_order_total_is_authoritative_over_item_sales_revenue_sum() -> None:
    payload = {
        "global_order_no": "103731847759327938",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "order_total": "$205.00",
        "item_info": [
            {
                "platform_order_no": "114-2218890-7377034",
                "product_no": "B0DBGBDHL7",
                "local_sku": "Tablecloth-Spandex-6ft",
                "quantity": 1,
                "sales_income": "$190.00",
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103731847759327938", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == "205.00"


def test_present_non_usd_order_total_does_not_fall_back_to_item_revenue() -> None:
    payload = {
        "global_order_no": "103731847759327939",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "order_total": {"amount": "205.00", "currency": "EUR"},
        "item_info": [
            {
                "platform_order_no": "114-2218890-7377035",
                "product_no": "B0DBGBDHL7",
                "local_sku": "Tablecloth-Spandex-6ft",
                "quantity": 1,
                "sales_income": "$205.00",
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103731847759327939", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_status == "non_usd"
    assert candidates[0].sales_revenue_source == "order_total"
    assert candidates[0].sales_revenue_total is None


def test_exactly_200_usd_enters_high_value_split_but_199_99_does_not() -> None:
    exact = evaluate_high_value_split(
        _item(sales_revenue_total="200.00"),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    below = evaluate_high_value_split(
        _item(sales_revenue_total="199.99"),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert exact.requires_stage is True
    assert exact.operation_required is True
    assert below.requires_stage is False
    assert below.status == "below_threshold"


def test_amazon_order_total_fills_only_missing_lingxing_amount() -> None:
    summary = AmazonOrderSummaryResult(
        status=AMAZON_ORDER_SUMMARY_RESOLVED,
        platform_order_no="112-2749063-2058610",
        order_total="207.21",
        order_currency="USD",
    )
    missing = _item(
        sales_revenue_total=None,
        sales_revenue_currency=None,
        sales_revenue_status="missing",
        sales_revenue_source=None,
    )
    invalid = _item(
        sales_revenue_total=None,
        sales_revenue_currency="EUR",
        sales_revenue_status="non_usd",
        sales_revenue_source="order_total",
    )

    assert contact_sync.apply_amazon_order_total_if_missing(missing, summary) is True
    assert missing.sales_revenue_total == "207.21"
    assert missing.sales_revenue_source == "amazon_order_total"
    assert contact_sync.apply_amazon_order_total_if_missing(invalid, summary) is False
    assert invalid.sales_revenue_status == "non_usd"


def test_destination_and_logistics_speed_do_not_add_extra_conditions() -> None:
    expedited = evaluate_high_value_split(
        _item(logistics="UPS Expedited"),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    canada = evaluate_high_value_split(
        _item(),
        _lines(),
        shipping_address_text="Toronto ON M5V 3A8 Canada",
    )

    assert expedited.status == "ready"
    assert expedited.operation_required is True
    assert canada.status == "ready"
    assert canada.operation_required is True


def test_feather_flags_are_included_in_all_supported_non_tent_products() -> None:
    lines = [replace(line, product_type="feather_flags") for line in _lines()]

    result = evaluate_high_value_split(
        _item(product_type="feather_flags"),
        lines,
        shipping_address_text="Toronto ON M5V 3A8 Canada",
    )

    assert result.status == "ready"
    assert result.operation_required is True


def test_missing_revenue_fails_closed_to_manual_review() -> None:
    result = evaluate_high_value_split(
        _item(
            sales_revenue_total=None,
            sales_revenue_currency=None,
            sales_revenue_status="missing",
        ),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert result.requires_stage is True
    assert result.operation_required is False
    assert result.status == "sales_revenue_missing"


def test_plan_replaces_every_row_and_aggregates_original_sku_quantity() -> None:
    processed_at = datetime(2026, 8, 27, 12, 0, tzinfo=CHINA_TIMEZONE)
    plan = build_high_value_sku_plan(
        item=_item(),
        system_order_no="103731847759327937",
        order_lines=_lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=processed_at,
    )

    assert plan.workflow_kind == HIGH_VALUE_WORKFLOW_KIND
    assert plan.manual_required is False
    assert [(action.sku, action.quantity) for action in plan.replace_main_items] == [
        ("Instruction", 2),
        ("Instruction", 1),
        ("Instruction", 2),
    ]
    assert [(action.sku, action.quantity) for action in plan.add_items] == [
        ("Tablecloth-Spandex-6ft", 5)
    ]
    assert plan.customer_remark == "8.27发说明书"

    split_plan = build_high_value_package_split_plan(plan)
    assert split_plan.required is True
    assert len(split_plan.packages_to_split) == 1
    assert split_plan.packages_to_split[0].package_key == "instruction"
    assert [
        (line.sku, line.quantity)
        for line in split_plan.packages_to_split[0].items
    ] == [("Instruction", 5)]


def test_mixed_unsupported_product_line_is_manual_review() -> None:
    lines = _lines()
    lines.append(
        OrderFolderLine(
            asin="B0UNKNOWN00",
            sku="Unknown-SKU",
            parent_asin=None,
            product_type=None,
            quantity=1,
            customization_text="custom",
            order_item_id="item-unknown",
        )
    )
    plan = build_high_value_sku_plan(
        item=_item(),
        system_order_no="103731847759327937",
        order_lines=lines,
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=datetime(2026, 8, 27, 12, 0, tzinfo=CHINA_TIMEZONE),
    )

    assert plan.manual_required is True
    assert plan.replace_main_items == []
    assert plan.add_items == []


def test_api_rejects_partial_high_value_replacement_if_live_order_has_extra_row() -> None:
    plan = build_high_value_sku_plan(
        item=_item(),
        system_order_no="103731847759327937",
        order_lines=_lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=datetime(2026, 8, 27, 12, 0, tzinfo=CHINA_TIMEZONE),
    )
    snapshot_items = [
        _ApiOrderItem(
            item_id=f"erp-{index}",
            order_item_no=f"item-{index}",
            msku="Tablecloth-Spandex-6ft",
            local_sku="Tablecloth-Spandex-6ft",
            quantity=quantity,
            payload={},
        )
        for index, quantity in enumerate((2, 1, 2), start=1)
    ]
    snapshot_items.append(
        _ApiOrderItem(
            item_id="erp-extra",
            order_item_no="item-extra",
            msku="Unexpected-SKU",
            local_sku="Unexpected-SKU",
            quantity=1,
            payload={},
        )
    )
    snapshot = _ApiOrderSnapshot(
        global_order_no="103731847759327937",
        platform_order_nos=("114-2218890-7377033",),
        shipping_deadline=None,
        remark="",
        items=tuple(snapshot_items),
        payload={},
    )

    with pytest.raises(CustomOrderApiPlanError, match="全部商品行"):
        LingxingCustomOrderApiOperations._build_sku_update_payload(
            plan,
            _lines(),
            snapshot,
        )


def test_processing_remark_switches_at_18_and_skips_non_workdays() -> None:
    assert build_processing_instruction_customer_remark(
        processed_at=datetime(2026, 8, 27, 17, 59, tzinfo=CHINA_TIMEZONE)
    ) == "8.27发说明书"
    assert build_processing_instruction_customer_remark(
        processed_at=datetime(2026, 8, 27, 18, 0, tzinfo=CHINA_TIMEZONE)
    ) == "8.28发说明书"
    assert build_processing_instruction_customer_remark(
        processed_at=datetime(2026, 8, 28, 22, 0, tzinfo=CHINA_TIMEZONE)
    ) == "8.31发说明书"


def test_replacement_timestamp_and_remark_persist_and_warehouse_is_not_required(tmp_path) -> None:
    path = tmp_path / "workflow.json"
    append_folder_complete_platform_order(
        path,
        "114-2218890-7377033",
        "103731847759327937",
        product_type="tablecloths",
        sku_adjustment_required=True,
    )
    append_sku_adjustment_platform_order(
        path,
        "114-2218890-7377033",
        "103731847759327937",
        instruction_replaced_at="2026-08-27T22:00:00+08:00",
        instruction_customer_remark="8.28发说明书",
        workflow_kind=HIGH_VALUE_WORKFLOW_KIND,
    )
    append_warehouse_logistics_platform_order(
        path,
        "114-2218890-7377033",
        "103731847759327937",
        warehouse_status="not_required_non_tent",
        warehouse_required=False,
    )

    record = load_order_workflow_record(path, "114-2218890-7377033")
    assert record is not None
    assert record["instruction_replaced_at"] == "2026-08-27T22:00:00+08:00"
    assert record["instruction_customer_remark"] == "8.28发说明书"
    assert record["sku_adjustment_workflow_kind"] == HIGH_VALUE_WORKFLOW_KIND
    assert record["warehouse_logistics_required"] is False
    assert record["warehouse_logistics_complete"] is True


def test_non_tent_stages_use_api_persist_remark_and_skip_warehouse(monkeypatch, tmp_path) -> None:
    class Operations:
        def __init__(self) -> None:
            self.sku_plan = None
            self.split_plan = None
            self.remark = None

        async def get_shipping_deadline_text(self, **_kwargs):
            raise AssertionError("非帐篷流程不应读取帐篷发货时限")

        async def update_tent_skus(self, *, plan, order_lines):
            self.sku_plan = plan
            assert len(order_lines) == 3
            return TentSkuAdjustmentResult(
                status="sku_adjustment_complete",
                actions=["api"],
            )

        async def split_tent_packages(self, *, plan):
            self.split_plan = plan
            return TentPackageSplitResult(
                status="package_split_complete",
                actions=["api"],
                system_order_nos=["103731847759327937", "103731847759327938"],
                instruction_system_order_no="103731847759327938",
            )

        async def set_instruction_remark(self, *, remark, **_kwargs):
            self.remark = remark
            return InstructionRemarkOutcome(
                status="succeeded",
                action="updated",
                target_system_order_no="103731847759327938",
            )

    async def close(_page) -> None:
        return None

    async def approve(_plan) -> bool:
        return True

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)
    monkeypatch.setattr(contact_sync, "confirm_tent_sku_plan_in_cmd", approve)
    monkeypatch.setattr(contact_sync, "confirm_tent_package_split_plan_in_cmd", approve)

    operations = Operations()
    item = _item()
    lines = _lines()
    path = tmp_path / "workflow.json"
    append_folder_complete_platform_order(
        path,
        item.platform_order_no,
        item.system_order_no,
        product_type="tablecloths",
        sku_adjustment_required=True,
    )
    folder = FolderBuildResult(status="folder_preview", folder_components=["tablecloth"])

    sku_payload = asyncio.run(
        contact_sync.run_tent_sku_adjustment_stage(
            object(),
            item,
            item.system_order_no,
            folder,
            lines,
            shipping_address_text="Los Angeles CA 90001 United States",
            dedupe_path=path,
            write_dedupe=True,
            allow_page_write=True,
            api_operations=operations,
        )
    )
    assert sku_payload["sku_adjustment_complete"] is True
    assert item.instruction_replaced_at
    assert item.instruction_customer_remark
    assert [(line.sku, line.quantity) for line in operations.sku_plan.add_items] == [
        ("Tablecloth-Spandex-6ft", 5)
    ]

    split_payload = asyncio.run(
        contact_sync.run_tent_package_split_stage(
            object(),
            item,
            item.system_order_no,
            folder,
            lines,
            shipping_address_text="Los Angeles CA 90001 United States",
            dedupe_path=path,
            write_dedupe=True,
            allow_page_write=True,
            api_operations=operations,
        )
    )
    assert split_payload["package_split_complete"] is True
    assert operations.split_plan.packages_to_split[0].package_key == "instruction"

    remark_payload = asyncio.run(
        contact_sync.run_tent_instruction_remark_stage(
            object(),
            item,
            item.system_order_no,
            folder,
            lines,
            shipping_address_text="Los Angeles CA 90001 United States",
            package_split_system_order_nos=split_payload["package_split_system_order_nos"],
            package_split_instruction_system_order_no=split_payload[
                "package_split_instruction_system_order_no"
            ],
            instruction_remark_confirmation_granted=True,
            dedupe_path=path,
            write_dedupe=True,
            allow_page_write=True,
            api_operations=operations,
        )
    )

    assert remark_payload["instruction_remark_complete"] is True
    assert remark_payload["warehouse_logistics_required"] is False
    assert remark_payload["warehouse_logistics_complete"] is True
    assert operations.remark == item.instruction_customer_remark
    record = load_order_workflow_record(path, item.platform_order_no)
    assert record is not None
    assert record["instruction_replaced_at"] == item.instruction_replaced_at
    assert record["instruction_customer_remark"] == item.instruction_customer_remark
    assert record["warehouse_logistics_required"] is False


def test_split_order_retry_reuses_persisted_remark_instead_of_recomputing(monkeypatch, tmp_path) -> None:
    class Operations:
        def __init__(self) -> None:
            self.remark = None

        async def set_instruction_remark(self, *, remark, **_kwargs):
            self.remark = remark
            return InstructionRemarkOutcome(
                status="succeeded",
                action="updated",
                target_system_order_no="103731847759327938",
            )

    async def close(_page) -> None:
        return None

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)
    path = tmp_path / "workflow.json"
    item = _item(
        instruction_replaced_at=None,
        instruction_customer_remark=None,
    )
    workflow_record = {
        "platform_order_no": item.platform_order_no,
        "system_order_no": item.system_order_no,
        "instruction_replaced_at": "2026-08-27T22:00:00+08:00",
        "instruction_customer_remark": "8.28发说明书",
        "package_split_instruction_system_order_no": "103731847759327938",
    }
    operations = Operations()

    result = asyncio.run(
        contact_sync.run_persisted_high_value_instruction_remark_stage(
            object(),
            item,
            workflow_record=workflow_record,
            candidate_system_order_nos=[
                "103731847759327937",
                "103731847759327938",
            ],
            dedupe_path=path,
            write_dedupe=False,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=operations,
            interaction_policy=None,
        )
    )

    assert result["instruction_remark_complete"] is True
    assert operations.remark == "8.28发说明书"
    assert result["instruction_replaced_at"] == "2026-08-27T22:00:00+08:00"
