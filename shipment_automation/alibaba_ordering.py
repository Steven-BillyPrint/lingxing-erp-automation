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

from erp_automation.domain.product_catalog import (
    TENT_TOP_SKUS,
    normalize_product_sku,
)


class AlibabaOrderRuleError(ValueError):
    """The draft cannot be prepared safely without operator correction."""


class UnsupportedProductError(AlibabaOrderRuleError):
    """No registered product category matched the order SKUs."""


class AmbiguousProductError(AlibabaOrderRuleError):
    """More than one registered category matched the same order."""


class ProductCategory(StrEnum):
    TENT = "tent"

    @property
    def label(self) -> str:
        return {ProductCategory.TENT: "帐篷类"}[self]


@dataclass(frozen=True)
class ProductCategoryDefinition:
    key: ProductCategory | str
    label: str
    skus: frozenset[str]

    @property
    def normalized_skus(self) -> frozenset[str]:
        return frozenset(normalize_product_sku(value) for value in self.skus)


@dataclass(frozen=True)
class ProductClassification:
    category: ProductCategory | str
    label: str
    order_skus: tuple[str, ...]
    matched_skus: tuple[str, ...]


class ProductCategoryRegistry:
    """Extensible SKU-to-category registry with explicit ambiguity handling."""

    def __init__(self, definitions: Iterable[ProductCategoryDefinition]) -> None:
        self._definitions = tuple(definitions)
        if not self._definitions:
            raise ValueError("产品分类注册表不能为空。")

    def classify(self, skus: Iterable[object]) -> ProductClassification:
        order_skus = tuple(
            dict.fromkeys(
                text
                for value in skus
                if (text := str(value or "").strip())
            )
        )
        normalized_order = {
            normalize_product_sku(value): value
            for value in order_skus
            if normalize_product_sku(value)
        }
        matches: list[tuple[ProductCategoryDefinition, tuple[str, ...]]] = []
        for definition in self._definitions:
            matched = tuple(
                original
                for normalized, original in normalized_order.items()
                if normalized in definition.normalized_skus
            )
            if matched:
                matches.append((definition, matched))
        if not matches:
            visible = "、".join(order_skus) if order_skus else "无 SKU"
            raise UnsupportedProductError(
                f"订单商品未匹配已支持的物流分类（{visible}），请人工处理。"
            )
        if len(matches) > 1:
            labels = "、".join(definition.label for definition, _ in matches)
            raise AmbiguousProductError(
                f"同一订单匹配到多个物流产品分类（{labels}），请人工确认。"
            )
        definition, matched_skus = matches[0]
        return ProductClassification(
            category=definition.key,
            label=definition.label,
            order_skus=order_skus,
            matched_skus=matched_skus,
        )


DEFAULT_PRODUCT_CATEGORY_REGISTRY = ProductCategoryRegistry(
    (
        ProductCategoryDefinition(
            key=ProductCategory.TENT,
            label=ProductCategory.TENT.label,
            skus=TENT_TOP_SKUS,
        ),
    )
)


_ITEM_CONTAINER_KEYS = frozenset(
    {
        "item_info",
        "iteminfo",
        "order_item",
        "orderitem",
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


def _mapping_text(mapping: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and (text := str(value).strip()):
            return text
    return ""


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
        "telephone",
    ),
    "email": (
        "email",
        "receiver_email",
        "receiverEmail",
        "recipient_email",
        "buyer_email",
        "buyerEmail",
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
    words = primary.split(" ")
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
    second = " ".join(part for part in (overflow, existing_second) if part)
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

    @property
    def address_search_text(self) -> str:
        """Text used only to select Alibaba's city-aware address suggestion."""

        parts = [self.address1, self.address2]
        if self.city and self.city.casefold() not in " ".join(parts).casefold():
            parts.append(self.city)
        return " ".join(part for part in parts if part)


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
    address1, address2 = split_address_lines(
        values["address1"],
        values["address2"],
    )
    postal_code = postal_code_for_alibaba(code, values["postal_code"])
    phone = _digits(values["phone"])
    dial_code = _digits(values["dial_code"])
    if not dial_code and code in {"US", "CA"}:
        dial_code = "1"
    if dial_code and phone.startswith(dial_code) and len(phone) > 10:
        phone = phone[len(dial_code) :]

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
class TentDeclaration:
    name_cn: str = "帐篷布顶"
    name_en: str = "Canopy Tent"
    material: str = "Polyester Fabri"
    purpose: str = "display"
    china_hs_code: str = "3926909090"
    destination_hs_code: str | None = "3926909989"
    quantity: int = 1
    declared_unit_price_usd: Decimal = Decimal("0")
    logistics_attribute: str = "普货"


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


def tent_declaration(
    *,
    destination_country_code: str,
    total_weight_kg: object,
    route: AlibabaRoute,
    expedited: bool,
    heavy_or_frame: bool,
) -> TentDeclaration:
    country = str(destination_country_code or "").strip().upper()
    return TentDeclaration(
        destination_hs_code=None if country == "CA" else "3926909989",
        declared_unit_price_usd=declaration_price_usd(
            destination_country_code=country,
            total_weight_kg=total_weight_kg,
            route=route,
            expedited=expedited,
            heavy_or_frame=heavy_or_frame,
        ),
    )


def signature_required(*, expedited: bool, requested: bool) -> bool:
    """Return the operator's independent signature-service choice.

    ``expedited`` remains in the public call signature because route selection and
    declaration pricing still use it, but expedited delivery does not by itself
    require the optional signature service.
    """

    del expedited
    return bool(requested)
