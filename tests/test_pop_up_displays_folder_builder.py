from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.pop_up_displays import (
    POP_UP_DISPLAY_EMAIL_PROMPT,
    POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND,
    POP_UP_DISPLAY_PHONE_PROMPT,
    POP_UP_DISPLAY_PRODUCT_NAME_BY_CHILD,
    POP_UP_DISPLAY_PRODUCT_NAME_BY_PARENT,
    PRODUCT_TYPE_POP_UP_DISPLAYS,
    find_pop_up_display_parent_asin,
    get_pop_up_display_product_name,
    get_pop_up_display_size,
    get_pop_up_display_stand_type,
    is_pop_up_display_asin,
)
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines


PROOF_TITLE = "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)."


def _order(platform_order_no: str = "112-0000000-0000000") -> BatchOrderItem:
    """构造展架文件夹生成测试所需的订单对象。"""
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-22 10:00:00",
    )


def _line(
    *,
    asin: str,
    quantity: int,
    pairs: dict[str, str],
    order_item_id: str = "pop-up-item-1",
) -> OrderFolderLine:
    """构造展架文件夹生成测试所需的订单行对象。"""
    return OrderFolderLine(
        asin=asin,
        sku="pop-up-sku",
        parent_asin=find_pop_up_display_parent_asin(asin),
        product_type=PRODUCT_TYPE_POP_UP_DISPLAYS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs,
        order_item_id=order_item_id,
    )


def _folder_name(platform_order_no: str, lines: list[OrderFolderLine], customer_name: str, tmp_path) -> str:
    """生成展架文件夹生成测试断言使用的文件夹名。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order(platform_order_no),
        order_lines=lines,
        recipient_name=customer_name,
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert result.status == "folder_preview"
    return result.folder_name or ""


def test_pop_up_display_parent_child_mapping_and_catalog():
    """验证展架文件夹生成中的展架 父子映射并目录场景。"""
    assert POP_UP_DISPLAY_PRODUCT_NAME_BY_PARENT == {
        "B0H36GPHVH": "拉网展架",
        "B0G6KJQPHK": "伸缩展架",
        "B0FX2828C9": "快幕秀",
    }
    assert set(POP_UP_DISPLAY_PRODUCT_NAME_BY_CHILD) == {
        "B0FX9VSXHD",
        "B0FX9XMP9D",
        "B0FX9W6684",
        "B0FX9XMBJK",
        "B0FX9VGDCY",
        "B0FX9Y2DXM",
        "B0FX9TDRPR",
        "B0FX9YPQ2C",
    }
    assert set(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND) == {"带支架", "不带支架"}
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["带支架"]["B0H36GPHVH"]) == 7
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["带支架"]["B0G6KJQPHK"]) == 2
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["带支架"]["B0FX2828C9"]) == 8
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["不带支架"]["B0H36GPHVH"]) == 7
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["不带支架"]["B0G6KJQPHK"]) == 2
    assert len(POP_UP_DISPLAY_PARENT_TO_CHILD_SIZE_BY_STAND["不带支架"]["B0FX2828C9"]) == 8

    assert is_pop_up_display_asin("B0H3V1K5W5")
    assert find_pop_up_display_parent_asin("B0H3V1K5W5") == "B0H36GPHVH"
    assert get_pop_up_display_size("B0H3V1K5W5") == "5x7.5ft"
    assert get_pop_up_display_stand_type("B0H3V1K5W5") == "带支架"
    assert get_pop_up_display_product_name("B0H36GPHVH") == "拉网展架"
    assert get_pop_up_display_stand_type("B0H36MBKJT") == "不带支架"
    assert get_pop_up_display_size("B0G6KQGJC9") == "8x10ft"
    assert get_pop_up_display_product_name("B0G6KJQPHK") == "伸缩展架"
    assert get_pop_up_display_size("B0FX9W5HGP") == "7.5x20ft"
    assert get_pop_up_display_stand_type("B0FX9XHQ7F") == "不带支架"
    assert get_pop_up_display_product_name("B0FX2828C9") == "快幕秀"
    assert get_pop_up_display_product_name("B0FX2828C9", "B0FX9VSXHD") == "门型快幕秀"
    assert get_pop_up_display_product_name("B0FX2828C9", "B0FX9W5HGP") == "快幕秀"

    match = match_supported_product("B0H3V1K5W5")
    assert match is not None
    assert match.product_type == PRODUCT_TYPE_POP_UP_DISPLAYS


def test_b0h36_options_folder_name_with_side_panels_led_and_proof(tmp_path):
    """验证展架文件夹生成中的b 0h 36 选项 文件夹名 带有面面板LED并确认稿场景。"""
    line = _line(
        asin="B0H3V1K5W5",
        quantity=1,
        pairs={
            "Single/Double-Sided Printing Options": "Double-Sided",
            "Is The Back Side Using The Same Design As The Front Side?": "No,Using Different Design for Back Side",
            "Side Panels Options": "Endcaps",
            "LED Light Options": "Yes, I need a 2-pack LED Light.",
            PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
        },
    )

    assert _folder_name("112-1111111-1111111", [line], "Display Buyer", tmp_path) == (
        "112-1111111-1111111+1个双面5x7.5ft拉网展架+不同设计+带支架+有侧边+2组LED+Display Buyer+在线检查"
    )


def test_no_stand_frame_options_replace_no_stand_text(tmp_path):
    """验证展架文件夹生成中的无展架框架选项替换无展架文本场景。"""
    line = _line(
        asin="B0H36MBKJT",
        quantity=1,
        pairs={
            "Single/Double-Sided Printing Options": "Single-Sided",
            "Is The Back Side Using The Same Design As The Front Side?": "Yes, Using Same Design for Back Side",
            "Frame Options": "Adjustable Frame",
            "Side Panels Options": "No Endcaps",
        },
    )

    assert _folder_name("112-2222222-2222222", [line], "Display Buyer", tmp_path) == (
        "112-2222222-2222222+1个单面5x7.5ft拉网展架+相同设计+可调节框架+无侧边+Display Buyer"
    )


def test_no_stand_b0fx_frame_option_replaces_no_stand_text(tmp_path):
    """验证展架文件夹生成中的无展架 b 0fx 框架选项替换无展架文本场景。"""
    line = _line(
        asin="B0FX29VVBH",
        quantity=1,
        pairs={
            "Double-sided Printing Options": "Single-Sided",
            "Frame Options": "Aluminum Frame",
        },
    )

    folder_name = _folder_name("111-8455789-7723439", [line], "scott carnwath", tmp_path)
    assert folder_name == (
        "111-8455789-7723439+1个单面7.5x7.5ft快幕秀+铝制框架+scott carnwath"
    )
    assert "不带支架" not in folder_name


def test_no_stand_no_frame_keeps_no_stand_text(tmp_path):
    """验证展架文件夹生成中的无展架无框架保留无展架文本场景。"""
    line = _line(
        asin="B0FX29VVBH",
        quantity=1,
        pairs={
            "Double-sided Printing Options": "Single-Sided",
            "Frame Options": "No Frame",
        },
    )

    assert _folder_name("112-2222222-2222223", [line], "Display Buyer", tmp_path) == (
        "112-2222222-2222223+1个单面7.5x7.5ft快幕秀+不带支架+Display Buyer"
    )


def test_b0g6_fabric_panel_quantity_field_is_included(tmp_path):
    """验证展架文件夹生成中的b 0g 6 面料面板数量字段为包含场景。"""
    line = _line(
        asin="B0G6JZJDDJ",
        quantity=1,
        pairs={
            "Is The Panel 2 Using The Same Design As Panel 1?": "Yes, Using Same Design for Panel 2",
            "Fabric Panel Quantity Options": "2 Panels (Single-Sided Print)",
            PROOF_TITLE: "Straight To Production",
        },
    )

    assert _folder_name("112-3333333-3333333", [line], "Display Buyer", tmp_path) == (
        "112-3333333-3333333+1个8x8ft伸缩展架+相同设计+带支架+2个布面+Display Buyer+直接制作"
    )


def test_b0fx_led_four_pack(tmp_path):
    """验证展架文件夹生成中的b 0fx LED四包场景。"""
    line = _line(
        asin="B0FX9VSXHD",
        quantity=1,
        pairs={
            "Double-sided Printing Options": "Single-Sided",
            "Is The Back Side Using The Same Design As The Front Side?": "Yes, Using Same Design for Back Side",
            "LED Light Options": "Yes, I need a 4-pack LED Light.",
        },
    )

    assert _folder_name("112-4444444-4444444", [line], "Display Buyer", tmp_path) == (
        "112-4444444-4444444+1个单面3x7.5ft门型快幕秀+相同设计+带支架+4组LED+Display Buyer"
    )


def test_b0fx_non_door_shape_children_keep_default_product_name(tmp_path):
    """验证展架文件夹生成中的b 0fx 非门型结构子项保留默认产品名称场景。"""
    line = _line(
        asin="B0FX9W5HGP",
        quantity=1,
        pairs={
            "Double-sided Printing Options": "Double-Sided",
            "Is The Back Side Using The Same Design As The Front Side?": "No,Using Different Design for Back Side",
        },
    )

    assert _folder_name("112-4444444-4444445", [line], "Display Buyer", tmp_path) == (
        "112-4444444-4444445+1个双面7.5x20ft快幕秀+不同设计+带支架+Display Buyer"
    )


def test_pop_up_display_missing_printing_sides_and_unknown_option_return_product_status(tmp_path):
    """验证展架文件夹生成中的展架 缺失打印面数并未知选项返回产品状态场景。"""
    missing = build_and_create_order_folder_from_lines(
        order_item=_order("112-5555555-5555555"),
        order_lines=[
            _line(
                asin="B0H3V1K5W5",
                quantity=1,
                pairs={"Is The Back Side Using The Same Design As The Front Side?": "Yes, Using Same Design for Back Side"},
            )
        ],
        recipient_name="Display Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert missing.status == "pop_up_displays_rule_missing_printing_sides"
    assert missing.missing_rule_title == "Single/Double-Sided Printing Options"
    assert missing.missing_rule_value == "missing"
    assert missing.missing_rule_line == "1.Single/Double-Sided Printing Options = missing"

    unknown = build_and_create_order_folder_from_lines(
        order_item=_order("112-5555555-5555555"),
        order_lines=[
            _line(
                asin="B0H3V1K5W5",
                quantity=1,
                pairs={
                    "Single/Double-Sided Printing Options": "Single-Sided",
                    "Side Panels Options": "Mystery Side",
                },
            )
        ],
        recipient_name="Display Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert unknown.status == "pop_up_displays_rule_missing"
    assert unknown.missing_rule_title == "Side Panels Options"
    assert unknown.missing_rule_value == "Mystery Side"


def test_pop_up_display_same_asin_same_options_keep_order_lines(tmp_path):
    """验证展架文件夹生成中的展架 相同ASIN相同选项保留订单行场景。"""
    pairs = {
        "Single/Double-Sided Printing Options": "Double-Sided",
        "Is The Back Side Using The Same Design As The Front Side?": "No,Using Different Design for Back Side",
        "Side Panels Options": "Endcaps",
    }
    lines = [
        _line(asin="B0H3V1K5W5", quantity=1, pairs=pairs, order_item_id="a"),
        _line(asin="B0H3V1K5W5", quantity=1, pairs=pairs, order_item_id="b"),
    ]

    assert _folder_name("112-6666666-6666666", lines, "Display Buyer", tmp_path) == (
        "112-6666666-6666666+1个双面5x7.5ft拉网展架+不同设计+带支架+有侧边+"
        "1个双面5x7.5ft拉网展架+不同设计+带支架+有侧边+Display Buyer"
    )


def test_pop_up_display_contact_prompts_use_json_values():
    """验证展架文件夹生成中的展架 联系方式 提示使用JSON值场景。"""
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0H3V1K5W5",
        title=None,
        quantity=1,
        pairs={
            POP_UP_DISPLAY_EMAIL_PROMPT: "buyer@example.com",
            POP_UP_DISPLAY_PHONE_PROMPT: "+1 925-822-2350",
        },
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "9258222350"
    assert contact.email == "buyer@example.com"
