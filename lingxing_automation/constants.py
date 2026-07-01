from __future__ import annotations

import re

ORDER_MANAGEMENT_URL = "https://erp.lingxing.com/erp/mmulti/mpOrderManagement"
SYSTEM_ORDER_RE = re.compile(r"\b\d{15,24}\b")
PLATFORM_ORDER_RE = re.compile(r"\b\d+-\d+-\d+\b")
EMAIL_LOCAL_RE_TEXT = r'[^\s/@<>()\[\]";,:：]+'
# 客户邮箱的 local-part 可能包含重音字母或撇号（例如 Ben’s.backflow@...）。
# 所有商品品类共用这一套邮箱匹配，避免旧 ASCII 正则从中间截断邮箱前缀。
EMAIL_RE_TEXT = rf"{EMAIL_LOCAL_RE_TEXT}@[A-Z0-9][A-Z0-9.\-]*\.[A-Z]{{2,}}"
EMAIL_RE = re.compile(rf"\b({EMAIL_RE_TEXT})\b", re.I)
EMAIL_LABEL_RE = re.compile(
    rf"(?:email\s*(?:address)?|e-mail|mailbox|buyer\s*email|买家邮箱|邮箱)"
    rf"[^:：\n]{{0,120}}[:：]\s*({EMAIL_RE_TEXT})",
    re.I | re.S,
)
PHONE_LABEL_RE = re.compile(
    r"(?:texting\s+number|text\s+number|phone\s+number|telephone|mobile|cell\s*phone|"
    r"sms\s+number|联系电话|联系手机|收件电话|"
    r"买家电话|手机号|手机|电话)"
    r"[^:：\n]{0,160}[:：]\s*(\+?\d[\d\s().\-]{6,28}\d)",
    re.I | re.S,
)
FIXED_EMAIL_RE = re.compile(
    rf"Please\s+provide\s+an\s+email\s+address\s+to\s+confirm\s+customization\s+design\s+and\s+details\s+or\s+for\s+emergencies\.\s*(?:[-–—]\s*Line\s*\d+\s*)?[:：]\s*({EMAIL_RE_TEXT})",
    re.I | re.S,
)
FIXED_PHONE_RE = re.compile(
    r"Please\s+provide\s+a\s+texting\s+number\s+to\s+confirm\s+customization\s+design\s+and\s+details\s+or\s+for\s+emergencies\.\s*(?:[-–—]\s*Line\s*\d+\s*)?[:：]\s*([^\r\n<]{0,120})",
    re.I | re.S,
)
FIXED_PROMPT_RE = re.compile(
    r"Please\s+provide\s+(?:an\s+email\s+address|a\s+texting\s+number)\s+to\s+confirm\s+customization\s+design\s+and\s+details\s+or\s+for\s+emergencies\.",
    re.I,
)
PHONE_ANSWER_RE = re.compile(r"\+?\d[\d\s().\-]{5,34}\d")
TRUE_VALUES = {"1", "true", "yes", "y", "on", "是"}
FALSE_VALUES = {"0", "false", "no", "n", "off", "否"}

# 批量巡检默认每 5 分钟跑一轮；旧版命令行仍可用小时参数覆盖。
DEFAULT_BATCH_INTERVAL_MINUTES = 5
