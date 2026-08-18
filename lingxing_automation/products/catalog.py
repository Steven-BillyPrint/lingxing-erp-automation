from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .car_magnets import (
    PRODUCT_TYPE_CAR_MAGNET,
    find_car_magnet_parent_asin,
    match_car_magnet_product,
)
from .feather_flags import (
    PRODUCT_TYPE_FEATHER_FLAGS,
    find_feather_flag_parent_asin,
    match_feather_flag_product,
)
from .pop_up_displays import (
    PRODUCT_TYPE_POP_UP_DISPLAYS,
    find_pop_up_display_parent_asin,
    match_pop_up_display_product,
)
from .posters import (
    PRODUCT_TYPE_POSTERS,
    find_poster_parent_asin,
    match_poster_product,
)
from .roll_up_banners import (
    PRODUCT_TYPE_ROLL_UP_BANNERS,
    find_roll_up_banner_parent_asin,
    match_roll_up_banner_product,
)
from .tablecloths import (
    PRODUCT_TYPE_TABLECLOTHS,
    find_tablecloth_parent_asin,
    match_tablecloth_product,
)
from .table_runners import (
    PRODUCT_TYPE_TABLE_RUNNERS,
    find_table_runner_parent_asin,
    match_table_runner_product,
)
from .tents import extract_asins, find_tent_parent_asin, match_tent_product
from .vinyl_banners import (
    PRODUCT_TYPE_VINYL_BANNERS,
    find_vinyl_banner_parent_asin,
    match_vinyl_banner_product,
)
from .x_stands import (
    PRODUCT_TYPE_X_STANDS,
    find_x_stand_parent_asin,
    match_x_stand_product,
)

PRODUCT_TYPE_TENT = "tent"
# Bump when catalogue identity mappings or historical attribution semantics
# change so unresolved historical orders are queried again without repeatedly
# re-reading every old order each scan.
PRODUCT_IDENTITY_CATALOG_VERSION = "2026-08-17.1"


@dataclass(frozen=True)
class SupportedProductMatch:
    asin: str
    parent_asin: str
    product_type: str
    contact_prompts: tuple[str, ...]


@dataclass(frozen=True)
class ProductIdentityMatch:
    """A catalogue identity independent from workflow-rule completeness.

    A parent ASIN can identify a product family even when a child-only fact,
    such as size or stand type, is still unavailable.  Consumers that only
    display or persist an order's product type must use this identity layer;
    consumers about to run automation must continue to use
    :func:`match_supported_product`.
    """

    asin: str
    parent_asin: str
    product_type: str


_PRODUCT_IDENTITY_FINDERS = (
    (PRODUCT_TYPE_TENT, find_tent_parent_asin),
    (PRODUCT_TYPE_CAR_MAGNET, find_car_magnet_parent_asin),
    (PRODUCT_TYPE_TABLECLOTHS, find_tablecloth_parent_asin),
    (PRODUCT_TYPE_TABLE_RUNNERS, find_table_runner_parent_asin),
    (PRODUCT_TYPE_POSTERS, find_poster_parent_asin),
    (PRODUCT_TYPE_POP_UP_DISPLAYS, find_pop_up_display_parent_asin),
    (PRODUCT_TYPE_ROLL_UP_BANNERS, find_roll_up_banner_parent_asin),
    (PRODUCT_TYPE_X_STANDS, find_x_stand_parent_asin),
    (PRODUCT_TYPE_FEATHER_FLAGS, find_feather_flag_parent_asin),
    (PRODUCT_TYPE_VINYL_BANNERS, find_vinyl_banner_parent_asin),
)


def identify_product(asin: object) -> ProductIdentityMatch | None:
    """Return the exact catalogue family for one parent or child ASIN.

    This function deliberately does not require size, option, contact, folder,
    shipment, or declaration rules to be complete.
    """

    normalized_asins = extract_asins(str(asin or ""))
    if len(normalized_asins) != 1:
        return None
    normalized = normalized_asins[0]
    for product_type, find_parent in _PRODUCT_IDENTITY_FINDERS:
        parent_asin = find_parent(normalized)
        if parent_asin:
            return ProductIdentityMatch(
                asin=normalized,
                parent_asin=parent_asin,
                product_type=product_type,
            )
    return None


def identify_products(
    texts: str | Iterable[str],
) -> tuple[ProductIdentityMatch, ...]:
    """Identify every catalogued ASIN while preserving source order."""

    matches: list[ProductIdentityMatch] = []
    for asin in extract_asins(texts):
        match = identify_product(asin)
        if match is not None:
            matches.append(match)
    return tuple(matches)


def identify_product_types(texts: str | Iterable[str]) -> tuple[str, ...]:
    """Return all distinct product families represented by the supplied ASINs."""

    return tuple(
        dict.fromkeys(match.product_type for match in identify_products(texts))
    )


def preferred_product_type(values: object) -> str:
    """Choose the one product family shown by shipment-facing read models.

    A shipment or customer notification intentionally exposes one family only.
    Tent wins whenever it is present; otherwise the first observed family is
    retained so the result stays deterministic without inventing a priority
    between unrelated product families.
    """

    if isinstance(values, str):
        source: Iterable[object] = (values,)
    elif isinstance(values, Iterable):
        source = values
    else:
        source = ()
    parts = (
        part.strip()
        for item in source
        for part in str(item or "").replace("、", "|").replace("｜", "|").split("|")
    )
    normalized = tuple(
        value
        for value in dict.fromkeys(parts)
        if value
    )
    if PRODUCT_TYPE_TENT in normalized:
        return PRODUCT_TYPE_TENT
    return normalized[0] if normalized else ""


def match_supported_product(texts: str | Iterable[str]) -> SupportedProductMatch | None:
    """识别当前自动化支持的商品类型。

    流程层只关心“这个订单是否支持处理”，具体商品规则仍由各产品模块维护，
    避免后续新增品类时继续把 ASIN 判断写进 contact_sync.py。
    """
    text_source = [texts] if isinstance(texts, str) else list(texts)
    tent_match = match_tent_product(text_source)
    if tent_match:
        return SupportedProductMatch(
            asin=tent_match.asin,
            parent_asin=tent_match.parent_asin,
            product_type=PRODUCT_TYPE_TENT,
            contact_prompts=tent_match.contact_prompts,
        )
    car_magnet_match = match_car_magnet_product(text_source)
    if car_magnet_match:
        return SupportedProductMatch(
            asin=car_magnet_match.asin,
            parent_asin=car_magnet_match.parent_asin,
            product_type=PRODUCT_TYPE_CAR_MAGNET,
            contact_prompts=car_magnet_match.contact_prompts,
        )
    for asin in extract_asins(text_source):
        tablecloth_match = match_tablecloth_product(asin)
        if tablecloth_match:
            return SupportedProductMatch(
                asin=tablecloth_match.asin,
                parent_asin=tablecloth_match.parent_asin,
                product_type=PRODUCT_TYPE_TABLECLOTHS,
                contact_prompts=(),
            )
        table_runner_match = match_table_runner_product(asin)
        if table_runner_match:
            return SupportedProductMatch(
                asin=table_runner_match.asin,
                parent_asin=table_runner_match.parent_asin,
                product_type=PRODUCT_TYPE_TABLE_RUNNERS,
                contact_prompts=(),
            )
        poster_match = match_poster_product(asin)
        if poster_match:
            return SupportedProductMatch(
                asin=poster_match.asin,
                parent_asin=poster_match.parent_asin,
                product_type=PRODUCT_TYPE_POSTERS,
                contact_prompts=(),
            )
        pop_up_display_match = match_pop_up_display_product(asin)
        if pop_up_display_match:
            return SupportedProductMatch(
                asin=pop_up_display_match.asin,
                parent_asin=pop_up_display_match.parent_asin,
                product_type=PRODUCT_TYPE_POP_UP_DISPLAYS,
                contact_prompts=(),
            )
        # 易拉宝是独立产品族，只复用联系方式提示，不复用拉网展架或磁贴的命名规则。
        roll_up_banner_match = match_roll_up_banner_product(asin)
        if roll_up_banner_match:
            return SupportedProductMatch(
                asin=roll_up_banner_match.asin,
                parent_asin=roll_up_banner_match.parent_asin,
                product_type=PRODUCT_TYPE_ROLL_UP_BANNERS,
                contact_prompts=roll_up_banner_match.contact_prompts,
            )
        # X展架是独立产品族，虽然字段和易拉宝相似，但不能共用易拉宝产品类型。
        x_stand_match = match_x_stand_product(asin)
        if x_stand_match:
            return SupportedProductMatch(
                asin=x_stand_match.asin,
                parent_asin=x_stand_match.parent_asin,
                product_type=PRODUCT_TYPE_X_STANDS,
                contact_prompts=x_stand_match.contact_prompts,
            )
        # 刀旗是独立商品族；帐篷套餐里的旗帜配件仍由帐篷规则处理，避免互相污染。
        feather_flag_match = match_feather_flag_product(asin)
        if feather_flag_match:
            return SupportedProductMatch(
                asin=feather_flag_match.asin,
                parent_asin=feather_flag_match.parent_asin,
                product_type=PRODUCT_TYPE_FEATHER_FLAGS,
                contact_prompts=feather_flag_match.contact_prompts,
            )
        vinyl_banner_match = match_vinyl_banner_product(asin)
        if vinyl_banner_match:
            return SupportedProductMatch(
                asin=vinyl_banner_match.asin,
                parent_asin=vinyl_banner_match.parent_asin,
                product_type=PRODUCT_TYPE_VINYL_BANNERS,
                contact_prompts=(),
            )
    return None


def is_supported_product_type(product_type: str | None) -> bool:
    """判断产品类型是否属于当前自动化支持范围。"""
    return product_type in {
        PRODUCT_TYPE_TENT,
        PRODUCT_TYPE_CAR_MAGNET,
        PRODUCT_TYPE_TABLECLOTHS,
        PRODUCT_TYPE_TABLE_RUNNERS,
        PRODUCT_TYPE_POSTERS,
        PRODUCT_TYPE_POP_UP_DISPLAYS,
        PRODUCT_TYPE_ROLL_UP_BANNERS,
        PRODUCT_TYPE_VINYL_BANNERS,
        PRODUCT_TYPE_X_STANDS,
        PRODUCT_TYPE_FEATHER_FLAGS,
    }
