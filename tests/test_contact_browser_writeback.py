from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.flows import contact_sync
from lingxing_automation.flows.contact_sync import (
    CustomOrderApiContext,
    CustomOrderInteractionPolicy,
)
from lingxing_automation.pages import order_detail_writeback
from lingxing_automation.models import (
    BatchOrderItem,
    ContactInfo,
    FolderBuildResult,
    OrderCustomZipBundle,
    OrderFolderLine,
)
from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityResult,
)


PLATFORM_ORDER_NO = "113-4350021-2746656"
SYSTEM_ORDER_NO = "103722290181226257"


class ApiOperationsThatRejectPhoneUse:
    async def update_phone(self, **_kwargs):
        raise AssertionError("定制订单联系方式阶段不得调用电话 updateOrder API")


class _FakePage:
    async def wait_for_timeout(self, _milliseconds):
        return None


def test_contact_save_must_survive_closing_and_reopening_detail(monkeypatch):
    contact = ContactInfo("5025004101", "splendidvlouisville@gmail.com", 1, "both")
    events: list[str] = []
    reads = iter(
        [
            {"phone": "+1 619-854-2705 ext. 01508", "email": "masked@marketplace.amazon.com"},
            {"phone": "5025004101", "email": "splendidvlouisville@gmail.com"},
        ]
    )

    async def identity(*_args, **_kwargs):
        return {"system_order_no": SYSTEM_ORDER_NO}

    async def no_op(*_args, **_kwargs):
        return None

    async def read_values(_page):
        return next(reads)

    async def fill_field(_page, _field, _value):
        return True

    async def save(_page):
        events.append("save")
        return True

    async def close(_page):
        events.append("close")

    async def reopen(_page, _system_order_no):
        events.append("reopen")

    async def wait_persisted(_page, _contact):
        events.append("persisted-read")
        return (
            {
                "phone": "+1 619-854-2705 ext. 01508",
                "email": "masked@marketplace.amazon.com",
            },
            "保存后电话校验失败",
        )

    async def confirm(_context):
        return True

    monkeypatch.setattr(order_detail_writeback, "assert_current_detail_order", identity)
    monkeypatch.setattr(order_detail_writeback, "try_open_edit_mode", no_op)
    monkeypatch.setattr(order_detail_writeback, "read_shipping_contact_values", read_values)
    monkeypatch.setattr(order_detail_writeback, "fill_shipping_contact_field", fill_field)
    monkeypatch.setattr(order_detail_writeback, "click_save_button", save)
    monkeypatch.setattr(order_detail_writeback, "close_order_detail_dialog", close)
    monkeypatch.setattr(order_detail_writeback, "click_system_order", reopen)
    monkeypatch.setattr(order_detail_writeback, "wait_for_detail", no_op)
    monkeypatch.setattr(
        order_detail_writeback,
        "wait_for_saved_contact_values",
        wait_persisted,
    )

    saved, message = asyncio.run(
        order_detail_writeback.update_current_detail_contact(
            _FakePage(),
            contact,
            expected_system_order_no=SYSTEM_ORDER_NO,
            expected_platform_order_no=PLATFORM_ORDER_NO,
            confirm_callback=confirm,
        )
    )

    assert saved is False
    assert "重新打开订单后持久化校验失败" in message
    assert events == ["save", "close", "reopen", "persisted-read", "close"]


def test_contact_false_save_stops_before_close_and_reopen_validation(monkeypatch):
    contact = ContactInfo("5514970464", None, 1, "phone")
    events: list[str] = []
    reads = iter(
        [
            {"phone": "+1 210-728-4548", "email": ""},
            {"phone": "5514970464", "email": ""},
        ]
    )

    async def identity(*_args, **_kwargs):
        return {"system_order_no": SYSTEM_ORDER_NO}

    async def no_op(*_args, **_kwargs):
        return None

    async def read_values(_page):
        return next(reads)

    async def fill_field(_page, _field, _value):
        return True

    async def save(_page):
        events.append("save")
        raise RuntimeError("保存按钮点击后未生效：联系方式表单仍处于编辑状态")

    async def close(_page):
        events.append("close")

    async def reopen(*_args, **_kwargs):
        events.append("reopen")

    async def confirm(_context):
        return True

    monkeypatch.setattr(order_detail_writeback, "assert_current_detail_order", identity)
    monkeypatch.setattr(order_detail_writeback, "try_open_edit_mode", no_op)
    monkeypatch.setattr(order_detail_writeback, "read_shipping_contact_values", read_values)
    monkeypatch.setattr(order_detail_writeback, "fill_shipping_contact_field", fill_field)
    monkeypatch.setattr(order_detail_writeback, "click_save_button", save)
    monkeypatch.setattr(order_detail_writeback, "close_order_detail_dialog", close)
    monkeypatch.setattr(order_detail_writeback, "click_system_order", reopen)

    saved, message = asyncio.run(
        order_detail_writeback.update_current_detail_contact(
            _FakePage(),
            contact,
            expected_system_order_no=SYSTEM_ORDER_NO,
            expected_platform_order_no=PLATFORM_ORDER_NO,
            confirm_callback=confirm,
        )
    )

    assert saved is False
    assert "表单仍处于编辑状态" in message
    assert events == ["save", "close"]


def _interaction_policy(
    events: list[tuple],
    *,
    confirm: bool = True,
    guard: bool = True,
) -> CustomOrderInteractionPolicy:
    async def confirm_writeback(context):
        events.append(("confirm", context["phone"], context["email"]))
        return confirm

    async def choose_contact(_platform, _system, contacts):
        events.append(("choose", len(contacts)))
        return contacts[0]

    async def runtime_guard(stage, platform, system):
        events.append(("guard", stage, platform, system))
        return guard

    async def capture_contact(platform, system, recipient_name, contact):
        events.append(
            (
                "capture",
                platform,
                system,
                recipient_name,
                contact.phone,
                contact.email,
            )
        )
        return True

    async def approve(*_args):
        return True

    return CustomOrderInteractionPolicy(
        confirm_writeback=confirm_writeback,
        confirm_folder_creation=approve,
        confirm_sku_plan=approve,
        confirm_manual_sku_done=approve,
        confirm_package_split_plan=approve,
        confirm_manual_package_split_done=approve,
        choose_contact=choose_contact,
        runtime_write_guard=runtime_guard,
        capture_notification_contact=capture_contact,
    )


def _patch_order_context(monkeypatch, contact: ContactInfo, *, web_saved: bool = True):
    async def no_op(*_args, **_kwargs):
        return None

    async def fill_search(*_args, **_kwargs):
        return {"search_validation_ok": True}

    async def find_order(*_args, **_kwargs):
        return [SYSTEM_ORDER_NO]

    async def collect_context(*_args, **_kwargs):
        return {
            "amazon_quantity_result": AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_RESOLVED,
                platform_order_no=PLATFORM_ORDER_NO,
                quantity=1,
            ),
            "zip_bundle": OrderCustomZipBundle(PLATFORM_ORDER_NO, status="ok"),
            "recipient_name": "Test Buyer",
            "order_lines": [
                OrderFolderLine(
                    asin="B0TESTCONTACT",
                    sku="test-sku",
                    parent_asin=None,
                    product_type="tent",
                    quantity=1,
                    customization_text="contact test",
                )
            ],
            "order_line_warnings": [],
            "order_line_error": None,
        }

    web_calls: list[ContactInfo] = []

    async def update_web(_page, selected, **kwargs):
        web_calls.append(selected)
        assert kwargs["expected_system_order_no"] == SYSTEM_ORDER_NO
        assert kwargs["expected_platform_order_no"] == PLATFORM_ORDER_NO
        confirmed = await kwargs["confirm_callback"](
            {
                "phone": selected.phone,
                "email": selected.email,
                "before_values": {},
                "after_fill_values": {
                    "phone": selected.phone,
                    "email": selected.email,
                },
            }
        )
        if not confirmed:
            return False, "用户取消或运行时急停阻止网页保存"
        return web_saved, "网页保存并读回成功" if web_saved else "网页保存后读回不一致"

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", no_op)
    monkeypatch.setattr(contact_sync, "fill_order_search", fill_search)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", find_order)
    monkeypatch.setattr(contact_sync, "click_system_order", no_op)
    monkeypatch.setattr(contact_sync, "wait_for_detail", no_op)
    monkeypatch.setattr(contact_sync, "assert_current_detail_order", no_op)
    monkeypatch.setattr(contact_sync, "collect_order_folder_json_context", collect_context)
    monkeypatch.setattr(contact_sync, "read_detail_shipping_address_text", no_op)
    monkeypatch.setattr(
        contact_sync,
        "extract_contact_candidates_from_json_items",
        lambda _items: [contact],
    )
    monkeypatch.setattr(contact_sync, "update_current_detail_contact", update_web)
    monkeypatch.setattr(
        contact_sync,
        "build_and_create_order_folder_from_lines",
        lambda **_kwargs: FolderBuildResult(status="folder_test_stop", error="stop after contact"),
    )
    return web_calls


@pytest.mark.parametrize(
    "contact",
    [
        ContactInfo("5551234567", None, 1, "phone only"),
        ContactInfo(None, "buyer@example.com", 1, "email only"),
        ContactInfo("5551234567", "buyer@example.com", 1, "phone and email"),
    ],
    ids=("phone-only", "email-only", "phone-and-email"),
)
def test_custom_order_contacts_always_use_browser(monkeypatch, contact):
    web_calls = _patch_order_context(monkeypatch, contact)
    events: list[tuple] = []

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy(events),
        )
    )

    assert web_calls == [contact]
    assert result["contact_writeback_source"] == "browser"
    assert result.get("phone_writeback_source") == ("browser" if contact.phone else None)
    assert result.get("buyer_email_writeback_source") == (
        "browser" if contact.email else None
    )
    assert result["writeback_fields"] == [
        field
        for field, present in (("电话", contact.phone), ("买家邮箱", contact.email))
        if present
    ]
    assert (
        "capture",
        PLATFORM_ORDER_NO,
        SYSTEM_ORDER_NO,
        "Test Buyer",
        contact.phone,
        contact.email,
    ) in events
    assert next(index for index, event in enumerate(events) if event[0] == "capture") < next(
        index for index, event in enumerate(events) if event[0] == "confirm"
    )
    assert ("guard", "contact_browser", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO) in events


def test_api_context_contact_writeback_reuses_verified_detail_without_system_search(
    monkeypatch,
):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)
    search_calls: list[tuple[str, str]] = []
    wait_calls: list[tuple[str, str]] = []

    async def fill_search(_page, order_no, search_kind):
        search_calls.append((order_no, search_kind))
        return {"search_validation_ok": True}

    async def find_order(_page, order_no, search_kind, _timeout):
        wait_calls.append((order_no, search_kind))
        return [SYSTEM_ORDER_NO]

    monkeypatch.setattr(contact_sync, "fill_order_search", fill_search)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", find_order)
    item = BatchOrderItem(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
        product_type="tent",
    )
    api_context = CustomOrderApiContext(
        item=item,
        system_order_nos=(SYSTEM_ORDER_NO,),
        recipient_name="Test Buyer",
        shipping_address_text="United States, CA, Los Angeles 90001",
        shipping_postal_code="90001",
        shipping_postal_source="lingxing_openapi",
    )

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            item,
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy([]),
            api_order_context=api_context,
        )
    )

    assert result["status"] == "updated_folder_failed"
    assert search_calls == [(PLATFORM_ORDER_NO, "platform")]
    assert wait_calls == [(PLATFORM_ORDER_NO, "platform")]
    assert result["contact_browser_search_count"] == 0
    assert result["contact_browser_detail_reused"] is True


def test_contact_writeback_fails_and_closes_when_current_detail_is_lost(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)
    closes: list[str] = []

    async def identity(_page, _system, _platform, stage):
        if stage == "before contact writeback":
            raise RuntimeError("当前订单详情已丢失")
        return {"system_order_no": SYSTEM_ORDER_NO}

    async def close(_page):
        closes.append("close")

    monkeypatch.setattr(contact_sync, "assert_current_detail_order", identity)
    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)

    with pytest.raises(RuntimeError, match="当前订单详情已丢失"):
        asyncio.run(
            contact_sync.process_batch_order_item(
                object(),
                BatchOrderItem(
                    system_order_no=SYSTEM_ORDER_NO,
                    platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                    product_type="tent",
                ),
                object(),
                create_folder=False,
                ignore_payment_window=True,
                write_dedupe=False,
                api_operations=ApiOperationsThatRejectPhoneUse(),
                interaction_policy=_interaction_policy([]),
            )
        )

    assert closes[-1] == "close"


def test_duplicate_product_rows_with_one_system_order_are_not_split(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)

    async def duplicate_rows(*_args, **_kwargs):
        return [SYSTEM_ORDER_NO, f" {SYSTEM_ORDER_NO} ", SYSTEM_ORDER_NO]

    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", duplicate_rows)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy([]),
        )
    )

    assert result["status"] != "split_order_after_search"
    assert result["system_order_nos"] == [SYSTEM_ORDER_NO]


def test_distinct_system_orders_still_stop_as_split(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)

    async def split_rows(*_args, **_kwargs):
        return [SYSTEM_ORDER_NO, "103722290181226258"]

    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", split_rows)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            interaction_policy=_interaction_policy([]),
        )
    )

    assert result["status"] == "split_order_after_search"
    assert result["system_order_nos"] == [
        SYSTEM_ORDER_NO,
        "103722290181226258",
    ]


def test_matching_contact_skips_edit_confirmation_and_save(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    web_calls = _patch_order_context(monkeypatch, contact)
    events: list[tuple] = []

    async def read_current(_page):
        return {"phone": "(555) 123-4567", "email": "BUYER@example.com"}

    monkeypatch.setattr(contact_sync, "read_shipping_contact_values", read_current)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy(events),
        )
    )

    assert web_calls == []
    assert result["contact_write_status"] == "already_current"
    assert result["contact_write_mutated"] is False
    assert result["contact_written_fields"] == []
    assert result["contact_writeback_skip_reason"] == "already_current"
    assert not any(event[0] in {"confirm", "guard"} for event in events)
    assert any(event[0] == "capture" for event in events)


def test_contact_write_only_sends_changed_fields(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    web_calls = _patch_order_context(monkeypatch, contact)
    events: list[tuple] = []

    async def read_current(_page):
        return {"phone": "555-123-4567", "email": "old@example.com"}

    monkeypatch.setattr(contact_sync, "read_shipping_contact_values", read_current)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy(events),
        )
    )

    assert len(web_calls) == 1
    assert web_calls[0].phone is None
    assert web_calls[0].email == "buyer@example.com"
    assert result["contact_write_status"] == "written"
    assert result["contact_write_mutated"] is True
    assert result["contact_written_fields"] == ["买家邮箱"]
    assert ("guard", "contact_browser", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO) in events


def test_processing_reuses_validated_candidate_search_without_second_click(
    monkeypatch,
):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)
    search_clicks: list[str] = []

    async def forbidden_fill(_page, order_no, _kind):
        search_clicks.append(order_no)
        raise AssertionError("候选扫描结果有效时不应再次点击搜索")

    async def search_snapshot(_page):
        return {
            "selectedLabel": "平台单号",
            "searchInputIndex": 4,
            "inputs": [
                {
                    "index": 4,
                    "value": PLATFORM_ORDER_NO,
                    "around": "平台单号",
                    "placeholder": "",
                }
            ],
        }

    async def visible_system_orders(*_args, **_kwargs):
        return [SYSTEM_ORDER_NO]

    async def read_current(_page):
        return {"phone": "5551234567", "email": "buyer@example.com"}

    monkeypatch.setattr(contact_sync, "fill_order_search", forbidden_fill)
    monkeypatch.setattr(contact_sync, "get_order_search_snapshot", search_snapshot)
    monkeypatch.setattr(
        contact_sync,
        "find_system_orders_for_order_no",
        visible_system_orders,
    )
    monkeypatch.setattr(contact_sync, "read_shipping_contact_values", read_current)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            interaction_policy=_interaction_policy([]),
            validated_search_context=contact_sync.ValidatedOrderSearchContext(
                order_no=PLATFORM_ORDER_NO,
                search_kind="platform",
                system_order_nos=(SYSTEM_ORDER_NO,),
                search_meta={"search_validation_ok": True},
                search_duration_ms=125,
            ),
        )
    )

    assert search_clicks == []
    assert result["browser_search_count"] == 1
    assert result["search_meta"]["search_reused"] is True
    assert result["search_meta"]["search_context_reused"] is True


def test_already_written_erp_contact_is_reverified_before_it_can_skip(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    web_calls = _patch_order_context(monkeypatch, contact)
    monkeypatch.setattr(contact_sync, "is_contact_writeback_done", lambda *_args: True)
    events: list[tuple] = []

    async def read_current(_page):
        return {
            "phone": "+1 619-854-2705 ext. 01508",
            "email": "masked@marketplace.amazon.com",
        }

    monkeypatch.setattr(contact_sync, "read_shipping_contact_values", read_current)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            dedupe_path="already-done.json",
            interaction_policy=_interaction_policy(events),
        )
    )

    assert result["contact_writeback_already_done"] is True
    assert result["email"] == "buyer@example.com"
    assert result["phone"] == "5551234567"
    assert web_calls == [contact]
    assert result["contact_write_status"] == "written"
    assert result["contact_writeback_verified"] is True
    assert any(event[0] == "capture" for event in events)


def test_already_written_matching_contact_skips_only_after_fresh_page_read(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    web_calls = _patch_order_context(monkeypatch, contact)
    monkeypatch.setattr(contact_sync, "is_contact_writeback_done", lambda *_args: True)

    async def read_current(_page):
        return {"phone": "(555) 123-4567", "email": "BUYER@example.com"}

    monkeypatch.setattr(contact_sync, "read_shipping_contact_values", read_current)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            dedupe_path="already-done.json",
            interaction_policy=_interaction_policy([]),
        )
    )

    assert web_calls == []
    assert result["contact_write_status"] == "already_current"
    assert result["contact_writeback_skip_reason"] == "already_current"
    assert result["contact_writeback_verified"] is True


def test_browser_readback_failure_does_not_complete_contact_stage(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact, web_saved=False)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy([]),
        )
    )

    assert result["status"] == "needs_manual_save"
    assert result["updated_system_order_nos"] == []
    assert result["message"] == "联系方式保存失败：网页保存后读回不一致"
    assert "完成列表" not in result["message"]
    assert "contact_writeback_recorded" not in result


@pytest.mark.parametrize(
    ("confirm", "guard", "expected_status", "expected_stage"),
    [
        (False, True, "needs_manual_save", None),
        (True, False, "contact_write_blocked", "contact_browser"),
    ],
    ids=("user-rejected", "runtime-emergency-stop"),
)
def test_browser_contact_respects_confirmation_and_runtime_guard(
    monkeypatch,
    confirm,
    guard,
    expected_status,
    expected_stage,
):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact)
    events: list[tuple] = []

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
                product_type="tent",
            ),
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy(events, confirm=confirm, guard=guard),
        )
    )

    assert result["status"] == expected_status
    assert result["updated_system_order_nos"] == []
    if expected_stage is None:
        assert not any(event[0] == "guard" for event in events)
    else:
        assert result["runtime_write_guard_stage"] == expected_stage
