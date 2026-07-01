from __future__ import annotations

import re
from dataclasses import dataclass

from .car_magnets import CAR_MAGNET_CONTACT_PROMPT
from .tents import extract_asins, normalize_asin


PRODUCT_TYPE_X_STANDS = "x_stands"
X_STAND_CONTACT_PROMPT = CAR_MAGNET_CONTACT_PROMPT
X_STAND_PROOF_TITLE = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"


@dataclass(frozen=True)
class XStandProductMatch:
    asin: str
    parent_asin: str
    product_type: str = PRODUCT_TYPE_X_STANDS
    contact_prompts: tuple[str, ...] = (X_STAND_CONTACT_PROMPT,)


X_STAND_PARENT_ASIN = "B0CY566Q8C"

# X展架是独立产品族，尺寸完全由子 ASIN 决定，不复用易拉宝/拉网展架规则。
X_STAND_FRAGMENT_BY_ASIN: dict[str, str] = {
    "B0D1FZKVV7": "24x63inX展架",
    "B0CW56CP7M": "32x71inX展架",
    "B0CW57ZPFN": "32x78inX展架",
}

X_STAND_ASIN_TO_PARENT_ASIN = {
    X_STAND_PARENT_ASIN: X_STAND_PARENT_ASIN,
    **{asin: X_STAND_PARENT_ASIN for asin in X_STAND_FRAGMENT_BY_ASIN},
}

# Proof 是 X展架文件夹名的可选尾部片段；出现未知值时才报规则缺失。
X_STAND_PROOF_OPTIONS = {
    "straight to production": "直接制作",
    "online proof (48h no reply=ship)": "在线检查",
}


def normalize_x_stand_option_value(value: str | None) -> str:
    """把 Proof 值规范成规则键，兼容大小写、连续空白和末尾句点。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text.rstrip(".")


def find_x_stand_parent_asin(asin: str | None) -> str | None:
    """根据父/子 ASIN 定位 X展架父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return X_STAND_ASIN_TO_PARENT_ASIN.get(normalized)


def is_x_stand_asin(asin: str | None) -> bool:
    """判断当前 ASIN 是否属于 X展架产品族。"""

    return find_x_stand_parent_asin(asin) is not None


def get_x_stand_fragment(asin: str | None) -> str | None:
    """按子 ASIN 读取“尺寸+X展架”片段，未知子 ASIN 不做猜测。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return X_STAND_FRAGMENT_BY_ASIN.get(normalized)


def match_x_stand_product(texts: str | list[str] | tuple[str, ...]) -> XStandProductMatch | None:
    """从页面文本或 ASIN 列表中识别 X展架。"""

    for asin in extract_asins(texts):
        parent_asin = find_x_stand_parent_asin(asin)
        if parent_asin:
            return XStandProductMatch(asin=asin, parent_asin=parent_asin)
    return None
