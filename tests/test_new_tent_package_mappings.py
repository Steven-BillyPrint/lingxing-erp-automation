from __future__ import annotations

import pytest

from lingxing_automation.products.tents import (
    NEW_TENT_PARENT_ASIN,
    TENT_PACKAGE_RULES_BY_ASIN,
    TENT_WALL_STRATEGY_DIRECTIONAL,
    TENT_WALL_STRATEGY_LEGACY,
    TENT_WALL_STRATEGY_NONE,
    find_tent_parent_asin,
    get_tent_package_rule,
    get_tent_top_size,
)
from lingxing_automation.services.customization_parser import parse_customization_pairs
from lingxing_automation.services.folder_builder import (
    FolderRuleMissingError,
    build_order_folder_components_from_pairs,
)
from lingxing_automation.services.order_folder_rules import (
    TITLE_DOUBLE_SIDE,
    TITLE_FABRIC,
    TITLE_FLAG,
    TITLE_FRAME_RECOMMENDED,
)
from lingxing_automation.services.tent_sku_planner import build_tent_sku_plan
from lingxing_automation.services.tent_sku_rules import (
    tent_accessory_component_to_sku_items,
    wall_sku_for_component,
)


NEW_CHILD_ASINS = (
    "B0H5TV9LXK",
    "B0H6PW43V1",
    "B0H6PN5HTB",
    "B0H6PSSCVM",
    "B0H6PQMPSW",
    "B0H6PNN62J",
    "B0H6PNRVV6",
    "B0H6PML9SS",
    "B0H6PLYKY4",
    "B0H6PS9BT5",
    "B0H6PXWBCH",
    "B0H6PNDK4K",
    "B0H6PTLMZ1",
)


def _base_pairs(**extra: str) -> dict[str, str]:
    return {
        TITLE_FRAME_RECOMMENDED: 'Standard 1.6"/40mm square aluminum',
        TITLE_FABRIC: "400D Polyester Fabric",
        **extra,
    }


def _components(asin: str, pairs: dict[str, str]) -> list[str]:
    return build_order_folder_components_from_pairs(
        platform_order_no="111-1111111-1111111",
        parent_asin=NEW_TENT_PARENT_ASIN,
        asin=asin,
        tent_quantity=1,
        pairs=pairs,
        recipient_name="Test Buyer",
    )


def _components_with_quantity(
    asin: str,
    pairs: dict[str, str],
    *,
    quantity: int,
) -> list[str]:
    return build_order_folder_components_from_pairs(
        platform_order_no="111-1111111-1111111",
        parent_asin=NEW_TENT_PARENT_ASIN,
        asin=asin,
        tent_quantity=quantity,
        pairs=pairs,
        recipient_name="Test Buyer",
    )


def _actions(plan) -> dict[str, int]:
    return {item.sku: item.quantity for item in plan.add_items}


def test_new_parent_registers_all_thirteen_children_and_sizes() -> None:
    assert tuple(TENT_PACKAGE_RULES_BY_ASIN) == NEW_CHILD_ASINS
    assert find_tent_parent_asin(NEW_TENT_PARENT_ASIN) == NEW_TENT_PARENT_ASIN
    for asin in NEW_CHILD_ASINS:
        assert find_tent_parent_asin(asin) == NEW_TENT_PARENT_ASIN
        assert get_tent_top_size(asin) == "3x3m帐篷顶"


def test_new_package_registry_keeps_wall_strategies_scoped_by_child_asin() -> None:
    assert {
        asin: get_tent_package_rule(asin).wall_strategy  # type: ignore[union-attr]
        for asin in NEW_CHILD_ASINS
    } == {
        "B0H5TV9LXK": TENT_WALL_STRATEGY_LEGACY,
        "B0H6PW43V1": TENT_WALL_STRATEGY_DIRECTIONAL,
        "B0H6PN5HTB": TENT_WALL_STRATEGY_LEGACY,
        "B0H6PSSCVM": TENT_WALL_STRATEGY_DIRECTIONAL,
        "B0H6PQMPSW": TENT_WALL_STRATEGY_DIRECTIONAL,
        "B0H6PNN62J": TENT_WALL_STRATEGY_LEGACY,
        "B0H6PNRVV6": TENT_WALL_STRATEGY_NONE,
        "B0H6PML9SS": TENT_WALL_STRATEGY_DIRECTIONAL,
        "B0H6PLYKY4": TENT_WALL_STRATEGY_LEGACY,
        "B0H6PS9BT5": TENT_WALL_STRATEGY_DIRECTIONAL,
        "B0H6PXWBCH": TENT_WALL_STRATEGY_LEGACY,
        "B0H6PNDK4K": TENT_WALL_STRATEGY_NONE,
        "B0H6PTLMZ1": TENT_WALL_STRATEGY_DIRECTIONAL,
    }
    assert get_tent_package_rule("B0DZ2W2QWK") is None


@pytest.mark.parametrize(
    ("asin", "flag_option"),
    [
        ("B0H5TV9LXK", None),
        ("B0H6PN5HTB", "2-Sided Printing: 6.9ft Same Design both Sides"),
        ("B0H6PNN62J", "2-Sided Printing: 1.64x6.56ft"),
        ("B0H6PLYKY4", None),
    ],
)
def test_non_directional_new_packages_reuse_legacy_wall_format(
    asin: str,
    flag_option: str | None,
) -> None:
    extra = {TITLE_DOUBLE_SIDE: "2-sided Printing: 2 Half Walls"}
    if flag_option:
        extra[TITLE_FLAG] = flag_option
    components = _components(
        asin,
        _base_pairs(**extra),
    )

    assert "1全高背墙" in components
    assert "2双面半高侧墙(带横杆)" in components
    assert not any(component.startswith(("左", "右", "背")) for component in components)


def test_package_k_reuses_legacy_single_half_wall_format() -> None:
    components = _components(
        "B0H6PXWBCH",
        _base_pairs(**{TITLE_DOUBLE_SIDE: "2-sided Printing: 1 Half Wall"}),
    )

    assert "1双面半高侧墙(带横杆)" in components


@pytest.mark.parametrize("asin", ["B0H6PW43V1", "B0H6PML9SS"])
def test_back_wall_packages_emit_directional_component(asin: str) -> None:
    components = _components(
        asin,
        _base_pairs(**{"Back Wall Options": "2-sided Printing: 1 Full Wall"}),
    )

    assert "背双面全墙" in components


def test_package_d_emits_each_full_wall_in_stable_direction_order() -> None:
    components = _components(
        "B0H6PSSCVM",
        _base_pairs(
            **{
                "Front Wall Options": "2-sided Printing: Full Front Wall",
                "Left Wall Options": "1-sided Printing: Full Left Wall",
                "Right Wall Options": "2-sided Printing: Full Right Wall",
                "Back Wall Options": "1-sided Printing: Full Back Wall",
            }
        ),
    )

    assert components[3:7] == [
        "前双面全墙",
        "左单面全墙",
        "右双面全墙",
        "背单面全墙",
    ]


@pytest.mark.parametrize("asin", ["B0H6PQMPSW", "B0H6PS9BT5"])
def test_three_full_wall_packages_emit_directional_components(asin: str) -> None:
    components = _components(
        asin,
        _base_pairs(
            **{
                "Left Wall Options": "2-sided Printing: Full Left Wall",
                "Right Wall Options": "1-sided Printing: Full Right Wall",
                "Back Wall Options": "2-sided Printing: Full Back Wall",
            }
        ),
    )

    assert components[3:6] == ["左双面全墙", "右单面全墙", "背双面全墙"]


def test_package_m_emits_directional_half_walls() -> None:
    components = _components(
        "B0H6PTLMZ1",
        _base_pairs(
            **{
                "Double-sided Printing Options - Left Half Wall": "2-sided Printing: 1 Half Wall",
                "Double-sided Printing Options - Right Half Wall": "2-sided Printing: 1 Half Wall",
                "Double-sided Printing Options - Back Half Wall": "1-sided Printing: 1 Half Wall",
            }
        ),
    )

    assert components[3:6] == ["左双面半墙", "右双面半墙", "背单面半墙"]


def test_directional_package_fails_closed_when_required_wall_option_is_missing() -> None:
    with pytest.raises(FolderRuleMissingError, match="Right Wall Options"):
        _components(
            "B0H6PQMPSW",
            _base_pairs(
                **{
                    "Left Wall Options": "1-sided Printing: Full Left Wall",
                    "Back Wall Options": "1-sided Printing: Full Back Wall",
                }
            ),
        )


@pytest.mark.parametrize("asin", ["B0H6PNRVV6", "B0H6PNDK4K"])
def test_no_wall_packages_ignore_fixed_page_wall_selector(asin: str) -> None:
    components = _components(asin, _base_pairs())

    assert not any("墙" in component for component in components)


def test_new_fixed_and_directional_titles_are_text_parser_boundaries() -> None:
    pairs = parse_customization_pairs(
        " ".join(
            [
                "Custom canopy tent 10x10 4 Full Walls : 4 Full Walls",
                "Front Wall Options : 2-sided Printing: Full Front Wall",
                "Left Wall Options : 1-sided Printing: Full Left Wall",
                "Right Wall Options : 2-sided Printing: Full Right Wall",
                "Back Wall Options : 1-sided Printing: Full Back Wall",
                "Fabric Material Options : 400D Polyester Fabric",
            ]
        )
    )

    assert pairs["Front Wall Options"] == "2-sided Printing: Full Front Wall"
    assert pairs["Left Wall Options"] == "1-sided Printing: Full Left Wall"
    assert pairs["Right Wall Options"] == "2-sided Printing: Full Right Wall"
    assert pairs["Back Wall Options"] == "1-sided Printing: Full Back Wall"
    assert pairs[TITLE_FABRIC] == "400D Polyester Fabric"


@pytest.mark.parametrize(
    "title",
    ["Custom Teardrop Flag", "Custom Feather Flag"],
)
def test_new_specific_flag_titles_alias_to_shared_tent_flag_field(title: str) -> None:
    pairs = parse_customization_pairs(
        f"{title} : 2-Sided Printing: 1.64x6.56ft "
        "Fabric Material Options : 400D Polyester Fabric"
    )

    assert pairs[TITLE_FLAG] == "2-Sided Printing: 1.64x6.56ft"
    assert pairs[TITLE_FABRIC] == "400D Polyester Fabric"


def test_package_c_maps_teardrop_flag_title_and_hardware() -> None:
    components = _components(
        "B0H6PN5HTB",
        _base_pairs(
            **{
                TITLE_FLAG: "2-Sided Printing: 9.8ft Different Design, + $130.00",
            }
        ),
    )

    assert "2套（0.95x2.3m双面水滴旗+不同设计+全玻璃纤维杆+连接件+夹具）" in components


@pytest.mark.parametrize(
    ("option", "component", "sku"),
    [
        (
            "2-Sided Printing: 1.64x6.56ft",
            "2套（0.5x2m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
            "Feather-Flag-0.5x2m",
        ),
        (
            "2-Sided Printing: 1.97x7.8ft, + $160.00",
            "2套（0.6x2.5m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
            "Feather-Flag-0.6x2.5m",
        ),
        (
            "2-Sided Printing: 2.3x11.15ft, + $237.84",
            "2套（0.7x3.4m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
            "Feather-Flag-0.7x3.4m",
        ),
        (
            "2-Sided Printing: 2.62x13.45ft, + $361.89",
            "2套（0.8x4.1m双面刀旗+全玻璃纤维杆+扁铁十字底座+水袋）",
            "Feather-Flag-0.8x4.1m",
        ),
    ],
)
def test_package_f_maps_all_flag_sizes_and_water_bag(option: str, component: str, sku: str) -> None:
    components = _components("B0H6PNN62J", _base_pairs(**{TITLE_FLAG: option}))
    items = tent_accessory_component_to_sku_items(component)

    assert component in components
    assert [(item.sku, item.quantity) for item in items] == [(sku, 2)]


@pytest.mark.parametrize(("tent_quantity", "flag_quantity"), [(1, 2), (2, 4)])
def test_package_f_flag_sku_quantity_scales_once_per_tent(
    tent_quantity: int,
    flag_quantity: int,
) -> None:
    components = _components_with_quantity(
        "B0H6PNN62J",
        _base_pairs(**{TITLE_FLAG: "2-Sided Printing: 2.3x11.15ft"}),
        quantity=tent_quantity,
    )
    plan = build_tent_sku_plan(
        platform_order_no="701-1111111-1111111",
        system_order_no="SYSTEM-F",
        folder_components=components,
        destination_text="Canada, ON, Toronto",
        asin="B0H6PNN62J",
    )

    assert plan.manual_required is False
    assert _actions(plan)["Feather-Flag-0.7x3.4m"] == flag_quantity


@pytest.mark.parametrize(
    ("component", "sku"),
    [
        ("左单面全墙", "10ft-Full-Wall"),
        ("背双面全墙", "10ft-Full-Wall-Double-Sided"),
        ("右单面半墙", "10ft-Half-Wall"),
        ("左双面半墙", "10ft-Half-Wall-Double-Sided"),
    ],
)
def test_directional_wall_component_maps_to_one_sku(component: str, sku: str) -> None:
    item = wall_sku_for_component("3x3m", component)

    assert item is not None
    assert (item.sku, item.quantity) == (sku, 1)


def test_directional_half_walls_count_skus_and_require_rail_frame() -> None:
    plan = build_tent_sku_plan(
        platform_order_no="701-1111111-1111111",
        system_order_no="SYSTEM-1",
        folder_components=[
            "701-1111111-1111111",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "左双面半墙",
            "右双面半墙",
            "背单面半墙",
            "Test Buyer",
        ],
        destination_text="Canada, ON, Toronto",
        asin="B0H6PTLMZ1",
    )

    assert plan.manual_required is False
    assert _actions(plan) == {
        "10X10-FRAME-40MM-SQUARE-RAIL": 1,
        "10ft-Half-Wall-Double-Sided": 2,
        "10ft-Half-Wall": 1,
    }


def test_existing_tent_asin_keeps_legacy_wall_output() -> None:
    components = build_order_folder_components_from_pairs(
        platform_order_no="111-2222222-2222222",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        pairs=_base_pairs(
            **{
                "Side Wall and Rail Options": "1 Full and 2 Half Walls with Rails",
                TITLE_DOUBLE_SIDE: "2-sided Printing: 2 Half Walls",
            }
        ),
        recipient_name="Legacy Buyer",
    )

    assert "1全高背墙" in components
    assert "2双面半高侧墙(带横杆)" in components
    assert not any(component in {"左双面半墙", "右双面半墙"} for component in components)
