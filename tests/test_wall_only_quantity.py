from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from lingxing_automation.models import BatchOrderItem, CustomZipFile
from lingxing_automation.services.custom_zip_parser import parse_custom_zip_file
from lingxing_automation.services.customization_json_parser import parse_customization_json_info
from lingxing_automation.services.folder_builder import (
    FOLDER_EXISTING_PLATFORM_ORDER,
    build_and_create_order_folder_from_lines,
    build_daily_folder,
    build_order_folder_components,
    build_order_folder_components_from_lines,
)
from lingxing_automation.services.order_line_matcher import build_order_folder_lines_from_json
from lingxing_automation.services.tent_sku_planner import build_tent_sku_plan


ORDER = "114-5509272-4631400"
ITEM_ID = "169021925367121"
PARENT_ASIN = "B0D6XW7V9T"
HALF_ASIN = "B0D6XWP8YN"
FULL_ASIN = "B0D6KZ7G88"


def _wall_json(asin, quantity, option, *, item_id=ITEM_ID):
    """保留故障订单的数量与定制字段结构，省略买家联系方式和图片。"""
    areas = [
        {
            "customizationType": "Options",
            "label": "Fabric Material Options",
            "optionValue": "400D Polyester Fabric",
        },
        {
            "customizationType": "Options",
            "label": "Add Half Wall Rail & Frame Adapter?",
            "optionValue": "No Rail and No Rail Pocket",
        },
    ]
    if option is not None:
        areas.append({
            "customizationType": "Options",
            "label": "Double-sided Printing Options",
            "optionValue": option,
        })
    return {
        "orderId": ORDER,
        "orderItemId": item_id,
        "asin": asin,
        "quantity": quantity,
        "version3.0": {"customizationInfo": {"surfaces": [{"areas": areas}]}},
    }


def _amazon_item(payload):
    return {
        "ASIN": payload["asin"],
        "SellerSKU": "Custom Canopy Tent Wall",
        "OrderItemId": payload["orderItemId"],
        "QuantityOrdered": payload["quantity"],
    }


def _folder_preview(tmp_path, lines):
    return build_and_create_order_folder_from_lines(
        order_item=BatchOrderItem(
            system_order_no="test-system-order",
            platform_order_no=ORDER,
            row_text="",
            asin=lines[0].asin,
            parent_asin=PARENT_ASIN,
        ),
        order_lines=lines,
        recipient_name="Test Buyer",
        payment_time="2026-09-05 08:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )


@pytest.mark.parametrize("quantity", [1, 2, 3, 12])
@pytest.mark.parametrize("sided", [None, 1, 2])
@pytest.mark.parametrize(
    ("asin", "english_wall", "chinese_wall", "base_sku"),
    [
        (HALF_ASIN, "Half Wall", "半高侧墙", "10ft-Half-Wall"),
        (FULL_ASIN, "Full Wall", "全高背墙", "10ft-Full-Wall"),
    ],
)
def test_wall_only_zip_quantity_scales_folder_and_row_bound_sku(
    tmp_path, quantity, sided, asin, english_wall, chinese_wall, base_sku
):
    option = f"{sided}-sided Printing: 1 {english_wall}" if sided else None
    payload = _wall_json(asin, quantity, option)
    zip_path = tmp_path / "customization.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"{ITEM_ID}.json", json.dumps(payload))
    parsed_zip, info = parse_custom_zip_file(
        CustomZipFile(1, asin, None, ITEM_ID, ORDER, "", zip_path.name, str(zip_path)),
        tmp_path / "staging",
    )
    assert parsed_zip.status == "custom_zip_json_parsed"
    assert info is not None and info.quantity == quantity
    lines, warnings = build_order_folder_lines_from_json(
        amazon_order_items=[_amazon_item(payload)], customization_items=[info]
    )
    assert warnings == []
    result = _folder_preview(tmp_path / "orders", lines)
    printing = "双面" if sided == 2 else ""
    product = (
        f"{quantity}个3x3m帐篷的{printing}{chinese_wall}"
        if asin == FULL_ASIN
        else f"{quantity}{printing}{chinese_wall}"
    )
    assert result.status == "folder_preview"
    assert result.folder_name == f"{ORDER}+{product}+400D面料+Test Buyer"
    assert not (tmp_path / "orders").exists()

    plan = build_tent_sku_plan(
        platform_order_no=ORDER,
        system_order_no="test-system-order",
        folder_components=result.folder_components,
        destination_text="United States of America (USA)",
        asin=asin,
        order_lines=lines,
    )
    expected_sku = base_sku + ("-Double-Sided" if sided == 2 else "")
    assert plan.manual_required is False, plan.manual_reason
    assert [(item.sku, item.quantity) for item in plan.replace_main_items] == [
        (expected_sku, quantity)
    ]
    assert plan.replace_main_items[0].source_order_item_id == ITEM_ID
    assert plan.replace_main_items[0].source_original_quantity == quantity
    assert plan.add_items == []


@pytest.mark.parametrize(
    ("asin", "option"),
    [
        (HALF_ASIN, "2-sided Printing: 2 Half Walls"),
        (HALF_ASIN, "2-sided Printing: 1 Full Wall"),
        (FULL_ASIN, "2-sided Printing: 2 Full Walls"),
        (FULL_ASIN, "2-sided Printing: 1 Half Wall"),
    ],
)
def test_wall_only_customization_is_validated_per_unit(tmp_path, asin, option):
    payload = _wall_json(asin, 3, option)
    lines, _ = build_order_folder_lines_from_json(
        amazon_order_items=[_amazon_item(payload)],
        customization_items=[parse_customization_json_info(payload)],
    )
    result = _folder_preview(tmp_path / "orders", lines)
    assert result.status == "folder_rule_missing"
    assert result.missing_rule_title == "Double-sided Printing Options"
    assert result.missing_rule_value == option
    assert not (tmp_path / "orders").exists()


def test_same_asin_rows_keep_separate_quantities_and_printing_options():
    double = _wall_json(HALF_ASIN, 2, "2-sided Printing: 1 Half Wall")
    single = _wall_json(HALF_ASIN, 3, "1-sided Printing: 1 Half Wall", item_id="single-row")
    lines, warnings = build_order_folder_lines_from_json(
        amazon_order_items=[_amazon_item(double), _amazon_item(single)],
        customization_items=[
            parse_customization_json_info(single),
            parse_customization_json_info(double),
        ],
    )
    assert warnings == []
    assert [(line.order_item_id, line.quantity) for line in lines] == [(ITEM_ID, 2), ("single-row", 3)]
    assert build_order_folder_components_from_lines(
        platform_order_no=ORDER, order_lines=lines, recipient_name="Test Buyer"
    ) == [ORDER, "2双面半高侧墙", "400D面料", "3半高侧墙", "400D面料", "Test Buyer"]


def test_manually_corrected_folder_keeps_design_files_and_uses_correct_sku_quantity(tmp_path):
    existing = build_daily_folder(tmp_path, date(2026, 9, 5)) / f"{ORDER}+2个双面半高侧墙+400D面料+Test Buyer"
    existing.mkdir(parents=True)
    design = existing / "proof.ai"
    design.write_bytes(b"existing design")
    payload = _wall_json(HALF_ASIN, 2, "2-sided Printing: 1 Half Wall")
    lines, _ = build_order_folder_lines_from_json(
        amazon_order_items=[_amazon_item(payload)],
        customization_items=[parse_customization_json_info(payload)],
    )
    result = _folder_preview(tmp_path, lines)
    assert result.status == FOLDER_EXISTING_PLATFORM_ORDER
    assert Path(result.folder_path) == existing
    assert result.folder_components == [ORDER, "2双面半高侧墙", "400D面料", "Test Buyer"]
    assert list(existing.parent.iterdir()) == [existing]
    assert design.read_bytes() == b"existing design"
    plan = build_tent_sku_plan(
        platform_order_no=ORDER,
        system_order_no="test-system-order",
        folder_components=result.folder_components,
        destination_text="United States of America (USA)",
        asin=HALF_ASIN,
        order_lines=lines,
    )
    assert plan.manual_required is False, plan.manual_reason
    assert [(item.sku, item.quantity) for item in plan.replace_main_items] == [
        ("10ft-Half-Wall-Double-Sided", 2)
    ]
    assert plan.add_items == []


def test_tent_packages_keep_partial_double_sided_selection_per_package():
    components = build_order_folder_components(
        platform_order_no=ORDER,
        parent_asin="B0FTV6XDGG",
        asin="B0DZ2W2QWK",
        tent_quantity=3,
        customization_text='''
        Frame Options : Standard 1.6"/40mm square aluminum
        Fabric Material Options : 400D Polyester Fabric
        Side Wall and Rail Options : 1 Full and 2 Half Walls with Rails
        Double-sided Printing Options : 2-sided Printing: 1 Half Wall
        ''',
        recipient_name="Test Buyer",
    )
    assert components == [
        ORDER,
        "3套（3x3m帐篷顶+40mm方形铝+1全高背墙+1双面半高侧墙(带横杆)+1半高侧墙(带横杆)+400D面料）",
        "Test Buyer",
    ]
