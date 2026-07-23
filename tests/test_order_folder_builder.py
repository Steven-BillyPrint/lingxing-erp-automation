from __future__ import annotations

from datetime import date
from pathlib import Path

from lingxing_automation.models import BatchOrderItem, ContactInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_complete_contact_candidates
from lingxing_automation.products.car_magnets import (
    get_car_magnet_fixed_size,
    normalize_car_magnet_size_value,
)
from lingxing_automation.products.tents import get_tent_top_size, get_wall_only_asin_kind
from lingxing_automation.services.customization_parser import parse_customization_pairs
from lingxing_automation.services.folder_builder import (
    FOLDER_EXISTING_PLATFORM_ORDER,
    FOLDER_NAME_MAX_UTF8_BYTES,
    build_and_create_order_folder,
    build_and_create_order_folder_from_lines,
    build_daily_folder,
    build_month_folder,
    build_order_folder_components,
    build_order_folder_components_from_lines,
    create_order_folder_from_preview,
    find_existing_platform_order_folder,
    resolve_folder_date,
    sanitize_folder_name,
    shorten_folder_name_by_components,
)


EXAMPLE_CUSTOMIZATION_TEXT = """
Custom Canopy Tent Package Configuration:
Frame Options - Our Frame Recommended for Best Fit：Premium 2"/50mm hexagonal aluminum
Side Wall and Rail Options : 1 Full and 2 Half Walls with Rails
Fabric Material Options : 600D Flame Retardant Polyester Fabric
Double-sided Printing Options : 2-Sided Printing: 1 Full & 2 Half Walls
Roller Bag Options : Add Roller Bag
Rope & Stake Kit Options : Bonus Rope & Stake Kit
Sandbags (4 piece set) : Add Sandbags (4 piece set)
Custom Fitted Table Cloth with Your Logo : 6Ft with Back (260GSM Polyester Fabric)
Custom Feather/Teardrop Flag : 1 Set: 6.9ft 2-Sided Feather+Pole+Base
Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com
Please provide a texting number to confirm customization design and details or for emergencies. : 5097688140
"""

EXPECTED_EXAMPLE_FOLDER_NAME = (
    "111-2789436-8737015+1个3x3m帐篷顶+50mm六角铝+1双面全高背墙+"
    "2双面半高侧墙(带横杆)+600D阻燃面料+拖轮包+沙袋四件套+绳子地钉+1个6FT方套桌布+260g经编布+"
    "1套（0.5x2m双面刀旗+全纤维杆+铁板十字底座3KG+水袋）+Sawako Hiraoka"
)


def _order_item() -> BatchOrderItem:
    """构造订单文件夹生成测试所需的订单对象。"""
    return BatchOrderItem(
        system_order_no="103702039132365313",
        platform_order_no="111-2789436-8737015",
        row_text="",
        paid_at_text="2026-06-04 15:23:10",
        asin="B0DZ2W2QWK",
        parent_asin="B0FTV6XDGG",
    )


def _wall_order_item(asin: str, platform_order_no: str = "114-0131738-0578639") -> BatchOrderItem:
    """构造订单文件夹生成测试所需的侧墙订单对象。"""
    return BatchOrderItem(
        system_order_no="103709321966368505",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-08 07:35:58",
        asin=asin,
        parent_asin="B0D6XW7V9T",
    )


def test_folder_date_uses_payment_time_and_builds_daily_folder(tmp_path):
    """验证订单文件夹生成中的文件夹日期 使用 付款时间 并生成每日文件夹场景。"""
    folder_date = resolve_folder_date("2026-06-04 15:23:10")
    assert folder_date == date(2026, 6, 4)
    assert build_daily_folder(tmp_path, folder_date) == tmp_path / "2026" / "6月" / "0604"
    assert build_month_folder(tmp_path, folder_date) == tmp_path / "2026" / "6月"


def test_folder_date_override_wins_over_payment_time(tmp_path):
    """验证订单文件夹生成中的文件夹日期 覆盖值优先优于 付款时间场景。"""
    folder_date = resolve_folder_date("2026-06-04 15:23:10", "2026-06-05")
    assert folder_date == date(2026, 6, 5)
    assert build_daily_folder(tmp_path, folder_date) == tmp_path / "2026" / "6月" / "0605"


def test_missing_payment_time_returns_status_and_does_not_create(tmp_path):
    """验证订单文件夹生成中的缺失 付款时间 返回状态并 不会 创建场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time=None,
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    assert result.status == "folder_missing_payment_time"
    assert not any(tmp_path.iterdir())


def test_invalid_payment_time_returns_status_and_does_not_fallback_to_today(tmp_path):
    """验证订单文件夹生成中的无效 付款时间 返回状态并 不会 兜底到当天场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="not-a-date",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    assert result.status == "folder_invalid_payment_time"
    assert result.folder_date is None
    assert not any(tmp_path.iterdir())


def test_sanitize_folder_name_keeps_plus_and_replaces_windows_invalid_chars():
    """验证订单文件夹生成中的清洗 文件夹名 保留加号并替换Windows无效字符场景。"""
    assert sanitize_folder_name('A+B<>:"/\\|?* name') == "A+B - name"


def test_parse_customization_pairs_supports_colons_spaces_and_newlines():
    """验证订单文件夹生成中的解析 定制化选项 支持冒号空格并换行场景。"""
    text = """
    Frame Options   ：   Premium 2"/50mm hexagonal aluminum
    Fabric Material Options :
      400D Polyester Fabric
    Custom Feather/Teardrop Flag : 1 Set: 6.9ft 2-Sided Feather+Pole+Base
    Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com
    """
    pairs = parse_customization_pairs(text)
    assert pairs["Frame Options"] == 'Premium 2"/50mm hexagonal aluminum'
    assert pairs["Fabric Material Options"] == "400D Polyester Fabric"
    assert pairs["Custom Feather/Teardrop Flag"] == "1 Set: 6.9ft 2-Sided Feather+Pole+Base"


def test_parse_customization_pairs_treats_notes_as_boundary_not_component():
    """验证订单文件夹生成中的解析 定制化选项 视为备注作为边界不组件场景。"""
    text = """
    Rope & Stake Kit Options : Yes Notes : j'aimerais confirmer le visuel finale de mon design.
    Sandbags (4 piece set) ： No
    Roller Bag Options : Add Roller Bag Notes/Share Link/PDF File : Jet black logo background please
    """
    pairs = parse_customization_pairs(text)

    assert pairs["Rope & Stake Kit Options"] == "Yes"
    assert pairs["Sandbags (4 piece set)"] == "No"
    assert pairs["Roller Bag Options"] == "Add Roller Bag"
    assert "Notes" not in pairs
    assert "Notes/Share" not in pairs
    assert "Notes/Share Link/PDF File" not in pairs


def test_parse_customization_pairs_treats_side_wall_only_double_side_title_as_boundary():
    """验证订单文件夹生成中的解析 定制化选项 视为面侧墙仅双面面标题作为边界场景。"""
    text = (
        "Side Wall and Rail Options : 1 Full and 2 Half Walls without Rails "
        "Double-sided Printing Options(Only Side Wall Options Chosen) : 2-sided Printing: 2 Half Walls"
    )

    pairs = parse_customization_pairs(text)

    assert pairs["Side Wall and Rail Options"] == "1 Full and 2 Half Walls without Rails"
    assert pairs["Double-sided Printing Options"] == "2-sided Printing: 2 Half Walls"


def test_parse_customization_pairs_supports_canopy_frame_size_title():
    """验证订单文件夹生成中的解析 定制化选项 支持帐篷架框架尺寸标题场景。"""
    text = (
        'Fabric Material Options : 400D Polyester Fabric '
        'Select a Standard Size or Provide Custom Canopy Frame Dimensions for a Perfect Fit : '
        'A=91.6", B=12.6", C=118" Fits 10\' Commercial '
        'Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com'
    )

    pairs = parse_customization_pairs(text)

    assert pairs["Fabric Material Options"] == "400D Polyester Fabric"
    assert (
        pairs["Select a Standard Size or Provide Custom Canopy Frame Dimensions for a Perfect Fit"]
        == 'A=91.6", B=12.6", C=118" Fits 10\' Commercial'
    )


def test_parse_customization_pairs_treats_topper_design_fields_as_boundaries():
    """验证订单文件夹生成中的解析 定制化选项 视为顶幅设计字段作为边界场景。"""
    text = """
    Custom Topper Front/Back:
    Canopy Topper Front/Back Color : Black #000000
    Pattern Background : IMG_1577.png
    Your Font 1 : Archivo Black
    Text Color 1 : Gray (#737373)
    Your Text 1 : CLYDESDALES & BLOODHOUNDS
    Other requirements for Top : Exact same colour and design on all 4 sides
    Custom Topper Left/Right:
    Do you want the Topper Left/Right and Front/Back to have the same design and text? : Yes, Please use the same design.
    Canopy Topper Left/Right Color : Black #000000
    Custom Full Wall:
    Full Wall Color : Black #000000
    Frame Options : Standard 1.5"/38mm square aluminum
    Fabric Material Options : 400D Polyester Fabric
    Side Wall and Rail Options : 1 Full Wall
    Custom Table Cloth with Your Logo : 6Ft with Back
    """

    pairs = parse_customization_pairs(text)

    assert pairs["Do you want the Topper Left/Right and Front/Back to have the same design and text?"] == "Yes, Please use the same design."
    assert pairs["Frame Options"] == 'Standard 1.5"/38mm square aluminum'
    assert pairs["Custom Table Cloth with Your Logo"] == "6Ft with Back"


def test_tent_top_size_mapping():
    """验证订单文件夹生成中的帐篷 顶布尺寸映射场景。"""
    assert get_tent_top_size("B0DZ2W2QWK") == "3x3m帐篷顶"
    assert get_tent_top_size("B0D6KZ7G88") is None
    assert get_tent_top_size("B0D6XWP8YN") is None
    assert get_wall_only_asin_kind("B0D6KZ7G88") == "full_wall"
    assert get_wall_only_asin_kind("B0D6XWP8YN") == "half_wall"


def test_car_magnet_size_mapping():
    """验证订单文件夹生成中的汽车磁贴 尺寸映射场景。"""
    assert get_car_magnet_fixed_size("B0CQLN8T6Z") == "12x18in"
    assert get_car_magnet_fixed_size("B0CQLN8T6Y") == "12x18in"


def test_car_magnet_custom_size_keeps_integer_trailing_zero():
    """验证订单文件夹生成中的汽车磁贴 自定义尺寸保留整数尾部零场景。"""
    assert normalize_car_magnet_size_value("20 inches") == "20in"
    assert normalize_car_magnet_size_value("20.0 inches") == "20in"
    assert normalize_car_magnet_size_value("20.50 inches") == "20.5in"


def test_user_example_builds_expected_folder_name_and_payment_date_path(tmp_path):
    """验证订单文件夹生成中的用户示例生成预期 文件夹名 并付款日期路径场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    assert result.status == "folder_created"
    assert result.folder_date == "2026-06-04"
    assert result.folder_date_source == "payment_time"
    assert result.folder_name_full == EXPECTED_EXAMPLE_FOLDER_NAME
    assert result.folder_name_was_shortened is True
    assert "1套（0.5x2m双面刀旗+全纤维杆+铁板十字底座3KG+水袋）" in result.folder_name_removed_components
    assert result.folder_name == "111-2789436-8737015+1个3x3m帐篷顶+50mm六角铝+1双面全高背墙+2双面半高侧墙(带横杆)+600D阻燃面料+拖轮包+沙袋四件套+绳子地钉+1个6FT方套桌布+260g经编布+Sawako Hiraoka"
    assert result.folder_path == str(tmp_path / "2026" / "6月" / "0604" / result.folder_name)


def test_shorten_folder_name_strips_component_edge_plus_and_limits_utf8_bytes():
    """验证订单文件夹生成中的缩短 文件夹名 去除组件边缘加号并边界UTF 8 字节场景。"""
    result = shorten_folder_name_by_components(
        [
            "111-0093341-7131417",
            "1个3x3m帐篷顶",
            "40mm方形铝",
            "1全高背墙",
            "400D面料",
            "拖轮包",
            "沙袋四件套",
            "绳子地钉",
            "1个6FT方套桌布+260g经编布",
            "1个3x6m帐篷顶",
            "40mm方形铝",
            "400D面料",
            "拖轮包",
            "绳子地钉",
            "1个6FT方套桌布+260g经编布",
            "Tory JacksonPOTory - Joey+",
        ],
    )

    assert result.was_shortened is True
    assert result.safe_folder_name.startswith("111-0093341-7131417")
    assert result.safe_folder_name.endswith("Tory JacksonPOTory - Joey")
    assert not result.safe_folder_name.endswith("+")
    assert len(result.safe_folder_name) <= result.max_length
    assert len(result.safe_folder_name.encode("utf-8")) <= FOLDER_NAME_MAX_UTF8_BYTES
    assert result.removed_components


def test_car_magnet_folder_name_uses_parent_quantity_and_options(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 文件夹名 使用父数量并选项场景。"""
    text = """
    Customize Design Left:
    Background Color : Black #000000
    Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc) : Nicholas.snow@nicksgarageapp.com
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
    Proof Option : No reply to the Proof we sent within 48hrs means we will proceed with shipping
    """
    contact = ContactInfo(
        phone=None,
        email="Nicholas.snow@nicksgarageapp.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )

    assert result.status == "folder_created"
    assert result.folder_name == "113-9484835-1608220+2个12x18in汽车磁贴+圆角+1mm+Edith Reynoso"


def test_car_magnet_proof_option_appends_after_recipient(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 确认稿选项追加之后收件人场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
    Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping : Straight To Production
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_components[-2:] == ["Edith Reynoso", "直接制作"]
    assert result.folder_name.endswith("+Edith Reynoso+直接制作")


def test_car_magnet_online_proof_option_appends_after_recipient(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 在线确认稿确认稿选项追加之后收件人场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
    Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping : Online Proof (48h No Reply=SHIP)
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_components[-2:] == ["Edith Reynoso", "在线检查"]
    assert result.folder_name.endswith("+Edith Reynoso+在线检查")


def test_car_magnet_same_design_for_parent_group_inserts_after_product_name():
    """验证订单文件夹生成中的汽车磁贴 相同设计用于父分组插入之后产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="112-4977581-8175462",
        parent_asin="B0CNVT6L7Y",
        asin="B0DRCWXR7S",
        tent_quantity=1,
        customization_text="""
        Surface Material Option : Standard Vinyl
        Corner : Rounded
        Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
        Is The Right Side Using The Same Design As The Left Side? : Yes, Using Same Design for Right Side
        """,
        recipient_name="Henry",
    )

    assert components[1:4] == [
        "2个16x23in汽车磁贴",
        "相同设计",
        "圆角",
    ]


def test_car_magnet_different_design_for_parent_group_inserts_after_product_name():
    """验证订单文件夹生成中的汽车磁贴 不同设计用于父分组插入之后产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="111-2312968-0681040",
        parent_asin="B0CNVT6L7Y",
        asin="B0CNVMQJFX",
        tent_quantity=1,
        customization_text="""
        Surface Material Option : Standard Vinyl
        Corner : Rounded
        Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
        Is The Right Side Using The Same Design As The Left Side? : No,Using Different Design for Right Side
        """,
        recipient_name="Richard K Brumley",
    )

    assert components[1:4] == [
        "2个10x20in汽车磁贴",
        "不同设计",
        "圆角",
    ]


def test_single_car_magnet_order_line_keeps_options_unwrapped(tmp_path):
    """验证单个汽车磁贴商品行不把品名和定制选项放进括号。"""

    line = OrderFolderLine(
        asin="B0CNVMQJFX",
        sku="BillyPrint-Car Magnet-10x20",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
        quantity=2,
        customization_text="",
        customization_pairs={
            "Corner": "Rounded",
            "Choose Your Magnet Thickness": "Heavy Strength 40mil/1mm Magnetic",
        },
        order_item_id="single-car-magnet-line",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem(
            "103719999999999999",
            "114-2858264-9869866",
            "",
            paid_at_text="2026-07-07 10:20:30",
        ),
        order_lines=[line],
        recipient_name="Austin Fleming",
        payment_time="2026-07-07 10:20:30",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "114-2858264-9869866+4个10x20in汽车磁贴+圆角+1mm+Austin Fleming"


def test_car_magnet_same_design_title_is_ignored_for_other_parent_group():
    """验证订单文件夹生成中的汽车磁贴 相同设计标题为忽略用于其他父分组场景。"""
    components = build_order_folder_components(
        platform_order_no="111-2312968-0681040",
        parent_asin="B0CNVSJWB2",
        asin="B0DRCY4HM5",
        tent_quantity=1,
        customization_text="""
        Surface Material Option : Standard Vinyl
        Corner : Rounded
        Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
        Is The Right Side Using The Same Design As The Left Side? : Yes, Using Same Design for Right Side
        """,
        recipient_name="Richard K Brumley",
    )

    assert "相同设计" not in components
    assert "不同设计" not in components


def test_car_magnet_screenshot_asins_use_two_pack_quantity_for_x1():
    """验证订单文件夹生成中的汽车磁贴 截图ASIN使用两包数量用于 x 1场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
    """

    first = build_order_folder_components(
        platform_order_no="112-4977581-8175462",
        parent_asin="B0CNVT6L7Y",
        asin="B0DRCWXR7S",
        tent_quantity=1,
        customization_text=text,
        recipient_name="Henry",
    )
    second = build_order_folder_components(
        platform_order_no="111-2312968-0681040",
        parent_asin="B0CNVT6L7Y",
        asin="B0CNVMQJFX",
        tent_quantity=1,
        customization_text=text,
        recipient_name="Richard K Brumley",
    )

    assert "2个16x23in汽车磁贴" in first
    assert "2个10x20in汽车磁贴" in second


def test_car_magnet_folder_can_build_without_contact_prompt(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 文件夹可以生成不依赖 联系方式提示场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Reflective Vinyl
    Corner : Square
    Choose Your Magnet Thickness : Standard Strength 20mil/0.5mm Magnetic
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-5984177-0877052",
        row_text="",
        paid_at_text="2026-06-10 11:37:03",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Erislandy Guerra",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "113-5984177-0877052+2个12x18in汽车磁贴+反光膜+0.5mm+Erislandy Guerra"


def test_car_magnet_special_shape_ratio_converts_size():
    """验证订单文件夹生成中的汽车磁贴 特殊结构比例转换尺寸场景。"""
    components = build_order_folder_components(
        platform_order_no="111-2222222-3333333",
        parent_asin="B0CRKSZ5TB",
        asin="B0CRKYV7C9",
        tent_quantity=1,
        customization_text="""
        Car Magnet Size : 8 inches
        Shapes / Die Cut : Rectangle (Length:Width=2:1)
        Surface Material Option : Standard Vinyl
        Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
        """,
        recipient_name="Buyer Name",
    )

    assert components == [
        "111-2222222-3333333",
        "1个8x4in方形汽车磁贴",
        "1mm",
        "Buyer Name",
    ]


def test_car_magnet_special_shape_round_corner_adds_corner_component():
    """验证订单文件夹生成中的汽车磁贴 特殊结构圆角圆角添加圆角组件场景。"""
    components = build_order_folder_components(
        platform_order_no="111-2222222-3333333",
        parent_asin="B0CRKSZ5TB",
        asin="B0CRKYV7C9",
        tent_quantity=1,
        customization_text="""
        Car Magnet Size : 8 inches
        Shapes / Die Cut : Square Rectangle Round Corners
        Surface Material Option : Standard Vinyl
        Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
        """,
        recipient_name="Buyer Name",
    )

    assert components == [
        "111-2222222-3333333",
        "1个8in方形汽车磁贴",
        "圆角",
        "1mm",
        "Buyer Name",
    ]


def test_car_magnet_special_shape_keeps_20_inch_size():
    """验证订单文件夹生成中的汽车磁贴 特殊结构保留 20 英寸尺寸场景。"""
    components = build_order_folder_components(
        platform_order_no="701-8072403-3881869",
        parent_asin="B0CRKSZ5TB",
        asin="B0CRKYV7C9",
        tent_quantity=2,
        customization_text="""
        Car Magnet Size : 20 inches
        Shapes / Die Cut : Square Rectangle Round Corners
        Surface Material Option : Standard Vinyl
        Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
        """,
        recipient_name="Brenda Valerio",
    )

    assert components == [
        "701-8072403-3881869",
        "2个20in方形汽车磁贴",
        "圆角",
        "1mm",
        "Brenda Valerio",
    ]


def test_car_magnet_special_shape_supports_proof_option():
    """验证订单文件夹生成中的汽车磁贴 特殊结构支持确认稿选项场景。"""
    components = build_order_folder_components(
        platform_order_no="701-8072403-3881869",
        parent_asin="B0CRKSZ5TB",
        asin="B0CRKYV7C9",
        tent_quantity=1,
        customization_text="""
        Car Magnet Size : 20 inches
        Shapes / Die Cut : Square Rectangle Round Corners
        Surface Material Option : Standard Vinyl
        Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
        Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping : Online Proof (48h No Reply=SHIP)
        """,
        recipient_name="Brenda Valerio",
    )

    assert components[-2:] == ["Brenda Valerio", "在线检查"]


def test_double_sided_printing_only_modifies_wall_components():
    """验证订单文件夹生成中的双面 打印仅调整侧墙组件场景。"""
    components = build_order_folder_components(
        platform_order_no="111-2789436-8737015",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text=EXAMPLE_CUSTOMIZATION_TEXT,
        recipient_name="Sawako Hiraoka",
    )
    assert "2-Sided Printing: 1 Full & 2 Half Walls" not in components
    assert "1双面全高背墙" in components
    assert "2双面半高侧墙(带横杆)" in components


def test_teardrop_flag_6_9ft_uses_waterdrop_size():
    """验证订单文件夹生成中的水滴旗旗帜 6 9ft 使用水滴尺寸场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text="""
        Custom Feather/Teardrop Flag : 1 Set: 6.9ft 2-Sided Teardrop+Pole+Holder
        """,
        recipient_name="Flag Buyer",
    )

    assert "1套（0.75x1.65m双面水滴旗+全纤维杆+连接件+夹具）" in components


def test_teardrop_flag_9_8ft_uses_waterdrop_size():
    """验证订单文件夹生成中的水滴旗旗帜 9 8ft 使用水滴尺寸场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text="""
        Custom Feather/Teardrop Flag : 2 Set: 9.8ft 2-Sided Teardrop+Pole+Base
        """,
        recipient_name="Flag Buyer",
    )

    assert "2套（0.95x2.3m双面水滴旗+全纤维杆+铁板十字底座3KG+水袋）" in components


def test_multi_quantity_tent_package_wraps_configuration_before_recipient():
    """验证订单文件夹生成中的多行数量 帐篷 套餐包装配置之前收件人场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=2,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Side Wall and Rail Options : 1 Full and 2 Half Walls with Rails
        Fabric Material Options : 400D Polyester Fabric
        Roller Bag Options : Add Roller Bag
        Sandbags (4 piece set) : Add Sandbags (4 piece set)
        """,
        recipient_name="Kirsten Force",
    )

    assert components == [
        "112-3183165-4090602",
        "2套（3x3m帐篷顶+40mm方形铝+1全高背墙+2半高侧墙(带横杆)+400D面料+拖轮包+沙袋四件套）",
        "Kirsten Force",
    ]
    assert "2个3x3m帐篷顶" not in components


def test_sandbags_six_piece_set_yes_generates_component():
    """验证订单文件夹生成中的沙袋六件套设置是生成组件场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : No Wall
    Fabric Material Options : 400D Polyester Fabric
    Sandbags (6 piece set) : Yes
    """

    pairs = parse_customization_pairs(text)
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text=text,
        recipient_name="Kirsten Force",
    )

    assert pairs["Sandbags (6 piece set)"] == "Yes"
    assert "沙袋六件套" in components


def test_sandbags_six_piece_set_add_generates_component():
    """验证订单文件夹生成中的沙袋六件套 Add 文案生成组件场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : No Wall
    Fabric Material Options : 400D Polyester Fabric
    Sandbags (6 piece set) : Add Sandbags (6 piece set)
    """

    components = build_order_folder_components(
        platform_order_no="113-7978998-3154600",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text=text,
        recipient_name="Kirsten Force",
    )

    assert "沙袋六件套" in components


def test_sandbags_six_piece_set_no_does_not_generate_component():
    """验证订单文件夹生成中的沙袋六件套设置无 不会 生成组件场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Side Wall and Rail Options : No Wall
        Fabric Material Options : 400D Polyester Fabric
        Sandbags (6 piece set) : No
        """,
        recipient_name="Kirsten Force",
    )

    assert "沙袋六件套" not in components


def test_tent_same_design_inserts_after_tent_product_name():
    """验证订单文件夹生成中的帐篷 相同设计插入之后 帐篷 产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text="""
        Do you want the Topper Left/Right and Front/Back to have the same design and text? : Yes, please use the same design.
        Frame Options : Standard 1.6"/40mm square aluminum
        Side Wall and Rail Options : No Wall
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
    )

    assert components[:4] == [
        "112-3183165-4090602",
        "1个3x3m帐篷顶",
        "相同设计",
        "40mm方形铝",
    ]


def test_multi_quantity_tent_package_wraps_different_design_after_product_name():
    """验证订单文件夹生成中的多行数量 帐篷 套餐包装不同设计之后产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=2,
        customization_text="""
        Do you want the Topper Left/Right and Front/Back to have the same design and text? : No, I would like different designs.
        Frame Options : Standard 1.6"/40mm square aluminum
        Side Wall and Rail Options : No Wall
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
    )

    assert components[1].startswith("2套（3x3m帐篷顶+不同设计+40mm方形铝")
    assert components[-1] == "Kirsten Force"


def test_frame_compatibility_alert_title_generates_frame_component():
    """验证订单文件夹生成中的框架兼容性警告标题生成框架组件场景。"""
    components = build_order_folder_components(
        platform_order_no="701-2292402-2697828",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text="""
        Frame Options - Compatibility Alert for Frame : Standard 1.6"/40mm square aluminum
        Side Wall and Rail Options : No Wall
        Fabric Material Options : 400D Polyester Fabric
        Roller Bag Options : Add Roller Bag
        """,
        recipient_name="Ryan Ellis",
        logistics="Expedited",
    )

    assert components == [
        "加急701-2292402-2697828",
        "1个3x3m帐篷顶",
        "40mm方形铝",
        "400D面料",
        "拖轮包",
        "Ryan Ellis",
    ]


def test_short_frame_compatibility_alert_add_value_generates_frame_component():
    """验证截图里的短框架兼容性标题和 Add 值会生成框架组件。"""
    components = build_order_folder_components(
        platform_order_no="702-3915493-5328228",
        parent_asin="B0FTV6XDGG",
        asin="B0D7DMK75P",
        tent_quantity=1,
        customization_text='''
        Do you want the Topper Left/Right and Front/Back to have the same design and text? : Yes, Please use the same design.
        Compatibility Alert for Frame : Add Standard 1.6"/40mm square alum frame
        Fabric Material Options : 400D Polyester Fabric
        ''',
        recipient_name="Construction SM Paquet",
        logistics="Expedited",
    )

    assert components == [
        "加急702-3915493-5328228",
        "1个3x3m帐篷顶",
        "相同设计",
        "40mm方形铝",
        "400D面料",
        "Construction SM Paquet",
    ]


def test_short_frame_compatibility_alert_add_hex_values_generate_frame_components():
    """验证短框架兼容性标题下的 Add 六角铝支架值会生成框架组件。"""
    cases = [
        ('Add Commercial 1.6"/40mm hex alum frame', "40mm六角铝"),
        ('Add Premium 2"/50mm hex alum frame', "50mm六角铝"),
    ]

    for value, expected_component in cases:
        components = build_order_folder_components(
            platform_order_no="702-3915493-5328228",
            parent_asin="B0FTV6XDGG",
            asin="B0D7DMK75P",
            tent_quantity=1,
            customization_text=f"""
            Compatibility Alert for Frame : {value}
            Fabric Material Options : 400D Polyester Fabric
            """,
            recipient_name="Construction SM Paquet",
            logistics="Expedited",
        )

        assert expected_component in components


def test_side_wall_only_double_side_title_only_modifies_half_wall(tmp_path):
    """验证订单文件夹生成中的面侧墙仅双面面标题仅调整半高侧墙场景。"""
    text = """
    Customization Confirmation:
    Frame Options : Standard 1.5"/38mm square aluminum
    Fabric Material Options : 400D Polyester Fabric
    Side Wall and Rail Options : 1 Full and 2 Half Walls without Rails
    Double-sided Printing Options(Only Side Wall Options Chosen) : 2-sided Printing: 2 Half Walls
    Roller Bag Options : Add Roller Bag
    Please provide a texting number to confirm customization design and details or for emergencies. : 9736186934
    Please provide an email address to confirm customization design and details or for emergencies. : dellemonache49@gmail.com
    """
    contact = ContactInfo(
        phone="9736186934",
        email="dellemonache49@gmail.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    order_item = BatchOrderItem(
        system_order_no="103709545124988424",
        platform_order_no="113-0987796-6853040",
        row_text="",
        paid_at_text="2026-06-08 23:36:18",
        asin="B0D5134SJ3",
        parent_asin="B0CZNZVG26",
    )

    result = build_and_create_order_folder(
        order_item=order_item,
        contact_info=contact,
        recipient_name="Roseanne DelleMonache",
        payment_time="2026-06-08 23:36:18",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert result.customization_pairs["Side Wall and Rail Options"] == "1 Full and 2 Half Walls without Rails"
    assert result.customization_pairs["Double-sided Printing Options"] == "2-sided Printing: 2 Half Walls"
    assert "1全高背墙" in result.folder_components
    assert "1双面全高背墙" not in result.folder_components
    assert "2双面半高侧墙" in result.folder_components
    assert "拖轮包" in result.folder_components
    assert not any(tmp_path.iterdir())


def test_full_wall_double_sided_count_splits_remaining_single_walls(tmp_path):
    """验证订单文件夹生成中的全高侧墙 双面 数量拆分剩余单面侧墙场景。"""
    text = """
    Custom Canopy Tent Package Configuration:
    Frame Options - Our Frame Recommended for Best Fit : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : 3 Full and 1 Half Wall without Rail
    Fabric Material Options : 600D Flame Retardant Polyester Fabric
    Double-sided Printing Options : 2-sided Printing: 1 Full Wall
    Roller Bag Options : Add Roller Bag
    Sandbags (4 piece set) : Add Sandbags (4 piece set)
    Please provide an email address to confirm customization design and details or for emergencies. : TiffanyKimlienTran@gmail.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 8088664700
    Customize Canopy Top (Front & Back):
    Canopy Top Color : Grass Green #126435
    Pattern Backgroud : IMG_5199.jpeg
    Your Logo or Image 1 : IMG_5199.jpeg
    Your Font : Sigmar One
    Text Color : Green (#00bf63)
    Your Text 1 - Line 1 : Lahaina Rising Phoenix Massage
    Your Text 1 - Line 2 : (808) 866-4700
    Customization Notes for Canopy Top - Line 1 : Please print in green color the name and phone # of my business on the Canopy Top:
    Customization Notes for Canopy Top - Line 2 : “Lahaina Rising Phoenix Massage
    Customization Notes for Canopy Top - Line 3 : (808) 866-4700”
    Do you want the Topper Left/Right and Front/Back to have the same design and text? : Yes, please use the same design.
    Customize Full Wall:
    Full Wall Background Color : Pale Green #b6d070
    Background Pattern : IMG_5199.jpeg
    Your Font : Archivo Black
    Text Color : Green (#00bf63)
    Your Text 1 - Line 2 : Lahaina Rising Phoenix Massage
    Your Text 1 - Line 3 : (808) 866-4700
    Customize Half Wall:
    Half Wall Background Color : Grass Green #126435
    Pattern Backgroud : IMG_5199.jpeg
    Your Font : Libre Baskerville
    Text Color : Green (#00bf63)
    Your Text 1 - Line 2 : Lahaina Rising Phoenix Massage
    Your Text 1 - Line 3 : (808) 866-4700
    """
    contact = ContactInfo(
        phone="8088664700",
        email="TiffanyKimlienTran@gmail.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    order_item = BatchOrderItem(
        system_order_no="103714418881523712",
        platform_order_no="113-0774847-3419420",
        row_text="",
        paid_at_text="2026-06-23 13:31:32",
        asin="B0DZ2W2QWK",
        parent_asin="B0FTV6XDGG",
    )

    result = build_and_create_order_folder(
        order_item=order_item,
        contact_info=contact,
        recipient_name="Kimlien Tiffany Tran",
        payment_time="2026-06-23 13:31:32",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == (
        "113-0774847-3419420+1个3x3m帐篷顶+相同设计+40mm方形铝+1双面全高背墙+"
        "2全高背墙+1半高侧墙+600D阻燃面料+拖轮包+沙袋四件套+Kimlien Tiffany Tran"
    )
    assert "相同设计" in result.folder_components
    assert "1双面全高背墙" in result.folder_components
    assert "2全高背墙" in result.folder_components
    assert "3双面全高背墙" not in "+".join(result.folder_components)


def test_no_and_none_options_do_not_generate_components_or_errors():
    """验证订单文件夹生成中的无并空值选项 不会 生成组件或 errors场景。"""
    text = """
    Side Wall and Rail Options : No Wall
    Roller Bag Options : No Roller Bag
    Rope & Stake Kit Options : None
    Sandbags (4 piece set) : No Sandbags (4 piece set)
    """
    components = build_order_folder_components(
        platform_order_no="111-0000000-0000000",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=1,
        customization_text=text,
        recipient_name="Buyer Name",
    )
    assert components == ["111-0000000-0000000", "1个3x3m帐篷顶", "Buyer Name"]


def test_notes_after_rope_yes_does_not_cause_rule_missing(tmp_path):
    """验证订单文件夹生成中的备注之后绳子是 不会 导致规则缺失场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : No Wall
    Fabric Material Options : 400D Polyester Fabric
    Rope & Stake Kit Options : Yes Notes : j'aimerais confirmer le visuel finale de mon design et que la qualité des mes images sont ok.
    """
    contact = ContactInfo(
        phone="8193602187",
        email=None,
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Brian Willett",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.customization_pairs["Rope & Stake Kit Options"] == "Yes"
    assert result.missing_rule_title is None
    assert result.folder_components == [
        "111-2789436-8737015",
        "1个3x3m帐篷顶",
        "40mm方形铝",
        "400D面料",
        "绳子地钉",
        "Brian Willett",
    ]
    assert not any(tmp_path.iterdir())


def test_rope_bonus_short_option_generates_rope_stake_component(tmp_path):
    """验证订单文件夹生成中的绳子赠送 short 选项生成绳子地钉组件场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : 1 Full Wall
    Fabric Material Options : 400D Polyester Fabric
    Rope & Stake Kit Options : Bonus
    """
    contact = ContactInfo(
        phone="6475359727",
        email="buyer@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    order_item = _order_item()
    order_item.platform_order_no = "701-2327833-0551442"
    order_item.asin = "B0CRRGTPFH"

    result = build_and_create_order_folder(
        order_item=order_item,
        contact_info=contact,
        recipient_name="Harmeet Chouhan",
        payment_time="2026-06-21 08:13:07",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert result.customization_pairs["Rope & Stake Kit Options"] == "Bonus"
    assert "绳子地钉" in result.folder_components


def test_notes_share_link_after_sandbags_does_not_cause_rule_missing(tmp_path):
    """验证订单文件夹生成中的备注分享链接之后沙袋 不会 导致规则缺失场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : No Wall
    Fabric Material Options : 400D Polyester Fabric
    Sandbags (4 piece set) : Add Sandbags (4 piece set) Notes/Share Link/PDF File : Jet black logo background please
    """
    contact = ContactInfo(
        phone="4132374950",
        email="Djclev7@gmail.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Buyer Name",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.customization_pairs["Sandbags (4 piece set)"] == "Add Sandbags (4 piece set)"
    assert result.missing_rule_title is None
    assert "沙袋四件套" in result.folder_components
    assert not any(tmp_path.iterdir())


def test_full_wall_only_asin_same_design_inserts_after_product_name():
    """验证订单文件夹生成中的单独全高侧墙 ASIN相同设计插入之后产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="114-0131738-0578639",
        parent_asin="B0D6XW7V9T",
        asin="B0D6KZ7G88",
        tent_quantity=1,
        customization_text="""
        Do you want the Topper Left/Right and Front/Back to have the same design and text? : No, I would like different design.
        Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses ties for attachment
        Fabric Material Option : 600D Flame Retardant Polyester Fabric
        """,
        recipient_name="Courtney Berry",
    )

    assert components[1:3] == [
        "1个3x3m帐篷的全高背墙",
        "不同设计",
    ]


def test_half_wall_only_asin_same_design_inserts_after_product_name():
    """验证订单文件夹生成中的单独半高侧墙 ASIN相同设计插入之后产品名称场景。"""
    components = build_order_folder_components(
        platform_order_no="114-0131738-0578639",
        parent_asin="B0D6XW7V9T",
        asin="B0D6XWP8YN",
        tent_quantity=1,
        customization_text="""
        Do you want the Topper Left/Right and Front/Back to have the same design and text? : Yes, please use the same design.
        Fabric Material Option : 400D Polyester Fabric
        """,
        recipient_name="Courtney Berry",
    )

    assert components[1:3] == [
        "1半高侧墙",
        "相同设计",
    ]


def test_full_wall_only_asin_builds_without_tent_top_or_accessories(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN生成不依赖 帐篷 顶布或配件场景。"""
    text = """
    Custom Full Wall Configuration:
    Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses ties for attachment
    Fabric Material Option : 600D Flame Retardant Polyester Fabric
    Select the Full Wall Size That Fits Your Canopy Tent Frame : 118×85.4" Wall – for Straight Leg 10x10‘
    Please provide an email address to confirm customization design and details or for emergencies. : courtney@thevoyage.church
    Please provide a texting number to confirm customization design and details or for emergencies. : 3373539712
    Customize Full Wall:
    Background Color : Black #000000
    Your Logo or Photo 1 : CONNECT2.png
    """
    contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=text[:500], customization_text=text)

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88"),
        contact_info=contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == (
        "114-0131738-0578639+1个3x3m帐篷的全高背墙+系带+"
        "600D阻燃面料+适配直腿足尺寸架子+Courtney Berry"
    )
    assert "帐篷顶" not in result.folder_name
    assert "拖轮包" not in result.folder_name
    assert "沙袋" not in result.folder_name


def test_full_wall_only_asin_handles_singular_double_side_title_one_sided(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN处理单数双面面标题单面场景。"""
    text = """
    Custom Full Wall Configuration:
    Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses ties for attachment
    Fabric Material Option : 600D Flame Retardant Polyester Fabric
    Double-sided Printing Option : 1-sided Printing: 1 Full Wall
    Select the Full Wall Size That Fits Your Canopy Tent Frame : 118×85.4" Wall – for Straight Leg 10x10‘
    Please provide an email address to confirm customization design and details or for emergencies. : courtney@thevoyage.church
    Please provide a texting number to confirm customization design and details or for emergencies. : 3373539712
    Customize Full Wall:
    Background Color : Black #000000
    Your Logo or Photo 1 : CONNECT2.png
    """
    contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=text[:500], customization_text=text)

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88"),
        contact_info=contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.customization_pairs["Fabric Material Options"] == "600D Flame Retardant Polyester Fabric"
    assert result.customization_pairs["Double-sided Printing Options"] == "1-sided Printing: 1 Full Wall"
    assert result.folder_name == (
        "114-0131738-0578639+1个3x3m帐篷的全高背墙+系带+"
        "600D阻燃面料+适配直腿足尺寸架子+Courtney Berry"
    )


def test_full_wall_size_options_generate_frame_fit_components():
    """验证订单文件夹生成中的全高侧墙尺寸选项生成框架适配组件场景。"""
    base = {
        "platform_order_no": "111-0000000-0000000",
        "parent_asin": "B0D6XW7V9T",
        "asin": "B0D6KZ7G88",
        "tent_quantity": 1,
        "recipient_name": "Buyer Name",
    }
    cases = [
        ('118×85.4" Wall – for Straight Leg 10x10‘', "适配直腿足尺寸架子"),
        ('114×85.4"Wall– for Straight Leg 9.5x9.5’', "适配直腿不足尺寸架子"),
        ('96×118×85.4" Wall – for Slant Leg 10x10‘', "适配斜腿架子"),
    ]

    for option_value, expected_component in cases:
        components = build_order_folder_components(
            **base,
            customization_text=f"""
            Fabric Material Option : 400D Polyester Fabric
            Select the Full Wall Size That Fits Your Canopy Tent Frame : {option_value}
            """,
        )

        assert expected_component in components


def test_canopy_frame_size_options_generate_components_before_recipient():
    """验证订单文件夹生成中的帐篷架框架尺寸选项生成组件之前收件人场景。"""
    base = {
        "platform_order_no": "111-0000000-0000000",
        "parent_asin": "B0FTV6XDGG",
        "asin": "B0DZ2W2QWK",
        "tent_quantity": 1,
        "recipient_name": "Buyer Name",
    }
    cases = [
        ('A=91.6" B=12.6" C=175.2" D=118"Fits 10\' Commercial', "适用足尺寸架子"),
        ('A=91" B=12.6" C=169.3" D=114" Fits 9.5\' Standard', "适用不足尺寸的架子"),
        ('I will provide A, B, C in "Customize Canopy Top"', "自定义尺寸"),
    ]

    for option_value, expected_component in cases:
        components = build_order_folder_components(
            **base,
            customization_text=f"""
            Fabric Material Options : 400D Polyester Fabric
            Select a Standard Size or Provide Custom Canopy Frame Dimensions for a Perfect Fit : {option_value}
            """,
        )

        assert components[-2:] == [expected_component, "Buyer Name"]


def test_full_wall_only_asin_adds_optional_rail_adapter(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN添加可选横杆转接件场景。"""
    text = """
    Fabric Material Option : 600D Flame Retardant Polyester Fabric
    Add Half Wall Rail & Frame Adapter?: Add Rail for 1.2"/30mm Square Leg Frame
    """
    contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=text[:500], customization_text=text)

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88"),
        contact_info=contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.folder_name == "114-0131738-0578639+1个3x3m帐篷的全高背墙+加横杆适配30mm方形铝夹具+600D阻燃面料+Courtney Berry"


def test_half_wall_only_asin_ignores_package_accessories(tmp_path):
    """验证订单文件夹生成中的单独半高侧墙 ASIN忽略套餐配件场景。"""
    text = """
    Fabric Material Option : 400D Polyester Fabric
    Add Half Wall Rail & Frame Adapter?: Add Rail for 2"/50mm Hex Leg Frame
    Roller Bag Options : Add Roller Bag
    Sandbags (4 piece set) : Add Sandbags (4 piece set)
    Rope & Stake Kit Options : Yes
    """
    contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=text[:500], customization_text=text)

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6XWP8YN"),
        contact_info=contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.folder_name == "114-0131738-0578639+1半高侧墙+加横杆适配50mm六角铝夹具+400D面料+Courtney Berry"
    assert "帐篷顶" not in result.folder_name
    assert "拖轮包" not in result.folder_name
    assert "沙袋" not in result.folder_name
    assert "绳子地钉" not in result.folder_name


def test_half_wall_only_no_rail_and_no_rail_pocket_is_valid_empty_option(tmp_path):
    """Amazon 新的“无横杆且无横杆袋”选项不应阻止文件夹生成。"""

    text = """
    Fabric Material Option : 400D Polyester Fabric
    Add Half Wall Rail & Frame Adapter?: No Rail and No Rail Pocket
    """
    contact = ContactInfo(
        phone="5083734171",
        email="buyer@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_wall_order_item(
            "B0D6XWP8YN",
            platform_order_no="114-8264889-1977059",
        ),
        contact_info=contact,
        recipient_name="Buyer Name",
        payment_time="2026-07-22 08:41:27",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == (
        "114-8264889-1977059+1半高侧墙+400D面料+Buyer Name"
    )
    assert not result.missing_rule_lines()


def test_wall_only_asin_double_sided_printing_modifies_wall_text(tmp_path):
    """验证订单文件夹生成中的侧墙仅ASIN 双面 打印调整侧墙文本场景。"""
    full_text = """
    Fabric Material Option : 600D Flame Retardant Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 1 Full Wall
    """
    half_text = """
    Fabric Material Option : 600D Flame Retardant Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 1 Half Wall
    """
    full_contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=full_text[:500], customization_text=full_text)
    half_contact = ContactInfo(phone="3373539712", email="courtney@thevoyage.church", source_count=1, source_excerpt=half_text[:500], customization_text=half_text)

    full_result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88"),
        contact_info=full_contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )
    half_result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6XWP8YN"),
        contact_info=half_contact,
        recipient_name="Courtney Berry",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert "1个3x3m帐篷的双面全高背墙" in full_result.folder_components
    assert "1双面半高侧墙" in half_result.folder_components


def test_full_wall_only_asin_adds_attachment_and_slant_size_option(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN添加附件并斜边尺寸选项场景。"""
    text = """
    Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses ties for attachment
    Fabric Material Option : 400D Polyester Fabric
    Select the Full Wall Size That Fits Your Canopy Tent Frame : 96×118×85.4" Wall – for Slant Leg 10x10‘
    """
    contact = ContactInfo(phone="3373539712", email="buyer@example.com", source_count=1, source_excerpt=text[:500], customization_text=text)

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88", platform_order_no="113-2099765-0299421"),
        contact_info=contact,
        recipient_name="JACK ROBINSON",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.folder_name == (
        "113-2099765-0299421+1个3x3m帐篷的全高背墙+系带+"
        "400D面料+适配斜腿架子+JACK ROBINSON"
    )


def test_full_wall_only_asin_expedited_velcro_loop_example(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN加急魔术贴毛面示例场景。"""
    text = """
    Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses the Velcro loop side
    Fabric Material Option : 400D Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 1 Full Wall
    Select the Full Wall Size That Fits Your Canopy Tent Frame : 114×85.4"Wall– for Straight Leg 9.5x9.5’
    """
    contact = ContactInfo(phone="3373539712", email="buyer@example.com", source_count=1, source_excerpt=text[:500], customization_text=text)
    order_item = _wall_order_item("B0D6KZ7G88", platform_order_no="111-9002235-2761863")
    order_item.logistics = "Expedited"

    result = build_and_create_order_folder(
        order_item=order_item,
        contact_info=contact,
        recipient_name="Danielle Matthews",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.folder_name == (
        "加急111-9002235-2761863+1个3x3m帐篷的双面全高背墙+魔术贴毛面+"
        "400D面料+适配直腿不足尺寸架子+Danielle Matthews"
    )


def test_full_wall_only_asin_velcro_hook_option_uses_single_attachment_component(tmp_path):
    """验证订单文件夹生成中的单独全高侧墙 ASIN魔术贴勾面选项使用单面附件组件场景。"""
    text = """
    Does Your Canopy Topper Have Velcro on the Bottom? Affects Full Wall attachment to Topper. : Full Wall uses the Velcro hook side
    Fabric Material Option : 400D Polyester Fabric
    Select the Full Wall Size That Fits Your Canopy Tent Frame : 118×85.4" Wall – for Straight Leg 10x10‘
    """
    contact = ContactInfo(
        phone="3373539712",
        email="buyer@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_wall_order_item("B0D6KZ7G88", platform_order_no="111-3212527-7310630"),
        contact_info=contact,
        recipient_name="Tonya Bland",
        payment_time="2026-06-21 19:17:10",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.folder_name == (
        "111-3212527-7310630+1个3x3m帐篷的全高背墙+魔术贴钩面+"
        "400D面料+适配直腿足尺寸架子+Tonya Bland"
    )
    assert "系带" not in result.folder_name


def test_unknown_option_returns_rule_missing_and_does_not_create(tmp_path):
    """验证订单文件夹生成中的未知选项返回规则缺失并 不会 创建场景。"""
    text = "Fabric Material Options : Mystery Fabric"
    contact = extract_complete_contact_candidates(
        [
            text
            + "\nPlease provide an email address to confirm customization design and details or for emergencies. : buyer@example.com"
            + "\nPlease provide a texting number to confirm customization design and details or for emergencies. : 5097688140"
        ]
    )[0]
    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Fabric Material Options"
    assert result.missing_rule_value == "Mystery Fabric"
    assert not any(tmp_path.iterdir())


def test_car_magnet_legacy_proof_instruction_is_ignored(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 旧格式确认稿说明书为忽略场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
    Proof Option : No reply to the Proof we sent within 48hrs means we will proceed with shipping
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.folder_components[-1] == "Edith Reynoso"


def test_car_magnet_unknown_proof_option_returns_rule_missing(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 未知确认稿选项返回规则缺失场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
    Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping : Mail Me A Proof
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )

    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"
    assert result.missing_rule_value == "Mail Me A Proof"
    assert not any(tmp_path.iterdir())


def test_car_magnet_unknown_same_design_option_returns_rule_missing(tmp_path):
    """验证订单文件夹生成中的汽车磁贴 未知相同设计选项返回规则缺失场景。"""
    text = """
    Customize Design Left:
    Surface Material Option : Standard Vinyl
    Corner : Rounded
    Choose Your Magnet Thickness : Heavy Strength 40mil/1mm Magnetic
    Is The Right Side Using The Same Design As The Left Side? : Maybe Use Same Design
    """
    contact = ContactInfo(phone=None, email=None, source_count=1, source_excerpt=text[:500], customization_text=text)
    item = BatchOrderItem(
        system_order_no="103710063719859764",
        platform_order_no="113-9484835-1608220",
        row_text="",
        paid_at_text="2026-06-10 10:45:58",
        asin="B0CQLN8T6Z",
        parent_asin="B0CNVT6L7Y",
        product_type="car_magnet",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Edith Reynoso",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )

    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Is The Right Side Using The Same Design As The Left Side?"
    assert result.missing_rule_value == "Maybe Use Same Design"
    assert not any(tmp_path.iterdir())


def test_identical_car_magnet_lines_merge_and_append_single_proof(tmp_path):
    """验证订单文件夹生成中的完全相同 汽车磁贴 行合并并追加单面确认稿场景。"""
    proof_title = "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping"
    common_pairs = {
        "Corner": "Rounded",
        "Choose Your Magnet Thickness": "Heavy Strength 40mil/1mm Magnetic",
        proof_title: "Online Proof (48h No Reply=SHIP)",
    }
    lines = [
        OrderFolderLine(
            asin="B0CNVLXTWB",
            sku="95-JX79-30NB",
            parent_asin="B0CNVT6L7Y",
            product_type="car_magnet",
            quantity=1,
            customization_text="",
            customization_pairs=dict(common_pairs),
            order_item_id="162092046008521",
        ),
        OrderFolderLine(
            asin="B0CNVLXTWB",
            sku="95-JX79-30NB",
            parent_asin="B0CNVT6L7Y",
            product_type="car_magnet",
            quantity=1,
            customization_text="",
            customization_pairs=dict(common_pairs),
            order_item_id="162092046008561",
        ),
    ]

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103710785419024933", "113-3229366-0649829", "", paid_at_text="2026-06-12 11:13:53"),
        order_lines=lines,
        recipient_name="Diamond Perdue",
        payment_time="2026-06-12 11:13:53",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_components[-2:] == ["Diamond Perdue", "在线检查"]
    assert result.folder_components.count("在线检查") == 1


def test_customization_text_is_not_limited_by_source_excerpt():
    """验证订单文件夹生成中的定制化文本 为不受限 by 来源摘要场景。"""
    long_prefix = "A" * 650
    text = f"""
    {long_prefix}
    Custom Feather/Teardrop Flag : 1 Set: 6.9ft 2-Sided Feather+Pole+Base
    Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 5097688140
    """
    contact = extract_complete_contact_candidates([text])[0]
    assert len(contact.source_excerpt) <= 500
    assert contact.customization_text is not None
    assert "Custom Feather/Teardrop Flag" in contact.customization_text


def test_notes_share_link_line_suffix_does_not_pollute_table_cloth_option():
    """验证订单文件夹生成中的备注分享链接行后缀 不会 污染 桌布 选项场景。"""
    text = """
    Custom Canopy Tent Package Configuration:
    Frame Options - Our Frame Recommended for Best Fit : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : 1 Full Wall
    Fabric Material Options : 400D Polyester Fabric
    Roller Bag Options : Add Roller Bag
    Rope & Stake Kit Options : Bonus Rope & Stake Kit
    Custom Fitted Table Cloth with Your Logo : 6Ft with Back (260GSM Polyester Fabric)
    Notes/Share Link/PDF File - Line 1 : We are wanting something like this.
    Notes/Share Link/PDF File - Line 2 : Please call at 9183384294 with any questions!
    Please provide an email address to confirm customization design and details or for emergencies. : test@example.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 9183273962
    """
    contact = ContactInfo(
        phone="9183273962",
        email="test@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Friends of the Museum",
        payment_time="2026-06-05 22:47:32",
        folder_root=Path("Z:/Amazon每日订单汇总"),
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert "1个6FT方套桌布+260g经编布" in result.folder_components
    assert result.missing_rule_title is None


def test_table_cloth_short_with_back_option_is_supported():
    """验证订单文件夹生成中的桌布 short 带有回退选项为支持场景。"""
    base_text = """
    Custom Canopy Tent Package Configuration:
    Frame Options : Standard 1.5"/38mm square aluminum
    Fabric Material Options : 400D Polyester Fabric
    Side Wall and Rail Options : 1 Full Wall
    Roller Bag Options : Add Roller Bag
    Please provide a texting number to confirm customization design and details or for emergencies. : 8764677149
    Please provide an email address to confirm customization design and details or for emergencies. : eelimek@yahoo.com
    """
    components = build_order_folder_components(
        platform_order_no="112-1165824-9982644",
        parent_asin="B0D6XW7V9T",
        asin="B0D14J92RZ",
        tent_quantity=1,
        customization_text=f"{base_text}\nCustom Table Cloth with Your Logo : 6Ft with Back",
        recipient_name="Alpyramids Limited",
    )
    fitted_components = build_order_folder_components(
        platform_order_no="112-1165824-9982644",
        parent_asin="B0D6XW7V9T",
        asin="B0D14J92RZ",
        tent_quantity=1,
        customization_text=f"{base_text}\nCustom Fitted Table Cloth with Your Logo : 6Ft with Back",
        recipient_name="Alpyramids Limited",
    )

    assert "1个6FT方套桌布+260g经编布" in components
    assert "1个6FT方套桌布+260g经编布" in fitted_components


def test_screenshot_table_cloth_short_option_no_longer_fails_folder_preview(tmp_path):
    """验证订单文件夹生成中的截图 桌布 short 选项无不再失败文件夹预览场景。"""
    text = """
    Custom Topper Front/Back:
    Customization Confirmation:
    Frame Options : Standard 1.5"/38mm square aluminum
    Fabric Material Options : 400D Polyester Fabric
    Side Wall and Rail Options : 1 Full Wall
    Double-sided Printing Options(Only Side Wall Options Chosen) : 2-sided Printing: 1 Full Wall
    Roller Bag Options : Add Roller Bag
    Custom Table Cloth with Your Logo : 6Ft with Back
    Please provide a texting number to confirm customization design and details or for emergencies. : 8764677149
    Please provide an email address to confirm customization design and details or for emergencies. : eelimek@yahoo.com
    """
    contact = ContactInfo(
        phone="8764677149",
        email="eelimek@yahoo.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    item = BatchOrderItem(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        row_text="",
        paid_at_text="2026-06-11 11:48:51",
        asin="B0D14J92RZ",
        parent_asin="B0D6XW7V9T",
    )

    result = build_and_create_order_folder(
        order_item=item,
        contact_info=contact,
        recipient_name="Alpyramids Limited",
        payment_time=item.paid_at_text,
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert "1个6FT方套桌布+260g经编布" in result.folder_components


def test_canada_10x15_same_design_typo_from_zip_json_builds_folder_preview(tmp_path):
    """验证订单文件夹生成中的加拿大 10x 15 相同设计拼写变体来自zipJSON生成文件夹预览场景。"""
    line = OrderFolderLine(
        asin="B0D47WD4NL",
        sku="canopytents10x15",
        parent_asin="B0CZNZVG26",
        product_type="tent",
        quantity=1,
        customization_text="",
        customization_pairs={
            "Do you want the Topper Left/Right and Front/Back to have the same design and text?": "Yes, Please ues the same design.",
            "Frame Options": 'Standard 1.5"/38mm square aluminum',
            "Fabric Material Options": "400D Polyester Fabric",
            "Side Wall and Rail Options": "1 Full Wall",
            "Double-sided Printing Options": "",
            "Roller Bag Options": "Add Roller Bag",
            "Rope & Stake Kit Options": "Yes",
            "Sandbags (4 piece set)": "No",
            "Custom Table Cloth with Your Logo": "6Ft with Back",
        },
        order_item_id="163812749158721",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem(
            system_order_no="103717326689745971",
            platform_order_no="701-5085303-6504254",
            row_text="",
            paid_at_text="2026-06-30 23:20:01",
            asin="B0D47WD4NL",
            parent_asin="B0D47WD4NL",
            sku="canopytents10x15",
        ),
        order_lines=[line],
        recipient_name="Andrea Thompson",
        payment_time="2026-06-30 23:20:01",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.missing_rule_title is None
    assert result.folder_name == (
        "701-5085303-6504254+"
        "1个3x4.5m帐篷顶+相同设计+40mm方形铝+1全高背墙+400D面料+拖轮包+绳子地钉+1个6FT方套桌布+260g经编布+"
        "Andrea Thompson"
    )


def test_existing_folder_is_success(tmp_path):
    """验证订单文件夹生成中的已存在文件夹为成功场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    first = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    second = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )
    assert first.status == "folder_created"
    assert second.status == FOLDER_EXISTING_PLATFORM_ORDER
    assert "existing_platform_order_folder" in second.folder_warnings


def test_existing_platform_order_folder_in_same_month_skips_new_folder(tmp_path):
    """验证订单文件夹生成中的已存在 平台订单 文件夹在相同月份跳过新文件夹场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    existing = tmp_path / "2026" / "6月" / "0609" / f"{_order_item().platform_order_no}+旧文件夹"
    existing.mkdir(parents=True)

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-10 10:02:05",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )

    assert result.status == FOLDER_EXISTING_PLATFORM_ORDER
    assert result.folder_path == str(existing)
    assert result.folder_name == existing.name
    assert "existing_platform_order_folder" in result.folder_warnings
    assert not (tmp_path / "2026" / "6月" / "0610").exists()
    assert find_existing_platform_order_folder(tmp_path, date(2026, 6, 10), _order_item().platform_order_no) == existing


def test_existing_platform_order_folder_in_other_month_does_not_skip(tmp_path):
    """验证订单文件夹生成中的已存在 平台订单 文件夹在其他月份 不会 跳过场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    other_month = tmp_path / "2026" / "5月" / "0531" / f"{_order_item().platform_order_no}+旧文件夹"
    other_month.mkdir(parents=True)

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-10 10:02:05",
        folder_root=tmp_path,
        create_folder=True,
        tent_quantity=1,
    )

    assert result.status == "folder_created"
    assert (tmp_path / "2026" / "6月" / "0610").exists()
    assert find_existing_platform_order_folder(tmp_path, date(2026, 6, 10), _order_item().platform_order_no) is not None


def test_folder_preview_does_not_create_until_confirmed(tmp_path):
    """验证订单文件夹生成中的文件夹预览 不会 创建直到确认场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    preview = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert preview.status == "folder_preview"
    assert preview.folder_path
    assert not any(tmp_path.iterdir())

    created = create_order_folder_from_preview(preview)

    assert created.status == "folder_created"
    assert created.folder_path == preview.folder_path
    assert Path(created.folder_path).exists()


def test_folder_preview_rechecks_existing_platform_order_before_create(tmp_path):
    """验证订单文件夹生成中的文件夹预览重新检查已存在 平台订单 之前创建场景。"""
    contact = extract_complete_contact_candidates([EXAMPLE_CUSTOMIZATION_TEXT])[0]
    preview = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-12 11:42:16",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )
    existing = tmp_path / "2026" / "6月" / "0611" / f"{_order_item().platform_order_no}+old-folder"
    existing.mkdir(parents=True)

    created = create_order_folder_from_preview(
        preview,
        platform_order_no=_order_item().platform_order_no,
    )

    assert created.status == FOLDER_EXISTING_PLATFORM_ORDER
    assert created.folder_path == str(existing)
    assert "existing_platform_order_folder_rechecked_before_create" in created.folder_warnings
    assert not Path(preview.folder_path).exists()



def test_three_half_walls_double_sided_returns_rule_missing_when_over_limit(tmp_path):
    """验证订单文件夹生成中的three 半高侧墙 双面 返回规则缺失当优于 limit场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : 1 Full and 3 Half Walls with Rails
    Fabric Material Options : 400D Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 1 Full & 3 Half Walls
    Roller Bag Options : Add Roller Bag
    """
    contact = ContactInfo(
        phone="3373539712",
        email="test@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Jaden postill",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Double-sided Printing Options"
    assert result.missing_rule_value == "2-Sided Printing: 1 Full & 3 Half Walls"


def test_half_wall_only_asin_double_sided_returns_rule_missing_when_over_limit(tmp_path):
    """验证订单文件夹生成中的单独半高侧墙 ASIN 双面 返回规则缺失当优于 limit场景。"""
    text = """
    Fabric Material Option : 400D Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 3 Half Walls
    """
    contact = ContactInfo(
        phone="3373539712",
        email="test@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )
    order_item = BatchOrderItem(
        system_order_no="103709321966368505",
        platform_order_no="114-0131738-0578639",
        row_text="",
        paid_at_text="2026-06-08 07:35:58",
        asin="B0D6XWP8YN",
        parent_asin="B0D6XW7V9T",
    )

    result = build_and_create_order_folder(
        order_item=order_item,
        contact_info=contact,
        recipient_name="Test User",
        payment_time="2026-06-08 07:35:58",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=3,
    )

    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Double-sided Printing Options"
    assert result.missing_rule_value == "2-Sided Printing: 3 Half Walls"


def test_double_sided_count_greater_than_wall_count_returns_rule_missing(tmp_path):
    """验证订单文件夹生成中的双面 数量 大于 侧墙数量返回规则缺失场景。"""
    text = """
    Frame Options : Standard 1.6"/40mm square aluminum
    Side Wall and Rail Options : 1 Full Wall
    Fabric Material Options : 400D Polyester Fabric
    Double-sided Printing Options : 2-Sided Printing: 2 Full Walls
    """
    contact = ContactInfo(
        phone="3373539712",
        email="test@example.com",
        source_count=1,
        source_excerpt=text[:500],
        customization_text=text,
    )

    result = build_and_create_order_folder(
        order_item=_order_item(),
        contact_info=contact,
        recipient_name="Test User",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
        tent_quantity=1,
    )

    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Double-sided Printing Options"
    assert result.missing_rule_value == "2-Sided Printing: 2 Full Walls"



def test_expedited_logistics_prefixes_platform_order_with_jiaji():
    """验证订单文件夹生成中的加急物流加前缀 平台订单 带有加急场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=2,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        Roller Bag Options : Add Roller Bag
        Sandbags (4 piece set) : Add Sandbags (4 piece set)
        """,
        recipient_name="Kirsten Force",
        logistics="Expedited",
    )
    assert components[0] == "加急112-3183165-4090602"
    assert "加急" in components[0]


def test_b0crrgtpfh_standard_logistics_does_not_prefix_platform_order():
    """验证 B0CRRGTPFH 已恢复普通发货，不再仅凭 ASIN 加急。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0F5CTQXG1",
        asin="B0CRRGTPFH",
        tent_quantity=1,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
        logistics="Standard",
    )

    assert components[0] == "112-3183165-4090602"
    assert "加急" not in components[0]


def test_b0crrgtpfh_explicit_expedited_logistics_still_prefixes_platform_order():
    """验证 B0CRRGTPFH 客选物流明确加急时仍按加急处理。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0F5CTQXG1",
        asin="B0CRRGTPFH",
        tent_quantity=1,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
        logistics="Expedited",
    )

    assert components[0] == "加急112-3183165-4090602"


def test_b0crrgtpfh_standard_logistics_does_not_prefix_canada_order():
    """验证加拿大 B0CRRGTPFH 普通物流不加文件夹前缀。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0F5CTQXG1",
        asin="B0CRRGTPFH",
        tent_quantity=1,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
        logistics="Standard",
        shipping_address_text="Canada, ON, TORONTO",
    )

    assert components[0] == "112-3183165-4090602"
    assert "加急" not in components[0]


def test_b0crrgtpfh_standard_logistics_does_not_prefix_us_non_mainland_order_lines():
    """验证美国非本土 B0CRRGTPFH 多商品入口不加普通物流前缀。"""
    components = build_order_folder_components_from_lines(
        platform_order_no="112-3183165-4090602",
        order_lines=[
            OrderFolderLine(
                asin="B0CRRGTPFH",
                sku="canopytents",
                parent_asin="B0F5CTQXG1",
                product_type=None,
                quantity=1,
                customization_text="""
                Frame Options : Standard 1.6"/40mm square aluminum
                Fabric Material Options : 400D Polyester Fabric
                """,
            )
        ],
        recipient_name="Kirsten Force",
        logistics="Standard",
        shipping_address_text="United States of America (USA), AK, ANCHORAGE",
    )

    assert components[0] == "112-3183165-4090602"
    assert "加急" not in components[0]


def test_us_mainland_38mm_frame_still_uses_40mm_folder_component():
    """验证美国本土 38mm 方形铝仍按旧规则生成 40mm 方形铝。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0F4PV828T",
        tent_quantity=1,
        customization_text='''
        Frame Options : Standard 1.5"/38mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        ''',
        recipient_name="Kirsten Force",
        shipping_address_text="United States of America (USA), TX, HOUSTON",
    )

    assert "40mm方形铝" in components
    assert "38mm方形铝" not in components


def test_canada_38mm_frame_keeps_38mm_folder_component():
    """验证加拿大订单 38mm 方形铝保留原规格生成文件夹片段。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0F4PV828T",
        tent_quantity=1,
        customization_text='''
        Frame Options : Standard 1.5"/38mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        ''',
        recipient_name="Kirsten Force",
        shipping_address_text="Canada, ON, TORONTO",
    )

    assert "38mm方形铝" in components
    assert "40mm方形铝" not in components


def test_standard_logistics_does_not_prefix():
    """验证订单文件夹生成中的standard 物流 不会 前缀场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=2,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        Roller Bag Options : Add Roller Bag
        """,
        recipient_name="Kirsten Force",
        logistics="Standard",
    )
    assert components[0] == "112-3183165-4090602"
    assert "加急" not in components[0]


def test_none_logistics_does_not_prefix():
    """验证订单文件夹生成中的空值物流 不会 前缀场景。"""
    components = build_order_folder_components(
        platform_order_no="112-3183165-4090602",
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=2,
        customization_text="""
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        """,
        recipient_name="Kirsten Force",
        logistics=None,
    )
    assert components[0] == "112-3183165-4090602"
