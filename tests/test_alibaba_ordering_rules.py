from __future__ import annotations

from decimal import Decimal

import pytest

from erp_automation.domain.product_catalog import TENT_TOP_SKUS
from lingxing_automation.services.tent_sku_rules import TENT_SIZE_RULES
from shipment_automation.alibaba_ordering import (
    AmbiguousProductError,
    AlibabaOrderRuleError,
    AlibabaRoute,
    ProductCategory,
    declaration_price_usd,
    extract_order_product_rows,
    extract_order_skus,
    extract_shipping_address,
    non_tent_declaration_price_usd,
    postal_code_for_alibaba,
    postal_first_five,
    product_declaration,
    province_name_for_alibaba,
    signature_required,
    shipping_address_payload_with_receive_info_fallback,
    shipping_address_payload_with_web_detail_fallback,
    split_address_lines,
    tent_declaration,
)
from shipment_automation.alibaba_product_classification import (
    classify_order_product,
)


def test_shared_tent_catalog_matches_customization_rule_tops() -> None:
    assert TENT_TOP_SKUS == frozenset(rule["top"] for rule in TENT_SIZE_RULES.values())


def test_lingxing_skus_classify_the_whole_order_as_tent() -> None:
    payload = {
        "data": {
            "item_info": [
                {"local_sku": "some-accessory"},
                {"seller_sku": " 10X10 canopy topper "},
            ]
        }
    }

    skus = extract_order_skus(payload)
    classification = classify_order_product(payload)

    assert skus == ("some-accessory", "10X10 canopy topper")
    assert classification.category is ProductCategory.TENT
    assert classification.matched_skus == ("10X10 canopy topper",)


def test_lingxing_openapi_order_item_extracts_local_sku() -> None:
    payload = {
        "order_number": "103000000000000001",
        "order_item": [
            {
                "MSKU": "Custom-Tent-Package-10x10",
                "sku": "10x10-Canopy-Topper",
            }
        ],
    }

    skus = extract_order_skus(payload)
    classification = classify_order_product(payload)

    assert skus == ("Custom-Tent-Package-10x10", "10x10-Canopy-Topper")
    assert classification.category is ProductCategory.TENT
    assert classification.matched_skus == ("10x10-Canopy-Topper",)


def test_lingxing_camel_case_order_item_container_is_supported() -> None:
    payload = {"orderItem": [{"SKU": "10x15-Canopy-Topper"}]}

    assert extract_order_skus(payload) == ("10x15-Canopy-Topper",)


def test_unknown_order_sku_is_blocked_instead_of_guessed() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="未匹配"):
        classify_order_product(
            {"order_item": [{"sku": "unknown-product"}]}
        )


def test_tent_uses_same_catalog_adapter_but_requires_a_main_product() -> None:
    assert (
        classify_order_product(
            {"order_item": [{"asin": "B0DZ2W2QWK"}]}
        ).category
        is ProductCategory.TENT
    )

    with pytest.raises(AlibabaOrderRuleError, match="未匹配"):
        classify_order_product(
            {
                "order_item": [
                    {"sku": "10ft-Full-Wall"},
                    {"asin": "B0D6KZ7G88"},
                ]
            }
        )


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        ("vinyl-banner-3x6", ProductCategory.VINYL_BANNER),
        ("feather-flag-10ft", ProductCategory.VINYL_BANNER),
        ("poster-24x36", ProductCategory.WALL_DECAL),
        ("car-magnet-12x18", ProductCategory.WALL_DECAL),
        ("tablecloth-rectangle-4ft", ProductCategory.CUSTOM_TABLECLOTH),
        ("table-runner-12x72", ProductCategory.CUSTOM_TABLECLOTH),
        ("pop-up-display-8x8", ProductCategory.CUSTOM_TABLECLOTH),
        ("x-banner-24x63", ProductCategory.X_BANNER_STAND),
        ("retractable-banner-33x81", ProductCategory.BANNER_STAND),
    ],
)
def test_supported_non_tent_sku_uses_declaration_template_category(
    sku: str,
    expected: ProductCategory,
) -> None:
    classification = classify_order_product({"order_item": [{"sku": sku}]})

    assert classification.category is expected
    assert classification.matched_skus == (sku,)


def test_mixed_order_uses_first_supported_product_row() -> None:
    feather_first = classify_order_product(
        {
            "order_item": [
                {"sku": "feather-flag-10ft"},
                {"sku": "car-magnet-12x18"},
            ]
        }
    )
    magnet_first = classify_order_product(
        {
            "order_item": [
                {"sku": "car-magnet-12x18"},
                {"sku": "feather-flag-10ft"},
            ]
        }
    )

    assert feather_first.category is ProductCategory.VINYL_BANNER
    assert magnet_first.category is ProductCategory.WALL_DECAL


def test_unimplemented_rows_are_skipped_before_supported_product() -> None:
    classification = classify_order_product(
        {
            "order_item": [
                {"sku": "brochure-folded"},
                {"sku": "car-decal-large"},
                {"sku": "x-banner-24x63"},
            ]
        }
    )

    assert classification.category is ProductCategory.X_BANNER_STAND


def test_order_with_only_unimplemented_non_tent_products_is_blocked() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="未匹配"):
        classify_order_product(
            {
                "order_item": [
                    {"sku": "brochure-folded"},
                    {"sku": "car-decal-large"},
                    {"sku": "tension-backdrop-8x8"},
                ]
            }
        )


def test_conflicting_identifiers_inside_one_product_row_are_blocked() -> None:
    with pytest.raises(AmbiguousProductError, match="同一商品行"):
        classify_order_product(
            {
                "order_item": [
                    {
                        "MSKU": "feather-flag-10ft",
                        "sku": "car-magnet-12x18",
                    }
                ]
            }
        )


def test_product_rows_preserve_sku_and_asin_order() -> None:
    assert extract_order_product_rows(
        {
            "order_item": [
                {"sku": "unknown", "asin": "B0DPX3YWVT"},
                {"local_sku": "car-magnet-12x18"},
            ]
        }
    ) == (
        ("unknown", "B0DPX3YWVT"),
        ("car-magnet-12x18",),
    )
    assert (
        classify_order_product(
            {"order_item": [{"asin": "B0DPX3YWVT"}]}
        ).category
        is ProductCategory.VINYL_BANNER
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("90012-1234", "90012"),
        (" 01020 ", "01020"),
    ],
)
def test_postal_code_uses_first_five_characters(raw: str, expected: str) -> None:
    assert postal_first_five(raw) == expected


def test_canadian_postal_code_keeps_all_six_characters() -> None:
    assert postal_code_for_alibaba("CA", "V5W 3C2") == "V5W 3C2"


def test_invalid_country_specific_postal_codes_are_blocked() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="加拿大订单邮编"):
        postal_code_for_alibaba("CA", "V5W 3C")


def test_address_split_preserves_complete_words_and_content() -> None:
    source = "12345 Very Long Industrial Boulevard Building Seven"

    first, second = split_address_lines(source)

    assert len(first) <= 35
    assert first == "12345 Very Long Industrial"
    assert second == "Boulevard Building Seven"
    assert f"{first} {second}" == source


def test_address_split_blocks_one_oversized_unbreakable_token() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="不能拆开的词组"):
        split_address_lines("X" * 36)


def test_existing_address_two_is_never_moved_into_address_one() -> None:
    assert split_address_lines("1 Main Street", "Suite 200") == (
        "1 Main Street",
        "Suite 200",
    )


@pytest.mark.parametrize(
    ("source", "street", "secondary"),
    [
        (
            "9876 NW 12TH LN APT SP-00012345",
            "9876 NW 12TH LN",
            "APT SP-00012345",
        ),
        ("42 Example Street Apartment 7", "42 Example Street", "Apartment 7"),
        ("8 Sample Avenue #12", "8 Sample Avenue", "#12"),
    ],
)
def test_inline_unit_is_preserved_in_address_two_for_autocomplete(
    source: str,
    street: str,
    secondary: str,
) -> None:
    assert split_address_lines(source) == (street, secondary)


def test_complete_lingxing_address_is_extracted_with_company_fallback() -> None:
    payload = {
        "receive_info": {
            "receiver_name": "Jane Smith",
            "company_name": "",
            "country_code": "US",
            "country": "United States",
            "state": "California",
            "city": "Los Angeles",
            "address_line1": "12345 Very Long Industrial Boulevard",
            "address_line2": "Building Seven",
            "postal_code": "90012-1234",
            "phone_code": "+1",
            "receiver_phone": "+1 (213) 555-0188",
            "receiver_email": "jane@example.com",
        }
    }

    address = extract_shipping_address(payload)

    assert address.company == "Jane Smith"
    assert address.recipient == "Jane Smith"
    assert address.country_code == "US"
    assert address.address1 == "12345 Very Long Industrial"
    assert address.address2 == "Boulevard Building Seven"
    assert address.postal_code == "90012"
    assert address.dial_code == "1"
    assert address.phone == "2135550188"
    assert address.email == "jane@example.com"


def test_canadian_address_keeps_complete_postal_code() -> None:
    address = extract_shipping_address(
        {
            "receive_info": {
                "receiver_name": "Jane Smith",
                "country_code": "CA",
                "country": "Canada",
                "state": "BC",
                "city": "Vancouver",
                "address_line1": "123 Main Street",
                "postal_code": "V5W 3C2",
                "phone_code": "+1",
                "receiver_phone": "+1 604 555 0188",
                "receiver_email": "jane@example.com",
            }
        }
    )

    assert address.postal_code == "V5W 3C2"
    assert address.province == "British Columbia"


def test_state_and_province_abbreviations_expand_to_alibaba_labels() -> None:
    assert province_name_for_alibaba("US", "CA") == "California"
    assert province_name_for_alibaba("CA", "BC") == "British Columbia"
    assert province_name_for_alibaba("CA", "Ontario") == "Ontario"


def test_missing_address_field_is_blocked() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="缺少.*邮箱"):
        extract_shipping_address(
            {
                "receive_info": {
                    "receiver_name": "Jane",
                    "country_code": "US",
                    "state": "CA",
                    "city": "Los Angeles",
                    "address": "1 Main Street",
                    "postal_code": "90012",
                    "phone": "2135550188",
                }
            }
        )


def test_address_can_take_one_missing_contact_field_from_order_root() -> None:
    payload = {
        "receiver_email": "jane@example.com",
        "receive_info": {
            "receiver_name": "Jane",
            "country_code": "US",
            "state": "CA",
            "city": "Los Angeles",
            "address": "1 Main Street",
            "postal_code": "90012",
            "phone": "2135550188",
        },
    }

    assert extract_shipping_address(payload).email == "jane@example.com"


def test_verified_web_receive_info_fills_missing_openapi_street() -> None:
    openapi_detail = {
        "receive_info": {
            "receiver_name": "Example Cooperative",
            "receiver_country_code": "US",
            "receiver_country_name": "United States of America (USA)",
            "state_or_region": "FL",
            "city": "MIAMI",
            "postal_code": "33182-1909",
            "receiver_mobile": "3055550199",
            "address_line1": "",
        },
    }
    web_order_detail = {
        "global_order_no": "103000000000000001",
        "buyer_info": {"buyer_email": "receiver@example.com"},
        "receive_info": {
            "receiver_name": "Example Cooperative",
            "receiver_country_code": "US",
            "receiver_country_name": "United States of America (USA)",
            "state_or_region": "FL",
            "city": "MIAMI",
            "postal_code": "33182-1909",
            "receiver_mobile": "3055550199",
            "address_line1": "987 Example Street Apt Unit 100",
        },
    }

    payload = shipping_address_payload_with_web_detail_fallback(
        openapi_detail,
        web_order_detail,
    )
    address = extract_shipping_address(payload)

    assert address.address1 == "987 Example Street"
    assert address.address2 == "Apt Unit 100"
    assert address.postal_code == "33182"
    assert address.email == "receiver@example.com"


def test_lingxing_web_order_item_info_container_is_supported() -> None:
    payload = {
        "order_item_info": [
            {"local_sku": "10x10-Canopy-Topper", "quantity": 1},
            {"local_sku": "10ft-Full-Wall", "quantity": 1},
            {"local_sku": "10ft-Half-Wall", "quantity": 2},
            {"local_sku": "Tablecloth-Rectangle-4ft", "quantity": 1},
        ]
    }

    skus = extract_order_skus(payload)
    classification = classify_order_product(payload)

    assert skus == (
        "10x10-Canopy-Topper",
        "10ft-Full-Wall",
        "10ft-Half-Wall",
        "Tablecloth-Rectangle-4ft",
    )
    assert classification.category is ProductCategory.TENT
    assert classification.matched_skus == ("10x10-Canopy-Topper",)


def test_third_address_line_and_doorplate_are_preserved_without_duplication() -> None:
    address = extract_shipping_address(
        {
            "receive_info": {
                "receiver_name": "Jane Smith",
                "country_code": "US",
                "state": "CA",
                "city": "Los Angeles",
                "address_line1": "1 Main Street",
                "address_line2": "Building A",
                "address_line3": "Floor 2",
                "doorplate_no": "Suite 3",
                "postal_code": "90012",
                "phone": "2135550188",
                "receiver_email": "jane@example.com",
            }
        }
    )

    assert address.address1 == "1 Main Street"
    assert address.address2 == "Building A Floor 2 Suite 3"


@pytest.mark.parametrize(
    ("country", "weight", "route_name", "expedited", "heavy", "expected"),
    [
        ("US", 6, "全球普货专线", False, False, Decimal("2.50")),
        ("US", 20, "普通快递", False, True, Decimal("4.00")),
        ("US", 20, "Express Expedited", True, True, Decimal("8.00")),
        ("CA", "14.999", "任意线路", True, False, Decimal("13.00")),
        ("CA", "15", "任意线路", False, False, Decimal("99.00")),
    ],
)
def test_declaration_price_rules(
    country: str,
    weight: object,
    route_name: str,
    expedited: bool,
    heavy: bool,
    expected: Decimal,
) -> None:
    assert (
        declaration_price_usd(
            destination_country_code=country,
            total_weight_kg=weight,
            route=AlibabaRoute(route_name),
            expedited=expedited,
            heavy_or_frame=heavy,
        )
        == expected
    )


def test_us_ddp_price_is_fixed_at_eight_hundred() -> None:
    assert declaration_price_usd(
        destination_country_code="US",
        total_weight_kg=6,
        route=AlibabaRoute("全球普货专线", customs_mode="DDP"),
        expedited=False,
        heavy_or_frame=False,
    ) == Decimal("800.00")


def test_us_ddp_rule_takes_priority_over_heavy_frame_formula() -> None:
    assert declaration_price_usd(
        destination_country_code="US",
        total_weight_kg=20,
        route=AlibabaRoute("全球普货专线DDP标准"),
        expedited=True,
        heavy_or_frame=True,
    ) == Decimal("800.00")


def test_canada_ddp_rule_takes_priority_over_weight_threshold() -> None:
    assert declaration_price_usd(
        destination_country_code="CA",
        total_weight_kg=10,
        route=AlibabaRoute("全球普货专线DDP标准"),
        expedited=False,
        heavy_or_frame=False,
    ) == Decimal("800.00")


def test_canada_declaration_omits_destination_hs_code() -> None:
    declaration = tent_declaration(
        destination_country_code="CA",
        total_weight_kg=10,
        route=AlibabaRoute("任意线路"),
        expedited=False,
        heavy_or_frame=False,
    )

    assert declaration.name_cn == "帐篷布顶"
    assert declaration.name_en == "Canopy Tent"
    assert declaration.material == "Polyester Fabri"
    assert declaration.purpose == "display"
    assert declaration.china_hs_code == "3926909090"
    assert declaration.destination_hs_code is None
    assert declaration.quantity == 1
    assert declaration.logistics_attribute == "普货"
    assert declaration.declared_unit_price_usd == Decimal("13.00")


@pytest.mark.parametrize(
    ("weight", "expected"),
    [
        ("0.001", Decimal("1.01")),
        ("3", Decimal("1.01")),
        ("3.001", Decimal("3.01")),
        ("10", Decimal("3.01")),
        ("10.001", Decimal("5.01")),
        ("14.999", Decimal("5.01")),
        ("15", Decimal("10.01")),
        ("99", Decimal("10.01")),
    ],
)
def test_non_tent_declaration_price_uses_total_weight_tiers(
    weight: str,
    expected: Decimal,
) -> None:
    assert non_tent_declaration_price_usd(total_weight_kg=weight) == expected


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (
            ProductCategory.VINYL_BANNER,
            (
                "喷绘",
                "Vinyl Banners",
                "Polyester",
                "display",
                "6302539010",
                "6302592000",
            ),
        ),
        (
            ProductCategory.WALL_DECAL,
            (
                "车贴",
                "wall decal",
                "polyester",
                "display",
                "9505900000",
                "9505101000",
            ),
        ),
        (
            ProductCategory.CUSTOM_TABLECLOTH,
            (
                "定制桌布",
                "Custom Tablecloth",
                "Polyester",
                "display",
                "6302539010",
                "6302592000",
            ),
        ),
        (
            ProductCategory.X_BANNER_STAND,
            (
                "X展架",
                "X Banner Stand",
                "Polyester",
                "Display",
                "6302539010",
                "6302592000",
            ),
        ),
        (
            ProductCategory.BANNER_STAND,
            (
                "易拉宝",
                "Banner Stand",
                "Polyester",
                "display",
                "6302539010",
                "6302592000",
            ),
        ),
    ],
)
def test_non_tent_declaration_templates_match_approved_fields(
    category: ProductCategory,
    expected: tuple[str, str, str, str, str, str],
) -> None:
    declaration = product_declaration(
        category=category,
        destination_country_code="US",
        total_weight_kg="12",
        route=AlibabaRoute("Express Expedited", customs_mode="DDP"),
        expedited=True,
        heavy_or_frame=True,
    )

    assert (
        declaration.name_cn,
        declaration.name_en,
        declaration.material,
        declaration.purpose,
        declaration.china_hs_code,
        declaration.destination_hs_code,
    ) == expected
    assert declaration.quantity == 1
    assert declaration.logistics_attribute == "普货"
    assert declaration.declared_unit_price_usd == Decimal("5.01")


def test_canada_non_tent_declaration_omits_destination_hs_code() -> None:
    declaration = product_declaration(
        category=ProductCategory.VINYL_BANNER,
        destination_country_code="CA",
        total_weight_kg="15",
        route=AlibabaRoute("任意线路"),
        expedited=False,
        heavy_or_frame=False,
    )

    assert declaration.destination_hs_code is None
    assert declaration.declared_unit_price_usd == Decimal("10.01")


def test_expedited_and_signature_choices_are_independent() -> None:
    assert signature_required(expedited=True, requested=False) is False
    assert signature_required(expedited=True, requested=True) is True
    assert signature_required(expedited=False, requested=True) is True
    assert signature_required(expedited=False, requested=False) is False
