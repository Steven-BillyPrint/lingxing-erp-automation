from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Generic, Mapping, TypeVar


T = TypeVar("T")

# Keep this intentionally small. These are customization/business-option words
# that Amazon has returned with inconsistent singular/plural spelling.
PLURAL_VARIANT_BASE_WORDS = frozenset(
    {
        "bag",
        "banner",
        "corner",
        "design",
        "edge",
        "fabric",
        "frame",
        "grommet",
        "image",
        "input",
        "kit",
        "magnet",
        "material",
        "method",
        "option",
        "package",
        "piece",
        "pocket",
        "proof",
        "rail",
        "runner",
        "sandbag",
        "set",
        "shape",
        "side",
        "table",
        "tablecloth",
        "text",
        "wall",
    }
)


@dataclass(frozen=True)
class RuleLookupResult(Generic[T]):
    matched: bool
    ambiguous: bool = False
    value: T | None = None
    matched_keys: tuple[str, ...] = ()


def _word_plural_variants(word: str) -> tuple[str, ...]:
    """生成单个英文单词的保守单复数变体。"""
    lower = word.lower()
    variants: list[str] = []
    if lower.endswith("s"):
        singular = lower[:-1]
        if singular in PLURAL_VARIANT_BASE_WORDS:
            variants.append(singular)
        if lower.endswith("es"):
            singular_es = lower[:-2]
            if singular_es in PLURAL_VARIANT_BASE_WORDS:
                variants.append(singular_es)
    elif lower in PLURAL_VARIANT_BASE_WORDS:
        variants.append(f"{lower}s")
        if lower.endswith(("s", "x", "z")) or lower.endswith(("ch", "sh")):
            variants.append(f"{lower}es")
    return tuple(dict.fromkeys(variants))


def plural_key_variants(key: str, *, max_variants: int = 128) -> tuple[str, ...]:
    """返回规范化键的保守英文单复数变体。"""

    text = str(key or "")
    matches = list(re.finditer(r"[A-Za-z]+", text))
    if not matches:
        return ()

    parts: list[str] = []
    choices: list[tuple[str, ...]] = []
    last = 0
    for match in matches:
        parts.append(text[last : match.start()])
        word = match.group(0)
        choices.append((word, *_word_plural_variants(word)))
        last = match.end()
    parts.append(text[last:])

    variants: list[tuple[int, int, str]] = []
    for index, selected_words in enumerate(itertools.product(*choices)):
        candidate = "".join(
            part + selected_words[word_index]
            for word_index, part in enumerate(parts[:-1])
        ) + parts[-1]
        if candidate == text:
            continue
        changed_count = sum(
            1
            for selected, original_choices in zip(selected_words, choices, strict=True)
            if selected != original_choices[0]
        )
        variants.append((changed_count, index, candidate))
        if len(variants) >= max_variants:
            break

    variants.sort(key=lambda item: (item[0], item[1]))
    return tuple(dict.fromkeys(candidate for _, _, candidate in variants))


def normalized_key_matches_any(key: str, expected_keys: set[str] | frozenset[str]) -> bool:
    """判断规范化后的键是否匹配任一候选规则键。"""
    if key in expected_keys:
        return True
    return any(candidate in expected_keys for candidate in plural_key_variants(key))


def lookup_with_plural_variants(mapping: Mapping[str, T], key: str) -> RuleLookupResult[T]:
    """使用单复数兼容规则查找选项值，并识别歧义匹配。"""
    if key in mapping:
        return RuleLookupResult(matched=True, value=mapping[key], matched_keys=(key,))

    matches: list[tuple[str, T]] = [
        (candidate, mapping[candidate])
        for candidate in plural_key_variants(key)
        if candidate in mapping
    ]
    if not matches:
        return RuleLookupResult(matched=False)

    first_value = matches[0][1]
    matched_keys = tuple(candidate for candidate, _ in matches)
    if all(value == first_value for _, value in matches):
        return RuleLookupResult(matched=True, value=first_value, matched_keys=matched_keys)
    return RuleLookupResult(matched=False, ambiguous=True, matched_keys=matched_keys)
