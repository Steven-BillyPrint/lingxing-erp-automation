"""桌布(tablecloths)商品识别与文件夹命名规则。

规则来源：`思维导图 (4).pdf` 内嵌的 XMind 图片。
PDF 本身不含可抽取文本节点，因此这里把图中可清晰读取的父 ASIN、子 ASIN、
尺寸和选项第二行中文集中维护，避免规则散落到流程代码里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .tents import normalize_asin


PRODUCT_TYPE_TABLECLOTHS = "tablecloths"
TABLECLOTH_CONTACT_PROMPT = (
    "Please provide a texting number/email to contact you for emergencies "
    "(low quality image, etc)"
)


@dataclass(frozen=True)
class TableclothProductMatch:
    """桌布商品匹配结果。"""

    product_type: str
    asin: str
    parent_asin: str
    size: str
    product_name: str


TABLECLOTH_PARENT_PRODUCT_NAME: dict[str, str] = {
    # 父 ASIN 的第二行是生产文件夹使用的品名。
    "B0D9HVTXW2": "方套桌布",
    "B0DBG9JWYS": "弹力桌布",
    "B0DL92H9H6": "平铺桌布",
}

TABLECLOTH_PARENT_TO_CHILD_SIZE: dict[str, dict[str, str]] = {
    "B0D9HVTXW2": {
        "B0D9HS5187": "4FT",
        "B0DBG11GM2": "5FT",
        "B0D9HT6M86": "6FT",
        "B0D9HT8LD4": "8FT",
    },
    "B0DBG9JWYS": {
        "B0DBG9KG7S": "4FT",
        "B0DBGBV6KN": "5FT",
        "B0DBGBDHL7": "6FT",
        "B0DBGDT7QF": "8FT",
    },
    "B0DL92H9H6": {
        "B0DLZ732R": "4FT",
        "B0DL72H6N1": "5FT",
        "B0DL6WCGP8": "6FT",
        "B0DL6SY8WZ": "8FT",
    },
}

TABLECLOTH_PARENT_ASINS = set(TABLECLOTH_PARENT_TO_CHILD_SIZE)
TABLECLOTH_CHILD_TO_PARENT: dict[str, str] = {
    asin: parent
    for parent, children in TABLECLOTH_PARENT_TO_CHILD_SIZE.items()
    for asin in children
}
TABLECLOTH_SIZE_BY_ASIN: dict[str, str] = {
    asin: size
    for children in TABLECLOTH_PARENT_TO_CHILD_SIZE.values()
    for asin, size in children.items()
}

TABLECLOTH_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "fabric": ("Choose Your Polyester Fabric",),
    "back": ("Open or Closed Back Option",),
    "proof": (
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)",
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
        "Proof Option",
    ),
}

TABLECLOTH_COMMON_PROOF_OPTIONS = {
    "straight to prod, i checked info/spell": "直接制作",
    "straight to production": "直接制作",
    "online proof (48h no reply=ship)": "在线检查",
    "online proof": "在线检查",
}

TABLECLOTH_OPTIONS_BY_PARENT: dict[str, dict[str, dict[str, str]]] = {
    "B0D9HVTXW2": {
        "fabric": {
            "150gsm polyester, light and versatile": "150g经编布",
            "260gsm polyester, high density & durable": "260g经编布",
        },
        "back": {
            "closed tablecloth in the back": "背后闭口",
            "open tablecloth in the back": "背后开口",
        },
        "proof": TABLECLOTH_COMMON_PROOF_OPTIONS,
    },
    "B0DBG9JWYS": {
        "fabric": {
            "190gsm spandex, light and versatile": "190g弹力布",
            "280gsm spandex, high density & durable": "280g弹力布",
        },
        "back": {
            "closed tablecloth in the back": "背后闭口",
            "open tablecloth in the back": "背后开口",
        },
        "proof": TABLECLOTH_COMMON_PROOF_OPTIONS,
    },
    "B0DL92H9H6": {
        "fabric": {
            "150gsm polyester, light and versatile": "150g经编布",
            "260gsm polyester, high density & durable": "260g经编布",
        },
        "proof": TABLECLOTH_COMMON_PROOF_OPTIONS,
    },
}


def normalize_tablecloth_title(title: str | None) -> str:
    """归一化 JSON 标题，避免大小写和多余空格影响匹配。"""

    return re.sub(r"\s+", " ", str(title or "")).strip().lower().rstrip(":：")


def normalize_tablecloth_option_value(value: str | None) -> str:
    """归一化选项值，按思维导图英文选项匹配第二行中文。"""

    text = str(value or "").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(".")


def get_tablecloth_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """按标题别名从 zip JSON pairs 中取对应值。"""

    normalized_aliases = {normalize_tablecloth_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_tablecloth_title(title), normalized_aliases):
            return value
    return None


def find_tablecloth_parent_asin(asin: str | None) -> str | None:
    """根据桌布子 ASIN 返回父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    if normalized in TABLECLOTH_PARENT_ASINS:
        return normalized
    return TABLECLOTH_CHILD_TO_PARENT.get(normalized)


def is_tablecloth_asin(asin: str | None) -> bool:
    """判断当前 ASIN 是否为桌布商品。"""

    return find_tablecloth_parent_asin(asin) is not None


def get_tablecloth_size(asin: str | None) -> str | None:
    """桌布尺寸由子 ASIN 决定，JSON 中通常不再重复提供尺寸。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TABLECLOTH_SIZE_BY_ASIN.get(normalized)


def get_tablecloth_product_name(parent_asin: str | None) -> str | None:
    """根据父 ASIN 读取思维导图第二行配置的桌布品名。"""

    normalized = normalize_asin(parent_asin)
    if not normalized:
        return None
    return TABLECLOTH_PARENT_PRODUCT_NAME.get(normalized)


def get_tablecloth_option_rules(parent_asin: str, group: str) -> dict[str, str]:
    """按父 ASIN 和选项分组读取桌布映射规则。"""

    normalized_parent = normalize_asin(parent_asin) or parent_asin
    return TABLECLOTH_OPTIONS_BY_PARENT.get(normalized_parent, {}).get(group, {})


def match_tablecloth_product(asin: str | None) -> TableclothProductMatch | None:
    """返回桌布产品匹配信息。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    parent = find_tablecloth_parent_asin(normalized)
    if not parent:
        return None
    return TableclothProductMatch(
        product_type=PRODUCT_TYPE_TABLECLOTHS,
        asin=normalized,
        parent_asin=parent,
        size=get_tablecloth_size(normalized) or "",
        product_name=get_tablecloth_product_name(parent) or "",
    )
