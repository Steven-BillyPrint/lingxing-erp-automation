from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..models import SkuDecision


def decide_sku(customization_text: str, rules: Iterable[Mapping[str, object]]) -> SkuDecision:
    """根据定制化文本和规则判断 SKU 处理决策。"""
    text = customization_text.lower()
    matches: list[Mapping[str, object]] = []
    for rule in rules:
        include_terms = [str(term).lower() for term in rule.get('must_include', []) or []]
        exclude_terms = [str(term).lower() for term in rule.get('must_not_include', []) or []]
        if include_terms and not all(term in text for term in include_terms):
            continue
        if any(term in text for term in exclude_terms):
            continue
        matches.append(rule)

    if not matches:
            return SkuDecision(status='review', reason='没有命中 SKU 规则。', review_required=True)

    skus = {str(rule.get('sku') or '').strip() for rule in matches if str(rule.get('sku') or '').strip()}
    if len(matches) > 1 or len(skus) != 1:
            return SkuDecision(status='conflict', reason='命中多个或冲突的 SKU 规则。', review_required=True)

    rule = matches[0]
    return SkuDecision(
        status='ready',
        sku=next(iter(skus)),
        rule_id=str(rule.get('rule_id') or ''),
        confidence=float(rule.get('confidence') or 1.0),
            reason='唯一命中 SKU 规则。',
        review_required=False,
    )
