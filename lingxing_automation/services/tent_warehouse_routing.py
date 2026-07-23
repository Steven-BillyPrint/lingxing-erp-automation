from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from .tent_package_split_planner import is_frame_sku
from .tent_sku_planner import (
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
    normalize_us_postal_code,
)
from .tent_sku_rules import INSTRUCTION_SKU, SANDBAG_SKU, TENT_SIZE_RULES


RULE_SCHEMA_VERSION = 1
DEFAULT_RULE_PATH = Path(__file__).resolve().parents[2] / "rules" / "tent_warehouse_routing.v1.json"
FEDEX_GROUND_ECONOMY = "FedEx Ground Economy"
UNRESTRICTED_CHANNEL_COMPARISON = "不限渠道比价"
UNRESTRICTED_CHANNEL_COMPARISON_BY_WAREHOUSE = {
    "CA": UNRESTRICTED_CHANNEL_COMPARISON,
    # NJ uses this exact name in Lingxing's warehouse logistics master data.
    "NJ": "不限比价渠道",
}


class TentWarehouseRuleError(ValueError):
    """机器分仓规则或包裹映射无法被安全解释。"""


@dataclass(frozen=True)
class ZipRoutingRule:
    start_zip: str
    end_zip: str
    ca_zone: int | None
    nj_zone: int | None
    action: str
    reason: str


@dataclass(frozen=True)
class TentWarehouseRules:
    schema_version: int
    source_workbook: str
    source_sha256: str
    tie_breaker: str
    warehouses: dict[str, dict[str, str]]
    rules: tuple[ZipRoutingRule, ...]


@dataclass(frozen=True)
class TentRoutingItem:
    sku: str
    quantity: int = 1
    item_id: str | None = None
    order_item_no: str | None = None

    @property
    def identifiers(self) -> frozenset[str]:
        return frozenset(
            value
            for value in (
                _normalized_identifier(self.item_id),
                _normalized_identifier(self.order_item_no),
            )
            if value
        )


@dataclass(frozen=True)
class TentRoutingPackage:
    system_order_no: str
    items: tuple[TentRoutingItem, ...]


@dataclass(frozen=True)
class TentWarehouseRoutingDecision:
    system_order_no: str
    status: str
    skus: tuple[str, ...]
    sku_classes: tuple[str, ...]
    is_main_product_package: bool = False
    target_warehouse_code: str | None = None
    target_warehouse_name: str | None = None
    target_channel_name: str | None = None
    channel_mode: str | None = None
    reason: str = ""

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "system_order_no": self.system_order_no,
            "status": self.status,
            "skus": list(self.skus),
            "sku_classes": list(self.sku_classes),
            "is_main_product_package": self.is_main_product_package,
            "target_warehouse_code": self.target_warehouse_code,
            "target_warehouse_name": self.target_warehouse_name,
            "target_channel_name": self.target_channel_name,
            "channel_mode": self.channel_mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TentWarehouseRoutingPlan:
    platform_order_no: str
    postal_code: str | None
    status: str
    required: bool
    postal_source: str | None = None
    postal_error: str | None = None
    decisions: tuple[TentWarehouseRoutingDecision, ...] = field(default_factory=tuple)
    reason: str = ""
    manual_required: bool = False
    source_sha256: str = ""
    schema_version: int = RULE_SCHEMA_VERSION

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "warehouse_logistics_required": self.required,
            "warehouse_logistics_plan_status": self.status,
            "warehouse_logistics_plan_reason": self.reason,
            "warehouse_logistics_manual_required": self.manual_required,
            "warehouse_logistics_postal_code": self.postal_code,
            "warehouse_logistics_postal_source": self.postal_source,
            "warehouse_logistics_postal_diagnostic": self.postal_error,
            "warehouse_logistics_rule_sha256": self.source_sha256,
            "warehouse_logistics_rule_version": self.schema_version,
            "warehouse_logistics_decisions": [item.to_log_dict() for item in self.decisions],
        }


@lru_cache(maxsize=4)
def load_tent_warehouse_rules(path: str | Path = DEFAULT_RULE_PATH) -> TentWarehouseRules:
    rule_path = Path(path)
    try:
        payload = json.loads(rule_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TentWarehouseRuleError(f"无法读取帐篷分仓规则：{rule_path}：{exc}") from exc
    if payload.get("schema_version") != RULE_SCHEMA_VERSION:
        raise TentWarehouseRuleError("帐篷分仓规则版本不受支持。")
    if payload.get("tie_breaker") != "CA":
        raise TentWarehouseRuleError("帐篷分仓规则必须明确同 Zone 选择 CA。")
    warehouses = payload.get("warehouses")
    if not isinstance(warehouses, dict):
        raise TentWarehouseRuleError("帐篷分仓规则缺少仓库定义。")
    for code, expected_name in (("CA", "港通 洛杉矶仓"), ("NJ", "港通 新泽西仓")):
        row = warehouses.get(code)
        if not isinstance(row, dict) or row.get("code") != code or row.get("name") != expected_name:
            raise TentWarehouseRuleError(f"帐篷分仓规则中的 {code} 仓定义不正确。")

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise TentWarehouseRuleError("帐篷分仓规则不包含邮编区间。")
    parsed: list[ZipRoutingRule] = []
    expected_start = 0
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, list) or len(raw) != 6:
            raise TentWarehouseRuleError(f"第 {index} 条邮编规则字段数量不正确。")
        start_zip, end_zip, ca_zone, nj_zone, action, reason = raw
        if not _is_five_digit_zip(start_zip) or not _is_five_digit_zip(end_zip):
            raise TentWarehouseRuleError(f"第 {index} 条邮编区间不是五位数字。")
        start_value, end_value = int(start_zip), int(end_zip)
        if start_value != expected_start:
            raise TentWarehouseRuleError(f"第 {index} 条邮编规则与前一条存在缺口或重叠。")
        if end_value < start_value:
            raise TentWarehouseRuleError(f"第 {index} 条邮编规则起止顺序错误。")
        if action not in {"CA", "NJ", "KEEP"}:
            raise TentWarehouseRuleError(f"第 {index} 条邮编规则动作无效。")
        if ca_zone is not None and not isinstance(ca_zone, int):
            raise TentWarehouseRuleError(f"第 {index} 条 CA Zone 无效。")
        if nj_zone is not None and not isinstance(nj_zone, int):
            raise TentWarehouseRuleError(f"第 {index} 条 NJ Zone 无效。")
        if action == "CA" and (ca_zone is None or nj_zone is None or ca_zone > nj_zone):
            raise TentWarehouseRuleError(f"第 {index} 条 CA 选择与 Zone 不一致。")
        if action == "NJ" and (ca_zone is None or nj_zone is None or nj_zone >= ca_zone):
            raise TentWarehouseRuleError(f"第 {index} 条 NJ 选择与 Zone 不一致。")
        parsed.append(
            ZipRoutingRule(
                start_zip=start_zip,
                end_zip=end_zip,
                ca_zone=ca_zone,
                nj_zone=nj_zone,
                action=action,
                reason=str(reason or ""),
            )
        )
        expected_start = end_value + 1
    if expected_start != 100_000:
        raise TentWarehouseRuleError("帐篷分仓规则没有完整覆盖 00000-99999。")
    return TentWarehouseRules(
        schema_version=RULE_SCHEMA_VERSION,
        source_workbook=str(payload.get("source_workbook") or ""),
        source_sha256=str(payload.get("source_sha256") or ""),
        tie_breaker="CA",
        warehouses={key: dict(value) for key, value in warehouses.items()},
        rules=tuple(parsed),
    )


def lookup_zip_routing_rule(
    postal_code: str | None,
    rules: TentWarehouseRules | None = None,
) -> ZipRoutingRule | None:
    normalized = normalize_us_zip(postal_code)
    if normalized is None:
        return None
    value = int(normalized)
    rule_set = rules or load_tent_warehouse_rules()
    for rule in rule_set.rules:
        if int(rule.start_zip) <= value <= int(rule.end_zip):
            return rule
    raise TentWarehouseRuleError(f"邮编 {normalized} 未被规则覆盖。")


def normalize_us_zip(postal_code: str | None) -> str | None:
    return normalize_us_postal_code(postal_code)


def classify_tent_routing_sku(sku: str | None) -> str:
    normalized = _sku_key(sku)
    if not normalized:
        return "unknown"
    if normalized == _sku_key(INSTRUCTION_SKU):
        return "instruction"
    if is_frame_sku(normalized):
        return "frame"
    roller_skus = {_sku_key(rule["roller"]) for rule in TENT_SIZE_RULES.values()}
    if normalized in roller_skus or re.search(r"ROLL(?:ER)?-?BAG", normalized):
        return "roller_bag"
    if normalized == _sku_key(SANDBAG_SKU) or "SANDBAG" in normalized:
        return "sandbag"
    top_skus = {_sku_key(rule["top"]) for rule in TENT_SIZE_RULES.values()}
    if normalized in top_skus:
        return "fabric"
    if re.fullmatch(r"(?:10|15|20)FT-(?:FULL|HALF)-WALL(?:-DOUBLE-SIDED)?", normalized):
        return "fabric"
    if normalized.startswith(("TABLECLOTH-", "FEATHER-FLAG-", "TEARDROP-FLAG-")):
        return "fabric"
    return "unknown"


def build_tent_warehouse_routing_plan(
    *,
    sku_plan: TentSkuAdjustmentPlan,
    packages: Sequence[TentRoutingPackage],
    rules: TentWarehouseRules | None = None,
) -> TentWarehouseRoutingPlan:
    rule_set = rules or load_tent_warehouse_rules()
    base = {
        "platform_order_no": sku_plan.platform_order_no,
        "postal_code": normalize_us_zip(sku_plan.destination.postal_code),
        "postal_source": sku_plan.destination.postal_source,
        "postal_error": sku_plan.destination.postal_error,
        "source_sha256": rule_set.source_sha256,
        "schema_version": rule_set.schema_version,
    }
    if not packages:
        return TentWarehouseRoutingPlan(
            **base,
            status="manual_review",
            required=True,
            manual_required=True,
            reason="拆单后未读取到任何系统单包裹。",
        )
    if sku_plan.destination.category != "us_mainland":
        decisions = tuple(_skip_decision(package, "美国非本土、领地或非美国本土订单不修改仓库物流。") for package in packages)
        return TentWarehouseRoutingPlan(
            **base,
            status="not_required",
            required=False,
            decisions=decisions,
            reason="目的地区域不适用美国本土分仓规则。",
        )
    zip_rule = lookup_zip_routing_rule(sku_plan.destination.postal_code, rule_set)
    if zip_rule is None:
        reason = (
            str(sku_plan.destination.postal_error or "").strip()
            or "接口及页面均未取得有效五位邮编，禁止自动设置仓库物流。"
        )
        decisions = tuple(_manual_decision(package, reason) for package in packages)
        return TentWarehouseRoutingPlan(
            **base,
            status="manual_review",
            required=True,
            decisions=decisions,
            manual_required=True,
            reason=reason,
        )
    if zip_rule.action == "KEEP":
        decisions = tuple(_skip_decision(package, zip_rule.reason) for package in packages)
        return TentWarehouseRoutingPlan(
            **base,
            status="not_required",
            required=False,
            decisions=decisions,
            reason=zip_rule.reason,
        )

    main_package_nos, mapping_error = _map_main_product_packages(sku_plan, packages)
    if mapping_error:
        decisions = tuple(
            _manual_decision(package, mapping_error)
            for package in packages
        )
        return TentWarehouseRoutingPlan(
            **base,
            status="manual_review",
            required=True,
            decisions=decisions,
            manual_required=True,
            reason=mapping_error,
        )

    decisions: list[TentWarehouseRoutingDecision] = []
    warehouse = rule_set.warehouses[zip_rule.action]
    for package in packages:
        classes = tuple(classify_tent_routing_sku(item.sku) for item in package.items)
        skus = tuple(item.sku for item in package.items)
        class_set = set(classes)
        is_main = package.system_order_no in main_package_nos
        manual_reason = _package_manual_reason(class_set)
        if manual_reason:
            decisions.append(_manual_decision(package, manual_reason, classes=classes, is_main=is_main))
            continue
        if class_set in ({"fabric"}, {"instruction"}):
            reason = "纯布面包裹保留默认仓库和原物流。" if class_set == {"fabric"} else "纯 Instruction 包裹不修改仓库物流。"
            decisions.append(
                TentWarehouseRoutingDecision(
                    system_order_no=package.system_order_no,
                    status="skip",
                    skus=skus,
                    sku_classes=classes,
                    is_main_product_package=is_main,
                    reason=reason,
                )
            )
            continue
        channel_mode = "fedex_ground_economy" if is_main else "unrestricted_comparison"
        channel_suffix = (
            FEDEX_GROUND_ECONOMY
            if is_main
            else UNRESTRICTED_CHANNEL_COMPARISON_BY_WAREHOUSE[zip_rule.action]
        )
        decisions.append(
            TentWarehouseRoutingDecision(
                system_order_no=package.system_order_no,
                status="ready",
                skus=skus,
                sku_classes=classes,
                is_main_product_package=is_main,
                target_warehouse_code=zip_rule.action,
                target_warehouse_name=warehouse["name"],
                target_channel_name=f"{warehouse['name']}-{channel_suffix}",
                channel_mode=channel_mode,
                reason=f"邮编 {base['postal_code']} 命中 {zip_rule.action} 仓；{zip_rule.reason}",
            )
        )
    manual = any(item.status == "manual_review" for item in decisions)
    ready = any(item.status == "ready" for item in decisions)
    return TentWarehouseRoutingPlan(
        **base,
        status="manual_review" if manual else ("ready" if ready else "not_required"),
        required=ready or manual,
        decisions=tuple(decisions),
        manual_required=manual,
        reason=(
            "至少一个包裹需要人工复核，禁止写入任何仓库物流。"
            if manual
            else ("已生成拆单后仓库物流计划。" if ready else "所有包裹均无需修改仓库物流。")
        ),
    )


def tent_sku_plan_to_routing_input(plan: TentSkuAdjustmentPlan) -> dict[str, Any]:
    """持久化仓库阶段所需的最小 SKU 计划，供拆单后的安全重试使用。"""

    destination = asdict(plan.destination)
    destination["postal_code"] = normalize_us_zip(plan.destination.postal_code)
    return {
        "platform_order_no": plan.platform_order_no,
        "system_order_no": plan.system_order_no,
        "destination": destination,
        "replace_main_sku": plan.replace_main_sku,
        "replace_main_quantity": plan.replace_main_quantity,
        "replace_main_items": [asdict(item) for item in plan.replace_main_items],
        "main_product_items": [asdict(item) for item in plan.main_product_items],
    }


def tent_sku_plan_from_routing_input(payload: Any) -> TentSkuAdjustmentPlan:
    """从持久化输入恢复只用于仓库路由的 SKU 计划。"""

    if not isinstance(payload, dict):
        raise TentWarehouseRuleError("仓库物流重试缺少已持久化的 SKU 计划。")
    destination_payload = payload.get("destination")
    if not isinstance(destination_payload, dict):
        raise TentWarehouseRuleError("仓库物流重试缺少已持久化的收货地区。")
    from .tent_sku_planner import DestinationRegion

    try:
        destination = DestinationRegion(**destination_payload)
        destination.postal_code = normalize_us_zip(destination.postal_code)
        replace_main_items = [
            TentSkuPlanAction(**item)
            for item in payload.get("replace_main_items") or []
            if isinstance(item, dict)
        ]
        main_product_items = [
            TentSkuPlanAction(**item)
            for item in payload.get("main_product_items") or []
            if isinstance(item, dict)
        ]
        return TentSkuAdjustmentPlan(
            platform_order_no=str(payload.get("platform_order_no") or "").strip(),
            system_order_no=str(payload.get("system_order_no") or "").strip(),
            destination=destination,
            replace_main_sku=(str(payload.get("replace_main_sku")).strip() if payload.get("replace_main_sku") else None),
            replace_main_quantity=max(1, int(payload.get("replace_main_quantity") or 1)),
            replace_main_items=replace_main_items,
            main_product_items=main_product_items,
        )
    except (TypeError, ValueError) as exc:
        raise TentWarehouseRuleError(f"仓库物流重试的持久化 SKU 计划无效：{exc}") from exc


def _package_manual_reason(classes: set[str]) -> str | None:
    if "unknown" in classes:
        return "包裹包含未识别 SKU，禁止自动设置仓库物流。"
    if "instruction" in classes and len(classes) > 1:
        return "Instruction 与实物混包，禁止自动设置仓库物流。"
    physical = {"frame", "roller_bag", "sandbag"}
    if "fabric" in classes and classes.intersection(physical):
        return "布面与海外仓商品混包，禁止自动设置仓库物流。"
    return None


def _main_product_actions(plan: TentSkuAdjustmentPlan) -> list[TentSkuPlanAction]:
    if plan.main_product_items:
        return [item for item in plan.main_product_items if item.sku and item.quantity > 0]
    if plan.replace_main_items:
        return [item for item in plan.replace_main_items if item.sku and item.quantity > 0]
    if plan.replace_main_sku:
        return [
            TentSkuPlanAction(
                action="main_product",
                sku=plan.replace_main_sku,
                quantity=max(1, int(plan.replace_main_quantity or 1)),
            )
        ]
    return []


def _map_main_product_packages(
    plan: TentSkuAdjustmentPlan,
    packages: Sequence[TentRoutingPackage],
) -> tuple[set[str], str | None]:
    actions = _main_product_actions(plan)
    if not actions:
        return set(), "无法从 SKU 调整计划识别拆单前原始商品行。"
    main_packages: set[str] = set()
    unresolved: list[TentSkuPlanAction] = []
    all_identifiers = {
        identifier
        for package in packages
        for item in package.items
        for identifier in item.identifiers
    }
    for action in actions:
        source_id = _normalized_identifier(action.source_order_item_id)
        if source_id and source_id in all_identifiers:
            matches = {
                package.system_order_no
                for package in packages
                if any(source_id in item.identifiers for item in package.items)
            }
            if len(matches) != 1:
                return set(), f"原始商品行 {source_id} 在拆单结果中映射到多个包裹。"
            main_packages.update(matches)
        else:
            unresolved.append(action)
    for action in unresolved:
        sku_key = _sku_key(action.sku)
        candidates = [
            (package.system_order_no, item)
            for package in packages
            for item in package.items
            if _sku_key(item.sku) == sku_key
        ]
        if len(candidates) != 1:
            return set(), f"主商品 SKU {action.sku} 在拆单结果中重复或缺失，无法唯一映射。"
        system_order_no, item = candidates[0]
        if int(item.quantity or 0) != int(action.quantity or 0):
            return set(), f"主商品 SKU {action.sku} 数量与拆单结果不一致，无法唯一映射。"
        main_packages.add(system_order_no)
    return main_packages, None


def _skip_decision(package: TentRoutingPackage, reason: str) -> TentWarehouseRoutingDecision:
    return TentWarehouseRoutingDecision(
        system_order_no=package.system_order_no,
        status="skip",
        skus=tuple(item.sku for item in package.items),
        sku_classes=tuple(classify_tent_routing_sku(item.sku) for item in package.items),
        reason=reason,
    )


def _manual_decision(
    package: TentRoutingPackage,
    reason: str,
    *,
    classes: tuple[str, ...] | None = None,
    is_main: bool = False,
) -> TentWarehouseRoutingDecision:
    return TentWarehouseRoutingDecision(
        system_order_no=package.system_order_no,
        status="manual_review",
        skus=tuple(item.sku for item in package.items),
        sku_classes=classes or tuple(classify_tent_routing_sku(item.sku) for item in package.items),
        is_main_product_package=is_main,
        reason=reason,
    )


def _sku_key(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _normalized_identifier(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _is_five_digit_zip(value: object) -> bool:
    return bool(re.fullmatch(r"\d{5}", str(value or "")))


__all__ = [
    "DEFAULT_RULE_PATH",
    "FEDEX_GROUND_ECONOMY",
    "TentRoutingItem",
    "TentRoutingPackage",
    "TentWarehouseRoutingDecision",
    "TentWarehouseRoutingPlan",
    "TentWarehouseRuleError",
    "TentWarehouseRules",
    "UNRESTRICTED_CHANNEL_COMPARISON",
    "UNRESTRICTED_CHANNEL_COMPARISON_BY_WAREHOUSE",
    "ZipRoutingRule",
    "build_tent_warehouse_routing_plan",
    "classify_tent_routing_sku",
    "load_tent_warehouse_rules",
    "lookup_zip_routing_rule",
    "normalize_us_zip",
    "tent_sku_plan_from_routing_input",
    "tent_sku_plan_to_routing_input",
]
