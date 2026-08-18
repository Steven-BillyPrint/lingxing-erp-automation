from lingxing_automation.products.catalog import (
    identify_product,
    identify_product_types,
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


def test_preferred_product_type_uses_tent_then_first_observed_family() -> None:
    assert preferred_product_type(("tablecloths", "tent", "feather_flags")) == "tent"
    assert preferred_product_type(("tablecloths", "feather_flags")) == "tablecloths"
    assert preferred_product_type(("tent | tablecloths",)) == "tent"
    assert preferred_product_type(()) == ""
