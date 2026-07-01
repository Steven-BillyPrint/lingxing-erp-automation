from datetime import date, datetime
from argparse import Namespace

from lingxing_automation.models import ContactInfo, OrderFolderTask
from lingxing_automation.flows.batch_runtime import get_batch_interval_seconds
from lingxing_automation.parsers.dates import classify_recent_payment_window, latest_payment_text
from lingxing_automation.pages.order_detail_writeback import verify_saved_contact_values
from lingxing_automation.products.tents import (
    EMAIL_PROMPT,
    PHONE_PROMPT,
    extract_asins,
    match_tent_product,
)
from lingxing_automation.services.folder_builder import (
    build_daily_folder,
    build_order_folder_name,
    sanitize_path_part,
)
from lingxing_automation.services.sku_engine import decide_sku
from lingxing_automation.services.split_engine import decide_split
from lingxing_automation.pages.order_search import fill_order_search


def test_folder_builder_sanitizes_and_builds_daily_folder(tmp_path):
    assert sanitize_path_part('bad<>:"/\\|?* name') == "bad - name"
    assert sanitize_path_part("...") == "未命名"

    daily_folder = build_daily_folder(tmp_path, date(2026, 5, 5))
    assert daily_folder == tmp_path / "2026" / "5月" / "0505"

    task = OrderFolderTask(
        platform_order_no="111-2222222-3333333",
        system_order_no="103699451234567890",
        title='10x10 Tent / Blue: "proof"',
    )
    folder_name = build_order_folder_name(task)

    assert "111-2222222-3333333" in folder_name
    assert "103699451234567890" in folder_name
    assert "/" not in folder_name
    assert build_order_folder_name(["111-2222222-3333333", "Name / Team"]).endswith("Name - Team")
    assert '"' not in folder_name


def test_sku_engine_returns_ready_review_or_conflict():
    rules = [
        {"rule_id": "r1", "must_include": ["10x10", "standard"], "sku": "TENT-10X10-STANDARD"},
    ]

    ready = decide_sku("Custom 10x10 Standard canopy", rules)
    assert ready.status == "ready"
    assert ready.sku == "TENT-10X10-STANDARD"
    assert ready.review_required is False

    review = decide_sku("unknown customization", rules)
    assert review.status == "review"
    assert review.review_required is True

    conflict = decide_sku(
        "Custom 10x10 Standard canopy",
        [
            {"rule_id": "r1", "must_include": ["10x10"], "sku": "SKU-A"},
            {"rule_id": "r2", "must_include": ["standard"], "sku": "SKU-B"},
        ],
    )
    assert conflict.status == "conflict"
    assert conflict.review_required is True


def test_order_search_keeps_detail_close_dependency_wired():
    assert callable(fill_order_search.__globals__["close_order_detail_dialog"])


def test_tent_asin_catalog_maps_child_to_parent_and_prompt_order():
    match = match_tent_product("ASIN B0D54Q9L98 SKU canopytents")
    assert match is not None
    assert match.asin == "B0D54Q9L98"
    assert match.parent_asin == "B0CZNZVG26"
    assert match.contact_prompts == (PHONE_PROMPT, EMAIL_PROMPT)

    match = match_tent_product("商品ID B0F5CCG9T5 ASIN B0F5CTQXG1")
    assert match is not None
    assert match.asin == "B0F5CCG9T5"
    assert match.parent_asin == "B0F5CTQXG1"
    assert match.contact_prompts == (EMAIL_PROMPT, PHONE_PROMPT)

    assert match_tent_product("ASIN B09NOTATENT") is None
    assert extract_asins("B0D6XWP8YN B0D6XWP8YN B0D6KZ7G88") == ["B0D6XWP8YN", "B0D6KZ7G88"]


def test_recent_payment_window_requires_payment_label():
    now = datetime(2026, 6, 1, 14, 0, 0)

    assert classify_recent_payment_window("付款 2026-06-01 01:22:14", now=now, hours=24) == "recent"
    assert classify_recent_payment_window("付款 2026-05-30 01:22:14", now=now, hours=24) == "old"
    assert classify_recent_payment_window("订购时间 2026-06-01 01:22:14", now=now, hours=24) == "unknown"
    assert classify_recent_payment_window("付款时间 2026-06-01 03...", now=now, hours=24) == "recent"
    assert latest_payment_text("付款 2026-06-01 01:22:14 付款 2026-06-01 03:00:00") == "2026-06-01 03:00:00"


def test_split_engine_returns_no_split_ready_or_conflict():
    no_match = decide_split({"country": "United States of America", "state": "CA"}, [])
    assert no_match.status == "no_split"
    assert no_match.should_split is False

    ready = decide_split(
        {"country": "United States of America", "state": "CA"},
        [
            {
                "rule_id": "ca-cost-rule",
                "country": "United States of America",
                "state": "CA",
                "should_split": True,
                "target_orders": [{"warehouse": "west"}],
            }
        ],
    )
    assert ready.status == "ready"
    assert ready.should_split is True
    assert ready.target_orders == [{"warehouse": "west"}]

    conflict = decide_split(
        {"country": "United States of America", "state": "CA"},
        [
            {"rule_id": "a", "country": "United States of America", "state": "CA", "should_split": True},
            {"rule_id": "b", "country": "United States of America", "state": "CA", "should_split": False},
        ],
    )
    assert conflict.status == "conflict"
    assert conflict.review_required is True


def test_batch_interval_defaults_to_five_minutes_and_accepts_minute_override():
    assert get_batch_interval_seconds(Namespace(batch_interval_hours=5 / 60)) == 300
    assert get_batch_interval_seconds(Namespace(batch_interval_hours=3, batch_interval_minutes=5)) == 300


def test_saved_contact_verification_requires_matching_phone_and_email():
    contact = ContactInfo(phone="9736340268", email="buyer@example.com", source_count=1, source_excerpt="")

    assert verify_saved_contact_values(contact, {"phone": "973 634 0268", "email": "buyer@example.com"}) is None
    assert "电话校验失败" in (
        verify_saved_contact_values(contact, {"phone": "1111111111", "email": "buyer@example.com"}) or ""
    )
    assert "买家邮箱校验失败" in (
        verify_saved_contact_values(contact, {"phone": "9736340268", "email": "seller@example.com"}) or ""
    )


def test_saved_contact_verification_keeps_unicode_email_prefix():
    contact = ContactInfo(phone=None, email="Ben’s.backflow@icloud.com", source_count=1, source_excerpt="")

    assert verify_saved_contact_values(contact, {"email": "Ben’s.backflow@icloud.com"}) is None
    assert verify_saved_contact_values(contact, {"email": "s.backflow@icloud.com"}) is not None
