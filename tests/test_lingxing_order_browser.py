from __future__ import annotations

import asyncio

import pytest

from shipment_automation.alibaba_ordering import AlibabaOrderRuleError
from shipment_automation.lingxing_order_browser import (
    LINGXING_ORDER_MANAGEMENT_URL,
    LingxingOrderBrowser,
    is_lingxing_erp_url,
)


class FakePage:
    def __init__(self, url: str, response: object) -> None:
        self.url = url
        self.response = response
        self.evaluate_args: object | None = None
        self.goto_calls: list[tuple[str, str]] = []

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))
        self.url = url

    async def evaluate(self, _script: str, args: object) -> object:
        self.evaluate_args = args
        return self.response


class FakeContext:
    def __init__(self, pages: list[FakePage], new_page: FakePage | None = None) -> None:
        self.pages = pages
        self._new_page = new_page
        self.new_page_calls = 0

    async def new_page(self) -> FakePage:
        self.new_page_calls += 1
        assert self._new_page is not None
        self.pages.append(self._new_page)
        return self._new_page


def _success_response(system_order_no: str) -> dict[str, object]:
    detail = {
        "global_order_no": system_order_no,
        "receive_info": {"address_line1": "987 Example Street"},
        "buyer_info": {"buyer_email": "buyer@example.com"},
    }
    return {
        "ok": True,
        "global_order_no": system_order_no,
        "order_detail": detail,
        "receive_info": detail["receive_info"],
    }


def test_lingxing_erp_url_requires_exact_https_host() -> None:
    assert is_lingxing_erp_url("https://erp.lingxing.com/erp/mmulti/mpOrderManagement")
    assert not is_lingxing_erp_url("http://erp.lingxing.com/erp/mmulti/mpOrderManagement")
    assert not is_lingxing_erp_url("https://erp.lingxing.com.evil.example/erp/")


def test_receive_info_uses_existing_page_and_verifies_order_identity() -> None:
    system_order_no = "103000000000000001"
    page = FakePage(
        "https://erp.lingxing.com/erp/mmulti/mpOrderManagement",
        _success_response(system_order_no),
    )
    context = FakeContext([page])

    result = asyncio.run(LingxingOrderBrowser(context).receive_info(system_order_no))

    assert result == {"address_line1": "987 Example Street"}
    assert page.evaluate_args == {
        "systemOrderNo": system_order_no,
        "path": "/api/platforms/oms/order_list/detail",
    }
    assert context.new_page_calls == 0


def test_order_detail_preserves_buyer_info_outside_receive_info() -> None:
    system_order_no = "103000000000000001"
    page = FakePage(
        "https://erp.lingxing.com/erp/mmulti/mpOrderManagement",
        _success_response(system_order_no),
    )

    result = asyncio.run(
        LingxingOrderBrowser(FakeContext([page])).order_detail(system_order_no)
    )

    assert result["receive_info"] == {"address_line1": "987 Example Street"}
    assert result["buyer_info"] == {"buyer_email": "buyer@example.com"}


def test_receive_info_opens_order_page_when_no_lingxing_tab_exists() -> None:
    system_order_no = "103000000000000001"
    page = FakePage("about:blank", _success_response(system_order_no))
    context = FakeContext([], new_page=page)

    result = asyncio.run(LingxingOrderBrowser(context).receive_info(system_order_no))

    assert result["address_line1"] == "987 Example Street"
    assert page.goto_calls == [
        (LINGXING_ORDER_MANAGEMENT_URL, "domcontentloaded")
    ]
    assert context.new_page_calls == 1


def test_receive_info_blocks_mismatched_system_order() -> None:
    page = FakePage(
        "https://erp.lingxing.com/erp/mmulti/mpOrderManagement",
        _success_response("103000000000000002"),
    )

    with pytest.raises(AlibabaOrderRuleError, match="系统单号与请求不一致"):
        asyncio.run(
            LingxingOrderBrowser(FakeContext([page])).receive_info(
                "103000000000000001"
            )
        )


def test_receive_info_reports_login_or_api_failure_without_using_empty_data() -> None:
    page = FakePage(
        "https://erp.lingxing.com/erp/mmulti/mpOrderManagement",
        {"ok": False, "message": "not logged in", "http_status": 401},
    )

    with pytest.raises(AlibabaOrderRuleError, match="not logged in"):
        asyncio.run(
            LingxingOrderBrowser(FakeContext([page])).receive_info(
                "103000000000000001"
            )
        )
