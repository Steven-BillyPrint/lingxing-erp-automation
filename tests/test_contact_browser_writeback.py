from __future__ import annotations

import asyncio

import pytest

from erp_automation.contracts.internal_orders import (
    ContactPatch,
    ContactSnapshot,
    ContactWriteOutcome,
    ContactWriteStatus,
    InternalOrderDetail,
)
from lingxing_automation.flows import contact_sync
from lingxing_automation.flows.contact_sync import (
    CustomOrderApiContext,
    CustomOrderInteractionPolicy,
)
from lingxing_automation.models import (
    BatchOrderItem,
    ContactInfo,
    FolderBuildResult,
    OrderCustomZipBundle,
    OrderFolderLine,
)
from lingxing_automation.pages import order_detail_writeback
from lingxing_automation.services.amazon_order_quantity import (
    AMAZON_QUANTITY_RESOLVED,
    AmazonOrderQuantityResult,
)


PLATFORM_ORDER_NO = "113-4350021-2746656"
SYSTEM_ORDER_NO = "103722290181226257"


class ApiOperationsThatRejectPhoneUse:
    async def update_phone(self, **_kwargs):
        raise AssertionError("定制订单联系方式阶段不得调用公开电话 updateOrder API")


class _FakePage:
    async def wait_for_timeout(self, _milliseconds):
        return None


def test_legacy_dom_writer_still_requires_persisted_reopen_readback(monkeypatch):
    """The retained low-level helper must not claim success on stale DOM."""

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
            {"phone": "+1 619-854-2705 ext. 01508", "email": "masked@marketplace.amazon.com"},
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
    monkeypatch.setattr(order_detail_writeback, "wait_for_saved_contact_values", wait_persisted)

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


def _detail(
    *,
    phone: str | None = "12107284548",
    email: str | None = "old@example.com",
) -> InternalOrderDetail:
    return InternalOrderDetail(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_nos=(PLATFORM_ORDER_NO,),
        recipient_name="Test Buyer",
        address_line1="100 Main St",
        address_line2=None,
        address_line3=None,
        city="Los Angeles",
        state_or_region="CA",
        country_code="US",
        country_name="United States",
        postal_code="90001",
        shipping_address_text="100 Main St, Los Angeles, CA, 90001, United States",
        contact=ContactSnapshot(phone=phone, email=email),
        status="2",
        revision="revision-1",
        request_id="request-1",
    )


class FakeInternalOrderOperations:
    def __init__(
        self,
        detail: InternalOrderDetail,
        *,
        write_status: ContactWriteStatus = ContactWriteStatus.CONFIRMED_APPLIED,
    ) -> None:
        self.detail = detail
        self.write_status = write_status
        self.get_calls: list[tuple[str, str]] = []
        self.write_calls: list[tuple[str, str, ContactPatch, str]] = []

    async def get_order_detail(self, system_order_no, expected_platform_order_no):
        self.get_calls.append((system_order_no, expected_platform_order_no))
        return self.detail

    async def update_contacts(
        self,
        system_order_no,
        expected_platform_order_no,
        patch,
        *,
        expected_revision,
    ):
        self.write_calls.append(
            (system_order_no, expected_platform_order_no, patch, expected_revision)
        )
        completed = self.write_status in {
            ContactWriteStatus.ALREADY_CURRENT,
            ContactWriteStatus.CONFIRMED_APPLIED,
        }
        after = ContactSnapshot(
            phone=patch.phone or self.detail.contact.phone,
            email=(patch.email.casefold() if patch.email else self.detail.contact.email),
        )
        return ContactWriteOutcome(
            status=self.write_status,
            attempted=True,
            before=self.detail.contact,
            after=after if completed else self.detail.contact,
            message=(
                "内部详情复核成功"
                if completed
                else "提交结果未知，等待人工复核"
                if self.write_status is ContactWriteStatus.INCONCLUSIVE
                else "内部接口拒绝修改"
            ),
            request_id="write-request",
            attempts=2,
            waited_seconds=1,
        )


def _interaction_policy(
    events: list[tuple],
    *,
    confirm: bool = True,
    guard: bool = True,
) -> CustomOrderInteractionPolicy:
    async def confirm_writeback(context):
        events.append(("confirm", tuple(context["write_fields"]), context["write_source"]))
        return confirm

    async def choose_contact(_platform, _system, contacts):
        events.append(("choose", len(contacts)))
        return contacts[0]

    async def runtime_guard(stage, platform, system):
        events.append(("guard", stage, platform, system))
        return guard

    async def capture_contact(platform, system, recipient_name, contact):
        events.append(("capture", platform, system, recipient_name, contact.phone, contact.email))
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


def _patch_non_contact_stages(monkeypatch, contact: ContactInfo) -> None:
    async def no_op(*_args, **_kwargs):
        return None

    async def collect_context(*_args, **kwargs):
        assert kwargs["internal_detail"].system_order_no == SYSTEM_ORDER_NO
        return {
            "amazon_quantity_result": AmazonOrderQuantityResult(
                status=AMAZON_QUANTITY_RESOLVED,
                platform_order_no=PLATFORM_ORDER_NO,
                quantity=1,
            ),
            "zip_bundle": OrderCustomZipBundle(PLATFORM_ORDER_NO, status="ok"),
            "recipient_name": "Test Buyer",
            "recipient_name_source": "lingxing_internal_detail",
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

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", no_op)
    monkeypatch.setattr(contact_sync, "collect_order_folder_json_context", collect_context)
    monkeypatch.setattr(
        contact_sync,
        "extract_contact_candidates_from_json_items",
        lambda _items: [contact],
    )
    monkeypatch.setattr(
        contact_sync,
        "build_and_create_order_folder_from_lines",
        lambda **_kwargs: FolderBuildResult(status="folder_test_stop", error="stop after contact"),
    )


def _run_order(
    monkeypatch,
    contact: ContactInfo,
    operations: FakeInternalOrderOperations,
    *,
    events: list[tuple] | None = None,
    confirm: bool = True,
    guard: bool = True,
):
    _patch_non_contact_stages(monkeypatch, contact)
    item = BatchOrderItem(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
        row_text=f"{PLATFORM_ORDER_NO} {SYSTEM_ORDER_NO} B0CRRGTPFH",
        product_type="tent",
    )
    api_context = CustomOrderApiContext(
        item=item,
        system_order_nos=(SYSTEM_ORDER_NO,),
        recipient_name="ignored-openapi-placeholder",
        shipping_address_text="",
        shipping_postal_code=None,
        shipping_postal_source="lingxing_openapi",
    )
    event_sink = events if events is not None else []
    return asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            item,
            object(),
            create_folder=False,
            ignore_payment_window=True,
            write_dedupe=False,
            api_operations=ApiOperationsThatRejectPhoneUse(),
            interaction_policy=_interaction_policy(event_sink, confirm=confirm, guard=guard),
            api_order_context=api_context,
            internal_order_operations=operations,
        )
    )


@pytest.mark.parametrize(
    "contact",
    [
        ContactInfo("5551234567", None, 1, "phone only"),
        ContactInfo(None, "buyer@example.com", 1, "email only"),
        ContactInfo("5551234567", "buyer@example.com", 1, "both"),
    ],
    ids=("phone-only", "email-only", "phone-and-email"),
)
def test_custom_order_contacts_use_only_internal_operations(monkeypatch, contact):
    operations = FakeInternalOrderOperations(_detail())
    events: list[tuple] = []
    result = _run_order(monkeypatch, contact, operations, events=events)

    assert len(operations.write_calls) == 1
    assert result["contact_writeback_source"] == "lingxing_internal_detail"
    assert result.get("phone_writeback_source") == (
        "lingxing_internal_detail" if contact.phone else None
    )
    assert result.get("buyer_email_writeback_source") == (
        "lingxing_internal_detail" if contact.email else None
    )
    assert ("guard", "contact_internal", PLATFORM_ORDER_NO, SYSTEM_ORDER_NO) in events
    assert next(i for i, event in enumerate(events) if event[0] == "capture") < next(
        i for i, event in enumerate(events) if event[0] == "confirm"
    )


def test_api_context_never_searches_opens_or_reads_order_dom(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    operations = FakeInternalOrderOperations(_detail())

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("生产 API 路径不得访问订单列表或详情 DOM")

    for name in (
        "fill_order_search",
        "wait_for_orders_in_list",
        "click_system_order",
        "wait_for_detail",
        "assert_current_detail_order",
    ):
        monkeypatch.setattr(contact_sync, name, forbidden)

    result = _run_order(monkeypatch, contact, operations)

    assert result["browser_search_count"] == 0
    assert result["search_meta"]["search_source"] == "lingxing_openapi"
    assert result["shipping_postal_source"] == "lingxing_internal_detail"
    assert operations.get_calls == [(SYSTEM_ORDER_NO, PLATFORM_ORDER_NO)]


def test_matching_internal_contact_skips_confirmation_and_post(monkeypatch):
    contact = ContactInfo("5551234567", "BUYER@example.com", 1, "both")
    operations = FakeInternalOrderOperations(
        _detail(phone="5551234567", email="buyer@example.com")
    )
    events: list[tuple] = []
    result = _run_order(monkeypatch, contact, operations, events=events)

    assert operations.write_calls == []
    assert result["contact_write_status"] == "already_current"
    assert result["contact_write_mutated"] is False
    assert result["contact_written_fields"] == []
    assert not any(event[0] in {"confirm", "guard"} for event in events)


def test_internal_contact_patch_contains_only_changed_fields(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    operations = FakeInternalOrderOperations(
        _detail(phone="5551234567", email="old@example.com")
    )
    result = _run_order(monkeypatch, contact, operations)

    patch = operations.write_calls[0][2]
    assert patch == ContactPatch(phone=None, email="buyer@example.com")
    assert result["contact_written_fields"] == ["买家邮箱"]
    assert result["contact_write_status"] == "confirmed_applied"


def test_inconclusive_internal_write_requires_manual_review_and_no_completion(monkeypatch):
    contact = ContactInfo("5551234567", "buyer@example.com", 1, "both")
    operations = FakeInternalOrderOperations(
        _detail(),
        write_status=ContactWriteStatus.INCONCLUSIVE,
    )
    result = _run_order(monkeypatch, contact, operations)

    assert len(operations.write_calls) == 1
    assert result["status"] == "needs_manual_save"
    assert result["contact_write_status"] == "contact_write_manual_review"
    assert result["contact_manual_review_required"] is True
    assert result["updated_system_order_nos"] == []
    assert "完成列表" not in result["message"]


@pytest.mark.parametrize(
    ("confirm", "guard", "expected_status", "expected_stage"),
    [
        (False, True, "needs_manual_save", None),
        (True, False, "contact_write_blocked", "contact_internal"),
    ],
    ids=("user-rejected", "runtime-emergency-stop"),
)
def test_internal_contact_respects_confirmation_and_runtime_guard(
    monkeypatch,
    confirm,
    guard,
    expected_status,
    expected_stage,
):
    operations = FakeInternalOrderOperations(_detail())
    events: list[tuple] = []
    result = _run_order(
        monkeypatch,
        ContactInfo("5551234567", "buyer@example.com", 1, "both"),
        operations,
        events=events,
        confirm=confirm,
        guard=guard,
    )

    assert result["status"] == expected_status
    assert operations.write_calls == []
    if expected_stage is None:
        assert not any(event[0] == "guard" for event in events)
    else:
        assert result["runtime_write_guard_stage"] == expected_stage
