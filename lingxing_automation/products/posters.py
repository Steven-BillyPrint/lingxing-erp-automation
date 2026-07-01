from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .tents import normalize_asin


PRODUCT_TYPE_POSTERS = "posters"

POSTER_CONTACT_PROMPTS = (
    "Please provide a texting number/email to contact you for emergencies (low quality image, etc)",
    "Please provide a texting number to contact you for emergencies (low quality image, etc)",
)

POSTER_PROOF_TITLE_ALIASES = (
    "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
    "Proof Option",
)


@dataclass(frozen=True)
class PosterProductMatch:
    """海报商品识别结果。"""

    product_type: str
    asin: str
    parent_asin: str
    fragment: str


# 海报规则来源：C:/Users/Mayn/Desktop/1.xlsx。
# Excel 第一行是父 ASIN，列下面的“子ASIN(片段名)”中括号内容就是生产文件夹里的尺寸规格品名。
POSTER_PARENT_TO_CHILD_FRAGMENT: dict[str, dict[str, str]] = {
    "B0DMVTR5GY": {
        "B0DNNCJW43": "11x17in可转移贴",
        "B0DMW1X2CG": "24x24in可转移贴",
        "B0DMVYP4L6": "12x12in可转移贴",
        "B0DMW2KVSZ": "8x10in可转移贴",
        "B0DMW4TXXM": "11x14in照片纸",
        "B0DMW29N2Z": "12x18in照片纸",
        "B0DMVYZ313": "18x24in油画布",
        "B0DMW2SWBY": "16x24in可转移贴",
        "B0DMW1DN3C": "12x18in可转移贴",
        "B0DMW2Z7YL": "16x16in可转移贴",
        "B0DMW2S8TQ": "11x14in可转移贴",
        "B0DMW1Y9T9": "12x18in油画布",
        "B0DMW1WNJR": "30x30in可转移贴",
        "B0DMW1LHN5": "28x40in油画布",
        "B0DMVZV73V": "36x48in油画布",
        "B0DMW43R3M": "40x60in照片纸",
        "B0DMW538HQ": "40x60in油画布",
        "B0DMW1N5XH": "22x28in照片纸",
        "B0DMW2XDMS": "12x12in油画布",
        "B0DMW2FMTL": "40x60in可转移贴",
        "B0DMW42D5F": "28x40in照片纸",
        "B0DMW1R8JH": "16x20in照片纸",
        "B0DMW4WHT2": "16x16in油画布",
        "B0DMW31VTD": "24x24in照片纸",
        "B0DMW1WJCL": "16x20in油画布",
        "B0DMW2KVSR": "12x12in照片纸",
        "B0DMW4QRT5": "8.5x11in照片纸",
        "B0DMW286S6": "22x28in油画布",
        "B0DMW4CS4K": "30x30in油画布",
        "B0DMW1SVZ9": "24x36in油画布",
        "B0DMW1T3TZ": "16x16in照片纸",
        "B0DMW3BJ3S": "16x24in油画布",
        "B0DMVYSW4F": "36x48in照片纸",
        "B0DMW3J5XX": "11x14in油画布",
        "B0DMW3JQGN": "30x30in照片纸",
        "B0DMW48ZM5": "16x24in照片纸",
        "B0DMW27MF2": "11x17in油画布",
        "B0DMW1VDXX": "11x17in照片纸",
        "B0DMW4N85H": "24x36in照片纸",
        "B0DMW1DKPW": "8x10in照片纸",
        "B0DMW1V38F": "18x24in照片纸",
        "B0DMVZHS1K": "8.5x11in油画布",
        "B0DMW1WZHZ": "24x24in油画布",
        "B0DMW281WQ": "8x10in油画布",
        "B0CZNRL13D": "24x36in可转移贴",
        "B0CZNTBM3N": "28x40in可转移贴",
        "B0CZNQ4LGX": "18x24in可转移贴",
        "B0CZNRFVZT": "16x20in可转移贴",
        "B0CZNTFL3M": "36x48in可转移贴",
        "B0CZNS66JN": "8.5x11in可转移贴",
        "B0CZNQ7VTB": "22x28in可转移贴",
    },
    "B0CQNV8JT8": {
        "B0CRYPGFBN": "36x48in可转移贴",
        "B0CRYTF5X6": "20x30in可转移贴",
        "B0CRYSGCGX": "24x24in可转移贴",
        "B0CRYDL8BS": "24x36in可转移贴",
        "B0CQYDG1WS": "12x18in可转移贴",
        "B0CQYD7KRM": "12x12in可转移贴",
        "B0CQYDT9LQ": "18x24in可转移贴",
    },
}

POSTER_PARENT_ASINS = set(POSTER_PARENT_TO_CHILD_FRAGMENT)
POSTER_CHILD_TO_PARENT: dict[str, str] = {
    child_asin: parent_asin
    for parent_asin, children in POSTER_PARENT_TO_CHILD_FRAGMENT.items()
    for child_asin in children
}
POSTER_FRAGMENT_BY_ASIN: dict[str, str] = {
    child_asin: fragment
    for children in POSTER_PARENT_TO_CHILD_FRAGMENT.values()
    for child_asin, fragment in children.items()
}

POSTER_PROOF_OPTIONS = {
    "straight to production": "直接制作",
    "online proof (48h no reply=ship)": "在线检查",
}


def normalize_poster_title(title: str | None) -> str:
    """归一化 JSON 标题，避免大小写和多余空格影响匹配。"""

    return re.sub(r"\s+", " ", str(title or "")).strip().lower().rstrip(":：")


def normalize_poster_option_value(value: str | None) -> str:
    """归一化海报选项值，Proof 末尾句点不参与业务含义。"""

    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return normalized.rstrip(".")


def find_poster_parent_asin(asin: str | None) -> str | None:
    """根据海报子 ASIN 返回父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    if normalized in POSTER_PARENT_ASINS:
        return normalized
    return POSTER_CHILD_TO_PARENT.get(normalized)


def is_poster_asin(asin: str | None) -> bool:
    """判断 ASIN 是否属于海报商品。"""

    return find_poster_parent_asin(asin) is not None


def get_poster_fragment(asin: str | None) -> str | None:
    """获取海报子 ASIN 对应的尺寸规格品名片段。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return POSTER_FRAGMENT_BY_ASIN.get(normalized)


def match_poster_product(asin: str | None) -> PosterProductMatch | None:
    """返回海报产品匹配信息。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    parent_asin = find_poster_parent_asin(normalized)
    if not parent_asin:
        return None
    return PosterProductMatch(
        product_type=PRODUCT_TYPE_POSTERS,
        asin=normalized,
        parent_asin=parent_asin,
        fragment=get_poster_fragment(normalized) or "",
    )


def get_poster_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """按多个标题别名从 JSON pairs 中取值。"""

    alias_keys = {normalize_poster_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_poster_title(title), alias_keys):
            return value
    return None


def lookup_poster_proof_option(value: str | None) -> str | None:
    """把 Proof Option 英文选项转换为文件夹中文片段。"""

    key = normalize_poster_option_value(value)
    if not key:
        return ""
    return POSTER_PROOF_OPTIONS.get(key)
