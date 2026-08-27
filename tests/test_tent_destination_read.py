from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.services import tent_sku_adjuster
from lingxing_automation.services.tent_sku_adjuster import (
    read_detail_shipping_destination,
)
from lingxing_automation.services.tent_sku_planner import normalize_us_postal_code


class _ApiPage:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    async def evaluate(self, _script, payload):
        self.calls += 1
        assert payload["systemOrderNo"] == "103725267407372040"
        assert payload["path"] == "/api/platforms/oms/order_list/detail"
        return self.response


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


def test_detail_destination_prefers_api_and_does_not_call_dom(monkeypatch):
    page = _ApiPage(
        {
            "ok": True,
            "http_status": 200,
            "code": 1,
            "request_id": "request-1",
            "global_order_no": "103725267407372040",
            "receive_info": {
                "receiver_country_name": "United States of America (USA)(美国)",
                "state_or_region": "OH",
                "city": "CLEVELAND",
                "postal_code": "44102-2135",
            },
        }
    )

    async def forbidden_dom(_page):
        raise AssertionError("API 成功时不应调用旧页面邮编读取")

    monkeypatch.setattr(
        tent_sku_adjuster,
        "read_detail_shipping_address_text",
        forbidden_dom,
    )

    result = asyncio.run(
        read_detail_shipping_destination(page, "103725267407372040")
    )

    assert result.postal_code == "44102"
    assert result.postal_source == "erp_detail_api"
    assert result.api_error is None
    assert "OH" in result.shipping_address_text
    assert "邮编 44102" in result.shipping_address_text


def test_detail_destination_uses_dom_only_after_api_failure(monkeypatch):
    page = _ApiPage(
        {
            "ok": False,
            "http_status": 503,
            "message": "temporary unavailable",
        }
    )
    dom_calls = 0

    async def dom_fallback(_page):
        nonlocal dom_calls
        dom_calls += 1
        return "收件地址 United States of America (USA)，OH，CLEVELAND 邮编 01020-1234"

    monkeypatch.setattr(
        tent_sku_adjuster,
        "read_detail_shipping_address_text",
        dom_fallback,
    )

    result = asyncio.run(
        read_detail_shipping_destination(page, "103725267407372040")
    )

    assert dom_calls == 1
    assert result.postal_code == "01020"
    assert result.postal_source == "detail_dom_fallback"
    assert "temporary unavailable" in (result.api_error or "")


@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (
            {
                "ok": True,
                "http_status": 200,
                "code": 1,
                "global_order_no": "wrong-order",
                "receive_info": {"postal_code": "44102"},
            },
            "系统单号不一致",
        ),
        (
            {
                "ok": True,
                "http_status": 200,
                "code": 1,
                "global_order_no": "103725267407372040",
                "receive_info": {"postal_code": ""},
            },
            "未返回有效五位邮编",
        ),
        (
            {
                "ok": True,
                "http_status": 200,
                "code": 1,
                "global_order_no": "103725267407372040",
                "receive_info": {"postal_code": "44***"},
            },
            "未返回有效五位邮编",
        ),
        (
            {
                "ok": True,
                "http_status": 200,
                "code": 1,
                "global_order_no": "103725267407372040",
                "receive_info": {"postal_code": "12-345"},
            },
            "未返回有效五位邮编",
        ),
    ],
)
def test_detail_destination_falls_back_for_untrusted_api_postal(
    monkeypatch,
    response,
    expected_error,
):
    page = _ApiPage(response)
    dom_calls = 0

    async def dom_fallback(_page):
        nonlocal dom_calls
        dom_calls += 1
        return "收件地址 United States of America (USA)，OH，CLEVELAND 邮编 01020"

    monkeypatch.setattr(
        tent_sku_adjuster,
        "read_detail_shipping_address_text",
        dom_fallback,
    )

    result = asyncio.run(
        read_detail_shipping_destination(page, "103725267407372040")
    )

    assert dom_calls == 1
    assert result.postal_code == "01020"
    assert result.postal_source == "detail_dom_fallback"
    assert expected_error in (result.api_error or "")


def test_detail_destination_reports_when_api_and_dom_have_no_valid_zip(monkeypatch):
    page = _ApiPage(
        {
            "ok": True,
            "http_status": 200,
            "code": 1,
            "global_order_no": "103725267407372040",
            "receive_info": {"postal_code": "-"},
        }
    )

    async def dom_without_postal(_page):
        return "收件地址 United States of America (USA)，OH，CLEVELAND"

    monkeypatch.setattr(
        tent_sku_adjuster,
        "read_detail_shipping_address_text",
        dom_without_postal,
    )

    result = asyncio.run(
        read_detail_shipping_destination(page, "103725267407372040")
    )

    assert result.postal_code is None
    assert result.postal_source == "unavailable"
    assert "旧页面也未读取到有效五位邮编" in (result.api_error or "")
