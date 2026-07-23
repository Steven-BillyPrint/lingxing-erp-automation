from __future__ import annotations

import re
from typing import Iterable

from ..constants import (
    EMAIL_LABEL_RE,
    EMAIL_RE,
    EMAIL_RE_TEXT,
    FIXED_EMAIL_RE,
    FIXED_PHONE_RE,
    FIXED_PROMPT_RE,
    PHONE_ANSWER_RE,
    PHONE_LABEL_RE,
)
from ..models import ContactInfo, CustomizationJsonInfo


CAR_MAGNET_CONTACT_PROMPT_RE = re.compile(
    r"Please\s+provide\s+a\s+Texting\s+Number\s+or\s+Email\s+to\s+contact\s+you\s+for\s+emergencies\s*"
    r"\(low\s+quality\s+image,\s*etc\)",
    re.I,
)
VINYL_BANNER_CONTACT_PROMPT_RE = re.compile(
    r"Please\s+provide\s+a\s+texting\s+number\s*/\s*email\s+to\s+contact\s+you\s+for\s+emergencies\s*"
    r"\(low\s+quality\s+image,\s*etc\)",
    re.I,
)
POSTER_CONTACT_PROMPT_RE = re.compile(
    r"Please\s+provide\s+a\s+texting\s+number(?:\s*/\s*email)?\s+to\s+contact\s+you\s+for\s+emergencies\s*"
    r"\(low\s+quality\s+image,\s*etc\)",
    re.I,
)
SINGLE_LINE_CONTACT_PROMPT_RE = re.compile(
    "|".join(
        (
            CAR_MAGNET_CONTACT_PROMPT_RE.pattern,
            VINYL_BANNER_CONTACT_PROMPT_RE.pattern,
            POSTER_CONTACT_PROMPT_RE.pattern,
        )
    ),
    re.I,
)
JSON_EMAIL_RE = re.compile(
    rf"(?:^|[\s:：<(\[/\\／])({EMAIL_RE_TEXT})",
    re.I,
)
FIXED_EMAIL_TITLE_RE = re.compile(
    r"Please\s+provide\s+an\s+email\s+address\s+to\s+confirm\s+customization\s+design\s+and\s+details\s+or\s+for\s+emergencies\.",
    re.I,
)
FIXED_PHONE_TITLE_RE = re.compile(
    r"Please\s+provide\s+a\s+texting\s+number\s+to\s+confirm\s+customization\s+design\s+and\s+details\s+or\s+for\s+emergencies\.",
    re.I,
)
CAR_MAGNET_ANSWER_BOUNDARY_RE = re.compile(
    r"\s+(?=Surface\s+Material\s+Option\s*[:：]|Corner\s*[:：]|Choose\s+Your\s+Magnet\s+Thickness\s*[:：]|"
    r"Proof\s+Option\s*[:：]|Customize\s+Design\s+(?:Left|Right)\s*[:：]|Background\s+Color\s*[:：]|"
    r"Image\s*\d+\s*[:：]|Please\s+double\s+check\b)",
    re.I,
)


CAR_MAGNET_ANSWER_BOUNDARY_RE = re.compile(
    r"\s+(?=Surface\s+Material\s+Option\s*[:：]|Corner\s*[:：]|Choose\s+Your\s+Magnet\s+Thickness\s*[:：]|"
    r"Proof\s+Option\s*[:：]|Customize\s+Design\s+(?:Left|Right)\s*[:：]|Background\s+Color\s*[:：]|"
    r"Image\s*\d+\s*[:：]|Printed\s+Sides\s*[:：]|Material\s+Type\s*[:：]|"
    r"Hanging\s+Options?\s*[:：]|Edge\s+Options?\s*[:：]|Packaging\s+Methods?\s*[:：]|Accessories\s*[:：]|"
    r"Is\s+The\s+(?:Front|Back)\s+Side\s+Using\s+The\s+Same\s+Design\b|Please\s+double\s+check\b)",
    re.I,
)


def normalize_text(value: str | None) -> str:
    """规范化页面文本空白和特殊字符，便于联系方式解析。"""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _normalize_phone_digits(raw: str, *, trim_trailing_noise: bool) -> str | None:
    """统一电话归一化。

    业务规则：美国电话有效号码是 10 位；如果客户填写 +1 或 11 位且首位是 1，
    这个 1 只是美国区号，写回 ERP 时需要去掉，避免把 +19258222350 写进收货电话。
    """

    text = str(raw or "").strip().rstrip(".,;:")
    has_plus = text.lstrip().startswith("+")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
        has_plus = False
    elif trim_trailing_noise and not has_plus and len(digits) == 11 and not digits.startswith("1"):
        digits = digits[:10]
    elif trim_trailing_noise and not has_plus and len(digits) > 11:
        if digits.startswith("1"):
            digits = digits[1:11]
        else:
            digits = digits[:10]
    elif trim_trailing_noise and has_plus and digits.startswith("1") and len(digits) > 11:
        digits = digits[1:11]
        has_plus = False
    elif has_plus and len(digits) > 16:
        digits = digits[:16]
    if not 7 <= len(digits) <= 16:
        return None
    return f"+{digits}" if has_plus else digits


def normalize_phone(value: str) -> str | None:
    """规范化电话号码文本，保留可写回的有效号码。"""
    return _normalize_phone_digits(value, trim_trailing_noise=False)


def has_supported_contact_prompt(value: str | None) -> bool:
    """判断文本里是否包含当前支持品类的联系方式提示标题。"""
    text = str(value or "")
    return bool(FIXED_PROMPT_RE.search(text) or SINGLE_LINE_CONTACT_PROMPT_RE.search(text))

def normalize_fixed_phone_answer(value: str) -> str | None:
    """解析固定提示后的电话答案。

    详情页有时会把整页文字压成一行，电话后面紧跟金额/数量等数字。
    对未带 + 的北美电话，优先保留 10 位，避免把后续无关数字拼进电话。
    """
    answer = re.split(
        r"\s+(?:Please\s+provide|Custom|Frame|Side|Fabric|Double|Roller|Rope|Sandbags|Notes?(?:\s*/\s*Share(?:\s+Link)?(?:\s*/\s*PDF\s+File)?)?|系统单号|商品|订单|交易|客服|收入|支出)\b",
        value.strip(),
        maxsplit=1,
        flags=re.I,
    )[0]
    match = PHONE_ANSWER_RE.search(answer)
    if not match:
        return None
    return _normalize_phone_digits(match.group(0), trim_trailing_noise=True)

def split_collapsed_fixed_prompts(value: str) -> str:
    """拆分粘连在一起的固定联系方式提示文本。"""
    text = re.sub(
        r"\s+(?=Please\s+provide\s+(?:an\s+email\s+address|a\s+texting\s+number)\s+to\s+confirm\b)",
        "\n",
        value,
        flags=re.I,
    )
    text = re.sub(
        r"\s+(?=Please\s+provide\s+a\s+Texting\s+Number\s+or\s+Email\s+to\s+contact\b)",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\s+(?=Please\s+provide\s+a\s+texting\s+number\s*/\s*email\s+to\s+contact\b)",
        "\n",
        text,
        flags=re.I,
    )
    return re.sub(
        r"\s+(?=Please\s+provide\s+a\s+texting\s+number\s+to\s+contact\b)",
        "\n",
        text,
        flags=re.I,
    )


def extract_car_magnet_contact_info(texts: Iterable[str]) -> ContactInfo | None:
    """解析汽车磁贴“电话或邮箱”单行答案。

    客户可能把电话和邮箱都填在同一个答案里，也可能只填其中一个。
    这里先用后续磁贴标题做边界截断答案，再分别提取邮箱和电话，避免把材质/厚度等选项混进联系方式。
    """
    raw_texts = [str(text) for text in texts if str(text).strip()]
    email: str | None = None
    phone: str | None = None
    excerpts: list[str] = []
    for raw_text in raw_texts:
        text = split_collapsed_fixed_prompts(raw_text)
        prompt_match = SINGLE_LINE_CONTACT_PROMPT_RE.search(text)
        if not prompt_match:
            continue
        excerpts.append(text.strip())
        answer = text[prompt_match.end():].lstrip(" :：-\n\t")
        answer = CAR_MAGNET_ANSWER_BOUNDARY_RE.split(answer, maxsplit=1)[0].strip()
        if email is None:
            email_match = EMAIL_RE.search(answer)
            if email_match:
                email = email_match.group(1).strip().rstrip(".,;:")
        if phone is None:
            phone_source = EMAIL_RE.sub(" ", answer)
            phone = normalize_fixed_phone_answer(phone_source)
        if email or phone:
            break
    if not excerpts:
        return None
    return ContactInfo(
        phone=phone,
        email=email,
        source_count=len(raw_texts),
        source_excerpt=normalize_text("\n".join(excerpts))[:500],
        customization_text="\n".join(excerpts).strip() or None,
    )

def extract_fixed_contact_info(texts: Iterable[str]) -> ContactInfo | None:
    """从固定提示语中提取买家电话和邮箱。"""
    raw_texts = [str(text) for text in texts if str(text).strip()]
    car_magnet_contact = extract_car_magnet_contact_info(raw_texts)
    if car_magnet_contact is not None:
        return car_magnet_contact
    email: str | None = None
    phone: str | None = None
    prompt_seen = False
    excerpts: list[str] = []

    for raw_text in raw_texts:
        text = split_collapsed_fixed_prompts(raw_text)
        if not FIXED_PROMPT_RE.search(text):
            continue
        prompt_seen = True
        excerpts.append(text.strip())

        if email is None:
            email_match = FIXED_EMAIL_RE.search(text)
            if email_match:
                email = email_match.group(1).strip().rstrip(".,;:")

        if phone is None:
            phone_match = FIXED_PHONE_RE.search(text)
            if phone_match:
                phone = normalize_fixed_phone_answer(phone_match.group(1))

        if email and phone:
            break

    if not prompt_seen:
        return None
    return ContactInfo(
        phone=phone,
        email=email,
        source_count=len(raw_texts),
        source_excerpt=normalize_text("\n".join(excerpts))[:500],
        customization_text="\n".join(excerpts).strip() or None,
    )

def extract_unique_fixed_contact_info(texts: Iterable[str]) -> ContactInfo | None:
    """从固定定制化提示中做安全兜底：只在全页唯一电话和唯一邮箱时自动合并。

    有些 ERP tooltip 会把邮箱提示和电话提示拆成多个 DOM 文本节点。逐段解析时会因为
    单段不完整而失败；这里只扫描固定英文提示，不使用页面上的旧收货邮箱或卖家邮箱，
    并且出现多组不同联系方式时不自动选择，避免多商品订单错写。
    """
    raw_texts = [str(text) for text in texts if str(text).strip()]
    emails: dict[str, str] = {}
    phones: dict[str, str] = {}
    excerpts: list[str] = []

    for raw_text in raw_texts:
        text = split_collapsed_fixed_prompts(raw_text)
        if not has_supported_contact_prompt(text):
            continue
        car_magnet_contact = extract_car_magnet_contact_info([text])
        if car_magnet_contact and (car_magnet_contact.phone or car_magnet_contact.email):
            if car_magnet_contact.email:
                emails[car_magnet_contact.email.lower()] = car_magnet_contact.email
            if car_magnet_contact.phone:
                phones[car_magnet_contact.phone] = car_magnet_contact.phone
            excerpts.append(text.strip())
            continue
        if not FIXED_PROMPT_RE.search(text):
            continue
        excerpts.append(text.strip())
        for email_match in FIXED_EMAIL_RE.finditer(text):
            email = email_match.group(1).strip().rstrip(".,;:")
            emails[email.lower()] = email
        for phone_match in FIXED_PHONE_RE.finditer(text):
            phone = normalize_fixed_phone_answer(phone_match.group(1))
            if phone:
                phones[phone] = phone

    if len(emails) != 1 or len(phones) != 1:
        return None
    return ContactInfo(
        phone=next(iter(phones.values())),
        email=next(iter(emails.values())),
        source_count=len(raw_texts),
        source_excerpt=normalize_text("\n".join(excerpts))[:500],
        customization_text="\n".join(excerpts).strip() or None,
    )

def contact_identity(contact: ContactInfo) -> tuple[str, str] | None:
    """把联系方式归一化成查重键；电话或邮箱缺失时不能作为可写回候选。"""
    if not contact.phone or not contact.email:
        return None
    normalized_phone = normalize_fixed_phone_answer(contact.phone) or normalize_phone(contact.phone)
    if not normalized_phone:
        return None
    return normalized_phone, contact.email.strip().lower()

def contact_choice_identity(contact: ContactInfo) -> tuple[str, str, str] | None:
    """候选联系方式查重键：完整候选按电话+邮箱，部分候选按已有字段。"""
    if contact.phone and contact.email:
        key = contact_identity(contact)
        return ("complete", key[0], key[1]) if key else None
    if contact.phone:
        normalized_phone = normalize_fixed_phone_answer(contact.phone) or normalize_phone(contact.phone)
        return ("phone", normalized_phone, "") if normalized_phone else None
    if contact.email:
        return ("email", "", contact.email.strip().lower())
    return None

def extract_complete_contact_candidates(texts: Iterable[str]) -> list[ContactInfo]:
    """逐段解析“更多商品信息”，避免把不同商品或页面旧邮箱混在一起。

    只接受包含固定定制化提示语的文本段。这样多商品订单会得到多个独立候选，
    后续流程再判断它们是否一致，或让用户选择。
    """
    raw_texts = [str(text) for text in texts if str(text).strip()]
    complete_candidates_by_key: dict[tuple[str, str], ContactInfo] = {}
    complete_order: list[tuple[str, str]] = []
    partial_candidates: list[ContactInfo] = []
    seen_partial: set[tuple[str, str, str]] = set()
    for raw_text in raw_texts:
        text = split_collapsed_fixed_prompts(raw_text)
        if not has_supported_contact_prompt(text):
            continue
        contact = extract_fixed_contact_info([text])
        if contact is None or (not contact.phone and not contact.email):
            continue
        key = contact_identity(contact)
        if key is not None:
            candidate = ContactInfo(
                phone=key[0],
                email=contact.email,
                source_count=1,
                source_excerpt=contact.source_excerpt,
                customization_text=text.strip(),
            )
            if key in complete_candidates_by_key:
                # 同一联系方式可能同时来自“整页详情文本”和真正 tooltip。
                # 保留更短的来源，通常就是鼠标悬停出的“更多商品信息”弹窗文本。
                current = complete_candidates_by_key[key]
                if len(candidate.source_excerpt or "") < len(current.source_excerpt or ""):
                    complete_candidates_by_key[key] = candidate
                continue
            complete_order.append(key)
            complete_candidates_by_key[key] = candidate
            continue

        partial_key = contact_choice_identity(contact)
        if partial_key is None or partial_key in seen_partial:
            continue
        seen_partial.add(partial_key)
        partial_candidates.append(
            ContactInfo(
                phone=partial_key[1] or None,
                email=contact.email,
                source_count=1,
                source_excerpt=contact.source_excerpt,
                customization_text=text.strip(),
            )
        )
    if complete_order:
        return [complete_candidates_by_key[key] for key in complete_order]

    if not complete_order:
        # 安全兜底：固定提示被拆成多段时，只允许唯一联系方式自动合并。
        contact = extract_unique_fixed_contact_info(raw_texts)
        key = contact_identity(contact) if contact else None
        if contact and key:
            return [
                ContactInfo(
                    phone=key[0],
                    email=contact.email,
                    source_count=contact.source_count,
                    source_excerpt=contact.source_excerpt,
                    customization_text=contact.customization_text,
                )
            ]
    return partial_candidates


def _extract_json_email_answer(value: str) -> str | None:
    """从 JSON 定制化答案中提取邮箱字段。"""
    match = JSON_EMAIL_RE.search(str(value or ""))
    if not match:
        return None
    return match.group(1).strip().rstrip(".,;:")


def _remove_json_email_answers(value: str) -> str:
    """移除 JSON 邮箱答案片段，避免重复识别。"""
    return JSON_EMAIL_RE.sub(" ", str(value or ""))


def extract_json_contact_answer(value: str) -> ContactInfo | None:
    """从 zip JSON 的单个答案值中解析联系方式。

    zip JSON 已经把定制标题和值拆成结构化 pairs；这里不能再拼成
    "Title : Value" 后用旧 tooltip 边界正则截取，否则会截断 affûtage 这类非 ASCII 邮箱。
    """

    answer = str(value or "").strip()
    if not answer:
        return None
    email = _extract_json_email_answer(answer)
    phone_source = _remove_json_email_answers(answer)
    phone = normalize_fixed_phone_answer(phone_source)
    if not email and not phone:
        return None
    return ContactInfo(
        phone=phone,
        email=email,
        source_count=1,
        source_excerpt=normalize_text(answer)[:500],
        customization_text=answer,
    )


def _add_json_contact_candidate(
    contact: ContactInfo | None,
    *,
    complete_candidates_by_key: dict[tuple[str, str], ContactInfo],
    complete_order: list[tuple[str, str]],
    partial_candidates: list[ContactInfo],
    seen_partial: set[tuple[str, str, str]],
) -> None:
    """把 JSON 定制化联系方式追加为候选结果。"""
    if contact is None or (not contact.phone and not contact.email):
        return
    key = contact_identity(contact)
    if key is not None:
        candidate = ContactInfo(
            phone=key[0],
            email=contact.email,
            source_count=contact.source_count,
            source_excerpt=contact.source_excerpt,
            customization_text=contact.customization_text,
        )
        if key not in complete_candidates_by_key:
            complete_order.append(key)
            complete_candidates_by_key[key] = candidate
        return

    partial_key = contact_choice_identity(contact)
    if partial_key is None or partial_key in seen_partial:
        return
    seen_partial.add(partial_key)
    partial_candidates.append(
        ContactInfo(
            phone=partial_key[1] or None,
            email=contact.email,
            source_count=contact.source_count,
            source_excerpt=contact.source_excerpt,
            customization_text=contact.customization_text,
        )
    )


def extract_contact_candidates_from_json_items(items: Iterable[CustomizationJsonInfo]) -> list[ContactInfo]:
    """从 zip JSON pairs 中解析电话/邮箱候选。

    帐篷、汽车磁贴、喷绘迁移到 zip JSON 后，不再从 ERP tooltip 文本提取联系方式。
    JSON pairs 已经是结构化键值，因此必须按标题拿对应 value，再在 value 内识别电话/邮箱。
    """

    complete_candidates_by_key: dict[tuple[str, str], ContactInfo] = {}
    complete_order: list[tuple[str, str]] = []
    partial_candidates: list[ContactInfo] = []
    seen_partial: set[tuple[str, str, str]] = set()
    global_emails: dict[str, str] = {}
    global_phones: dict[str, str] = {}

    for item in items:
        item_email: str | None = None
        item_phone: str | None = None
        item_excerpts: list[str] = []
        for title, value in item.pairs.items():
            if not value:
                continue
            title_text = str(title or "")
            value_text = str(value or "").strip()
            if SINGLE_LINE_CONTACT_PROMPT_RE.search(title_text):
                contact = extract_json_contact_answer(value_text)
                _add_json_contact_candidate(
                    contact,
                    complete_candidates_by_key=complete_candidates_by_key,
                    complete_order=complete_order,
                    partial_candidates=partial_candidates,
                    seen_partial=seen_partial,
                )
                if contact and contact.email:
                    global_emails[contact.email.lower()] = contact.email
                if contact and contact.phone:
                    global_phones[contact.phone] = contact.phone
                continue

            if FIXED_EMAIL_TITLE_RE.search(title_text):
                email = _extract_json_email_answer(value_text)
                if email:
                    item_email = item_email or email
                    global_emails[email.lower()] = email
                    item_excerpts.append(value_text)
                continue

            if FIXED_PHONE_TITLE_RE.search(title_text):
                phone = normalize_fixed_phone_answer(value_text)
                if phone:
                    item_phone = item_phone or phone
                    global_phones[phone] = phone
                    item_excerpts.append(value_text)

        if item_email or item_phone:
            _add_json_contact_candidate(
                ContactInfo(
                    phone=item_phone,
                    email=item_email,
                    source_count=1,
                    source_excerpt=normalize_text("\n".join(item_excerpts))[:500],
                    customization_text="\n".join(item_excerpts).strip() or None,
                ),
                complete_candidates_by_key=complete_candidates_by_key,
                complete_order=complete_order,
                partial_candidates=partial_candidates,
                seen_partial=seen_partial,
            )

    if complete_order:
        return [complete_candidates_by_key[key] for key in complete_order]

    # 多个 JSON 行把固定邮箱/电话拆开时，只在全订单唯一邮箱、唯一电话时合并。
    if len(global_emails) == 1 and len(global_phones) == 1:
        email = next(iter(global_emails.values()))
        phone = next(iter(global_phones.values()))
        return [
            ContactInfo(
                phone=phone,
                email=email,
                source_count=len(global_emails) + len(global_phones),
                source_excerpt=normalize_text(f"{phone} {email}")[:500],
                customization_text=f"{phone}\n{email}",
            )
        ]

    return partial_candidates


def customization_json_has_contact_fields(
    items: Iterable[CustomizationJsonInfo],
) -> bool:
    """Return whether JSON explicitly presented a supported contact question.

    Empty answers are still authoritative: they prove that the buyer saw the
    field and left it blank, which is different from an unread or missing JSON.
    """

    for item in items:
        for title in item.pairs:
            title_text = str(title or "")
            if (
                SINGLE_LINE_CONTACT_PROMPT_RE.search(title_text)
                or FIXED_EMAIL_TITLE_RE.search(title_text)
                or FIXED_PHONE_TITLE_RE.search(title_text)
            ):
                return True
    return False

def extract_contact_info(texts: Iterable[str]) -> ContactInfo:
    """从定制化文本中提取单组联系方式。"""
    raw_texts = [str(text) for text in texts if str(text).strip()]
    fixed_contact = extract_fixed_contact_info(raw_texts)
    if fixed_contact is not None:
        return fixed_contact

    clean_texts = [normalize_text(text) for text in raw_texts if normalize_text(text)]
    combined = "\n".join(clean_texts)
    email: str | None = None
    phone: str | None = None

    email_match = EMAIL_LABEL_RE.search(combined)
    if email_match:
        email = email_match.group(1).strip().rstrip(".,;:")

    phone_match = PHONE_LABEL_RE.search(combined)
    if phone_match:
        phone = normalize_phone(phone_match.group(1))

    excerpt_source = next(
        (text for text in clean_texts if (email and email in text) or (phone and phone in re.sub(r"\D", "", text))),
        combined,
    )
    return ContactInfo(
        phone=phone,
        email=email,
        source_count=len(clean_texts),
        source_excerpt=excerpt_source[:500],
        customization_text=fixed_contact.customization_text if fixed_contact is not None else combined,
    )

def missing_contact_fields(contact: ContactInfo) -> list[str]:
    """判断联系方式结果中缺失的必填字段。"""
    missing: list[str] = []
    if not contact.phone:
        missing.append("电话")
    if not contact.email:
        missing.append("买家邮箱")
    return missing
