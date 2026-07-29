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


def normalize_product_sku(value: object) -> str:
    """Normalize harmless SKU formatting differences without fuzzy guessing."""

    return re.sub(r"[^0-9a-z]+", "", str(value or "").casefold())
