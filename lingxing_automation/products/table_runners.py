"""桌旗(table_runners)商品识别与文件夹命名规则。

桌旗继续复用 zip JSON 管线：页面 DOM 只负责下载 zip，业务规则只从
JSON pairs 读取。这里集中维护 ASIN、尺寸和选项中文片段，避免把规则散落
到流程编排代码里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .tents import normalize_asin


PRODUCT_TYPE_TABLE_RUNNERS = "table_runners"
TABLE_RUNNER_PRODUCT_NAME = "桌旗"
TABLE_RUNNER_CONTACT_PROMPT = (
    "Please provide a texting number/email to contact you for emergencies "
    "(low quality image, etc)"
)


@dataclass(frozen=True)
class TableRunnerProductMatch:
    """桌旗商品匹配结果。"""

    product_type: str
    asin: str
    parent_asin: str
    size: str


# 桌旗尺寸由子 ASIN 决定，zip JSON 里通常不会重复提供尺寸；
# 因此必须先通过子 ASIN 查尺寸映射，再生成“x个{尺寸}桌旗”。
TABLE_RUNNER_PARENT_TO_CHILD_SIZE: dict[str, dict[str, str]] = {
    "B0DL61S1C9": {
        "B0DL6CY8FB": "12x72in",
        "B0DL6F3HMF": "48x72in",
        "B0DL6GL3D3": "24x72in",
        "B0DL6HFD37": "36x72in",
    }
}

TABLE_RUNNER_PARENT_ASINS = set(TABLE_RUNNER_PARENT_TO_CHILD_SIZE)
TABLE_RUNNER_CHILD_TO_PARENT: dict[str, str] = {
    child_asin: parent_asin
    for parent_asin, child_map in TABLE_RUNNER_PARENT_TO_CHILD_SIZE.items()
    for child_asin in child_map
}
TABLE_RUNNER_SIZE_BY_ASIN: dict[str, str] = {
    child_asin: size
    for child_map in TABLE_RUNNER_PARENT_TO_CHILD_SIZE.values()
    for child_asin, size in child_map.items()
}

TABLE_RUNNER_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "material": ("Choose Your Material for the Table Runner",),
    "proof": (
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)",
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
        "Proof Option",
    ),
}

TABLE_RUNNER_OPTIONS_BY_PARENT: dict[str, dict[str, dict[str, str]]] = {
    "B0DL61S1C9": {
        "material": {
            "vinyl material, not fabric": "喷绘布",
            "150gsm poly fabric, light & versatile": "150g经编布",
            "150gsm poly fabric, light and versatile": "150g经编布",
            "260gsm poly fabric, high dens & durable": "260g经编布",
            "260gsm poly fabric, high dens and durable": "260g经编布",
            "260gsm poly fabric, high density & durable": "260g经编布",
            "260gsm poly fabric, high density and durable": "260g经编布",
        },
        "proof": {
            "straight to prod, i checked info/spell": "直接制作",
            "straight to prod": "直接制作",
            "straight to production": "直接制作",
            "online proof (48h no reply=ship)": "在线检查",
            "online proof": "在线检查",
        },
    }
}


def normalize_table_runner_title(title: str | None) -> str:
    """归一化 JSON 标题，避免大小写、冒号和多余空格影响匹配。"""

    return re.sub(r"\s+", " ", str(title or "")).strip().lower().rstrip(":：")


def normalize_table_runner_option_value(value: str | None) -> str:
    """归一化选项值，用于匹配思维导图第二行中文片段。"""

    text = str(value or "").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.rstrip(".")


def get_table_runner_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """按标题别名从 zip JSON pairs 中读取选项值。"""

    normalized_aliases = {normalize_table_runner_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_table_runner_title(title), normalized_aliases):
            return value
    return None


def find_table_runner_parent_asin(asin: str | None) -> str | None:
    """根据桌旗子 ASIN 返回父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    if normalized in TABLE_RUNNER_PARENT_ASINS:
        return normalized
    return TABLE_RUNNER_CHILD_TO_PARENT.get(normalized)


def is_table_runner_asin(asin: str | None) -> bool:
    """判断当前 ASIN 是否属于桌旗。"""

    return find_table_runner_parent_asin(asin) is not None


def get_table_runner_size(asin: str | None) -> str | None:
    """读取桌旗子 ASIN 对应的尺寸规格。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TABLE_RUNNER_SIZE_BY_ASIN.get(normalized)


def get_table_runner_option_rules(parent_asin: str, group: str) -> dict[str, str]:
    """按父 ASIN 和选项分组读取桌旗规则。"""

    normalized_parent = normalize_asin(parent_asin) or parent_asin
    return TABLE_RUNNER_OPTIONS_BY_PARENT.get(normalized_parent, {}).get(group, {})


def match_table_runner_product(asin: str | None) -> TableRunnerProductMatch | None:
    """返回桌旗商品匹配信息。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    parent = find_table_runner_parent_asin(normalized)
    if not parent:
        return None
    return TableRunnerProductMatch(
        product_type=PRODUCT_TYPE_TABLE_RUNNERS,
        asin=normalized,
        parent_asin=parent,
        size=get_table_runner_size(normalized) or "",
    )
