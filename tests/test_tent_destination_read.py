from __future__ import annotations

import asyncio

import pytest

from erp_automation.contracts.internal_orders import (
    ContactSnapshot,
    InternalOrderDetail,
)
from lingxing_automation.services.tent_sku_adjuster import (
    read_detail_shipping_destination,
    shipping_destination_from_internal_detail,
)
from lingxing_automation.services.tent_sku_planner import normalize_us_postal_code


SYSTEM_ORDER = "103725267407372040"
PLATFORM_ORDER = "112-1234567-1234567"


def _detail(postal_code: str | None = "44102-2135") -> InternalOrderDetail:
    return InternalOrderDetail(
        system_order_no=SYSTEM_ORDER,
        platform_order_nos=(PLATFORM_ORDER,),
        recipient_name="Buyer",
        address_line1="987 Example Street",
        address_line2=None,
        address_line3=None,
        city="CLEVELAND",
        state_or_region="OH",
        country_code="US",
        country_name="United States",
        postal_code=postal_code,
        shipping_address_text="987 Example Street, CLEVELAND, OH, 44102, United States",
        contact=ContactSnapshot(),
        status="2",
        revision="revision",
        request_id="request-1",
    )


class InternalOperations:
    def __init__(self, detail: InternalOrderDetail):
        self.detail = detail
        self.calls = []

    async def get_order_detail(self, system_order_no, platform_order_no):
        self.calls.append((system_order_no, platform_order_no))
        return self.detail

    async def update_contacts(self, *_args, **_kwargs):
        raise AssertionError("destination reader must not write")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("44102", "44102"),
        ("44102-2135", "44102"),
        ("441022135", "44102"),
        ("01020-1234", "01020"),
        ("-", None),
        ("***44102", None),
        ("1234", None),
    ],
)
def test_postal_normalization_keeps_only_first_five_digits(raw, expected):
    assert normalize_us_postal_code(raw) == expected


def test_destination_uses_internal_operations_without_page_or_dom():
    operations = InternalOperations(_detail())

    result = asyncio.run(
        read_detail_shipping_destination(
            operations,
            SYSTEM_ORDER,
            PLATFORM_ORDER,
        )
    )

    assert operations.calls == [(SYSTEM_ORDER, PLATFORM_ORDER)]
    assert result.postal_code == "44102"
    assert result.postal_source == "lingxing_internal_detail"
    assert result.api_error is None
    assert "CLEVELAND" in result.shipping_address_text
    assert result.request_id == "request-1"


@pytest.mark.parametrize("postal", [None, "", "-", "44***", "1234"])
def test_invalid_internal_postal_never_falls_back_to_dom(postal):
    result = shipping_destination_from_internal_detail(_detail(postal))

    assert result.postal_code is None
    assert result.postal_source == "unavailable"
    assert "禁止回退 DOM" in (result.api_error or "")
