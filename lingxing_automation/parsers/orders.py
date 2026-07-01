from __future__ import annotations

import re

from ..constants import PLATFORM_ORDER_RE, SYSTEM_ORDER_RE
from .contact import normalize_text


def is_single_main_sku_order_text(row_text: str, platform_occurrence_count: int = 1) -> bool:
    """判断订单文本是否符合单主 SKU 订单特征。"""
    text = normalize_text(row_text)
    if platform_occurrence_count != 1:
        return False
    if not text:
        return False
    if re.search(r"拆分订单|拆分单|拆单", text):
        return False
    if re.search(r"共\s*\d+\s+更多|SKU.{0,120}更多|商品.{0,120}更多", text):
        return False
    sku_counts = [int(value) for value in re.findall(r"共\s*(\d+)", text)]
    if any(value > 1 for value in sku_counts):
        return False
    return True

def guess_search_kind(order_no: str | None, explicit_kind: str | None) -> str:
    """根据用户输入猜测订单搜索类型。"""
    if not order_no:
        return "visible"
    text = order_no.strip()
    if PLATFORM_ORDER_RE.fullmatch(text):
        detected = "platform"
    elif SYSTEM_ORDER_RE.fullmatch(text):
        detected = "system"
    else:
        raise ValueError("订单号格式无法识别：平台单号应为 num-num-num，系统单号应为一长串连续数字。")
    if explicit_kind and explicit_kind != detected:
        raise ValueError(f"订单号格式与 --search-kind 不一致：输入看起来是 {detected}，但指定的是 {explicit_kind}。")
    return detected

def validate_search_snapshot(
    order_no: str,
    expected_label: str,
    selected_label: str | None,
    inputs: list[dict[str, Any]],
    search_input_index: int | None,
) -> tuple[bool, str]:
    """校验搜索区域状态是否与预期输入一致。"""
    if selected_label != expected_label:
        return False, f"搜索类型应为 {expected_label}，但页面当前是 {selected_label or '未知'}。"
    if search_input_index is None:
        return False, "没有定位到订单号搜索输入框。"
    search_input = next((item for item in inputs if item.get("index") == search_input_index), None)
    if search_input is None:
        return False, "订单号搜索输入框在校验时消失。"
    if str(search_input.get("value") or "").strip() != order_no:
        return False, "订单号没有填入平台/系统单号搜索框。"

    contaminated = [
        item
        for item in inputs
        if item.get("index") != search_input_index and order_no in str(item.get("value") or "")
    ]
    if contaminated:
        labels = [str(item.get("around") or item.get("placeholder") or f"input#{item.get('index')}") for item in contaminated]
        return False, f"订单号被填入了其它输入框，疑似日期/订购时间控件：{'；'.join(labels[:2])}"
    return True, "搜索输入框校验通过。"
