from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from erp_automation.coordination.local_alibaba_order import (
    LocalAlibabaOrderActionExecutor,
)
from erp_automation.ui.models import (
    LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
    DesktopWriteAction,
    DesktopWriteConfirmation,
)
from shipment_automation.alibaba_order_browser import (
    AlibabaDraftFacts,
    AlibabaDraftFillResult,
)
from shipment_automation.alibaba_ordering import AlibabaOrderRuleError, AlibabaRoute


def _detail() -> dict[str, object]:
    return {
        "order_item": [{"sku": "feather-flag-10ft"}],
        "receive_info": {
            "receiver_name": "Jane Smith",
            "country_code": "US",
            "country": "United States",
            "state": "CA",
            "city": "Los Angeles",
            "address_line1": "123 Main Street",
            "postal_code": "90012",
            "phone_code": "1",
            "receiver_phone": "2135550188",
            "receiver_email": "jane@example.com",
        }
    }


def test_local_fill_action_attaches_to_local_chrome_and_never_submits(
    monkeypatch,
) -> None:
    old_url = "https://scm.alibaba.com/web/express/order.htm?old=1"
    new_url = "https://scm.alibaba.com/web/express/order.htm?new=1"
    observed: dict[str, object] = {}

    @asynccontextmanager
    async def context(endpoint):
        observed["endpoint"] = endpoint
        yield object()

    class Browser:
        def __init__(self, _context):
            pass

        async def draft_urls(self):
            return old_url, new_url

        async def page_for_url(self, url):
            observed["url"] = url
            return object()

        async def ensure_logged_in(self, page, login_config, **kwargs):
            observed["login_account"] = login_config.account
            observed["return_url"] = kwargs["return_url"]
            return page

        async def inspect_draft(self, _page):
            return AlibabaDraftFacts(
                url=new_url,
                route=AlibabaRoute("Express Expedited"),
                total_weight_kg=Decimal("20"),
                route_is_expedited=True,
                signature_available=True,
            )

        async def fill_draft(self, _page, **kwargs):
            observed["customer_order_no"] = kwargs["customer_order_no"]
            observed["declaration"] = kwargs["declaration"]
            return AlibabaDraftFillResult(
                url=new_url,
                route_name="Express Expedited",
                total_weight_kg=Decimal("20"),
                declared_unit_price_usd=kwargs[
                    "declaration"
                ].declared_unit_price_usd,
                signature_selected=False,
                signature_fee_text="",
            )

    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.attached_alibaba_context",
        context,
    )
    monkeypatch.setattr(
        "shipment_automation.alibaba_order_browser.AlibabaOrderBrowser",
        Browser,
    )
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT,
        "platform-one",
        system_order_no="platform-one",
    )
    executor = LocalAlibabaOrderActionExecutor("http://127.0.0.1:28076")

    result = executor.execute(
        LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
        {
            "detail": _detail(),
            "command_order_no": "platform-one",
            "system_order_no": "system-one",
            "platform_order_no": "platform-one",
            "baseline_draft_urls": [old_url],
            "login_config": {
                "account": "configured@example.com",
                "password": "configured-password",
                "auto_login": True,
            },
            "expedited": True,
            "signature_requested": False,
            "heavy_or_frame": True,
            "category": "vinyl_banner",
            "confirmation": confirmation.to_payload(),
        },
    )

    assert observed["endpoint"] == "http://127.0.0.1:28076"
    assert observed["url"] == new_url
    assert observed["return_url"] == new_url
    assert observed["login_account"] == "configured@example.com"
    assert observed["customer_order_no"] == "platform-one"
    assert result["declared_unit_price_usd"] == "10.01"
    assert result["alibaba_submit_calls"] == 0
    assert observed["declaration"].name_cn == "喷绘"


def test_local_fill_action_rejects_category_changed_after_prepare() -> None:
    confirmation = DesktopWriteConfirmation.create(
        DesktopWriteAction.FILL_ALIBABA_ORDER_DRAFT,
        "platform-one",
        system_order_no="platform-one",
    )
    executor = LocalAlibabaOrderActionExecutor("http://127.0.0.1:28076")

    with pytest.raises(AlibabaOrderRuleError, match="分类与查价准备记录不一致"):
        executor.execute(
            LOCAL_BROWSER_ACTION_ALIBABA_ORDER_FILL,
            {
                "detail": _detail(),
                "command_order_no": "platform-one",
                "system_order_no": "system-one",
                "platform_order_no": "platform-one",
                "baseline_draft_urls": [],
                "login_config": {
                    "account": "configured@example.com",
                    "password": "configured-password",
                    "auto_login": True,
                },
                "category": "wall_decal",
                "confirmation": confirmation.to_payload(),
            },
        )
