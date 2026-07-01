from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.roll_up_banners import (
    DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT,
    DESKTOP_ROLL_UP_BANNER_PHONE_PROMPT,
    PRODUCT_TYPE_ROLL_UP_BANNERS,
    ROLL_UP_BANNER_CONTACT_PROMPT,
    ROLL_UP_BANNER_PROOF_TITLE,
    find_roll_up_banner_parent_asin,
    get_roll_up_banner_fragment,
    is_roll_up_banner_asin,
)
from lingxing_automation.services.customization_parser import parse_customization_pairs
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines


def _order(platform_order_no: str = "112-0000000-0000000") -> BatchOrderItem:
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-22 10:00:00",
    )


def _line(*, asin: str, quantity: int, proof: str | None) -> OrderFolderLine:
    pairs = {}
    if proof is not None:
        pairs[ROLL_UP_BANNER_PROOF_TITLE] = proof
    return OrderFolderLine(
        asin=asin,
        sku="roll-up-sku",
        parent_asin=find_roll_up_banner_parent_asin(asin),
        product_type=PRODUCT_TYPE_ROLL_UP_BANNERS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs,
        order_item_id="roll-up-item-1",
    )


def test_roll_up_banner_parent_child_mapping_and_catalog():
    assert is_roll_up_banner_asin("B0CMPSJCXH")
    assert find_roll_up_banner_parent_asin("B0CMPSJCXH") == "B0CMPYV549"
    assert get_roll_up_banner_fragment("B0CMPSJCXH") == "33x81in豪华易拉宝"

    match = match_supported_product("ASIN B0CMPSJCXH")

    assert match is not None
    assert match.product_type == PRODUCT_TYPE_ROLL_UP_BANNERS
    assert match.parent_asin == "B0CMPYV549"


def test_desktop_roll_up_banner_parent_child_mapping_and_contact_prompts():
    assert is_roll_up_banner_asin("B0D1VB1J31")
    assert find_roll_up_banner_parent_asin("B0D1VB1J31") == "B0D1VB6YF1"
    assert get_roll_up_banner_fragment("B0D1VB1J31") == "11.5x17.5in双面桌面易拉宝"

    match = match_supported_product("ASIN B0D1T9P2PR")

    assert match is not None
    assert match.product_type == PRODUCT_TYPE_ROLL_UP_BANNERS
    assert match.parent_asin == "B0D1TW6RDZ"
    assert match.contact_prompts == (
        DESKTOP_ROLL_UP_BANNER_PHONE_PROMPT,
        DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT,
    )


def test_roll_up_banner_online_proof_folder_name(tmp_path):
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-1111111-1111111"),
        order_lines=[
            _line(
                asin="B0CMPSJCXH",
                quantity=1,
                proof="Online Proof (48h No Reply=SHIP)",
            )
        ],
        recipient_name="Roll Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-1111111-1111111+1个33x81in豪华易拉宝+Roll Buyer+在线检查"


def test_roll_up_banner_direct_proof_folder_name(tmp_path):
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-2222222-2222222"),
        order_lines=[
            _line(
                asin="B0CZLDHF75",
                quantity=2,
                proof="Straight To Production.",
            )
        ],
        recipient_name="Roll Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-2222222-2222222+2个31.5x79in双面易拉宝+Roll Buyer+直接制作"


def test_desktop_roll_up_banner_large_double_sided_online_proof_folder_name(tmp_path):
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-5555555-5555555"),
        order_lines=[
            _line(
                asin="B0D1VB1J31",
                quantity=1,
                proof="Online Proof (48h No Reply=SHIP)",
            )
        ],
        recipient_name="Desktop Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-5555555-5555555+1个11.5x17.5in双面桌面易拉宝+Desktop Buyer+在线检查"


def test_desktop_roll_up_banner_small_direct_proof_folder_name(tmp_path):
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-6666666-6666666"),
        order_lines=[
            _line(
                asin="B0D1V4TXC3",
                quantity=1,
                proof="Straight To Production.",
            )
        ],
        recipient_name="Desktop Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-6666666-6666666+1个8.2x12.5in桌面易拉宝+Desktop Buyer+直接制作"


def test_desktop_roll_up_banner_small_double_sided_missing_proof_skips_tail(tmp_path):
    line = _line(asin="B0D1T9P2PR", quantity=2, proof=None)
    line.customization_pairs = {DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT: "desktop@example.com"}

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-7777777-7777777"),
        order_lines=[line],
        recipient_name="Desktop Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-7777777-7777777+2个8.2x12.5in双面桌面易拉宝+Desktop Buyer"


def test_roll_up_banner_contact_prompt_uses_json_value():
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0CMPSJCXH",
        title=None,
        quantity=1,
        pairs={ROLL_UP_BANNER_CONTACT_PROMPT: "Call 555-111-2222 or email roll@example.com"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "5551112222"
    assert contact.email == "roll@example.com"


def test_desktop_roll_up_banner_contact_prompts_use_json_values():
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0D1VB1J31",
        title=None,
        quantity=1,
        pairs={
            DESKTOP_ROLL_UP_BANNER_PHONE_PROMPT: "+1 555-222-3333",
            DESKTOP_ROLL_UP_BANNER_EMAIL_PROMPT: "desktop@example.com",
        },
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "5552223333"
    assert contact.email == "desktop@example.com"


def test_roll_up_banner_proof_title_is_parsed_from_tooltip_text():
    text = f"""
    {ROLL_UP_BANNER_CONTACT_PROMPT} : roll@example.com
    {ROLL_UP_BANNER_PROOF_TITLE} : Online Proof (48h No Reply=SHIP)
    """

    pairs = parse_customization_pairs(text)

    assert pairs[ROLL_UP_BANNER_PROOF_TITLE] == "Online Proof (48h No Reply=SHIP)"


def test_roll_up_banner_unknown_proof_returns_product_status(tmp_path):
    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-3333333-3333333"),
        order_lines=[_line(asin="B0CMPSJCXH", quantity=1, proof="Send Me A Proof")],
        recipient_name="Roll Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "roll_up_banners_rule_missing"
    assert result.missing_rule_title == ROLL_UP_BANNER_PROOF_TITLE
    assert result.missing_rule_value == "Send Me A Proof"


def test_roll_up_banner_missing_proof_skips_tail_component(tmp_path):
    line = _line(asin="B0CMPSJCXH", quantity=1, proof=None)
    line.customization_pairs = {ROLL_UP_BANNER_CONTACT_PROMPT: "roll@example.com"}

    result = build_and_create_order_folder_from_lines(
        order_item=_order("112-4444444-4444444"),
        order_lines=[line],
        recipient_name="Roll Buyer",
        payment_time="2026-06-22 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "folder_preview"
    assert result.folder_name == "112-4444444-4444444+1个33x81in豪华易拉宝+Roll Buyer"
