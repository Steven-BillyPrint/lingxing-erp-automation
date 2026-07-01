from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

EMAIL_PROMPT = "Please provide an email address to confirm customization design and details or for emergencies."
PHONE_PROMPT = "Please provide a texting number to confirm customization design and details or for emergencies."

ASIN_RE = re.compile(r"\bB0[A-Z0-9]{8}\b", re.I)

TENT_PARENT_TO_CHILD_ASINS: dict[str, tuple[str, ...]] = {
    "B0FTV6XDGG": ("B0F4PV828T", "B0F4PVDH8N", "B0DZ2W2QWK"),
    "B0CZNZVG26": (
        "B0D54Q9L98",
        "B0D5134SJ3",
        "B0D47YNR5Y",
        "B0D47X986N",
        "B0D47WD4NL",
        "B0D1TSTBDW",
        "B0D14M8RTM",
        "B0D14J92RZ",
        "B0CZNRR64T",
    ),
    "B0F5CTQXG1": ("B0F5CCG9T5", "B0F5CKNVYJ", "B0CRRGTPFH"),
    "B0D6XW7V9T": ("B0D7DMK75P", "B0D6XWP8YN", "B0D6KZ7G88"),
}

TENT_CONTACT_PROMPTS_BY_PARENT: dict[str, tuple[str, str]] = {
    "B0FTV6XDGG": (EMAIL_PROMPT, PHONE_PROMPT),
    "B0CZNZVG26": (PHONE_PROMPT, EMAIL_PROMPT),
    "B0F5CTQXG1": (EMAIL_PROMPT, PHONE_PROMPT),
    "B0D6XW7V9T": (EMAIL_PROMPT, PHONE_PROMPT),
}

TENT_ASIN_TO_PARENT_ASIN: dict[str, str] = {}
for parent_asin, child_asins in TENT_PARENT_TO_CHILD_ASINS.items():
    TENT_ASIN_TO_PARENT_ASIN[parent_asin] = parent_asin
    for child_asin in child_asins:
        TENT_ASIN_TO_PARENT_ASIN[child_asin] = parent_asin

# ASIN 到帐篷顶尺寸的映射来自“思维导图 (3).pdf”的尺寸节点。
# 文件夹归档依赖这个规格，统一放在产品模块，避免流程代码里散落重复规则。
TENT_ASIN_TO_TOP_SIZE: dict[str, str] = {
    "B0DZ2W2QWK": "3x3m帐篷顶",
    "B0CRRGTPFH": "3x3m帐篷顶",
    "B0D7DMK75P": "3x3m帐篷顶",
    "B0D5134SJ3": "3x3m帐篷顶",
    "B0D14M8RTM": "3x3m帐篷顶",
    "B0D14J92RZ": "3x3m帐篷顶",
    "B0F4PV828T": "3x4.5m帐篷顶",
    "B0F5CCG9T5": "3x4.5m帐篷顶",
    "B0D54Q9L98": "3x4.5m帐篷顶",
    "B0D47WD4NL": "3x4.5m帐篷顶",
    "B0CZNRR64T": "3x4.5m帐篷顶",
    "B0F4PVDH8N": "3x6m帐篷顶",
    "B0F5CKNVYJ": "3x6m帐篷顶",
    "B0D47YNR5Y": "3x6m帐篷顶",
    "B0D47X986N": "3x6m帐篷顶",
    "B0D1TSTBDW": "3x6m帐篷顶",
}

WALL_ONLY_ASIN_KIND: dict[str, str] = {
    # 这两个 ASIN 属于独立墙体商品，不是帐篷顶套餐，文件夹名不能生成“帐篷顶”片段。
    "B0D6KZ7G88": "full_wall",
    "B0D6XWP8YN": "half_wall",
}


@dataclass(frozen=True)
class TentProductMatch:
    asin: str
    parent_asin: str
    contact_prompts: tuple[str, str]


def normalize_asin(value: str | None) -> str | None:
    if not value:
        return None
    asin = value.strip().upper()
    return asin if ASIN_RE.fullmatch(asin) else None


def extract_asins(texts: str | Iterable[str]) -> list[str]:
    if isinstance(texts, str):
        joined = texts
    else:
        joined = "\n".join(str(text) for text in texts if text)
    seen: set[str] = set()
    output: list[str] = []
    for match in ASIN_RE.finditer(joined.upper()):
        asin = match.group(0)
        if asin not in seen:
            seen.add(asin)
            output.append(asin)
    return output


def find_tent_parent_asin(asin: str | None) -> str | None:
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TENT_ASIN_TO_PARENT_ASIN.get(normalized)


def is_tent_asin(asin: str | None) -> bool:
    return find_tent_parent_asin(asin) is not None


def get_tent_top_size(asin: str | None) -> str | None:
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TENT_ASIN_TO_TOP_SIZE.get(normalized)


def get_wall_only_asin_kind(asin: str | None) -> str | None:
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return WALL_ONLY_ASIN_KIND.get(normalized)


def match_tent_product(texts: str | Iterable[str]) -> TentProductMatch | None:
    for asin in extract_asins(texts):
        parent_asin = find_tent_parent_asin(asin)
        if parent_asin:
            return TentProductMatch(
                asin=asin,
                parent_asin=parent_asin,
                contact_prompts=TENT_CONTACT_PROMPTS_BY_PARENT[parent_asin],
            )
    return None
