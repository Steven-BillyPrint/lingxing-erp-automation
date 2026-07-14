from lingxing_automation.models import OrderFolderLine
from lingxing_automation.services.china_workday import (
    ChinaWorkdayCalendarMissingError,
    ShippingDeadlineDateParseError,
    build_expedited_instruction_customer_remark,
    build_instruction_customer_remark,
    is_china_workday,
)
from lingxing_automation.services.tent_sku_planner import (
    build_tent_sku_plan,
    extract_shipping_address_line,
    parse_destination_region,
)
from lingxing_automation.services.tent_sku_rules import tent_accessory_component_to_sku_items, wall_sku_for_component


def _actions(plan):
    """提取计划动作列表，便于测试断言。"""
    return {item.sku: item.quantity for item in plan.add_items}


def _replacements(plan):
    return [(item.sku, item.quantity) for item in plan.replace_main_items]


def _multi_main_order_lines():
    return [
        OrderFolderLine(
            asin="B0F5CKNVYJ",
            sku="Canopy-Tent-10x20",
            parent_asin="B0F5CTQXG1",
            product_type="tent",
            quantity=1,
            customization_text="",
        ),
        OrderFolderLine(
            asin="B0DBGBDHL7",
            sku="Tablecloth-Spandex-6ft",
            parent_asin=None,
            product_type="tablecloths",
            quantity=1,
            customization_text="",
        ),
    ]


def test_parse_us_non_mainland_region_requires_manual_sku():
    """验证帐篷 SKU 计划中的解析美国非美国本土地区要求人工SKU场景。"""
    region = parse_destination_region("United States of America (USA), AK, ANCHORAGE")

    assert region.category == "us_non_mainland"
    assert region.state == "AK"


def test_parse_destination_prefers_shipping_address_line_over_street_abbreviation():
    """验证帐篷 SKU 计划中的解析目的地优先使用 收货地址 行优于 street abbreviation场景。"""
    text = (
        "收货信息 收件人 Sulema Catano-Vicki Roy Home Healh S... 买家姓名 Juliana Santana "
        "电话 9084279104 买家邮箱 juliana@thephoenixhc.com "
        "收件地址 United States of America (USA)(美国), TX, HARLINGEN "
        "详细地址 606 W LELA ST STE B 邮编 78550-4876"
    )

    region = parse_destination_region(text)

    assert extract_shipping_address_line(text) == "United States of America (USA)(美国), TX, HARLINGEN"
    assert region.country == "US"
    assert region.state == "TX"
    assert region.city == "HARLINGEN"
    assert region.category == "us_mainland"


def test_us_mainland_plan_replaces_roller_and_adds_tent_accessories():
    """验证帐篷 SKU 计划中的美国美国本土计划替换拖轮包并添加 帐篷 配件场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-7615573-3879423",
        system_order_no="103714959937870558",
        folder_components=[
            "114-7615573-3879423",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "1全高背墙",
            "2半高侧墙",
            "400D面料",
            "拖轮包",
            "沙袋四件套",
            "绳子地钉",
            "1个6FT方套桌布+260g经编布",
            "Sterling Automotive",
        ],
        destination_text="United States of America (USA), UT, LINDON",
        shipping_deadline_text="6天1小时",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "TENT-ROLLER-BAG-10X10-50MM"
    assert plan.customer_remark is None
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
        "10ft-Full-Wall": 1,
        "10ft-Half-Wall": 2,
        "SANDBAGS-4PCS": 1,
        "Tablecloth-Rectangle-6ft": 1,
    }


def test_canada_plan_replaces_main_with_tent_top_and_does_not_add_top_again():
    """验证帐篷 SKU 计划中的加拿大计划替换主带有 帐篷 顶布并 不会 添加顶布 again场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="701-2327833-0551442",
        system_order_no="103713919106585921",
        folder_components=[
            "701-2327833-0551442",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "1全高背墙",
            "Harmeet Chouhan",
        ],
        destination_text="Canada(加拿大), Ontario, Brampton",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10x10-Canopy-Topper"
    assert plan.customer_remark is None
    assert _actions(plan) == {
        "10X10-FRAME-40MM-SQUARE": 1,
        "10ft-Full-Wall": 1,
    }


def test_canada_keeps_tent_top_replacement_even_with_roller_and_sandbag():
    """验证帐篷 SKU 计划中的加拿大保留 帐篷 顶布替换即使带有拖轮包并沙袋场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="701-2327833-0551442",
        system_order_no="103713919106585921",
        folder_components=[
            "701-2327833-0551442",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "拖轮包",
            "沙袋四件套",
            "Buyer Name",
        ],
        destination_text="Canada(加拿大), Ontario, Brampton",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10x10-Canopy-Topper"
    assert _actions(plan) == {
        "10X10-FRAME-40MM-SQUARE": 1,
        "TENT-ROLLER-BAG-10X10-50MM": 1,
        "SANDBAGS-4PCS": 1,
    }


def test_us_non_mainland_plan_uses_roller_when_present():
    """验证帐篷 SKU 计划中的美国非美国本土计划使用拖轮包当 present场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "拖轮包",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), AK, ANCHORAGE",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "TENT-ROLLER-BAG-10X10-50MM"
    assert plan.customer_remark is None
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
    }


def test_us_non_mainland_plan_uses_sandbag_when_no_roller():
    """验证帐篷 SKU 计划中的美国非美国本土计划使用沙袋当无拖轮包场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "沙袋四件套",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), HI, HONOLULU",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "SANDBAGS-4PCS"
    assert plan.customer_remark is None
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
    }


def test_us_non_mainland_without_roller_or_sandbag_replaces_top_not_instruction():
    """验证帐篷 SKU 计划中的美国非美国本土不依赖拖轮包或沙袋替换顶布不说明书场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "1全高背墙",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), PR, SAN JUAN",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10x10-Canopy-Topper"
    assert plan.customer_remark is None
    assert _actions(plan) == {
        "10X10-FRAME-40MM-SQUARE": 1,
        "10ft-Full-Wall": 1,
    }


def test_multi_set_tent_components_apply_group_multiplier():
    """验证帐篷 SKU 计划中的多行设置 帐篷 组件应用分组倍数场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "2套（3x3m帐篷顶+适配40mm六角铝+1全高背墙+2半高侧墙+1个6FT方套桌布+260g经编布）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.replace_main_sku == "Instruction"
    assert plan.replace_main_quantity == 2
    assert plan.customer_remark == "7.3发说明书"
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10X10-FRAME-40MM-HEX": 2,
        "10ft-Full-Wall": 2,
        "10ft-Half-Wall": 4,
        "Tablecloth-Rectangle-6ft": 2,
    }
    assert not plan.warnings


def test_multi_set_sandbag_replacement_does_not_add_duplicate_sandbags():
    """验证多套帐篷用沙袋换主商品后，不再额外添加同数量沙袋。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-9790716-5757037",
        system_order_no="103718452242155014",
        folder_components=[
            "加急111-9790716-5757037",
            "2套（3x3m帐篷顶+相同设计+40mm方形铝+1全高背墙+2双面半高侧墙+400D面料+沙袋四件套+绳子地钉）",
            "Deanna Sherman",
        ],
        destination_text="United States of America (USA)(美国), OH, CLEVELAND",
        shipping_deadline_text="2026-07-10 14:59:59",
        payment_time_text="2026-07-04 03:38:52",
        logistics_text="Expedited",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "SANDBAGS-4PCS"
    assert plan.replace_main_quantity == 2
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10X10-FRAME-40MM-SQUARE": 2,
        "10ft-Full-Wall": 2,
        "10ft-Half-Wall-Double-Sided": 4,
    }


def test_instruction_customer_remark_uses_china_workdays_and_deadline_date_only():
    """验证帐篷 SKU 计划中的说明书 客户备注 使用中国工作日并截止日期日期仅场景。"""
    assert build_instruction_customer_remark("2026-07-08 14:59:59") == "7.3发说明书"
    assert build_instruction_customer_remark("2026-07-03 14:59:59") == "6.30发说明书"


def test_expedited_instruction_customer_remark_uses_payment_date_only():
    """验证加急说明书备注使用付款当天日期。"""
    assert build_expedited_instruction_customer_remark("2026-07-03 14:59:59") == "7.3发说明书"
    assert build_expedited_instruction_customer_remark("付款时间 2026/07/04 08:00:00") == "7.4发说明书"


def test_expedited_instruction_plan_uses_payment_date_for_remark():
    """验证加急帐篷说明书备注使用付款当天而不是发货时限。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        payment_time_text="2026-07-03 14:59:59",
        logistics_text="Expedited",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "Instruction"
    assert plan.customer_remark == "7.3发说明书"


def test_chinese_expedited_instruction_plan_uses_payment_date_for_remark():
    """验证中文加急客选物流也按付款当天生成说明书备注。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        payment_time_text="2026-07-03 14:59:59",
        logistics_text="加急",
    )

    assert plan.manual_required is False
    assert plan.customer_remark == "7.3发说明书"


def test_expedited_instruction_plan_requires_manual_without_payment_date():
    """验证加急说明书备注缺少付款时间时不会猜日期。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        payment_time_text="",
        logistics_text="Expedited",
    )

    assert plan.manual_required is True
    assert plan.customer_remark is None
    assert "无法从付款时间中解析日期" in (plan.manual_reason or "")


def test_china_workday_calendar_loads_holidays_and_adjusted_workdays_from_json():
    """验证帐篷 SKU 计划中的中国工作日日历 loads holidays 并调休工作日来自JSON场景。"""
    from datetime import date

    assert is_china_workday(date(2026, 1, 1)) is False
    assert is_china_workday(date(2026, 1, 4)) is True


def test_instruction_customer_remark_accepts_common_date_formats():
    """验证帐篷 SKU 计划中的说明书 客户备注 接受常见日期 formats场景。"""
    assert build_instruction_customer_remark("2026-07-08") == "7.3发说明书"
    assert build_instruction_customer_remark("2026.07.08") == "7.3发说明书"
    assert build_instruction_customer_remark("2026/07/08") == "7.3发说明书"


def test_instruction_customer_remark_never_guesses_without_date_or_calendar():
    """验证帐篷 SKU 计划中的说明书 客户备注 绝不 guesses 不依赖日期或日历场景。"""
    try:
        build_instruction_customer_remark("6天1小时")
    except ShippingDeadlineDateParseError as exc:
        assert "无法从发货时限中解析日期" in str(exc)
    else:
        raise AssertionError("missing deadline date must not be guessed")

    try:
        build_instruction_customer_remark("2027-01-06 14:59:59")
    except ChinaWorkdayCalendarMissingError as exc:
        assert "缺少 2027 年" in str(exc)
    else:
        raise AssertionError("missing calendar year must not be guessed")


def test_instruction_plan_requires_manual_when_deadline_cannot_build_remark():
    """验证帐篷 SKU 计划中的说明书计划要求人工当截止日期无法生成备注场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="6天1小时",
    )

    assert plan.manual_required is True
    assert plan.customer_remark is None
    assert "无法自动生成客服备注" in (plan.manual_reason or "")


def test_sandbag_branch_does_not_generate_instruction_remark():
    """验证帐篷 SKU 计划中的沙袋分支 不会 生成说明书备注场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "沙袋四件套",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "SANDBAGS-4PCS"
    assert plan.customer_remark is None


def test_tent_accessory_flag_group_generates_sku_without_tent_size_warning():
    """验证帐篷 SKU 计划中的帐篷 配件旗帜分组生成SKU不依赖 帐篷 尺寸 warning场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="113-4042500-0544239",
        system_order_no="103716991507624096",
        folder_components=[
            "113-4042500-0544239",
            "1套（3x3m帐篷顶+40mm六角铝+1全高背墙+2半高侧墙+沙袋四件套）",
            "1套（0.5x2m双面刀旗+全纤维杆+连接件+夹具）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HARLINGEN",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "SANDBAGS-4PCS"
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-HEX": 1,
        "10ft-Full-Wall": 1,
        "10ft-Half-Wall": 2,
        "Feather-Flag-0.5x2m": 1,
    }
    assert not plan.warnings


def test_back_open_tablecloth_mappings_from_screenshot():
    """验证帐篷 SKU 计划中的回退打开 桌布 映射来自截图场景。"""
    cases = [
        ("1个4ft方套桌布（背后开口）+260g经编布", "Tablecloth-Rectangle-4ft"),
        ("1个5ft方套桌布（背后开口）+260g经编布", "Tablecloth-Rectangle-5ft"),
        ("1个6ft方套桌布（背后开口）+260g经编布", "Tablecloth-Rectangle-6ft"),
        ("1个8ft方套桌布（背后开口）+260g经编布", "Tablecloth-Rectangle-8ft"),
    ]

    for component, expected_sku in cases:
        items = tent_accessory_component_to_sku_items(component)
        assert [item.sku for item in items] == [expected_sku]


def test_tent_accessory_flag_group_applies_group_multiplier():
    """验证帐篷 SKU 计划中的帐篷 配件旗帜分组应用分组倍数场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm六角铝）",
            "2套（0.95x2.3m双面水滴旗+全纤维杆+铁板十字底座3KG+水袋）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    assert _actions(plan)["Teardrop-Flag-0.95x2.3m"] == 2
    assert not plan.warnings


def test_double_sided_wall_components_use_double_sided_skus():
    """验证帐篷 SKU 计划中的双面 侧墙组件使用 双面 skus场景。"""
    cases = [
        ("3x3m", "1双面全高背墙", "10ft-Full-Wall-Double-Sided", 1),
        ("3x3m", "2双面半高侧墙", "10ft-Half-Wall-Double-Sided", 2),
        ("3x4.5m", "1全围双面", "15ft-Full-Wall-Double-Sided", 1),
        ("3x4.5m", "2半围双面", "15ft-Half-Wall-Double-Sided", 2),
        ("3x6m", "1双面全围", "20ft-Full-Wall-Double-Sided", 1),
        ("3x6m", "2双面半围", "20ft-Half-Wall-Double-Sided", 2),
    ]

    for size_key, component, expected_sku, expected_quantity in cases:
        item = wall_sku_for_component(size_key, component)
        assert item is not None
        assert item.sku == expected_sku
        assert item.quantity == expected_quantity


def test_tent_plan_uses_double_sided_wall_skus():
    """验证帐篷 SKU 计划中的帐篷 计划使用 双面 侧墙 skus场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm六角铝+1双面全高背墙+2双面半高侧墙）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    actions = _actions(plan)
    assert actions["10ft-Full-Wall-Double-Sided"] == 1
    assert actions["10ft-Half-Wall-Double-Sided"] == 2
    assert "10ft-Full-Wall" not in actions
    assert "10ft-Half-Wall" not in actions


def test_wall_only_full_wall_asin_replaces_main_with_full_wall_sku():
    """验证帐篷 SKU 计划中的侧墙仅全高侧墙ASIN替换主带有全高侧墙SKU场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "1个3x3m帐篷的全高背墙",
            "系带",
            "400D面料",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6KZ7G88",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Full-Wall"
    assert plan.customer_remark is None
    assert _actions(plan) == {}


def test_wall_only_full_wall_asin_uses_double_sided_full_wall_sku():
    """验证帐篷 SKU 计划中的侧墙仅全高侧墙ASIN使用 双面 全高侧墙SKU场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "1个3x3m帐篷的双面全高背墙",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6KZ7G88",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Full-Wall-Double-Sided"
    assert _actions(plan) == {}


def test_wall_only_full_wall_replacement_skips_full_replaced_quantity():
    """验证独立全高墙换主商品后，主商品行数量按原数量设置，不再重复补同 SKU。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "2个3x3m帐篷的全高背墙",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6KZ7G88",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Full-Wall"
    assert plan.replace_main_quantity == 2
    assert _actions(plan) == {}


def test_wall_only_half_wall_asin_replaces_main_with_half_wall_sku_without_size_text():
    """验证帐篷 SKU 计划中的侧墙仅半高侧墙ASIN替换主带有半高侧墙SKU不依赖尺寸文本场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "1半高侧墙",
            "加横杆适配50mm六角铝夹具",
            "400D面料",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6XWP8YN",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Half-Wall"
    assert plan.customer_remark is None
    assert _actions(plan) == {}
    assert not plan.warnings


def test_wall_only_half_wall_replacement_skips_full_replaced_quantity():
    """验证独立半墙换主商品后，主商品行数量按原数量设置，不再重复补同 SKU。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "2半高侧墙",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6XWP8YN",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Half-Wall"
    assert plan.replace_main_quantity == 2
    assert _actions(plan) == {}


def test_wall_only_half_wall_asin_uses_double_sided_half_wall_sku():
    """验证帐篷 SKU 计划中的侧墙仅半高侧墙ASIN使用 双面 半高侧墙SKU场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "1双面半高侧墙",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6XWP8YN",
    )

    assert plan.manual_required is False
    assert plan.replace_main_sku == "10ft-Half-Wall-Double-Sided"
    assert _actions(plan) == {}


def test_wall_only_asin_requires_manual_when_matching_wall_component_missing():
    """验证帐篷 SKU 计划中的侧墙仅ASIN要求人工当匹配侧墙组件缺失场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="114-0131738-0578639",
        system_order_no="103700000000000000",
        folder_components=[
            "114-0131738-0578639",
            "1个3x3m帐篷顶",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        asin="B0D6KZ7G88",
    )

    assert plan.manual_required is True
    assert "独立墙体 ASIN" in (plan.manual_reason or "")


def test_3x3m_half_wall_with_rail_uses_frame_rail_sku():
    """验证帐篷 SKU 计划中的3x 3m 半高侧墙带有横杆使用框架横杆SKU场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="113-0617749-0645052",
        system_order_no="103716416030789168",
        folder_components=[
            "113-0617749-0645052",
            "1个3x3m帐篷顶",
            "50mm六角铝",
            "1双面全高背墙",
            "2半高侧墙(带横杆)",
            "拖轮包",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), MA, CAMBRIDGE",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    actions = _actions(plan)
    assert plan.replace_main_sku == "TENT-ROLLER-BAG-10X10-50MM"
    assert actions["10X10-FRAME-50MM-HEX-RAIL"] == 1
    assert "10X10-FRAME-50MM-HEX" not in actions
    assert actions["10ft-Full-Wall-Double-Sided"] == 1
    assert "10ft-Full-Wall" not in actions
    assert actions["10ft-Half-Wall"] == 2


def test_3x3m_half_wall_with_rail_applies_to_square_frame():
    """验证帐篷 SKU 计划中的3x 3m 半高侧墙带有横杆应用到 square 框架场景。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+2半高侧墙(带横杆)）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    actions = _actions(plan)
    assert actions["10X10-FRAME-40MM-SQUARE-RAIL"] == 1
    assert "10X10-FRAME-40MM-SQUARE" not in actions


def test_larger_tents_with_half_wall_rail_keep_non_rail_frame_sku():
    """验证帐篷 SKU 计划中的larger tents 带有半高侧墙横杆保留非横杆框架SKU场景。"""
    cases = [
        (
            "3x4.5m",
            "50mm六角铝",
            "10X15-FRAME-50MM-HEX",
            "10X15-FRAME-50MM-HEX-RAIL",
        ),
        (
            "3x6m",
            "40mm方形铝",
            "10X20-FRAME-40MM-SQUARE",
            "10X20-FRAME-40MM-SQUARE-RAIL",
        ),
    ]

    for size_text, frame_text, expected_sku, unexpected_sku in cases:
        plan = build_tent_sku_plan(
            platform_order_no="111-0000000-0000000",
            system_order_no="103700000000000000",
            folder_components=[
                "111-0000000-0000000",
                f"1套（{size_text}帐篷顶+{frame_text}+2半高侧墙(带横杆)）",
                "Buyer Name",
            ],
            destination_text="United States of America (USA), TX, HOUSTON",
            shipping_deadline_text="2026-07-08 14:59:59",
        )

        actions = _actions(plan)
        assert actions[expected_sku] == 1
        assert unexpected_sku not in actions


def test_non_mainland_larger_tents_with_half_wall_rail_use_rail_frame_sku():
    """验证加拿大/美国非本土大尺寸帐篷带横杆时使用带横杆支架 SKU。"""
    cases = [
        (
            "Canada, ON, TORONTO",
            "3x4.5m",
            "40mm方形铝",
            "10X15-FRAME-40MM-SQUARE-RAIL",
            "10X15-FRAME-40MM-SQUARE",
        ),
        (
            "United States of America (USA), PR, SAN JUAN",
            "3x6m",
            "50mm六角铝",
            "10X20-FRAME-50MM-HEX-RAIL",
            "10X20-FRAME-50MM-HEX",
        ),
    ]

    for destination_text, size_text, frame_text, expected_sku, unexpected_sku in cases:
        plan = build_tent_sku_plan(
            platform_order_no="111-0000000-0000000",
            system_order_no="103700000000000000",
            folder_components=[
                "111-0000000-0000000",
                f"1套（{size_text}帐篷顶+{frame_text}+2半高侧墙(带横杆)）",
                "Buyer Name",
            ],
            destination_text=destination_text,
            shipping_deadline_text="2026-07-08 14:59:59",
        )

        actions = _actions(plan)
        assert actions[expected_sku] == 1
        assert unexpected_sku not in actions


def test_canada_38mm_frame_uses_38mm_sku_with_rail():
    """验证加拿大 38mm 方形铝支架添加 38mm 且带横杆的 SKU。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x4.5m帐篷顶+38mm方形铝+2半高侧墙(带横杆)）",
            "Buyer Name",
        ],
        destination_text="Canada, ON, TORONTO",
        shipping_deadline_text="2026-07-08 14:59:59",
    )

    actions = _actions(plan)
    assert actions["10X15-FRAME-38MM-SQUARE-RAIL"] == 1
    assert "10X15-FRAME-40MM-SQUARE-RAIL" not in actions


def test_default_expedited_tent_asin_replaces_frame_without_instruction_remark():
    """验证默认加急 ASIN 美国本土无配件时换支架且不写说明书备注。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "400D面料",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON",
        shipping_deadline_text="2026-07-08 14:59:59",
        payment_time_text="2026-07-04 08:00:00",
        logistics_text="Standard",
        asin="B0CRRGTPFH",
    )

    assert _replacements(plan) == [("10X10-FRAME-40MM-SQUARE", 1)]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
    }
    assert plan.customer_remark is None


def test_parse_destination_region_keeps_us_zip_leading_zero():
    region = parse_destination_region(
        "收件地址 United States of America (USA), NY, ALBANY 详细地址 1 TEST RD 邮编 01020-1234"
    )

    assert region.category == "us_mainland"
    assert region.state == "NY"
    assert region.postal_code == "01020"


def test_multi_tent_non_priority_zip_replaces_each_main_with_roller_and_deducts_added_rollers():
    plan = build_tent_sku_plan(
        platform_order_no="111-8112209-3174649",
        system_order_no="103719401767966430",
        folder_components=[
            "111-8112209-3174649",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), MI, PETOSKEY 邮编 49779-1234",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [
        ("TENT-ROLLER-BAG-10X10-50MM", 1),
        ("TENT-ROLLER-BAG-10X10-50MM", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10X10-FRAME-40MM-SQUARE": 2,
    }


def test_grouped_multi_tent_frame_priority_zip_replaces_each_main_with_frame():
    """验证新文件夹括号分组不会导致帐篷配件被整体误判为帐篷顶。"""
    plan = build_tent_sku_plan(
        platform_order_no="111-8112209-3174649",
        system_order_no="103719401767966430",
        folder_components=[
            "111-8112209-3174649",
            "1个(3x3m帐篷顶+相同设计+40mm方形铝+1全高背墙+400D面料+拖轮包)",
            "1个(3x3m帐篷顶+相同设计+40mm方形铝+400D面料+拖轮包)",
            "Xander Tams",
        ],
        destination_text="United States of America (USA), MI, PETOSKEY 邮编 12010-1234",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [
        ("10X10-FRAME-40MM-SQUARE", 1),
        ("10X10-FRAME-40MM-SQUARE", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10ft-Full-Wall": 1,
        "TENT-ROLLER-BAG-10X10-50MM": 2,
    }
    assert plan.customer_remark is None


def test_b0crrgtpfh_priority_zip_still_prefers_roller_before_frame():
    plan = build_tent_sku_plan(
        platform_order_no="111-8112209-3174649",
        system_order_no="103719401767966430",
        folder_components=[
            "111-8112209-3174649",
            "1个(3x3m帐篷顶+相同设计+40mm方形铝+1全高背墙+400D面料+拖轮包)",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), MI, PETOSKEY 邮编 12010-1234",
        shipping_deadline_text="2026-07-10 14:59:59",
        payment_time_text="2026-07-04 08:00:00",
        logistics_text="Standard",
        asin="B0CRRGTPFH",
    )

    assert _replacements(plan) == [("TENT-ROLLER-BAG-10X10-50MM", 1)]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
        "10ft-Full-Wall": 1,
    }
    assert plan.customer_remark is None


def test_b0crrgtpfh_uses_sandbag_when_no_roller():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+沙袋四件套）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON 邮编 77001",
        shipping_deadline_text="2026-07-10 14:59:59",
        payment_time_text="2026-07-04 08:00:00",
        logistics_text="Standard",
        asin="B0CRRGTPFH",
    )

    assert _replacements(plan) == [("SANDBAGS-4PCS", 1)]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
    }
    assert plan.customer_remark is None


def test_b0crrgtpfh_uses_frame_when_accessories_are_short():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "1套（3x3m帐篷顶+40mm方形铝）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON 邮编 77001",
        shipping_deadline_text="2026-07-10 14:59:59",
        payment_time_text="2026-07-04 08:00:00",
        logistics_text="Standard",
        asin="B0CRRGTPFH",
    )

    assert _replacements(plan) == [
        ("TENT-ROLLER-BAG-10X10-50MM", 1),
        ("10X10-FRAME-40MM-SQUARE", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10X10-FRAME-40MM-SQUARE": 1,
    }
    assert plan.customer_remark is None


def test_multi_tent_non_priority_zip_uses_sandbag_fallback_when_accessories_are_short():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "1套（3x3m帐篷顶+40mm方形铝）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), MI, PETOSKEY 邮编 49779",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [
        ("TENT-ROLLER-BAG-10X10-50MM", 1),
        ("SANDBAGS-4PCS", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "10X10-FRAME-40MM-SQUARE": 2,
    }


def test_us_zip_010_to_199_prefers_frame_replacements():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), NY, ALBANY 邮编 01020",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [
        ("10X10-FRAME-40MM-SQUARE", 1),
        ("10X10-FRAME-40MM-SQUARE", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
        "TENT-ROLLER-BAG-10X10-50MM": 2,
    }


def test_ca_zip_900_to_961_prefers_frame_but_other_900_zip_does_not():
    ca_plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), CA, LOS ANGELES 邮编 90001",
        shipping_deadline_text="2026-07-10 14:59:59",
    )
    tx_plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), TX, HOUSTON 邮编 90001",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(ca_plan) == [("10X10-FRAME-40MM-SQUARE", 1)]
    assert _actions(ca_plan) == {
        "10x10-Canopy-Topper": 1,
        "TENT-ROLLER-BAG-10X10-50MM": 1,
    }
    assert _replacements(tx_plan) == [("TENT-ROLLER-BAG-10X10-50MM", 1)]
    assert _actions(tx_plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
    }


def test_ca_zip_962_does_not_prefer_frame():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), CA, LOS ANGELES 邮编 96200",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [("TENT-ROLLER-BAG-10X10-50MM", 1)]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 1,
        "10X10-FRAME-40MM-SQUARE": 1,
    }


def test_frame_priority_uses_accessory_or_instruction_when_frames_are_short():
    plan = build_tent_sku_plan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        folder_components=[
            "111-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝）",
            "1套（3x3m帐篷顶+拖轮包）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), NY, ALBANY 邮编 19999",
        shipping_deadline_text="2026-07-10 14:59:59",
    )

    assert _replacements(plan) == [
        ("10X10-FRAME-40MM-SQUARE", 1),
        ("TENT-ROLLER-BAG-10X10-50MM", 1),
    ]
    assert _actions(plan) == {
        "10x10-Canopy-Topper": 2,
    }


def test_multi_main_tent_uses_roller_and_sandbag_without_recipient_warning():
    plan = build_tent_sku_plan(
        platform_order_no="113-7978998-3154600",
        system_order_no="103720929318172445",
        folder_components=[
            "113-7978998-3154600",
            "3x6m帐篷顶",
            "40mm六角铝",
            "拖轮包",
            "沙袋六件套",
            "April Tollette, Bixby Fire Department",
        ],
        destination_text="United States of America (USA), OK, Bixby 邮编 74008",
        shipping_deadline_text="2026-07-21 14:59:59",
        payment_time_text="2026-07-11 03:35:01",
        logistics_text="Standard",
        asin="B0F5CKNVYJ",
        order_lines=_multi_main_order_lines(),
    )

    assert [(item.sku, item.source_scope, item.source_sku) for item in plan.replace_main_items] == [
        ("TENT-ROLLER-BAG-10X20-50MM", "tent", "Canopy-Tent-10x20"),
        ("SANDBAGS-4PCS", "other_main", "Tablecloth-Spandex-6ft"),
    ]
    assert [(item.sku, item.quantity) for item in plan.main_product_items] == [
        ("TENT-ROLLER-BAG-10X20-50MM", 1),
        ("SANDBAGS-4PCS", 1),
    ]
    assert not any("April Tollette" in warning for warning in plan.warnings)


def test_multi_main_tent_with_one_accessory_only_replaces_tent():
    plan = build_tent_sku_plan(
        platform_order_no="113-7978998-3154600",
        system_order_no="103720929318172445",
        folder_components=[
            "113-7978998-3154600",
            "3x6m帐篷顶",
            "40mm六角铝",
            "拖轮包",
            "April Tollette, Bixby Fire Department",
        ],
        destination_text="United States of America (USA), OK, Bixby 邮编 74008",
        shipping_deadline_text="2026-07-21 14:59:59",
        asin="B0F5CKNVYJ",
        order_lines=_multi_main_order_lines(),
    )

    assert [(item.sku, item.source_scope) for item in plan.replace_main_items] == [
        ("TENT-ROLLER-BAG-10X20-50MM", "tent"),
    ]
    assert [item.sku for item in plan.main_product_items] == [
        "TENT-ROLLER-BAG-10X20-50MM",
        "Tablecloth-Spandex-6ft",
    ]


def test_multi_main_tent_without_accessory_uses_frame_for_priority_zip():
    plan = build_tent_sku_plan(
        platform_order_no="113-0000000-0000000",
        system_order_no="103720000000000000",
        folder_components=["113-0000000-0000000", "3x6m帐篷顶", "40mm六角铝", "Buyer Name"],
        destination_text="United States of America (USA), NY, Albany ZIP 12010",
        shipping_deadline_text="2026-07-21 14:59:59",
        asin="B0F5CKNVYJ",
        order_lines=_multi_main_order_lines(),
    )

    assert [(item.sku, item.source_scope) for item in plan.replace_main_items] == [
        ("10X20-FRAME-40MM-HEX", "tent"),
    ]
    assert [item.sku for item in plan.main_product_items] == [
        "10X20-FRAME-40MM-HEX",
        "Tablecloth-Spandex-6ft",
    ]


def test_multi_main_tent_without_accessory_uses_instruction_for_normal_zip():
    plan = build_tent_sku_plan(
        platform_order_no="113-0000000-0000000",
        system_order_no="103720000000000000",
        folder_components=["113-0000000-0000000", "3x6m帐篷顶", "40mm六角铝", "Buyer Name"],
        destination_text="United States of America (USA), OK, Bixby 邮编 74008",
        shipping_deadline_text="2026-07-21 14:59:59",
        payment_time_text="2026-07-11 03:35:01",
        logistics_text="Standard",
        asin="B0F5CKNVYJ",
        order_lines=_multi_main_order_lines(),
    )

    assert [(item.sku, item.source_scope) for item in plan.replace_main_items] == [
        ("Instruction", "tent"),
    ]
    assert plan.customer_remark
    assert [item.sku for item in plan.main_product_items] == ["Instruction", "Tablecloth-Spandex-6ft"]


def test_two_tent_main_rows_use_their_post_replacement_skus():
    order_lines = [
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="custom-tent-package-10x10",
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=1,
            customization_text="",
            order_item_id="164651094611521",
        ),
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="custom-tent-package-10x10",
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=1,
            customization_text="",
            order_item_id="164651094611441",
        ),
    ]
    plan = build_tent_sku_plan(
        platform_order_no="113-5993563-8330664",
        system_order_no="103722006385024604",
        folder_components=[
            "113-5993563-8330664",
            "1个（3x3m帐篷顶+40mm方形铝+400D面料）",
            "1个（3x3m帐篷顶+400D面料+适用足尺寸架子）",
            "Hannah bailiff",
        ],
        destination_text="United States of America (USA), LA, SHREVEPORT 邮编 71101",
        shipping_deadline_text="2026-07-20 14:59:59",
        payment_time_text="2026-07-14 04:39:45",
        logistics_text="Standard",
        asin="B0DZ2W2QWK",
        order_lines=order_lines,
    )

    assert [(item.sku, item.quantity) for item in plan.replace_main_items] == [
        ("Instruction", 1),
        ("Instruction", 1),
    ]
    assert [(item.sku, item.quantity) for item in plan.main_product_items] == [
        ("Instruction", 1),
        ("Instruction", 1),
    ]
    assert all(item.sku != "custom-tent-package-10x10" for item in plan.main_product_items)


def test_two_tent_main_rows_support_different_post_replacement_skus():
    order_lines = [
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="custom-tent-package-10x10-a",
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=1,
            customization_text="",
        ),
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="custom-tent-package-10x10-b",
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=1,
            customization_text="",
        ),
    ]
    plan = build_tent_sku_plan(
        platform_order_no="113-0000000-0000000",
        system_order_no="103720000000000000",
        folder_components=[
            "113-0000000-0000000",
            "1套（3x3m帐篷顶+40mm方形铝）",
            "1套（3x3m帐篷顶）",
            "Buyer Name",
        ],
        destination_text="United States of America (USA), NY, Albany ZIP 12010",
        shipping_deadline_text="2026-07-20 14:59:59",
        payment_time_text="2026-07-14 04:39:45",
        logistics_text="Standard",
        asin="B0DZ2W2QWK",
        order_lines=order_lines,
    )

    assert [(item.sku, item.quantity) for item in plan.main_product_items] == [
        ("10X10-FRAME-40MM-SQUARE", 1),
        ("Instruction", 1),
    ]


def test_single_main_row_quantity_keeps_post_replacement_quantity_together():
    order_lines = [
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="custom-tent-package-10x10",
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=2,
            customization_text="",
        )
    ]
    plan = build_tent_sku_plan(
        platform_order_no="113-0000000-0000000",
        system_order_no="103720000000000000",
        folder_components=["113-0000000-0000000", "2套（3x3m帐篷顶）", "Buyer Name"],
        destination_text="United States of America (USA), LA, SHREVEPORT ZIP 71101",
        shipping_deadline_text="2026-07-20 14:59:59",
        payment_time_text="2026-07-14 04:39:45",
        logistics_text="Standard",
        asin="B0DZ2W2QWK",
        order_lines=order_lines,
    )

    assert [(item.sku, item.quantity) for item in plan.main_product_items] == [("Instruction", 2)]
