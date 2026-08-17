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
    NON_TENT_HIGH_VALUE_PRODUCT_TYPES,
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
        shipping_address_text="Los Angeles CA 90001 United States",
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


def test_official_multiplatform_transaction_total_and_currency_are_used() -> None:
    payload = {
        "global_order_no": "103732045296813607",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "amount_currency": "USD",
        "transaction_info": [{"order_total_amount": "$207.21"}],
        "item_info": [
            {
                "platform_order_no": "111-1262035-7672263",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "190.00",
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103732045296813607", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == "207.21"
    assert candidates[0].sales_revenue_currency == "USD"
    assert candidates[0].sales_revenue_status == "complete"
    assert candidates[0].sales_revenue_source == "order_total"


@pytest.mark.parametrize(
    ("raw_total", "normalized_total"),
    [
        ("CA$104.93", "104.93"),
        ("CA$200.00", "200.00"),
    ],
)
def test_canadian_transaction_total_never_enters_high_value_split(
    raw_total: str,
    normalized_total: str,
) -> None:
    payload = {
        "global_order_no": "103000000000000002",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "transaction_info": {"order_total_amount": raw_total},
        "item_info": [
            {
                "platform_order_no": "701-0000000-0000001",
                "product_no": "B0DBGBDHL7",
                "local_sku": "Tablecloth-Rectangle-6ft",
                "quantity": 1,
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103000000000000002", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == normalized_total
    assert candidates[0].sales_revenue_currency == "CAD"
    assert candidates[0].sales_revenue_status == "complete"
    assert candidates[0].sales_revenue_source == "order_total"
    evaluation = evaluate_high_value_split(
        candidates[0],
        _lines(),
        shipping_address_text="Toronto ON M5V 3A8 Canada",
    )
    assert evaluation.status == "canada_no_claim_protection"
    assert evaluation.requires_stage is False
    assert evaluation.operation_required is False
    assert "加拿大订单没有索赔保护" in evaluation.reason


def test_official_multiplatform_item_revenue_uses_order_currency() -> None:
    payload = {
        "global_order_no": "103732045296813608",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "amount_currency": "USD",
        "transaction_info": [],
        "item_info": [
            {
                "platform_order_no": "111-1262035-7672264",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "140.91",
            },
            {
                "platform_order_no": "111-1262035-7672264",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "70.45",
            },
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103732045296813608", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total == "211.36"
    assert candidates[0].sales_revenue_currency == "USD"
    assert candidates[0].sales_revenue_status == "complete"
    assert candidates[0].sales_revenue_source == "item_sales_revenue"


def test_merged_platform_orders_do_not_duplicate_system_transaction_total() -> None:
    payload = {
        "global_order_no": "103732045296813609",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "amount_currency": "USD",
        "transaction_info": [{"order_total_amount": "400.00"}],
        "item_info": [
            {
                "platform_order_no": "111-1262035-7672265",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "140.00",
            },
            {
                "platform_order_no": "111-1262035-7672266",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "260.00",
            },
        ],
        "platform_info": [
            {"platform_order_no": "111-1262035-7672265"},
            {"platform_order_no": "111-1262035-7672266"},
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103732045296813609", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert [candidate.platform_order_no for candidate in candidates] == [
        "111-1262035-7672265",
        "111-1262035-7672266",
    ]
    assert [candidate.sales_revenue_total for candidate in candidates] == [
        "140.00",
        "260.00",
    ]
    assert all(
        candidate.sales_revenue_source == "item_sales_revenue"
        for candidate in candidates
    )


def test_conflicting_transaction_totals_fail_closed() -> None:
    payload = {
        "global_order_no": "103732045296813610",
        "global_payment_time": int(datetime.now().timestamp()),
        "status": 4,
        "order_tag": [],
        "amount_currency": "USD",
        "transaction_info": [
            {"order_total_amount": "205.00"},
            {"order_total_amount": "206.00"},
        ],
        "item_info": [
            {
                "platform_order_no": "111-1262035-7672267",
                "product_no": "B0CQLN5GNL",
                "local_sku": 'BillyPrint-Car Magnet-12"x24"-2',
                "quantity": 1,
                "sales_revenue_amount": "205.00",
            }
        ],
    }
    rows, _, _ = _normalize_order(
        OrderRecord("103732045296813610", None, payload),
        source_page=1,
        source_order_index=0,
    )

    candidates = build_batch_candidates_from_rows(
        rows,
        set(),
        payment_window_hours=999999,
    )

    assert len(candidates) == 1
    assert candidates[0].sales_revenue_total is None
    assert candidates[0].sales_revenue_currency == "USD"
    assert candidates[0].sales_revenue_status == "invalid"
    assert candidates[0].sales_revenue_source == "order_total"


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


@pytest.mark.parametrize("currency", ["USD", "CAD"])
def test_exactly_200_supported_currency_enters_high_value_split_but_199_99_does_not(
    currency: str,
) -> None:
    exact = evaluate_high_value_split(
        _item(sales_revenue_total="200.00", sales_revenue_currency=currency),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    below = evaluate_high_value_split(
        _item(sales_revenue_total="199.99", sales_revenue_currency=currency),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert exact.requires_stage is True
    assert exact.operation_required is True
    assert f"200 {currency}" in exact.reason
    assert below.requires_stage is False
    assert below.status == "below_threshold"
    assert f"200 {currency}" in below.reason


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


@pytest.mark.parametrize("logistics", ["UPS Expedited", "Expedited", "加急配送"])
def test_expedited_orders_do_not_enter_high_value_split(logistics: str) -> None:
    result = evaluate_high_value_split(
        _item(logistics=logistics),
        _lines(),
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert result.requires_stage is False
    assert result.operation_required is False
    assert result.status == "expedited_excluded"
    assert "加急订单不执行" in result.reason


def test_canadian_destination_bypasses_all_high_value_conditions() -> None:
    canada = evaluate_high_value_split(
        _item(
            sales_revenue_total=None,
            sales_revenue_currency=None,
            sales_revenue_status="missing",
        ),
        None,
        shipping_address_text="Toronto ON M5V 3A8 Canada",
    )

    assert canada.status == "canada_no_claim_protection"
    assert canada.requires_stage is False
    assert canada.operation_required is False


def test_unrecognized_destination_never_enters_automatic_high_value_split() -> None:
    result = evaluate_high_value_split(
        _item(),
        _lines(),
        shipping_address_text="Paris 75001 France",
    )

    assert result.status == "destination_not_us"
    assert result.requires_stage is True
    assert result.operation_required is False
    assert "禁止自动换成说明书或拆单" in result.reason


@pytest.mark.parametrize(
    ("product_types", "quantities", "expected_status", "expected_operation"),
    [
        (("tablecloths",), (4,), "table_linen_quantity_not_over_4", False),
        (("table_runners", "table_runners"), (2, 2), "table_linen_quantity_not_over_4", False),
        (("tablecloths", "table_runners"), (2, 2), "table_linen_quantity_not_over_4", False),
        (("tablecloths",), (5,), "ready", True),
        (("tablecloths", "table_runners"), (3, 2), "ready", True),
    ],
)
def test_table_linen_high_value_split_requires_total_quantity_over_4(
    product_types: tuple[str, ...],
    quantities: tuple[int, ...],
    expected_status: str,
    expected_operation: bool,
) -> None:
    lines = [
        replace(
            _lines()[0],
            product_type=product_type,
            quantity=quantity,
            order_item_id=f"table-linen-{index}",
        )
        for index, (product_type, quantity) in enumerate(
            zip(product_types, quantities, strict=True),
            start=1,
        )
    ]

    result = evaluate_high_value_split(
        _item(product_type=product_types[0]),
        lines,
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert result.status == expected_status
    assert result.operation_required is expected_operation
    assert result.requires_stage is expected_operation


def test_table_linen_quantity_rule_fails_closed_when_lines_are_missing() -> None:
    result = evaluate_high_value_split(
        _item(product_type="tablecloths"),
        None,
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert result.status == "table_linen_quantity_missing"
    assert result.requires_stage is True
    assert result.operation_required is False


def test_pipeline_gate_skips_expedited_and_small_table_linen_orders() -> None:
    four_tablecloths = [replace(_lines()[0], quantity=4)]
    five_tablecloths = [replace(_lines()[0], quantity=5)]

    assert not contact_sync.order_requires_tent_sku_adjustment(
        _item(logistics="Expedited"),
        five_tablecloths,
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    assert not contact_sync.order_requires_tent_sku_adjustment(
        _item(logistics="Standard"),
        four_tablecloths,
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    assert contact_sync.order_requires_tent_sku_adjustment(
        _item(logistics="Standard"),
        five_tablecloths,
        shipping_address_text="Los Angeles CA 90001 United States",
    )
    assert not contact_sync.order_requires_tent_sku_adjustment(
        _item(logistics="Standard"),
        five_tablecloths,
        shipping_address_text="Toronto ON M5V 3A8 Canada",
    )


@pytest.mark.parametrize("product_type", sorted(NON_TENT_HIGH_VALUE_PRODUCT_TYPES))
def test_all_supported_non_tent_canadian_products_skip_instruction_split(
    product_type: str,
) -> None:
    lines = [
        replace(line, product_type=product_type)
        for line in _lines()
    ]

    result = evaluate_high_value_split(
        _item(product_type=product_type),
        lines,
        shipping_address_text="Canada, ON, Toronto, M5V 3A8",
    )

    assert result.status == "canada_no_claim_protection"
    assert result.requires_stage is False
    assert result.operation_required is False
    assert not contact_sync.order_requires_tent_sku_adjustment(
        _item(product_type=product_type),
        lines,
        shipping_address_text="Canada, ON, Toronto, M5V 3A8",
    )


def test_feather_flags_are_included_in_all_supported_non_tent_products() -> None:
    lines = [replace(line, product_type="feather_flags") for line in _lines()]

    result = evaluate_high_value_split(
        _item(product_type="feather_flags"),
        lines,
        shipping_address_text="Los Angeles CA 90001 United States",
    )

    assert result.status == "ready"
    assert result.operation_required is True


def test_canadian_plan_contains_no_instruction_or_split_actions() -> None:
    plan = build_high_value_sku_plan(
        item=_item(),
        system_order_no="103733256347324481",
        order_lines=_lines(),
        shipping_address_text="Canada, ON, Toronto, M5V 3A8",
        processed_at=datetime(2026, 8, 27, 12, 0, tzinfo=CHINA_TIMEZONE),
    )

    assert plan.operation_required is False
    assert plan.manual_required is False
    assert plan.replace_main_items == []
    assert plan.add_items == []
    assert plan.customer_remark is None
    assert plan.warnings == ["加拿大订单没有索赔保护，无需换成说明书或拆单。"]

    split_plan = build_high_value_package_split_plan(plan)
    assert split_plan.status == "not_required"
    assert split_plan.required is False
    assert split_plan.manual_required is False
    assert split_plan.packages_to_split == []


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


def test_plan_replaces_every_row_without_precomputing_sku_additions() -> None:
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
    assert plan.add_items == []
    assert plan.customer_remark == "8.27发说明书"

    split_plan = build_high_value_package_split_plan(plan)
    assert split_plan.required is True
    assert len(split_plan.packages_to_split) == 1
    assert split_plan.packages_to_split[0].package_key == "instruction"
    assert [
        (line.sku, line.quantity)
        for line in split_plan.packages_to_split[0].items
    ] == [("Instruction", 5)]


def test_api_adds_one_aggregated_row_for_repeated_live_local_sku() -> None:
    lines = [replace(line, sku=None) for line in _lines()]
    item = _item()
    plan = build_high_value_sku_plan(
        item=item,
        system_order_no=item.system_order_no,
        order_lines=lines,
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=datetime(2026, 8, 27, 12, 0, tzinfo=CHINA_TIMEZONE),
    )
    live_local_sku = "Tablecloth-Spandex-6ft-Lingxing"
    snapshot = _ApiOrderSnapshot(
        global_order_no=item.system_order_no,
        platform_order_nos=(item.platform_order_no,),
        shipping_deadline=None,
        remark="",
        items=tuple(
            _ApiOrderItem(
                item_id=f"erp-{index}",
                order_item_no=line.order_item_id,
                msku=f"amazon-msku-{index}",
                local_sku=live_local_sku,
                quantity=line.quantity,
                payload={},
            )
            for index, line in enumerate(lines, start=1)
        ),
        payload={},
    )

    wire_items, _replacements, _expected_totals, exact_added_totals = (
        LingxingCustomOrderApiOperations._build_sku_update_payload(
            plan,
            lines,
            snapshot,
        )
    )

    assert [entry for entry in wire_items if entry["type"] == 1] == [
        {
            "sku": live_local_sku,
            "quantity": 5,
            "type": 1,
            "platformOrderNo": item.platform_order_no,
        }
    ]
    assert len([entry for entry in wire_items if entry["type"] == 3]) == 3
    assert dict(exact_added_totals or {}) == {live_local_sku: 5}


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


@pytest.mark.parametrize(
    ("asin", "msku", "parsed_sku", "local_sku", "product_type", "quantity"),
    [
        (
            "B0CNVLXTWB",
            "95-JX79-30NB",
            "95-jx79-30nb",
            "Car-Magent-18x24in-2pcs",
            "car_magnet",
            2,
        ),
        (
            "B0DBG9KG7S",
            "Tablecloth-Spandex-4FT",
            "tablecloth-spandex-4ft",
            "Tablecloth-Spandex-4ft",
            "tablecloths",
            5,
        ),
        (
            "B0DL6CY8FB",
            "Custom Table Runner 12x72in",
            "custom-table-runner-12x72in",
            "Custom-Table-Runner-12x72in",
            "table_runners",
            5,
        ),
        (
            "B0DMW1DKPW",
            "Photo Poster 8x10in",
            "photo-poster-8x10in",
            "Photo-Poster-8x10in",
            "posters",
            1,
        ),
        (
            "B0CMQD3PH7",
            "BillyPrint-Vinyl-9'x9'",
            "vinyl-banners-9x9ft",
            "Vinyl-Banners-9x9ft",
            "vinyl_banners",
            2,
        ),
        (
            "B0CW57ZPFN",
            'BillyPrint- Retractable Banner-32"x78"',
            "x-banner-32x78in",
            "x-banner-32x78in",
            "x_stands",
            4,
        ),
        (
            "B0D1VBFL6R",
            "Table Top Retractable 11.5 x 17.5 1Sided",
            "table-top-retractable-11.5-x-17.5in-1-sided",
            "Table-Top-Retractable-11.5-x-17.5in-1-Sided",
            "roll_up_banners",
            5,
        ),
        (
            "B0G6JZJDDJ",
            "Step and Repeat Banner with Frame-8x8",
            "step-and-repeat-banner-with-frame-8x8",
            "Step-and-Repeat-Banner-with-Frame-8x8",
            "pop_up_displays",
            1,
        ),
        (
            "B0DS1ZD2DQ",
            "Rectangular Flag-0.75x4.4m",
            "rectangular-flag-0.75x4.4m",
            "Rectangular-Flag-0.75x4.4m",
            "feather_flags",
            1,
        ),
    ],
)
def test_high_value_api_restores_exact_lingxing_local_sku(
    asin: str,
    msku: str,
    parsed_sku: str,
    local_sku: str,
    product_type: str,
    quantity: int,
) -> None:
    line = OrderFolderLine(
        asin=asin,
        sku=parsed_sku,
        parent_asin=None,
        product_type=product_type,
        quantity=quantity,
        customization_text="custom",
        order_item_id="amazon-line-1",
    )
    item = _item(
        asin=asin,
        sku=msku,
        product_type=product_type,
        row_text=f"{asin} {msku}",
    )
    plan = build_high_value_sku_plan(
        item=item,
        system_order_no=item.system_order_no,
        order_lines=[line],
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=datetime(2026, 8, 13, 12, 0, tzinfo=CHINA_TIMEZONE),
    )
    before = _ApiOrderSnapshot(
        global_order_no=item.system_order_no,
        platform_order_nos=(item.platform_order_no,),
        shipping_deadline=None,
        remark="",
        items=(
            _ApiOrderItem(
                item_id="erp-line-1",
                order_item_no="amazon-line-1",
                msku=msku,
                local_sku=local_sku,
                quantity=quantity,
                payload={},
            ),
        ),
        payload={},
    )

    wire_items, replacements, expected_totals, expected_exact_added_totals = (
        LingxingCustomOrderApiOperations._build_sku_update_payload(
            plan,
            [line],
            before,
        )
    )

    assert [item for item in wire_items if item["type"] == 1] == [
        {
            "sku": local_sku,
            "quantity": quantity,
            "type": 1,
            "platformOrderNo": item.platform_order_no,
        }
    ]
    assert dict(expected_exact_added_totals or {}) == {local_sku: quantity}

    def readback(
        *,
        restored_sku: str,
        restored_quantity: int = quantity,
        instruction_quantity: int = quantity,
        deleted: bool = False,
    ) -> _ApiOrderSnapshot:
        return _ApiOrderSnapshot(
            global_order_no=item.system_order_no,
            platform_order_nos=(item.platform_order_no,),
            shipping_deadline=None,
            remark="",
            items=(
                _ApiOrderItem(
                    item_id="erp-line-1",
                    order_item_no="amazon-line-1",
                    msku=msku,
                    local_sku="Instruction",
                    quantity=instruction_quantity,
                    payload={},
                ),
                _ApiOrderItem(
                    item_id="erp-added-1",
                    order_item_no=None,
                    msku=None,
                    local_sku=restored_sku,
                    quantity=restored_quantity,
                    payload={},
                    sku_is_deleted=deleted,
                ),
            ),
            payload={},
        )

    assert not LingxingCustomOrderApiOperations._sku_update_applied(
        readback(restored_sku=local_sku.swapcase()),
        replacements,
        expected_totals,
        expected_exact_added_totals=expected_exact_added_totals,
    )
    assert not LingxingCustomOrderApiOperations._sku_update_applied(
        readback(restored_sku=local_sku, deleted=True),
        replacements,
        expected_totals,
        expected_exact_added_totals=expected_exact_added_totals,
    )
    assert not LingxingCustomOrderApiOperations._sku_update_applied(
        readback(restored_sku=local_sku, restored_quantity=quantity + 1),
        replacements,
        expected_totals,
        expected_exact_added_totals=expected_exact_added_totals,
    )
    assert not LingxingCustomOrderApiOperations._sku_update_applied(
        readback(restored_sku=local_sku, instruction_quantity=quantity + 1),
        replacements,
        expected_totals,
        expected_exact_added_totals=expected_exact_added_totals,
    )
    assert LingxingCustomOrderApiOperations._sku_update_applied(
        readback(restored_sku=local_sku),
        replacements,
        expected_totals,
        expected_exact_added_totals=expected_exact_added_totals,
    )


def test_high_value_api_rejects_deleted_source_local_sku() -> None:
    line = replace(_lines()[0], product_type="car_magnet")
    plan = build_high_value_sku_plan(
        item=_item(product_type="car_magnet"),
        system_order_no="103731847759327937",
        order_lines=[line],
        shipping_address_text="Los Angeles CA 90001 United States",
        processed_at=datetime(2026, 8, 13, 12, 0, tzinfo=CHINA_TIMEZONE),
    )
    snapshot = _ApiOrderSnapshot(
        global_order_no="103731847759327937",
        platform_order_nos=("114-2218890-7377033",),
        shipping_deadline=None,
        remark="",
        items=(
            _ApiOrderItem(
                item_id="erp-1",
                order_item_no=line.order_item_id,
                msku=line.sku,
                local_sku=line.sku,
                quantity=line.quantity,
                payload={},
                sku_is_deleted=True,
            ),
        ),
        payload={},
    )

    with pytest.raises(CustomOrderApiPlanError, match="有效的原始本地 SKU"):
        LingxingCustomOrderApiOperations._build_sku_update_payload(
            plan,
            [line],
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
    assert operations.sku_plan.add_items == []

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


def test_non_tent_sku_write_is_blocked_when_order_api_is_unavailable(
    monkeypatch,
    tmp_path,
) -> None:
    async def close(_page) -> None:
        return None

    async def approve(_plan) -> bool:
        return True

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)
    monkeypatch.setattr(contact_sync, "confirm_tent_sku_plan_in_cmd", approve)

    item = _item()
    payload = asyncio.run(
        contact_sync.run_tent_sku_adjustment_stage(
            object(),
            item,
            item.system_order_no,
            FolderBuildResult(status="folder_preview", folder_components=["tablecloth"]),
            _lines(),
            shipping_address_text="Los Angeles CA 90001 United States",
            dedupe_path=tmp_path / "workflow.json",
            write_dedupe=False,
            allow_page_write=True,
            api_operations=None,
        )
    )

    assert payload["sku_adjustment_write_source"] == "none"
    assert payload["sku_adjustment_status"] == "sku_adjustment_api_required"
    assert "实时 local_sku 和数量" in payload["sku_adjustment_error"]


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
