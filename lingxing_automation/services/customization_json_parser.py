from __future__ import annotations

from typing import Any

from ..models import CustomizationJsonInfo
from .order_folder_rules import ORDER_FOLDER_TITLE_ALIASES, ORDER_FOLDER_TITLES

CAR_MAGNET_CONTACT_TITLE = (
    "Please provide a Texting Number or Email to contact you for emergencies "
    "(low quality image, etc)"
)


def _normalize_title(value: str | None) -> str:
    """规范化标题，便于后续匹配和比较。"""
    return " ".join(str(value or "").strip().split()).lower()


FOLDER_TITLE_BY_NORMALIZED = {
    _normalize_title(title): ORDER_FOLDER_TITLE_ALIASES.get(title, title)
    for title in ORDER_FOLDER_TITLES
}
FOLDER_TITLE_BY_NORMALIZED[_normalize_title(CAR_MAGNET_CONTACT_TITLE)] = CAR_MAGNET_CONTACT_TITLE


def canonical_json_title(label: str | None) -> str:
    """把 JSON 里的标题归一到文件夹规则使用的标准标题。"""

    raw = " ".join(str(label or "").strip().split())
    return FOLDER_TITLE_BY_NORMALIZED.get(_normalize_title(raw), raw)


def _first_non_empty(*values: Any) -> str:
    """从多个候选值中返回第一个非空文本。"""
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _add_pair(pairs: dict[str, str], warnings: list[str], label: str | None, value: str | None) -> None:
    """向定制化选项字典追加有效标题和值。"""
    title = canonical_json_title(label)
    if not title:
        return
    text = str(value or "").strip()
    old_value = pairs.get(title)
    if old_value is None:
        pairs[title] = text
        return
    if old_value == text or not text:
        return
    if not old_value:
        pairs[title] = text
        return
    warnings.append(f"duplicate_json_title:{title}")


def _extract_version3_pairs(data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """优先读取 version3.0，它是 Amazon 当前定制 JSON 中更平铺、更稳定的结构。"""

    pairs: dict[str, str] = {}
    warnings: list[str] = []
    version3 = data.get("version3.0")
    if not isinstance(version3, dict):
        return pairs, warnings
    customization_info = version3.get("customizationInfo")
    surfaces = customization_info.get("surfaces") if isinstance(customization_info, dict) else None
    if not isinstance(surfaces, list):
        return pairs, warnings
    for surface in surfaces:
        if not isinstance(surface, dict):
            continue
        areas = surface.get("areas")
        if not isinstance(areas, list):
            continue
        for area in areas:
            if not isinstance(area, dict):
                continue
            label = area.get("label")
            customization_type = str(area.get("customizationType") or "")
            if customization_type == "Options":
                value = area.get("optionValue")
            elif customization_type == "TextPrinting":
                value = area.get("text")
            elif customization_type == "ImagePrinting":
                value = area.get("buyerFilename")
            else:
                value = _first_non_empty(area.get("optionValue"), area.get("text"), area.get("buyerFilename"))
            _add_pair(pairs, warnings, label, value)
    return pairs, warnings


def _walk_legacy_children(node: Any, pairs: dict[str, str], warnings: list[str]) -> None:
    """version3.0 不存在时，递归读取 legacy customizationData.children。"""

    if isinstance(node, list):
        for child in node:
            _walk_legacy_children(child, pairs, warnings)
        return
    if not isinstance(node, dict):
        return

    label = _first_non_empty(node.get("label"), node.get("name"))
    node_type = str(node.get("type") or node.get("customizationType") or "")
    option_selection = node.get("optionSelection") if isinstance(node.get("optionSelection"), dict) else {}
    if "OptionCustomization" in node_type or "Options" in node_type:
        value = _first_non_empty(
            node.get("displayValue"),
            option_selection.get("label"),
            option_selection.get("name"),
            node.get("optionValue"),
        )
        _add_pair(pairs, warnings, label, value)
    elif "TextCustomization" in node_type or "TextPrinting" in node_type:
        _add_pair(pairs, warnings, label, _first_non_empty(node.get("inputValue"), node.get("text")))
    elif "ImageCustomization" in node_type or "ImagePrinting" in node_type:
        _add_pair(pairs, warnings, label, _first_non_empty(node.get("buyerFilename")))

    children = node.get("children")
    if children is not None:
        _walk_legacy_children(children, pairs, warnings)


def _extract_legacy_pairs(data: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """从输入内容中提取旧格式选项对。"""
    pairs: dict[str, str] = {}
    warnings: list[str] = []
    customization_data = data.get("customizationData")
    if isinstance(customization_data, dict):
        _walk_legacy_children(customization_data.get("children"), pairs, warnings)
    return pairs, warnings


def parse_customization_json_info(
    data: dict[str, Any],
    *,
    raw_json_path: str | None = None,
    source_zip_path: str | None = None,
) -> CustomizationJsonInfo:
    """从 zip 内 JSON 解析单个 OrderItem 的定制化业务数据。"""

    pairs, warnings = _extract_version3_pairs(data)
    if not pairs:
        pairs, warnings = _extract_legacy_pairs(data)

    order_id = str(data.get("orderId") or data.get("order_id") or "")
    order_item_id = str(data.get("orderItemId") or data.get("order_item_id") or "")
    asin = str(data.get("asin") or data.get("ASIN") or "").upper()
    try:
        quantity = int(data.get("quantity") or data.get("QuantityOrdered") or 0)
    except (TypeError, ValueError):
        quantity = 0
    return CustomizationJsonInfo(
        order_id=order_id,
        order_item_id=order_item_id,
        asin=asin,
        title=str(data.get("title") or data.get("Title") or "") or None,
        quantity=max(quantity, 0),
        pairs=pairs,
        raw_json_path=raw_json_path,
        source_zip_path=source_zip_path,
        warnings=warnings,
    )


def pairs_to_text(pairs: dict[str, str]) -> str:
    """兼容旧函数：把 JSON pairs 转成 Title : Value 文本。"""

    return "\n".join(f"{title} : {value}" for title, value in pairs.items())
