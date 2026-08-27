from __future__ import annotations

import asyncio
import copy

import pytest

from erp_automation.contracts.internal_orders import (
    ContactPatch,
    ContactWriteStatus,
)
from erp_automation.integrations.lingxing.internal_orders import (
    InternalOrderProtocolError,
    LingxingInternalOrderClient,
    build_order_edit_payload,
    parse_internal_order_detail,
)


SYSTEM_ORDER = "103737209528929820"
PLATFORM_ORDER = "112-8414446-3065035"


def _detail(*, phone: str = "+1 210-728-4548", email: str = "old@example.com"):
    return {
        "global_order_no": SYSTEM_ORDER,
        "status": 2,
        "order_item_info": [
            {
                "id": 91,
                "pid": 11,
                "quantity": 1,
                "local_sku": "tent-a",
                "local_product_name": "Tent",
                "platform_order_no": PLATFORM_ORDER,
                "platform_order_name": PLATFORM_ORDER,
                "ignored_display_value": "must not leak into edit row",
            }
        ],
        "receive_info": {
            "receiver_name": "Buyer",
            "receiver_mobile": phone,
            "receiver_tel": "+1 210-728-4548",
            "buyer_email": "masked@marketplace.invalid",
            "address_line1": "100 Main St",
            "address_line2": "Unit 2",
            "city": "Austin",
            "state_or_region": "TX",
            "postal_code": "78701",
            "receiver_country_code": "US",
            "receiver_country_name": "United States",
            "do_not_drop": "preserved",
        },
        "buyer_info": {"buyer_name": "Buyer", "buyer_email": email},
        "remark": "keep remark",
        "remark_attachment": ["one"],
        "logistics_info": {
            "wid": 3,
            "warehouse_type": 1,
            "logistics_type_id": 5,
            "first_mile_type_id": 0,
            "first_mile_provider_id": 0,
            "display_only": "ignored",
        },
        "tax_info": {"tax_type": "vat"},
        "tax_list": [{"tax_type": "sales"}],
        "extra_info": {"channel": "amazon"},
        "custom_fields": [{"key": "one", "value": "two"}],
        "sync_pair": True,
        "is_reorder": 0,
        "tags": [{"type": 2, "tag_no": 18}],
    }


def _api_payload(detail=None, *, code=1, request_id="req-1"):
    return {"code": code, "data": detail, "require_id": request_id}


class FakeResponse:
    def __init__(self, payload, *, ok=True, status=200):
        self.payload = payload
        self.ok = ok
        self.status = status

    async def json(self):
        return copy.deepcopy(self.payload)


class FakeRequestContext:
    def __init__(self, gets, *, post=None):
        self.gets = list(gets)
        self.post_response = post or FakeResponse(_api_payload({}, request_id="post-1"))
        self.get_calls = []
        self.post_calls = []

    async def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        value = self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        if isinstance(value, Exception):
            raise value
        return value

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response


def test_parse_detail_exposes_stable_projection_and_normalized_contacts():
    result = parse_internal_order_detail(
        _detail(),
        expected_system_order_no=SYSTEM_ORDER,
        expected_platform_order_no=PLATFORM_ORDER,
        request_id="safe-request-id",
    )

    assert result.system_order_no == SYSTEM_ORDER
    assert result.platform_order_nos == (PLATFORM_ORDER,)
    assert result.recipient_name == "Buyer"
    assert result.postal_code == "78701"
    assert "100 Main St" in result.shipping_address_text
    assert result.contact.phone == "12107284548"
    assert result.contact.email == "old@example.com"
    assert len(result.revision) == 64


def test_parse_detail_rejects_platform_identity_mismatch():
    with pytest.raises(InternalOrderProtocolError, match="平台单号不一致"):
        parse_internal_order_detail(
            _detail(),
            expected_system_order_no=SYSTEM_ORDER,
            expected_platform_order_no="wrong-order",
        )


def test_edit_payload_mutates_only_requested_contact_and_preserves_form_state():
    source = _detail()
    result = build_order_edit_payload(source, ContactPatch(email="NEW@Example.com"))

    assert result["receive_info"]["buyer_email"] == "NEW@Example.com"
    assert result["receive_info"]["receiver_mobile"] == source["receive_info"]["receiver_mobile"]
    assert result["receive_info"]["do_not_drop"] == "preserved"
    assert result["order_item_info"] == [
        {
            "id": 91,
            "pid": 11,
            "quantity": 1,
            "local_sku": "tent-a",
            "local_product_name": "Tent",
        }
    ]
    assert result["logistics_info"]["wid"] == 3
    assert "display_only" not in result["logistics_info"]
    assert result["remark_info"]["remark"] == "keep remark"
    assert result["tag_no"] == 18
    assert result["taxs"] == [{"tax_type": "vat"}, {"tax_type": "sales"}]
    assert source["buyer_info"]["buyer_email"] == "old@example.com"


def test_phone_only_payload_preserves_authoritative_buyer_email():
    result = build_order_edit_payload(
        _detail(),
        ContactPatch(phone="5514970464"),
    )

    assert result["receive_info"]["receiver_mobile"] == "5514970464"
    assert result["receive_info"]["buyer_email"] == "old@example.com"


def test_update_contacts_noop_never_posts():
    context = FakeRequestContext([FakeResponse(_api_payload(_detail()))])
    client = LingxingInternalOrderClient(context, readback_delays_seconds=(0,))
    initial = asyncio.run(client.get_order_detail(SYSTEM_ORDER, PLATFORM_ORDER))

    outcome = asyncio.run(
        client.update_contacts(
            SYSTEM_ORDER,
            PLATFORM_ORDER,
            ContactPatch(phone="1 (210) 728-4548", email="OLD@example.com"),
            expected_revision=initial.revision,
        )
    )

    assert outcome.status is ContactWriteStatus.ALREADY_CURRENT
    assert outcome.completed is True
    assert outcome.attempted is False
    assert context.post_calls == []


def test_update_contacts_posts_once_then_confirms_with_internal_get():
    before = _detail()
    after = _detail(phone="5514970464", email="new@example.com")
    context = FakeRequestContext(
        [
            FakeResponse(_api_payload(before)),
            FakeResponse(_api_payload(before)),
            FakeResponse(_api_payload(before)),
            FakeResponse(_api_payload(after, request_id="readback-2")),
        ],
        post=FakeResponse(_api_payload({}, request_id="post-2")),
    )
    client = LingxingInternalOrderClient(context, readback_delays_seconds=(0, 0))
    initial = asyncio.run(client.get_order_detail(SYSTEM_ORDER, PLATFORM_ORDER))

    outcome = asyncio.run(
        client.update_contacts(
            SYSTEM_ORDER,
            PLATFORM_ORDER,
            ContactPatch(phone="5514970464", email="new@example.com"),
            expected_revision=initial.revision,
        )
    )

    assert outcome.status is ContactWriteStatus.CONFIRMED_APPLIED
    assert outcome.completed is True
    assert outcome.attempted is True
    assert outcome.attempts == 2
    assert len(context.post_calls) == 1
    submitted = context.post_calls[0][1]["data"]
    assert submitted["receive_info"]["receiver_mobile"] == "5514970464"
    assert submitted["receive_info"]["buyer_email"] == "new@example.com"


def test_update_contacts_revision_conflict_never_posts():
    before = _detail()
    changed = _detail(email="someone-else@example.com")
    context = FakeRequestContext(
        [FakeResponse(_api_payload(before)), FakeResponse(_api_payload(changed))]
    )
    client = LingxingInternalOrderClient(context, readback_delays_seconds=(0,))
    initial = asyncio.run(client.get_order_detail(SYSTEM_ORDER, PLATFORM_ORDER))

    outcome = asyncio.run(
        client.update_contacts(
            SYSTEM_ORDER,
            PLATFORM_ORDER,
            ContactPatch(phone="5514970464"),
            expected_revision=initial.revision,
        )
    )

    assert outcome.status is ContactWriteStatus.CONFLICT
    assert outcome.attempted is False
    assert context.post_calls == []


def test_update_contacts_stale_readback_is_inconclusive_and_never_reposts():
    before = _detail()
    context = FakeRequestContext(
        [FakeResponse(_api_payload(before)), FakeResponse(_api_payload(before))]
    )
    client = LingxingInternalOrderClient(context, readback_delays_seconds=(0, 0, 0))
    initial = asyncio.run(client.get_order_detail(SYSTEM_ORDER, PLATFORM_ORDER))

    outcome = asyncio.run(
        client.update_contacts(
            SYSTEM_ORDER,
            PLATFORM_ORDER,
            ContactPatch(phone="5514970464"),
            expected_revision=initial.revision,
        )
    )

    assert outcome.status is ContactWriteStatus.INCONCLUSIVE
    assert outcome.completed is False
    assert outcome.attempts == 3
    assert len(context.post_calls) == 1


def test_update_contacts_transport_failure_after_post_start_is_inconclusive():
    before = _detail()
    context = FakeRequestContext(
        [FakeResponse(_api_payload(before))],
        post=TimeoutError("network result unknown"),
    )
    client = LingxingInternalOrderClient(context, readback_delays_seconds=(0,))
    initial = asyncio.run(client.get_order_detail(SYSTEM_ORDER, PLATFORM_ORDER))

    outcome = asyncio.run(
        client.update_contacts(
            SYSTEM_ORDER,
            PLATFORM_ORDER,
            ContactPatch(phone="5514970464"),
            expected_revision=initial.revision,
        )
    )

    assert outcome.status is ContactWriteStatus.INCONCLUSIVE
    assert outcome.attempted is True
    assert len(context.post_calls) == 1
