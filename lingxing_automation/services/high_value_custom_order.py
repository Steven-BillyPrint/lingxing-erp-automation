from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from ..models import BatchOrderItem, OrderFolderLine
from ..products.car_magnets import PRODUCT_TYPE_CAR_MAGNET
from ..products.feather_flags import PRODUCT_TYPE_FEATHER_FLAGS
from ..products.pop_up_displays import PRODUCT_TYPE_POP_UP_DISPLAYS
from ..products.posters import PRODUCT_TYPE_POSTERS
from ..products.roll_up_banners import PRODUCT_TYPE_ROLL_UP_BANNERS
from ..products.table_runners import PRODUCT_TYPE_TABLE_RUNNERS
from ..products.tablecloths import PRODUCT_TYPE_TABLECLOTHS
from ..products.vinyl_banners import PRODUCT_TYPE_VINYL_BANNERS
from ..products.x_stands import PRODUCT_TYPE_X_STANDS
from .china_workday import build_processing_instruction_customer_remark
from .tent_package_split_planner import (
    TentPackageSplitItem,
    TentPackageSplitPackage,
    TentPackageSplitPlan,
)
from .tent_sku_planner import (
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
    parse_destination_region,
)
from .tent_sku_rules import INSTRUCTION_SKU


HIGH_VALUE_SPLIT_THRESHOLD_USD = Decimal("200")
HIGH_VALUE_WORKFLOW_KIND = "non_tent_high_value"
NON_TENT_HIGH_VALUE_PRODUCT_TYPES = frozenset(
    {
        PRODUCT_TYPE_CAR_MAGNET,
        PRODUCT_TYPE_FEATHER_FLAGS,
        PRODUCT_TYPE_TABLECLOTHS,
        PRODUCT_TYPE_TABLE_RUNNERS,
        PRODUCT_TYPE_POSTERS,
        PRODUCT_TYPE_POP_UP_DISPLAYS,
        PRODUCT_TYPE_ROLL_UP_BANNERS,
        PRODUCT_TYPE_VINYL_BANNERS,
        PRODUCT_TYPE_X_STANDS,
    }
)


@dataclass(frozen=True)
class HighValueSplitEvaluation:
    requires_stage: bool
    operation_required: bool
    status: str
    reason: str
    sales_revenue_total: Decimal | None = None


def evaluate_high_value_split(
    item: BatchOrderItem,
    order_lines: Iterable[OrderFolderLine] | None,
    *,
    shipping_address_text: str | None,
) -> HighValueSplitEvaluation:
    # Non-tent custom orders intentionally have one business condition only:
    # the order's displayed total amount must be at least USD 200.
    # Destination and logistics speed are not exclusions for this workflow.
    del shipping_address_text
    product_types = {
        str(value or "").strip()
        for value in [
            item.product_type,
            *(line.product_type for line in order_lines or ()),
        ]
        if str(value or "").strip()
    }
    if not product_types.intersection(NON_TENT_HIGH_VALUE_PRODUCT_TYPES):
        return HighValueSplitEvaluation(False, False, "not_applicable", "不属于本规则支持的非帐篷品类。")

    revenue_status = str(item.sales_revenue_status or "missing").strip()
    if revenue_status != "complete":
        return HighValueSplitEvaluation(
            True,
            False,
            f"sales_revenue_{revenue_status}",
            "订单总金额缺失、格式异常或币种不完整，禁止自动换货拆单。",
        )
    if str(item.sales_revenue_currency or "").strip().upper() != "USD":
        return HighValueSplitEvaluation(
            True,
            False,
            "sales_revenue_non_usd",
            "销售收入币种不是 USD，禁止自动换货拆单。",
        )
    try:
        total = Decimal(str(item.sales_revenue_total or ""))
    except InvalidOperation:
        return HighValueSplitEvaluation(
            True,
            False,
            "sales_revenue_invalid",
            "销售收入合计无法解析，禁止自动换货拆单。",
        )
    if not total.is_finite() or total < 0:
        return HighValueSplitEvaluation(
            True,
            False,
            "sales_revenue_invalid",
            "销售收入合计无效，禁止自动换货拆单。",
        )
    if total < HIGH_VALUE_SPLIT_THRESHOLD_USD:
        return HighValueSplitEvaluation(
            False,
            False,
            "below_threshold",
            "销售收入合计不足 200 USD，无需拆单。",
            total,
        )
    return HighValueSplitEvaluation(
        True,
        True,
        "ready",
        "订单销售收入合计达到 200 USD。",
        total,
    )


def build_high_value_sku_plan(
    *,
    item: BatchOrderItem,
    system_order_no: str,
    order_lines: Iterable[OrderFolderLine] | None,
    shipping_address_text: str | None,
    processed_at: datetime,
    persisted_customer_remark: str | None = None,
    persisted_replaced_at: str | None = None,
) -> TentSkuAdjustmentPlan:
    lines = list(order_lines or [])
    destination = parse_destination_region(shipping_address_text)
    evaluation = evaluate_high_value_split(
        item,
        lines,
        shipping_address_text=shipping_address_text,
    )
    base = {
        "platform_order_no": item.platform_order_no,
        "system_order_no": system_order_no,
        "destination": destination,
        "workflow_kind": HIGH_VALUE_WORKFLOW_KIND,
        "operation_required": evaluation.operation_required,
        "instruction_replaced_at": persisted_replaced_at,
    }
    if not evaluation.operation_required:
        return TentSkuAdjustmentPlan(
            **base,
            manual_required=evaluation.requires_stage,
            manual_reason=evaluation.reason if evaluation.requires_stage else None,
            warnings=[evaluation.reason],
        )
    if not lines:
        return TentSkuAdjustmentPlan(
            **base,
            manual_required=True,
            manual_reason="订单商品行为空，禁止自动换货拆单。",
        )

    invalid_lines = [
        line
        for line in lines
        if line.product_type not in NON_TENT_HIGH_VALUE_PRODUCT_TYPES
        or not str(line.sku or "").strip()
        or not str(line.order_item_id or "").strip()
        or int(line.quantity or 0) <= 0
        or str(line.sku or "").strip().casefold() == INSTRUCTION_SKU.casefold()
    ]
    if invalid_lines:
        return TentSkuAdjustmentPlan(
            **base,
            manual_required=True,
            manual_reason="订单含不支持、标识不完整或已换货的商品行，禁止自动处理整单。",
        )

    # Validate both the current result and the possible post-18:00 result before mutation.
    customer_remark = str(persisted_customer_remark or "").strip()
    if not customer_remark:
        customer_remark = build_processing_instruction_customer_remark(processed_at=processed_at)
        build_processing_instruction_customer_remark(
            processed_at=processed_at.replace(hour=23, minute=59, second=59, microsecond=0)
        )

    replace_items: list[TentSkuPlanAction] = []
    aggregate: dict[str, TentSkuPlanAction] = {}
    aggregate_order: list[str] = []
    for line in lines:
        source_sku = str(line.sku or "").strip()
        quantity = int(line.quantity)
        replace_items.append(
            TentSkuPlanAction(
                action="replace_main",
                sku=INSTRUCTION_SKU,
                quantity=quantity,
                reason="高金额定制订单整行换货为说明书",
                source_scope="order_item",
                source_sku=source_sku,
                source_order_item_id=str(line.order_item_id or "").strip(),
                source_original_quantity=quantity,
            )
        )
        key = source_sku.casefold()
        if key not in aggregate:
            aggregate_order.append(key)
            aggregate[key] = TentSkuPlanAction(
                action="add",
                sku=source_sku,
                quantity=quantity,
                reason="换货后聚合回加原 SKU",
            )
        else:
            aggregate[key].quantity += quantity

    return TentSkuAdjustmentPlan(
        **base,
        replace_main_items=replace_items,
        add_items=[aggregate[key] for key in aggregate_order],
        customer_remark=customer_remark,
    )


def build_high_value_package_split_plan(
    sku_plan: TentSkuAdjustmentPlan,
) -> TentPackageSplitPlan:
    instruction_quantity = sum(
        int(action.quantity)
        for action in sku_plan.replace_main_items
        if str(action.sku or "").casefold() == INSTRUCTION_SKU.casefold()
    )
    base = {
        "platform_order_no": sku_plan.platform_order_no,
        "system_order_no": sku_plan.system_order_no,
        "destination": sku_plan.destination,
        "customer_remark": sku_plan.customer_remark,
    }
    if sku_plan.manual_required or instruction_quantity <= 0 or not sku_plan.add_items:
        return TentPackageSplitPlan(
            **base,
            status="manual_required",
            required=False,
            manual_required=True,
            manual_reason=sku_plan.manual_reason or "SKU 计划不完整，禁止自动拆单。",
            reason="高金额定制订单 SKU 计划未准备完成。",
        )
    return TentPackageSplitPlan(
        **base,
        status="ready",
        required=True,
        packages_to_split=[
            TentPackageSplitPackage(
                package_key="instruction",
                title="说明书商品组",
                items=[
                    TentPackageSplitItem(
                        sku=INSTRUCTION_SKU,
                        quantity=instruction_quantity,
                        reason="所有换货后的说明书商品行",
                    )
                ],
            )
        ],
        reason="说明书商品与回加的原 SKU 必须拆成两个系统订单。",
    )
