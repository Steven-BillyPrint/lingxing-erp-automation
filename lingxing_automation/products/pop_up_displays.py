from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .tents import normalize_asin


PRODUCT_TYPE_POP_UP_DISPLAYS = "pop_up_displays"

POP_UP_DISPLAY_EMAIL_PROMPT = "Please provide an email address to confirm customization design and details or for emergencies."
POP_UP_DISPLAY_PHONE_PROMPT = "Please provide a texting number to confirm customization design and details or for emergencies."


@dataclass(frozen=True)
class PopUpDisplayProductMatch:
    """拉网展架商品识别结果。"""

    product_type: str
    asin: str
    parent_asin: str
    size: str
    stand_type: str


POP_UP_DISPLAY_PARENT_ASINS = {
    "B0H36GPHVH",
    "B0G6KJQPHK",
    "B0FX2828C9",
}

# 同一套展架规则下有三个不同父 ASIN，但生产文件夹里的品名不同；
# 品名必须集中维护，避免在 folder_builder 里继续写死“拉网展架”。
POP_UP_DISPLAY_PRODUCT_NAME_BY_PARENT = {
    "B0H36GPHVH": "拉网展架",
    "B0G6KJQPHK": "伸缩展架",
    "B0FX2828C9": "快幕秀",
}

# B0FX2828C9 下部分尺寸属于门型快幕秀；同一父 ASIN 下品名不完全一致，
# 因此用子 ASIN 覆盖父 ASIN 默认品名，文件夹生成层只读取最终品名。
POP_UP_DISPLAY_PRODUCT_NAME_BY_CHILD = {
    "B0FX9VSXHD": "门型快幕秀",
    "B0FX9XMP9D": "门型快幕秀",
    "B0FX9W6684": "门型快幕秀",
    "B0FX9XMBJK": "门型快幕秀",
    "B0FX9VGDCY": "门型快幕秀",
    "B0FX9Y2DXM": "门型快幕秀",
    "B0FX9TDRPR": "门型快幕秀",
    "B0FX9YPQ2C": "门型快幕秀",
}


# 规则来源：C:/Users/Mayn/Downloads/2.pdf 的可抽取文本节点。
# “尺寸规格”节点同时决定尺寸和带/不带支架，不能从商品标题或截图猜测。
POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND: dict[str, dict[str, dict[str, str]]] = {
    "带支架": {
        "B0H36GPHVH": {
            "B0H3V1K5W5": "5x7.5ft",
            "B0H3PH15S8": "7.5x7.5ft",
            "B0H39K6BXS": "10x7.5ft",
            "B0H3B9GSS7": "10x10ft",
            "B0H3B9GGTR": "12x7.5ft",
            "B0H39QG3DB": "15x7.5ft",
            "B0H39BJBVG": "20x7.5ft",
        },
        "B0G6KJQPHK": {
            "B0G6JZJDDJ": "8x8ft",
            "B0G6KJP22W": "8x10ft",
        },
        "B0FX2828C9": {
            "B0FX9VSXHD": "3x7.5ft",
            "B0FX9TDRPR": "2x5.2ft",
            "B0FX9XMBJK": "2.6x6.6ft",
            "B0FX9VGDCY": "2.6x6ft",
            "B0FX2BH6J8": "7.5x7.5ft",
            "B0FX4XBY4Y": "7.5x10ft",
            "B0FX9XZTFD": "7.5x13ft",
            "B0FX9W5HGP": "7.5x20ft",
        },
    },
    "不带支架": {
        "B0H36GPHVH": {
            "B0H36MBKJT": "5x7.5ft",
            "B0H39L1D7C": "7.5x7.5ft",
            "B0H39PYQNN": "10x7.5ft",
            "B0H3BDBVZ7": "10x10ft",
            "B0H3B9D1WR": "12x7.5ft",
            "B0H3B5WWLF": "15x7.5ft",
            "B0H39P95SG": "20x7.5ft",
        },
        "B0G6KJQPHK": {
            "B0G6JZKD13": "8x8ft",
            "B0G6KQGJC9": "8x10ft",
        },
        "B0FX2828C9": {
            "B0FX9W6684": "3x7.5ft",
            "B0FX9YPQ2C": "2x5.2ft",
            "B0FX9XMP9D": "2.6x6.6ft",
            "B0FX9Y2DXM": "2.6x6ft",
            "B0FX29VVBH": "7.5x7.5ft",
            "B0FX9W3MJL": "7.5x10ft",
            "B0FX9XMLPW": "7.5x13ft",
            "B0FX9XHQ7F": "7.5x20ft",
        },
    },
}

POP_UP_DISPLAY_CHILD_TO_PARENT: dict[str, str] = {}
POP_UP_DISPLAY_SIZE_BY_ASIN: dict[str, str] = {}
POP_UP_DISPLAY_STAND_BY_ASIN: dict[str, str] = {}

for stand_type, parent_children in POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND.items():
    for parent_asin, children in parent_children.items():
        for child_asin, size in children.items():
            POP_UP_DISPLAY_CHILD_TO_PARENT[child_asin] = parent_asin
            POP_UP_DISPLAY_SIZE_BY_ASIN[child_asin] = size
            POP_UP_DISPLAY_STAND_BY_ASIN[child_asin] = stand_type


POP_UP_DISPLAY_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "printing_sides": (
        "Single/Double-Sided Printing Options",
        "Single/Double-Sided Printing Option",
        "Double-sided Printing Options",
        "Double-sided Printing Option",
    ),
    "same_design": (
        "Is The Back Side Using The Same Design As The Front Side?",
        "Is The Panel 2 Using The Same Design As Panel 1?",
    ),
    "side_panels": ("Side Panels Options", "Side Panels Option"),
    "fabric_panel_quantity": ("Fabric Panel Quantity Options", "Fabric Panel Quantity Option"),
    "frame": ("Frame Options", "Frame Option"),
    "led": ("LED Light Options", "LED Light Option"),
    "proof": (
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
        "Proof Option",
    ),
    "contact": (POP_UP_DISPLAY_EMAIL_PROMPT, POP_UP_DISPLAY_PHONE_PROMPT),
}

POP_UP_DISPLAY_PARENTS_WITH_PRINTING_SIDES = {"B0H36GPHVH", "B0FX2828C9"}
POP_UP_DISPLAY_PARENTS_WITH_LED = {"B0H36GPHVH", "B0FX2828C9"}
POP_UP_DISPLAY_PARENT_WITH_SIDE_PANELS = "B0H36GPHVH"
POP_UP_DISPLAY_PARENT_WITH_FABRIC_PANEL_QUANTITY = "B0G6KJQPHK"

POP_UP_DISPLAY_OPTIONS_BY_PARENT: dict[str, dict[str, dict[str, str]]] = {
    "B0H36GPHVH": {
        "printing_sides": {
            "single-sided": "单面",
            "single sided": "单面",
            "double-sided": "双面",
            "double sided": "双面",
        },
        "same_design": {
            "yes,using same design for back side": "相同设计",
            "no,using different design for back side": "不同设计",
        },
        "side_panels": {
            "no endcaps": "无侧边",
            "endcaps": "有侧边",
        },
        "led": {
            "yes,i need 1-pack led light": "1组LED",
            "yes,i need a 2-pack led light": "2组LED",
            "no,i don't need a led light": "",
            "no,i don’t need a led light": "",
        },
        "proof": {
            "straight to production": "直接制作",
            "online proof (48h no reply=ship)": "在线检查",
        },
    },
    "B0G6KJQPHK": {
        "same_design": {
            "yes,using same design for panel 2": "相同设计",
            "no,using different design for panel 2": "不同设计",
        },
        # “1/2个布面”是 B0G6KJQPHK 独有字段，位置在侧边之后、LED 之前。
        "fabric_panel_quantity": {
            "1 panel (single-sided print)": "1个布面",
            "2 panels (single-sided print)": "2个布面",
        },
        "proof": {
            "straight to production": "直接制作",
            "online proof (48h no reply=ship)": "在线检查",
        },
    },
    "B0FX2828C9": {
        "printing_sides": {
            "single-sided": "单面",
            "single sided": "单面",
            "double-sided": "双面",
            "double sided": "双面",
        },
        "same_design": {
            "yes,using same design for back side": "相同设计",
            "no,using different design for back side": "不同设计",
        },
        "led": {
            "yes,i need 1-pack led light": "1组LED",
            "yes,i need a 2-pack led light": "2组LED",
            "yes,i need a 3-pack led light": "3组LED",
            "yes,i need a 4-pack led light": "4组LED",
            "no,i don't need a led light": "",
            "no,i don’t need a led light": "",
            "no,i don't need a 2-pack led light": "",
            "no,i don’t need a 2-pack led light": "",
        },
        "proof": {
            "straight to production": "直接制作",
            "online proof (48h no reply=ship)": "在线检查",
        },
    },
}

# Frame Options 只在“不带支架”ASIN 上有意义；No Frame 第二行为空，所以跳过不报错。
POP_UP_DISPLAY_FRAME_OPTIONS = {
    "no frame": "",
    "adjustable frame": "可调节框架",
    "aluminum frame": "铝制框架",
}


def normalize_pop_up_display_title(title: str | None) -> str:
    """归一化 JSON 标题，兼容大小写、冒号和多空格差异。"""

    normalized = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    return normalized.rstrip(":：")


def normalize_pop_up_display_option_value(value: str | None) -> str:
    """归一化选项值，避免句点、逗号空格和弯引号影响规则匹配。"""

    normalized = str(value or "").replace("’", "'").replace("‘", "'")
    normalized = normalized.replace("–", "-").replace("—", "-")
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    normalized = re.sub(r"\s*,\s*", ",", normalized)
    return normalized.rstrip(".")


def get_pop_up_display_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...] | list[str]) -> str | None:
    """按标题别名从 zip JSON pairs 中取值；业务数据不再来自 ERP 浮窗文本。"""

    alias_keys = {normalize_pop_up_display_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_pop_up_display_title(title), alias_keys):
            return value
    return None


def is_pop_up_display_asin(asin: str | None) -> bool:
    """判断 ASIN 是否属于拉网展架。"""

    normalized = normalize_asin(asin)
    return bool(normalized and (normalized in POP_UP_DISPLAY_PARENT_ASINS or normalized in POP_UP_DISPLAY_CHILD_TO_PARENT))


def find_pop_up_display_parent_asin(asin: str | None) -> str | None:
    """根据拉网展架子 ASIN 返回父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    if normalized in POP_UP_DISPLAY_PARENT_ASINS:
        return normalized
    return POP_UP_DISPLAY_CHILD_TO_PARENT.get(normalized)


def get_pop_up_display_size(asin: str | None) -> str | None:
    """根据子 ASIN 返回拉网展架尺寸。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return POP_UP_DISPLAY_SIZE_BY_ASIN.get(normalized)


def get_pop_up_display_stand_type(asin: str | None) -> str | None:
    """根据子 ASIN 返回“带支架/不带支架”。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return POP_UP_DISPLAY_STAND_BY_ASIN.get(normalized)


def get_pop_up_display_product_name(parent_asin: str | None, asin: str | None = None) -> str | None:
    """根据父/子 ASIN 返回生产文件夹中的展架品名。"""

    normalized_asin = normalize_asin(asin)
    if normalized_asin and normalized_asin in POP_UP_DISPLAY_PRODUCT_NAME_BY_CHILD:
        return POP_UP_DISPLAY_PRODUCT_NAME_BY_CHILD[normalized_asin]
    normalized_parent = normalize_asin(parent_asin)
    if not normalized_parent:
        return None
    return POP_UP_DISPLAY_PRODUCT_NAME_BY_PARENT.get(normalized_parent)


def get_pop_up_display_option_rules(parent_asin: str, group: str) -> dict[str, str]:
    """按父 ASIN 和规则组获取选项映射。"""

    normalized_parent = normalize_asin(parent_asin) or parent_asin
    if group == "frame":
        return POP_UP_DISPLAY_FRAME_OPTIONS
    return POP_UP_DISPLAY_OPTIONS_BY_PARENT.get(normalized_parent, {}).get(group, {})


def match_pop_up_display_product(asin: str | None) -> PopUpDisplayProductMatch | None:
    """返回拉网展架产品匹配信息。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    parent_asin = find_pop_up_display_parent_asin(normalized)
    size = get_pop_up_display_size(normalized)
    stand_type = get_pop_up_display_stand_type(normalized)
    if not parent_asin or not size or not stand_type:
        return None
    return PopUpDisplayProductMatch(
        product_type=PRODUCT_TYPE_POP_UP_DISPLAYS,
        asin=normalized,
        parent_asin=parent_asin,
        size=size,
        stand_type=stand_type,
    )
