from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from ..models import BatchOrderItem, OrderFolderLine
from ..products.car_magnets import PRODUCT_TYPE_CAR_MAGNET
from ..products.catalog import PRODUCT_TYPE_TENT
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
HIGH_VALUE_DIRECT_THRESHOLD_CURRENCIES = frozenset({"USD", "CAD"})
HIGH_VALUE_SPLIT_WEIGHT_THRESHOLDS_G = frozenset({3000, 4000, 5000})
DEFAULT_HIGH_VALUE_SPLIT_WEIGHT_THRESHOLD_G = 3000
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
    estimated_actual_weight_g: Decimal | None = None
    weight_threshold_g: int | None = None


def evaluate_high_value_split(
    item: BatchOrderItem,
    order_lines: Iterable[OrderFolderLine] | None,
    *,
    shipping_address_text: str | None,
) -> HighValueSplitEvaluation:
    # Non-tent custom orders use the displayed order total as a direct numeric
    # threshold (not FX conversion).  Claim protection applies only to US
    # destinations: Canadian orders must stay intact regardless of their
    # amount or quantity, while an unrecognized destination fails closed.
    lines = list(order_lines or ())
    product_types = {
        str(value or "").strip()
        for value in [
            item.product_type,
            *(line.product_type for line in lines),
        ]
        if str(value or "").strip()
    }
    if PRODUCT_TYPE_TENT in product_types:
        return HighValueSplitEvaluation(
            False,
            False,
            "tent_not_applicable",
            "帐篷产品不适用高金额非帐篷估重拆单规则。",
        )
    if not product_types.intersection(NON_TENT_HIGH_VALUE_PRODUCT_TYPES):
        return HighValueSplitEvaluation(False, False, "not_applicable", "不属于本规则支持的非帐篷品类。")

    destination = parse_destination_region(shipping_address_text)
    if destination.country == "CA":
        return HighValueSplitEvaluation(
            False,
            False,
            "canada_no_claim_protection",
            "加拿大订单没有索赔保护，无需换成说明书或拆单。",
        )
    if destination.country != "US":
        return HighValueSplitEvaluation(
            True,
            False,
            "destination_not_us",
            "无法确认目的国为美国，禁止自动换成说明书或拆单，请人工核对。",
        )

    customer_shipping_service = str(
        item.customer_shipping_service or ""
    ).strip()
    service_text = customer_shipping_service.casefold()
    if "expedited" in service_text or "加急" in service_text:
        return HighValueSplitEvaluation(
            False,
            False,
            "expedited_excluded",
            "加急订单不执行高金额非帐篷换货拆单。",
        )

    table_linen_product_types = {
        PRODUCT_TYPE_TABLECLOTHS,
        PRODUCT_TYPE_TABLE_RUNNERS,
    }
    if product_types and product_types.issubset(table_linen_product_types):
        if not lines:
            return HighValueSplitEvaluation(
                True,
                False,
                "table_linen_quantity_missing",
                "桌布/桌旗订单缺少可核对的商品数量，禁止自动换货拆单。",
            )
        try:
            table_linen_quantity = sum(int(line.quantity) for line in lines)
        except (TypeError, ValueError):
            table_linen_quantity = 0
        if table_linen_quantity <= 0:
            return HighValueSplitEvaluation(
                True,
                False,
                "table_linen_quantity_invalid",
                "桌布/桌旗订单商品总数量无效，禁止自动换货拆单。",
            )
        if table_linen_quantity <= 4:
            return HighValueSplitEvaluation(
                False,
                False,
                "table_linen_quantity_not_over_4",
                f"桌布/桌旗商品总数量为 {table_linen_quantity}，不超过 4，无需拆单。",
            )

    revenue_status = str(item.sales_revenue_status or "missing").strip()
    revenue_currency = str(item.sales_revenue_currency or "").strip().upper()
    if revenue_status == "non_usd" or (
        revenue_status == "complete"
        and revenue_currency
        and revenue_currency not in HIGH_VALUE_DIRECT_THRESHOLD_CURRENCIES
    ):
        return HighValueSplitEvaluation(
            True,
            False,
            "sales_revenue_non_usd",
            f"订单总金额币种为 {revenue_currency or '非 USD'}，"
            "当前规则仅支持 USD 和 CAD 直接按数值 200 判定，"
            "禁止自动换货拆单。",
        )
    if revenue_status != "complete":
        return HighValueSplitEvaluation(
            True,
            False,
            f"sales_revenue_{revenue_status}",
            "订单总金额缺失、格式异常或币种不完整，禁止自动换货拆单。",
        )
    if revenue_currency not in HIGH_VALUE_DIRECT_THRESHOLD_CURRENCIES:
        return HighValueSplitEvaluation(
            True,
            False,
            "sales_revenue_non_usd",
            "订单总金额币种不是 USD 或 CAD，禁止自动换货拆单。",
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
            f"订单总金额不足 200 {revenue_currency}，无需拆单。",
            total,
        )
    try:
        weight_threshold_g = int(item.high_value_split_weight_threshold_g)
    except (TypeError, ValueError):
        weight_threshold_g = 0
    if weight_threshold_g not in HIGH_VALUE_SPLIT_WEIGHT_THRESHOLDS_G:
        return HighValueSplitEvaluation(
            True,
            False,
            "weight_threshold_invalid",
            "高金额订单拆单估重阈值无效，必须选择 3、4 或 5kg，请检查设置。",
            total,
            None,
            weight_threshold_g or None,
        )
    estimated_weight_status = str(
        item.estimated_actual_weight_status or "missing"
    ).strip()
    if estimated_weight_status != "complete":
        return HighValueSplitEvaluation(
            True,
            False,
            f"estimated_actual_weight_{estimated_weight_status}",
            "订单金额已达到 200，但领星订单列表未返回有效的预估实重"
            "（logistics_info.pre_weight），禁止自动拆单，请人工核对。",
            total,
            None,
            weight_threshold_g,
        )
    try:
        estimated_actual_weight_g = Decimal(
            str(item.estimated_actual_weight_g or "")
        )
    except InvalidOperation:
        estimated_actual_weight_g = Decimal("NaN")
    if (
        not estimated_actual_weight_g.is_finite()
        or estimated_actual_weight_g <= 0
    ):
        return HighValueSplitEvaluation(
            True,
            False,
            "estimated_actual_weight_invalid",
            "领星预估实重无效，禁止自动拆单，请人工核对。",
            total,
            None,
            weight_threshold_g,
        )
    if estimated_actual_weight_g <= weight_threshold_g:
        return HighValueSplitEvaluation(
            False,
            False,
            "weight_not_over_threshold",
            f"订单总金额达到 200 {revenue_currency}，但预估实重 "
            f"{format(estimated_actual_weight_g, 'f')}g 未超过设置阈值 "
            f"{weight_threshold_g}g，无需拆单。",
            total,
            estimated_actual_weight_g,
            weight_threshold_g,
        )
    if not customer_shipping_service:
        return HighValueSplitEvaluation(
            True,
            False,
            "customer_shipping_service_missing",
            "订单详情未返回客选配送级别，禁止自动换成说明书或拆单，请人工核对。",
            total,
            estimated_actual_weight_g,
            weight_threshold_g,
        )
    return HighValueSplitEvaluation(
        True,
        True,
        "ready",
        f"订单总金额达到 200 {revenue_currency}，且预估实重 "
        f"{format(estimated_actual_weight_g, 'f')}g 超过设置阈值 {weight_threshold_g}g。",
        total,
        estimated_actual_weight_g,
        weight_threshold_g,
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
        or not str(line.order_item_id or "").strip()
        or int(line.quantity or 0) <= 0
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
    for line in lines:
        quantity = int(line.quantity)
        replace_items.append(
            TentSkuPlanAction(
                action="replace_main",
                sku=INSTRUCTION_SKU,
                quantity=quantity,
                reason="高金额定制订单整行换货为说明书",
                source_scope="order_item",
                source_sku=None,
                source_order_item_id=str(line.order_item_id or "").strip(),
                source_original_quantity=quantity,
            )
        )

    return TentSkuAdjustmentPlan(
        **base,
        replace_main_items=replace_items,
        # The order API reads and aggregates the live Lingxing local_sku values
        # immediately before mutation.  ASIN-derived or Amazon-side SKUs must
        # never be precomputed into this non-tent plan.
        add_items=[],
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
    if not sku_plan.operation_required and not sku_plan.manual_required:
        return TentPackageSplitPlan(
            **base,
            status="not_required",
            required=False,
            reason=(
                next(
                    (
                        str(warning or "").strip()
                        for warning in sku_plan.warnings
                        if str(warning or "").strip()
                    ),
                    "当前非帐篷订单无需拆单。",
                )
            ),
        )
    if sku_plan.manual_required or instruction_quantity <= 0:
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
