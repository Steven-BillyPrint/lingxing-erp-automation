from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.x_stands import (
    PRODUCT_TYPE_X_STANDS,
    X_STAND_CONTACT_PROMPT,
    X_STAND_PRINTING_PROCESS_TITLE,
    X_STAND_PROOF_TITLE,
    find_x_stand_parent_asin,
    get_x_stand_fragment,
    is_x_stand_asin,
)
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines


def _order(platform_order_no: str = "112-0000000-0000000") -> BatchOrderItem:
    """构造X 展架文件夹生成测试所需的订单对象。"""
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
    proof: str | None,
    printing_process: str | None = None,
) -> OrderFolderLine:
    """构造X 展架文件夹生成测试所需的订单行对象。"""
    pairs = {}
    if proof is not None:
        pairs[X_STAND_PROOF_TITLE] = proof
    if printing_process is not None:
        pairs[X_STAND_PRINTING_PROCESS_TITLE] = printing_process
    return OrderFolderLine(
        asin=asin,
        sku="x-stand-sku",
        parent_asin=find_x_stand_parent_asin(asin),
        product_type=PRODUCT_TYPE_X_STANDS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs,
        order_item_id="x-stand-item-1",
    )


def test_x_stand_parent_child_mapping_and_catalog():
    """验证X 展架文件夹生成中的展架 父子映射并目录场景。"""
    assert is_x_stand_asin("B0D1FZKVV7")
    assert find_x_stand_parent_asin("B0D1FZKVV7") == "B0CY566Q8C"
    assert get_x_stand_fragment("B0D1FZKVV7") == "24x63inX展架"
    assert is_x_stand_asin("B0CNXBVM34")
    assert find_x_stand_parent_asin("B0CNXBVM34") == "B0CY566Q8C"
    assert get_x_stand_fragment("B0CNXBVM34") == "32x71inX展架"

    match = match_supported_product("ASIN B0D1FZKVV7")

    assert match is not None
    assert match.product_type == PRODUCT_TYPE_X_STANDS
    assert match.parent_asin == "B0CY566Q8C"
    assert match.contact_prompts == (X_STAND_CONTACT_PROMPT,)


def test_x_stand_online_proof_folder_name(tmp_path):
    """验证X 展架文件夹生成中的展架 在线确认稿确认稿 文件夹名场景。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-1111111-1111111"),
        order_lines=[
            _line(
                asin="B0D1FZKVV7",
                quantity=1,
                proof="Online Proof (48h No Reply=SHIP)",
            )
        ],
        recipient_name="X Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-1111111-1111111+1个24x63inX展架+X Buyer+在线检查"


def test_x_stand_water_based_printing_with_proof_folder_name(tmp_path):
    """验证 X 展架水性打印位于规格之后且 Proof 保持在末尾。"""

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-5555555-5555555"),
        order_lines=[
            _line(
                asin="B0D1FZKVV7",
                quantity=1,
                proof="Online Proof (48h No Reply=SHIP)",
                printing_process="Water-based Inkjet Printing",
            )
        ],
        recipient_name="X Buyer",
        payment_time="2026-07-15 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-5555555-5555555+1个24x63inX展架+水性打印+X Buyer+在线检查"


def test_x_stand_uv_printing_normalizes_case_whitespace_and_period(tmp_path):
    """验证 X 展架 UV 打印兼容大小写、连续空格和末尾句点。"""

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-6666666-6666666"),
        order_lines=[
            _line(
                asin="B0CW56CP7M",
                quantity=2,
                proof=None,
                printing_process="  PREMIUM   UV PRINTING.  ",
            )
        ],
        recipient_name="X Buyer",
        payment_time="2026-07-15 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-6666666-6666666+2个32x71inX展架+UV打印+X Buyer"


def test_x_stand_direct_proof_folder_name(tmp_path):
    """验证X 展架文件夹生成中的展架 直接确认稿确认稿 文件夹名场景。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-2222222-2222222"),
        order_lines=[
            _line(
                asin="B0CW56CP7M",
                quantity=2,
                proof="Straight To Production",
            )
        ],
        recipient_name="X Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-2222222-2222222+2个32x71inX展架+X Buyer+直接制作"


def test_x_stand_missing_proof_skips_tail_component(tmp_path):
    """验证X 展架文件夹生成中的展架 缺失确认稿跳过尾部组件场景。"""
    line = _line(asin="B0CW57ZPFN", quantity=1, proof=None)
    line.customization_pairs = {X_STAND_CONTACT_PROMPT: "x@example.com"}

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-3333333-3333333"),
        order_lines=[line],
        recipient_name="X Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-3333333-3333333+1个32x78inX展架+X Buyer"


def test_x_stand_contact_prompt_uses_json_value():
    """验证X 展架文件夹生成中的展架 联系方式提示 使用JSON值场景。"""
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0D1FZKVV7",
        title=None,
        quantity=1,
        pairs={X_STAND_CONTACT_PROMPT: "Call 555-111-2222 or email x@example.com"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "5551112222"
    assert contact.email == "x@example.com"


def test_x_stand_unknown_proof_returns_product_status(tmp_path):
    """验证X 展架文件夹生成中的展架 未知确认稿返回产品状态场景。"""
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-4444444-4444444"),
        order_lines=[_line(asin="B0D1FZKVV7", quantity=1, proof="Send Me A Proof")],
        recipient_name="X Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "x_stands_rule_missing"
    assert result.missing_rule_title == X_STAND_PROOF_TITLE
    assert result.missing_rule_value == "Send Me A Proof"


def test_x_stand_unknown_printing_process_returns_product_status(tmp_path):
    """验证未知打印工艺返回 X 展架专用规则缺失状态。"""

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-7777777-7777777"),
        order_lines=[
            _line(
                asin="B0D1FZKVV7",
                quantity=1,
                proof=None,
                printing_process="Mystery Printing",
            )
        ],
        recipient_name="X Buyer",
        payment_time="2026-07-15 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "x_stands_rule_missing"
    assert result.missing_rule_title == X_STAND_PRINTING_PROCESS_TITLE
    assert result.missing_rule_value == "Mystery Printing"
