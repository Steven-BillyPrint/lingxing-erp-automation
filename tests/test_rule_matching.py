from __future__ import annotations

from lingxing_automation.rule_matching import lookup_with_plural_variants, plural_key_variants


def test_plural_key_variants_toggles_business_words_only():
    """验证规则匹配中的复数键变体 toggles 业务 words 仅场景。"""
    variants = plural_key_variants("edges options")

    assert "edge options" in variants
    assert "edges option" in variants
    assert "edge option" in variants


def test_lookup_with_plural_variants_matches_only_when_unambiguous():
    """验证规则匹配中的查找带有复数变体匹配仅当 unambiguous场景。"""
    result = lookup_with_plural_variants({"edge option": "edge-value"}, "edges options")

    assert result.matched is True
    assert result.value == "edge-value"


def test_lookup_with_plural_variants_reports_ambiguous_candidates():
    """验证规则匹配中的查找带有复数变体报告歧义候选场景。"""
    result = lookup_with_plural_variants(
        {
            "edge options": "A",
            "edges option": "B",
        },
        "edges options",
    )

    assert result.matched is False
    assert result.ambiguous is True
