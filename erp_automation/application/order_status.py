"""Conservative helpers for order-level processing status hints.

Lingxing keeps "buyer requested cancellation" as a system-processing tag while
the main order status can remain pending review.  Business code must therefore
inspect the typed status/tag containers instead of relying on ``status == 4``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


BUYER_CANCEL_REQUEST_TEXT = "买家申请取消"
ORDER_CANCELLED_TEXT = "订单已取消"

_STATUS_CONTAINER_KEYS = frozenset(
    {
        "ordertag",
        "pendingordertag",
        "exceptionordertag",
    }
)
_VISIBLE_STATUS_KEYS = frozenset(
    {
        "statustext",
        "orderstatusname",
        "statusname",
    }
)
_ORDER_STATUS_KEYS = frozenset(
    {
        "status",
        "orderstatus",
        "orderstatuscode",
    }
)
_BUYER_CANCEL_FLAG_KEYS = frozenset(
    {
        "buyercancelrequested",
        "isbuyercancelrequested",
    }
)


def _canonical_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _status_strings(value: Any) -> Iterable[str]:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, Mapping):
        preferred = False
        for key, nested in value.items():
            if _canonical_key(key) in {"tagname", "name", "label", "text", "value"}:
                preferred = True
                yield from _status_strings(nested)
        if not preferred:
            for nested in value.values():
                yield from _status_strings(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            yield from _status_strings(nested)
        return
    text = str(value).strip()
    if text:
        yield text


def has_buyer_cancel_request(payload: Mapping[str, Any]) -> bool:
    """Return true only for an explicit cancellation status/tag signal.

    Customer remarks and buyer messages are intentionally excluded so a free
    text mention of cancellation cannot silently dispose a production order.
    """

    queue: list[Mapping[str, Any]] = [payload]
    while queue:
        mapping = queue.pop(0)
        for key, value in mapping.items():
            canonical = _canonical_key(key)
            if canonical in _BUYER_CANCEL_FLAG_KEYS and value is True:
                return True
            if canonical in _STATUS_CONTAINER_KEYS or canonical in _VISIBLE_STATUS_KEYS:
                if any(BUYER_CANCEL_REQUEST_TEXT in text for text in _status_strings(value)):
                    return True
            if isinstance(value, Mapping):
                queue.append(value)
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes, bytearray),
            ):
                queue.extend(item for item in value if isinstance(item, Mapping))
    return False


def is_order_cancelled(payload: Mapping[str, Any]) -> bool:
    """Return true only for an explicit terminal cancellation status.

    Lingxing's documented order-status value ``7`` is terminal cancellation.
    Text matching is intentionally limited to typed status fields so customer
    remarks mentioning cancellation cannot dispose a real order.
    """

    queue: list[Mapping[str, Any]] = [payload]
    while queue:
        mapping = queue.pop(0)
        for key, value in mapping.items():
            canonical = _canonical_key(key)
            if canonical in _ORDER_STATUS_KEYS and str(value or "").strip() == "7":
                return True
            if canonical in _VISIBLE_STATUS_KEYS:
                for text in _status_strings(value):
                    normalized = text.casefold().replace(" ", "")
                    if any(
                        marker in normalized
                        for marker in ("已取消", "订单取消", "cancelled", "canceled")
                    ):
                        return True
            if isinstance(value, Mapping):
                queue.append(value)
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes, bytearray),
            ):
                queue.extend(item for item in value if isinstance(item, Mapping))
    return False


__all__ = [
    "BUYER_CANCEL_REQUEST_TEXT",
    "ORDER_CANCELLED_TEXT",
    "has_buyer_cancel_request",
    "is_order_cancelled",
]
