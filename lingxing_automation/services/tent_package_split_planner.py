from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tent_sku_planner import DestinationRegion, TentSkuAdjustmentPlan
from .tent_sku_rules import INSTRUCTION_SKU, SANDBAG_SKU, TENT_SIZE_RULES


@dataclass(frozen=True)
class TentPackageSplitItem:
    """描述拆分包裹中需要移动的一条 SKU 和数量。"""

    sku: str
    quantity: int
    reason: str = ""


@dataclass(frozen=True)
class TentPackageSplitPackage:
    """描述一个需要主动拆出的新包裹。"""

    package_key: str
    title: str
    items: list[TentPackageSplitItem] = field(default_factory=list)


@dataclass
class TentPackageSplitPlan:
    """描述帐篷订单拆分包裹阶段的业务计划。"""

    platform_order_no: str
    system_order_no: str
    destination: DestinationRegion
    status: str
    required: bool
    packages_to_split: list[TentPackageSplitPackage] = field(default_factory=list)
    reason: str = ""
    manual_required: bool = False
    manual_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_log_dict(self) -> dict[str, Any]:
        """转换为批量日志字段，便于排查拆包阶段。"""

        return {
            "package_split_required": self.required,
            "package_split_plan_status": self.status,
            "package_split_plan_reason": self.reason,
            "package_split_manual_required": self.manual_required,
            "package_split_manual_reason": self.manual_reason,
            "package_split_warnings": self.warnings,
            "package_split_packages": [
                {
                    "package_key": package.package_key,
                    "title": package.title,
                    "items": [item.__dict__ for item in package.items],
                }
                for package in self.packages_to_split
            ],
        }


def build_tent_package_split_plan(sku_plan: TentSkuAdjustmentPlan) -> TentPackageSplitPlan:
    """根据帐篷 SKU 调整计划生成拆分包裹计划。"""

    base = {
        "platform_order_no": sku_plan.platform_order_no,
        "system_order_no": sku_plan.system_order_no,
        "destination": sku_plan.destination,
    }
    if sku_plan.destination.category in {"canada", "us_non_mainland"}:
        return TentPackageSplitPlan(
            **base,
            status="not_required",
            required=False,
            reason="加拿大或美国非本土订单无需拆分包裹。",
        )
    if sku_plan.destination.category != "us_mainland":
        return TentPackageSplitPlan(
            **base,
            status="manual_required",
            required=False,
            manual_required=True,
            manual_reason=sku_plan.destination.warning or "未识别美国本土地区，拆包阶段需要人工确认。",
            reason="地区无法自动判断。",
        )
    if sku_plan.manual_required:
        return TentPackageSplitPlan(
            **base,
            status="manual_required",
            required=False,
            manual_required=True,
            manual_reason=sku_plan.manual_reason or "SKU 阶段需要人工处理，暂不自动拆分包裹。",
            reason="SKU 计划未自动完成。",
        )

    if sku_plan.main_product_items:
        return _build_multi_main_product_split_plan(sku_plan, base)

    final_items = _final_sku_items_from_sku_plan(sku_plan)
    grouped = _group_final_items(final_items)
    active_group_count = sum(1 for items in grouped.values() if items)
    if active_group_count <= 1:
        return TentPackageSplitPlan(
            **base,
            status="not_required",
            required=False,
            reason="只有一个有效包裹组，无需拆分。",
        )

    packages_to_split = _packages_to_split_from_groups(grouped)
    if not packages_to_split:
        return TentPackageSplitPlan(
            **base,
            status="not_required",
            required=False,
            reason="没有需要主动拆出的配件包或支架包。",
        )

    return TentPackageSplitPlan(
        **base,
        status="ready",
        required=True,
        packages_to_split=packages_to_split,
        reason="美国本土帐篷订单需要拆分配件包和/或支架包。",
    )


def _build_multi_main_product_split_plan(
    sku_plan: TentSkuAdjustmentPlan,
    base: dict[str, Any],
) -> TentPackageSplitPlan:
    main_items = [
        TentPackageSplitItem(sku=item.sku or "", quantity=item.quantity, reason="带主图商品行")
        for item in sku_plan.main_product_items
        if item.sku and item.quantity > 0
    ]
    final_items = _final_sku_items_from_sku_plan(sku_plan)
    remaining = _subtract_items(final_items, main_items)
    if not remaining:
        return TentPackageSplitPlan(
            **base,
            status="not_required",
            required=False,
            reason="所有带主图商品行已经位于同一包裹，且没有其它 SKU 需要拆分。",
        )
    packages = [
        TentPackageSplitPackage(
            package_key="main-products",
            title="主图商品包",
            items=main_items,
        )
    ]
    packages.extend(_packages_to_split_from_groups(_group_final_items(remaining)))
    return TentPackageSplitPlan(
        **base,
        status="ready",
        required=True,
        packages_to_split=packages,
        reason="多主图帐篷订单需要把所有带主图商品行拆入同一个包裹。",
    )


def _subtract_items(
    items: list[TentPackageSplitItem],
    consumed_items: list[TentPackageSplitItem],
) -> list[TentPackageSplitItem]:
    consumed: dict[str, int] = {}
    for item in consumed_items:
        key = _normalize_sku(item.sku)
        consumed[key] = consumed.get(key, 0) + item.quantity
    output: list[TentPackageSplitItem] = []
    for item in items:
        key = _normalize_sku(item.sku)
        used = min(item.quantity, consumed.get(key, 0))
        consumed[key] = max(0, consumed.get(key, 0) - used)
        if item.quantity > used:
            output.append(
                TentPackageSplitItem(
                    sku=item.sku,
                    quantity=item.quantity - used,
                    reason=item.reason,
                )
            )
    return output


def _final_sku_items_from_sku_plan(sku_plan: TentSkuAdjustmentPlan) -> list[TentPackageSplitItem]:
    """从 SKU 调整计划还原调整完成后的订单 SKU 数量。"""

    aggregated: dict[str, TentPackageSplitItem] = {}
    if sku_plan.replace_main_items:
        for item in sku_plan.replace_main_items:
            if item.sku and item.quantity > 0:
                _add_item(aggregated, item.sku, item.quantity, "主商品换货后的 SKU")
        for item in sku_plan.add_items:
            if not item.sku or item.quantity <= 0:
                continue
            _add_item(aggregated, item.sku, item.quantity, item.reason)
        return list(aggregated.values())
    if sku_plan.replace_main_sku:
        _add_item(aggregated, sku_plan.replace_main_sku, sku_plan.replace_main_quantity, "主商品换货后的 SKU")
    for item in sku_plan.add_items:
        if not item.sku or item.quantity <= 0:
            continue
        _add_item(aggregated, item.sku, item.quantity, item.reason)
    return list(aggregated.values())


def _add_item(aggregated: dict[str, TentPackageSplitItem], sku: str, quantity: int, reason: str) -> None:
    """把相同 SKU 的拆包数量合并成一条记录。"""

    old = aggregated.get(sku)
    if old:
        merged_reason = old.reason
        if reason and reason not in merged_reason:
            merged_reason = f"{merged_reason}；{reason}" if merged_reason else reason
        aggregated[sku] = TentPackageSplitItem(sku=sku, quantity=old.quantity + quantity, reason=merged_reason)
        return
    aggregated[sku] = TentPackageSplitItem(sku=sku, quantity=max(1, int(quantity)), reason=reason)


def _group_final_items(items: list[TentPackageSplitItem]) -> dict[str, list[TentPackageSplitItem]]:
    """把最终 SKU 按配件、支架、布料三类分组。"""

    grouped: dict[str, list[TentPackageSplitItem]] = {"accessory": [], "frame": [], "fabric": []}
    for item in items:
        if is_package_accessory_sku(item.sku):
            grouped["accessory"].append(item)
        elif is_frame_sku(item.sku):
            grouped["frame"].append(item)
        else:
            grouped["fabric"].append(item)
    return grouped


def _packages_to_split_from_groups(grouped: dict[str, list[TentPackageSplitItem]]) -> list[TentPackageSplitPackage]:
    """根据分组决定需要主动拆出的新包裹。"""

    packages: list[TentPackageSplitPackage] = []
    accessory_items = grouped.get("accessory") or []
    frame_items = grouped.get("frame") or []
    fabric_items = grouped.get("fabric") or []
    if accessory_items:
        packages.append(TentPackageSplitPackage(package_key="accessory", title="配件包", items=accessory_items))
    if frame_items and fabric_items:
        packages.extend(_single_frame_packages(frame_items))
        frame_items = []
    if frame_items and fabric_items:
        packages.append(TentPackageSplitPackage(package_key="frame", title="支架包", items=frame_items))
    if not fabric_items and len(packages) == 2:
        # 如果没有布料留在原包裹，保留支架在原包裹，避免把原包裹拆空。
        packages = packages[:1]
    return packages


def _single_frame_packages(frame_items: list[TentPackageSplitItem]) -> list[TentPackageSplitPackage]:
    units: list[TentPackageSplitItem] = []
    for item in frame_items:
        for _ in range(max(1, item.quantity)):
            units.append(TentPackageSplitItem(sku=item.sku, quantity=1, reason=item.reason))
    if len(units) <= 1:
        return [TentPackageSplitPackage(package_key="frame", title="支架包", items=units)]
    return [
        TentPackageSplitPackage(
            package_key=f"frame-{index}",
            title=f"支架包{index}",
            items=[item],
        )
        for index, item in enumerate(units, start=1)
    ]


def is_package_accessory_sku(sku: str | None) -> bool:
    """判断 SKU 是否属于拖轮包、沙袋或说明书配件包。"""

    text = _normalize_sku(sku)
    if not text:
        return False
    roller_skus = {_normalize_sku(rule["roller"]) for rule in TENT_SIZE_RULES.values()}
    if text in roller_skus or re.search(r"ROLL(?:ER)?-?BAG", text):
        return True
    return text == _normalize_sku(INSTRUCTION_SKU) or text == _normalize_sku(SANDBAG_SKU) or "SANDBAG" in text


def is_frame_sku(sku: str | None) -> bool:
    """判断 SKU 是否属于帐篷支架包。"""

    text = _normalize_sku(sku)
    return bool(text and "FRAME" in text)


def _normalize_sku(sku: str | None) -> str:
    """规范化 SKU 文本，便于拆包分类比较。"""

    return re.sub(r"\s+", "", str(sku or "")).upper()
