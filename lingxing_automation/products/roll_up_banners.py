from __future__ import annotations

import re
from dataclasses import dataclass

from .car_magnets import CAR_MAGNET_CONTACT_PROMPT
from .pop_up_displays import POP_UP_DISPLAY_EMAIL_PROMPT
from .posters import POSTER_CONTACT_PROMPTS
from .tents import extract_asins, normalize_asin


PRODUCT_TYPE_ROLL_UP_BANNERS = "roll_up_banners"
ROLL_UP_BANNER_CONTACT_PROMPT = CAR_MAGNET_CONTACT_PROMPT
DESKTOP_ROLL_UP_BANNER_PHONE_PROMPT = POSTER_CONTACT_PROMPTS[1]
DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT = POP_UP_DISPLAY_EMAIL_PROMPT
ROLL_UP_BANNER_PROOF_TITLE = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"
ROLL_UP_BANNER_PRINTING_PROCESS_TITLE = "Printing Process"


@dataclass(frozen=True)
class RollUpBannerProductMatch:
    asin: str
    parent_asin: str
    product_type: str = PRODUCT_TYPE_ROLL_UP_BANNERS
    contact_prompts: tuple[str, ...] = (ROLL_UP_BANNER_CONTACT_PROMPT,)


ROLL_UP_BANNER_PARENT_ASIN = "B0CMPYV549"
DESKTOP_ROLL_UP_BANNER_PARENT_ASINS = {"B0D1VB6YF1", "B0D1TW6RDZ"}

# 易拉宝和桌面易拉宝共用同一个产品族；每个子 ASIN 直接映射最终品名片段。
ROLL_UP_BANNER_PARENT_TO_CHILD_FRAGMENT: dict[str, dict[str, str]] = {
    ROLL_UP_BANNER_PARENT_ASIN: {
        "B0CZLDHF75": "31.5x79in双面易拉宝",
        "B0CZLGKFJ6": "31.5x71in双面易拉宝",
        "B0CZ73KTHS": "47x81in标准易拉宝",
        "B0CYLCY61S": "24x62in标准易拉宝",
        "B0CYC3W8P6": "33x81in标准易拉宝",
        "B0CMPTFP9R": "33x81in标准易拉宝",
        "B0CMPSJCXH": "33x81in豪华易拉宝",
    },
    "B0D1VB6YF1": {
        "B0D1VBFL6R": "11.5x17.5in桌面易拉宝",
        "B0D1VB1J31": "11.5x17.5in双面桌面易拉宝",
    },
    "B0D1TW6RDZ": {
        "B0D1V4TXC3": "8.2x12.5in桌面易拉宝",
        "B0D1T9P2PR": "8.2x12.5in双面桌面易拉宝",
    },
}

# 子 ASIN 当前没有重复；若后续 Amazon 重用子 ASIN，应改为按父 ASIN 查片段，避免误建。
ROLL_UP_BANNER_FRAGMENT_BY_ASIN: dict[str, str] = {
    child_asin: fragment
    for child_fragments in ROLL_UP_BANNER_PARENT_TO_CHILD_FRAGMENT.values()
    for child_asin, fragment in child_fragments.items()
}
ROLL_UP_BANNER_ASIN_TO_PARENT_ASIN = {
    parent_asin: parent_asin
    for parent_asin in ROLL_UP_BANNER_PARENT_TO_CHILD_FRAGMENT
}
ROLL_UP_BANNER_ASIN_TO_PARENT_ASIN.update(
    {
        child_asin: parent_asin
        for parent_asin, child_fragments in ROLL_UP_BANNER_PARENT_TO_CHILD_FRAGMENT.items()
        for child_asin in child_fragments
    }
)

# Proof 是易拉宝文件夹命名的可选尾部片段；出现时才翻译为“直接制作/在线检查”。
ROLL_UP_BANNER_PROOF_OPTIONS = {
    "straight to production": "直接制作",
    "online proof (48h no reply=ship)": "在线检查",
}

ROLL_UP_BANNER_PRINTING_PROCESS_OPTIONS = {
    "water-based inkjet printing": "水性打印",
    "premium uv printing": "UV打印",
}


def normalize_roll_up_banner_option_value(value: str | None) -> str:
    """把页面/JSON 里的选项值规范成规则键，兼容大小写、空白和末尾句点差异。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text.rstrip(".")


def find_roll_up_banner_parent_asin(asin: str | None) -> str | None:
    """根据父/子 ASIN 定位易拉宝父 ASIN，供扫描命中和文件夹生成共用。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return ROLL_UP_BANNER_ASIN_TO_PARENT_ASIN.get(normalized)


def is_roll_up_banner_asin(asin: str | None) -> bool:
    """判断当前 ASIN 是否属于易拉宝产品族。"""

    return find_roll_up_banner_parent_asin(asin) is not None


def get_roll_up_banner_fragment(asin: str | None) -> str | None:
    """按子 ASIN 读取“尺寸+规格+易拉宝”片段，未知子 ASIN 不做猜测。"""

    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return ROLL_UP_BANNER_FRAGMENT_BY_ASIN.get(normalized)


def get_roll_up_banner_contact_prompts(parent_asin: str | None) -> tuple[str, ...]:
    """按父 ASIN 返回联系方式标题；桌面易拉宝电话/邮箱拆成两个独立字段。"""

    normalized_parent = normalize_asin(parent_asin)
    if normalized_parent in DESKTOP_ROLL_UP_BANNER_PARENT_ASINS:
        return (DESKTOP_ROLL_UP_BANNER_PHONE_PROMPT, DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT)
    return (ROLL_UP_BANNER_CONTACT_PROMPT,)


def match_roll_up_banner_product(texts: str | list[str] | tuple[str, ...]) -> RollUpBannerProductMatch | None:
    """从页面文本或 ASIN 列表中识别易拉宝，并返回统一的产品匹配结果。"""

    for asin in extract_asins(texts):
        parent_asin = find_roll_up_banner_parent_asin(asin)
        if parent_asin:
            return RollUpBannerProductMatch(
                asin=asin,
                parent_asin=parent_asin,
                contact_prompts=get_roll_up_banner_contact_prompts(parent_asin),
            )
    return None
