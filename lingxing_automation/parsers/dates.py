from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Literal

DATETIME_RE = re.compile(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\b")
PAYMENT_DATETIME_RE = re.compile(
    r"(?:付款时间|付款|支付时间|支付)[^\d]{0,30}"
    r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
)
PARTIAL_PAYMENT_DATETIME_RE = re.compile(
    r"(?:付款时间|付款|支付时间|支付)[^\d]{0,30}"
    r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2})(?:[:\s]*\.\.\.|$|[^\d:])",
)

PaymentWindowStatus = Literal["recent", "old", "unknown"]


def parse_lingxing_datetime(value: str) -> datetime | None:
    normalized = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def extract_lingxing_datetimes(text: str) -> list[datetime]:
    output: list[datetime] = []
    for match in DATETIME_RE.finditer(text):
        parsed = parse_lingxing_datetime(match.group(0))
        if parsed:
            output.append(parsed)
    return output


def extract_payment_datetimes(text: str) -> list[datetime]:
    output: list[datetime] = []
    for match in PAYMENT_DATETIME_RE.finditer(text):
        parsed = parse_lingxing_datetime(match.group(1))
        if parsed:
            output.append(parsed)
    if output:
        return output
    for match in PARTIAL_PAYMENT_DATETIME_RE.finditer(text):
        parsed = parse_lingxing_datetime(f"{match.group(1)}:00:00")
        if parsed:
            output.append(parsed)
    return output


def classify_recent_payment_window(
    text: str,
    *,
    now: datetime | None = None,
    hours: float = 24,
) -> PaymentWindowStatus:
    payment_datetimes = extract_payment_datetimes(text)
    if not payment_datetimes:
        return "unknown"
    current = now or datetime.now()
    window = timedelta(hours=max(0.0, hours))
    for paid_at in payment_datetimes:
        age = current - paid_at
        if timedelta(0) <= age <= window:
            return "recent"
    return "old"


def latest_payment_text(text: str) -> str | None:
    payment_datetimes = extract_payment_datetimes(text)
    if not payment_datetimes:
        return None
    return max(payment_datetimes).strftime("%Y-%m-%d %H:%M:%S")
