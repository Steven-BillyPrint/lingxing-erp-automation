from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.vinyl_banners import (
    PRODUCT_TYPE_VINYL_BANNERS,
    VINYL_BANNER_CONTACT_PROMPT,
    VINYL_BANNER_PARENT_TO_CHILD_SIZE,
    find_vinyl_banner_parent_asin,
    get_vinyl_banner_size,
    is_vinyl_banner_asin,
)
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines
from lingxing_automation.services.order_line_matcher import build_order_folder_lines_from_json


def _order(platform_order_no: str = "112-9190230-3413048") -> BatchOrderItem:
    """构造喷绘横幅文件夹生成测试所需的订单对象。"""
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-12 10:00:00",
    )


def _line(
    *,
    asin: str,
    quantity: int,
    pairs: dict[str, str],
    order_item_id: str = "item-1",
) -> OrderFolderLine:
    """构造喷绘横幅文件夹生成测试所需的订单行对象。"""
    return OrderFolderLine(
        asin=asin,
        sku="banner-sku",
        parent_asin=find_vinyl_banner_parent_asin(asin),
        product_type=PRODUCT_TYPE_VINYL_BANNERS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs,
        order_item_id=order_item_id,
    )


def _folder_name(platform_order_no: str, lines: list[OrderFolderLine], customer_name: str, tmp_path) -> str:
    """生成喷绘横幅文件夹生成测试断言使用的文件夹名。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order(platform_order_no),
        order_lines=lines,
        recipient_name=customer_name,
        payment_time="2026-06-12 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert result.status == "folder_preview"
    return result.folder_name or ""


def test_vinyl_banner_parent_child_mapping_matches_verified_dict():
    """验证喷绘横幅文件夹生成中的喷绘横幅 父子映射匹配 verified dict场景。"""
    verified_parent_to_children = {
        "B0CMTSMLJT": [
            "B0CR2SLGHR", "B0CR2ZTTNN", "B0CR2W965D", "B0CMQK16Q2", "B0CR2TLS7W", "B0CR2SLSJY",
            "B0CR31PFLR", "B0CR2XN6BG", "B0CR35R6JT", "B0CR34YND5", "B0CR2YXMFM", "B0CR2TM3WC",
            "B0CR328ZR8", "B0CR36B2HG", "B0CR2Z1H7G", "B0CR37TQ68", "B0CR337FMZ", "B0CR37P6JQ",
            "B0CR37GQ8Q", "B0CR2SH9KX", "B0CR3382BJ", "B0CR326T9C", "B0CR383W6C", "B0CR2W75M9",
            "B0CR358N7V", "B0CR2WP2HN", "B0CR2RG8FQ", "B0CR3197NL", "B0CR2Y8MYN", "B0CR38YSLP",
            "B0CR36S1YJ", "B0CR2XPH5B", "B0CR2XLR94", "B0CMQJ9S4N", "B0CMQHGRQV", "B0CMQGWKY7",
            "B0CMQGKFY1", "B0CMQGCZL1", "B0CMQJK1B8", "B0CMQHDPPP", "B0CMQG9W4N", "B0CMQFY1M1",
            "B0CMQDQ47S", "B0CMQG73C1", "B0CMQF5S38", "B0CMQHWC68", "B0CMQHPGSY", "B0CMQHR8KC",
            "B0CMQG7JKS", "B0CMQF85X7", "B0CMQDFQVB", "B0CMQJ3GJC", "B0CMQGPS4G", "B0CMQHSVV3",
            "B0CMQG7MKX", "B0CMQHQFW4", "B0CMQFYGFK", "B0CMQJ99D4", "B0CMQGXLZN", "B0CMQFWQM2",
            "B0CMQGLN9N", "B0CMQK4KBF", "B0CMQHXR1F",
        ],
        "B0CMTVM5HS": [
            "B0DQJW6C1T", "B0DNTWF7YP", "B0DHVC3ZJS", "B0DHVDV9BD", "B0DHVCYKRB", "B0DHVD4HVZ",
            "B0DHV9C391", "B0DHV7828Y", "B0DH2VYFQ5", "B0CXD63XBS", "B0CWLC2TH6", "B0CWL9NZRK",
            "B0CWL99XJS", "B0CWLCMPDZ", "B0CWL7824Z", "B0CR2RCT9Q", "B0CR318DBT", "B0CMQKD7KD",
            "B0CMQD3PH7", "B0CMQFV3TR", "B0CMQHXD62", "B0CMQFZFZQ", "B0CMQK5NPN", "B0CMQHKD5Y",
            "B0CMQJ1PJP", "B0CMQDVH2S", "B0CMQJXLTM", "B0CMQK2MFT", "B0CMQH9R5Q", "B0CMQJS213",
            "B0CMQFKYZZ", "B0CMQGYBSZ", "B0CMQDF62Q", "B0CMQJRCS8", "B0CMQJJ129", "B0CMQH2MW4",
            "B0CMQJK55Y", "B0CMQGXH29", "B0CMQGQNKK", "B0CMQDR4B4", "B0CMQHYDQB", "B0CMQFL48P",
            "B0CMQK44YP", "B0CMQHMQ2T", "B0CMQGD8X5", "B0CMQJKNH1", "B0CMQGCBTW", "B0CMQHDMFB",
            "B0CMQGQCK4", "B0CMQHC2Y9", "B0CMQFXVV6", "B0CMQHSJB5",
        ],
        "B0CMTT81C2": [
            "B0CMQFXVV8", "B0CR318PBN", "B0CMQHKHHM", "B0CMQFGSZC", "B0CMQG89Q9", "B0CMQFYRSF",
            "B0CMQF9MJ6", "B0CMQH18YW", "B0CMQHMYXR", "B0CMQKP2N4", "B0CMQJXLPF", "B0CMQGXS51",
            "B0CMQJ2DWZ", "B0CMQHZ86T", "B0CMQKTP18", "B0CMQGCNM4", "B0CMQHB7FR", "B0CMQHMPWS",
            "B0CMQGZ9H1", "B0CMQGGCF9", "B0CMQHFSW7", "B0CMQDMR41", "B0CMQHQT9H", "B0CMQF85XH",
            "B0CMQHRLPJ", "B0CMQFKNJG", "B0CMQGK23M", "B0CMQJSDDS", "B0CMQFWZRJ", "B0CMQFRTWQ",
            "B0CMQHMY7R", "B0CMQHG17N", "B0CMQFMM2X", "B0CMQGH154", "B0CMQHDHB6", "B0CMQF85X1",
            "B0CMQJPC1L", "B0CMQKPSF3", "B0CMQFSW41", "B0CMQF37KZ", "B0CMQJYZJY", "B0CMQJ5TSY",
            "B0CMQHBKCH", "B0CMQJVZBS", "B0CMQH5H9R", "B0CMQDDM5H",
        ],
        "B0CX56LVTB": [
            "B0CXDPL9NS", "B0CXDD435L", "B0CXDLX4VP", "B0CXDS8SXT", "B0CXDMWH9T", "B0CX5634K3",
            "B0CX57JDN5", "B0CX4XN2ZR", "B0CX4W1F5J", "B0CX54SFVB",
        ],
    }

    assert {parent: list(children) for parent, children in VINYL_BANNER_PARENT_TO_CHILD_SIZE.items()} == verified_parent_to_children


def test_vinyl_banner_catalog_and_size_mapping():
    """验证喷绘横幅文件夹生成中的喷绘横幅 目录并尺寸映射场景。"""
    assert is_vinyl_banner_asin("B0CMQHG17N")
    assert find_vinyl_banner_parent_asin("B0CMQHG17N") == "B0CMTT81C2"
    assert get_vinyl_banner_size("B0CMQHG17N") == "3x3ft"
    assert get_vinyl_banner_size("B0CMQJSDDS") == "3x12ft"
    assert get_vinyl_banner_size("B0CMQFGSZC") == "3x10ft"
    assert find_vinyl_banner_parent_asin("B0CX54SFVB") == "B0CX56LVTB"
    assert get_vinyl_banner_size("B0CXDPL9NS") == "3x25ft"
    assert get_vinyl_banner_size("B0CXDD435L") == "3x12ft"
    assert get_vinyl_banner_size("B0CXDLX4VP") == "3x15ft"
    assert get_vinyl_banner_size("B0CXDS8SXT") == "3x30ft"
    assert get_vinyl_banner_size("B0CXDMWH9T") == "3x20ft"
    assert get_vinyl_banner_size("B0CX5634K3") == "3x9ft"
    assert get_vinyl_banner_size("B0CX57JDN5") == "4x6ft"
    assert get_vinyl_banner_size("B0CX4XN2ZR") == "3x6ft"
    assert get_vinyl_banner_size("B0CX4W1F5J") == "3x4ft"
    assert get_vinyl_banner_size("B0CX54SFVB") == "3x5ft"

    match = match_supported_product("B0CMQHG17N")
    assert match is not None
    assert match.product_type == PRODUCT_TYPE_VINYL_BANNERS


def test_vinyl_banner_example_single_side_550(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 示例单面面 550场景。"""
    line = _line(
        asin="B0CMQHG17N",
        quantity=1,
        pairs={
            "Printed Sides": "Single-Sided",
            "Material Type": "Studry 15 oz. sturdy vinyl",
            "Hanging Option": "No Grommet",
            "Edge Options": "No Edge",
        },
    )

    assert _folder_name("112-9190230-3413048", [line], "Calvin Xiong", tmp_path) == (
        "112-9190230-3413048+1个3x3ft单面喷绘+550+无扣+不折边+Calvin Xiong"
    )


def test_vinyl_banner_example_double_side_roll_packaging(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 示例双面面 roll packaging场景。"""
    line = _line(
        asin="B0CMQJSDDS",
        quantity=1,
        pairs={
            "Printed Sides": "Double-Sided",
            "Hanging Options": "Grommets Every 2 ft",
            "Edge Options": "No Edge",
            "Packaging Methods": "Rolled Packaging",
        },
    )

    assert _folder_name("112-0218406-6861878", [line], "Cioci's Picture Mart", tmp_path) == (
        "112-0218406-6861878+1个3x12ft双面喷绘+每60cm打扣+不折边+卷装+Cioci's Picture Mart"
    )


def test_vinyl_banner_hanging_option_every_2_to_3ft_variant(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 hanging 选项每 2 到 3ft variant场景。"""
    line = _line(
        asin="B0CMQHMQ2T",
        quantity=1,
        pairs={
            "Printed Sides": "Single-Sided",
            "Material Type": "Standard 13 oz. lightweight vinyl",
            "Hanging Option": "Grommets Every 2~3ft",
            "Edge Options": "No Edge",
        },
    )

    assert _folder_name("701-7802019-2322652", [line], "Rick Churilla", tmp_path) == (
        "701-7802019-2322652+1个3x6ft单面喷绘+每60cm打扣+不折边+Rick Churilla"
    )


def test_vinyl_banner_edge_option_welded_edges_variant(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 边缘选项 welded 边缘 variant场景。"""
    line = _line(
        asin="B0CMQHMQ2T",
        quantity=1,
        pairs={
            "Printed Sides": "Single-Sided",
            "Material Type": "Studry 15 oz. sturdy vinyl",
            "Hanging Option": "Grommets Every 2~3ft",
            "Edge Option": "Welded Edges",
        },
    )

    assert _folder_name("702-2358932-9383401", [line], "michelle mitchell", tmp_path) == (
        "702-2358932-9383401+1个3x6ft单面喷绘+550+每60cm打扣+折边胶粘+michelle mitchell"
    )


def test_vinyl_banner_edge_option_sewn_edges_variant(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 边缘选项 sewn 边缘 variant场景。"""
    line = _line(
        asin="B0CMQDVH2S",
        quantity=1,
        pairs={
            "Printed Sides": "Double-Sided",
            "Is The Front Side Using The Same Design As The Back Side?": "Yes, Using Same Design for Back Side",
            "Hanging Options": "Grommets Every 2~3ft",
            "Edge Option": "Sewn Edges",
            "Packaging methods": "Folded Packaging",
        },
    )

    assert _folder_name("113-9355933-6171449", [line], "AMHCC-Dawna Huhman", tmp_path) == (
        "113-9355933-6171449+1个3x4ft双面喷绘+双面相同+每60cm打扣+踩线折边+折叠装+AMHCC-Dawna Huhman"
    )


def test_vinyl_banner_fixed_double_sided_asin_without_printed_sides(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 固定 双面 ASIN不依赖打印面数场景。"""
    line = _line(
        asin="B0CMQJPC1L",
        quantity=1,
        pairs={
            "Is The Front Side Using The Same Design As The Back Side?": "Yes, Using Same Design for Back Side",
            "Hanging Options": "Grommets Every 2~3ft",
            "Edge Options": "No Edge",
            "Packaging Methods": "Folded Packaging",
        },
    )

    assert _folder_name("114-8706887-0057811", [line], "Isem Perdue", tmp_path) == (
        "114-8706887-0057811+1个3x5ft双面喷绘+双面相同+每60cm打扣+不折边+折叠装+Isem Perdue"
    )


def test_vinyl_banner_pluralized_edge_title_and_value_match_existing_rule(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 复数化边缘标题并值匹配已存在规则场景。"""
    line = _line(
        asin="B0CMQDVH2S",
        quantity=1,
        pairs={
            "Printed Sides": "Double-Sided",
            "Is The Front Side Using The Same Design As The Back Side?": "Yes, Using Same Design for Back Side",
            "Hanging Options": "Grommets Every 2~3ft",
            "Edges Options": "Sewn Edges",
            "Packaging methods": "Folded Packaging",
        },
    )

    standard_line = _line(
        asin="B0CMQDVH2S",
        quantity=1,
        pairs={
            "Printed Sides": "Double-Sided",
            "Is The Front Side Using The Same Design As The Back Side?": "Yes, Using Same Design for Back Side",
            "Hanging Options": "Grommets Every 2~3ft",
            "Edge Option": "Sewn Edge",
            "Packaging methods": "Folded Packaging",
        },
    )

    assert _folder_name("702-1512197-2106639", [line], "AMZ Buyer", tmp_path) == _folder_name(
        "702-1512197-2106639",
        [standard_line],
        "AMZ Buyer",
        tmp_path,
    )


def test_vinyl_banner_example_accessory_and_proof_after_name(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 示例配件并确认稿之后名称场景。"""
    line = _line(
        asin="B0CMQFGSZC",
        quantity=1,
        pairs={
            "Printed Sides": "Single-Sided",
            "Hanging Option": "Grommets Every 2 ft",
            "Edge Options": "Welded Edge",
            "Accessories": "Zip Ties (enough for use)",
            "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed).": (
                "Online Proof (48h No Reply=SHIP)"
            ),
        },
    )

    assert _folder_name("701-1660835-9499430", [line], "Nicole Soontiens", tmp_path) == (
        "701-1660835-9499430+1个3x10ft单面喷绘+每60cm打扣+折边胶粘+扎带+Nicole Soontiens+在线检查"
    )


def test_vinyl_banner_missing_and_unknown_printed_sides(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 缺失并未知打印面数场景。"""
    missing = build_and_create_order_folder_from_lines(
        order_item=_order(),
        order_lines=[_line(asin="B0CMQHG17N", quantity=1, pairs={"Edge Options": "No Edge"})],
        recipient_name="Calvin Xiong",
        payment_time="2026-06-12 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert missing.status == "vinyl_banners_rule_missing_printed_sides"
    assert missing.missing_rule_title == "Printed Sides"
    assert missing.missing_rule_value == "missing"
    assert missing.missing_rule_line == "1.Printed Sides = missing"

    unknown = build_and_create_order_folder_from_lines(
        order_item=_order(),
        order_lines=[_line(asin="B0CMQHG17N", quantity=1, pairs={"Printed Sides": "Mystery Side"})],
        recipient_name="Calvin Xiong",
        payment_time="2026-06-12 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert unknown.status == "vinyl_banners_rule_missing"
    assert unknown.missing_rule_title == "Printed Sides"


def test_vinyl_banner_contact_prompt_supports_phone_email_and_empty():
    """验证喷绘横幅文件夹生成中的喷绘横幅 联系方式提示 支持电话邮箱并空值场景。"""
    both = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0CMQHG17N",
        title=None,
        quantity=1,
        pairs={VINYL_BANNER_CONTACT_PROMPT: "555-123-4567 buyer@example.com"},
    )
    contact = extract_contact_candidates_from_json_items([both])[0]
    assert contact.phone == "5551234567"
    assert contact.email == "buyer@example.com"

    email_only = CustomizationJsonInfo(
        order_id="112",
        order_item_id="2",
        asin="B0CMQHG17N",
        title=None,
        quantity=1,
        pairs={VINYL_BANNER_CONTACT_PROMPT: "buyer2@example.com"},
    )
    assert extract_contact_candidates_from_json_items([email_only])[0].email == "buyer2@example.com"

    empty = CustomizationJsonInfo(
        order_id="112",
        order_item_id="3",
        asin="B0CMQHG17N",
        title=None,
        quantity=1,
        pairs={VINYL_BANNER_CONTACT_PROMPT: ""},
    )
    assert extract_contact_candidates_from_json_items([empty]) == []

    unicode_email = CustomizationJsonInfo(
        order_id="112",
        order_item_id="4",
        asin="B0CMQHG17N",
        title=None,
        quantity=1,
        pairs={VINYL_BANNER_CONTACT_PROMPT: "affûtage.letourneau@outlook.com"},
    )
    assert extract_contact_candidates_from_json_items([unicode_email])[0].email == "affûtage.letourneau@outlook.com"

    unicode_both = CustomizationJsonInfo(
        order_id="112",
        order_item_id="5",
        asin="B0CMQHG17N",
        title=None,
        quantity=1,
        pairs={VINYL_BANNER_CONTACT_PROMPT: "8193011336 affûtage.letourneau@outlook.com"},
    )
    contact = extract_contact_candidates_from_json_items([unicode_both])[0]
    assert contact.phone == "8193011336"
    assert contact.email == "affûtage.letourneau@outlook.com"


def test_vinyl_banner_multi_lines_use_order_item_id_and_do_not_merge(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 多行行使用 订单行 ID并 不会 合并场景。"""
    amazon_items = [
        {"OrderItemId": "a", "ASIN": "B0CMQJSDDS", "SellerSKU": "same", "QuantityOrdered": 1},
        {"OrderItemId": "b", "ASIN": "B0CMQJSDDS", "SellerSKU": "same", "QuantityOrdered": 2},
        {"OrderItemId": "c", "ASIN": "B0CMQFGSZC", "SellerSKU": "other", "QuantityOrdered": 3},
    ]
    customization_items = [
        CustomizationJsonInfo(
            order_id="112",
            order_item_id="a",
            asin="B0CMQJSDDS",
            title=None,
            quantity=1,
            pairs={"Printed Sides": "Single-Sided", "Hanging Option": "No Grommet", "Edge Options": "No Edge"},
        ),
        CustomizationJsonInfo(
            order_id="112",
            order_item_id="b",
            asin="B0CMQJSDDS",
            title=None,
            quantity=2,
            pairs={"Printed Sides": "Double-Sided", "Hanging Option": "No Grommet", "Edge Options": "No Edge"},
        ),
        CustomizationJsonInfo(
            order_id="112",
            order_item_id="c",
            asin="B0CMQFGSZC",
            title=None,
            quantity=3,
            pairs={"Printed Sides": "Single-Sided", "Hanging Option": "Grommets Every 2 ft", "Edge Options": "Welded Edge"},
        ),
    ]

    lines, warnings = build_order_folder_lines_from_json(
        amazon_order_items=amazon_items,
        customization_items=customization_items,
    )

    assert warnings == []
    assert [line.order_item_id for line in lines] == ["a", "b", "c"]
    assert _folder_name("112-0000000-0000000", lines, "Banner Buyer", tmp_path) == (
        "112-0000000-0000000+1个3x12ft单面喷绘+无扣+不折边+"
        "2个3x12ft双面喷绘+无扣+不折边+"
        "3个3x10ft单面喷绘+每60cm打扣+折边胶粘+Banner Buyer"
    )


def test_vinyl_banner_identical_lines_keep_order_lines(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 完全相同行保留订单行场景。"""
    lines = [
        _line(
            asin="B0CMQHMQ2T",
            quantity=1,
            pairs={
                "Printed Sides": "Single-Sided",
                "Hanging Option": "Grommets Every 2~3ft",
                "Edge Options": "No Edge",
            },
        ),
        _line(
            asin="B0CMQHMQ2T",
            quantity=2,
            pairs={
                "Printed Sides": "Single-Sided",
                "Hanging Option": "Grommets Every 2~3ft",
                "Edge Options": "No Edge",
            },
        ),
    ]

    assert _folder_name("112-0000000-0000000", lines, "Banner Buyer", tmp_path) == (
        "112-0000000-0000000+1个3x6ft单面喷绘+每60cm打扣+不折边+"
        "2个3x6ft单面喷绘+每60cm打扣+不折边+Banner Buyer"
    )


def test_vinyl_banner_single_line_quantity_does_not_mark_different_designs(tmp_path):
    """验证喷绘横幅文件夹生成中的喷绘横幅 单面行数量 不会 标记不同 designs场景。"""
    lines = [
        _line(
            asin="B0CMQHMQ2T",
            quantity=3,
            pairs={
                "Printed Sides": "Single-Sided",
                "Hanging Option": "Grommets Every 2~3ft",
                "Edge Options": "No Edge",
            },
        ),
    ]

    assert _folder_name("112-0000000-0000000", lines, "Banner Buyer", tmp_path) == (
        "112-0000000-0000000+3个3x6ft单面喷绘+每60cm打扣+不折边+Banner Buyer"
    )
