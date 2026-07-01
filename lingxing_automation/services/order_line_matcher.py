from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from ..models import CustomizationJsonInfo, OrderCustomizationItem, OrderFolderLine
from ..products.catalog import match_supported_product
from .custom_attachment_downloader import normalize_item_match_text
from .customization_json_parser import pairs_to_text


class OrderLineMatchError(ValueError):
    """订单商品行与定制化文本无法一一匹配。"""


class CustomJsonAmbiguousSameAsinError(OrderLineMatchError):
    """同 ASIN 多行无法用 orderItemId 区分。"""


def _norm(value: str | None) -> str:
    """规范化文本，便于订单行匹配。"""
    return (normalize_item_match_text(value) or "").strip().lower()


def _asin(value: str | None) -> str:
    """处理ASIN相关逻辑，并返回后续流程所需结果。"""
    return _norm(value).upper()


def _quantity_ordered(item: dict[str, Any]) -> int:
    """读取 Amazon 订单行中的订购数量，并兼容不同字段命名。"""
    try:
        quantity = int(item.get("quantity_ordered", item.get("QuantityOrdered", 0)) or 0)
    except (TypeError, ValueError):
        return 0
    return max(quantity, 0)


def _build_customization_queues(customization_items: list[OrderCustomizationItem]) -> tuple[dict[tuple[str, str], deque[OrderCustomizationItem]], dict[str, deque[OrderCustomizationItem]]]:
    """按 ASIN 构建定制化文本队列，支持逐行配对。"""
    exact: dict[tuple[str, str], deque[OrderCustomizationItem]] = defaultdict(deque)
    by_asin: dict[str, deque[OrderCustomizationItem]] = defaultdict(deque)
    for item in sorted(customization_items, key=lambda value: value.row_index if value.row_index is not None else 10_000):
        asin = _asin(item.asin)
        sku = _norm(item.sku)
        if not asin:
            continue
        if sku:
            exact[(asin, sku)].append(item)
        by_asin[asin].append(item)
    return exact, by_asin


def _pop_matching_customization(
    *,
    exact: dict[tuple[str, str], deque[OrderCustomizationItem]],
    by_asin: dict[str, deque[OrderCustomizationItem]],
    asin: str,
    sku: str,
) -> OrderCustomizationItem | None:
    """弹出与当前 Amazon 订单行匹配的定制化文本。"""
    if asin and sku and exact.get((asin, sku)):
        chosen = exact[(asin, sku)].popleft()
    elif asin and by_asin.get(asin):
        chosen = by_asin[asin].popleft()
    else:
        return None
    # 同一个对象可能同时在 exact/by_asin 队列里；取走后要从另一个队列移除，避免重复使用。
    if asin and by_asin.get(asin):
        by_asin[asin] = deque(item for item in by_asin[asin] if item is not chosen)
    if asin and sku and exact.get((asin, sku)):
        exact[(asin, sku)] = deque(item for item in exact[(asin, sku)] if item is not chosen)
    return chosen


def build_order_folder_lines(
    *,
    amazon_order_items: list[dict[str, Any]],
    customization_items: list[OrderCustomizationItem],
) -> list[OrderFolderLine]:
    """把 Amazon 订单商品行与领星定制文本配对。

    Amazon API 是数量和 OrderItem 顺序的唯一来源；领星 DOM 只提供每个商品行的
    定制化文本。对于同 ASIN/SKU 的多行，按 Amazon 返回顺序和页面行顺序逐条配对。
    """

    exact, by_asin = _build_customization_queues(customization_items)
    lines: list[OrderFolderLine] = []
    missing_customization: list[str] = []
    for index, item in enumerate(amazon_order_items):
        asin = _asin(str(item.get("asin") or item.get("ASIN") or ""))
        sku = _norm(str(item.get("seller_sku") or item.get("SellerSKU") or ""))
        product = match_supported_product(asin)
        if not product:
            continue
        quantity = _quantity_ordered(item)
        if quantity <= 0:
            raise OrderLineMatchError(f"Amazon OrderItem 缺少有效 QuantityOrdered：{asin or '-'} / {sku or '-'}")
        customization = _pop_matching_customization(exact=exact, by_asin=by_asin, asin=asin, sku=sku)
        if not customization or not customization.customization_text.strip():
            missing_customization.append(f"{asin or '-'} / {sku or '-'}")
            continue
        lines.append(
            OrderFolderLine(
                asin=asin,
                sku=sku,
                parent_asin=product.parent_asin,
                product_type=product.product_type,
                quantity=quantity,
                customization_text=customization.customization_text,
                order_item_id=str(item.get("order_item_id") or item.get("OrderItemId") or "") or None,
                source_index=index,
            )
        )
    if missing_customization:
        raise OrderLineMatchError("缺少当前商品行的定制化文本：" + "；".join(missing_customization))
    if not lines:
        raise OrderLineMatchError("Amazon OrderItems 中没有匹配到当前支持的定制商品。")
    return lines


def build_order_folder_lines_from_json(
    *,
    amazon_order_items: list[dict[str, Any]],
    customization_items: list[CustomizationJsonInfo],
) -> tuple[list[OrderFolderLine], list[str]]:
    """用 orderItemId 把 Amazon OrderItems 和 zip JSON 一一配对。

    同一个 ASIN 可能在同一个平台单里出现多行，并且每行定制化信息不同；
    因此必须按 orderItemId 逐行处理，不能按 ASIN 汇总数量。
    """

    by_order_item_id = {
        str(item.order_item_id): item
        for item in customization_items
        if item.order_item_id
    }
    by_asin: dict[str, list[CustomizationJsonInfo]] = defaultdict(list)
    for item in customization_items:
        by_asin[_asin(item.asin)].append(item)

    lines: list[OrderFolderLine] = []
    warnings: list[str] = []
    missing: list[str] = []
    for index, item in enumerate(amazon_order_items):
        asin = _asin(str(item.get("asin") or item.get("ASIN") or ""))
        sku = _norm(str(item.get("seller_sku") or item.get("SellerSKU") or ""))
        product = match_supported_product(asin)
        if not product:
            continue
        order_item_id = str(item.get("order_item_id") or item.get("OrderItemId") or "")
        customization = by_order_item_id.get(order_item_id)
        if customization is None:
            asin_matches = by_asin.get(asin, [])
            if len(asin_matches) == 1:
                customization = asin_matches[0]
                warnings.append(f"json_match_by_asin_without_order_item_id:{asin}")
            elif len(asin_matches) > 1:
                raise CustomJsonAmbiguousSameAsinError(f"同 ASIN 多行无法区分 orderItemId：{asin}")
        if customization is None:
            missing.append(f"{order_item_id or '-'} / {asin or '-'} / {sku or '-'}")
            continue
        quantity = _quantity_ordered(item)
        if quantity <= 0:
            raise OrderLineMatchError(f"Amazon OrderItem 缺少有效 QuantityOrdered：{order_item_id or '-'} / {asin or '-'}")
        if customization.quantity and customization.quantity != quantity:
            warnings.append(f"quantity_mismatch:{order_item_id}:json={customization.quantity}:amazon={quantity}")
        lines.append(
            OrderFolderLine(
                asin=asin,
                sku=sku,
                parent_asin=product.parent_asin,
                product_type=product.product_type,
                quantity=quantity,
                customization_text=pairs_to_text(customization.pairs),
                customization_pairs=dict(customization.pairs),
                order_item_id=order_item_id or customization.order_item_id,
                source_index=index,
            )
        )
    if missing:
        raise OrderLineMatchError("缺少当前 Amazon OrderItem 对应的 zip JSON：" + "；".join(missing))
    if not lines:
        raise OrderLineMatchError("Amazon OrderItems 中没有匹配到当前支持的定制商品。")
    return lines, warnings
