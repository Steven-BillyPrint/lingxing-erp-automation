"""Pure rules for preparing an Alibaba international-logistics draft.

The browser adapter is intentionally kept out of this module.  All decisions
can therefore be tested without opening Lingxing or Alibaba, and new product
categories can be registered without changing the tent declaration rules.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
from typing import Any


class AlibabaOrderRuleError(ValueError):
    """The draft cannot be prepared safely without operator correction."""


class UnsupportedProductError(AlibabaOrderRuleError):
    """No registered product category matched the order SKUs."""


class AmbiguousProductError(AlibabaOrderRuleError):
    """More than one registered category matched the same order."""


class ProductCategory(StrEnum):
    TENT = "tent"
    VINYL_BANNER = "vinyl_banner"
    WALL_DECAL = "wall_decal"
    CUSTOM_TABLECLOTH = "custom_tablecloth"
    X_BANNER_STAND = "x_banner_stand"
    BANNER_STAND = "banner_stand"

    @property
    def label(self) -> str:
        return {
            ProductCategory.TENT: "帐篷类",
            ProductCategory.VINYL_BANNER: "喷绘类",
            ProductCategory.WALL_DECAL: "车贴类",
            ProductCategory.CUSTOM_TABLECLOTH: "定制桌布类",
            ProductCategory.X_BANNER_STAND: "X展架类",
            ProductCategory.BANNER_STAND: "易拉宝类",
        }[self]


@dataclass(frozen=True)
class ProductCategoryDefinition:
    key: ProductCategory | str
    label: str
    product_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProductEvidence:
    identifier: str
    product_type: str
    source_kind: str = ""
    sales_amount: Decimal | None = None
    sales_currency: str = ""
    amount_status: str = "missing"


@dataclass(frozen=True)
class OrderProductIdentifier:
    """One product identifier read from an exact Lingxing item row."""

    identifier: str
    source_kind: str


@dataclass(frozen=True)
class OrderProductIdentifierRow:
    """Typed identifiers and authoritative sales amount for one item row."""

    identifiers: tuple[OrderProductIdentifier, ...]
    sales_amount: Decimal | None = None
    sales_currency: str = ""
    amount_status: str = "missing"


@dataclass(frozen=True)
class ProductClassification:
    category: ProductCategory | str
    label: str
    order_skus: tuple[str, ...]
    matched_skus: tuple[str, ...]
    unmatched_identifiers: tuple[str, ...] = ()
    selected_sales_amount: Decimal | None = None
    selected_sales_currency: str = ""
    selection_reason: str = "single_category"


class ProductCategoryRegistry:
    """Ordered product-evidence registry used by Alibaba declaration templates."""

    def __init__(self, definitions: Iterable[ProductCategoryDefinition]) -> None:
        self._definitions = tuple(definitions)
        if not self._definitions:
            raise ValueError("产品分类注册表不能为空。")

    def _matches_product_type(
        self,
        product_type: str,
    ) -> tuple[ProductCategoryDefinition, ...]:
        return tuple(
            definition
            for definition in self._definitions
            if product_type and product_type in definition.product_types
        )

    def classify_rows(
        self,
        rows: Iterable[Iterable[ProductEvidence]],
    ) -> ProductClassification:
        """Classify the complete order without depending on product-row order."""

        normalized_rows = tuple(tuple(row) for row in rows)
        order_identifiers = tuple(
            dict.fromkeys(
                evidence.identifier
                for row in normalized_rows
                for evidence in row
                if evidence.identifier
            )
        )
        matched_by_definition: dict[
            ProductCategoryDefinition,
            list[str],
        ] = {}
        amount_rows_by_definition: dict[
            ProductCategoryDefinition,
            list[ProductEvidence],
        ] = {}
        unimplemented_by_type: dict[str, list[str]] = {}
        unknown_asin_rows: list[tuple[str, ...]] = []

        for row in normalized_rows:
            row_matches: dict[
                ProductCategoryDefinition,
                list[str],
            ] = {}
            row_asin_matches: dict[
                ProductCategoryDefinition,
                list[str],
            ] = {}
            row_unimplemented: list[ProductEvidence] = []
            row_unknown_asins: list[str] = []
            for evidence in row:
                definitions = self._matches_product_type(evidence.product_type)
                if definitions:
                    for definition in definitions:
                        row_matches.setdefault(definition, []).append(
                            evidence.identifier
                        )
                        if evidence.source_kind == "asin":
                            row_asin_matches.setdefault(definition, []).append(
                                evidence.identifier
                            )
                    continue
                if evidence.product_type:
                    row_unimplemented.append(evidence)
                elif evidence.source_kind == "asin":
                    row_unknown_asins.append(evidence.identifier)
            # A recognized ASIN is the authoritative product identity for one
            # Lingxing row.  SKU patterns remain the fallback when that row has
            # no catalogued ASIN.  This prevents a component/local SKU from
            # overriding an exact marketplace product identity.
            effective_row_matches = (
                {
                    definition: row_matches[definition]
                    for definition in row_asin_matches
                }
                if row_asin_matches
                else row_matches
            )
            for definition, identifiers in effective_row_matches.items():
                matched_by_definition.setdefault(definition, []).extend(identifiers)
                amount_rows_by_definition.setdefault(definition, []).append(row[0])
            for evidence in row_unimplemented:
                if row_asin_matches and evidence.source_kind != "asin":
                    continue
                unimplemented_by_type.setdefault(
                    evidence.product_type,
                    [],
                ).append(evidence.identifier)
            if row_unknown_asins and not effective_row_matches:
                unknown_asin_rows.append(tuple(dict.fromkeys(row_unknown_asins)))

        if unimplemented_by_type:
            product_types = "、".join(unimplemented_by_type)
            identifiers = "、".join(
                dict.fromkeys(
                    identifier
                    for values in unimplemented_by_type.values()
                    for identifier in values
                )
            )
            raise UnsupportedProductError(
                "订单商品已识别为"
                f" {product_types}（{identifiers}），"
                "但尚未配置阿里巴巴申报分类/模板，请人工处理。"
            )

        if unknown_asin_rows:
            identifiers = "、".join(
                dict.fromkeys(
                    identifier
                    for row in unknown_asin_rows
                    for identifier in row
                )
            )
            raise UnsupportedProductError(
                f"订单包含尚未录入商品目录的 ASIN（{identifiers}），请人工处理。"
            )

        selected_sales_amount: Decimal | None = None
        selected_sales_currency = ""
        selection_reason = "single_category"
        if len(matched_by_definition) > 1:
            tent_definition = next(
                (
                    definition
                    for definition in matched_by_definition
                    if definition.key == ProductCategory.TENT
                ),
                None,
            )
            if tent_definition is not None:
                # Confirmed Lingxing history contains tent bundles with
                # tablecloth and flag rows.  The shared product-domain rule is
                # that a tent main product wins for shipment-facing workflows;
                # make that priority explicit instead of depending on row order.
                matched_by_definition = {
                    tent_definition: matched_by_definition[tent_definition]
                }
                selection_reason = "tent_priority"
            else:
                totals: dict[ProductCategoryDefinition, Decimal] = {}
                currencies: set[str] = set()
                invalid_labels: list[str] = []
                for definition in matched_by_definition:
                    amount_rows = amount_rows_by_definition.get(definition, [])
                    if not amount_rows or any(
                        evidence.amount_status != "valid"
                        or evidence.sales_amount is None
                        or not evidence.sales_currency
                        for evidence in amount_rows
                    ):
                        invalid_labels.append(definition.label)
                        continue
                    totals[definition] = sum(
                        (
                            evidence.sales_amount
                            for evidence in amount_rows
                            if evidence.sales_amount is not None
                        ),
                        Decimal("0"),
                    )
                    currencies.update(
                        evidence.sales_currency for evidence in amount_rows
                    )
                amounts_comparable = (
                    not invalid_labels
                    and len(totals) == len(matched_by_definition)
                    and len(currencies) == 1
                )
                if amounts_comparable:
                    highest = max(totals.values())
                    winners = frozenset(
                        definition
                        for definition, amount in totals.items()
                        if amount == highest
                    )
                    # A non-tent mixed order must never depend on Lingxing's
                    # item-row order.  Equal totals use the registry's stable
                    # declaration priority as a deterministic final tie-break.
                    winner = next(
                        definition
                        for definition in self._definitions
                        if definition in winners
                    )
                    selected_sales_amount = highest
                    selected_sales_currency = next(iter(currencies))
                    selection_reason = (
                        "highest_sales_amount"
                        if len(winners) == 1
                        else "highest_sales_amount_tie_priority"
                    )
                else:
                    # The operator requested that cross-category non-tent
                    # orders no longer block.  If amounts cannot be compared
                    # safely, fall back to the same stable declaration
                    # priority instead of guessing from row order.
                    winner = next(
                        definition
                        for definition in self._definitions
                        if definition in matched_by_definition
                    )
                    selection_reason = "category_priority_amount_fallback"
                matched_by_definition = {
                    winner: matched_by_definition[winner]
                }

        if matched_by_definition:
            definition, matched = next(iter(matched_by_definition.items()))
            matched_set = frozenset(matched)
            return ProductClassification(
                category=definition.key,
                label=definition.label,
                order_skus=order_identifiers,
                matched_skus=tuple(dict.fromkeys(matched)),
                unmatched_identifiers=tuple(
                    dict.fromkeys(
                        evidence.identifier
                        for row in normalized_rows
                        for evidence in row
                        if evidence.identifier not in matched_set
                    )
                ),
                selected_sales_amount=selected_sales_amount,
                selected_sales_currency=selected_sales_currency,
                selection_reason=selection_reason,
            )

        visible = "、".join(order_identifiers) if order_identifiers else "无 SKU/ASIN"
        raise UnsupportedProductError(
            f"订单商品未匹配已支持的物流分类（{visible}），请人工处理。"
        )

DEFAULT_PRODUCT_CATEGORY_REGISTRY = ProductCategoryRegistry(
    (
        ProductCategoryDefinition(
            key=ProductCategory.TENT,
            label=ProductCategory.TENT.label,
            product_types=frozenset({"tent"}),
        ),
        ProductCategoryDefinition(
            key=ProductCategory.VINYL_BANNER,
            label=ProductCategory.VINYL_BANNER.label,
            product_types=frozenset(
                {"vinyl_banners", "feather_flags"}
            ),
        ),
        ProductCategoryDefinition(
            key=ProductCategory.WALL_DECAL,
            label=ProductCategory.WALL_DECAL.label,
            product_types=frozenset({"posters", "car_magnet"}),
        ),
        ProductCategoryDefinition(
            key=ProductCategory.CUSTOM_TABLECLOTH,
            label=ProductCategory.CUSTOM_TABLECLOTH.label,
            product_types=frozenset(
                {
                    "tablecloths",
                    "table_runners",
                    "pop_up_displays",
                }
            ),
        ),
        ProductCategoryDefinition(
            key=ProductCategory.X_BANNER_STAND,
            label=ProductCategory.X_BANNER_STAND.label,
            product_types=frozenset({"x_stands"}),
        ),
        ProductCategoryDefinition(
            key=ProductCategory.BANNER_STAND,
            label=ProductCategory.BANNER_STAND.label,
            product_types=frozenset({"roll_up_banners"}),
        ),
    )
)


_ITEM_CONTAINER_KEYS = frozenset(
    {
        "item_info",
        "iteminfo",
        "order_item",
        "orderitem",
        "order_item_info",
        "orderiteminfo",
        "order_item_list",
        "orderitemlist",
        "order_items",
        "orderitems",
        "items",
        "product_list",
        "productlist",
        "products",
    }
)
_SKU_KEYS = (
    "local_sku",
    "localSku",
    "seller_sku",
    "sellerSku",
    "msku",
    "sku",
    "product_sku",
    "productSku",
)
_NORMALIZED_SKU_KEYS = frozenset(key.casefold() for key in _SKU_KEYS)
_ASIN_KEYS = (
    "asin",
    "amazon_asin",
    "amazonAsin",
    "product_id",
    "productId",
    "product_no",
    "productNo",
    "parent_asin",
    "parentAsin",
    "child_asin",
    "childAsin",
)
_NORMALIZED_ASIN_KEYS = frozenset(
    key.replace("-", "_").casefold() for key in _ASIN_KEYS
)
_PRODUCT_IDENTIFIER_KEYS = _NORMALIZED_SKU_KEYS | _NORMALIZED_ASIN_KEYS
ORDER_PRODUCT_EVIDENCE_SNAPSHOT_KEY = (
    "_lingxing_order_list_product_identity_snapshot"
)
_SALES_AMOUNT_KEYS = (
    "sales_income",
    "salesIncome",
    "sales_revenue",
    "salesRevenue",
    "sales_revenue_amount",
    "salesRevenueAmount",
    "sales_proceeds",
    "salesProceeds",
    "sale_income",
    "saleIncome",
    "item_income",
    "itemIncome",
    "order_income",
    "orderIncome",
    "item_sales_amount",
    "itemSalesAmount",
    "sales_amount",
    "salesAmount",
    "revenue_amount",
    "revenueAmount",
    "income",
    "revenue",
)
_SALES_CURRENCY_KEYS = (
    "sales_currency",
    "salesCurrency",
    "sales_income_currency",
    "salesIncomeCurrency",
    "sales_revenue_currency",
    "salesRevenueCurrency",
    "amount_currency",
    "amountCurrency",
    "currency_code",
    "currencyCode",
    "currency_name",
    "currencyName",
    "currency",
)


def _mapping_text(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and (text := str(value).strip()):
            return text
    return ""


def _mapping_value(
    mapping: Mapping[str, Any],
    keys: Sequence[str],
) -> tuple[bool, object | None]:
    for key in keys:
        if key in mapping:
            return True, mapping[key]
    return False, None


def _normalized_sales_currency(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _parse_sales_amount(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    match = re.fullmatch(
        r"(?:[A-Za-z]{3}|[$€£¥])?\s*"
        r"([+-]?(?:\d[\d,]*(?:\.\d*)?|\.\d+))\s*"
        r"(?:[A-Za-z]{3})?",
        text,
    )
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _amount_evidence_from_mapping(
    mapping: Mapping[str, Any],
    *,
    inherited_currency: str = "",
) -> tuple[Decimal | None, str, str]:
    has_amount, raw_amount = _mapping_value(mapping, _SALES_AMOUNT_KEYS)
    currency = _normalized_sales_currency(
        _mapping_text(mapping, _SALES_CURRENCY_KEYS) or inherited_currency
    )
    if not has_amount or raw_amount is None or not str(raw_amount).strip():
        return None, currency, "missing"
    amount = _parse_sales_amount(raw_amount)
    if amount is None:
        return None, currency, "invalid"
    return amount, currency, "valid"


def extract_order_skus(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Read SKU values from common Lingxing order-detail item containers."""

    found: list[str] = []

    def visit(value: object, *, inside_items: bool = False) -> None:
        if isinstance(value, Mapping):
            if inside_items:
                # Lingxing's order-detail API currently returns ``order_item``
                # rows with both ``MSKU`` (the marketplace identifier) and
                # ``sku`` (the local product identifier).  Keep every usable
                # identifier instead of choosing one by field priority: the
                # supported product may be present in either field, depending
                # on the order's workflow stage.
                for key, raw_sku in value.items():
                    if str(key).casefold() not in _NORMALIZED_SKU_KEYS:
                        continue
                    sku = str(raw_sku or "").strip()
                    if sku:
                        found.append(sku)
            for key, child in value.items():
                normalized_key = str(key).replace("-", "_").casefold()
                visit(
                    child,
                    inside_items=inside_items or normalized_key in _ITEM_CONTAINER_KEYS,
                )
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                visit(child, inside_items=inside_items)

    visit(payload)
    return tuple(dict.fromkeys(found))


def extract_order_product_rows(
    payload: Mapping[str, Any],
) -> tuple[tuple[str, ...], ...]:
    """Read ordered SKU/ASIN evidence grouped by Lingxing product row."""

    return tuple(
        tuple(item.identifier for item in row.identifiers)
        for row in extract_order_product_identifier_rows_with_amount(payload)
    )


def extract_order_product_identifier_rows(
    payload: Mapping[str, Any],
) -> tuple[tuple[OrderProductIdentifier, ...], ...]:
    """Read typed SKU/ASIN evidence grouped by exact Lingxing product row."""

    return tuple(
        row.identifiers
        for row in extract_order_product_identifier_rows_with_amount(payload)
    )


def extract_order_product_identifier_rows_with_amount(
    payload: Mapping[str, Any],
) -> tuple[OrderProductIdentifierRow, ...]:
    """Read exact Lingxing item rows with identifiers, sales amount and currency.

    When desktop order-detail enrichment has attached an exact order-list
    snapshot, that list snapshot is authoritative.  Preferring it also avoids
    counting the same item once from detail and again from the list response.
    """

    snapshot = payload.get(ORDER_PRODUCT_EVIDENCE_SNAPSHOT_KEY)
    source_payload = snapshot if isinstance(snapshot, Mapping) else payload
    root_currency = _normalized_sales_currency(
        _mapping_text(source_payload, _SALES_CURRENCY_KEYS)
        or _mapping_text(payload, _SALES_CURRENCY_KEYS)
    )

    snapshot_rows = source_payload.get("rows")
    if isinstance(snapshot_rows, Sequence) and not isinstance(
        snapshot_rows, (str, bytes, bytearray)
    ):
        canonical_rows: list[OrderProductIdentifierRow] = []
        for raw_row in snapshot_rows:
            if not isinstance(raw_row, Mapping):
                continue
            raw_identifiers = raw_row.get("identifiers")
            if not isinstance(raw_identifiers, Sequence) or isinstance(
                raw_identifiers, (str, bytes, bytearray)
            ):
                continue
            identifiers = tuple(
                dict.fromkeys(
                    OrderProductIdentifier(
                        identifier=str(item.get("identifier") or "").strip(),
                        source_kind=str(item.get("source_kind") or "").strip(),
                    )
                    for item in raw_identifiers
                    if isinstance(item, Mapping)
                    and str(item.get("identifier") or "").strip()
                    and str(item.get("source_kind") or "").strip()
                    in {"asin", "sku"}
                )
            )
            if not identifiers:
                continue
            amount, currency, amount_status = _amount_evidence_from_mapping(
                raw_row,
                inherited_currency=root_currency,
            )
            raw_status = str(raw_row.get("amount_status") or "").strip()
            if raw_status in {"missing", "invalid"} and amount is None:
                amount_status = raw_status
            canonical_rows.append(
                OrderProductIdentifierRow(
                    identifiers=identifiers,
                    sales_amount=amount,
                    sales_currency=currency,
                    amount_status=amount_status,
                )
            )
        return tuple(canonical_rows)

    rows: list[OrderProductIdentifierRow] = []

    def visit(
        value: object,
        *,
        inside_items: bool = False,
        inherited_currency: str = "",
    ) -> None:
        if isinstance(value, Mapping):
            local_currency = _normalized_sales_currency(
                _mapping_text(value, _SALES_CURRENCY_KEYS)
                or inherited_currency
            )
            if inside_items:
                row = tuple(
                    dict.fromkeys(
                        OrderProductIdentifier(
                            identifier=text,
                            source_kind=(
                                "asin"
                                if str(key).replace("-", "_").casefold()
                                in _NORMALIZED_ASIN_KEYS
                                else "sku"
                            ),
                        )
                        for key, raw_identifier in value.items()
                        if str(key).replace("-", "_").casefold()
                        in _PRODUCT_IDENTIFIER_KEYS
                        if (text := str(raw_identifier or "").strip())
                    )
                )
                if row:
                    amount, currency, amount_status = _amount_evidence_from_mapping(
                        value,
                        inherited_currency=local_currency,
                    )
                    rows.append(
                        OrderProductIdentifierRow(
                            identifiers=row,
                            sales_amount=amount,
                            sales_currency=currency,
                            amount_status=amount_status,
                        )
                    )
            for key, child in value.items():
                normalized_key = str(key).replace("-", "_").casefold()
                visit(
                    child,
                    inside_items=inside_items or normalized_key in _ITEM_CONTAINER_KEYS,
                    inherited_currency=local_currency,
                )
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                visit(
                    child,
                    inside_items=inside_items,
                    inherited_currency=inherited_currency,
                )

    visit(source_payload, inherited_currency=root_currency)
    return tuple(rows)


_ADDRESS_CONTAINER_KEYS = frozenset(
    {
        "receive_info",
        "receiveinfo",
        "receiver_info",
        "receiverinfo",
        "recipient",
        "recipient_info",
        "recipientinfo",
        "address_info",
        "addressinfo",
        "shipping_address",
        "shippingaddress",
        "delivery_address",
        "deliveryaddress",
        "buyer_info",
        "buyerinfo",
        "buyers_info",
        "buyersinfo",
        "buyer",
        "customer_info",
        "customerinfo",
        "customer",
        "contact_info",
        "contactinfo",
    }
)

_ADDRESS_ALIASES: dict[str, tuple[str, ...]] = {
    "company": (
        "company",
        "company_name",
        "companyName",
        "company_name_en",
        "companyNameEn",
        "receiver_company",
        "receiverCompany",
        "receiver_company_name",
        "receiverCompanyName",
        "recipient_company",
    ),
    "recipient": (
        "recipient",
        "recipient_name",
        "recipientName",
        "receiver",
        "receiver_name",
        "receiverName",
        "receiver_full_name",
        "receiverFullName",
        "consignee",
        "name",
        "contact_person",
        "contactPerson",
        "buyer_name",
        "buyerName",
        "customer_name",
        "customerName",
    ),
    "country_code": (
        "country_code",
        "countryCode",
        "country_iso2",
        "countryIso2",
        "country_abbr",
        "countryAbbr",
        "receiver_country_code",
        "receiverCountryCode",
    ),
    "country_name": (
        "country",
        "country_name",
        "countryName",
        "receiver_country",
        "receiverCountry",
        "receiver_country_name",
        "receiverCountryName",
    ),
    "province": (
        "province",
        "province_name",
        "provinceName",
        "state",
        "state_name",
        "stateName",
        "state_or_region",
        "stateOrRegion",
        "receiver_state",
        "receiverState",
        "region",
    ),
    "city": (
        "city",
        "city_name",
        "cityName",
        "town",
        "receiver_city",
        "receiverCity",
        "receiver_city_name",
        "receiverCityName",
    ),
    "address1": (
        "address1",
        "address_1",
        "address_line1",
        "addressLine1",
        "address_line_1",
        "address",
        "receiver_address",
        "receiverAddress",
        "receiver_address1",
        "receiverAddress1",
        "receiver_address_line1",
        "receiverAddressLine1",
        "street",
        "street_address",
        "streetAddress",
        "detail_address",
        "detailAddress",
        "short_address",
        "shortAddress",
    ),
    "address2": (
        "address2",
        "address_2",
        "address_line2",
        "addressLine2",
        "address_line_2",
        "receiver_address2",
        "receiverAddress2",
        "receiver_address_line2",
        "receiverAddressLine2",
        "street2",
        "detail_address2",
        "detailAddress2",
    ),
    "address3": (
        "address3",
        "address_3",
        "address_line3",
        "addressLine3",
        "address_line_3",
        "receiver_address3",
        "receiverAddress3",
        "receiver_address_line3",
        "receiverAddressLine3",
        "detail_address3",
        "detailAddress3",
    ),
    "doorplate": (
        "doorplate_no",
        "doorplateNo",
        "doorplate",
        "house_number",
        "houseNumber",
    ),
    "postal_code": (
        "postal_code",
        "postalCode",
        "zip",
        "zip_code",
        "zipCode",
        "postcode",
        "receiver_postal_code",
        "receiverPostalCode",
    ),
    "dial_code": (
        "phone_code",
        "phoneCode",
        "country_calling_code",
        "countryCallingCode",
        "dial_code",
        "dialCode",
        "receiver_phone_code",
        "receiverPhoneCode",
    ),
    "phone": (
        "phone",
        "mobile",
        "mobile_no",
        "mobileNo",
        "receiver_phone",
        "receiverPhone",
        "receiver_tel",
        "receiverTel",
        "receiver_mobile",
        "receiverMobile",
        "contact_phone",
        "contactPhone",
        "buyer_mobile",
        "buyerMobile",
        "buyer_phone",
        "buyerPhone",
        "customer_phone",
        "customerPhone",
        "telephone",
    ),
    "email": (
        "email",
        "receiver_email",
        "receiverEmail",
        "recipient_email",
        "buyer_email",
        "buyerEmail",
        "customer_email",
        "customerEmail",
        "contact_email",
        "contactEmail",
    ),
}


def _country_code(code: str, name: str) -> str:
    normalized = re.sub(r"[^A-Z]", "", code.upper())
    if len(normalized) == 2:
        return normalized
    folded = re.sub(r"[^a-z]", "", name.casefold())
    aliases = {
        "unitedstates": "US",
        "unitedstatesofamerica": "US",
        "usa": "US",
        "america": "US",
        "canada": "CA",
    }
    return aliases.get(folded, "")


_US_PROVINCES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
_CA_PROVINCES = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}


def province_name_for_alibaba(country_code: object, value: object) -> str:
    """Expand common state/province abbreviations to Alibaba option labels."""

    raw = re.sub(r"\s+", " ", str(value or "")).strip()
    if not raw:
        return ""
    country = str(country_code or "").strip().upper()
    options = _US_PROVINCES if country == "US" else _CA_PROVINCES if country == "CA" else {}
    upper = raw.upper().replace(".", "")
    if upper in options:
        return options[upper]
    for name in options.values():
        if raw.casefold() == name.casefold():
            return name
    return raw


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


_PHONE_EXTENSION_PATTERN = re.compile(
    r"^(?P<number>.*?)(?:\s*(?:ext(?:ension)?\.?|x|转)\s*:?\s*\d+)\s*$",
    flags=re.IGNORECASE,
)


def _phone_for_alibaba(
    raw_value: object,
    *,
    country_code: str,
    dial_code: str,
) -> tuple[str, str]:
    """Return the main phone number and preserve a virtual number as address text."""

    raw_phone = re.sub(r"\s+", " ", str(raw_value or "")).strip()
    extension_match = _PHONE_EXTENSION_PATTERN.fullmatch(raw_phone)
    number_part = extension_match.group("number") if extension_match else raw_phone
    phone = _digits(number_part)
    if dial_code and phone.startswith(dial_code) and len(phone) > 10:
        phone = phone[len(dial_code) :]

    if extension_match:
        if country_code in {"US", "CA"} and len(phone) != 10:
            raise AlibabaOrderRuleError(
                "领星订单的虚拟手机号无法提取 10 位主号码，请人工确认。"
            )
        return phone, raw_phone
    return phone, ""


def postal_first_five(value: object) -> str:
    """Return the US ZIP5 prefix, ignoring ZIP+4 separators."""

    compact = re.sub(r"\D", "", str(value or ""))
    return compact[:5]


def postal_code_for_alibaba(country_code: object, value: object) -> str:
    """Normalize a complete US or Canadian postal code for Alibaba."""

    country = str(country_code or "").strip().upper()
    raw = str(value or "").strip()
    if country == "US":
        postal_code = postal_first_five(raw)
        if len(postal_code) != 5:
            raise AlibabaOrderRuleError("美国订单邮编不是有效的 5 位 ZIP Code。")
        return postal_code
    if country == "CA":
        compact = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
        if not re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", compact):
            raise AlibabaOrderRuleError("加拿大订单邮编不是有效的 6 位 Postal Code。")
        return f"{compact[:3]} {compact[3:]}"
    raise AlibabaOrderRuleError(
        f"当前仅支持美国和加拿大帐篷订单，目的国 {country or '未知'} 请人工处理。"
    )


def split_address_lines(
    address1: object,
    address2: object = "",
    *,
    address1_limit: int = 35,
) -> tuple[str, str]:
    """Split on word boundaries and prove no address token was lost."""

    limit = int(address1_limit)
    if limit <= 0:
        raise ValueError("地址1字符上限必须大于零。")
    primary = re.sub(r"\s+", " ", str(address1 or "")).strip()
    existing_second = re.sub(r"\s+", " ", str(address2 or "")).strip()
    source = " ".join(part for part in (primary, existing_second) if part)
    if not primary:
        raise AlibabaOrderRuleError("领星订单缺少详细地址。")
    # Keep unit, apartment, suite, room, floor, lot or hash suffixes in address
    # line 2.  This preserves a readable street-only first line and respects
    # Alibaba's 35-character limit without discarding any address token.
    secondary_match = re.search(
        r"\s+(?=(?:APT\.?|APARTMENT|SUITE|STE\.?|UNIT|FLOOR|ROOM|RM\.?|LOT)"
        r"(?:\s|$)|#\s*\S+)",
        primary,
        flags=re.IGNORECASE,
    )
    inline_second = ""
    street = primary
    if secondary_match is not None:
        street = primary[: secondary_match.start()].strip()
        inline_second = primary[secondary_match.end() :].strip()
    if not street:
        raise AlibabaOrderRuleError("领星订单详细地址缺少可识别的街道部分。")
    words = street.split(" ")
    oversized = next((word for word in words if len(word) > limit), None)
    if oversized is not None:
        raise AlibabaOrderRuleError(
            f"地址中存在超过 {limit} 个字符且不能拆开的词组（{oversized}），请人工处理。"
        )
    first_words: list[str] = []
    remainder: list[str] = []
    for index, word in enumerate(words):
        candidate = " ".join((*first_words, word))
        if len(candidate) <= limit:
            first_words.append(word)
            continue
        remainder = words[index:]
        break
    first = " ".join(first_words)
    overflow = " ".join(remainder)
    second = " ".join(
        part for part in (overflow, inline_second, existing_second) if part
    )
    if " ".join(part for part in (first, second) if part) != source:
        raise AlibabaOrderRuleError("地址拆分完整性校验失败，请人工处理。")
    return first, second


@dataclass(frozen=True)
class ShippingAddress:
    company: str
    recipient: str
    country_code: str
    country_name: str
    province: str
    city: str
    address1: str
    address2: str
    postal_code: str
    dial_code: str
    phone: str
    email: str

def _candidate_address_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []

    def visit(value: object, *, named_address: bool = False) -> None:
        if isinstance(value, Mapping):
            if named_address:
                candidates.append(value)
            for key, child in value.items():
                normalized_key = str(key).replace("-", "_").casefold()
                visit(
                    child,
                    named_address=normalized_key in _ADDRESS_CONTAINER_KEYS,
                )
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for child in value:
                visit(child, named_address=named_address)

    visit(payload)
    candidates.append(payload)
    return candidates


def _address_score(mapping: Mapping[str, Any]) -> int:
    important = (
        "recipient",
        "country_code",
        "country_name",
        "province",
        "city",
        "address1",
        "postal_code",
        "phone",
        "email",
    )
    return sum(
        1
        for field in important
        if _mapping_text(mapping, _ADDRESS_ALIASES[field])
    )


def extract_shipping_address(payload: Mapping[str, Any]) -> ShippingAddress:
    """Extract and validate the complete receiver address from Lingxing detail."""

    candidates = _candidate_address_mappings(payload)
    source = max(candidates, key=_address_score)
    values: dict[str, str] = {}
    for field, aliases in _ADDRESS_ALIASES.items():
        primary = _mapping_text(source, aliases)
        if primary:
            values[field] = primary
            continue
        fallback_values = tuple(
            dict.fromkeys(
                value
                for candidate in candidates
                if (value := _mapping_text(candidate, aliases))
            )
        )
        if len(fallback_values) > 1:
            raise AlibabaOrderRuleError(
                f"领星订单存在多组不同的收货{field}信息，请人工确认。"
            )
        values[field] = fallback_values[0] if fallback_values else ""
    recipient = values["recipient"]
    company = values["company"] or recipient
    code = _country_code(values["country_code"], values["country_name"])
    country_name = values["country_name"] or {
        "US": "United States",
        "CA": "Canada",
    }.get(code, "")
    secondary_parts: list[str] = []
    combined_address = values["address1"].casefold()
    for field in ("address2", "address3", "doorplate"):
        part = re.sub(r"\s+", " ", values[field]).strip()
        if not part or part.casefold() in {"-", "--", "n/a", "none", "null"}:
            continue
        if part.casefold() in combined_address:
            continue
        secondary_parts.append(part)
        combined_address = f"{combined_address} {part.casefold()}".strip()
    address1, address2 = split_address_lines(
        values["address1"],
        " ".join(secondary_parts),
    )
    postal_code = postal_code_for_alibaba(code, values["postal_code"])
    dial_code = _digits(values["dial_code"])
    if not dial_code and code in {"US", "CA"}:
        dial_code = "1"
    phone, virtual_phone = _phone_for_alibaba(
        values["phone"],
        country_code=code,
        dial_code=dial_code,
    )
    if virtual_phone:
        address2 = " ".join(
            part for part in (address2, virtual_phone) if part
        )

    required = {
        "公司名/收件人": company,
        "收件人": recipient,
        "国家": code,
        "州/省": values["province"],
        "城市": values["city"],
        "地址": address1,
        "邮编": postal_code,
        "国家码": dial_code,
        "手机号码": phone,
        "邮箱": values["email"],
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise AlibabaOrderRuleError(
            "领星订单缺少阿里下单所需地址信息：" + "、".join(missing) + "。"
        )
    return ShippingAddress(
        company=company,
        recipient=recipient,
        country_code=code,
        country_name=country_name,
        province=province_name_for_alibaba(code, values["province"]),
        city=values["city"],
        address1=address1,
        address2=address2,
        postal_code=postal_code,
        dial_code=dial_code,
        phone=phone,
        email=values["email"],
    )


@dataclass(frozen=True)
class AlibabaRoute:
    name: str
    customs_mode: str = ""

    @property
    def is_ddp(self) -> bool:
        return bool(re.search(r"(?<![A-Z])DDP(?![A-Z])", self.name.upper())) or (
            self.customs_mode.strip().upper() == "DDP"
        )


@dataclass(frozen=True)
class ProductDeclaration:
    name_cn: str = "帐篷布顶"
    name_en: str = "Canopy Tent"
    material: str = "Polyester Fabri"
    purpose: str = "display"
    china_hs_code: str = "3926909090"
    destination_hs_code: str | None = "3926909989"
    quantity: int = 1
    declared_unit_price_usd: Decimal = Decimal("0")
    logistics_attribute: str = "普货"


# Backward-compatible import used by existing callers and tests.
TentDeclaration = ProductDeclaration


@dataclass(frozen=True)
class ProductDeclarationTemplate:
    name_cn: str
    name_en: str
    material: str
    purpose: str
    china_hs_code: str
    destination_hs_code: str


PRODUCT_DECLARATION_TEMPLATES: Mapping[
    ProductCategory,
    ProductDeclarationTemplate,
] = {
    ProductCategory.TENT: ProductDeclarationTemplate(
        name_cn="帐篷布顶",
        name_en="Canopy Tent",
        material="Polyester Fabri",
        purpose="display",
        china_hs_code="3926909090",
        destination_hs_code="3926909989",
    ),
    ProductCategory.VINYL_BANNER: ProductDeclarationTemplate(
        name_cn="喷绘",
        name_en="Vinyl Banners",
        material="Polyester",
        purpose="display",
        china_hs_code="6302539010",
        destination_hs_code="6302592000",
    ),
    ProductCategory.WALL_DECAL: ProductDeclarationTemplate(
        name_cn="车贴",
        name_en="wall decal",
        material="polyester",
        purpose="display",
        china_hs_code="9505900000",
        destination_hs_code="9505101000",
    ),
    ProductCategory.CUSTOM_TABLECLOTH: ProductDeclarationTemplate(
        name_cn="定制桌布",
        name_en="Custom Tablecloth",
        material="Polyester",
        purpose="display",
        china_hs_code="6302539010",
        destination_hs_code="6302592000",
    ),
    ProductCategory.X_BANNER_STAND: ProductDeclarationTemplate(
        name_cn="X展架",
        name_en="X Banner Stand",
        material="Polyester",
        purpose="Display",
        china_hs_code="6302539010",
        destination_hs_code="6302592000",
    ),
    ProductCategory.BANNER_STAND: ProductDeclarationTemplate(
        name_cn="易拉宝",
        name_en="Banner Stand",
        material="Polyester",
        purpose="display",
        china_hs_code="6302539010",
        destination_hs_code="6302592000",
    ),
}


def _weight(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AlibabaOrderRuleError("包裹总重量无效，请检查阿里页面。") from exc
    if not result.is_finite() or result <= 0:
        raise AlibabaOrderRuleError("包裹总重量必须大于 0kg。")
    return result


def declaration_price_usd(
    *,
    destination_country_code: str,
    total_weight_kg: object,
    route: AlibabaRoute,
    expedited: bool,
    heavy_or_frame: bool,
) -> Decimal:
    """Calculate the complete tent declaration-price rules."""

    country = str(destination_country_code or "").strip().upper()
    weight = _weight(total_weight_kg)
    if country not in {"US", "CA"}:
        raise AlibabaOrderRuleError(
            f"当前仅支持美国和加拿大帐篷订单，目的国 {country or '未知'} 请人工处理。"
        )
    if route.is_ddp:
        price = Decimal("800")
    elif country == "CA":
        price = Decimal("13") if weight < Decimal("15") else Decimal("99")
    elif country == "US" and heavy_or_frame:
        multiplier = Decimal("0.4") if expedited else Decimal("0.2")
        price = weight * multiplier
    else:
        price = Decimal("2.5")
    return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def non_tent_declaration_price_usd(*, total_weight_kg: object) -> Decimal:
    """Calculate the approved weight-only price tiers for non-tent templates."""

    weight = _weight(total_weight_kg)
    if weight <= Decimal("3"):
        return Decimal("1.01")
    if weight <= Decimal("10"):
        return Decimal("3.01")
    if weight < Decimal("15"):
        return Decimal("5.01")
    return Decimal("10.01")


def product_declaration(
    *,
    category: ProductCategory | str,
    destination_country_code: str,
    total_weight_kg: object,
    route: AlibabaRoute,
    expedited: bool,
    heavy_or_frame: bool,
) -> ProductDeclaration:
    """Build one declaration row from the selected ordered product template."""

    try:
        normalized_category = ProductCategory(str(category))
    except ValueError as exc:
        raise AlibabaOrderRuleError(f"不支持的阿里商品申报分类：{category}") from exc
    country = str(destination_country_code or "").strip().upper()
    if country not in {"US", "CA"}:
        raise AlibabaOrderRuleError(
            f"当前仅支持美国和加拿大订单，目的国 {country or '未知'} 请人工处理。"
        )
    template = PRODUCT_DECLARATION_TEMPLATES[normalized_category]
    if normalized_category is ProductCategory.TENT:
        price = declaration_price_usd(
            destination_country_code=country,
            total_weight_kg=total_weight_kg,
            route=route,
            expedited=expedited,
            heavy_or_frame=heavy_or_frame,
        )
    else:
        price = non_tent_declaration_price_usd(total_weight_kg=total_weight_kg)
    return ProductDeclaration(
        name_cn=template.name_cn,
        name_en=template.name_en,
        material=template.material,
        purpose=template.purpose,
        china_hs_code=template.china_hs_code,
        destination_hs_code=(
            None if country == "CA" else template.destination_hs_code
        ),
        declared_unit_price_usd=price,
    )


def tent_declaration(
    *,
    destination_country_code: str,
    total_weight_kg: object,
    route: AlibabaRoute,
    expedited: bool,
    heavy_or_frame: bool,
) -> TentDeclaration:
    return product_declaration(
        category=ProductCategory.TENT,
        destination_country_code=destination_country_code,
        total_weight_kg=total_weight_kg,
        route=route,
        expedited=expedited,
        heavy_or_frame=heavy_or_frame,
    )


def signature_required(*, expedited: bool, requested: bool) -> bool:
    """Return the operator's independent signature-service choice.

    ``expedited`` remains in the public call signature because route selection and
    declaration pricing still use it, but expedited delivery does not by itself
    require the optional signature service.
    """

    del expedited
    return bool(requested)
