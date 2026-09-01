"""Neutral product catalogue.

This module contains product identity facts only.  It deliberately does not
contain customization, shipment, declaration, or browser-automation rules so
those workflows cannot accidentally call into one another.
"""

from __future__ import annotations

import re
from types import MappingProxyType


TENT_TOP_SKU_BY_SIZE = MappingProxyType(
    {
        "3x3m": "10x10-Canopy-Topper",
        "3x4.5m": "10x15-Canopy-Topper",
        "3x6m": "10x20-Canopy-Topper",
    }
)
TENT_TOP_SKUS = frozenset(TENT_TOP_SKU_BY_SIZE.values())

# Exact tent-frame SKUs emitted by the shared tent SKU planner.  Keep this
# finite catalogue at the neutral product-identity boundary so shipment
# classification and SKU generation cannot disagree about whether a standalone
# frame belongs to the tent family.  ``-RAIL`` is a frame variant, not a
# different product category.
_TENT_FRAME_SIZE_PREFIXES = ("10X10", "10X15", "10X20")
_TENT_FRAME_PROFILES = (
    "38MM-SQUARE",
    "40MM-SQUARE",
    "40MM-HEX",
    "50MM-HEX",
)
TENT_FRAME_SKUS = frozenset(
    f"{size}-FRAME-{profile}{rail_suffix}"
    for size in _TENT_FRAME_SIZE_PREFIXES
    for profile in _TENT_FRAME_PROFILES
    for rail_suffix in ("", "-RAIL")
)

# Exact tent accessory ASINs observed in the confirmed 2026-02-27 through
# 2026-08-27 Lingxing replay.  They participate in Alibaba logistics
# classification only; keeping them outside the customization ASIN catalogue
# avoids inventing parent/child, size, or contact-prompt rules that are not
# known for these independent products.
TENT_LOGISTICS_ONLY_ASINS = frozenset(
    {
        "B0DM1JHFYD",  # 10x10 tent roller bag
        "B0DP4DQZND",  # 10x10 38/40mm tent frame
    }
)


def normalize_product_sku(value: object) -> str:
    """Normalize harmless SKU formatting differences without fuzzy guessing."""

    return re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())


_NORMALIZED_TENT_FRAME_SKUS = frozenset(
    normalize_product_sku(value) for value in TENT_FRAME_SKUS
)


def is_tent_frame_sku(value: object) -> bool:
    """Return whether one exact SKU is a reviewed standalone tent frame."""

    return normalize_product_sku(value) in _NORMALIZED_TENT_FRAME_SKUS
