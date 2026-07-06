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
    append_package_split_platform_order,
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
    is_package_split_done,
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
    write_batch_scan_log,
)
from lingxing_automation.flows.contact_sync import build_payment_source_for_window, print_batch_table_debug
from lingxing_automation.parsers.dates import latest_payment_text


def test_extract_contact_from_custom_more_product_info():
    """验证领星同步主流程中的提取 联系方式 来自自定义更多产品信息场景。"""
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
    """验证领星同步主流程中的提取 联系方式 来自固定更多产品信息句子场景。"""
    text = """
    Please provide an email address to confirm customization design and details or for emergencies. : esawchuk@rogers.com
    Please provide a texting number to confirm customization design and details or for emergencies. : 2262373747
    """

    contact = extract_contact_info([text])

    assert contact.phone == "2262373747"
    assert contact.email == "esawchuk@rogers.com"


def test_extract_contact_from_fixed_line_answers_without_seller_fallback():
    """验证领星同步主流程中的提取 联系方式 来自固定行答案不依赖卖家兜底场景。"""
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
    """验证领星同步主流程中的固定提示不依赖答案 不会 回退到 到卖家邮箱场景。"""
    text = """
    ec@billyprint.com-AU
    Please provide an email address to confirm customization design and details or for emergencies. :
    """

    contact = extract_contact_info([text])

    assert contact.phone is None
    assert contact.email is None


def test_complete_contact_candidates_dedupe_identical_pairs():
    """验证领星同步主流程中的完成 联系方式候选 去重完全相同选项对场景。"""
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
    """验证领星同步主流程中的固定电话答案裁剪全高 页面 尾部数字噪音场景。"""
    phone = normalize_fixed_phone_answer("7788954288 0 CA$0.00 商品金额")

    assert phone == "7788954288"


def test_complete_contact_candidates_prefer_tooltip_over_full_page_duplicate():
    """验证领星同步主流程中的完成 联系方式候选 优先提示框优于全高 页面 重复场景。"""
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
    """验证领星同步主流程中的完成 联系方式候选 保留冲突选项对用于用户选择场景。"""
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
    """验证领星同步主流程中的完成 联系方式候选 合并唯一拆分固定提示场景。"""
    texts = [
        "Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com",
        "Please provide a texting number to confirm customization design and details or for emergencies. : 5551239876",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "5551239876"
    assert candidates[0].email == "buyer@example.com"


def test_contact_candidates_keep_partial_email_for_manual_confirm():
    """验证领星同步主流程中的联系方式候选 保留部分邮箱用于人工确认场景。"""
    texts = [
        "Please provide an email address to confirm customization design and details or for emergencies. : buyer@example.com",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone is None
    assert candidates[0].email == "buyer@example.com"


def test_contact_candidates_keep_partial_phone_for_manual_confirm():
    """验证领星同步主流程中的联系方式候选 保留部分电话用于人工确认场景。"""
    texts = [
        "Please provide a texting number to confirm customization design and details or for emergencies. : 5551239876",
    ]

    candidates = extract_complete_contact_candidates(texts)

    assert len(candidates) == 1
    assert candidates[0].phone == "5551239876"
    assert candidates[0].email is None


def test_car_magnet_combined_contact_prompt_extracts_phone_and_email():
    """验证领星同步主流程中的汽车磁贴 合并 联系方式提示 提取电话并邮箱场景。"""
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
    """验证领星同步主流程中的合并 联系方式提示 保留邮箱带有弯引号撇号场景。"""
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
    """验证领星同步主流程中的汽车磁贴 联系方式提示 允许仅邮箱或电话场景。"""
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
    """验证领星同步主流程中的完成 联系方式候选 不会 合并冲突拆分提示场景。"""
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
    """验证领星同步主流程中的提取加号电话并邮箱场景。"""
    text = """
    Please provide a phone number for delivery questions: +1 (555) 222-3344
    Email Address : ops.team+tent@example.co.uk
    """

    contact = extract_contact_info([text])

    assert contact.phone == "5552223344"
    assert contact.email == "ops.team+tent@example.co.uk"


def test_extract_chinese_labels():
    """验证领星同步主流程中的提取中文标签场景。"""
    text = "收货信息 电话：4698352508 买家邮箱：buyer@example.com"

    contact = extract_contact_info([text])

    assert contact.phone == "4698352508"
    assert contact.email == "buyer@example.com"


def test_does_not_use_order_number_as_unlabelled_phone():
    """验证领星同步主流程中的不会 使用 订单号 作为无标签电话场景。"""
    text = "系统单号 103701981938320384 平台单号 111-6622902-4192214"

    contact = extract_contact_info([text])

    assert contact.phone is None
    assert contact.email is None


def test_normalize_phone_limits():
    """验证领星同步主流程中的normalize 电话边界场景。"""
    assert normalize_phone("+1 (469) 835-2508") == "4698352508"
    assert normalize_phone("19258222350") == "9258222350"
    assert normalize_fixed_phone_answer("+19258222350") == "9258222350"
    assert normalize_phone("123") is None


def test_guess_search_kind():
    """验证领星同步主流程中的guess 搜索类型场景。"""
    assert guess_search_kind("103701981938320384", None) == "system"
    assert guess_search_kind("111-6622902-4192214", None) == "platform"
    assert guess_search_kind(None, None) == "visible"


def test_guess_search_kind_rejects_invalid_or_conflicting_input():
    """验证领星同步主流程中的guess 搜索类型拒绝无效或冲突输入框场景。"""
    with pytest.raises(ValueError, match="格式无法识别"):
        guess_search_kind("abc-123", None)
    with pytest.raises(ValueError, match="不一致"):
        guess_search_kind("111-6622902-4192214", "system")


def test_read_lingxing_env_supports_comments_blanks_and_quotes(tmp_path):
    """验证领星同步主流程中的读取 lingxing 环境变量支持注释空行并引号场景。"""
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
    """验证领星同步主流程中的缺失账号或密码禁用自动登录账号密码场景。"""
    env_path = tmp_path / ".env"
    env_path.write_text("LINGXING_ACCOUNT=worker@example.com\n", encoding="utf-8")

    config = load_login_config(env_path)

    assert config.account == "worker@example.com"
    assert config.password is None
    assert config.has_credentials is False


def test_remember_login_bool_values(tmp_path):
    """验证领星同步主流程中的记住登录布尔值场景。"""
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
    """验证领星同步主流程中的缺失 联系方式 字段要求电话并邮箱场景。"""
    complete = ContactInfo(phone="4698352508", email="buyer@example.com", source_count=1, source_excerpt="")
    no_phone = ContactInfo(phone=None, email="buyer@example.com", source_count=1, source_excerpt="")
    no_email = ContactInfo(phone="4698352508", email=None, source_count=1, source_excerpt="")

    assert missing_contact_fields(complete) == []
    assert missing_contact_fields(no_phone) == ["电话"]
    assert missing_contact_fields(no_email) == ["买家邮箱"]


def test_single_main_sku_order_text_filter():
    """验证领星同步主流程中的单面主SKU订单文本过滤场景。"""
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
    """构造批量巡检测试所需的订单行文本。"""
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
    """验证领星同步主流程中的批量候选订单 命中未拆单多行产品 帐篷 订单场景。"""
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
    """验证领星同步主流程中的批量候选订单 保留加急物流用于文件夹前缀场景。"""
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


def test_batch_candidate_records_unknown_asins_once_and_keeps_supported_candidate():
    """验证扫描阶段会记录未识别 ASIN，并按本轮全局去重。"""
    debug: dict = {"scan_rows": []}
    rows = [
        _batch_row(
            platform_order_no="112-3183165-4090602",
            system_order_no="103710013229475363",
            asin_text="B0CRRGTPFH 共1 B0ZZZZZZZZ 共1",
            sku="canopytents 共1 Mystery 共1",
        ),
        _batch_row(
            platform_order_no="113-0000000-0000000",
            system_order_no="103710013229475364",
            asin_text="B0ZZZZZZZZ 共1",
            sku="Mystery 共1",
        ),
    ]

    candidates = build_batch_candidates_from_rows(rows, set(), payment_window_hours=999999, debug=debug)

    assert len(candidates) == 1
    assert candidates[0].asin == "B0CRRGTPFH"
    assert debug["skip_counts"]["not_tent_asin"] == 1
    assert debug["unknown_asins"] == [
        {
            "asin": "B0ZZZZZZZZ",
            "platform_order_no": "112-3183165-4090602",
            "system_order_no": "103710013229475363",
            "sku": "canopytents 共1 Mystery 共1",
            "payment_time": rows[0]["paid_at_text"],
            "source_page": 1,
            "source_scroll_top": 0,
        }
    ]
    assert debug["platform_groups"][0]["unknown_asins"] == ["B0ZZZZZZZZ"]
    assert debug["platform_groups"][1]["unknown_asins"] == ["B0ZZZZZZZZ"]


def test_print_batch_table_debug_shows_unknown_asins_deduped(capsys):
    """验证 CMD 扫描摘要会显示去重后的未识别 ASIN。"""
    debug = {
        "detected_headers": ["系统单号", "平台单号", "付款时间", "ASIN/商品ID"],
        "column_indexes": {"platform": 1, "payment": 8, "asin": 9},
        "orders_to_update": [],
        "unknown_asins": [
            {
                "asin": "B0ZZZZZZZZ",
                "platform_order_no": "112-3183165-4090602",
                "system_order_no": "103710013229475363",
                "sku": "Mystery 共1",
                "payment_time": "2026-07-06 10:00:00",
            },
            {
                "asin": "B0ZZZZZZZZ",
                "platform_order_no": "113-0000000-0000000",
                "system_order_no": "103710013229475364",
                "sku": "Mystery 共1",
                "payment_time": "2026-07-06 10:01:00",
            },
        ],
    }

    print_batch_table_debug(debug)

    output = capsys.readouterr().out
    assert "[未识别ASIN]" in output
    assert output.count("B0ZZZZZZZZ") == 1
    assert "平台单号：112-3183165-4090602" in output


def test_batch_candidate_skips_order_with_non_empty_tag():
    """验证领星同步主流程中的批量候选订单 跳过订单带有非空值标签场景。"""
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
    """验证领星同步主流程中的批量候选订单 接受 汽车磁贴 作为支持产品场景。"""
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
    """验证领星同步主流程中的批量候选订单 接受 喷绘横幅 作为支持产品场景。"""
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
    """验证领星同步主流程中的批量候选订单 接受弹力 8ft 桌布 作为支持产品场景。"""
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
    """验证领星同步主流程中的批量候选订单 跳过带标签 喷绘横幅 但记录产品类型场景。"""
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
    """验证领星同步主流程中的批量候选订单 跳过带标签弹力 8ft 桌布 但记录产品类型场景。"""
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
    """验证领星同步主流程中的批量候选订单 跳过相同平台多个系统订单作为拆分场景。"""
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
    """验证领星同步主流程中的批量候选订单 跳过行带有拆分标记场景。"""
    debug: dict = {"scan_rows": []}
    row = _batch_row(row_text="103707647124038656 114-5730494-1851427 拆分订单 B0CRRGTPFH 共1")

    candidates = build_batch_candidates_from_rows([row], set(), payment_window_hours=999999, debug=debug)

    assert candidates == []
    assert debug["skip_counts"]["split_order"] == 1


def test_processed_platform_order_dedupe_file(tmp_path):
    """验证领星同步主流程中的已处理 平台订单 去重文件场景。"""
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
    """验证领星同步主流程中的联系方式 写回阶段 不会 标记最终已处理场景。"""
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
    """验证领星同步主流程中的帐篷 文件夹完成等待用于SKU调整场景。"""
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


def test_tent_sku_adjustment_waits_for_package_split(tmp_path):
    """验证帐篷 SKU 完成后仍需等待拆分包裹阶段。"""
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

    assert load_processed_platform_orders(dedupe_path) == set()
    assert is_sku_adjustment_done(dedupe_path, "111-2222222-3333333") is True
    assert is_package_split_done(dedupe_path, "111-2222222-3333333") is False

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["sku_adjustment_complete"] is True
    assert record["sku_adjustment_status"] == "manual"
    assert record["package_split_required"] is True
    assert record["package_split_complete"] is False
    assert record["workflow_status"] == "package_split_pending"


def test_tent_package_split_completes_final_processed(tmp_path):
    """验证帐篷拆分包裹完成后才进入最终查重。"""
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
        sku_status="auto",
    )
    append_package_split_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        package_status="auto",
        package_required=True,
        system_order_nos=["103700000000000001", "103700000000000002"],
    )

    assert load_processed_platform_orders(dedupe_path) == {"111-2222222-3333333"}
    assert is_package_split_done(dedupe_path, "111-2222222-3333333") is True

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["package_split_required"] is True
    assert record["package_split_complete"] is True
    assert record["package_split_status"] == "auto"
    assert record["package_split_system_order_nos"] == ["103700000000000001", "103700000000000002"]
    assert record["workflow_status"] == "completed"


def test_tent_package_split_not_required_also_completes_final_processed(tmp_path):
    """验证无需拆包的帐篷订单也会完成最终查重。"""
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
        sku_status="auto",
    )
    append_package_split_platform_order(
        dedupe_path,
        "111-2222222-3333333",
        "103699451234567890",
        package_status="not_required",
        package_required=False,
        system_order_nos=[],
    )

    assert load_processed_platform_orders(dedupe_path) == {"111-2222222-3333333"}

    payload = json.loads(dedupe_path.read_text(encoding="utf-8"))
    record = payload["orders"]["111-2222222-3333333"]
    assert record["package_split_required"] is False
    assert record["package_split_complete"] is True
    assert record["workflow_status"] == "completed"


def test_non_tent_folder_complete_is_final_without_sku_adjustment(tmp_path):
    """验证领星同步主流程中的非 帐篷 文件夹完成为最终不依赖SKU调整场景。"""
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
    """验证领星同步主流程中的已处理 平台订单 加载器接受旧格式文本文件场景。"""
    dedupe_path = tmp_path / "processed_platform_orders.txt"
    dedupe_path.write_text("111-2222222-3333333\t103699451234567890\t2026-06-03 10:00:00\n", encoding="utf-8")

    processed = load_processed_platform_orders(dedupe_path)

    assert processed == {"111-2222222-3333333"}


def test_processed_platform_order_migrates_legacy_json_records(tmp_path):
    """验证领星同步主流程中的已处理 平台订单 迁移旧格式JSON记录场景。"""
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
    """验证领星同步主流程中的已处理 平台订单 迁移旧格式 联系方式 阶段映射场景。"""
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
    """验证领星同步主流程中的写入 批量 结果压缩候选调试场景。"""
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
    assert "candidate_debug" in payload
    assert data["candidate_debug_summary"]["scan_summary"]["candidate_count"] == 2
    assert data["candidate_debug_summary"]["orders_to_update"] == [{"platform_order_no": "112-3570266-4393830"}]
    assert "scan_rows" not in data["candidate_debug_summary"]
    assert "platform_groups" not in data["candidate_debug_summary"]


def test_write_batch_scan_log_compacts_debug(tmp_path):
    """验证 batch_scan 日志只保留摘要、候选和少量行定位信息。"""
    debug = {
        "scan_started_at": "2026-07-01 10:00:00",
        "scan_finished_at": "2026-07-01 10:00:05",
        "payment_window_hours": 96,
        "recent_threshold": "2026-06-27 10:00:00",
        "skip_counts": {"already_processed_pre_scan": 10},
        "warnings": ["table shifted"],
        "detected_headers": ["系统单号", "平台单号", "付款时间", "ASIN/商品ID"],
        "column_indexes": {"platform": 1, "payment": 8, "asin": 9},
        "scan_summary": {"read_total_unique_rows": 100, "candidate_count": 1},
        "candidate_count": 1,
        "unknown_asins": [
            {
                "asin": "B0ZZZZZZZZ",
                "platform_order_no": "112-3570266-4393830",
                "system_order_no": "103700000000000000",
                "sku": "sku 共1",
                "payment_time": "2026-07-01 09:00:00",
                "source_page": 1,
                "source_scroll_top": 120,
            }
        ],
        "orders_to_update": [
            {
                "platform_order_no": "112-3570266-4393830",
                "system_order_no": "103700000000000000",
                "payment_time": "2026-07-01 09:00:00",
                "asin_or_product_id": "B0TEST0000",
                "parent_asin": "B0PARENT000",
                "product_type": "tent",
                "sku": "sku 共1",
                "source_page": 1,
                "source_scroll_top": 120,
                "row_text": "very long raw row text " * 80,
            }
        ],
        "scan_rows": [
            {
                "row": 1,
                "platform_order_no": "112-processed",
                "system_order_no": "1031",
                "skip_reason": "already_processed_pre_scan",
                "row_text": "processed row " * 80,
            },
            {
                "row": 2,
                "platform_order_no": "112-hit",
                "system_order_no": "1032",
                "hit": True,
                "row_text": "hit row " * 80,
            },
        ],
        "table_probe": {"huge": ["x" * 1000]},
        "table_candidates": [{"huge": "x" * 1000}],
        "current_visible_rows": [{"row_text": "x" * 1000}],
        "visited_pages": [{"rows": ["x" * 1000]}],
        "payment_sort_attempts": [{"html": "x" * 1000}],
        "selected_table": {
            "index": 0,
            "score": 289,
            "headers": ["系统单号", "平台单号"],
            "column_indexes": {"platform": 1},
            "first_rows": ["x" * 1000],
            "scrollables": [{"x": "y" * 500}],
        },
    }

    result_path = Path(write_batch_scan_log(tmp_path, debug))
    data = json.loads(result_path.read_text(encoding="utf-8"))

    assert debug["scan_log_file"] == str(result_path)
    assert data["scan_log_file"] == str(result_path)
    assert data["scan_summary"]["candidate_count"] == 1
    assert data["orders_to_update"] == [
        {
            "platform_order_no": "112-3570266-4393830",
            "system_order_no": "103700000000000000",
            "payment_time": "2026-07-01 09:00:00",
            "asin_or_product_id": "B0TEST0000",
            "parent_asin": "B0PARENT000",
            "product_type": "tent",
            "sku": "sku 共1",
            "source_page": 1,
            "source_scroll_top": 120,
        }
    ]
    assert data["unknown_asins"] == [
        {
            "asin": "B0ZZZZZZZZ",
            "platform_order_no": "112-3570266-4393830",
            "system_order_no": "103700000000000000",
            "sku": "sku 共1",
            "payment_time": "2026-07-01 09:00:00",
            "source_page": 1,
            "source_scroll_top": 120,
        }
    ]
    assert data["scan_rows"][0]["skip_reason"] == "already_processed_pre_scan"
    assert data["scan_rows"][0]["row_text_preview"].endswith("...")
    assert "table_probe" not in data
    assert "table_candidates" not in data
    assert "current_visible_rows" not in data
    assert "visited_pages" not in data
    assert "payment_sort_attempts" not in data
    assert "first_rows" not in data["selected_table"]
    assert "scrollables" not in data["selected_table"]


def test_write_batch_result_compacts_items(tmp_path):
    """验证 batch_result 成功订单瘦身，失败订单保留定位信息。"""
    payload = {
        "started_at": "2026-07-01 10:00:00",
        "finished_at": "2026-07-01 10:01:00",
        "status": "completed",
        "candidate_count": 2,
        "updated_count": 1,
        "skipped_count": 1,
        "items": [
            {
                "platform_order_no": "112-success",
                "system_order_no": "103-success",
                "status": "updated",
                "message": "已校验平台单号/系统单号并成功写回：" + "成功" * 300,
                "phone": "5551234567",
                "email": "buyer@example.com",
                "writeback_fields": ["电话", "买家邮箱"],
                "folder_status": "folder_created",
                "folder_path": "Z:/folder",
                "custom_zip_status": "custom_zip_moved",
                "customization_pairs": {"1.Frame": "Commercial"},
                "amazon_order_items": [{"ASIN": "B0TEST", "QuantityOrdered": 1, "Huge": "x" * 1000}],
                "custom_zip_files": [{"zip_filename": "a.zip", "zip_path": "logs/a.zip"}],
                "order_folder_lines": [{"asin": "B0TEST", "quantity": 1, "customization_pairs": {"Huge": "x" * 1000}}],
                "folder_components": ["component"] * 20,
                "shipping_address_text": "address " * 300,
                "update_messages": ["保存前后值：" + "x" * 1000],
                "extracted_contacts": [
                    {
                        "system_order_no": "103-success",
                        "phone": "5551234567",
                        "email": "buyer@example.com",
                        "source_excerpt": "excerpt " * 100,
                    }
                ],
            },
            {
                "platform_order_no": "112-failed",
                "system_order_no": "103-failed",
                "status": "updated_folder_failed",
                "message": "文件夹生成失败：vinyl_banners_rule_missing_printed_sides",
                "folder_status": "vinyl_banners_rule_missing_printed_sides",
                "folder_error": "喷绘缺少 Printed Sides 定制选项",
                "folder_missing_rule_title": "Printed Sides",
                "folder_missing_rule_value": "missing",
                "folder_missing_rule_line": "1.Printed Sides = missing",
                "order_line_error": "missing line",
                "custom_zip_status": "ok",
                "custom_zip_error": "zip warning",
                "custom_zip_files": [{"zip_filename": "failed.zip", "zip_path": "logs/failed.zip", "status": "ok"}],
                "customization_pairs": {"1.Printed Sides": "missing"},
                "amazon_quantity_status": "amazon_quantity_error",
                "amazon_quantity_error": "timeout",
                "shipping_address_text": "failed address " * 100,
            },
        ],
    }

    result_path = Path(write_batch_result(tmp_path, payload))
    data = json.loads(result_path.read_text(encoding="utf-8"))

    success = data["items"][0]
    assert success["platform_order_no"] == "112-success"
    assert success["message"].endswith("...")
    assert "customization_pairs" not in success
    assert "amazon_order_items" not in success
    assert "custom_zip_files" not in success
    assert "folder_components" not in success
    assert "shipping_address_text" not in success
    assert success["order_folder_lines"] == [{"asin": "B0TEST", "quantity": 1}]
    assert success["update_messages"][0].endswith("...")
    assert success["extracted_contacts"][0]["source_excerpt"].endswith("...")

    failed = data["items"][1]
    assert failed["folder_missing_rule_title"] == "Printed Sides"
    assert failed["folder_missing_rule_value"] == "missing"
    assert failed["folder_missing_rule_line"] == "1.Printed Sides = missing"
    assert failed["order_line_error"] == "missing line"
    assert failed["custom_zip_error"] == "zip warning"
    assert failed["custom_zip_files"] == [{"zip_filename": "failed.zip", "status": "ok"}]
    assert failed["customization_pair_count"] == 1
    assert failed["shipping_address_text_preview"].endswith("...")
    assert failed["amazon_quantity_error"] == "timeout"


def test_print_batch_table_debug_uses_table_format_without_visible_rows(capsys):
    """验证领星同步主流程中的print 批量 表格调试使用表格格式不依赖可见行场景。"""
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
    """验证领星同步主流程中的批量 付款来源 使用中文付款标签场景。"""
    source = build_payment_source_for_window("2026-06-23 14:01:47", "row text without date")

    assert source == "付款时间 2026-06-23 14:01:47"
    assert latest_payment_text(source) == "2026-06-23 14:01:47"


def test_partial_contact_writeback_is_treated_as_processed_success():
    """验证领星同步主流程中的部分 联系方式 写回为视为作为已处理成功场景。"""
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
    """验证领星同步主流程中的文件夹失败写回消息 不会 声称已处理场景。"""
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
    """验证领星同步主流程中的validate 搜索快照拒绝日期输入框污染场景。"""
    inputs = [
        {"index": 0, "value": "114-1948180-7433822", "around": "平台单号", "placeholder": ""},
        {"index": 1, "value": "2026-04-29 00:00:00 - 114-1948180-7433822", "around": "订购时间", "placeholder": ""},
    ]

    ok, message = validate_search_snapshot("114-1948180-7433822", "平台单号", "平台单号", inputs, 0)

    assert ok is False
    assert "订购时间" in message


def test_validate_search_snapshot_accepts_exact_search_input():
    """验证领星同步主流程中的validate 搜索快照接受精确搜索输入框场景。"""
    inputs = [
        {"index": 0, "value": "114-1948180-7433822", "around": "平台单号", "placeholder": ""},
        {"index": 1, "value": "2026-04-29 00:00:00 - 2026-05-29 23:59:59", "around": "订购时间", "placeholder": ""},
    ]

    ok, message = validate_search_snapshot("114-1948180-7433822", "平台单号", "平台单号", inputs, 0)

    assert ok is True
    assert message == "搜索输入框校验通过。"


def test_batch_patrol_bat_uses_five_minute_interval():
    """验证领星同步主流程中的批量 巡检批处理脚本使用五分钟间隔场景。"""
    bat_text = (ROOT / "启动领星批量巡检.bat").read_text(encoding="utf-8")

    assert "--batch-interval-minutes 5" in bat_text
    assert "--batch-interval-hours 3" not in bat_text
