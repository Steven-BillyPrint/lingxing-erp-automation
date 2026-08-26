"""Integration boundary between the shared product catalog and Alibaba templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from erp_automation.domain.product_catalog import (
    TENT_TOP_SKUS,
    normalize_product_sku,
)
from lingxing_automation.products.catalog import (
    PRODUCT_TYPE_TENT,
    identify_product,
    identify_product_type_from_sku,
)
from lingxing_automation.products.tents import get_wall_only_asin_kind

from .alibaba_ordering import (
    DEFAULT_PRODUCT_CATEGORY_REGISTRY,
    ProductClassification,
    ProductEvidence,
    extract_order_product_rows,
)


_NORMALIZED_TENT_TOP_SKUS = frozenset(
    normalize_product_sku(value) for value in TENT_TOP_SKUS
)


def resolve_catalog_product_type(identifier: str) -> str:
    """Resolve one SKU or ASIN without leaking catalog details into draft rules."""

    if normalize_product_sku(identifier) in _NORMALIZED_TENT_TOP_SKUS:
        return PRODUCT_TYPE_TENT
    sku_type = identify_product_type_from_sku(identifier)
    if sku_type and sku_type != PRODUCT_TYPE_TENT:
        return sku_type
    identity = identify_product(identifier)
    if (
        identity is not None
        and identity.product_type == PRODUCT_TYPE_TENT
        and get_wall_only_asin_kind(identifier) is not None
    ):
        return ""
    return identity.product_type if identity is not None else ""


def classify_order_product(payload: Mapping[str, Any]) -> ProductClassification:
    """Select the first supported Lingxing row through the catalog adapter."""

    evidence_rows = tuple(
        tuple(
            ProductEvidence(
                identifier=identifier,
                product_type=resolve_catalog_product_type(identifier),
            )
            for identifier in row
        )
        for row in extract_order_product_rows(payload)
    )
    return DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify_rows(evidence_rows)
