from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.flows import contact_sync
from lingxing_automation.flows.contact_sync import CustomOrderInteractionPolicy
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
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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


def test_already_written_erp_contact_still_reads_and_captures_json(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    web_calls = _patch_order_context(monkeypatch, contact)
    monkeypatch.setattr(contact_sync, "is_contact_writeback_done", lambda *_args: True)
    events: list[tuple] = []

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
    assert web_calls == []
    assert any(event[0] == "capture" for event in events)


def test_browser_readback_failure_does_not_complete_contact_stage(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    _patch_order_context(monkeypatch, contact, web_saved=False)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                system_order_no=SYSTEM_ORDER_NO,
                platform_order_no=PLATFORM_ORDER_NO,
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
    assert "未加入联系方式完成列表" in result["message"]
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
                row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO}",
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
