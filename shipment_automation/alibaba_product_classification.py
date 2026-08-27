"""Integration boundary between the shared product catalog and Alibaba templates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from erp_automation.domain.product_catalog import (
    TENT_LOGISTICS_ONLY_ASINS,
    TENT_TOP_SKUS,
    normalize_product_sku,
)
from lingxing_automation.products.catalog import (
    PRODUCT_TYPE_TENT,
    identify_product,
    identify_product_type_from_sku,
)
from lingxing_automation.products.tents import normalize_asin

from .alibaba_ordering import (
    DEFAULT_PRODUCT_CATEGORY_REGISTRY,
    ProductClassification,
    ProductEvidence,
    extract_order_product_identifier_rows_with_amount,
)


_NORMALIZED_TENT_TOP_SKUS = frozenset(
    normalize_product_sku(value) for value in TENT_TOP_SKUS
)


def resolve_catalog_product_type(
    identifier: str,
    *,
    source_kind: str = "",
) -> str:
    """Resolve one SKU or ASIN without leaking catalog details into draft rules."""

    normalized_asin = normalize_asin(identifier)
    if normalized_asin in TENT_LOGISTICS_ONLY_ASINS:
        return PRODUCT_TYPE_TENT
    if source_kind == "asin":
        identity = identify_product(identifier)
        return identity.product_type if identity is not None else ""
    if normalize_product_sku(identifier) in _NORMALIZED_TENT_TOP_SKUS:
        return PRODUCT_TYPE_TENT
    sku_type = identify_product_type_from_sku(identifier)
    if sku_type and sku_type != PRODUCT_TYPE_TENT:
        return sku_type
    identity = identify_product(identifier)
    return identity.product_type if identity is not None else ""


def classify_order_product(payload: Mapping[str, Any]) -> ProductClassification:
    """Classify a complete Lingxing order through the shared catalog adapter."""

    evidence_rows = tuple(
        tuple(
            ProductEvidence(
                identifier=item.identifier,
                product_type=resolve_catalog_product_type(
                    item.identifier,
                    source_kind=item.source_kind,
                ),
                source_kind=(
                    "asin"
                    if item.source_kind == "asin"
                    and normalize_asin(item.identifier) is not None
                    else ("sku" if item.source_kind == "sku" else "")
                ),
                sales_amount=row.sales_amount,
                sales_currency=row.sales_currency,
                amount_status=row.amount_status,
            )
            for item in row.identifiers
        )
        for row in extract_order_product_identifier_rows_with_amount(payload)
    )
    return DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify_rows(evidence_rows)
