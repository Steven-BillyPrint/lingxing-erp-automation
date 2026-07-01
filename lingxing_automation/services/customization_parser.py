from __future__ import annotations

import re

from ..models import CustomizationData
from .order_folder_rules import ORDER_FOLDER_TITLE_ALIASES, ORDER_FOLDER_TITLES

CONTACT_PROMPT_RE = re.compile(
    r"\bPlease\s+provide\s+(?:an\s+email\s+address|a\s+texting\s+number|a\s+Texting\s+Number\s+or\s+Email)\b",
    re.I,
)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


TITLE_BY_NORMALIZED = {
    _normalize_title(title): ORDER_FOLDER_TITLE_ALIASES.get(title, title)
    for title in ORDER_FOLDER_TITLES
}
NON_FOLDER_BOUNDARY_TITLES = (
    "Additional Notes",
    "Customer Notes",
)
NON_FOLDER_BOUNDARY_PATTERNS = (
    # 备注/素材链接类字段只用于截断上一项定制选项，不参与文件夹命名规则。
    # Amazon 定制项会出现 Notes、Notes/Share、Notes/Share Link/PDF File 等折叠写法。
    r"Notes?\s*/\s*Share\s+Link\s*/\s*PDF\s+File",
    r"Notes?\s*/\s*Share\s*/\s*PDF\s+File",
    r"Notes?\s*/\s*Share\s+Link",
    r"Notes?\s*/\s*Share",
    r"Additional\s+Notes?",
    r"Customer\s+Notes?",
    r"Notes?",
    r"Does\s+Your\s+Canopy\s+Topper\s+Have\s+Velcro\s+on\s+the\s+Bottom\?\s*Affects\s+(?:Full|Half)\s+Wall\s+attachment\s+to\s+Topper\.?",
    r"Select\s+the\s+(?:Full|Half)\s+Wall\s+Size\s+That\s+Fits\s+Your\s+Canopy\s+Tent\s+Frame",
    r"Customize\s+(?:Full|Half)\s+Wall",
    r"Custom\s+(?:Full|Half)\s+Wall",
    r"Custom\s+Topper\s+(?:Front\s*/\s*Back|Left\s*/\s*Right)",
    r"Canopy\s+Topper\s+(?:Front\s*/\s*Back|Left\s*/\s*Right)\s+Color",
    r"(?:Full|Half)\s+Wall\s+Color",
    r"Customization\s+Notes?\s+for\s+(?:Full|Half)\s+Wall",
    r"Pattern\s+(?:Background|Backgroud)",
    r"Background\s+Color",
    r"Your\s+Logo\s+or\s+Photo\s+\d+",
    r"Your\s+Font\s+\d+",
    r"Your\s+Text\s+\d+",
    r"Text\s+Color\s+\d+",
    r"Text\s+input\s+\d+",
    r"Other\s+requirements\s+for\s+Top",
    r"Customize\s+Design\s+(?:Left|Right)",
    r"Image\s*\d+",
    r"Is\s+The\s+Right\s+Side\s+Using\s+The\s+Same\s+Design\s+As\s+The\s+Left\s+Side\?",
    r"Design\s+for\s+Right\s+Side",
    r"Please\s+double\s+check\s+your\s+artwork\s+personalization\s+and\s+confirm",
    r"Proof\s+Option",
    r"Please\s+provide\s+a\s+Texting\s+Number\s+or\s+Email\s+to\s+contact\s+you\s+for\s+emergencies\s*\(low\s+quality\s+image,\s*etc\)",
)


def _title_regex(title: str) -> str:
    # 备注类标题不是文件夹组件，只用于切断上一项 value；这里允许标题中的空格和 / 有轻微格式变化。
    pattern = re.escape(title)
    pattern = pattern.replace(r"\ ", r"\s+")
    pattern = pattern.replace(r"\/", r"\s*/\s*")
    return pattern


def normalize_customization_text(value: str | None) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    # Tooltip 里经常把多空格、Tab 和换行混在一起；解析标题时保留换行边界，
    # 但把一行内的连续空白压缩，避免同一个标题因为空格不同而匹配失败。
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


NOTE_VALUE_CLIP_PATTERNS = (
    # 只要选项值中出现 Notes/Share Link/PDF File，后续内容都是客户备注/素材链接，
    # 不属于文件夹命名选项；不关心后面是 Line 1、冒号、链接还是其它文本。
    r"Notes?\s*/\s*Share\s+Link\s*/\s*PDF\s+File",
    r"Notes?\s*/\s*Share\s*/\s*PDF\s+File",
    r"Notes?\s*/\s*Share\s+Link",
    r"Notes?\s*/\s*Share",
    r"Additional\s+Notes?",
    r"Customer\s+Notes?",
    r"Proof\s+Option",
    # 普通 Notes 也常常紧跟在选项后面，例如 “Rope & Stake Kit Options : Yes Notes : ...”。
    # 这里放在更具体的 Notes/Share 之后，统一按最早出现位置截断。
    r"Notes?",
)


def _clip_non_folder_note_text(value: str) -> str:
    """截断定制化选项值后面的备注/素材链接内容。

    ERP tooltip 中经常把客户备注、Canva 链接、PDF/File 上传说明接在上一个选项后面。
    这些内容只用于客户沟通，不参与文件夹命名；如果不截断，会污染桌布/旗帜等选项值，
    最终触发 folder_rule_missing。
    """
    earliest_index: int | None = None
    for pattern in NOTE_VALUE_CLIP_PATTERNS:
        match = re.search(pattern, value, flags=re.I)
        if match and (earliest_index is None or match.start() < earliest_index):
            earliest_index = match.start()
    return value[:earliest_index].strip() if earliest_index is not None else value.strip()


def _clip_contact_prompts(value: str) -> str:
    value = _clip_non_folder_note_text(value)
    match = CONTACT_PROMPT_RE.search(value)
    return value[: match.start()].strip() if match else value.strip()


def parse_customization_pairs(text: str, titles: tuple[str, ...] = ORDER_FOLDER_TITLES) -> dict[str, str]:
    """从完整定制化文本中解析标题和值。

    source_excerpt 可能被截断，后面的桌布、旗帜选项会丢失；
    生成文件夹名必须使用完整 customization_text。
    """
    normalized_text = normalize_customization_text(text)
    if not normalized_text:
        return {}

    boundary_titles = tuple(dict.fromkeys([*titles, *NON_FOLDER_BOUNDARY_TITLES]))
    folder_titles = {
        _normalize_title(title): TITLE_BY_NORMALIZED.get(_normalize_title(title), title)
        for title in titles
    }
    title_patterns = [_title_regex(title) for title in sorted(boundary_titles, key=len, reverse=True)]
    title_patterns.extend(NON_FOLDER_BOUNDARY_PATTERNS)
    title_pattern = "|".join(title_patterns)
    pattern = re.compile(rf"(?P<title>{title_pattern})\s*[:：]\s*", re.I)
    matches = list(pattern.finditer(normalized_text))
    pairs: dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_title = re.sub(r"\s+", " ", match.group("title")).strip()
        canonical_title = folder_titles.get(_normalize_title(raw_title))
        if canonical_title is None:
            continue
        value_start = match.end()
        value_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized_text)
        value = normalized_text[value_start:value_end].strip(" \n\t:：")
        value = re.sub(r"\s*\n\s*", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
        value = _clip_contact_prompts(value)
        if value:
            pairs[canonical_title] = value
    return pairs


def parse_customization_data(text: str) -> CustomizationData:
    raw_text = normalize_customization_text(text)
    return CustomizationData(raw_text=raw_text, pairs=parse_customization_pairs(raw_text))
