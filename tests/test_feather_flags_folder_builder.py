from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.feather_flags import (
    FEATHER_FLAG_CONTACT_PROMPT,
    FEATHER_FLAG_PARENT_ASIN,
    FEATHER_FLAG_PRODUCT_NAME_BY_ASIN,
    FEATHER_FLAG_SIZE_BY_ASIN,
    PRODUCT_TYPE_FEATHER_FLAGS,
    find_feather_flag_parent_asin,
    get_feather_flag_product_name,
    get_feather_flag_size,
    is_feather_flag_asin,
)
from lingxing_automation.products.table_runners import PRODUCT_TYPE_TABLE_RUNNERS
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines


PRINTING_SIDE = "Printing Side"
POLE_TYPE = "Pole Type"
CROSS_BASE = "Cross Base"
GROUND_SPIKE = "Ground Spike"
WATER_BAG = "Wather Bag"
CARRYING_BAG = "Carrying Bag"
PROOF_TITLE = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"


def _order(platform_order_no: str = "112-0000000-0000000") -> BatchOrderItem:
    """构造刀旗文件夹生成测试所需的订单对象。"""
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-23 10:00:00",
    )


def _base_pairs() -> dict[str, str]:
    """构造刀旗文件夹生成测试所需的基础定制化选项。"""
    return {
        PRINTING_SIDE: "Single-Sided Printing",
        POLE_TYPE: "No, I don't need a Pole.",
        CROSS_BASE: "No, I don't need a Cross Base.",
        GROUND_SPIKE: "No, I don't need a Ground Spike.",
        WATER_BAG: "No",
        CARRYING_BAG: "No",
    }


def _line(*, asin: str = "B0DS22NHGT", quantity: int = 1, pairs: dict[str, str] | None = None) -> OrderFolderLine:
    """构造刀旗文件夹生成测试所需的订单行对象。"""
    return OrderFolderLine(
        asin=asin,
        sku="feather-flag-sku",
        parent_asin=find_feather_flag_parent_asin(asin),
        product_type=PRODUCT_TYPE_FEATHER_FLAGS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs or _base_pairs(),
        order_item_id=f"feather-{asin}-{quantity}",
    )


def test_feather_flag_parent_child_mapping_and_catalog():
    """验证刀旗文件夹生成中的刀旗 父子映射并目录场景。"""
    assert set(FEATHER_FLAG_PRODUCT_NAME_BY_ASIN) == set(FEATHER_FLAG_SIZE_BY_ASIN)

    for asin, size in FEATHER_FLAG_SIZE_BY_ASIN.items():
        assert is_feather_flag_asin(asin)
        assert find_feather_flag_parent_asin(asin) == FEATHER_FLAG_PARENT_ASIN
        assert get_feather_flag_size(asin) == size
        assert get_feather_flag_product_name(asin) == FEATHER_FLAG_PRODUCT_NAME_BY_ASIN[asin]

        match = match_supported_product(f"ASIN {asin}")

        assert match is not None
        assert match.product_type == PRODUCT_TYPE_FEATHER_FLAGS
        assert match.parent_asin == FEATHER_FLAG_PARENT_ASIN
        assert match.contact_prompts == (FEATHER_FLAG_CONTACT_PROMPT,)


def test_feather_flag_single_quantity_folder_name(tmp_path):
    """验证刀旗文件夹生成中的刀旗 单面数量 文件夹名场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Same design both sides",
        POLE_TYPE: "Aluminum-fiberglass Pole",
        GROUND_SPIKE: "Yes, I need a Ground Spike.",
        PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("113-8805683-8497834"),
        order_lines=[_line(pairs=pairs)],
        recipient_name="Courtney Brown",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert (
        result.folder_name
        == "113-8805683-8497834+1套(0.5x2m双面刀旗+相同设计+铝纤维杆+地钉)+Courtney Brown+在线检查"
    )


def test_feather_flag_multiple_quantity_wraps_package(tmp_path):
    """验证刀旗文件夹生成中的刀旗 多个数量包装套餐场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "Single-Sided Printing",
        POLE_TYPE: "Aluminum-fiberglass Pole",
        CARRYING_BAG: "Yes, I need a Carrying Bag",
        PROOF_TITLE: "Straight To Production",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("113-7917109-2635430"),
        order_lines=[_line(quantity=2, pairs=pairs)],
        recipient_name="Jason Brown",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "113-7917109-2635430+2套（0.5x2m单面刀旗+铝纤维杆+手提袋）+Jason Brown+直接制作"


def test_feather_flag_aluminum_pole_alias_builds_known_order_folder(tmp_path):
    """验证刀旗文件夹生成中的刀旗 铝杆杆别名生成已知 订单文件夹场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Same design both sides",
        POLE_TYPE: "Aluminum-Pole",
        CROSS_BASE: "Yes, I need an Iron Pipe Cross Base.",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("702-9402546-2859420"),
        order_lines=[_line(asin="B0DS22CWH8", quantity=2, pairs=pairs)],
        recipient_name="Dillion Clarke",
        payment_time="2026-06-25 08:55:20",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert (
        result.folder_name
        == "702-9402546-2859420+2套（0.65x1.7m双面方形旗帜+相同设计+铝纤维杆+铁管十字底座）+Dillion Clarke"
    )


def test_feather_flag_all_accessories_and_different_design(tmp_path):
    """验证刀旗文件夹生成中的刀旗 全部配件并不同设计场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Different designs",
        POLE_TYPE: "All-fiberglass Pole",
        CROSS_BASE: "Yes, I need a Flat Iron Cross Base.",
        GROUND_SPIKE: "Yes, I need a Ground Spike.",
        WATER_BAG: "Yes, I need a Water Bag",
        CARRYING_BAG: "Yes, I need a Carrying Bag",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-0994002-7975462"),
        order_lines=[_line(asin="B0DS21PFM7", pairs=pairs)],
        recipient_name="Joe Demascal",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert (
        result.folder_name
        == "112-0994002-7975462+1套(0.6x2.5m双面刀旗+不同设计+全玻璃纤维杆+扁铁十字底座+手提袋+水袋+地钉)+Joe Demascal"
    )


def test_feather_flag_teardrop_child_asin_uses_teardrop_product_name(tmp_path):
    """验证刀旗文件夹生成中的刀旗 水滴旗子ASIN使用水滴旗产品名称场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Same design both sides",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("111-0000000-0000000"),
        order_lines=[_line(asin="B0DS21LCF1", pairs=pairs)],
        recipient_name="Waterdrop Buyer",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "111-0000000-0000000+1套(0.95x2.3m双面水滴旗+相同设计)+Waterdrop Buyer"


def test_feather_flag_all_fiberglass_pole_hyphen_alias(tmp_path):
    """验证刀旗文件夹生成中的刀旗 全部玻璃纤维杆杆连字符别名场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Same design both sides",
        POLE_TYPE: "All-Fiberglass-Pole",
        CROSS_BASE: "Yes, I need a Flat Iron Cross Base.",
        WATER_BAG: "Yes, I need a Water Bag",
        CARRYING_BAG: "Yes, I need a Carrying Bag",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-2462109-8073004"),
        order_lines=[_line(asin="B0DS23HZLC", pairs=pairs)],
        recipient_name="Allison Remy",
        payment_time="2026-07-01 00:10:01",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert "全玻璃纤维杆" in "+".join(result.folder_components)


def test_feather_flag_and_table_runner_order_with_all_fiberglass_pole_builds_folder(tmp_path):
    """验证刀旗文件夹生成中的刀旗 并 桌旗 订单带有全部玻璃纤维杆杆生成文件夹场景。"""
    flag_pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "2-Sided Printing: Same design both sides",
        POLE_TYPE: "All-Fiberglass-Pole",
        CROSS_BASE: "Yes, I need a Flat Iron Cross Base.",
        WATER_BAG: "Yes, I need a Water Bag",
        CARRYING_BAG: "Yes, I need a Carrying Bag",
        PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
    }
    table_runner_line = OrderFolderLine(
        asin="B0DL6GL3D3",
        sku="custom-table-runner-24x72in",
        parent_asin="B0DL61S1C9",
        product_type=PRODUCT_TYPE_TABLE_RUNNERS,
        quantity=1,
        customization_text="",
        customization_pairs={
            "Choose Your Material for the Table Runner": "150GSM Poly Fabric, Light & Versatile",
            "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)": (
                "Online Proof (48h No Reply=SHIP)"
            ),
        },
        order_item_id="163808258275521",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-2462109-8073004"),
        order_lines=[
            _line(asin="B0DS23HZLC", pairs=flag_pairs),
            table_runner_line,
        ],
        recipient_name="Allison Remy",
        payment_time="2026-07-01 00:10:01",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert result.folder_name == (
        "112-2462109-8073004+1套(0.65x2.4m双面方形旗帜+相同设计+全玻璃纤维杆+"
        "扁铁十字底座+手提袋+水袋)+1个(24x72in桌旗+150g经编布)+Allison Remy+在线检查"
    )


def test_feather_flag_same_customization_lines_keep_order_lines(tmp_path):
    """验证刀旗文件夹生成中的刀旗 相同 定制化 行保留订单行场景。"""
    pairs = {
        **_base_pairs(),
        PRINTING_SIDE: "Single-Sided Printing",
        POLE_TYPE: "Aluminum-fiberglass Pole",
    }

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-2222222-2222222"),
        order_lines=[_line(pairs=pairs), _line(pairs=pairs)],
        recipient_name="Flag Buyer",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == (
        "112-2222222-2222222+1套(0.5x2m单面刀旗+铝纤维杆)+"
        "1套(0.5x2m单面刀旗+铝纤维杆)+Flag Buyer"
    )


def test_feather_flag_unknown_printing_side_returns_product_status(tmp_path):
    """验证刀旗文件夹生成中的刀旗 未知打印面返回产品状态场景。"""
    pairs = {**_base_pairs(), PRINTING_SIDE: "Mirror Printed"}

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-3333333-3333333"),
        order_lines=[_line(pairs=pairs)],
        recipient_name="Flag Buyer",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "feather_flags_rule_missing"
    assert result.missing_rule_title == PRINTING_SIDE
    assert result.missing_rule_value == "Mirror Printed"


def test_feather_flag_missing_printing_side_returns_rule_missing_with_line(tmp_path):
    """验证刀旗文件夹生成中的刀旗 缺失打印面返回规则缺失带有行场景。"""
    pairs = _base_pairs()
    pairs.pop(PRINTING_SIDE)

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-3333333-3333334"),
        order_lines=[_line(pairs=pairs)],
        recipient_name="Flag Buyer",
        payment_time="2026-06-23 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "feather_flags_rule_missing_printing_side"
    assert result.missing_rule_title == PRINTING_SIDE
    assert result.missing_rule_value == "missing"
    assert result.missing_rule_line == "1.Printing Side = missing"


def test_feather_flag_contact_prompt_uses_json_value():
    """验证刀旗文件夹生成中的刀旗 联系方式提示 使用JSON值场景。"""
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0DS22NHGT",
        title=None,
        quantity=1,
        pairs={FEATHER_FLAG_CONTACT_PROMPT: "Call 555-111-2222 or email flag@example.com"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "5551112222"
    assert contact.email == "flag@example.com"
