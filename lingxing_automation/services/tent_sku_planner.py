from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..products.tents import get_wall_only_asin_kind, is_default_expedited_tent_asin
from .china_workday import ChinaWorkdayError, build_expedited_instruction_customer_remark, build_instruction_customer_remark
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
    category: str = "unknown"
    warning: str | None = None


@dataclass
class TentSkuPlanAction:
    """SKU 调整的一个动作。"""

    action: str
    sku: str | None = None
    quantity: int = 1
    reason: str = ""


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
    add_items: list[TentSkuPlanAction] = field(default_factory=list)
    customer_remark: str | None = None
    manual_required: bool = False
    manual_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_log_dict(self) -> dict[str, Any]:
        """将当前对象转换为日志字典，便于批量流程记录和排查。"""
        return {
            "sku_adjustment_destination": self.destination.__dict__,
            "sku_adjustment_replace_main_sku": self.replace_main_sku,
            "sku_adjustment_replace_main_quantity": self.replace_main_quantity,
            "sku_adjustment_replace_main_items": [item.__dict__ for item in self.replace_main_items],
            "sku_adjustment_add_items": [item.__dict__ for item in self.add_items],
            "sku_adjustment_customer_remark": self.customer_remark,
            "sku_adjustment_manual_required": self.manual_required,
            "sku_adjustment_manual_reason": self.manual_reason,
            "sku_adjustment_warnings": self.warnings,
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
    if "canada" in compact or "加拿大" in raw:
        region.country = "CA"
        region.category = "canada"
        return region
    if "united states" in compact or "usa" in compact or "美国" in raw:
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
        parsed = _normalize_postal_code(match.group(1))
        if parsed:
            return parsed
    match = re.search(r"\b(\d{5})(?:-\d{4})?\b", normalized)
    return match.group(1) if match else None


def _normalize_postal_code(value: str | None) -> str | None:
    text = str(value or "").strip(" ,;:-:：")
    if not text or text == "-":
        return None
    us_zip = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
    if us_zip:
        return us_zip.group(1)
    compact = re.sub(r"[^A-Za-z0-9]", "", text)
    return compact or None


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

    tent_groups = _extract_tent_groups(folder_components)
    if not tent_groups:
        plan.manual_required = True
        plan.manual_reason = "未从文件夹组件中识别到帐篷配置，请人工添加 SKU。"
        return plan

    aggregated: dict[str, TentSkuPlanAction] = {}
    replaced_sku_to_skip: str | None = None
    wall_only_kind = get_wall_only_asin_kind(asin)
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
    replaced_sku_to_skip = plan.replace_main_sku
    if plan.replace_main_sku:
        plan.replace_main_items = [
            TentSkuPlanAction(
                action="replace_main",
                sku=plan.replace_main_sku,
                quantity=plan.replace_main_quantity,
            )
        ]
    skip_used = False
    for group_multiplier, group_components in tent_groups:
        size_key = detect_tent_size_key(group_components)
        if not size_key and wall_only_kind:
            wall_item = _wall_only_item_for_components(wall_only_kind, group_components)
            sku_items = [wall_item] if wall_item else []
            for component in group_components:
                sku_items.extend(tent_accessory_component_to_sku_items(component))
            for item in sku_items:
                quantity = item.quantity * group_multiplier
                quantity, skip_used = _skip_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    replaced_sku_to_skip=replaced_sku_to_skip,
                    skip_used=skip_used,
                )
                if quantity <= 0:
                    continue
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)
            continue
        if not size_key:
            group_text = "+".join(group_components)
            accessory_items = tent_accessory_component_to_sku_items(group_text)
            if not accessory_items:
                plan.warnings.append(f"未识别该帐篷配件 SKU，已跳过 SKU 生成：{group_text}")
                continue
            for item in accessory_items:
                quantity = item.quantity * group_multiplier
                quantity, skip_used = _skip_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    replaced_sku_to_skip=replaced_sku_to_skip,
                    skip_used=skip_used,
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
                quantity, skip_used = _skip_replaced_sku_quantity(
                    quantity,
                    sku=item.sku,
                    replaced_sku_to_skip=replaced_sku_to_skip,
                    skip_used=skip_used,
                )
                if quantity <= 0:
                    continue
                _add_aggregated_action(aggregated, item.sku, quantity, item.reason)

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
) -> TentSkuAdjustmentPlan:
    replacement_items, consumed_sku_quantities = _build_us_mainland_replacements(tent_groups, plan.destination)
    plan.replace_main_items = replacement_items
    _sync_legacy_replacement_fields(plan)
    if any(item.sku == INSTRUCTION_SKU for item in replacement_items):
        try:
            plan.customer_remark = _build_instruction_remark_for_order(
                shipping_deadline_text=shipping_deadline_text,
                payment_time_text=payment_time_text,
                logistics_text=logistics_text,
                asin=asin,
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
            if not accessory_items:
                plan.warnings.append(f"未识别该帐篷配件 SKU，已跳过 SKU 生成：{group_text}")
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

    plan.add_items = list(aggregated.values())
    return plan


def _build_us_mainland_replacements(
    tent_groups: list[tuple[int, list[str]]],
    destination: DestinationRegion,
) -> tuple[list[TentSkuPlanAction], dict[str, int]]:
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

    def choose_replacement_sku() -> str:
        if frame_priority and frame_queue:
            return frame_queue.pop(0)
        if roller_queue:
            return roller_queue.pop(0)
        if sandbag_queue:
            return sandbag_queue.pop(0)
        if has_any_accessory:
            return SANDBAG_SKU
        return INSTRUCTION_SKU

    for group_multiplier, _group_components in groups_with_size:
        group_skus = [choose_replacement_sku() for _ in range(max(1, group_multiplier))]
        replacements.extend(_compress_replacement_skus(group_skus))

    for item in replacements:
        if item.sku:
            consumed[item.sku] = consumed.get(item.sku, 0) + item.quantity
    return replacements, consumed


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
    """推导主商品换货后应保留的商品数量。"""

    if not replacement_sku:
        return 1
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
                    return max(1, item.quantity * group_multiplier)
    return max(1, tent_groups[0][0]) if tent_groups else 1


def _skip_replaced_sku_quantity(
    quantity: int,
    *,
    sku: str,
    replaced_sku_to_skip: str | None,
    skip_used: bool,
) -> tuple[int, bool]:
    """扣掉将由换货主商品行显式设置的数量，避免再次添加同 SKU。"""

    if sku != replaced_sku_to_skip or skip_used:
        return quantity, skip_used
    return 0, True


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
    return groups


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
) -> str:
    if _is_expedited_logistics(logistics_text) or is_default_expedited_tent_asin(asin):
        return build_expedited_instruction_customer_remark(payment_time_text)
    return build_instruction_customer_remark(shipping_deadline_text)


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
    if plan.customer_remark:
        lines.append(f"客服备注：{plan.customer_remark}")
    for warning in plan.warnings:
        lines.append(f"警告：{warning}")
    return "\n".join(lines)
