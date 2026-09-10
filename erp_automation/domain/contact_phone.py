"""Shared placeholder detection; callers retain their own phone formatting rules."""

from __future__ import annotations

import re


def phone_placeholder_reason(value: str | None) -> str | None:
    """Reject whole-number placeholders, not repeated groups or local patterns.

    The optional North American country code is ignored for this comparison.
    This detects obvious placeholders, not whether a number is reachable.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not digits:
        return None
    if len(set(digits)) == 1:
        return "全零占位号码" if digits[0] == "0" else "全同数字占位号码"
    if len(digits) >= 7:
        steps = {(int(right) - int(left)) % 10 for left, right in zip(digits, digits[1:])}
        if steps == {1} or steps == {9}:
            return "整段连续递增或递减的占位号码"
    return None
