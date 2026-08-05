from __future__ import annotations

from decimal import Decimal

import pytest

from erp_automation.domain.product_catalog import TENT_TOP_SKUS
from lingxing_automation.services.tent_sku_rules import TENT_SIZE_RULES
from shipment_automation.alibaba_ordering import (
    AlibabaOrderRuleError,
    AlibabaRoute,
    DEFAULT_PRODUCT_CATEGORY_REGISTRY,
    ProductCategory,
    declaration_price_usd,
    extract_order_skus,
    extract_shipping_address,
    postal_code_for_alibaba,
    postal_first_five,
    province_name_for_alibaba,
    signature_required,
    shipping_address_payload_with_receive_info_fallback,
    shipping_address_payload_with_web_detail_fallback,
    split_address_lines,
    tent_declaration,
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
    classification = DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify(skus)

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
    classification = DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify(skus)

    assert skus == ("Custom-Tent-Package-10x10", "10x10-Canopy-Topper")
    assert classification.category is ProductCategory.TENT
    assert classification.matched_skus == ("10x10-Canopy-Topper",)


def test_lingxing_camel_case_order_item_container_is_supported() -> None:
    payload = {"orderItem": [{"SKU": "10x15-Canopy-Topper"}]}

    assert extract_order_skus(payload) == ("10x15-Canopy-Topper",)


def test_unknown_order_sku_is_blocked_instead_of_guessed() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="未匹配"):
        DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify(["unknown-product"])


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
    assert address.address_search_text.endswith("Los Angeles")


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

    assert address.address1 == "987 Example Street Apt Unit 100"
    assert address.address2 == ""
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
    classification = DEFAULT_PRODUCT_CATEGORY_REGISTRY.classify(skus)

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


def test_expedited_and_signature_choices_are_independent() -> None:
    assert signature_required(expedited=True, requested=False) is False
    assert signature_required(expedited=True, requested=True) is True
    assert signature_required(expedited=False, requested=True) is True
    assert signature_required(expedited=False, requested=False) is False
