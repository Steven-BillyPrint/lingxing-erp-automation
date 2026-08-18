from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..models import OrderFolderLine
from ..products.feather_flags import PRODUCT_TYPE_FEATHER_FLAGS
from ..products.tablecloths import PRODUCT_TYPE_TABLECLOTHS
from ..products.tents import (
    get_tent_top_size,
    get_wall_only_asin_kind,
    is_default_expedited_tent_asin,
    normalize_asin,
)
from .china_workday import (
    ChinaWorkdayError,
    build_expedited_instruction_customer_remark,
    build_latest_instruction_customer_remark,
)
from .tent_sku_rules import (
    INSTRUCTION_SKU,
    SANDBAG_SKU,
    component_to_sku_items,
    detect_tent_size_key,
    roller_bag_sku,
    tent_accessory_component_to_sku_items,
    tent_top_sku,
    wall_sku_for_component,
)


US_STATE_CODES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "DC",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "PR",
    "VI",
    "GU",
}
US_NON_MAINLAND_STATES = {"AK", "PR", "VI", "HI", "GU", "GUM"}
US_NON_MAINLAND_NAMES = (
    "alaska",
    "puerto rico",
    "u.s. virgin islands",
    "us virgin islands",
    "virgin islands",
    "hawaii",
    "guam",
    "territory of guam",
)


@dataclass
class DestinationRegion:
    """收货地址解析结果。"""

    raw_text: str
    country: str | None = None
    state: str | None = None
    city: str | None = None
    postal_code: str | None = None
    postal_source: str | None = None
    postal_error: str | None = None
    category: str = "unknown"
    warning: str | None = None


@dataclass
class TentSkuPlanAction:
    """SKU 调整的一个动作。"""

    action: str
    sku: str | None = None
    quantity: int = 1
    reason: str = ""
    source_scope: str | None = None
    source_sku: str | None = None
    source_order_item_id: str | None = None
    source_original_quantity: int | None = None


@dataclass
class TentSkuAdjustmentPlan:
    """帐篷订单 SKU 调整计划。

    该结构只描述“应该做什么”，不直接依赖 Playwright 页面，方便单元测试和后续维护。
    """

    platform_order_no: str
    system_order_no: str
    destination: DestinationRegion
    replace_main_sku: str | None = None
    replace_main_quantity: int = 1
    replace_main_items: list[TentSkuPlanAction] = field(default_factory=list)
    main_product_items: list[TentSkuPlanAction] = field(default_factory=list)
    add_items: list[TentSkuPlanAction] = field(default_factory=list)
    customer_remark: str | None = None
    manual_required: bool = False
    manual_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    workflow_kind: str = "tent"
    operation_required: bool = True
    instruction_replaced_at: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        """将当前对象转换为日志字典，便于批量流程记录和排查。"""
        return {
            "sku_adjustment_destination": self.destination.__dict__,
            "sku_adjustment_replace_main_sku": self.replace_main_sku,
            "sku_adjustment_replace_main_quantity": self.replace_main_quantity,
            "sku_adjustment_replace_main_items": [item.__dict__ for item in self.replace_main_items],
            "sku_adjustment_main_product_items": [item.__dict__ for item in self.main_product_items],
            "sku_adjustment_add_items": [item.__dict__ for item in self.add_items],
            "sku_adjustment_customer_remark": self.customer_remark,
            "sku_adjustment_manual_required": self.manual_required,
            "sku_adjustment_manual_reason": self.manual_reason,
            "sku_adjustment_warnings": self.warnings,
            "sku_adjustment_workflow_kind": self.workflow_kind,
            "sku_adjustment_operation_required": self.operation_required,
            "instruction_replaced_at": self.instruction_replaced_at,
        }


def parse_destination_region(address_text: str | None) -> DestinationRegion:
    """解析收货地址里的国家、州和城市。

    美国非本土地区不写说明书备注，但仍会按 SKU 计划自动换货和补商品。
    """

    raw = str(address_text or "")
    compact = raw.lower()
    region = DestinationRegion(raw_text=raw, postal_code=extract_postal_code(raw))
    address_line = extract_shipping_address_line(raw) or raw
    address_compact = address_line.lower()
    leading_country_code = re.match(
        r"^\s*(US|CA)\s*(?=[,，])",
        address_line,
        flags=re.IGNORECASE,
    )
    country_code = leading_country_code.group(1).upper() if leading_country_code else ""
    if "canada" in compact or "加拿大" in raw or country_code == "CA":
        region.country = "CA"
        region.category = "canada"
        return region
    if (
        "united states" in compact
        or "usa" in compact
        or "美国" in raw
        or country_code == "US"
    ):
        region.country = "US"
        region.state = _extract_us_state(address_line) or _extract_us_state(raw)
        region.city = _extract_city_after_state(address_line, region.state)
        if region.state in US_NON_MAINLAND_STATES or any(name in compact for name in US_NON_MAINLAND_NAMES):
            region.category = "us_non_mainland"
            region.warning = "美国非本土地区，不自动换成说明书。"
        else:
            region.category = "us_mainland"
        return region
    region.warning = "未能从收货地址识别美国/加拿大地区。"
    return region


def extract_shipping_address_line(text: str | None) -> str:
    """从详情页整块收货信息中提取“收件地址”字段值，排除详细街道地址。"""

    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return ""
    labels = (
        "收件地址",
        "收货地址",
        "Shipping Address",
        "Ship To",
    )
    stop_labels = (
        "详细地址",
        "门牌号",
        "邮编",
        "地址类型",
        "买家姓名",
        "收件人",
        "公司",
        "电话",
        "买家邮箱",
    )
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*[:：]?\s*(.+?)(?=\s*(?:{stop_pattern})\s*[:：]?|$)",
            normalized,
            flags=re.IGNORECASE,
        )
        if match:
            value = match.group(1).strip(" -:：")
            if value and value != "-":
                return value
    return ""


def extract_postal_code(text: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return None
    labels = (
        "邮编",
        "閭紪",
        "ZIP",
        "Zip Code",
        "Postal Code",
        "Postcode",
    )
    stop_labels = (
        "地址类型",
        "鍦板潃绫诲瀷",
        "买家姓名",
        "涔板濮撳悕",
        "收件人",
        "鏀朵欢浜",
        "电话",
        "鐢佃瘽",
        "买家邮箱",
        "涔板閭",
    )
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*[:：]?\s*(.+?)(?=\s*(?:{stop_pattern})\s*[:：]?|$)",
            normalized,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        parsed = normalize_us_postal_code(match.group(1))
        if parsed:
            return parsed
    match = re.search(r"\b(\d{5})(?:-?\d{4})?\b", normalized)
    return match.group(1) if match else None


def normalize_us_postal_code(value: str | None) -> str | None:
    """Return only the first five US ZIP digits, preserving leading zeroes."""

    text = str(value or "").strip()
    if not text or text == "-" or "*" in text:
        return None
    match = re.fullmatch(r"(\d{5})(?:-\d{4}|\d{4})?", text)
    return match.group(1) if match else None


def _extract_us_state(text: str) -> str | None:
    """从输入内容中提取us州。"""
    normalized = str(text or "").replace("，", ",")
    parts = [part.strip(" ,()（）") for part in normalized.split(",") if part.strip()]
    for index, part in enumerate(parts[:-1]):
        lowered = part.lower()
        if "united states" not in lowered and "usa" not in lowered and "美国" not in part:
            continue
        state_match = re.search(r"\b([A-Z]{2})\b", parts[index + 1])
        if state_match and state_match.group(1) in US_STATE_CODES:
            return state_match.group(1)
    candidates = re.findall(r"(?:,|\s)([A-Z]{2,3})(?:,|\s|$)", normalized)
    for candidate in candidates:
        if candidate in {"USA", "US"}:
            continue
        if candidate not in US_STATE_CODES and candidate not in US_NON_MAINLAND_STATES:
            continue
        return candidate
    return None


def _extract_city_after_state(text: str, state: str | None) -> str | None:
    """从地址文本中提取州名后的城市信息。"""
    if not state:
        return None
    match = re.search(
        rf"\b{re.escape(state)}\b\s*,?\s*([A-Za-z .'-]+?)(?=\s*(?:,|详细地址|门牌号|邮编|地址类型|买家姓名|收件人|公司|电话|买家邮箱)\s*[:：]?|$)",
        text or "",
    )
    return match.group(1).strip() if match else None


def build_tent_sku_plan(
    *,
    platform_order_no: str,
    system_order_no: str,
    folder_components: list[str],
    destination_text: str | None,
    shipping_deadline_text: str | None = None,
    asin: str | None = None,
    payment_time_text: str | None = None,
    logistics_text: str | None = None,
    order_lines: list[OrderFolderLine] | None = None,
    processed_at: datetime | date | None = None,
) -> TentSkuAdjustmentPlan:
    """根据帐篷文件夹组件生成 SKU 调整计划。

    文件夹名已经是业务规则汇总后的稳定结果；SKU 阶段只根据这些中文片段反推需要补哪些商品。
    """

    destination = parse_destination_region(destination_text)
    plan = TentSkuAdjustmentPlan(
        platform_order_no=platform_order_no,
        system_order_no=system_order_no,
        destination=destination,
    )
    if destination.category not in {"us_mainland", "us_non_mainland", "canada"}:
        plan.manual_required = True
        plan.manual_reason = destination.warning or "未识别收货国家/地区，请人工添加 SKU。"
        return plan

    wall_only_kind = get_wall_only_asin_kind(asin)
    tent_groups = _extract_tent_groups(folder_components)
    tent_groups = _exclude_independent_main_product_groups(tent_groups, order_lines)
    if not wall_only_kind:
        tent_groups = _apply_unambiguous_order_line_quantity(tent_groups, order_lines)
    if not tent_groups:
        plan.manual_required = True
        plan.manual_reason = "未从文件夹组件中识别到帐篷配置，请人工添加 SKU。"
        return plan

    aggregated: dict[str, TentSkuPlanAction] = {}
    wall_only_replacement_sku = _wall_only_replacement_sku(wall_only_kind, tent_groups)
    first_size_key = _first_size_key(tent_groups)
    if wall_only_kind and not wall_only_replacement_sku:
        plan.manual_required = True
        plan.manual_reason = "独立墙体 ASIN 未从文件夹组件中识别到对应全围/半围 SKU，请人工添加 SKU。"
        return plan
    if not first_size_key and not wall_only_kind:
        plan.manual_required = True
        plan.manual_reason = "未识别帐篷尺寸，无法确定 SKU 表规格。"
        return plan

    if destination.category == "us_mainland" and not wall_only_kind:
        return _build_us_mainland_tent_sku_plan(
            plan=plan,
            tent_groups=tent_groups,
            shipping_deadline_text=shipping_deadline_text,
            payment_time_text=payment_time_text,
            logistics_text=logistics_text,
            asin=asin,
            order_lines=order_lines,
            processed_at=processed_at,
        )

    if wall_only_replacement_sku:
        plan.replace_main_sku = wall_only_replacement_sku
    elif destination.category == "canada":
        # 加拿大订单主商品换成帐篷顶；后续补加配件时不再重复添加帐篷顶。
        plan.replace_main_sku = tent_top_sku(first_size_key)
    elif destination.category == "us_non_mainland":
        roller_sku = _first_matching_sku(tent_groups, "拖轮包")
        sandbag_present = any("沙袋" in component for _, components in tent_groups for component in components)
        if roller_sku:
            plan.replace_main_sku = roller_sku
        elif sandbag_present:
            plan.replace_main_sku = SANDBAG_SKU
        else:
            plan.replace_main_sku = tent_top_sku(first_size_key)
    else:
        roller_sku = _first_matching_sku(tent_groups, "拖轮包")
        sandbag_present = any("沙袋" in component for _, components in tent_groups for component in components)
        if roller_sku:
            plan.replace_main_sku = roller_sku
        elif sandbag_present:
            plan.replace_main_sku = SANDBAG_SKU
        else:
            plan.replace_main_sku = INSTRUCTION_SKU
            try:
                plan.customer_remark = _build_instruction_remark_for_order(
                    shipping_deadline_text=shipping_deadline_text,
                    payment_time_text=payment_time_text,
                    logistics_text=logistics_text,
                    asin=asin,
                    processed_at=processed_at,
                )
            except ChinaWorkdayError as exc:
                # 说明书备注依赖明确的发货时限和节假日表；缺数据时宁可转人工，也不猜日期。
                plan.manual_required = True
                plan.manual_reason = (
                    f"主商品将换为说明书，但无法自动生成客服备注：{exc}。"
                    f"请人工添加说明书备注。发货时限：{shipping_deadline_text or '-'}；"
                    f"付款时间：{payment_time_text or '-'}；客选物流：{logistics_text or '-'}"
                )
                return plan

    allow_large_frame_rail = destination.category in {"canada", "us_non_mainland"}
    plan.replace_main_quantity = _replacement_quantity_for_sku(
        plan.replace_main_sku,
        tent_groups,
        allow_large_frame_rail=allow_large_frame_rail,
    )
    consumed_replacement_quantities: dict[str, int] = {}
    if plan.replace_main_sku:
        if order_lines is not None:
            replacement_items, replacement_error = _build_row_bound_replacements(
                target_sku=plan.replace_main_sku,
                replacement_quantity=plan.replace_main_quantity,
                order_lines=order_lines,
                asin=asin,
                allow_partial_quantity=(
                    destination.category == "us_non_mainland"
                    and not wall_only_kind
                    and (
                        _is_roller_sku(plan.replace_main_sku)
                        or plan.replace_main_sku == SANDBAG_SKU
                    )
                ),
            )
            if replacement_error:
                plan.manual_required = True
                plan.manual_reason = replacement_error
                return plan
            plan.replace_main_items = replacement_items
            plan.main_product_items = _build_final_main_product_items(
                order_lines,
                replacement_items,
            )
            _sync_legacy_replacement_fields(plan)
        else:
            plan.replace_main_items = [
                TentSkuPlanAction(
                    action="replace_main",
                    sku=plan.replace_main_sku,
                    quantity=plan.replace_main_quantity,
                )
            ]
        consumed_replacement_quantities[plan.replace_main_sku] = sum(
            item.quantity for item in plan.replace_main_items
        )
    for group_multiplier, group_components in tent_groups:
        size_key = detect_tent_size_key(group_components)
        if not size_key and wall_only_kind:
            wall_item = _wall_only_item_for_components(wall_only_kind, group_components)
            sku_items = [wall_item] if wall_item else []
            for component in group_components:
                sku_items.extend(tent_accessory_component_to_sku_items(component))
            for item in sku_items:
                quantity = item.quantity * group_multiplier
                quantity = _consume_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    consumed_sku_quantities=consumed_replacement_quantities,
                )
                if quantity <= 0:
                    continue
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)
            continue
        if not size_key:
            group_text = "+".join(group_components)
            accessory_items = tent_accessory_component_to_sku_items(group_text)
            if not accessory_items and _looks_like_tent_component(group_text):
                plan.warnings.append(f"未识别该帐篷配件 SKU，已跳过 SKU 生成：{group_text}")
                continue
            if not accessory_items:
                continue
            for item in accessory_items:
                quantity = item.quantity * group_multiplier
                quantity = _consume_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    consumed_sku_quantities=consumed_replacement_quantities,
                )
                if quantity <= 0:
                    continue
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)
            continue
        rail_required = _group_requires_frame_rail(size_key, group_components)
        for component in group_components:
            for item in component_to_sku_items(
                size_key,
                component,
                rail_required=rail_required,
                allow_large_frame_rail=allow_large_frame_rail,
            ):
                quantity = item.quantity * group_multiplier
                quantity = _consume_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    consumed_sku_quantities=consumed_replacement_quantities,
                )
                if quantity <= 0:
                    continue
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)

    restore_error = _restore_replaced_independent_main_products(
        aggregated=aggregated,
        replacements=plan.replace_main_items,
        order_lines=order_lines,
    )
    if restore_error:
        plan.manual_required = True
        plan.manual_reason = restore_error
        plan.add_items = []
        return plan

    plan.add_items = list(aggregated.values())
    return plan


def _build_us_mainland_tent_sku_plan(
    *,
    plan: TentSkuAdjustmentPlan,
    tent_groups: list[tuple[int, list[str]]],
    shipping_deadline_text: str | None,
    payment_time_text: str | None,
    logistics_text: str | None,
    asin: str | None,
    order_lines: list[OrderFolderLine] | None,
    processed_at: datetime | date | None,
) -> TentSkuAdjustmentPlan:
    replacement_items, consumed_sku_quantities, replacement_error = _build_us_mainland_replacements(
        tent_groups,
        plan.destination,
        order_lines=order_lines,
    )
    if replacement_error:
        plan.manual_required = True
        plan.manual_reason = replacement_error
        return plan
    plan.replace_main_items = replacement_items
    plan.main_product_items = _build_final_main_product_items(order_lines, replacement_items)
    _sync_legacy_replacement_fields(plan)
    if any(item.sku == INSTRUCTION_SKU for item in replacement_items):
        try:
            plan.customer_remark = _build_instruction_remark_for_order(
                shipping_deadline_text=shipping_deadline_text,
                payment_time_text=payment_time_text,
                logistics_text=logistics_text,
                asin=asin,
                processed_at=processed_at,
            )
        except ChinaWorkdayError as exc:
            plan.manual_required = True
            plan.manual_reason = (
                f"主商品将换为说明书，但无法自动生成客服备注：{exc}。"
                f"请人工添加说明书备注。发货时限：{shipping_deadline_text or '-'}；"
                f"付款时间：{payment_time_text or '-'}；客选物流：{logistics_text or '-'}"
            )
            return plan

    aggregated: dict[str, TentSkuPlanAction] = {}
    for group_multiplier, group_components in tent_groups:
        size_key = detect_tent_size_key(group_components)
        if not size_key:
            group_text = "+".join(group_components)
            accessory_items = tent_accessory_component_to_sku_items(group_text)
            if not accessory_items and _looks_like_tent_component(group_text):
                plan.warnings.append(f"未识别该帐篷配件 SKU，已跳过 SKU 生成：{group_text}")
                continue
            if not accessory_items:
                continue
            for item in accessory_items:
                quantity = _consume_replaced_sku_quantity(
                    item.quantity * group_multiplier,
                    sku=item.sku,
                    consumed_sku_quantities=consumed_sku_quantities,
                )
                if quantity > 0:
                    _add_aggregated_action(aggregated, item.sku, quantity, item.reason)
            continue

        for item in _group_sku_plan_actions(
            group_multiplier,
            group_components,
            allow_large_frame_rail=False,
        ):
            quantity = _consume_replaced_sku_quantity(
                item.quantity,
                sku=item.sku or "",
                consumed_sku_quantities=consumed_sku_quantities,
            )
            if quantity > 0 and item.sku:
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)

    restore_error = _restore_replaced_independent_main_products(
        aggregated=aggregated,
        replacements=plan.replace_main_items,
        order_lines=order_lines,
    )
    if restore_error:
        plan.manual_required = True
        plan.manual_reason = restore_error
        plan.add_items = []
        return plan

    plan.add_items = list(aggregated.values())
    return plan


def _build_us_mainland_replacements(
    tent_groups: list[tuple[int, list[str]]],
    destination: DestinationRegion,
    *,
    order_lines: list[OrderFolderLine] | None = None,
) -> tuple[list[TentSkuPlanAction], dict[str, int], str | None]:
    groups_with_size = [
        (group_multiplier, group_components)
        for group_multiplier, group_components in tent_groups
        if detect_tent_size_key(group_components)
    ]
    frame_priority = _is_frame_priority_destination(destination)
    all_items: list[TentSkuPlanAction] = []
    for group_multiplier, group_components in groups_with_size:
        all_items.extend(
            _group_sku_plan_actions(
                group_multiplier,
                group_components,
                allow_large_frame_rail=False,
            )
        )

    frame_queue = _expanded_sku_queue(all_items, _is_frame_sku)
    roller_queue = _expanded_sku_queue(all_items, _is_roller_sku)
    sandbag_queue = _expanded_sku_queue(all_items, lambda sku: sku == SANDBAG_SKU)
    has_any_accessory = bool(roller_queue or sandbag_queue)
    consumed: dict[str, int] = {}
    replacements: list[TentSkuPlanAction] = []

    if order_lines:
        return _build_row_bound_us_mainland_replacements(
            order_lines=order_lines,
            frame_queue=frame_queue,
            roller_queue=roller_queue,
            sandbag_queue=sandbag_queue,
            has_any_accessory=has_any_accessory,
            frame_priority=frame_priority,
        )

    main_lines = _expanded_main_product_lines(order_lines)
    tent_main_lines = [line for line in main_lines if _is_tent_order_line(line)]
    other_main_lines = [line for line in main_lines if not _is_tent_order_line(line)]
    if len(main_lines) > 1 and len(tent_main_lines) == 1:
        tent_line = tent_main_lines[0]
        selected: list[tuple[str, str, str | None]] = []
        if roller_queue and sandbag_queue and other_main_lines:
            selected.append((roller_queue.pop(0), "tent", tent_line.sku))
            selected.append((sandbag_queue.pop(0), "other_main", other_main_lines[0].sku))
        elif roller_queue:
            selected.append((roller_queue.pop(0), "tent", tent_line.sku))
        elif sandbag_queue:
            selected.append((sandbag_queue.pop(0), "tent", tent_line.sku))
        elif frame_priority and frame_queue:
            selected.append((frame_queue.pop(0), "tent", tent_line.sku))
        else:
            selected.append((INSTRUCTION_SKU, "tent", tent_line.sku))
        for sku, source_scope, source_sku in selected:
            replacements.append(
                TentSkuPlanAction(
                    action="replace_main",
                    sku=sku,
                    quantity=1,
                    source_scope=source_scope,
                    source_sku=source_sku,
                )
            )
        for item in replacements:
            if item.sku:
                consumed[item.sku] = consumed.get(item.sku, 0) + item.quantity
        return replacements, consumed, None

    def choose_replacement_sku() -> str:
        if roller_queue:
            return roller_queue.pop(0)
        if sandbag_queue:
            return sandbag_queue.pop(0)
        if has_any_accessory:
            return SANDBAG_SKU
        if frame_priority and frame_queue:
            return frame_queue.pop(0)
        return INSTRUCTION_SKU

    for group_multiplier, _group_components in groups_with_size:
        group_skus: list[str] = []
        for _ in range(max(1, group_multiplier)):
            group_skus.append(choose_replacement_sku())
        replacements.extend(_compress_replacement_skus(group_skus))

    for item in replacements:
        if item.sku:
            consumed[item.sku] = consumed.get(item.sku, 0) + item.quantity
    return replacements, consumed, None


def _build_row_bound_replacements(
    *,
    target_sku: str,
    replacement_quantity: int,
    order_lines: list[OrderFolderLine],
    asin: str | None,
    allow_partial_quantity: bool,
) -> tuple[list[TentSkuPlanAction], str | None]:
    """Bind a non-mainland/wall-only replacement to immutable source rows."""

    normalized_asin = normalize_asin(asin)
    if normalized_asin:
        candidates = [
            line
            for line in order_lines
            if normalize_asin(line.asin) == normalized_asin
        ]
        if not candidates:
            return (
                [],
                f"帐篷换货计划无法在原商品行中定位 ASIN {normalized_asin}，请人工处理。",
            )
    else:
        candidates = [line for line in order_lines if _is_tent_order_line(line)]
        if not candidates:
            return [], "帐篷换货计划没有可绑定的原商品行，请人工处理。"

    target_quantity = max(1, int(replacement_quantity or 0))
    selections_by_quantity: dict[int, list[tuple[int, ...]]] = {0: [()]}
    for index, line in enumerate(candidates):
        quantity = _order_line_quantity(line)
        previous = [
            (subtotal, list(selections))
            for subtotal, selections in selections_by_quantity.items()
        ]
        for subtotal, selections in previous:
            combined = subtotal + quantity
            if combined > target_quantity:
                continue
            bucket = selections_by_quantity.setdefault(combined, [])
            for selection in selections:
                candidate_selection = (*selection, index)
                if candidate_selection not in bucket:
                    bucket.append(candidate_selection)
                if len(bucket) >= 2:
                    break

    if allow_partial_quantity:
        usable_quantities = [value for value in selections_by_quantity if value > 0]
        selected_quantity = max(usable_quantities, default=0)
    else:
        selected_quantity = target_quantity
    selections = selections_by_quantity.get(selected_quantity, [])
    if not selections:
        qualifier = "完整覆盖" if not allow_partial_quantity else "整行承接"
        return (
            [],
            f"原商品行数量无法{qualifier}目标 SKU {target_sku} × {target_quantity}，请人工处理。",
        )
    if len(selections) != 1:
        return (
            [],
            f"目标 SKU {target_sku} 可匹配多组原商品行，无法唯一确定整行换货对象，请人工处理。",
        )

    selected_lines = [candidates[index] for index in selections[0]]
    source_ids = [str(line.order_item_id or "").strip() for line in selected_lines]
    if any(not value for value in source_ids):
        return [], "帐篷整行换货缺少原商品行 ID，请人工处理。"
    if len(source_ids) != len(set(source_ids)):
        return [], "帐篷整行换货的原商品行 ID 重复，请人工处理。"
    if any(not str(line.sku or "").strip() for line in selected_lines):
        return [], "帐篷整行换货缺少原商品行 SKU，请人工处理。"

    return (
        [
            TentSkuPlanAction(
                action="replace_main",
                sku=target_sku,
                quantity=_order_line_quantity(line),
                source_scope="tent",
                source_sku=line.sku,
                source_order_item_id=str(line.order_item_id).strip(),
                source_original_quantity=_order_line_quantity(line),
            )
            for line in selected_lines
        ],
        None,
    )


def _build_row_bound_us_mainland_replacements(
    *,
    order_lines: list[OrderFolderLine],
    frame_queue: list[str],
    roller_queue: list[str],
    sandbag_queue: list[str],
    has_any_accessory: bool,
    frame_priority: bool,
) -> tuple[list[TentSkuPlanAction], dict[str, int], str | None]:
    """按真实原商品行生成换货动作。

    一个线上商品行是不可拆分的换货单位。换货后的数量必须与原商品行
    quantity 完全一致；配件总需求中未被完整商品行消耗的部分由新增行承接。
    """

    main_lines = list(order_lines)
    tent_lines = [line for line in main_lines if _is_tent_order_line(line)]
    other_lines = [line for line in main_lines if not _is_tent_order_line(line)]
    if not tent_lines:
        return [], {}, "没有可绑定的帐篷原商品行，无法生成安全的换货计划。"

    consumed: dict[str, int] = {}
    replacements: list[TentSkuPlanAction] = []

    def take(queue: list[str], quantity: int) -> str | None:
        return _take_complete_row_sku(queue, quantity)

    def append(line: OrderFolderLine, sku: str, scope: str) -> None:
        quantity = _order_line_quantity(line)
        replacements.append(
            TentSkuPlanAction(
                action="replace_main",
                sku=sku,
                quantity=quantity,
                source_scope=scope,
                source_sku=line.sku,
                source_order_item_id=line.order_item_id,
                source_original_quantity=quantity,
            )
        )
        consumed[sku] = consumed.get(sku, 0) + quantity

    # 多主图订单中，拖轮包和沙袋同时存在时，维持现有业务语义：
    # 帐篷行优先换拖轮包，另一条带主图商品行可换沙袋。
    if len(main_lines) > 1 and len(tent_lines) == 1:
        tent_line = tent_lines[0]
        tent_quantity = _order_line_quantity(tent_line)
        roller_sku = take(roller_queue, tent_quantity)
        if roller_sku:
            append(tent_line, roller_sku, "tent")
            for other_line in other_lines:
                if not _is_independent_tent_option_line(other_line):
                    continue
                sandbag_sku = take(sandbag_queue, _order_line_quantity(other_line))
                if sandbag_sku:
                    append(other_line, sandbag_sku, "other_main")
                    break
            return replacements, consumed, None
        sandbag_sku = take(sandbag_queue, tent_quantity)
        if sandbag_sku:
            append(tent_line, sandbag_sku, "tent")
            return replacements, consumed, None
        frame_sku = (
            take(frame_queue, tent_quantity)
            if frame_priority and not has_any_accessory
            else None
        )
        if frame_sku:
            append(tent_line, frame_sku, "tent")
            return replacements, consumed, None
        append(tent_line, INSTRUCTION_SKU, "tent")
        return replacements, consumed, None

    for line in tent_lines:
        quantity = _order_line_quantity(line)
        replacement_sku: str | None = None
        if frame_priority and not has_any_accessory:
            replacement_sku = take(frame_queue, quantity)
        else:
            replacement_sku = take(roller_queue, quantity) or take(sandbag_queue, quantity)

        # 有配件但剩余数量不足以覆盖完整原商品行时，不能部分换货或超量换货。
        # 普通订单改用说明书承接该原商品行，未消耗的配件随后作为新增行加入。
        if replacement_sku is None:
            replacement_sku = INSTRUCTION_SKU
        append(line, replacement_sku, "tent")

    return replacements, consumed, None


def _take_complete_row_sku(queue: list[str], quantity: int) -> str | None:
    """仅当同一 SKU 足够覆盖整个原商品行时才从需求队列中扣除。"""

    required = max(1, int(quantity or 0))
    ordered_skus = list(dict.fromkeys(queue))
    for sku in ordered_skus:
        if queue.count(sku) < required:
            continue
        removed = 0
        remaining: list[str] = []
        for value in queue:
            if value == sku and removed < required:
                removed += 1
                continue
            remaining.append(value)
        queue[:] = remaining
        return sku
    return None


def _order_line_quantity(line: OrderFolderLine | None) -> int:
    return max(1, int(getattr(line, "quantity", 0) or 0))


def _expanded_main_product_lines(order_lines: list[OrderFolderLine] | None) -> list[OrderFolderLine]:
    expanded: list[OrderFolderLine] = []
    for line in order_lines or []:
        expanded.extend([line] * max(1, int(line.quantity or 0)))
    return expanded


def _is_tent_order_line(line: OrderFolderLine) -> bool:
    return line.product_type == "tent" or bool(get_tent_top_size(line.asin))


def _is_independent_tent_option_line(line: OrderFolderLine) -> bool:
    """Return whether a main-image order line is also a supported tent option family."""

    return line.product_type in {
        PRODUCT_TYPE_TABLECLOTHS,
        PRODUCT_TYPE_FEATHER_FLAGS,
    }


def _independent_group_product_type(components: list[str]) -> str | None:
    """Identify a no-size folder group that belongs to a standalone option product."""

    group_text = "+".join(str(component or "") for component in components)
    items = tent_accessory_component_to_sku_items(group_text)
    if not items:
        return None
    sku = str(items[0].sku or "").strip().lower()
    if sku.startswith("tablecloth-"):
        return PRODUCT_TYPE_TABLECLOTHS
    if sku.startswith(("feather-flag-", "teardrop-flag-")):
        return PRODUCT_TYPE_FEATHER_FLAGS
    return None


def _exclude_independent_main_product_groups(
    groups: list[tuple[int, list[str]]],
    order_lines: list[OrderFolderLine] | None,
) -> list[tuple[int, list[str]]]:
    """Keep standalone main-image products out of generic tent accessory parsing.

    A group with a tent size is always a tent configuration and remains eligible for
    normal tent-option mapping. Only a separate, no-size group is excluded, and only
    when the order actually contains a main-image line of the matching product type.
    """

    independent_types = {
        line.product_type
        for line in order_lines or []
        if _is_independent_tent_option_line(line)
    }
    if not independent_types:
        return groups

    output: list[tuple[int, list[str]]] = []
    for multiplier, components in groups:
        if detect_tent_size_key(components):
            output.append((multiplier, components))
            continue
        product_type = _independent_group_product_type(components)
        if product_type in independent_types:
            continue
        output.append((multiplier, components))
    return output


def _restore_replaced_independent_main_products(
    *,
    aggregated: dict[str, TentSkuPlanAction],
    replacements: list[TentSkuPlanAction],
    order_lines: list[OrderFolderLine] | None,
) -> str | None:
    """Add back the original SKU when an independent main-image row is repurposed."""

    indexed_lines: dict[str, list[OrderFolderLine]] = {}
    for line in order_lines or []:
        order_item_id = str(line.order_item_id or "").strip()
        if order_item_id:
            indexed_lines.setdefault(order_item_id, []).append(line)

    for replacement in replacements:
        if replacement.source_scope != "other_main":
            continue
        source_order_item_id = str(replacement.source_order_item_id or "").strip()
        if not source_order_item_id:
            return "独立带主图商品被换货，但缺少原商品行 ID，禁止猜测补回商品。"
        matches = indexed_lines.get(source_order_item_id, [])
        if len(matches) != 1:
            return "独立带主图商品被换货，但无法通过原商品行 ID 唯一定位商品，禁止猜测补回商品。"
        source_line = matches[0]
        if not _is_independent_tent_option_line(source_line):
            return "被换货的独立带主图商品不属于已支持的帐篷选项商品类型，请人工处理。"

        source_sku = str(source_line.sku or "").strip()
        replacement_source_sku = str(replacement.source_sku or "").strip()
        if not source_sku or source_sku.casefold() != replacement_source_sku.casefold():
            return "独立带主图商品被换货，但原 Seller SKU 缺失或与换货快照不一致，禁止自动补回。"

        source_quantity = _order_line_quantity(source_line)
        if (
            replacement.source_original_quantity != source_quantity
            or replacement.quantity != source_quantity
        ):
            return "独立带主图商品被换货，但原购买数量与换货数量不一致，禁止自动补回。"

        _add_aggregated_action(
            aggregated,
            source_sku,
            source_quantity,
            f"补回被换货的独立带主图商品：{source_sku}",
        )
    return None


def _build_final_main_product_items(
    order_lines: list[OrderFolderLine] | None,
    replacements: list[TentSkuPlanAction],
) -> list[TentSkuPlanAction]:
    main_lines = list(order_lines or [])
    main_unit_count = sum(max(1, int(line.quantity or 0)) for line in main_lines)
    if main_unit_count <= 1:
        return []

    if any(
        item.source_order_item_id or item.source_original_quantity is not None
        for item in replacements
    ):
        pending = list(replacements)
        output: list[TentSkuPlanAction] = []
        for line in main_lines:
            replacement = _pop_row_bound_replacement(pending, line)
            quantity = _order_line_quantity(line)
            final_sku = replacement.sku if replacement and replacement.sku else line.sku
            if not final_sku:
                continue
            output.append(
                TentSkuPlanAction(
                    action="main_product",
                    sku=final_sku,
                    quantity=quantity,
                    source_scope=replacement.source_scope if replacement else None,
                    source_sku=line.sku,
                    source_order_item_id=line.order_item_id,
                    source_original_quantity=quantity,
                )
            )
        # 理论上所有绑定动作都应被对应原商品行消费；保留未匹配动作可让后续
        # 校验明确暴露数据不一致，而不是静默丢失换货后的 SKU。
        for replacement in pending:
            if replacement.sku and replacement.quantity > 0:
                output.append(
                    TentSkuPlanAction(
                        action="main_product",
                        sku=replacement.sku,
                        quantity=replacement.quantity,
                        source_scope=replacement.source_scope,
                        source_sku=replacement.source_sku,
                        source_order_item_id=replacement.source_order_item_id,
                        source_original_quantity=replacement.source_original_quantity,
                    )
                )
        return output

    tent_replacements = _expanded_replacement_units(replacements, source_scope="tent")
    other_replacements = _expanded_replacement_units(replacements, source_scope="other_main")
    unscoped_replacements = _expanded_replacement_units(replacements, source_scope=None)
    output: list[TentSkuPlanAction] = []
    for line in main_lines:
        line_items: list[TentSkuPlanAction] = []
        for _ in range(max(1, int(line.quantity or 0))):
            replacement = None
            if _is_tent_order_line(line):
                replacement = _pop_replacement_for_source(tent_replacements, line.sku)
                if replacement is None and unscoped_replacements:
                    replacement = unscoped_replacements.pop(0)
            else:
                replacement = _pop_replacement_for_source(other_replacements, line.sku)

            final_sku = replacement.sku if replacement else line.sku
            if not final_sku:
                continue
            if line_items and line_items[-1].sku == final_sku:
                line_items[-1].quantity += 1
            else:
                line_items.append(TentSkuPlanAction(action="main_product", sku=final_sku, quantity=1))
        output.extend(line_items)
    for replacement in [*tent_replacements, *other_replacements, *unscoped_replacements]:
        existing = next((item for item in output if item.sku == replacement.sku), None)
        if existing:
            existing.quantity += replacement.quantity
        elif replacement.sku:
            output.append(
                TentSkuPlanAction(
                    action="main_product",
                    sku=replacement.sku,
                    quantity=replacement.quantity,
                )
            )
    return output


def _pop_row_bound_replacement(
    replacements: list[TentSkuPlanAction],
    line: OrderFolderLine,
) -> TentSkuPlanAction | None:
    """按 OrderItemId 优先、SKU/商品范围后备，取出某原商品行的换货动作。"""

    order_item_id = str(line.order_item_id or "").strip()
    if order_item_id:
        for index, item in enumerate(replacements):
            if str(item.source_order_item_id or "").strip() == order_item_id:
                return replacements.pop(index)

    expected_scope = "tent" if _is_tent_order_line(line) else "other_main"
    normalized_sku = str(line.sku or "").strip().lower()
    for index, item in enumerate(replacements):
        if item.source_scope != expected_scope:
            continue
        source_sku = str(item.source_sku or "").strip().lower()
        if source_sku and source_sku == normalized_sku:
            return replacements.pop(index)
    for index, item in enumerate(replacements):
        if item.source_scope == expected_scope and not item.source_sku:
            return replacements.pop(index)
    return None


def _expanded_replacement_units(
    replacements: list[TentSkuPlanAction],
    *,
    source_scope: str | None,
) -> list[TentSkuPlanAction]:
    units: list[TentSkuPlanAction] = []
    for item in replacements:
        if item.source_scope != source_scope or not item.sku or item.quantity <= 0:
            continue
        for _ in range(item.quantity):
            units.append(
                TentSkuPlanAction(
                    action=item.action,
                    sku=item.sku,
                    quantity=1,
                    reason=item.reason,
                    source_scope=item.source_scope,
                    source_sku=item.source_sku,
                    source_order_item_id=item.source_order_item_id,
                    source_original_quantity=item.source_original_quantity,
                )
            )
    return units


def _pop_replacement_for_source(
    replacements: list[TentSkuPlanAction],
    source_sku: str | None,
) -> TentSkuPlanAction | None:
    normalized_source = str(source_sku or "").strip().lower()
    for index, item in enumerate(replacements):
        if str(item.source_sku or "").strip().lower() == normalized_source and normalized_source:
            return replacements.pop(index)
    for index, item in enumerate(replacements):
        if not item.source_sku:
            return replacements.pop(index)
    return None


def _group_sku_plan_actions(
    group_multiplier: int,
    group_components: list[str],
    *,
    allow_large_frame_rail: bool,
) -> list[TentSkuPlanAction]:
    size_key = detect_tent_size_key(group_components)
    if not size_key:
        return []
    rail_required = _group_requires_frame_rail(size_key, group_components)
    actions: list[TentSkuPlanAction] = []
    for component in group_components:
        for item in component_to_sku_items(
            size_key,
            component,
            rail_required=rail_required,
            allow_large_frame_rail=allow_large_frame_rail,
        ):
            actions.append(
                TentSkuPlanAction(
                    action="add_product",
                    sku=item.sku,
                    quantity=item.quantity * group_multiplier,
                    reason=item.reason,
                )
            )
    return actions


def _expanded_sku_queue(items: list[TentSkuPlanAction], predicate) -> list[str]:
    queue: list[str] = []
    for item in items:
        sku = item.sku or ""
        if not predicate(sku):
            continue
        queue.extend([sku] * max(1, item.quantity))
    return queue


def _compress_replacement_skus(skus: list[str]) -> list[TentSkuPlanAction]:
    actions: list[TentSkuPlanAction] = []
    for sku in skus:
        if actions and actions[-1].sku == sku:
            actions[-1].quantity += 1
            continue
        actions.append(TentSkuPlanAction(action="replace_main", sku=sku, quantity=1))
    return actions


def _sync_legacy_replacement_fields(plan: TentSkuAdjustmentPlan) -> None:
    if not plan.replace_main_items:
        plan.replace_main_sku = None
        plan.replace_main_quantity = 1
        return
    first = plan.replace_main_items[0]
    plan.replace_main_sku = first.sku
    same_sku = all(item.sku == first.sku for item in plan.replace_main_items)
    plan.replace_main_quantity = (
        sum(item.quantity for item in plan.replace_main_items)
        if same_sku
        else first.quantity
    )


def _consume_replaced_sku_quantity(
    quantity: int,
    *,
    sku: str,
    consumed_sku_quantities: dict[str, int],
) -> int:
    consumed = consumed_sku_quantities.get(sku, 0)
    if consumed <= 0:
        return quantity
    used = min(quantity, consumed)
    remaining_consumed = consumed - used
    if remaining_consumed:
        consumed_sku_quantities[sku] = remaining_consumed
    else:
        consumed_sku_quantities.pop(sku, None)
    return quantity - used


def _is_frame_sku(sku: str) -> bool:
    return "-FRAME-" in str(sku or "").upper()


def _is_roller_sku(sku: str) -> bool:
    return str(sku or "").upper().startswith("TENT-ROLLER-BAG-")


def _is_frame_priority_destination(destination: DestinationRegion) -> bool:
    prefix = _postal_prefix_int(destination.postal_code)
    if prefix is None or destination.category != "us_mainland":
        return False
    if 10 <= prefix <= 199:
        return True
    return destination.state == "CA" and 900 <= prefix <= 961


def _postal_prefix_int(postal_code: str | None) -> int | None:
    match = re.match(r"\D*(\d{3})", str(postal_code or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _replacement_quantity_for_sku(
    replacement_sku: str | None,
    tent_groups: list[tuple[int, list[str]]],
    *,
    allow_large_frame_rail: bool = False,
) -> int:
    """推导主商品换货后应保留的商品总数量。"""

    if not replacement_sku:
        return 1
    matched_quantity = 0
    for group_multiplier, group_components in tent_groups:
        size_key = detect_tent_size_key(group_components) or "3x3m"
        rail_required = _group_requires_frame_rail(size_key, group_components)
        for component in group_components:
            for item in component_to_sku_items(
                size_key,
                component,
                rail_required=rail_required,
                allow_large_frame_rail=allow_large_frame_rail,
            ):
                if item.sku == replacement_sku:
                    matched_quantity += item.quantity * group_multiplier
    if matched_quantity:
        return matched_quantity
    return max(1, sum(group_multiplier for group_multiplier, _ in tent_groups))


def _strip_outer_components(components: list[str]) -> list[str]:
    """剥离文件夹组件中的外层数量包装。"""
    values = [str(component).strip() for component in components if str(component).strip()]
    if len(values) >= 2:
        return values[1:-1]
    return values


def _extract_tent_groups(folder_components: list[str]) -> list[tuple[int, list[str]]]:
    """从输入内容中提取帐篷分组。"""
    product_components = _strip_outer_components(folder_components)
    if not product_components:
        return []

    groups: list[tuple[int, list[str]]] = []
    current: list[str] = []
    for component in product_components:
        multi_set = _parse_multi_set_component(component)
        if multi_set:
            if current:
                groups.append((1, current))
                current = []
            groups.append(multi_set)
            continue
        if current and ("帐篷顶" in component or "帐篷的" in component):
            groups.append((1, current))
            current = [component]
        else:
            current.append(component)
    if current:
        groups.append((1, current))
    return [_normalize_legacy_explicit_main_quantity(group) for group in groups]


def _normalize_legacy_explicit_main_quantity(
    group: tuple[int, list[str]],
) -> tuple[int, list[str]]:
    """把旧格式主商品数量前缀转换成组倍数，避免下游重复相乘。

    新文件夹格式会把多套帐篷写成 ``N套（单套组件...）``，解析时天然得到
    组倍数。旧数据可能写成 ``N个3x6m帐篷顶+拖轮包``，其中 N 同样表示
    整套配置数量；若继续把 N 留在帐篷顶组件里，再叠加订单行数量，就会把
    主商品算成 N×N，同时配件数量也无法正确随整套重复。

    这里只识别“帐篷顶”或独立墙体的“帐篷的...”主商品片段。普通套餐内
    ``2半高侧墙``、``2个拖轮包``等仍是单套内部数量，必须继续与组倍数相乘。
    """

    group_multiplier, components = group
    if group_multiplier != 1:
        return group

    normalized_components = list(components)
    for index, component in enumerate(normalized_components):
        text = str(component or "").strip()
        match = re.match(r"^(\d+)\s*(?:个|套)\s*(.+)$", text)
        if not match:
            continue
        main_component = match.group(2).strip()
        if "帐篷顶" not in main_component and "帐篷的" not in main_component:
            continue
        quantity = max(1, int(match.group(1)))
        normalized_components[index] = main_component
        return quantity, normalized_components
    return group


def _apply_unambiguous_order_line_quantity(
    groups: list[tuple[int, list[str]]],
    order_lines: list[OrderFolderLine] | None,
) -> list[tuple[int, list[str]]]:
    """用唯一帐篷商品行数量补齐未显式写出套数的单一配置。

    正常文件夹生成器会把同一商品行的多件帐篷写成 ``N套（...）``，
    此时直接保留已经解析出的倍数，避免重复相乘。该兜底只处理唯一帐篷
    商品行、唯一帐篷配置且文件夹倍数仍为 1 的无歧义情况。
    """

    tent_lines = [line for line in order_lines or [] if _is_tent_order_line(line)]
    sized_group_indexes = [
        index
        for index, (_, components) in enumerate(groups)
        if detect_tent_size_key(components)
    ]
    if len(tent_lines) != 1 or len(sized_group_indexes) != 1:
        return groups

    group_index = sized_group_indexes[0]
    group_multiplier, group_components = groups[group_index]
    line_quantity = _order_line_quantity(tent_lines[0])
    if group_multiplier != 1 or line_quantity <= 1:
        return groups

    normalized = list(groups)
    normalized[group_index] = (line_quantity, group_components)
    return normalized


def _looks_like_tent_component(component: str) -> bool:
    text = str(component or "")
    return any(marker in text for marker in ("帐篷", "拖轮包", "沙袋", "支架", "围墙", "侧墙", "面料"))


def _parse_multi_set_component(component: str) -> tuple[int, list[str]] | None:
    """解析带数量和括号包装的帐篷配置组件。"""
    text = str(component or "").strip()
    match = re.match(r"^\s*(\d+)\s*(?:个|套)([（(])", text)
    if not match:
        return None
    quantity = max(1, int(match.group(1)))
    open_paren = match.group(2)
    close_paren = "）" if open_paren == "（" else ")"
    text = re.sub(r"[（(]\d+个不同画面[）)]\s*$", "", text)
    start = text.find(open_paren)
    end = text.rfind(close_paren)
    if start < 0 or end <= start:
        return None
    inner = text[start + 1 : end]
    parts = [part.strip() for part in inner.split("+") if part.strip()]
    return quantity, parts


def _first_size_key(groups: list[tuple[int, list[str]]]) -> str | None:
    """处理第一个尺寸键相关逻辑，并返回后续流程所需结果。"""
    for _, components in groups:
        size_key = detect_tent_size_key(components)
        if size_key:
            return size_key
    return None


def _first_matching_sku(groups: list[tuple[int, list[str]]], marker: str) -> str | None:
    """处理第一个匹配SKU相关逻辑，并返回后续流程所需结果。"""
    for _, components in groups:
        if not any(marker in component for component in components):
            continue
        size_key = detect_tent_size_key(components)
        if size_key and marker == "拖轮包":
            return roller_bag_sku(size_key)
    return None


def _wall_only_replacement_sku(wall_only_kind: str | None, groups: list[tuple[int, list[str]]]) -> str | None:
    """为单独侧墙 ASIN 选择需要替换的 SKU。"""
    if not wall_only_kind:
        return None
    for _, components in groups:
        item = _wall_only_item_for_components(wall_only_kind, components)
        if item:
            return item.sku
    return None


def _wall_only_item_for_components(wall_only_kind: str | None, components: list[str]) -> TentSkuPlanAction | None:
    """根据侧墙组件生成单独侧墙的 SKU 动作。"""
    if not wall_only_kind:
        return None
    # 独立墙体 ASIN 目前只对应 3x3m/10ft 墙体；半围文件夹片段不一定带尺寸。
    size_key = detect_tent_size_key(components) or "3x3m"
    for component in components:
        item = wall_sku_for_component(size_key, component)
        if item and _wall_sku_matches_kind(item.sku, wall_only_kind):
            return TentSkuPlanAction(action="add_product", sku=item.sku, quantity=item.quantity, reason=item.reason)
    return None


def _wall_sku_matches_kind(sku: str, wall_only_kind: str) -> bool:
    """判断侧墙 SKU 是否匹配全高或半高类型。"""
    if wall_only_kind == "full_wall":
        return "-Full-Wall" in sku
    if wall_only_kind == "half_wall":
        return "-Half-Wall" in sku
    return False


def _build_instruction_remark_for_order(
    *,
    shipping_deadline_text: str | None,
    payment_time_text: str | None,
    logistics_text: str | None,
    asin: str | None = None,
    processed_at: datetime | date | None = None,
) -> str:
    if _is_expedited_logistics(logistics_text) or is_default_expedited_tent_asin(asin):
        return build_expedited_instruction_customer_remark(
            payment_time_text,
            processed_at=processed_at,
        )
    return build_latest_instruction_customer_remark(
        shipping_deadline_text,
        payment_time_text,
        processed_at=processed_at,
    )


def _is_expedited_logistics(logistics_text: str | None) -> bool:
    text = str(logistics_text or "").strip().lower()
    return "expedited" in text or "加急" in text


def _group_requires_frame_rail(size_key: str, components: list[str]) -> bool:
    """判断帐篷配置组是否带半高墙横杆需求。"""
    for component in components:
        text = re.sub(r"\s+", "", str(component or ""))
        if "横杆" in text and ("半高侧墙" in text or "半围" in text):
            return True
    return False


def _add_aggregated_action(aggregated: dict[str, TentSkuPlanAction], sku: str, quantity: int, reason: str) -> None:
    """把相同 SKU 的计划动作聚合为一条记录。"""
    old = aggregated.get(sku)
    if old:
        old.quantity += quantity
        if reason and reason not in old.reason:
            old.reason = f"{old.reason}；{reason}" if old.reason else reason
        return
    aggregated[sku] = TentSkuPlanAction(action="add_product", sku=sku, quantity=quantity, reason=reason)


def format_tent_sku_plan_for_cmd(plan: TentSkuAdjustmentPlan) -> str:
    """把 SKU 调整计划格式化为 CMD 可读文本。"""

    lines = [
        "",
        "[帐篷 SKU 调整计划]",
        f"平台单号：{plan.platform_order_no}",
        f"系统单号：{plan.system_order_no}",
        f"地区：{plan.destination.category}（国家={plan.destination.country or '-'}，州={plan.destination.state or '-'}）",
    ]
    if plan.manual_required:
        lines.append(f"需要人工处理：{plan.manual_reason or '-'}")
        return "\n".join(lines)
    replace_text = (
        f"{plan.replace_main_sku} x {plan.replace_main_quantity}"
        if plan.replace_main_sku
        else "-"
    )
    lines.append(f"主商品换货为：{replace_text}")
    if plan.add_items:
        lines.append("需要添加商品：")
        for item in plan.add_items:
            lines.append(f"  - {item.sku} x {item.quantity}（{item.reason or '-'}）")
    else:
        lines.append("需要添加商品：无")
    for warning in plan.warnings:
        lines.append(f"警告：{warning}")
    return "\n".join(lines)
