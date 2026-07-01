"""喷绘(vinyl_banners)商品识别与规则配置。

规则来源：XMind「生成文件夹」大纲节点与用户提供的父/子 ASIN 校验表。
喷绘尺寸由子 ASIN 决定，定制化 JSON 里通常没有尺寸；因此必须先通过子 ASIN 查尺寸映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..rule_matching import normalized_key_matches_any
from .tents import normalize_asin


PRODUCT_TYPE_VINYL_BANNERS = "vinyl_banners"

VINYL_BANNER_CONTACT_PROMPT = (
    "Please provide a texting number/email to contact you for emergencies "
    "(low quality image, etc)"
)


@dataclass(frozen=True)
class VinylBannerProductMatch:
    """喷绘商品匹配结果。"""

    product_type: str
    asin: str
    parent_asin: str
    size: str


VINYL_BANNER_PARENT_ASINS = {
    "B0CMTSMLJT",
    "B0CMTVM5HS",
    "B0CMTT81C2",
    "B0CX56LVTB",
}


# 同一个父 ASIN 下的子 ASIN 必须按真实父子关系维护，不能因为 OCR 把 1/I、0/O 读错后错挂父类。
# 父子 ASIN 关系以用户提供的校验表为准；尺寸来自 PDF 内嵌原始 PNG 中每个子 ASIN 的第二行。
VINYL_BANNER_PARENT_TO_CHILD_SIZE: dict[str, dict[str, str]] = {
    "B0CMTSMLJT": {
        "B0CR2SLGHR": "8x80ft",
        "B0CR2ZTTNN": "7x14ft",
        "B0CR2W965D": "6x12ft",
        "B0CMQK16Q2": "8x60ft",
        "B0CR2TLS7W": "7x30ft",
        "B0CR2SLSJY": "4x20ft",
        "B0CR31PFLR": "3x30ft",
        "B0CR2XN6BG": "7x20ft",
        "B0CR35R6JT": "8x20ft",
        "B0CR34YND5": "4x40ft",
        "B0CR2YXMFM": "4x16ft",
        "B0CR2TM3WC": "3x25ft",
        "B0CR328ZR8": "4x12ft",
        "B0CR36B2HG": "4x25ft",
        "B0CR2Z1H7G": "6x45ft",
        "B0CR37TQ68": "7x25ft",
        "B0CR337FMZ": "3x20ft",
        "B0CR37P6JQ": "7x70ft",
        "B0CR37GQ8Q": "5x30ft",
        "B0CR2SH9KX": "5x20ft",
        "B0CR3382BJ": "5x40ft",
        "B0CR326T9C": "6x16ft",
        "B0CR383W6C": "5x6ft",
        "B0CR2W75M9": "6x6ft",
        "B0CR358N7V": "4x8ft",
        "B0CR2WP2HN": "6x10ft",
        "B0CR2RG8FQ": "3x9ft",
        "B0CR3197NL": "5x10ft",
        "B0CR2Y8MYN": "4x10ft",
        "B0CR38YSLP": "3x10ft",
        "B0CR36S1YJ": "3x12ft",
        "B0CR2XPH5B": "5x8ft",
        "B0CR2XLR94": "3x15ft",
        "B0CMQJ9S4N": "7x60ft",
        "B0CMQHGRQV": "6x30ft",
        "B0CMQGWKY7": "8x16ft",
        "B0CMQGKFY1": "3x5ft",
        "B0CMQGCZL1": "5x50ft",
        "B0CMQJK1B8": "8x45ft",
        "B0CMQHDPPP": "8x30ft",
        "B0CMQG9W4N": "6x20ft",
        "B0CMQFY1M1": "6x50ft",
        "B0CMQDQ47S": "7x40ft",
        "B0CMQG73C1": "5x25ft",
        "B0CMQF5S38": "4x30ft",
        "B0CMQHWC68": "8x25ft",
        "B0CMQHPGSY": "8x40ft",
        "B0CMQHR8KC": "7x35ft",
        "B0CMQG7JKS": "7x50ft",
        "B0CMQF85X7": "4x6ft",
        "B0CMQDFQVB": "8x70ft",
        "B0CMQJ3GJC": "6x40ft",
        "B0CMQGPS4G": "8x35ft",
        "B0CMQHSVV3": "5x35ft",
        "B0CMQG7MKX": "6x35ft",
        "B0CMQHQFW4": "4x35ft",
        "B0CMQFYGFK": "3x4ft",
        "B0CMQJ99D4": "6x25ft",
        "B0CMQGXLZN": "3x6ft",
        "B0CMQFWQM2": "7x45ft",
        "B0CMQGLN9N": "5x15ft",
        "B0CMQK4KBF": "8x50ft",
        "B0CMQHXR1F": "5x45ft",
    },
    "B0CMTVM5HS": {
        "B0DQJW6C1T": "4x6ft",
        "B0DNTWF7YP": "3x5ft",
        "B0DHVC3ZJS": "10x10ft",
        "B0DHVDV9BD": "8x8ft",
        "B0DHVCYKRB": "8x10ft",
        "B0DHVD4HVZ": "1x9ft",
        "B0DHV9C391": "2x10ft",
        "B0DHV7828Y": "1x8ft",
        "B0DH2VYFQ5": "1x2ft",
        "B0CXD63XBS": "1x6ft",
        "B0CWLC2TH6": "1x4ft",
        "B0CWL9NZRK": "2x4ft",
        "B0CWL99XJS": "2x2ft",
        "B0CWLCMPDZ": "1x6ft",
        "B0CWL7824Z": "2x3ft",
        "B0CR2RCT9Q": "5x20ft",
        "B0CR318DBT": "4x25ft",
        "B0CMQKD7KD": "4x15ft",
        "B0CMQD3PH7": "9x9ft",
        "B0CMQFV3TR": "4x20ft",
        "B0CMQHXD62": "3x10ft",
        "B0CMQFZFZQ": "2x8ft",
        "B0CMQK5NPN": "3x7ft",
        "B0CMQHKD5Y": "7x7ft",
        "B0CMQJ1PJP": "4x12ft",
        "B0CMQDVH2S": "3x4ft",
        "B0CMQJXLTM": "3x12ft",
        "B0CMQK2MFT": "6x15ft",
        "B0CMQH9R5Q": "3x3ft",
        "B0CMQJS213": "3x20ft",
        "B0CMQFKYZZ": "3x15ft",
        "B0CMQGYBSZ": "4x4ft",
        "B0CMQDF62Q": "5x6ft",
        "B0CMQJRCS8": "6x10ft",
        "B0CMQJJ129": "6x6ft",
        "B0CMQH2MW4": "2x5ft",
        "B0CMQJK55Y": "5x8ft",
        "B0CMQGXH29": "5x5ft",
        "B0CMQGQNKK": "5x30ft",
        "B0CMQDR4B4": "4x5ft",
        "B0CMQHYDQB": "2x7ft",
        "B0CMQFL48P": "2x6ft",
        "B0CMQK44YP": "5x15ft",
        "B0CMQHMQ2T": "3x6ft",
        "B0CMQGD8X5": "4x9ft",
        "B0CMQJKNH1": "3x9ft",
        "B0CMQGCBTW": "4x8ft",
        "B0CMQHDMFB": "5x25ft",
        "B0CMQGQCK4": "4x10ft",
        "B0CMQHC2Y9": "3x8ft",
        "B0CMQFXVV6": "5x10ft",
        "B0CMQHSJB5": "4x7ft",
    },
    "B0CMTT81C2": {
        "B0CMQFXVV8": "6x6ft",
        "B0CR318PBN": "3x15ft",
        "B0CMQHKHHM": "4x8ft",
        "B0CMQFGSZC": "3x10ft",
        "B0CMQG89Q9": "6x20ft",
        "B0CMQFYRSF": "4x7ft",
        "B0CMQF9MJ6": "5x30ft",
        "B0CMQH18YW": "4x12ft",
        "B0CMQHMYXR": "3x6ft",
        "B0CMQKP2N4": "4x10ft",
        "B0CMQJXLPF": "1x4ft",
        "B0CMQGXS51": "2x4ft",
        "B0CMQJ2DWZ": "4x6ft",
        "B0CMQHZ86T": "3x4ft",
        "B0CMQKTP18": "2x7ft",
        "B0CMQGCNM4": "5x8ft",
        "B0CMQHB7FR": "2x8ft",
        "B0CMQHMPWS": "4x4ft",
        "B0CMQGZ9H1": "3x8ft",
        "B0CMQGGCF9": "5x15ft",
        "B0CMQHFSW7": "4x9ft",
        "B0CMQDMR41": "5x6ft",
        "B0CMQHQT9H": "5x20ft",
        "B0CMQF85XH": "5x5ft",
        "B0CMQHRLPJ": "4x5ft",
        "B0CMQFKNJG": "2x5ft",
        "B0CMQGK23M": "3x9ft",
        "B0CMQJSDDS": "3x12ft",
        "B0CMQFWZRJ": "4x20ft",
        "B0CMQFRTWQ": "2x6ft",
        "B0CMQHMY7R": "2x3ft",
        "B0CMQHG17N": "3x3ft",
        "B0CMQFMM2X": "6x10ft",
        "B0CMQGH154": "4x15ft",
        "B0CMQHDHB6": "3x7ft",
        "B0CMQF85X1": "9x9ft",
        "B0CMQJPC1L": "3x5ft",
        "B0CMQKPSF3": "7x7ft",
        "B0CMQFSW41": "2x2ft",
        "B0CMQF37KZ": "3x20ft",
        "B0CMQJYZJY": "6x15ft",
        "B0CMQJ5TSY": "5x10ft",
        "B0CMQHBKCH": "5x25ft",
        "B0CMQJVZBS": "8x8ft",
        "B0CMQH5H9R": "4x25ft",
        "B0CMQDDM5H": "1x6ft",
    },
    "B0CX56LVTB": {
        "B0CXDPL9NS": "3x25ft",
        "B0CXDD435L": "3x12ft",
        "B0CXDLX4VP": "3x15ft",
        "B0CXDS8SXT": "3x30ft",
        "B0CXDMWH9T": "3x20ft",
        "B0CX5634K3": "3x9ft",
        "B0CX57JDN5": "4x6ft",
        "B0CX4XN2ZR": "3x6ft",
        "B0CX4W1F5J": "3x4ft",
        "B0CX54SFVB": "3x5ft",
    },
}

VINYL_BANNER_SIZE_BY_ASIN: dict[str, str] = {
    asin: size
    for children in VINYL_BANNER_PARENT_TO_CHILD_SIZE.values()
    for asin, size in children.items()
}

VINYL_BANNER_CHILD_TO_PARENT: dict[str, str] = {
    asin: parent
    for parent, children in VINYL_BANNER_PARENT_TO_CHILD_SIZE.items()
    for asin in children
}


VINYL_BANNER_DEFAULT_PRINTED_SIDES_BY_ASIN: dict[str, str] = {
    "B0CMQJPC1L": "Double-Sided",
}


VINYL_BANNER_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "printed_sides": ("Printed Sides",),
    "same_design": (
        "Is The Front Side Using The Same Design As The Back Side?",
        "Is The Back Side Using The Same Design As The Front Side?",
        "Same Design Option",
    ),
    "surface_material": (
        "Material Type",
        "Surface Material",
        "Surface Material Option",
    ),
    # Hanging Option(s) 表示标题可能是单数也可能是复数；匹配 JSON pairs 时需要同时查。
    "hanging": ("Hanging Option", "Hanging Options", "Hanging Option(s)"),
    "edge": ("Edge Option", "Edge Options", "Edge Option(s)"),
    "packaging": ("Packaging Method", "Packaging Methods", "Packaging Method(s)"),
    "accessories": ("Accessories",),
    "proof": (
        "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).",
        "Proof Option",
    ),
    "contact": (VINYL_BANNER_CONTACT_PROMPT,),
}


VINYL_BANNER_COMMON_OPTION_RULES: dict[str, dict[str, str]] = {
    "printed_sides": {
        "single-sided": "单面",
        "single sided": "单面",
        "single side": "单面",
        "1 side": "单面",
        "1-sided": "单面",
        "double-sided": "双面",
        "double sided": "双面",
        "double side": "双面",
        "2 sides": "双面",
        "2-sided": "双面",
    },
    "same_design": {
        "yes, using same design for back side": "双面相同",
        "yes, using same design": "双面相同",
        "same design": "双面相同",
        "no, using different design for back side": "双面不同",
        "no, using different design": "双面不同",
        "different design": "双面不同",
    },
    "surface_material": {
        # 喷绘的 15oz sturdy vinyl 在生产命名中用“550”表示；
        # 只有命中该材质选项时才追加 550，其它材质不写入文件夹名。
        "studry 15 oz. sturdy vinyl": "550",
        "sturdy 15 oz. sturdy vinyl": "550",
        "15 oz. sturdy vinyl": "550",
        "standard 13 oz. lightweight vinyl": "",
    },
    "hanging": {
        "no grommet": "无扣",
        "no grommets": "无扣",
        "grommets every 2 ft": "每60cm打扣",
        "grommets every 2ft": "每60cm打扣",
        "grommets every 2~3ft": "每60cm打扣",
        "grommets every 2~3 ft": "每60cm打扣",
        "grommets every 2-3ft": "每60cm打扣",
        "grommets every 2-3 ft": "每60cm打扣",
        "grommets every 2 to 3ft": "每60cm打扣",
        "grommets every 2 to 3 ft": "每60cm打扣",
        "grommets every 60cm": "每60cm打扣",
        "pole pocket top only": "顶部缝套筒",
        "pole pocket bottom only": "底部缝套筒",
        "pole pocket top + bottom": "上下缝套筒",
        "pole pocket top and bottom": "上下缝套筒",
        "pole pocket left + right": "左右缝套筒",
        "pole pocket left and right": "左右缝套筒",
    },
    "edge": {
        "no edge": "不折边",
        "no hem": "不折边",
        "welded edge": "折边胶粘",
        "welded edges": "折边胶粘",
        "hemmed edges": "折边胶粘",
        "sewn edge": "踩线折边",
        "sewn edges": "踩线折边",
    },
    "packaging": {
        "folded packaging": "折叠装",
        "folded": "折叠装",
        "rolled packaging": "卷装",
        "rolled packaging expedited shipping": "卷装（加急）",
        "rolled packaging+expedited shipping": "卷装（加急）",
        "rolled": "卷装",
    },
    "accessories": {
        "zip ties (enough for use)": "扎带",
        "zip ties": "扎带",
        "nylon rope (15ft/0.3inch)": "尼龙绳(15ft 0.3inch)",
        "nylon rope (30ft/0.3inch)": "尼龙绳(30ft 0.3inch)",
    },
    "proof": {
        "straight to production": "直接制作",
        "online proof (48h no reply=ship)": "在线检查",
        "online proof": "在线检查",
    },
}


# 选项第二行没有中文时，说明该选项不参与文件夹命名；这种情况应跳过，不应报规则缺失。
VINYL_BANNER_OPTIONS_BY_PARENT: dict[str, dict[str, dict[str, str]]] = {
    parent: VINYL_BANNER_COMMON_OPTION_RULES for parent in VINYL_BANNER_PARENT_ASINS
}


def normalize_customization_title(title: str) -> str:
    """归一化 JSON 中的定制化标题，便于匹配大小写、空格和单复数差异。"""

    normalized = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    normalized = normalized.replace("option(s)", "options")
    normalized = normalized.replace("method(s)", "methods")
    return normalized.rstrip(":：")


def normalize_option_value(value: str) -> str:
    """归一化定制化选项值，避免大小写或多余空格影响规则匹配。"""

    normalized = str(value or "").replace("–", "-").replace("—", "-").replace("～", "~")
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized.rstrip(".")


def get_pair_by_title_aliases(pairs: dict[str, str], aliases: tuple[str, ...] | list[str]) -> str | None:
    """按多个标题别名从 JSON pairs 中取值。"""

    alias_keys = {normalize_customization_title(alias) for alias in aliases}
    for title, value in pairs.items():
        if normalized_key_matches_any(normalize_customization_title(title), alias_keys):
            return value
    return None


def is_vinyl_banner_asin(asin: str | None) -> bool:
    """判断 ASIN 是否属于喷绘商品。

    这里使用父子关系表而不是尺寸表做识别，因为某些新增子 ASIN 可能已确认归属，
    但尺寸节点尚未能从 XMind 中安全读取；这类订单应进入流程后明确报缺尺寸规则。
    """

    normalized = normalize_asin(asin)
    return bool(normalized and (normalized in VINYL_BANNER_PARENT_ASINS or normalized in VINYL_BANNER_CHILD_TO_PARENT))


def find_vinyl_banner_parent_asin(asin: str | None) -> str | None:
    """根据喷绘子 ASIN 返回父 ASIN。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    if normalized in VINYL_BANNER_PARENT_ASINS:
        return normalized
    return VINYL_BANNER_CHILD_TO_PARENT.get(normalized)


def get_vinyl_banner_size(asin: str | None) -> str | None:
    """根据喷绘子 ASIN 返回尺寸。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return VINYL_BANNER_SIZE_BY_ASIN.get(normalized)


def get_vinyl_banner_default_printed_sides(asin: str | None) -> str | None:
    """根据固定单双面喷绘子 ASIN 返回默认 Printed Sides。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return VINYL_BANNER_DEFAULT_PRINTED_SIDES_BY_ASIN.get(normalized)


def match_vinyl_banner_product(asin: str | None) -> VinylBannerProductMatch | None:
    """返回喷绘产品匹配信息。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    parent_asin = find_vinyl_banner_parent_asin(normalized)
    if not parent_asin:
        return None
    return VinylBannerProductMatch(
        product_type=PRODUCT_TYPE_VINYL_BANNERS,
        asin=normalized,
        parent_asin=parent_asin,
        size=get_vinyl_banner_size(normalized) or "",
    )


def get_vinyl_banner_option_rules(parent_asin: str, group: str) -> dict[str, str]:
    """按父 ASIN 和规则组获取喷绘选项映射。"""

    normalized_parent = normalize_asin(parent_asin) or parent_asin
    return VINYL_BANNER_OPTIONS_BY_PARENT.get(normalized_parent, {}).get(group, {})
