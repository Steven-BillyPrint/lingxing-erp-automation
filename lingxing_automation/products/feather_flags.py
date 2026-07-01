"""刀旗(feather_flags)商品识别与文件夹命名规则。

刀旗同样走 zip JSON 管线：页面 DOM 只用于下载每个商品行自己的 zip，
业务数据统一从 zip 内 JSON pairs 读取。规则来源是 flag.pdf 的可抽取文本层；
禁止用截图 OCR 猜测规则，避免把 ASIN 或选项文字识别错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .car_magnets import CAR_MAGNET_CONTACT_PROMPT
from .tents import extract_asins, normalize_asin


PRODUCT_TYPE_FEATHER_FLAGS = "feather_flags"
FEATHER_FLAG_PARENT_ASIN = "B0DPX3YWVT"
FEATHER_FLAG_CONTACT_PROMPT = CAR_MAGNET_CONTACT_PROMPT


@dataclass(frozen=True)
class FeatherFlagProductMatch:
    """刀旗商品匹配结果。"""

    asin: str
    parent_asin: str
    product_type: str = PRODUCT_TYPE_FEATHER_FLAGS
    contact_prompts: tuple[str, ...] = (FEATHER_FLAG_CONTACT_PROMPT,)


# 刀旗尺寸由子 ASIN 决定，zip JSON 中通常只保存选项，不保存规格；
# 因此必须先用子 ASIN 查尺寸，缺失时不能从标题或 SKU 猜测。
FEATHER_FLAG_SIZE_BY_ASIN: dict[str, str] = {
    "B0DS1ZD2DQ": "0.75x4.4m",
    "B0DS2394QH": "0.7x3.4m",
    "B0DS22CWH8": "0.65x1.7m",
    "B0DS23HZLC": "0.65x2.4m",
    "B0DS21LCF1": "0.95x2.3m",
    "B0DS22RB3S": "0.75x1.65m",
    "B0DS22ZKQ9": "1.2x3.5m",
    "B0DS23R3M7": "1.2x3.2m",
    "B0DS22QQ7G": "0.95x2.6m",
    "B0DS22HMGQ": "0.8x4.1m",
    "B0DS21PFM7": "0.6x2.5m",
    "B0DS22NHGT": "0.5x2m",
    "B0DS21RCJ8": "0.7x3.4m",
}

FEATHER_FLAG_PRODUCT_NAME_BY_ASIN: dict[str, str] = {
    "B0DS1ZD2DQ": "方形旗帜",
    "B0DS2394QH": "方形旗帜",
    "B0DS22CWH8": "方形旗帜",
    "B0DS23HZLC": "方形旗帜",
    "B0DS21LCF1": "水滴旗",
    "B0DS22RB3S": "水滴旗",
    "B0DS22ZKQ9": "水滴旗",
    "B0DS23R3M7": "水滴旗",
    "B0DS22QQ7G": "水滴旗",
    "B0DS22HMGQ": "刀旗",
    "B0DS21PFM7": "刀旗",
    "B0DS22NHGT": "刀旗",
    "B0DS21RCJ8": "刀旗",
}

FEATHER_FLAG_ASIN_TO_PARENT_ASIN: dict[str, str] = {
    FEATHER_FLAG_PARENT_ASIN: FEATHER_FLAG_PARENT_ASIN,
    **{asin: FEATHER_FLAG_PARENT_ASIN for asin in FEATHER_FLAG_SIZE_BY_ASIN},
}

FEATHER_FLAG_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "printing_side": ("Printing Side",),
    "pole": ("Pole Type",),
    "cross_base": ("Cross Base",),
    "ground_spike": ("Ground Spike",),
    "water_bag": ("Wather Bag", "Water Bag"),
    "carrying_bag": ("Carrying Bag",),
    "proof": (
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)",
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
        "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping",
        "Proof Option",
    ),
}

# Printing Side 比较特殊：“单面/双面”必须嵌入品名片段，
# “相同/不同设计”则作为后续独立片段。
FEATHER_FLAG_PRINTING_SIDE_OPTIONS: dict[str, tuple[str, str]] = {
    "single-sided printing": ("单面", ""),
    "single sided printing": ("单面", ""),
    "2-sided printing: same design both sides": ("双面", "相同设计"),
    "2 sided printing: same design both sides": ("双面", "相同设计"),
    "2-sided printing: different designs": ("双面", "不同设计"),
    "2 sided printing: different designs": ("双面", "不同设计"),
}

FEATHER_FLAG_OPTIONS_BY_PARENT: dict[str, dict[str, dict[str, str]]] = {
    FEATHER_FLAG_PARENT_ASIN: {
        "pole": {
            "no, i don't need a pole": "",
            "aluminum-pole": "铝纤维杆",
            "aluminum-fiberglass pole": "铝纤维杆",
            "aluminum fiberglass pole": "铝纤维杆",
            "all-fiberglass-pole": "全玻璃纤维杆",
            "all-fiberglass pole": "全玻璃纤维杆",
            "all fiberglass pole": "全玻璃纤维杆",
        },
        "cross_base": {
            "no, i don't need an iron pipe cross base": "",
            "no, i don't need a cross base": "",
            "yes, i need an iron pipe cross base": "铁管十字底座",
            "yes, i need a flat iron cross base": "扁铁十字底座",
        },
        "ground_spike": {
            "no, i don't need a ground spike": "",
            "yes, i need a ground spike": "地钉",
        },
        "water_bag": {
            "no, i don't need a water bag": "",
            "no": "",
            "yes, i need a water bag": "水袋",
        },
        "carrying_bag": {
            "no, i don't need a carrying bag": "",
            "no": "",
            "yes, i need a carrying bag": "手提袋",
        },
        "proof": {
            "straight to production": "直接制作",
            "straight to prod": "直接制作",
            "online proof (48h no reply=ship)": "在线检查",
        },
    }
}


def normalize_feather_flag_title(title: str | None) -> str:
    """归一化 JSON 标题，兼容大小写、末尾冒号和连续空白差异。"""

    return re.sub(r"\s+", " ", str(title or "")).strip().lower().rstrip(":：")


def normalize_feather_flag_option_value(value: str | None) -> str:
    """归一化选项值，用于匹配 flag.pdf 文本层转录出来的规则键。"""

    text = str(value or "").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(".")


def get_feather_flag_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """按标题别名从 zip JSON pairs 中读取选项值。"""

    normalized_aliases = {normalize_feather_flag_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_feather_flag_title(title), normalized_aliases):
            return value
    return None


def find_feather_flag_parent_asin(asin: str | None) -> str | None:
    """根据父/子 ASIN 定位刀旗父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return FEATHER_FLAG_ASIN_TO_PARENT_ASIN.get(normalized)


def is_feather_flag_asin(asin: str | None) -> bool:
    """判断当前 ASIN 是否属于刀旗产品族。"""

    return find_feather_flag_parent_asin(asin) is not None


def get_feather_flag_size(asin: str | None) -> str | None:
    """读取刀旗子 ASIN 对应的尺寸规格。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return FEATHER_FLAG_SIZE_BY_ASIN.get(normalized)


def get_feather_flag_product_name(asin: str | None) -> str | None:
    """读取子 ASIN 对应的旗帜品名。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return FEATHER_FLAG_PRODUCT_NAME_BY_ASIN.get(normalized)


def get_feather_flag_option_rules(parent_asin: str, group: str) -> dict[str, str]:
    """按父 ASIN 和选项分组读取刀旗规则。"""

    normalized_parent = normalize_asin(parent_asin) or parent_asin
    return FEATHER_FLAG_OPTIONS_BY_PARENT.get(normalized_parent, {}).get(group, {})


def get_feather_flag_printing_side_rules(parent_asin: str) -> dict[str, tuple[str, str]]:
    """读取 Printing Side 规则；返回值同时包含品名内的单双面和设计相同/不同片段。"""

    normalized_parent = normalize_asin(parent_asin)
    if normalized_parent == FEATHER_FLAG_PARENT_ASIN:
        return FEATHER_FLAG_PRINTING_SIDE_OPTIONS
    return {}


def match_feather_flag_product(texts: str | list[str] | tuple[str, ...]) -> FeatherFlagProductMatch | None:
    """从页面文本或 ASIN 列表中识别刀旗商品。"""

    for asin in extract_asins(texts):
        parent_asin = find_feather_flag_parent_asin(asin)
        if parent_asin:
            return FeatherFlagProductMatch(asin=asin, parent_asin=parent_asin)
    return None
