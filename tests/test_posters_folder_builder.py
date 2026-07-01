from __future__ import annotations

from lingxing_automation.models import BatchOrderItem, CustomizationJsonInfo, OrderFolderLine
from lingxing_automation.parsers.contact import extract_contact_candidates_from_json_items
from lingxing_automation.products.catalog import match_supported_product
from lingxing_automation.products.posters import (
    POSTER_CONTACT_PROMPTS,
    POSTER_PARENT_TO_CHILD_FRAGMENT,
    PRODUCT_TYPE_POSTERS,
    find_poster_parent_asin,
    get_poster_fragment,
    is_poster_asin,
)
from lingxing_automation.services.folder_builder import build_and_create_order_folder_from_lines
from lingxing_automation.services.order_line_matcher import build_order_folder_lines_from_json


PROOF_TITLE = "Proof Option(No reply to Proof within 48 hours means confirmation and shipping will be proceed)."


def _order(platform_order_no: str = "701-1191899-3884229") -> BatchOrderItem:
    return BatchOrderItem(
        system_order_no="103",
        platform_order_no=platform_order_no,
        row_text="",
        paid_at_text="2026-06-17 10:00:00",
    )


def _line(
    *,
    asin: str,
    quantity: int,
    pairs: dict[str, str] | None = None,
    order_item_id: str = "poster-item-1",
) -> OrderFolderLine:
    return OrderFolderLine(
        asin=asin,
        sku="poster-sku",
        parent_asin=find_poster_parent_asin(asin),
        product_type=PRODUCT_TYPE_POSTERS,
        quantity=quantity,
        customization_text="",
        customization_pairs=pairs or {},
        order_item_id=order_item_id,
    )


def _folder_name(platform_order_no: str, lines: list[OrderFolderLine], customer_name: str, tmp_path) -> str:
    result = build_and_create_order_folder_from_lines(
        order_item=_order(platform_order_no),
        order_lines=lines,
        recipient_name=customer_name,
        payment_time="2026-06-17 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert result.status == "folder_preview"
    return result.folder_name or ""


def test_poster_parent_child_mapping_from_excel():
    assert set(POSTER_PARENT_TO_CHILD_FRAGMENT) == {"B0DMVTR5GY", "B0CQNV8JT8"}
    assert len(POSTER_PARENT_TO_CHILD_FRAGMENT["B0DMVTR5GY"]) == 51
    assert len(POSTER_PARENT_TO_CHILD_FRAGMENT["B0CQNV8JT8"]) == 7

    assert is_poster_asin("B0DMW4QRT5")
    assert find_poster_parent_asin("B0DMW4QRT5") == "B0DMVTR5GY"
    assert get_poster_fragment("B0DMW4QRT5") == "8.5x11in照片纸"
    assert get_poster_fragment("B0DMVZHS1K") == "8.5x11in油画布"
    assert get_poster_fragment("B0DMW27MF2") == "11x17in油画布"
    assert get_poster_fragment("B0CQYDT9LQ") == "18x24in可转移贴"

    match = match_supported_product("B0DMW4QRT5")
    assert match is not None
    assert match.product_type == PRODUCT_TYPE_POSTERS


def test_poster_example_with_same_asin_keeps_order_lines_and_proof_after_name(tmp_path):
    lines = [
        _line(asin="B0DMW4QRT5", quantity=1, pairs={PROOF_TITLE: "Straight To Production."}, order_item_id="a"),
        _line(asin="B0DMW4QRT5", quantity=1, pairs={}, order_item_id="b"),
        _line(asin="B0DMVZHS1K", quantity=1, pairs={}, order_item_id="c"),
    ]

    assert _folder_name("701-1191899-3884229", lines, "Ahlam al ghadar", tmp_path) == (
        "701-1191899-3884229+1个8.5x11in照片纸+1个8.5x11in照片纸+"
        "1个8.5x11in油画布+Ahlam al ghadar+直接制作"
    )


def test_poster_example_online_proof(tmp_path):
    line = _line(
        asin="B0DMW27MF2",
        quantity=1,
        pairs={PROOF_TITLE: "Online Proof (48h No Reply=SHIP)"},
    )

    assert _folder_name("111-2869267-2465833", [line], "kenMetz", tmp_path) == (
        "111-2869267-2465833+1个11x17in油画布+kenMetz+在线检查"
    )


def test_poster_same_asin_keeps_order_lines_even_when_pairs_differ(tmp_path):
    lines = [
        _line(asin="B0DMW4QRT5", quantity=1, pairs={PROOF_TITLE: "Straight To Production."}, order_item_id="a"),
        _line(asin="B0DMW4QRT5", quantity=3, pairs={PROOF_TITLE: "Online Proof (48h No Reply=SHIP)"}, order_item_id="b"),
    ]

    assert _folder_name("701-0000000-0000000", lines, "Poster Buyer", tmp_path) == (
        "701-0000000-0000000+1个8.5x11in照片纸+3个8.5x11in照片纸+Poster Buyer+直接制作"
    )


def test_poster_single_line_quantity_does_not_mark_different_designs(tmp_path):
    lines = [
        _line(asin="B0DMW4QRT5", quantity=2, pairs={PROOF_TITLE: "Straight To Production."}, order_item_id="a"),
    ]

    assert _folder_name("702-0504082-2245030", lines, "Koteswararao korrapati", tmp_path) == (
        "702-0504082-2245030+2个8.5x11in照片纸+Koteswararao korrapati+直接制作"
    )


def test_poster_proof_missing_is_skipped_and_unknown_returns_error(tmp_path):
    no_proof = build_and_create_order_folder_from_lines(
        order_item=_order("111-0000000-0000000"),
        order_lines=[_line(asin="B0DMW4QRT5", quantity=1, pairs={})],
        recipient_name="Poster Buyer",
        payment_time="2026-06-17 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert no_proof.status == "folder_preview"
    assert no_proof.folder_name == "111-0000000-0000000+1个8.5x11in照片纸+Poster Buyer"

    unknown = build_and_create_order_folder_from_lines(
        order_item=_order("111-0000000-0000000"),
        order_lines=[_line(asin="B0DMW4QRT5", quantity=1, pairs={PROOF_TITLE: "Mystery Proof"})],
        recipient_name="Poster Buyer",
        payment_time="2026-06-17 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )
    assert unknown.status == "posters_rule_missing"
    assert unknown.missing_rule_title == PROOF_TITLE
    assert unknown.missing_rule_value == "Mystery Proof"


def test_poster_missing_fragment_rule(tmp_path):
    line = _line(asin="B0DMVTR5GY", quantity=1, pairs={})
    result = build_and_create_order_folder_from_lines(
        order_item=_order("111-0000000-0000000"),
        order_lines=[line],
        recipient_name="Poster Buyer",
        payment_time="2026-06-17 10:00:00",
        folder_root=tmp_path,
        create_folder=False,
    )

    assert result.status == "posters_missing_fragment_rule"


def test_poster_contact_prompts_from_json_value():
    both = CustomizationJsonInfo(
        order_id="701",
        order_item_id="1",
        asin="B0DMW4QRT5",
        title=None,
        quantity=1,
        pairs={POSTER_CONTACT_PROMPTS[0]: "555-123-4567 buyer@example.com"},
    )
    contact = extract_contact_candidates_from_json_items([both])[0]
    assert contact.phone == "5551234567"
    assert contact.email == "buyer@example.com"

    phone_only = CustomizationJsonInfo(
        order_id="701",
        order_item_id="2",
        asin="B0DMW4QRT5",
        title=None,
        quantity=1,
        pairs={POSTER_CONTACT_PROMPTS[1]: "555-222-3333"},
    )
    assert extract_contact_candidates_from_json_items([phone_only])[0].phone == "5552223333"

    empty = CustomizationJsonInfo(
        order_id="701",
        order_item_id="3",
        asin="B0DMW4QRT5",
        title=None,
        quantity=1,
        pairs={POSTER_CONTACT_PROMPTS[0]: ""},
    )
    assert extract_contact_candidates_from_json_items([empty]) == []


def test_poster_contact_prompt_splits_phone_slash_email():
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0DMW4QRT5",
        title=None,
        quantity=1,
        pairs={POSTER_CONTACT_PROMPTS[0]: "478-454-7491/tammie.shinholster@wilkinson.k12.ga.us"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "4784547491"
    assert contact.email == "tammie.shinholster@wilkinson.k12.ga.us"


def test_json_contact_prompt_strips_us_country_code_from_phone():
    info = CustomizationJsonInfo(
        order_id="112",
        order_item_id="1",
        asin="B0DMW4QRT5",
        title=None,
        quantity=1,
        pairs={POSTER_CONTACT_PROMPTS[0]: "+19258222350"},
    )

    contact = extract_contact_candidates_from_json_items([info])[0]

    assert contact.phone == "9258222350"
    assert contact.email is None


def test_poster_order_lines_from_json_use_order_item_id():
    amazon_items = [
        {"OrderItemId": "a", "ASIN": "B0DMW4QRT5", "SellerSKU": "poster-a", "QuantityOrdered": 2},
        {"OrderItemId": "b", "ASIN": "B0DMVZHS1K", "SellerSKU": "poster-b", "QuantityOrdered": 1},
    ]
    customization_items = [
        CustomizationJsonInfo("701", "a", "B0DMW4QRT5", None, 1, {PROOF_TITLE: "Straight To Production."}),
        CustomizationJsonInfo("701", "b", "B0DMVZHS1K", None, 1, {}),
    ]

    lines, warnings = build_order_folder_lines_from_json(
        amazon_order_items=amazon_items,
        customization_items=customization_items,
    )

    assert [line.product_type for line in lines] == [PRODUCT_TYPE_POSTERS, PRODUCT_TYPE_POSTERS]
    assert [line.quantity for line in lines] == [2, 1]
    assert warnings == ["quantity_mismatch:a:json=1:amazon=2"]
