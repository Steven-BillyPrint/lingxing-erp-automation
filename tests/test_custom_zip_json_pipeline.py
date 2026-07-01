from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lingxing_automation.flows.contact_sync import finalize_custom_zip_files_for_folder
from lingxing_automation.models import BatchOrderItem, CustomZipFile, CustomizationJsonInfo, FolderBuildResult, FolderNameShortenResult, OrderCustomZipBundle, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.services.custom_zip_parser import (
    CUSTOM_ZIP_STAGING_CLEANED,
    CUSTOM_ZIP_STAGING_CLEANUP_ERROR,
    cleanup_custom_zip_staging_dir,
    copy_custom_zip_files_to_folder,
    parse_custom_zip_file,
    write_full_folder_name_txt,
)
from lingxing_automation.services.customization_json_parser import parse_customization_json_info
from lingxing_automation.services.folder_builder import (
    build_and_create_order_folder_from_lines,
    shorten_folder_name_by_components,
)
from lingxing_automation.services.order_line_matcher import build_order_folder_lines_from_json
from lingxing_automation.products.tablecloths import PRODUCT_TYPE_TABLECLOTHS


def _magnet_json(
    order_item_id: str,
    asin: str,
    quantity: int,
    *,
    thickness: str,
    corner: str = "",
    proof: str = "",
    same_design: str = "",
) -> dict:
    areas = [
        {"customizationType": "Options", "label": "Surface Material Option", "optionValue": "Standard Vinyl"},
        {"customizationType": "Options", "label": "Choose Your Magnet Thickness", "optionValue": thickness},
    ]
    if corner:
        areas.append({"customizationType": "Options", "label": "Corner", "optionValue": corner})
    if proof:
        areas.append(
            {
                "customizationType": "Options",
                "label": "Proof Option - No reply to the Proof we sent within 48hrs means we will proceed with shipping",
                "optionValue": proof,
            }
        )
    if same_design:
        areas.append(
            {
                "customizationType": "Options",
                "label": "Is The Right Side Using The Same Design As The Left Side?",
                "optionValue": same_design,
            }
        )
    return {
        "orderId": "112-5663586-1765001",
        "orderItemId": order_item_id,
        "asin": asin,
        "title": "Car Magnet",
        "quantity": quantity,
        "version3.0": {"customizationInfo": {"surfaces": [{"areas": areas}]}},
    }


def _write_zip(path: Path, payload: dict) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{payload['orderItemId']}.json", json.dumps(payload))
        archive.writestr("image.png", b"fake-image")


def test_zip_parser_reads_json_and_keeps_same_asin_rows_separate(tmp_path):
    zip_a = tmp_path / "B0CQLN5GNL_25_CustomizedInfo.zip"
    zip_b = tmp_path / "B0CQLN5GNL_59_CustomizedInfo.zip"
    _write_zip(zip_a, _magnet_json("161986526102481", "B0CQLN5GNL", 1, thickness="Heavy Strength 40mil/1mm Magnetic", corner="Rounded"))
    _write_zip(zip_b, _magnet_json("161986526102441", "B0CQLN5GNL", 1, thickness="Standard Strength 20mil/0.5mm Magnetic", corner="Rounded"))

    file_a, info_a = parse_custom_zip_file(
        CustomZipFile(1, "B0CQLN5GNL", None, None, "112-5663586-1765001", "共4", zip_a.name, str(zip_a)),
        tmp_path,
    )
    file_b, info_b = parse_custom_zip_file(
        CustomZipFile(2, "B0CQLN5GNL", None, None, "112-5663586-1765001", "共5", zip_b.name, str(zip_b)),
        tmp_path,
    )

    assert file_a.json_filename == "161986526102481.json"
    assert file_b.json_filename == "161986526102441.json"
    assert info_a is not None and info_a.order_item_id == "161986526102481"
    assert info_b is not None and info_b.order_item_id == "161986526102441"
    assert info_a.pairs["Choose Your Magnet Thickness"] == "Heavy Strength 40mil/1mm Magnetic"
    assert info_b.pairs["Choose Your Magnet Thickness"] == "Standard Strength 20mil/0.5mm Magnetic"


def test_tent_frame_compatibility_alert_json_title_is_canonicalized():
    info = parse_customization_json_info(
        {
            "orderId": "701-2292402-2697828",
            "orderItemId": "tent-item-1",
            "asin": "B0DZ2W2QWK",
            "quantity": 1,
            "version3.0": {
                "customizationInfo": {
                    "surfaces": [
                        {
                            "areas": [
                                {
                                    "customizationType": "Options",
                                    "label": "Frame Options - Compatibility Alert for Frame",
                                    "optionValue": 'Standard 1.6"/40mm square aluminum',
                                }
                            ]
                        }
                    ]
                }
            },
        }
    )

    assert info.pairs["Frame Options"] == 'Standard 1.6"/40mm square aluminum'


def test_car_magnet_multi_order_items_build_expected_folder_name(tmp_path):
    infos = [
        _magnet_json("161986526102441", "B0CQLN5GNL", 1, thickness="Standard Strength 20mil/0.5mm Magnetic", corner="Rounded"),
        _magnet_json("161986526102481", "B0CQLN5GNL", 1, thickness="Heavy Strength 40mil/1mm Magnetic", corner="Rounded"),
        _magnet_json("161986526102401", "B0DRCY4HM5", 30, thickness="Standard Strength 20mil/0.5mm Magnetic"),
    ]
    parsed = []
    for payload in infos:
        zip_path = tmp_path / f"{payload['asin']}_{payload['orderItemId']}_CustomizedInfo.zip"
        _write_zip(zip_path, payload)
        _, info = parse_custom_zip_file(
            CustomZipFile(1, payload["asin"], None, None, payload["orderId"], "共1", zip_path.name, str(zip_path)),
            tmp_path,
        )
        assert info is not None
        parsed.append(info)
    amazon_items = [
        {"asin": "B0CQLN5GNL", "seller_sku": 'BillyPrint-Car Magnet-12"x24"-2', "quantity_ordered": 1, "order_item_id": "161986526102441"},
        {"asin": "B0CQLN5GNL", "seller_sku": 'BillyPrint-Car Magnet-12"x24"-2', "quantity_ordered": 1, "order_item_id": "161986526102481"},
        {"asin": "B0DRCY4HM5", "seller_sku": "Car-Magent-3x10in-1pcs", "quantity_ordered": 30, "order_item_id": "161986526102401"},
    ]

    lines, warnings = build_order_folder_lines_from_json(amazon_order_items=amazon_items, customization_items=parsed)
    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103710114768856314", "112-5663586-1765001", "", paid_at_text="2026-06-10 13:45:05"),
        order_lines=lines,
        recipient_name="Doniel Hagee",
        payment_time="2026-06-10 13:45:05",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert warnings == []
    assert result.folder_name == (
        "112-5663586-1765001+2个12x24in汽车磁贴+圆角+0.5mm+"
        "2个12x24in汽车磁贴+圆角+1mm+30个3x10in汽车磁贴+0.5mm+Doniel Hagee"
    )


def test_car_magnet_zip_json_proof_appends_after_recipient(tmp_path):
    payload = _magnet_json(
        "161986526102441",
        "B0CQLN5GNL",
        1,
        thickness="Standard Strength 20mil/0.5mm Magnetic",
        corner="Rounded",
        proof="Straight To Production",
    )
    zip_path = tmp_path / f"{payload['asin']}_{payload['orderItemId']}_CustomizedInfo.zip"
    _write_zip(zip_path, payload)
    _, info = parse_custom_zip_file(
        CustomZipFile(1, payload["asin"], None, None, payload["orderId"], "鍏?", zip_path.name, str(zip_path)),
        tmp_path,
    )
    assert info is not None
    amazon_items = [
        {
            "asin": "B0CQLN5GNL",
            "seller_sku": 'BillyPrint-Car Magnet-12"x24"-2',
            "quantity_ordered": 1,
            "order_item_id": "161986526102441",
        }
    ]

    lines, warnings = build_order_folder_lines_from_json(amazon_order_items=amazon_items, customization_items=[info])
    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103710114768856314", "112-5663586-1765001", "", paid_at_text="2026-06-10 13:45:05"),
        order_lines=lines,
        recipient_name="Doniel Hagee",
        payment_time="2026-06-10 13:45:05",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert warnings == []
    assert result.status == "folder_preview"
    assert result.folder_components[-2:] == ["Doniel Hagee", "直接制作"]
    assert result.folder_name.endswith("+Doniel Hagee+直接制作")


def test_car_magnet_zip_json_same_design_inserts_after_product_name(tmp_path):
    payload = _magnet_json(
        "161986526102441",
        "B0CQLN5GNL",
        1,
        thickness="Standard Strength 20mil/0.5mm Magnetic",
        corner="Rounded",
        same_design="No,Using Different Design for Right Side",
    )
    zip_path = tmp_path / f"{payload['asin']}_{payload['orderItemId']}_CustomizedInfo.zip"
    _write_zip(zip_path, payload)
    _, info = parse_custom_zip_file(
        CustomZipFile(1, payload["asin"], None, None, payload["orderId"], "鍏?", zip_path.name, str(zip_path)),
        tmp_path,
    )
    assert info is not None
    amazon_items = [
        {
            "asin": "B0CQLN5GNL",
            "seller_sku": 'BillyPrint-Car Magnet-12"x24"-2',
            "quantity_ordered": 1,
            "order_item_id": "161986526102441",
        }
    ]

    lines, warnings = build_order_folder_lines_from_json(amazon_order_items=amazon_items, customization_items=[info])
    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103710114768856314", "112-5663586-1765001", "", paid_at_text="2026-06-10 13:45:05"),
        order_lines=lines,
        recipient_name="Doniel Hagee",
        payment_time="2026-06-10 13:45:05",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert warnings == []
    assert result.status == "folder_preview"
    assert result.folder_components[1:4] == [
        "2个12x24in汽车磁贴",
        "不同设计",
        "圆角",
    ]


def test_tablecloth_zip_json_order_item_builds_folder_name(tmp_path):
    customization = CustomizationJsonInfo(
        order_id="114-0873348-5648216",
        order_item_id="162557678960121",
        asin="B0DBGBV6KN",
        title="BillyPrint Custom Table Cloth",
        quantity=1,
        pairs={
            "Choose Your Polyester Fabric": "280GSM Spandex, High Density & Durable",
            "Open or Closed Back Option": "Open Tablecloth in the Back",
            "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)": (
                "Online Proof (48h No Reply=SHIP)"
            ),
        },
    )
    amazon_items = [
        {
            "asin": "B0DBGBV6KN",
            "seller_sku": "Tablecloth-Spandex-5FT",
            "quantity_ordered": 1,
            "order_item_id": "162557678960121",
        }
    ]

    lines, warnings = build_order_folder_lines_from_json(amazon_order_items=amazon_items, customization_items=[customization])
    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103712718018525855", "114-0873348-5648216", "", paid_at_text="2026-06-17 22:17:49"),
        order_lines=lines,
        recipient_name="Priscila nohr",
        payment_time="2026-06-17 22:17:49",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert warnings == []
    assert len(lines) == 1
    assert lines[0].product_type == PRODUCT_TYPE_TABLECLOTHS
    assert result.status == "folder_preview"
    assert result.folder_name == "114-0873348-5648216+1个5FT弹力桌布+280g弹力布+背后开口+Priscila nohr+在线检查"


def test_contact_candidates_are_read_from_customization_json():
    info = CustomizationJsonInfo(
        order_id="112-5663586-1765001",
        order_item_id="161986526102441",
        asin="B0CQLN5GNL",
        title="Car Magnet",
        quantity=1,
        pairs={
            "Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc)": (
                "Call 555-123-4567 or email doniel@example.com"
            )
        },
    )

    contacts = extract_contact_candidates_from_json_items([info])

    assert len(contacts) == 1
    assert contacts[0].phone == "5551234567"
    assert contacts[0].email == "doniel@example.com"


def test_json_contact_value_keeps_unicode_email_prefix():
    info = CustomizationJsonInfo(
        order_id="111-0876474-3960252",
        order_item_id="161986526102441",
        asin="B0CQLN8T6Z",
        title="Car Magnet",
        quantity=1,
        pairs={
            "Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc)": (
                "7373970043 Ben’s.backflow@icloud.com"
            )
        },
    )

    contacts = extract_contact_candidates_from_json_items([info])

    assert len(contacts) == 1
    assert contacts[0].phone == "7373970043"
    assert contacts[0].email == "Ben’s.backflow@icloud.com"


def test_tent_contact_candidates_are_read_from_json_pair_values():
    info = CustomizationJsonInfo(
        order_id="111-2789436-8737015",
        order_item_id="tent-item-1",
        asin="B0DZ2W2QWK",
        title="Tent",
        quantity=1,
        pairs={
            "Please provide an email address to confirm customization design and details or for emergencies.": (
                "affûtage.letourneau@outlook.com"
            ),
            "Please provide a texting number to confirm customization design and details or for emergencies.": (
                "8193011336"
            ),
        },
    )

    contacts = extract_contact_candidates_from_json_items([info])

    assert len(contacts) == 1
    assert contacts[0].phone == "8193011336"
    assert contacts[0].email == "affûtage.letourneau@outlook.com"


def test_tent_folder_components_can_be_built_from_json_pairs(tmp_path):
    line = OrderFolderLine(
        asin="B0DZ2W2QWK",
        sku="canopytents",
        parent_asin=None,
        product_type="tent",
        quantity=1,
        customization_text="",
        customization_pairs={
            "Frame Options - Our Frame Recommended for Best Fit": 'Premium 2"/50mm hexagonal aluminum',
            "Side Wall and Rail Options": "1 Full and 2 Half Walls with Rails",
            "Double-sided Printing Options": "2-Sided Printing: 1 Full & 2 Half Walls",
            "Fabric Material Options": "600D Flame Retardant Polyester Fabric",
        },
        order_item_id="tent-item-1",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103", "111-2789436-8737015", "", paid_at_text="2026-06-04 15:23:10"),
        order_lines=[line],
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.folder_name == (
        "111-2789436-8737015+1个3x3m帐篷顶+50mm六角铝+"
        "1双面全高背墙+2双面半高侧墙(带横杆)+600D阻燃面料+Sawako Hiraoka"
    )


def test_tent_same_design_can_be_built_from_json_pairs(tmp_path):
    line = OrderFolderLine(
        asin="B0DZ2W2QWK",
        sku="canopytents",
        parent_asin=None,
        product_type="tent",
        quantity=1,
        customization_text="",
        customization_pairs={
            "Do you want the Topper Left/Right and Front/Back to have the same design and text?": (
                "Yes, please use the same design."
            ),
            "Frame Options - Our Frame Recommended for Best Fit": 'Premium 2"/50mm hexagonal aluminum',
            "Side Wall and Rail Options": "No Wall",
            "Fabric Material Options": "600D Flame Retardant Polyester Fabric",
        },
        order_item_id="tent-item-1",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103", "111-2789436-8737015", "", paid_at_text="2026-06-04 15:23:10"),
        order_lines=[line],
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.folder_components[:4] == [
        "111-2789436-8737015",
        "1个3x3m帐篷顶",
        "相同设计",
        "50mm六角铝",
    ]


def test_tent_option_values_accept_safe_singular_plural_variants(tmp_path):
    line = OrderFolderLine(
        asin="B0DZ2W2QWK",
        sku="canopytents",
        parent_asin=None,
        product_type="tent",
        quantity=1,
        customization_text="",
        customization_pairs={
            "Frame Options - Our Frame Recommended for Best Fit": 'Premium 2"/50mm hexagonal aluminum',
            "Side Wall and Rail Options": "1 Full and 2 Half Wall with Rail",
            "Double-sided Printing Options": "2-Sided Printing: 1 Full & 2 Half Walls",
            "Fabric Material Options": "600D Flame Retardant Polyester Fabric",
            "Sandbags (4 piece set)": "Add Sandbag (4 piece set)",
        },
        order_item_id="tent-item-plural-variants",
    )

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103", "111-2789436-8737015", "", paid_at_text="2026-06-04 15:23:10"),
        order_lines=[line],
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
    )

    standard_line = OrderFolderLine(
        asin="B0DZ2W2QWK",
        sku="canopytents",
        parent_asin=None,
        product_type="tent",
        quantity=1,
        customization_text="",
        customization_pairs={
            "Frame Options - Our Frame Recommended for Best Fit": 'Premium 2"/50mm hexagonal aluminum',
            "Side Wall and Rail Options": "1 Full and 2 Half Walls with Rails",
            "Double-sided Printing Options": "2-Sided Printing: 1 Full & 2 Half Walls",
            "Fabric Material Options": "600D Flame Retardant Polyester Fabric",
            "Sandbags (4 piece set)": "Add Sandbags (4 piece set)",
        },
        order_item_id="tent-item-standard",
    )
    standard_result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103", "111-2789436-8737015", "", paid_at_text="2026-06-04 15:23:10"),
        order_lines=[standard_line],
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == standard_result.folder_name


def test_identical_car_magnet_lines_keep_order_lines_in_folder_name(tmp_path):
    lines = [
        OrderFolderLine(
            asin="B0CNVLXTWB",
            sku="95-JX79-30NB",
            parent_asin="B0CNVT6L7Y",
            product_type="car_magnet",
            quantity=1,
            customization_text="",
            customization_pairs={
                "Corner": "Rounded",
                "Choose Your Magnet Thickness": "Heavy Strength 40mil/1mm Magnetic",
            },
            order_item_id="162092046008521",
        ),
        OrderFolderLine(
            asin="B0CNVLXTWB",
            sku="95-JX79-30NB",
            parent_asin="B0CNVT6L7Y",
            product_type="car_magnet",
            quantity=1,
            customization_text="",
            customization_pairs={
                "Corner": "Rounded",
                "Choose Your Magnet Thickness": "Heavy Strength 40mil/1mm Magnetic",
            },
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

    assert result.folder_components == [
        "113-3229366-0649829",
        "2个18x24in汽车磁贴",
        "圆角",
        "1mm",
        "2个18x24in汽车磁贴",
        "圆角",
        "1mm",
        "Diamond Perdue",
    ]


def test_identical_tent_lines_keep_order_lines(tmp_path):
    pairs = {
        "Frame Options - Our Frame Recommended for Best Fit": 'Premium 2"/50mm hexagonal aluminum',
        "Side Wall and Rail Options": "1 Full and 2 Half Walls with Rails",
        "Double-sided Printing Options": "2-Sided Printing: 1 Full & 2 Half Walls",
        "Fabric Material Options": "600D Flame Retardant Polyester Fabric",
    }
    lines = [
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="canopytents",
            parent_asin=None,
            product_type="tent",
            quantity=1,
            customization_text="",
            customization_pairs=pairs,
            order_item_id="tent-item-1",
        ),
        OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku="canopytents",
            parent_asin=None,
            product_type="tent",
            quantity=1,
            customization_text="",
            customization_pairs=pairs,
            order_item_id="tent-item-2",
        ),
    ]

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem("103", "111-2789436-8737015", "", paid_at_text="2026-06-04 15:23:10"),
        order_lines=lines,
        recipient_name="Sawako Hiraoka",
        payment_time="2026-06-04 15:23:10",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.folder_name == (
        "111-2789436-8737015+1个3x3m帐篷顶+50mm六角铝+"
        "1双面全高背墙+2双面半高侧墙(带横杆)+600D阻燃面料+"
        "1个3x3m帐篷顶+50mm六角铝+"
        "1双面全高背墙+2双面半高侧墙(带横杆)+600D阻燃面料+Sawako Hiraoka"
    )


def test_tablecloth_order_113_5784182_0867428_keeps_order_lines(tmp_path):
    proof_title = "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)"
    lines = [
        OrderFolderLine(
            asin="B0D9HS5187",
            sku="tablecloth-rectangle-4ft",
            parent_asin="B0D9HVTXW2",
            product_type=PRODUCT_TYPE_TABLECLOTHS,
            quantity=2,
            customization_text="",
            customization_pairs={
                "Choose Your Polyester Fabric": "150GSM Polyester, Light and Versatile",
                "Open or Closed Back Option": "Open Tablecloth in the Back",
                proof_title: "Online Proof (48h No Reply=SHIP)",
            },
            order_item_id="163815919269281",
        ),
        OrderFolderLine(
            asin="B0D9HT8LD4",
            sku="tablecloth-rectangle-8ft",
            parent_asin="B0D9HVTXW2",
            product_type=PRODUCT_TYPE_TABLECLOTHS,
            quantity=3,
            customization_text="",
            customization_pairs={
                "Choose Your Polyester Fabric": "260GSM Polyester, High Density & Durable",
                "Open or Closed Back Option": "Open Tablecloth in the Back",
                proof_title: "Online Proof (48h No Reply=SHIP)",
            },
            order_item_id="163815919269321",
        ),
        OrderFolderLine(
            asin="B0D9HT8LD4",
            sku="tablecloth-rectangle-8ft",
            parent_asin="B0D9HVTXW2",
            product_type=PRODUCT_TYPE_TABLECLOTHS,
            quantity=1,
            customization_text="",
            customization_pairs={
                "Choose Your Polyester Fabric": "260GSM Polyester, High Density & Durable",
                "Open or Closed Back Option": "Open Tablecloth in the Back",
                proof_title: "Online Proof (48h No Reply=SHIP)",
            },
            order_item_id="163815919269361",
        ),
    ]

    result = build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem(
            "103717344856145029",
            "113-5784182-0867428",
            "",
            paid_at_text="2026-07-01 00:04:21",
        ),
        order_lines=lines,
        recipient_name="Kaycee Wright",
        payment_time="2026-07-01 00:04:21",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.folder_name == (
        "113-5784182-0867428+2个4FT方套桌布+150g经编布+背后开口+"
        "3个8FT方套桌布+260g经编布+背后开口+"
        "1个8FT方套桌布+260g经编布+背后开口+Kaycee Wright+在线检查"
    )
    assert "不同画面" not in (result.folder_name or "")


def test_shorten_folder_name_removes_whole_components_only():
    result = shorten_folder_name_by_components(
        ["112-5663586-1765001", "一段很长的中间片段A", "一段很长的中间片段B", "Doniel Hagee"],
        max_length=45,
    )

    assert result.was_shortened is True
    assert result.safe_folder_name.startswith("112-5663586-1765001")
    assert result.safe_folder_name.endswith("Doniel Hagee")
    assert "++" not in result.safe_folder_name
    assert result.removed_components


def test_final_folder_only_gets_zip_and_full_name_txt(tmp_path):
    zip_path = tmp_path / "B0CQLN5GNL_25_CustomizedInfo.zip"
    _write_zip(zip_path, _magnet_json("161986526102481", "B0CQLN5GNL", 1, thickness="Heavy Strength 40mil/1mm Magnetic"))
    extract_dir = tmp_path / "B0CQLN5GNL_25_CustomizedInfo"
    extract_dir.mkdir()
    (extract_dir / "161986526102481.json").write_text("{}", encoding="utf-8")
    folder = tmp_path / "final"
    folder.mkdir()

    status, copied, error = copy_custom_zip_files_to_folder(
        [CustomZipFile(1, "B0CQLN5GNL", None, None, "112-5663586-1765001", "共4", zip_path.name, str(zip_path))],
        folder,
    )
    txt = write_full_folder_name_txt(
        folder,
        FolderNameShortenResult("full+name", "safe+name", ["full", "name"], ["safe", "name"], [], False, 180),
    )

    assert status == "custom_zip_moved"
    assert error is None
    assert copied and Path(copied[0]).suffix == ".zip"
    assert Path(txt).name == "完整文件夹名.txt"
    assert not list(folder.glob("*.json"))
    assert not list(folder.glob("*.png"))


def test_cleanup_custom_zip_staging_dir_removes_order_dir_but_not_root(tmp_path):
    staging_root = tmp_path / "custom_zip_staging"
    order_dir = staging_root / "112-5663586-1765001"
    order_dir.mkdir(parents=True)
    (order_dir / "source.zip").write_bytes(b"zip")

    status, error = cleanup_custom_zip_staging_dir(order_dir)

    assert status == CUSTOM_ZIP_STAGING_CLEANED
    assert error is None
    assert not order_dir.exists()
    assert staging_root.exists()

    status, error = cleanup_custom_zip_staging_dir(staging_root)

    assert status == CUSTOM_ZIP_STAGING_CLEANUP_ERROR
    assert error and "拒绝删除 staging 根目录" in error
    assert staging_root.exists()


def test_finalize_cleans_staging_only_after_successful_copy(tmp_path):
    staging_root = tmp_path / "custom_zip_staging"
    order_dir = staging_root / "112-5663586-1765001"
    order_dir.mkdir(parents=True)
    zip_path = order_dir / "B0CQLN5GNL_25_CustomizedInfo.zip"
    _write_zip(zip_path, _magnet_json("161986526102481", "B0CQLN5GNL", 1, thickness="Heavy Strength 40mil/1mm Magnetic"))
    final_folder = tmp_path / "final"
    final_folder.mkdir()
    folder_result = FolderBuildResult(
        status="folder_created",
        folder_path=str(final_folder),
        folder_name="safe+name",
        folder_name_full="full+name",
        folder_components=["safe", "name"],
        folder_components_full=["full", "name"],
        folder_name_max_length=180,
    )
    bundle = OrderCustomZipBundle(
        platform_order_no="112-5663586-1765001",
        zip_files=[CustomZipFile(1, "B0CQLN5GNL", None, None, "112-5663586-1765001", "共4", zip_path.name, str(zip_path))],
    )

    result = finalize_custom_zip_files_for_folder(
        folder_result,
        {"zip_bundle": bundle, "custom_zip_staging_dir": str(order_dir)},
    )

    assert result["custom_zip_status"] == "custom_zip_moved"
    assert result["full_folder_name_txt"] is None
    assert result["custom_zip_staging_cleanup_status"] == CUSTOM_ZIP_STAGING_CLEANED
    assert not order_dir.exists()
    assert (final_folder / zip_path.name).exists()
    assert not list(final_folder.glob("*.txt"))

    failed_order_dir = staging_root / "failed-order"
    failed_order_dir.mkdir()
    missing_zip = failed_order_dir / "missing.zip"
    failed_bundle = OrderCustomZipBundle(
        platform_order_no="failed-order",
        zip_files=[CustomZipFile(1, "B0CQLN5GNL", None, None, "failed-order", "共4", missing_zip.name, str(missing_zip))],
    )

    failed = finalize_custom_zip_files_for_folder(
        folder_result,
        {"zip_bundle": failed_bundle, "custom_zip_staging_dir": str(failed_order_dir)},
    )

    assert failed["custom_zip_status"] == "custom_zip_move_error"
    assert failed["custom_zip_staging_cleanup_status"] is None
    assert failed_order_dir.exists()


def test_finalize_writes_full_folder_name_txt_only_when_shortened(tmp_path):
    staging_root = tmp_path / "custom_zip_staging"
    order_dir = staging_root / "shortened-order"
    order_dir.mkdir(parents=True)
    zip_path = order_dir / "B0CQLN5GNL_25_CustomizedInfo.zip"
    _write_zip(zip_path, _magnet_json("161986526102481", "B0CQLN5GNL", 1, thickness="Heavy Strength 40mil/1mm Magnetic"))
    final_folder = tmp_path / "final"
    final_folder.mkdir()
    folder_result = FolderBuildResult(
        status="folder_created",
        folder_path=str(final_folder),
        folder_name="safe+name",
        folder_name_full="full+removed+name",
        folder_components=["safe", "name"],
        folder_components_full=["full", "removed", "name"],
        folder_name_removed_components=["removed"],
        folder_name_was_shortened=True,
        folder_name_max_length=180,
    )
    bundle = OrderCustomZipBundle(
        platform_order_no="shortened-order",
        zip_files=[CustomZipFile(1, "B0CQLN5GNL", None, None, "shortened-order", "共1", zip_path.name, str(zip_path))],
    )

    result = finalize_custom_zip_files_for_folder(
        folder_result,
        {"zip_bundle": bundle, "custom_zip_staging_dir": str(order_dir)},
    )

    txt_path = result["full_folder_name_txt"]
    assert result["custom_zip_status"] == "custom_zip_moved"
    assert txt_path is not None
    assert Path(txt_path).exists()
    assert Path(txt_path).parent == final_folder
    assert folder_result.full_folder_name_txt == txt_path
    assert not order_dir.exists()
