from __future__ import annotations

import asyncio

import pytest

from shipment_automation.alibaba_ordering import AlibabaOrderRuleError
from shipment_automation.lingxing_order_browser import (
    LINGXING_ORDER_DETAIL_API_PATH,
    LINGXING_ORDER_MANAGEMENT_URL,
    LingxingOrderBrowser,
    is_lingxing_erp_url,
)


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.ok = 200 <= status < 300

    async def json(self) -> object:
        return self.payload


class FakeRequest:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


class FakeContext:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.pages: list[object] = []
        self.request = FakeRequest(FakeResponse(payload, status=status))


def _success_response(system_order_no: str) -> dict[str, object]:
    detail = {
        "global_order_no": system_order_no,
        "receive_info": {"address_line1": "987 Example Street"},
        "buyer_info": {"buyer_email": "buyer@example.com"},
    }
    return {
        "code": 1,
        "msg": "success",
        "data": detail,
    }


def test_lingxing_erp_url_requires_exact_https_host() -> None:
    assert is_lingxing_erp_url("https://erp.lingxing.com/erp/mmulti/mpOrderManagement")
    assert not is_lingxing_erp_url("http://erp.lingxing.com/erp/mmulti/mpOrderManagement")
    assert not is_lingxing_erp_url("https://erp.lingxing.com.evil.example/erp/")


def test_receive_info_uses_background_request_and_verifies_order_identity() -> None:
    system_order_no = "103000000000000001"
    context = FakeContext(_success_response(system_order_no))

    result = asyncio.run(LingxingOrderBrowser(context).receive_info(system_order_no))

    assert result == {"address_line1": "987 Example Street"}
    assert context.pages == []
    assert context.request.calls == [{
        "url": f"https://erp.lingxing.com{LINGXING_ORDER_DETAIL_API_PATH}",
        "params": {
            "global_order_no": system_order_no,
            "req_time_sequence": f"{LINGXING_ORDER_DETAIL_API_PATH}$$4",
        },
        "headers": {
            "Accept": "application/json",
            "Referer": LINGXING_ORDER_MANAGEMENT_URL,
        },
        "timeout": 10000,
    }]


def test_order_detail_preserves_buyer_info_outside_receive_info() -> None:
    system_order_no = "103000000000000001"
    result = asyncio.run(
        LingxingOrderBrowser(FakeContext(_success_response(system_order_no))).order_detail(
            system_order_no
        )
    )

    assert result["receive_info"] == {"address_line1": "987 Example Street"}
    assert result["buyer_info"] == {"buyer_email": "buyer@example.com"}


def test_receive_info_never_opens_a_lingxing_page() -> None:
    system_order_no = "103000000000000001"
    context = FakeContext(_success_response(system_order_no))

    result = asyncio.run(LingxingOrderBrowser(context).receive_info(system_order_no))

    assert result["address_line1"] == "987 Example Street"
    assert context.pages == []


def test_receive_info_blocks_mismatched_system_order() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="系统单号与请求不一致"):
        asyncio.run(
            LingxingOrderBrowser(
                FakeContext(_success_response("103000000000000002"))
            ).receive_info(
                "103000000000000001"
            )
        )


def test_receive_info_reports_login_or_api_failure_without_using_empty_data() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="not logged in"):
        asyncio.run(
            LingxingOrderBrowser(
                FakeContext(
                    {"code": 0, "msg": "not logged in", "data": {}},
                    status=401,
                )
            ).receive_info(
                "103000000000000001"
            )
        )
