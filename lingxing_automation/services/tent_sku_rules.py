from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TentSkuRuleItem:
    """单个需要添加或换货的 SKU 规则项。"""

    sku: str
    quantity: int = 1
    reason: str = ""


TENT_SIZE_RULES: dict[str, dict[str, str]] = {
    "3x3m": {
        "top": "10x10-Canopy-Topper",
        "frame_prefix": "10X10",
        "wall_prefix": "10ft",
        "roller": "TENT-ROLLER-BAG-10X10-50MM",
    },
    "3x4.5m": {
        "top": "10x15-Canopy-Topper",
        "frame_prefix": "10X15",
        "wall_prefix": "15ft",
        "roller": "TENT-ROLLER-BAG-10X15-50MM",
    },
    "3x6m": {
        "top": "10x20-Canopy-Topper",
        "frame_prefix": "10X20",
        "wall_prefix": "20ft",
        "roller": "TENT-ROLLER-BAG-10X20-50MM",
    },
}

SANDBAG_SKU = "SANDBAGS-4PCS"
INSTRUCTION_SKU = "Instruction"


def _compact(text: str) -> str:
    """压缩文本空白和大小写差异，便于规则匹配。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def detect_tent_size_key(texts: list[str] | tuple[str, ...]) -> str | None:
    """从文件夹片段中识别帐篷规格。

    SKU 添加阶段只处理已经生成好的帐篷文件夹，因此规格来源以文件夹中文片段为准。
    """

    text = _compact("+".join(texts))
    if any(marker in text for marker in ("3x6m", "3×6m", "10x20", "20ft")):
        return "3x6m"
    if any(marker in text for marker in ("3x4.5m", "3×4.5m", "10x15", "15ft")):
        return "3x4.5m"
    if any(marker in text for marker in ("3x3m", "3×3m", "10x10", "10ft")):
        return "3x3m"
    return None


def tent_top_sku(size_key: str) -> str:
    """返回指定帐篷尺寸对应的顶布 SKU。"""
    return TENT_SIZE_RULES[size_key]["top"]


def roller_bag_sku(size_key: str) -> str:
    """处理拖轮包包SKU相关逻辑，并返回后续流程所需结果。"""
    return TENT_SIZE_RULES[size_key]["roller"]


def wall_prefix(size_key: str) -> str:
    """处理侧墙前缀相关逻辑，并返回后续流程所需结果。"""
    return TENT_SIZE_RULES[size_key]["wall_prefix"]


def frame_sku_for_component(size_key: str, component: str, *, rail_required: bool = False) -> TentSkuRuleItem | None:
    """根据支架中文片段生成支架 SKU。

    横杆版本在 SKU 表里通过 -RAIL 区分；普通支架则不追加后缀。
    """

    text = _compact(component)
    prefix = TENT_SIZE_RULES[size_key]["frame_prefix"]
    if "40mm方形铝" in text:
        sku = f"{prefix}-FRAME-40MM-SQUARE"
    elif "40mm六角铝" in text:
        sku = f"{prefix}-FRAME-40MM-HEX"
    elif "50mm六角铝" in text:
        sku = f"{prefix}-FRAME-50MM-HEX"
    else:
        return None
    if size_key == "3x3m" and (rail_required or "横杆" in text):
        sku = f"{sku}-RAIL"
    return TentSkuRuleItem(sku=sku, reason=component)


def _leading_quantity(component: str) -> int:
    """读取组件文本开头的数量，缺失时按一件处理。"""
    text = str(component or "")
    # 文件夹片段里经常包含 3x3m、6FT 这类尺寸数字；
    # SKU 数量只能来自“2个/2套/2半高侧墙”等明确数量前缀，不能随便抓第一个数字。
    match = re.match(r"\s*(\d+)\s*(?:个|套|组|件|张)", text)
    if not match:
        match = re.match(r"\s*(\d+)(?=(?:双面|单面)?(?:全高|半高|全围|半围))", text)
    if not match:
        return 1
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return 1


def wall_sku_for_component(size_key: str, component: str) -> TentSkuRuleItem | None:
    """把全围/半围片段转换为基础 SKU 和数量。

    表格里的 10ft-Half-Wall-2 末尾 -2 不是 SKU 的一部分，只代表添加商品后数量框要填 2。
    """

    text = str(component or "")
    compact = _compact(text)
    prefix = wall_prefix(size_key)
    quantity = _leading_quantity(text)
    double_sided_suffix = "-Double-Sided" if "双面" in compact else ""
    # 文件夹里常写“全高背墙/半高侧墙”，SKU 表里对应“全围/半围”。
    if "全高背墙" in compact or "全围" in compact:
        return TentSkuRuleItem(sku=f"{prefix}-Full-Wall{double_sided_suffix}", quantity=quantity, reason=component)
    if "半高侧墙" in compact or "半围" in compact:
        return TentSkuRuleItem(sku=f"{prefix}-Half-Wall{double_sided_suffix}", quantity=quantity, reason=component)
    return None


def tablecloth_sku_for_component(component: str) -> TentSkuRuleItem | None:
    """把桌布组件转换为对应的 SKU 规则项。"""
    text = str(component or "")
    match = re.search(r"([4568])\s*ft", text, flags=re.IGNORECASE)
    if not match:
        return None
    return TentSkuRuleItem(sku=f"Tablecloth-Rectangle-{match.group(1)}ft", reason=component)


def flag_sku_for_component(component: str) -> TentSkuRuleItem | None:
    """把旗帜组件转换为对应的 SKU 规则项。"""
    text = _compact(component)
    if "刀旗" in text or "feather" in text:
        if "0.6x2.5m" in text or "0.6×2.5m" in text:
            return TentSkuRuleItem(sku="Feather-Flag-0.6x2.5m", reason=component)
        if "0.5x2m" in text or "0.5×2m" in text:
            return TentSkuRuleItem(sku="Feather-Flag-0.5x2m", reason=component)
    if "水滴旗" in text or "teardrop" in text:
        if "0.95x2.3m" in text or "0.95×2.3m" in text:
            return TentSkuRuleItem(sku="Teardrop-Flag-0.95x2.3m", reason=component)
        if "0.75x1.65m" in text or "0.75×1.65m" in text:
            return TentSkuRuleItem(sku="Teardrop-Flag-0.75x1.65m", reason=component)
    return None


def tent_accessory_component_to_sku_items(component: str) -> list[TentSkuRuleItem]:
    """匹配帐篷订单内不依赖主帐篷尺寸的配件 SKU。"""

    text = str(component or "")
    if not text or "绳子地钉" in text:
        return []
    for matcher in (
        tablecloth_sku_for_component,
        flag_sku_for_component,
    ):
        item = matcher(text)
        if item:
            return [item]
    return []


def component_to_sku_items(size_key: str, component: str, *, rail_required: bool = False) -> list[TentSkuRuleItem]:
    """把单个文件夹片段转换为需要补加的 SKU。

    绳子地钉按业务要求不需要添加到订单商品里，因此这里直接跳过。
    """

    text = str(component or "")
    if not text or "绳子地钉" in text:
        return []
    if "帐篷顶" in text:
        return [TentSkuRuleItem(sku=tent_top_sku(size_key), quantity=_leading_quantity(text), reason=text)]
    if "拖轮包" in text:
        return [TentSkuRuleItem(sku=roller_bag_sku(size_key), quantity=_leading_quantity(text), reason=text)]
    if "沙袋" in text:
        return [TentSkuRuleItem(sku=SANDBAG_SKU, quantity=_leading_quantity(text), reason=text)]
    for matcher in (
        lambda value: frame_sku_for_component(size_key, value, rail_required=rail_required),
        lambda value: wall_sku_for_component(size_key, value),
    ):
        item = matcher(text)
        if item:
            return [item]
    return tent_accessory_component_to_sku_items(text)
