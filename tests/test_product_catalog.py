import pytest

from erp_automation.domain.product_catalog import TENT_FRAME_SKUS
from lingxing_automation.products.catalog import (
    identify_product,
    identify_product_type_from_sku,
    identify_product_types,
    identify_product_types_from_skus,
    is_supported_product_type,
    match_supported_product,
    preferred_product_type,
)


def test_identity_does_not_require_child_only_automation_facts() -> None:
    identity = identify_product("B0H36GPHVH")

    assert identity is not None
    assert identity.product_type == "pop_up_displays"
    assert identity.parent_asin == "B0H36GPHVH"
    assert match_supported_product("B0H36GPHVH") is None


def test_supported_child_has_both_identity_and_automation_match() -> None:
    identity = identify_product("B0FX9W3MJL")
    automation_match = match_supported_product("B0FX9W3MJL")

    assert identity is not None
    assert identity.product_type == "pop_up_displays"
    assert automation_match is not None
    assert automation_match.product_type == identity.product_type


def test_order_identity_returns_every_distinct_product_type_in_source_order() -> None:
    assert identify_product_types(
        ["B0CRRGTPFH", "B0FX9W3MJL", "B0D5134SJ3"]
    ) == ("tent", "pop_up_displays")


def test_unknown_asin_has_no_catalogue_identity() -> None:
    assert identify_product("B0ZZZZZZZZ") is None


def test_display_only_asin_identity_does_not_enable_automation() -> None:
    identity = identify_product("B0CWGLSQ6N")

    assert identity is not None
    assert identity.product_type == "brochures"
    assert is_supported_product_type(identity.product_type) is False


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        ("10X15-FRAME-40MM-SQUARE", "tent"),
        ("10x10-Canopy-Topper", "tent"),
        ("10ft-Full-Wall-Double-Sided", "tent"),
        ("Stakes-Ropes-Kit", "tent"),
        ("Custom-Canopy-Tent-Package-#05", "tent"),
        ("Custom-Canopy-Tent-10x20", "tent"),
        ("Custom-Canopy-Top", "tent"),
        ("Custom-Full-Wall-for-Canopy-Tent", "tent"),
        ("Roller-Bag", "tent"),
        ("Car-Magnet-12x18in-2pcs", "car_magnet"),
        ("Tablecloth-Spandex-8ft", "tablecloths"),
        ("Custom-Fitted-Table-Covers", "tablecloths"),
        ("Custom-Stretch-Table-Covers", "tablecloths"),
        ("Custom-Table-Runner-48x72in", "table_runners"),
        ("Adhesive-Vinyl-Posters-16x24in", "posters"),
        ("Table-Top-Retractable-11.5-x-17.5in-1-Sided", "roll_up_banners"),
        ("Retractable-Banner-33x81in-standard", "roll_up_banners"),
        ("x-banner-24x63in", "x_stands"),
        ("Feather-Flag-0.5x2m", "feather_flags"),
        ("Vinyl-Banners-2x4ft", "vinyl_banners"),
        ("Brochures-Bi-Fold-8.5x11in-157g-25pcs", "brochures"),
        ("Car-Decals", "car_decals"),
        ("Tension-Backdrop-with-Frame-7.5x7.5", "tension_backdrops"),
        ("tension-fabric-displays", "tension_backdrops"),
        ("Instruction", ""),
        ("Unknown-SKU", ""),
    ],
)
def test_exact_sku_identity_catalog(sku: str, expected: str) -> None:
    assert identify_product_type_from_sku(sku) == expected


def test_all_generated_tent_frame_skus_have_tent_identity() -> None:
    assert len(TENT_FRAME_SKUS) == 24
    assert all(
        identify_product_type_from_sku(sku) == "tent"
        for sku in TENT_FRAME_SKUS
    )


def test_sku_identity_uses_distinct_source_order() -> None:
    assert identify_product_types_from_skus(
        "Instruction | Tablecloth-Spandex-6ft | Car-Magnet-10x20in-2pcs"
    ) == ("tablecloths", "car_magnet")
    assert identify_product_types_from_skus(
        "Instruction 共1 10X10-FRAME-40MM-SQUARE 共1 更多"
    ) == ("tent",)


def test_preferred_product_type_uses_tent_then_first_observed_family() -> None:
    assert preferred_product_type(("tablecloths", "tent", "feather_flags")) == "tent"
    assert preferred_product_type(("tablecloths", "feather_flags")) == "tablecloths"
    assert preferred_product_type(("tent | tablecloths",)) == "tent"
    assert preferred_product_type(()) == ""
