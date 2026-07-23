from __future__ import annotations

import pytest

from lingxing_automation.services.tent_sku_planner import (
    DestinationRegion,
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
)
from lingxing_automation.services.tent_warehouse_routing import (
    TentRoutingItem,
    TentRoutingPackage,
    TentWarehouseRuleError,
    build_tent_warehouse_routing_plan,
    classify_tent_routing_sku,
    load_tent_warehouse_rules,
    lookup_zip_routing_rule,
    tent_sku_plan_from_routing_input,
    tent_sku_plan_to_routing_input,
)


def _sku_plan(postal_code: str = "11725", *, category: str = "us_mainland") -> TentSkuAdjustmentPlan:
    return TentSkuAdjustmentPlan(
        platform_order_no="112-1234567-1234567",
        system_order_no="10001",
        destination=DestinationRegion(
            raw_text="test",
            country="US",
            state="NY",
            postal_code=postal_code,
            category=category,
        ),
        replace_main_items=[
            TentSkuPlanAction(
                action="replace",
                sku="10X10-FRAME-40MM-SQUARE",
                quantity=1,
                source_order_item_id="main-row",
            )
        ],
    )


def _package(no: str, *items: tuple[str, str | None, int]) -> TentRoutingPackage:
    return TentRoutingPackage(
        system_order_no=no,
        items=tuple(
            TentRoutingItem(sku=sku, order_item_no=item_id, quantity=quantity)
            for sku, item_id, quantity in items
        ),
    )


def test_rule_file_has_complete_non_overlapping_zip_coverage():
    rules = load_tent_warehouse_rules()

    assert len(rules.rules) == 191
    assert rules.rules[0].start_zip == "00000"
    assert rules.rules[-1].end_zip == "99999"
    assert len(rules.source_sha256) == 64


@pytest.mark.parametrize(
    ("postal_code", "expected"),
    [
        ("11725", "NJ"),
        ("68000", "CA"),
        ("70000", "NJ"),
        ("71000", "CA"),
        ("77600", "NJ"),
        ("90000", "CA"),
        ("00601", "KEEP"),
        ("09012", "KEEP"),
        ("96701", "KEEP"),
        ("99501", "KEEP"),
    ],
)
def test_representative_zip_routes(postal_code, expected):
    assert lookup_zip_routing_rule(postal_code).action == expected


def test_equal_zone_uses_ca():
    rule = lookup_zip_routing_rule("68000")

    assert rule.ca_zone == rule.nj_zone
    assert rule.action == "CA"


@pytest.mark.parametrize(
    ("sku", "expected"),
    [
        ("10X10-FRAME-40MM-SQUARE", "frame"),
        ("TENT-ROLLER-BAG-10X10-50MM", "roller_bag"),
        ("SANDBAGS-4PCS", "sandbag"),
        ("Instruction", "instruction"),
        ("10x10-Canopy-Topper", "fabric"),
        ("10ft-Half-Wall-Double-Sided", "fabric"),
        ("Tablecloth-Rectangle-6ft", "fabric"),
        ("mystery-part", "unknown"),
    ],
)
def test_sku_classification(sku, expected):
    assert classify_tent_routing_sku(sku) == expected


def test_main_frame_uses_warehouse_owned_fedex_channel():
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan("11725"),
        packages=[_package("child-main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1))],
    )

    decision = plan.decisions[0]
    assert plan.status == "ready"
    assert decision.target_warehouse_code == "NJ"
    assert decision.is_main_product_package is True
    assert decision.target_channel_name == "港通 新泽西仓-FedEx Ground Economy"


def test_non_main_frame_roller_and_sandbag_use_unrestricted_comparison():
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan("90000"),
        packages=[
            _package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1)),
            _package(
                "accessories",
                ("TENT-ROLLER-BAG-10X10-50MM", "roller", 1),
                ("SANDBAGS-4PCS", "sandbag", 1),
            ),
        ],
    )

    decision = plan.decisions[1]
    assert decision.target_warehouse_code == "CA"
    assert decision.is_main_product_package is False
    assert decision.target_channel_name == "港通 洛杉矶仓-不限渠道比价"


def test_nj_non_main_package_uses_exact_erp_unrestricted_channel_name():
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan("44102"),
        packages=[
            _package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1)),
            _package(
                "accessories",
                ("TENT-ROLLER-BAG-10X10-50MM", "roller", 1),
            ),
        ],
    )

    decision = plan.decisions[1]
    assert decision.target_warehouse_code == "NJ"
    assert decision.is_main_product_package is False
    assert decision.target_channel_name == "港通 新泽西仓-不限比价渠道"


@pytest.mark.parametrize(
    ("sku", "item_id"),
    [
        ("10x10-Canopy-Topper", "fabric"),
        ("Instruction", "instruction"),
    ],
)
def test_pure_fabric_and_instruction_packages_are_unchanged(sku, item_id):
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan(),
        packages=[
            _package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1)),
            _package(item_id, (sku, item_id, 1)),
        ],
    )

    decision = plan.decisions[1]
    assert decision.status == "skip"
    assert decision.target_warehouse_code is None
    assert decision.target_channel_name is None


@pytest.mark.parametrize(
    "items",
    [
        (("10x10-Canopy-Topper", "fabric", 1), ("SANDBAGS-4PCS", "sandbag", 1)),
        (("Instruction", "instruction", 1), ("10X10-FRAME-40MM-SQUARE", "frame", 1)),
        (("mystery-part", "unknown", 1),),
    ],
)
def test_mixed_or_unknown_package_forces_whole_plan_to_manual_review(items):
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan(),
        packages=[
            _package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1)),
            _package("bad", *items),
        ],
    )

    assert plan.status == "manual_review"
    assert plan.manual_required is True


def test_duplicate_main_sku_without_preserved_lineage_is_manual_review():
    sku_plan = _sku_plan()
    sku_plan.replace_main_items[0].source_order_item_id = "missing-after-split"
    plan = build_tent_warehouse_routing_plan(
        sku_plan=sku_plan,
        packages=[
            _package("one", ("10X10-FRAME-40MM-SQUARE", "new-1", 1)),
            _package("two", ("10X10-FRAME-40MM-SQUARE", "new-2", 1)),
        ],
    )

    assert plan.status == "manual_review"
    assert "重复或缺失" in plan.reason


@pytest.mark.parametrize(
    "postal_code",
    ["00601", "96701", "99501"],
)
def test_non_mainland_zip_does_not_modify_routes(postal_code):
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan(postal_code, category="us_non_mainland"),
        packages=[_package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1))],
    )

    assert plan.status == "not_required"
    assert all(item.status == "skip" for item in plan.decisions)


def test_invalid_zip_requires_manual_review_and_never_completes_as_noop():
    plan = build_tent_warehouse_routing_plan(
        sku_plan=_sku_plan("bad", category="us_mainland"),
        packages=[_package("main", ("10X10-FRAME-40MM-SQUARE", "main-row", 1))],
    )

    assert plan.status == "manual_review"
    assert plan.required is True
    assert plan.manual_required is True
    assert all(item.status == "manual_review" for item in plan.decisions)
    assert "有效五位邮编" in plan.reason


def test_loader_rejects_a_gap(tmp_path):
    source = load_tent_warehouse_rules()
    payload = {
        "schema_version": 1,
        "source_workbook": source.source_workbook,
        "source_sha256": source.source_sha256,
        "tie_breaker": "CA",
        "warehouses": source.warehouses,
        "rules": [
            ["00000", "00009", None, None, "KEEP", "test"],
            ["00011", "99999", 2, 8, "CA", "test"],
        ],
    }
    path = tmp_path / "bad.json"
    path.write_text(__import__("json").dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TentWarehouseRuleError, match="缺口或重叠"):
        load_tent_warehouse_rules(path)


def test_routing_input_round_trip_preserves_destination_and_main_lineage():
    original = _sku_plan("11725")

    restored = tent_sku_plan_from_routing_input(
        tent_sku_plan_to_routing_input(original)
    )

    assert restored.platform_order_no == original.platform_order_no
    assert restored.destination.postal_code == "11725"
    assert restored.destination.category == "us_mainland"
    assert restored.replace_main_items[0].source_order_item_id == "main-row"
    assert restored.replace_main_items[0].sku == "10X10-FRAME-40MM-SQUARE"
