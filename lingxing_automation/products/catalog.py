from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .car_magnets import PRODUCT_TYPE_CAR_MAGNET, match_car_magnet_product
from .feather_flags import PRODUCT_TYPE_FEATHER_FLAGS, match_feather_flag_product
from .pop_up_displays import PRODUCT_TYPE_POP_UP_DISPLAYS, match_pop_up_display_product
from .posters import PRODUCT_TYPE_POSTERS, match_poster_product
from .roll_up_banners import PRODUCT_TYPE_ROLL_UP_BANNERS, match_roll_up_banner_product
from .tablecloths import PRODUCT_TYPE_TABLECLOTHS, match_tablecloth_product
from .table_runners import PRODUCT_TYPE_TABLE_RUNNERS, match_table_runner_product
from .tents import extract_asins, match_tent_product
from .vinyl_banners import PRODUCT_TYPE_VINYL_BANNERS, match_vinyl_banner_product
from .x_stands import PRODUCT_TYPE_X_STANDS, match_x_stand_product

PRODUCT_TYPE_TENT = "tent"


@dataclass(frozen=True)
class SupportedProductMatch:
    asin: str
    parent_asin: str
    product_type: str
    contact_prompts: tuple[str, ...]


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
