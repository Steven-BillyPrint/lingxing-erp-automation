from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.models import ContactInfo, CustomizationJsonInfo
from lingxing_automation.pages import order_detail_writeback as writeback
from lingxing_automation.pages import order_detail_extraction as extraction
from lingxing_automation.parsers.contact import (
    extract_complete_contact_candidates,
    extract_contact_candidates_from_json_items,
    extract_contact_info,
    normalize_fixed_phone_answer,
    normalize_phone,
)
from shipment_automation.notification_domain import normalize_phone as notification_phone


PHONE_TITLE = "Please provide a texting number to confirm customization design and details or for emergencies."
EMAIL_TITLE = "Please provide an email address to confirm customization design and details or for emergencies."
COMBINED_TITLE = "Please provide a Texting Number or Email to contact you for emergencies (low quality image, etc)"


@pytest.mark.parametrize("value", [
    "000000", "0000000000", "000-000-0000", "+1 (000) 000-0000",
    "1111111111", "+1 (999) 999-9999", "1234567890", "0123456789",
    "9876543210", "+1 123-456-7890", "123", "",
])
def test_obvious_placeholders_are_never_usable_for_writeback_or_notifications(value):
    assert normalize_phone(value) is None
    assert normalize_fixed_phone_answer(value) is None
    assert notification_phone(value) is None


@pytest.mark.parametrize("value, expected", [
    ("1212121212", "1212121212"),
    ("123123123", "123123123"),
    ("1234512345", "1234512345"),
    ("+1 (121) 212-1212", "1212121212"),
    ("+1 (469) 835-2508", "4698352508"),
    ("4690001234", "4690001234"),
    ("5555550123", "5555550123"),
])
def test_repeated_groups_and_local_patterns_still_allow_phone_replacement(value, expected):
    assert normalize_phone(value) == expected
    assert normalize_fixed_phone_answer(value) == expected
    if len(expected) == 10:
        assert notification_phone(value) == "+1" + expected


def _json_item(pairs):
    return CustomizationJsonInfo("ORDER", "ITEM", "ASIN", "Tent", 1, pairs)


@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("phone", ["000000", "0000000000", "1234567890", "9876543210"])
def test_invalid_phone_retains_email_and_reason_in_json_and_tooltip_candidates(combined, phone):
    pairs = (
        {COMBINED_TITLE: f"{phone} buyer@example.com"}
        if combined else {PHONE_TITLE: phone, EMAIL_TITLE: "buyer@example.com"}
    )
    reasons = []
    candidates = extract_contact_candidates_from_json_items([_json_item(pairs)], phone_rejections=reasons)
    tooltip = "\n".join(f"{title} : {answer}" for title, answer in pairs.items())
    assert reasons
    for contacts in (candidates, extract_complete_contact_candidates([tooltip])):
        assert len(contacts) == 1
        assert contacts[0].phone is None
        assert contacts[0].email == "buyer@example.com"
        assert contacts[0].phone_rejection_reason


@pytest.mark.parametrize("title", [PHONE_TITLE, COMBINED_TITLE])
def test_invalid_only_contact_is_not_a_candidate_but_reason_is_retained(title):
    reasons = []
    assert extract_contact_candidates_from_json_items(
        [_json_item({title: "0000000000"})], phone_rejections=reasons,
    ) == []
    assert "全零" in reasons[0]
    parsed = extract_contact_info([f"{title} : 0000000000"])
    assert parsed.phone is None
    assert "全零" in parsed.phone_rejection_reason


def test_blank_answer_is_distinct_from_invalid_answer():
    reasons = []
    contacts = extract_contact_candidates_from_json_items(
        [_json_item({PHONE_TITLE: "", EMAIL_TITLE: "buyer@example.com"})], phone_rejections=reasons,
    )
    assert reasons == []
    assert contacts[0].phone is None
    assert contacts[0].phone_rejection_reason is None


def test_placeholder_does_not_compete_with_valid_phone_on_another_item():
    contacts = extract_contact_candidates_from_json_items([
        _json_item({PHONE_TITLE: "0000000000", EMAIL_TITLE: "buyer@example.com"}),
        _json_item({PHONE_TITLE: "1212121212", EMAIL_TITLE: "buyer@example.com"}),
    ])
    assert len(contacts) == 1
    assert contacts[0].phone == "1212121212"
    assert contacts[0].phone_rejection_reason is None


@pytest.fixture
def contact_page(monkeypatch):
    """Exercise the real write/save/reopen orchestration through an in-memory page adapter."""
    class Page:
        def __init__(self):
            self.values = {"phone": "4698352508", "email": "old@example.com"}
            self.events = []
            self.change_phone_on_fill = False
            self.change_phone_on_reopen = False

        async def wait_for_timeout(self, _ms):
            pass

    page = Page()

    async def identity(*_args, **_kwargs):
        return {"system_order_no": "SYSTEM"}

    async def edit(_page):
        page.events.append(("edit",))

    async def read(_page):
        return dict(page.values)

    async def fill(_page, field, value):
        page.events.append(("fill", field, value))
        page.values[field] = value
        if page.change_phone_on_fill:
            page.values["phone"] = "0000000000"
        return True

    async def save(_page):
        page.events.append(("save",))
        return True

    async def cancel(_page):
        page.events.append(("cancel",))
        return True

    async def close(_page):
        page.events.append(("close",))

    async def reopen(_page, _order):
        page.events.append(("reopen",))
        if page.change_phone_on_reopen:
            page.values["phone"] = "0000000000"

    async def wait(*_args):
        pass

    async def confirm(context):
        page.events.append(("confirm", context["phone"]))
        return True

    for name, function in {
        "assert_current_detail_order": identity, "try_open_edit_mode": edit,
        "read_shipping_contact_values": read, "fill_shipping_contact_field": fill,
        "click_save_button": save, "click_cancel_edit_button": cancel,
        "close_order_detail_dialog": close, "click_system_order": reopen, "wait_for_detail": wait,
    }.items():
        monkeypatch.setattr(writeback, name, function)

    def run(phone, email="buyer@example.com"):
        return asyncio.run(writeback.update_current_detail_contact(
            page, ContactInfo(phone, email, 1, "test"),
            expected_system_order_no="SYSTEM", expected_platform_order_no="ORDER",
            confirm_callback=confirm,
        ))

    return page, run


@pytest.mark.parametrize("old_phone", ["4698352508", "0000000000", ""])
@pytest.mark.parametrize("new_phone", ["0000000000", "1234567890", "000000"])
def test_writer_rechecks_unfiltered_input_and_preserves_old_phone_while_saving_email(contact_page, old_phone, new_phone):
    page, run = contact_page
    page.values["phone"] = old_phone
    saved, message = run(new_phone)
    assert saved
    assert "电话已跳过" in message
    assert page.values == {"phone": old_phone, "email": "buyer@example.com"}
    assert "原电话已核验保持不变" in message
    assert [event for event in page.events if event[0] == "fill"] == [("fill", "email", "buyer@example.com")]
    assert ("reopen",) in page.events


@pytest.mark.parametrize("phone", ["1212121212", "123123123", "4690001234"])
def test_writer_accepts_repeated_groups_even_when_original_phone_is_a_placeholder(contact_page, phone):
    page, run = contact_page
    page.values["phone"] = "0000000000"
    saved, _message = run(phone)
    assert saved
    assert page.values["phone"] == phone
    assert ("fill", "phone", phone) in page.events


def test_writer_with_only_an_invalid_phone_never_edits_or_saves(contact_page):
    page, run = contact_page
    saved, message = run("1234567890", email=None)
    assert not saved
    assert "电话已跳过" in message
    assert page.events == [("close",)]


@pytest.mark.parametrize("phase", ["fill", "reopen"])
def test_email_only_write_checks_preserved_phone_before_save_and_after_reopen(contact_page, phase):
    page, run = contact_page
    setattr(page, f"change_phone_on_{phase}", True)
    saved, message = run("0000000000")
    assert not saved
    assert "原电话发生变化" in message
    if phase == "fill":
        assert ("save",) not in page.events
        assert ("cancel",) in page.events
    else:
        assert ("reopen",) in page.events


def test_last_phone_field_guard_never_touches_page_for_a_placeholder():
    assert asyncio.run(writeback.fill_shipping_contact_field(object(), "phone", "1234567890")) is False


@pytest.mark.parametrize("has_valid_later_order", [False, True])
def test_single_order_discovery_preserves_rejection_without_hiding_a_valid_contact(monkeypatch, has_valid_later_order):
    invalid = extract_contact_info([f"{PHONE_TITLE} : 0000000000"])
    valid = ContactInfo("1212121212", "buyer@example.com", 1, "valid source")

    async def extract(_page, system_order_no):
        contact = valid if system_order_no == "VALID" else invalid
        return contact, [contact.source_excerpt]

    monkeypatch.setattr(extraction, "extract_contact_from_system_order", extract)
    orders = ["INVALID", "VALID"] if has_valid_later_order else ["INVALID"]
    source, contact, _texts = asyncio.run(extraction.find_contact_from_system_orders(object(), orders))
    if has_valid_later_order:
        assert source == "VALID"
        assert contact == valid
    else:
        assert source == "INVALID"
        assert contact.phone is None
        assert contact.phone_rejection_reason == "全零占位号码"
