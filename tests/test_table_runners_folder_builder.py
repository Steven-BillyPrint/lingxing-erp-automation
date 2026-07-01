from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.table_runners import (
    PRODUCT_TYPE_TABLE_RUNNERS,
    TABLE_RUNNER_CONTACT_PROMPT,
    TABLE_RUNNER_PARENT_TO_CHILD_SIZE,
    find_table_runner_parent_asin,
    get_table_runner_size,
    is_table_runner_asin,
)
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines


MATERIAL_TITLE = "Choose Your Material for the Table Runner"
PROOF_TITLE = "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)"


def _order(platform_order_no: str = "112-0000000-0000000") -> BatchOrderItem:
    """构造桌旗文件夹生成测试所需的订单对象。"""
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-18 10:00:00",
    )


def _line(
    *,
    asin: str,
    quantity: int,
    pairs: dict[str, str],
    order_item_id: str = "table-runner-item-1",
) -> OrderFolderLine:
    """构造桌旗文件夹生成测试所需的订单行对象。"""
    return OrderFolderLine(
        asin=asin,
        sku="table-runner-sku",
        parent_asin=find_table_runner_parent_asin(asin),
        product_type=PRODUCT_TYPE_TABLE_RUNNERS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs,
        order_item_id=order_item_id,
    )


def _folder_name(platform_order_no: str, lines: list[OrderFolderLine], customer_name: str, tmp_path) -> str:
    """生成桌旗文件夹生成测试断言使用的文件夹名。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order(platform_order_no),
        order_lines=lines,
        recipient_name=customer_name,
        payment_time="2026-06-18 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert result.status == "folder_preview"
    return result.folder_name or ""


def test_table_runner_parent_child_mapping_and_catalog():
    """验证桌旗文件夹生成中的桌旗 父子映射并目录场景。"""
    assert TABLE_RUNNER_PARENT_TO_CHILD_SIZE == {
        "B0DL61S1C9": {
            "B0DL6CY8FB": "12x72in",
            "B0DL6F3HMF": "48x72in",
            "B0DL6GL3D3": "24x72in",
            "B0DL6HFD37": "36x72in",
        }
    }
    assert is_table_runner_asin("B0DL6CY8FB")
    assert find_table_runner_parent_asin("B0DL6CY8FB") == "B0DL61S1C9"
    assert get_table_runner_size("B0DL6F3HMF") == "48x72in"

    match = match_supported_product("B0DL6HFD37")
    assert match is not None
    assert match.product_type == PRODUCT_TYPE_TABLE_RUNNERS


def test_table_runner_folder_name_with_material_and_online_proof(tmp_path):
    """验证桌旗文件夹生成中的桌旗 文件夹名 带有材质并在线确认稿确认稿场景。"""
    line = _line(
        asin="B0DL6CY8FB",
        quantity=1,
        pairs={
            MATERIAL_TITLE: "150GSM Poly Fabric, Light & Versatile",
            PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
        },
    )

    assert _folder_name("112-0000000-0000000", [line], "Runner Buyer", tmp_path) == (
        "112-0000000-0000000+1个12x72in桌旗+150g经编布+Runner Buyer+在线检查"
    )


def test_table_runner_pluralized_material_title_matches_existing_alias(tmp_path):
    """验证桌旗文件夹生成中的桌旗 复数化材质标题匹配已存在别名场景。"""
    line = _line(
        asin="B0DL6CY8FB",
        quantity=1,
        pairs={
            "Choose Your Materials for the Table Runner": "150GSM Poly Fabric, Light & Versatile",
            PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
        },
    )

    standard_line = _line(
        asin="B0DL6CY8FB",
        quantity=1,
        pairs={
            MATERIAL_TITLE: "150GSM Poly Fabric, Light & Versatile",
            PROOF_TITLE: "Online Proof (48h No Reply=SHIP)",
        },
    )

    assert _folder_name("112-0000000-0000000", [line], "Runner Buyer", tmp_path) == _folder_name(
        "112-0000000-0000000",
        [standard_line],
        "Runner Buyer",
        tmp_path,
    )


def test_table_runner_material_variants_and_direct_proof(tmp_path):
    """验证桌旗文件夹生成中的桌旗 材质变体并直接确认稿确认稿场景。"""
    lines = [
        _line(
            asin="B0DL6GL3D3",
            quantity=2,
            pairs={MATERIAL_TITLE: "Vinyl Material, NOT fabric", PROOF_TITLE: "Straight to Prod, I checked info/spell"},
            order_item_id="vinyl",
        ),
        _line(
            asin="B0DL6F3HMF",
            quantity=1,
            pairs={MATERIAL_TITLE: "260GSM Poly Fabric, High Dens & Durable"},
            order_item_id="poly260",
        ),
    ]

    assert _folder_name("113-0000000-0000000", lines, "Runner Buyer", tmp_path) == (
        "113-0000000-0000000+2个24x72in桌旗+喷绘布+1个48x72in桌旗+260g经编布+Runner Buyer+直接制作"
    )


def test_table_runner_same_asin_same_options_keep_order_lines(tmp_path):
    """验证桌旗文件夹生成中的桌旗 相同ASIN相同选项保留订单行场景。"""
    pairs = {MATERIAL_TITLE: "150GSM Poly Fabric, Light & Versatile"}
    lines = [
        _line(asin="B0DL6CY8FB", quantity=1, pairs=pairs, order_item_id="a"),
        _line(asin="B0DL6CY8FB", quantity=2, pairs=pairs, order_item_id="b"),
    ]

    assert _folder_name("114-0000000-0000000", lines, "Runner Buyer", tmp_path) == (
        "114-0000000-0000000+1个12x72in桌旗+150g经编布+2个12x72in桌旗+150g经编布+Runner Buyer"
    )


def test_table_runner_single_line_quantity_does_not_mark_different_designs(tmp_path):
    """验证桌旗文件夹生成中的桌旗 单面行数量 不会 标记不同 designs场景。"""
    line = _line(
        asin="B0DL6CY8FB",
        quantity=3,
        pairs={MATERIAL_TITLE: "150GSM Poly Fabric, Light & Versatile"},
        order_item_id="a",
    )

    assert _folder_name("114-0000000-0000000", [line], "Runner Buyer", tmp_path) == (
        "114-0000000-0000000+3个12x72in桌旗+150g经编布+Runner Buyer"
    )


def test_table_runner_same_asin_different_options_do_not_merge(tmp_path):
    """验证桌旗文件夹生成中的桌旗 相同ASIN不同选项 不会 合并场景。"""
    lines = [
        _line(asin="B0DL6CY8FB", quantity=1, pairs={MATERIAL_TITLE: "150GSM Poly Fabric, Light & Versatile"}, order_item_id="a"),
        _line(asin="B0DL6CY8FB", quantity=1, pairs={MATERIAL_TITLE: "260GSM Poly Fabric, High Dens & Durable"}, order_item_id="b"),
    ]

    assert _folder_name("701-0000000-0000000", lines, "Runner Buyer", tmp_path) == (
        "701-0000000-0000000+1个12x72in桌旗+150g经编布+1个12x72in桌旗+260g经编布+Runner Buyer"
    )


def test_table_runner_unknown_material_returns_product_status(tmp_path):
    """验证桌旗文件夹生成中的桌旗 未知材质返回产品状态场景。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order(),
        order_lines=[_line(asin="B0DL6CY8FB", quantity=1, pairs={MATERIAL_TITLE: "Mystery Fabric"})],
        recipient_name="Runner Buyer",
        payment_time="2026-06-18 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "table_runners_rule_missing"
    assert result.missing_rule_title == MATERIAL_TITLE
    assert result.missing_rule_value == "Mystery Fabric"


def test_table_runner_contact_prompt_uses_json_value():
    """验证桌旗文件夹生成中的桌旗 联系方式提示 使用JSON值场景。"""
    info = CustomizationJsonInfo(
        order_id="112-0000000-0000000",
        order_item_id="runner-item",
        asin="B0DL6CY8FB",
        title=None,
        quantity=1,
        pairs={TABLE_RUNNER_CONTACT_PROMPT: "555-222-3333 runner@example.com"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]
    assert contact.phone == "5552223333"
    assert contact.email == "runner@example.com"
