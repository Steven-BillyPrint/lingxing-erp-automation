from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..models import SplitDecision


def decide_split(order_region: Mapping[str, Any], rules: Iterable[Mapping[str, Any]]) -> SplitDecision:
    """根据订单区域和拆单规则判断是否需要拆单。"""
    country = str(order_region.get('country') or '').strip().lower()
    state = str(order_region.get('state') or '').strip().lower()
    matches: list[Mapping[str, Any]] = []
    for rule in rules:
        rule_country = str(rule.get('country') or '').strip().lower()
        rule_state = str(rule.get('state') or '').strip().lower()
        if rule_country and rule_country != country:
            continue
        if rule_state and rule_state != state:
            continue
        matches.append(rule)

    if not matches:
            return SplitDecision(status='no_split', should_split=False, reason='没有命中拆单规则。', review_required=False)
    if len(matches) > 1:
            return SplitDecision(status='conflict', should_split=False, reason='命中多个拆单规则。', review_required=True)

    rule = matches[0]
    should_split = bool(rule.get('should_split'))
    return SplitDecision(
        status='ready' if should_split else 'no_split',
        should_split=should_split,
        rule_id=str(rule.get('rule_id') or ''),
        target_orders=list(rule.get('target_orders') or []),
            reason=str(rule.get('reason') or ('需要拆单。' if should_split else '规则命中但无需拆单。')),
        review_required=False,
    )
