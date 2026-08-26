from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.models import LoginConfig
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
    def __init__(self, response: FakeResponse | list[FakeResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[dict[str, object]] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class FakeContext:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.pages: list[object] = []
        self.request = FakeRequest(FakeResponse(payload, status=status))


class FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.closed = False
        self.front = False

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.url = url

    async def close(self) -> None:
        self.closed = True

    async def bring_to_front(self) -> None:
        self.front = True


class RecoveringContext:
    def __init__(self, system_order_no: str) -> None:
        self.pages: list[FakePage] = []
        self.page = FakePage()
        self.request = FakeRequest(
            [
                FakeResponse(
                    {"code": 0, "msg": "未登录或登录已过期", "data": {}},
                    status=401,
                ),
                FakeResponse(_success_response(system_order_no)),
            ]
        )

    async def new_page(self) -> FakePage:
        self.pages.append(self.page)
        return self.page


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


def test_receive_info_reports_non_auth_api_failure_without_using_empty_data() -> None:
    with pytest.raises(AlibabaOrderRuleError, match="detail unavailable"):
        asyncio.run(
            LingxingOrderBrowser(
                FakeContext(
                    {"code": 100, "msg": "detail unavailable", "data": {}},
                    status=503,
                )
            ).receive_info(
                "103000000000000001"
            )
        )


def test_order_detail_restores_expired_login_with_imported_credentials(
    monkeypatch,
) -> None:
    system_order_no = "103000000000000001"
    context = RecoveringContext(system_order_no)
    observed: dict[str, object] = {}

    async def login_page(_page) -> bool:
        return True

    async def auto_login(_page, config) -> bool:
        observed["config"] = config
        return True

    monkeypatch.setattr(
        "shipment_automation.lingxing_order_browser.is_login_page",
        login_page,
    )
    monkeypatch.setattr(
        "shipment_automation.lingxing_order_browser.try_auto_login",
        auto_login,
    )

    config = LoginConfig(
        account="evelyn@example.com",
        password="configured-password",
        remember_login=True,
    )
    result = asyncio.run(
        LingxingOrderBrowser(context, config).order_detail(system_order_no)
    )

    assert result["global_order_no"] == system_order_no
    assert observed["config"] is config
    assert context.page.closed is True
    assert len(context.request.calls) == 2


def test_order_detail_explains_cookie_migration_when_credentials_missing() -> None:
    context = FakeContext(
        {"code": 0, "msg": "未登录或登录已过期", "data": {}},
        status=401,
    )

    with pytest.raises(AlibabaOrderRuleError, match="Cookie 不会跟随授权文件迁移"):
        asyncio.run(
            LingxingOrderBrowser(context).order_detail("103000000000000001")
        )
