from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .tents import extract_asins, normalize_asin

PRODUCT_TYPE_CAR_MAGNET = "car_magnet"

CAR_MAGNET_CONTACT_PROMPT = (
    "Please provide a Texting Number or Email to contact you for emergencies "
    "(low quality image, etc)"
)
CAR_MAGNET_PROOF_TITLE = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"
CAR_MAGNET_LEGACY_PROOF_INSTRUCTION = "No reply to the Proof we sent within 48hrs means we will proceed with shipping"
CAR_MAGNET_PROOF_OPTIONS = {
    "straight to production": "直接制作",
    "online proof (48h no reply=ship)": "在线检查",
    "online proof": "在线检查",
}
CAR_MAGNET_SAME_DESIGN_PARENT_ASIN = "B0CNVT6L7Y"
CAR_MAGNET_SAME_DESIGN_TITLE = "Is The Right Side Using The Same Design As The Left Side?"
CAR_MAGNET_SAME_DESIGN_OPTIONS = {
    "yes,using same design for right side": "相同设计",
    "no,using different design for right side": "不同设计",
}

CAR_MAGNET_PARENT_TO_CHILD_ASINS: dict[str, tuple[str, ...]] = {
    "B0CNVT6L7Y": (
        "B0DRCTCB1N",
        "B0DRCVM897",
        "B0DRCX533X",
        "B0CNVMQJFX",
        "B0CQLN8T6Z",
        "B0CQLN5GNL",
        "B0DRCWXR7S",
        "B0CNVLXTWB",
        "B0DRCWKQ4Z",
        "B0DRCT1YYZ",
        "B0DRCVBD7L",
        "B0DRCYZG4K",
    ),
    "B0CNVSJWB2": (
        "B0DRCY4HM5",
        "B0DRCWYC98",
        "B0DRCW8QHY",
        "B0DQVJ8YGG",
        "B0CQLN8T6Y",
        "B0CQLMZW9Z",
        "B0DRCVNYCZ",
        "B0CNVMXKTJ",
        "B0DRCW219P",
        "B0DRCVMGZV",
        "B0DRCVJZC1",
        "B0DRCSMG19",
    ),
    "B0CRKSZ5TB": ("B0CRKYV7C9",),
}

CAR_MAGNET_UNIT_QUANTITY_BY_PARENT: dict[str, int] = {
    # 思维导图“规格数量”节点：B0CNVT6L7Y 是 2 个装，其它两个父 ASIN 是 1 个装。
    "B0CNVT6L7Y": 2,
    "B0CNVSJWB2": 1,
    "B0CRKSZ5TB": 1,
}

CAR_MAGNET_CHILD_SIZE: dict[str, str] = {
    # 固定尺寸 ASIN 映射来自“未命名 (1).pdf”的规格数量节点，集中维护避免散落到流程代码里。
    "B0DRCTCB1N": "3x10in",
    "B0DRCVM897": "3.5x12in",
    "B0DRCX533X": "4.5x15in",
    "B0CNVMQJFX": "10x20in",
    "B0CQLN8T6Z": "12x18in",
    "B0CQLN5GNL": "12x24in",
    "B0DRCWXR7S": "16x23in",
    "B0CNVLXTWB": "18x24in",
    "B0DRCWKQ4Z": "18x36in",
    "B0DRCT1YYZ": "24x24in",
    "B0DRCVBD7L": "24x36in",
    "B0DRCYZG4K": "24x48in",
    "B0DRCY4HM5": "3x10in",
    "B0DRCWYC98": "3.5x12in",
    "B0DRCW8QHY": "4.5x15in",
    "B0DQVJ8YGG": "10x20in",
    "B0CQLN8T6Y": "12x18in",
    "B0CQLMZW9Z": "12x24in",
    "B0DRCVNYCZ": "16x23in",
    "B0CNVMXKTJ": "18x24in",
    "B0DRCW219P": "18x36in",
    "B0DRCVMGZV": "24x24in",
    "B0DRCVJZC1": "24x36in",
    "B0DRCSMG19": "24x48in",
}

CAR_MAGNET_ASIN_TO_PARENT_ASIN: dict[str, str] = {}
for parent_asin, child_asins in CAR_MAGNET_PARENT_TO_CHILD_ASINS.items():
    CAR_MAGNET_ASIN_TO_PARENT_ASIN[parent_asin] = parent_asin
    for child_asin in child_asins:
        CAR_MAGNET_ASIN_TO_PARENT_ASIN[child_asin] = parent_asin


@dataclass(frozen=True)
class CarMagnetProductMatch:
    asin: str
    parent_asin: str
    contact_prompts: tuple[str, ...] = (CAR_MAGNET_CONTACT_PROMPT,)
    product_type: str = PRODUCT_TYPE_CAR_MAGNET


def find_car_magnet_parent_asin(asin: str | None) -> str | None:
    """查找汽车磁贴父ASIN并返回匹配结果。"""
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return CAR_MAGNET_ASIN_TO_PARENT_ASIN.get(normalized)


def is_car_magnet_asin(asin: str | None) -> bool:
    """判断汽车磁贴ASIN是否满足业务条件。"""
    return find_car_magnet_parent_asin(asin) is not None


def get_car_magnet_unit_quantity(parent_asin: str | None) -> int | None:
    """获取汽车磁贴子 ASIN 对应的套装数量。"""
    normalized = normalize_asin(parent_asin)
    if not normalized:
        return None
    return CAR_MAGNET_UNIT_QUANTITY_BY_PARENT.get(normalized)


def get_car_magnet_fixed_size(asin: str | None) -> str | None:
    """获取汽车磁贴固定尺寸。"""
    normalized = normalize_asin(asin)
    if not normalized:
        return None
    return CAR_MAGNET_CHILD_SIZE.get(normalized)


def match_car_magnet_product(texts: str | Iterable[str]) -> CarMagnetProductMatch | None:
    """匹配汽车磁贴产品并返回结构化结果。"""
    for asin in extract_asins(texts):
        parent_asin = find_car_magnet_parent_asin(asin)
        if parent_asin:
            return CarMagnetProductMatch(asin=asin, parent_asin=parent_asin)
    return None


def normalize_car_magnet_size_value(value: str | None) -> str | None:
    """把 8 inches / 8inches / 8in 统一成 8in，供特殊形状 ASIN 生成尺寸。"""
    match = re.search(r"(?P<number>\d+(?:\.\d+)?)\s*(?:inches|inch|in)\b", str(value or ""), re.I)
    if not match:
        return None
    number = match.group("number")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{number}in"


def normalize_car_magnet_proof_value(value: str | None) -> str:
    """规范化汽车磁贴确认稿值，便于后续匹配和比较。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text.rstrip(".")


def normalize_car_magnet_same_design_value(value: str | None) -> str:
    """规范化汽车磁贴相同设计值，便于后续匹配和比较。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    text = re.sub(r"\s*,\s*", ",", text)
    return text.rstrip(".")
