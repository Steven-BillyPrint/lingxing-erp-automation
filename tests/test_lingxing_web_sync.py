import json
from datetime import datetime
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lingxing_web_sync import (
    ContactInfo,
    append_contact_writeback_platform_order,
    append_folder_complete_platform_order,
    append_processed_platform_order,
    append_sku_adjustment_platform_order,
    build_writeback_success_message,
    build_writeback_without_processed_message,
    build_batch_candidates_from_rows,
    contact_writeback_fields,
    extract_complete_contact_candidates,
    extract_contact_info,
    guess_search_kind,
    is_contact_writeback_done,
    is_folder_complete,
    is_single_main_sku_order_text,
    is_sku_adjustment_done,
    load_login_config,
    load_contact_writeback_platform_orders,
    load_folder_complete_platform_orders,
    load_processed_platform_orders,
    migrate_dedupe_file,
    missing_contact_fields,
    normalize_fixed_phone_answer,
    normalize_phone,
    parse_env_bool,
    read_lingxing_env,
    validate_search_snapshot,
    write_batch_result,
)
from lingxing_automation.flows.contact_sync import build_payment_source_for_window, print_batch_table_debug
from lingxing_automation.parsers.dates import latest_payment_text


def test_extract_contact_from_custom_more_product_info():
    text = """
    更多商品信息
    Custom Canopy Tent Package Configuration:
    Frame Options : Commercial 1.6"/40mm hexagonal aluminum
    Please provide a texting number to confirm customization design and details or for emergencies. : 4698352508
    Please provide an email address to confirm customization design and details or for emergencies. : annagarcia@prospercdjr.com
    """

    contact = extract_contact_info([text])

    assert contact.phone == "4698352508"
    assert contact.email == "annagarcia@prospercdjr.com"


def test_extract_contact_from_fixed_more_product_info_sentences():
    text = """
    Please provide an email address to confirm customization design and details or for emergencies. : esawchuk@rogers.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 2262373747
    """

    contact = extract_contact_info([text])

    assert contact.phone == "2262373747"
    assert contact.email == "esawchuk@rogers.com"


def test_extract_contact_from_fixed_line_answers_without_seller_fallback():
    text = """
    ec@billyprint.com-AU
    Please provide an email address to confirm customization design and details or for emergencies. - Line 1 : zfischer@luxeliftpllc.com
    Please provide an email address to confirm customization design and details or for emergencies. - Line 2 : bobbi@luxeliftpllc.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 7655058931
    """

    contact = extract_contact_info([text])

    assert contact.phone == "7655058931"
    assert contact.email == "zfischer@luxeliftpllc.com"


def test_fixed_prompt_without_answer_does_not_fall_back_to_seller_email():
    text = """
    ec@billyprint.com-AU
    Please provide an email address to confirm customization design and details or for emergencies. :
    """

    contact = extract_contact_info([text])

    assert contact.phone is None
    assert contact.email is None


def test_complete_contact_candidates_dedupe_identical_pairs():
    texts = [
        """
        Custom Canopy Tent Package Configuration:
        Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 8088537811
        """,
        """
        Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 808-853-7811
        """,
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "8088537811"
    assert candidates[0].email == "buyer@example.com"


def test_fixed_phone_answer_trims_full_page_trailing_digit_noise():
    phone = normalize_fixed_phone_answer("7788954288 0 CA$0.00 商品金额")

    assert phone == "7788954288"


def test_complete_contact_candidates_prefer_tooltip_over_full_page_duplicate():
    texts = [
        """
        系统单号 103708118760357515 显示平台源数据 关闭 编辑
        Please provide an email address to confirm customization design and details or for emergencies. : Mpeppin@expediacruises.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 7788954288 0 CA$0.00
        """,
        """
        Custom Canopy Tent Package Configuration:
        Please provide an email address to confirm customization design and details or for emergencies. : Mpeppin@expediacruises.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 7788954288
        """,
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "7788954288"
    assert candidates[0].email == "Mpeppin@expediacruises.com"
    assert candidates[0].source_excerpt.startswith("Custom Canopy Tent Package Configuration")


def test_complete_contact_candidates_keep_conflicting_pairs_for_user_choice():
    texts = [
        """
        Please provide an email address to confirm customization design and details or for emergencies. : first@example.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 1112223333
        """,
        """
        Please provide an email address to confirm customization design and details or for emergencies. : second@example.com
        Please provide a texting number to confirm customization design and details or for emergencies. : 4445556666
        """,
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert [(item.phone, item.email) for item in candidates] == [
        ("1112223333", "first@example.com"),
        ("4445556666", "second@example.com"),
    ]


def test_complete_contact_candidates_merge_unique_split_fixed_prompts():
    texts = [
        "Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com",
        "Please provide a texting number to confirm customization design and details or for emergencies. : 5551239876",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "5551239876"
    assert candidates[0].email == "buyer@example.com"


def test_contact_candidates_keep_partial_email_for_manual_confirm():
    texts = [
        "Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone is None
    assert candidates[0].email == "buyer@example.com"


def test_contact_candidates_keep_partial_phone_for_manual_confirm():
    texts = [
        "Please provide a texting number to confirm customization design and details or for emergencies. : 5551239876",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "5551239876"
    assert candidates[0].email is None


def test_car_magnet_combined_contact_prompt_extracts_phone_and_email():
    text = """
    Customize Design Left:
    Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc) :
    602-671-6610 buyer@example.com
    Surface Material Option : Standard Vinyl
    """

    candidates = extract_complete_contact_candidates([text])

    assert len(candidates) == 1
    assert candidates[0].phone == "6026716610"
    assert candidates[0].email == "buyer@example.com"


def test_combined_contact_prompt_keeps_email_with_curly_apostrophe():
    text = """
    Customize Design Left:
    Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc) - Line 1 : 7373970043
    Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc) - Line 2 : Ben’s.backflow@icloud.com
    Surface Material Option : Standard Vinyl
    """

    candidates = extract_complete_contact_candidates([text])

    assert len(candidates) == 1
    assert candidates[0].phone == "7373970043"
    assert candidates[0].email == "Ben’s.backflow@icloud.com"


def test_car_magnet_contact_prompt_allows_only_email_or_phone():
    email_only = (
        "Please provide a Texting Number or Email to contact you for emergencies "
        "(low quality image, etc) : buyer@example.com Surface Material Option : Standard Vinyl"
    )
    phone_only = (
        "Please provide a Texting Number or Email to contact you for emergencies "
        "(low quality image, etc) : +1 207-835-4259 Surface Material Option : Standard Vinyl"
    )

    email_candidate = extract_complete_contact_candidates([email_only])[0]
    phone_candidate = extract_complete_contact_candidates([phone_only])[0]

    assert email_candidate.email == "buyer@example.com"
    assert email_candidate.phone is None
    assert phone_candidate.phone == "2078354259"
    assert phone_candidate.email is None


def test_complete_contact_candidates_do_not_merge_conflicting_split_prompts():
    texts = [
        "Please provide an email address to confirm customization design and details or for emergencies. : first@example.com",
        "Please provide an email address to confirm customization design and details or for emergencies. : second@example.com",
        "Please provide a texting number to confirm customization design and details or for emergencies. : 5551239876",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert [(item.phone, item.email) for item in candidates] == [
        (None, "first@example.com"),
        (None, "second@example.com"),
        ("5551239876", None),
    ]


def test_extract_plus_phone_and_email():
    text = """
    Please provide a phone number for delivery questions: +1 (555) 222-3344
    Email Address : ops.team+tent@example.co.uk
    """

    contact = extract_contact_info([text])

    assert contact.phone == "5552223344"
    assert contact.email == "ops.team+tent@example.co.uk"


def test_extract_chinese_labels():
    text = "收货信息 电话：4698352508 买家邮箱：buyer@example.com"

    contact = extract_contact_info([text])

    assert contact.phone == "4698352508"
    assert contact.email == "buyer@example.com"


def test_does_not_use_order_number_as_unlabelled_phone():
    text = "系统单号 103701981938320384 平台单号 111-6622902-4192214"

    contact = extract_contact_info([text])

    assert contact.phone is None
    assert contact.email is None


def test_normalize_phone_limits():
    assert normalize_phone("+1 (469) 835-2508") == "4698352508"
    assert normalize_phone("19258222350") == "9258222350"
    assert normalize_fixed_phone_answer("+19258222350") == "9258222350"
    assert normalize_phone("123") is None


def test_guess_search_kind():
    assert guess_search_kind("103701981938320384", None) == "system"
    assert guess_search_kind("111-6622902-4192214", None) == "platform"
    assert guess_search_kind(None, None) == "visible"


def test_guess_search_kind_rejects_invalid_or_conflicting_input():
    with pytest.raises(ValueError, match="格式无法识别"):
        guess_search_kind("abc-123", None)
    with pytest.raises(ValueError, match="不一致"):
        guess_search_kind("111-6622902-4192214", "system")


def test_read_lingxing_env_supports_comments_blanks_and_quotes(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
# local login
LINGXING_ACCOUNT="worker@example.com"

LINGXING_PASSWORD='secret value'
LINGXING_REMEMBER_LOGIN=false
IGNORED_LINE
""",
        encoding="utf-8",
    )

    values = read_lingxing_env(env_path)

    assert values["LINGXING_ACCOUNT"] == "worker@example.com"
    assert values["LINGXING_PASSWORD"] == "secret value"
    assert values["LINGXING_REMEMBER_LOGIN"] == "false"
    assert "IGNORED_LINE" not in values


def test_missing_account_or_password_disables_auto_login_credentials(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("LINGXING_ACCOUNT=worker@example.com\n", encoding="utf-8")

    config = load_login_config(env_path)

    assert config.account == "worker@example.com"
    assert config.password is None
    assert config.has_credentials is False


def test_remember_login_bool_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LINGXING_ACCOUNT=worker@example.com\nLINGXING_PASSWORD=secret\nLINGXING_REMEMBER_LOGIN=false\n",
        encoding="utf-8",
    )

    config = load_login_config(env_path)

    assert config.has_credentials is True
    assert config.remember_login is False
    assert parse_env_bool("true") is True
    assert parse_env_bool("否") is False
    assert parse_env_bool("unexpected", default=True) is True


def test_missing_contact_fields_requires_phone_and_email():
    complete = ContactInfo(phone="4698352508", email="buyer@example.com", source_count=1, source_excerpt="")
    no_phone = ContactInfo(phone=None, email="buyer@example.com", source_count=1, source_excerpt="")
    no_email = ContactInfo(phone="4698352508", email=None, source_count=1, source_excerpt="")

    assert missing_contact_fields(complete) == []
    assert missing_contact_fields(no_phone) == ["电话"]
    assert missing_contact_fields(no_email) == ["买家邮箱"]


def test_single_main_sku_order_text_filter():
    assert is_single_main_sku_order_text("系统单号 103699 平台单号 111-2222222-3333333 SKU TENT 共1 客户已确认", 1)
    assert not is_single_main_sku_order_text("系统单号 103699 平台单号 111-2222222-3333333 拆分订单 SKU TENT 共1", 1)
    assert not is_single_main_sku_order_text("系统单号 103699 平台单号 111-2222222-3333333 SKU TENT 共2 更多", 1)
    assert not is_single_main_sku_order_text("系统单号 103699 平台单号 111-2222222-3333333 SKU TENT 共1", 2)


def _batch_row(
    *,
    platform_order_no: str = "113-2884580-8642639",
    system_order_no: str = "103707592074036755",
    asin_text: str = "B0D5134SJ3 共1 B0FX9W3MJL 共1",
    sku: str = "canopytents 共1 Tension-Backdrop-7.5x10 共1",
    logistics: str = "Standard",
    tag_text: str = "",
    row_text: str = "",
    paid_at_text: str | None = None,
) -> dict[str, object]:
    paid_at = paid_at_text or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "system_order_no": system_order_no,
        "platform_order_no": platform_order_no,
        "asin_text": asin_text,
        "sku": sku,
        "logistics": logistics,
        "tag_text": tag_text,
        "paid_at_text": paid_at,
        "row_text": row_text or f"{platform_order_no} {system_order_no} {paid_at} {asin_text} {sku} {logistics} {tag_text}",
        "source_page": 1,
        "source_scroll_top": 0,
    }


def test_batch_candidate_hits_unsplit_multi_product_tent_order():
    debug: dict = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [_batch_row()],
        set(),
        payment_window_hours=999999,
        debug=debug,
    )

    assert len(candidates) == 1
    assert candidates[0].platform_order_no == "113-2884580-8642639"
    assert candidates[0].asin == "B0D5134SJ3"
    assert candidates[0].parent_asin == "B0CZNZVG26"
    assert candidates[0].matched_asins == ["B0D5134SJ3", "B0FX9W3MJL"]
    assert candidates[0].logistics == "Standard"


def test_batch_candidate_preserves_expedited_logistics_for_folder_prefix():
    debug: dict = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [
            _batch_row(
                platform_order_no="112-3183165-4090602",
                system_order_no="103710013229475363",
                asin_text="B0CRRGTPFH 共2",
                logistics="Expedited",
            )
        ],
        set(),
        payment_window_hours=999999,
        debug=debug,
    )

    assert len(candidates) == 1
    assert candidates[0].platform_order_no == "112-3183165-4090602"
    assert candidates[0].logistics == "Expedited"
    assert debug["platform_groups"][0]["logistics"] == "Expedited"


def test_batch_candidate_skips_order_with_non_empty_tag():
    debug: dict = {"scan_rows": []}
    rows = [
        _batch_row(
            platform_order_no="112-2338413-2911410",
            system_order_no="103710815566603776",
            asin_text="B0CRRGTPFH 共1",
            tag_text="客户确认中",
        )
    ]

    candidates = build_batch_candidates_from_rows(rows, set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["has_tag"] == 1
    assert debug["platform_groups"][0]["skip_reason"] == "has_tag"
    assert debug["platform_groups"][0]["tag_text"] == "客户确认中"


def test_batch_candidate_accepts_car_magnet_as_supported_product():
    debug: dict = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [
            _batch_row(
                platform_order_no="113-9484835-1608220",
                system_order_no="103710063719859764",
                asin_text="B0CQLN8T6Z 共1",
                row_text="113-9484835-1608220 B0CQLN8T6Z Car-Magnet-12x18in-2pcs",
            )
        ],
        set(),
        payment_window_hours=999999,
        debug=debug,
    )

    assert len(candidates) == 1
    assert candidates[0].asin == "B0CQLN8T6Z"
    assert candidates[0].parent_asin == "B0CNVT6L7Y"
    assert candidates[0].product_type == "car_magnet"


def test_batch_candidate_accepts_vinyl_banner_as_supported_product():
    debug: dict = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [
            _batch_row(
                platform_order_no="701-7802019-2322652",
                system_order_no="103711735910313047",
                asin_text="B0CMQHMQ2T 共1",
                sku="Vinyl-Banners-3x6ft 共1",
                row_text="701-7802019-2322652 B0CMQHMQ2T Vinyl-Banners-3x6ft Standard",
            )
        ],
        set(),
        payment_window_hours=999999,
        debug=debug,
    )

    assert len(candidates) == 1
    assert candidates[0].asin == "B0CMQHMQ2T"
    assert candidates[0].product_type == "vinyl_banners"


def test_batch_candidate_accepts_spandex_8ft_tablecloth_as_supported_product():
    debug: dict = {"scan_rows": []}
    candidates = build_batch_candidates_from_rows(
        [
            _batch_row(
                platform_order_no="114-0980781-7909842",
                system_order_no="103716001760271950",
                asin_text="B0DBGDT7QF 共1",
                sku="Tablecloth-Spandex-8ft 共1",
                row_text="114-0980781-7909842 B0DBGDT7QF Tablecloth-Spandex-8ft Standard",
            )
        ],
        set(),
        payment_window_hours=999999,
        debug=debug,
    )

    assert len(candidates) == 1
    assert candidates[0].asin == "B0DBGDT7QF"
    assert candidates[0].parent_asin == "B0DBG9JWYS"
    assert candidates[0].product_type == "tablecloths"


def test_batch_candidate_skips_tagged_vinyl_banner_but_logs_product_type():
    debug: dict = {"scan_rows": []}
    rows = [
        _batch_row(
            platform_order_no="701-7802019-2322652",
            system_order_no="103711735910313047",
            asin_text="B0CMQHMQ2T 共1",
            sku="Vinyl-Banners-3x6ft 共1",
            tag_text="客户确认中",
            row_text="701-7802019-2322652 B0CMQHMQ2T Vinyl-Banners-3x6ft 客户确认中",
        )
    ]

    candidates = build_batch_candidates_from_rows(rows, set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["has_tag"] == 1
    assert debug["platform_groups"][0]["skip_reason"] == "has_tag"
    assert debug["platform_groups"][0]["product_type"] == "vinyl_banners"
    assert debug["platform_groups"][0]["matched_asin"] == "B0CMQHMQ2T"


def test_batch_candidate_skips_tagged_spandex_8ft_tablecloth_but_logs_product_type():
    debug: dict = {"scan_rows": []}
    rows = [
        _batch_row(
            platform_order_no="114-0980781-7909842",
            system_order_no="103716001760271950",
            asin_text="B0DBGDT7QF 共1",
            sku="Tablecloth-Spandex-8ft 共1",
            tag_text="客户确认中",
            row_text="114-0980781-7909842 B0DBGDT7QF Tablecloth-Spandex-8ft 客户确认中",
        )
    ]

    candidates = build_batch_candidates_from_rows(rows, set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["has_tag"] == 1
    assert debug["platform_groups"][0]["skip_reason"] == "has_tag"
    assert debug["platform_groups"][0]["product_type"] == "tablecloths"
    assert debug["platform_groups"][0]["matched_asin"] == "B0DBGDT7QF"


def test_batch_candidate_skips_same_platform_multiple_system_orders_as_split():
    debug: dict = {"scan_rows": []}
    rows = [
        _batch_row(platform_order_no="114-5730494-1851427", system_order_no="103707647124038656", asin_text="B0CRRGTPFH 共1"),
        _batch_row(platform_order_no="114-5730494-1851427", system_order_no="103707647124038657", asin_text="无商品编码"),
    ]

    candidates = build_batch_candidates_from_rows(rows, set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["split_order"] == 1
    assert debug["platform_groups"][0]["is_split_order"] is True


def test_batch_candidate_skips_row_with_split_marker():
    debug: dict = {"scan_rows": []}
    row = _batch_row(row_text="103707647124038656 114-5730494-1851427 拆分订单 B0CRRGTPFH 共1")

    candidates = build_batch_candidates_from_rows([row], set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["split_order"] == 1


def test_processed_platform_order_dedupe_file(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_processed_platform_order(dedupe_path, "111-2222222-3333333", "103699451234567890")

    processed = load_processed_platform_orders(dedupe_path)
    contact_done = load_contact_writeback_platform_orders(dedupe_path)
    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]

    assert "111-2222222-3333333" in processed
    assert "111-2222222-3333333" in contact_done
    assert "103699451234567890" not in processed
    assert record["system_order_no"] == "103699451234567890"
    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is True


def test_contact_writeback_stage_does_not_mark_final_processed(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_contact_writeback_platform_order(dedupe_path, "111-2222222-3333333", "103699451234567890")

    assert load_processed_platform_orders(dedupe_path) == set()
    assert load_contact_writeback_platform_orders(dedupe_path) == {"111-2222222-3333333"}
    assert is_contact_writeback_done(dedupe_path, "111-2222222-3333333") is True

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is False
    assert record["workflow_status"] == "contact_writeback_complete"


def test_tent_folder_complete_waits_for_sku_adjustment(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_folder_complete_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        product_type="tent",
        sku_adjustment_required=True,
    )

    assert load_processed_platform_orders(dedupe_path) == set()
    assert load_contact_writeback_platform_orders(dedupe_path) == {"111-2222222-3333333"}
    assert load_folder_complete_platform_orders(dedupe_path) == {"111-2222222-3333333"}
    assert is_contact_writeback_done(dedupe_path, "111-2222222-3333333") is True
    assert is_folder_complete(dedupe_path, "111-2222222-3333333") is True
    assert is_sku_adjustment_done(dedupe_path, "111-2222222-3333333") is False

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is True
    assert record["sku_adjustment_required"] is True
    assert record["workflow_status"] == "sku_adjustment_pending"


def test_tent_sku_adjustment_completes_final_processed(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_folder_complete_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        product_type="tent",
        sku_adjustment_required=True,
    )
    append_sku_adjustment_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        sku_status="manual",
    )

    assert load_processed_platform_orders(dedupe_path) == {"111-2222222-3333333"}
    assert is_sku_adjustment_done(dedupe_path, "111-2222222-3333333") is True

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["sku_adjustment_complete"] is True
    assert record["sku_adjustment_status"] == "manual"
    assert record["workflow_status"] == "completed"


def test_non_tent_folder_complete_is_final_without_sku_adjustment(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    append_folder_complete_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        product_type="car_magnets",
        sku_adjustment_required=False,
    )

    assert load_processed_platform_orders(dedupe_path) == {"111-2222222-3333333"}

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is True
    assert "sku_adjustment_required" not in record
    assert "sku_adjustment_complete" not in record
    assert record["workflow_status"] == "completed"


def test_processed_platform_order_loader_accepts_legacy_txt(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.txt"
    dedupe_path.write_text("111-2222222-3333333\t103699451234567890\t2026-06-03 10:00:00\n", encoding="utf-8")

    processed = load_processed_platform_orders(dedupe_path)

    assert processed == {"111-2222222-3333333"}


def test_processed_platform_order_migrates_legacy_json_records(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    dedupe_path.write_text(
        json.dumps(
            {
                "version": 1,
                "orders": {
                    "111-2222222-3333333": {
                        "platform_order_no": "111-2222222-3333333",
                        "system_order_no": "103699451234567890",
                        "processed_at": "2026-06-03 10:00:00",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrate_dedupe_file(dedupe_path)
    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]

    assert payload["version"] == 3
    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is True
    assert load_processed_platform_orders(dedupe_path) == {"111-2222222-3333333"}


def test_processed_platform_order_migrates_legacy_contact_stage_map(tmp_path):
    dedupe_path = tmp_path / "processed_platform_orders.json"
    dedupe_path.write_text(
        json.dumps(
            {
                "version": 2,
                "orders": {},
                "contact_writeback_orders": {
                    "111-2222222-3333333": {
                        "platform_order_no": "111-2222222-3333333",
                        "system_order_no": "103699451234567890",
                        "contact_status": "written",
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrate_dedupe_file(dedupe_path)
    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]

    assert record["contact_writeback_complete"] is True
    assert record["folder_complete"] is False
    assert load_processed_platform_orders(dedupe_path) == set()
    assert load_contact_writeback_platform_orders(dedupe_path) == {"111-2222222-3333333"}


def test_write_batch_result_compacts_candidate_debug(tmp_path):
    payload = {
        "status": "completed",
        "items": [],
        "candidate_debug": {
            "scan_log_file": "logs/batch_scan_demo.json",
            "detected_headers": ["系统单号", "平台单号", "付款时间", "ASIN/商品ID"],
            "column_indexes": {"platform": 1, "payment": 8, "asin": 9},
            "scan_summary": {"read_total_unique_rows": 100, "candidate_count": 2},
            "skip_counts": {"not_tent_asin": 98},
            "warnings": [],
            "orders_to_update": [{"platform_order_no": "112-3570266-4393830"}],
            "scan_rows": [{"row": index, "platform_order_no": str(index)} for index in range(100)],
            "platform_groups": [{"platform_order_no": str(index)} for index in range(100)],
        },
    }

    result_path = Path(write_batch_result(tmp_path, payload))
    data = json.loads(result_path.read_text(encoding="utf-8"))

    assert "candidate_debug" not in data
    assert "candidate_debug" not in payload
    assert data["candidate_debug_summary"]["scan_summary"]["candidate_count"] == 2
    assert data["candidate_debug_summary"]["orders_to_update"] == [{"platform_order_no": "112-3570266-4393830"}]
    assert "scan_rows" not in data["candidate_debug_summary"]
    assert "platform_groups" not in data["candidate_debug_summary"]


def test_print_batch_table_debug_uses_table_format_without_visible_rows(capsys):
    print_batch_table_debug(
        {
            "detected_headers": ["系统单号", "平台单号", "商品", "SKU", "状态", "标签", "付款时间", "ASIN/商品ID", "客选物流"],
            "column_indexes": {"platform": 1, "payment": 6, "asin": 7, "tag": 5, "logistics": 8},
            "scan_summary": {
                "read_total_unique_rows": 50,
                "raw_recent_unprocessed_rows": 0,
                "unique_raw_item_count": 0,
                "candidate_count": 0,
                "covered_recent_threshold": True,
                "warning_count": 0,
            },
            "current_visible_rows": [
                {
                    "row_index": 1,
                    "platform_order_no": "114-8981702-7881824",
                    "paid_at_text": "2026-06-23 16:38:43",
                    "asin": "B0TEST",
                    "sku": "SKU 共1",
                }
            ],
            "orders_to_update": [
                {
                    "platform_order_no": "114-3564418-8113052",
                    "payment_time": "2026-06-23 14:01:47",
                    "asin_or_product_id": "B0DRCT1YYZ",
                }
            ],
        }
    )

    output = capsys.readouterr().out
    assert "[表格识别] 当前订单表头：" in output
    assert "1: 平台单号" in output
    assert "[列索引]" in output
    assert "平台单号列 = 1" in output
    assert "付款时间列 = 6" in output
    assert "ASIN/商品ID列 = 7" in output
    assert "标签列 = 5" in output
    assert "客选物流列 = 8" in output
    assert "[需要修改订单 list]" in output
    assert '"platform_order_no": "114-3564418-8113052"' in output
    assert '"payment_time": "2026-06-23 14:01:47"' in output
    assert "[批量扫描]" not in output
    assert "[当前可见行读取]" not in output
    assert "row=1" not in output
    assert "read_total_unique_rows" not in output
    assert '"candidate_count"' not in output


def test_batch_payment_source_uses_chinese_payment_label():
    source = build_payment_source_for_window("2026-06-23 14:01:47", "row text without date")

    assert source == "付款时间 2026-06-23 14:01:47"
    assert latest_payment_text(source) == "2026-06-23 14:01:47"


def test_partial_contact_writeback_is_treated_as_processed_success():
    contact = ContactInfo(
        phone="8027542228",
        email=None,
        source_excerpt="fixed prompt phone only",
        source_count=1,
    )

    assert contact_writeback_fields(contact) == ["电话"]
    message = build_writeback_success_message(contact)

    assert "成功写回：电话" in message
    assert "缺少 买家邮箱" in message
    assert "已加入最终完成列表" in message


def test_folder_failed_writeback_message_does_not_claim_processed():
    contact = ContactInfo(
        phone="8027542228",
        email="buyer@example.com",
        source_excerpt="fixed prompt",
        source_count=1,
    )

    message = build_writeback_without_processed_message(contact)

    assert "已加入联系方式完成列表" in message
    assert "未加入最终完成列表" in message
    assert "已加入最终完成列表" not in message


def test_validate_search_snapshot_rejects_date_input_contamination():
    inputs = [
        {"index": 0, "value": "114-1948180-7433822", "around": "平台单号", "placeholder": ""},
        {"index": 1, "value": "2026-04-29 00:00:00 - 114-1948180-7433822", "around": "订购时间", "placeholder": ""},
    ]

    ok, message = validate_search_snapshot("114-1948180-7433822", "平台单号", "平台单号", inputs, 0)

    assert ok is False
    assert "订购时间" in message


def test_validate_search_snapshot_accepts_exact_search_input():
    inputs = [
        {"index": 0, "value": "114-1948180-7433822", "around": "平台单号", "placeholder": ""},
        {"index": 1, "value": "2026-04-29 00:00:00 - 2026-05-29 23:59:59", "around": "订购时间", "placeholder": ""},
    ]

    ok, message = validate_search_snapshot("114-1948180-7433822", "平台单号", "平台单号", inputs, 0)

    assert ok is True
    assert message == "搜索输入框校验通过。"


def test_batch_patrol_bat_uses_five_minute_interval():
    bat_text = (ROOT / "启动领星批量巡检.bat").read_text(encoding="utf-8")

    assert "--batch-interval-minutes 5" in bat_text
    assert "--batch-interval-hours 3" not in bat_text
