from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

EMAIL_PROMPT = "Please provide an email address to confirm customization design and details or for emergencies."
PHONE_PROMPT = "Please provide a texting number to confirm customization design and details or for emergencies."

NEW_TENT_PARENT_ASIN = "B0H5Q8N8NV"

TENT_WALL_STRATEGY_LEGACY = "legacy"
TENT_WALL_STRATEGY_DIRECTIONAL = "directional"
TENT_WALL_STRATEGY_NONE = "none"


@dataclass(frozen=True)
class TentDirectionalWallSpec:
    """一个由独立 Amazon 选项控制的方向墙。"""

    option_title: str
    direction: str
    wall_kind: str

    def component(self, *, double_sided: bool) -> str:
        printing = "双面" if double_sided else "单面"
        wall = "全墙" if self.wall_kind == "full_wall" else "半墙"
        return f"{self.direction}{printing}{wall}"


@dataclass(frozen=True)
class TentBundledFlagSpec:
    """帐篷套餐内固定数量旗帜的选项映射。"""

    option_title: str
    option_components: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TentPackageRule:
    """单个帐篷子 ASIN 的固定套餐事实。

    墙体策略和捆绑旗帜都集中在产品目录中，文件夹和 SKU
    流程只消费这些事实，避免为每个新 ASIN 增加分散的条件分支。
    """

    package_code: str
    top_size: str
    wall_strategy: str
    legacy_wall_value: str | None = None
    directional_walls: tuple[TentDirectionalWallSpec, ...] = ()
    bundled_flag: TentBundledFlagSpec | None = None

    def __post_init__(self) -> None:
        if self.wall_strategy not in {
            TENT_WALL_STRATEGY_LEGACY,
            TENT_WALL_STRATEGY_DIRECTIONAL,
            TENT_WALL_STRATEGY_NONE,
        }:
            raise ValueError(f"unknown tent wall strategy: {self.wall_strategy}")
        if self.wall_strategy == TENT_WALL_STRATEGY_LEGACY and not self.legacy_wall_value:
            raise ValueError("legacy tent wall strategy requires legacy_wall_value")
        if self.wall_strategy == TENT_WALL_STRATEGY_DIRECTIONAL and not self.directional_walls:
            raise ValueError("directional tent wall strategy requires directional_walls")


_BACK_FULL_WALL = TentDirectionalWallSpec("Back Wall Options", "背", "full_wall")
_THREE_DIRECTIONAL_FULL_WALLS = (
    TentDirectionalWallSpec("Left Wall Options", "左", "full_wall"),
    TentDirectionalWallSpec("Right Wall Options", "右", "full_wall"),
    _BACK_FULL_WALL,
)
_FOUR_DIRECTIONAL_FULL_WALLS = (
    TentDirectionalWallSpec("Front Wall Options", "前", "full_wall"),
    *_THREE_DIRECTIONAL_FULL_WALLS,
)
_THREE_DIRECTIONAL_HALF_WALLS = (
    TentDirectionalWallSpec("Double-sided Printing Options - Left Half Wall", "左", "half_wall"),
    TentDirectionalWallSpec("Double-sided Printing Options - Right Half Wall", "右", "half_wall"),
    TentDirectionalWallSpec("Double-sided Printing Options - Back Half Wall", "背", "half_wall"),
)

_TEARDROP_FLAG_SPEC = TentBundledFlagSpec(
    option_title="Custom Feather/Teardrop Flag",
    option_components=(
        (
            "2-Sided Printing: 6.9ft Same Design both Sides",
            "2套（0.75x1.65m双面水滴旗+相同设计+全玻璃纤维杆+连接件+夹具）",
        ),
        (
            "2-Sided Printing: 6.9ft Different Design",
            "2套（0.75x1.65m双面水滴旗+不同设计+全玻璃纤维杆+连接件+夹具）",
        ),
        (
            "2-Sided Printing: 9.8ft Same Design both Sides",
            "2套（0.95x2.3m双面水滴旗+相同设计+全玻璃纤维杆+连接件+夹具）",
        ),
        (
            "2-Sided Printing: 9.8ft Different Design",
            "2套（0.95x2.3m双面水滴旗+不同设计+全玻璃纤维杆+连接件+夹具）",
        ),
    ),
)

_FEATHER_FLAG_SPEC = TentBundledFlagSpec(
    option_title="Custom Feather/Teardrop Flag",
    option_components=(
        (
            "2-Sided Printing: 1.64x6.56ft",
            "2套（0.5x2m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
        ),
        (
            "2-Sided Printing: 1.97x7.8ft",
            "2套（0.6x2.5m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
        ),
        (
            "2-Sided Printing: 2.3x11.15ft",
            "2套（0.7x3.4m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
        ),
        (
            "2-Sided Printing: 2.62x13.45ft",
            "2套（0.8x4.1m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
        ),
    ),
)

# 新父体的 13 个套餐均以子 ASIN 作为固定组成的唯一权威来源。
# 新增子 ASIN 时只需扩展此表以及父子关系，无需在文件夹流程中增加 ASIN if/else。
TENT_PACKAGE_RULES_BY_ASIN: dict[str, TentPackageRule] = {
    "B0H5TV9LXK": TentPackageRule(
        "A",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_LEGACY,
        legacy_wall_value="1 Full and 2 Half Walls with Rails",
    ),
    "B0H6PW43V1": TentPackageRule(
        "B",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=(_BACK_FULL_WALL,),
    ),
    "B0H6PN5HTB": TentPackageRule(
        "C",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_LEGACY,
        legacy_wall_value="1 Full and 2 Half Walls with Rails",
        bundled_flag=_TEARDROP_FLAG_SPEC,
    ),
    "B0H6PSSCVM": TentPackageRule(
        "D",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=_FOUR_DIRECTIONAL_FULL_WALLS,
    ),
    "B0H6PQMPSW": TentPackageRule(
        "E",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=_THREE_DIRECTIONAL_FULL_WALLS,
    ),
    "B0H6PNN62J": TentPackageRule(
        "F",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_LEGACY,
        legacy_wall_value="1 Full and 2 Half Walls with Rails",
        bundled_flag=_FEATHER_FLAG_SPEC,
    ),
    "B0H6PNRVV6": TentPackageRule("G", "3x3m帐篷顶", TENT_WALL_STRATEGY_NONE),
    "B0H6PML9SS": TentPackageRule(
        "H",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=(_BACK_FULL_WALL,),
    ),
    "B0H6PLYKY4": TentPackageRule(
        "I",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_LEGACY,
        legacy_wall_value="1 Full and 2 Half Walls with Rails",
    ),
    "B0H6PS9BT5": TentPackageRule(
        "J",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=_THREE_DIRECTIONAL_FULL_WALLS,
    ),
    "B0H6PXWBCH": TentPackageRule(
        "K",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_LEGACY,
        legacy_wall_value="1 Half Wall With Rail",
    ),
    "B0H6PNDK4K": TentPackageRule("L", "3x3m帐篷顶", TENT_WALL_STRATEGY_NONE),
    "B0H6PTLMZ1": TentPackageRule(
        "M",
        "3x3m帐篷顶",
        TENT_WALL_STRATEGY_DIRECTIONAL,
        directional_walls=_THREE_DIRECTIONAL_HALF_WALLS,
    ),
}

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
    NEW_TENT_PARENT_ASIN: tuple(TENT_PACKAGE_RULES_BY_ASIN),
}

TENT_CONTACT_PROMPTS_BY_PARENT: dict[str, tuple[str, str]] = {
    "B0FTV6XDGG": (EMAIL_PROMPT, PHONE_PROMPT),
    "B0CZNZVG26": (PHONE_PROMPT, EMAIL_PROMPT),
    "B0F5CTQXG1": (EMAIL_PROMPT, PHONE_PROMPT),
    "B0D6XW7V9T": (EMAIL_PROMPT, PHONE_PROMPT),
    NEW_TENT_PARENT_ASIN: (EMAIL_PROMPT, PHONE_PROMPT),
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
    **{asin: rule.top_size for asin, rule in TENT_PACKAGE_RULES_BY_ASIN.items()},
}

WALL_ONLY_ASIN_KIND: dict[str, str] = {
    # 这两个 ASIN 属于独立墙体商品，不是帐篷顶套餐，文件夹名不能生成“帐篷顶”片段。
    "B0D6KZ7G88": "full_wall",
    "B0D6XWP8YN": "half_wall",
}

# 当前没有需要无视客选物流、默认按加急处理的帐篷 ASIN。
# B0CRRGTPFH 已恢复为普通发货；只有订单物流本身明确为 Expedited/加急时才按加急处理。
DEFAULT_EXPEDITED_TENT_ASINS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TentProductMatch:
    asin: str
    parent_asin: str
    contact_prompts: tuple[str, str]


def normalize_asin(value: str | None) -> str | None:
    """规范化ASIN，便于后续匹配和比较。"""
    if not value:
        return None
    asin = value.strip().upper()
    return asin if ASIN_RE.fullmatch(asin) else None


def extract_asins(texts: str | Iterable[str]) -> list[str]:
    """从输入内容中提取ASIN集合。"""
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
    """查找帐篷父ASIN并返回匹配结果。"""
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TENT_ASIN_TO_PARENT_ASIN.get(normalized)


def is_tent_asin(asin: str | None) -> bool:
    """判断帐篷ASIN是否满足业务条件。"""
    return find_tent_parent_asin(asin) is not None


def get_tent_top_size(asin: str | None) -> str | None:
    """获取帐篷 ASIN 对应的顶布尺寸。"""
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TENT_ASIN_TO_TOP_SIZE.get(normalized)


def get_tent_package_rule(asin: str | None) -> TentPackageRule | None:
    """返回只对特定子 ASIN 生效的固定套餐规则。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return TENT_PACKAGE_RULES_BY_ASIN.get(normalized)


def get_wall_only_asin_kind(asin: str | None) -> str | None:
    """判断侧墙单品 ASIN 属于全高墙还是半高墙。"""
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return WALL_ONLY_ASIN_KIND.get(normalized)


def is_default_expedited_tent_asin(asin: str | None) -> bool:
    """判断 ASIN 是否默认按加急物流处理。"""

    normalized = normalize_asin(asin)
    return bool(normalized and normalized in DEFAULT_EXPEDITED_TENT_ASINS)


def match_tent_product(texts: str | Iterable[str]) -> TentProductMatch | None:
    """匹配帐篷产品并返回结构化结果。"""
    for asin in extract_asins(texts):
        parent_asin = find_tent_parent_asin(asin)
        if parent_asin:
            return TentProductMatch(
                asin=asin,
                parent_asin=parent_asin,
                contact_prompts=TENT_CONTACT_PROMPTS_BY_PARENT[parent_asin],
            )
    return None
