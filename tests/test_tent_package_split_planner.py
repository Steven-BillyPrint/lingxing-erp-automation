from lingxing_automation.services.tent_package_split_planner import (
    build_tent_package_split_plan,
    is_frame_sku,
    is_package_accessory_sku,
)
from lingxing_automation.services.tent_sku_planner import (
    DestinationRegion,
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
)


def _sku_plan(category: str, *, replace_main_sku: str | None = None, add_items: list[tuple[str, int]] | None = None):
    """构造拆包 planner 测试所需的帐篷 SKU 计划。"""

    return TentSkuAdjustmentPlan(
        platform_order_no="114-9238856-6341844",
        system_order_no="103717561076404736",
        destination=DestinationRegion(raw_text="test", country="US", state="TX", category=category),
        replace_main_sku=replace_main_sku,
        add_items=[TentSkuPlanAction(action="add", sku=sku, quantity=qty) for sku, qty in (add_items or [])],
    )


def _package_items(plan):
    """把拆包计划转换为便于断言的包裹 SKU 映射。"""

    return {
        package.package_key: {item.sku: item.quantity for item in package.items}
        for package in plan.packages_to_split
    }


def test_us_mainland_splits_accessories_and_frame_leaving_fabric_original():
    """验证美国本土帐篷订单会主动拆出配件包和支架包。"""

    plan = build_tent_package_split_plan(
        _sku_plan(
            "us_mainland",
            replace_main_sku="TENT-ROLLER-BAG-10X10-50MM",
            add_items=[
                ("SANDBAGS-4PCS", 1),
                ("10X10-FRAME-40MM-SQUARE", 1),
                ("10x10-Canopy-Topper", 1),
                ("10ft-Full-Wall", 1),
                ("10ft-Half-Wall", 2),
                ("Tablecloth-Rectangle-6ft", 1),
            ],
        )
    )

    assert plan.required is True
    assert [package.package_key for package in plan.packages_to_split] == ["accessory", "frame"]
    assert _package_items(plan) == {
        "accessory": {"TENT-ROLLER-BAG-10X10-50MM": 1, "SANDBAGS-4PCS": 1},
        "frame": {"10X10-FRAME-40MM-SQUARE": 1},
    }


def test_instruction_uses_same_accessory_package_logic_without_roller_or_sandbag():
    """验证没有拖轮包和沙袋时说明书按配件包逻辑拆出。"""

    plan = build_tent_package_split_plan(
        _sku_plan(
            "us_mainland",
            replace_main_sku="Instruction",
            add_items=[
                ("10X10-FRAME-40MM-SQUARE", 1),
                ("10x10-Canopy-Topper", 1),
                ("10ft-Full-Wall", 1),
            ],
        )
    )

    assert plan.required is True
    assert _package_items(plan)["accessory"] == {"Instruction": 1}
    assert _package_items(plan)["frame"] == {"10X10-FRAME-40MM-SQUARE": 1}


def test_canada_and_us_non_mainland_do_not_require_package_split():
    """验证加拿大和美国非本土订单不需要打开拆包弹窗。"""

    for category in ["canada", "us_non_mainland"]:
        plan = build_tent_package_split_plan(
            _sku_plan(
                category,
                replace_main_sku="TENT-ROLLER-BAG-10X10-50MM",
                add_items=[("10X10-FRAME-40MM-SQUARE", 1), ("10x10-Canopy-Topper", 1)],
            )
        )

        assert plan.required is False
        assert plan.status == "not_required"
        assert plan.packages_to_split == []


def test_frame_only_split_when_accessory_absent_and_fabric_remains():
    """验证没有配件时只主动拆出支架包，布料留在原包裹。"""

    plan = build_tent_package_split_plan(
        _sku_plan(
            "us_mainland",
            add_items=[
                ("10X10-FRAME-40MM-SQUARE", 1),
                ("10x10-Canopy-Topper", 1),
                ("10ft-Half-Wall", 2),
            ],
        )
    )

    assert plan.required is True
    assert [package.package_key for package in plan.packages_to_split] == ["frame"]
    assert _package_items(plan)["frame"] == {"10X10-FRAME-40MM-SQUARE": 1}


def test_accessory_split_leaves_frame_original_when_no_fabric_remains():
    """验证没有布料留底时只拆配件包，避免把原包裹拆空。"""

    plan = build_tent_package_split_plan(
        _sku_plan(
            "us_mainland",
            replace_main_sku="SANDBAGS-4PCS",
            add_items=[("10X10-FRAME-40MM-SQUARE", 1)],
        )
    )

    assert plan.required is True
    assert [package.package_key for package in plan.packages_to_split] == ["accessory"]
    assert _package_items(plan)["accessory"] == {"SANDBAGS-4PCS": 1}


def test_single_fabric_group_does_not_require_split():
    """验证只有布料商品时不会生成拆包动作。"""

    plan = build_tent_package_split_plan(
        _sku_plan("us_mainland", add_items=[("10x10-Canopy-Topper", 1), ("10ft-Half-Wall", 2)])
    )

    assert plan.required is False
    assert plan.status == "not_required"


def test_sku_classifier_matches_accessory_and_frame_variants():
    """验证拆包 SKU 分类器能识别拖轮包、沙袋、说明书和支架。"""

    assert is_package_accessory_sku("Tent-Roller-Bag-10x10-50mm")
    assert is_package_accessory_sku("SANDBAGS-4PCS")
    assert is_package_accessory_sku("Instruction")
    assert is_frame_sku("10X10-FRAME-40MM-SQUARE")
    assert not is_frame_sku("10ft-Half-Wall")
