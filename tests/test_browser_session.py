import argparse
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lingxing_automation.browser.session import (
    OrderPageAuthenticationRequired,
    OrderPageLoadFailed,
    _install_headless_order_page_resource_guard,
    _strip_modulepreload_links,
    build_launch_kwargs,
    wait_for_order_page,
)
from lingxing_automation.constants import ORDER_MANAGEMENT_URL
from lingxing_automation.models import LoginConfig


def make_args(*, headless: bool, browser_channel: str = "chrome") -> argparse.Namespace:
    """提供浏览器会话启动测试辅助能力：构造参数。"""
    return argparse.Namespace(
        headless=headless,
        width=1920,
        height=1080,
        browser_channel=browser_channel,
    )


def test_headed_chrome_uses_real_window_viewport_and_sandbox():
    """验证浏览器会话启动中的有头模式Chrome使用真实窗口视口并沙箱场景。"""
    launch_kwargs = build_launch_kwargs(make_args(headless=False))

    assert launch_kwargs["headless"] is False
    assert launch_kwargs["no_viewport"] is True
    assert "viewport" not in launch_kwargs
    assert launch_kwargs["chromium_sandbox"] is True
    assert "--start-maximized" in launch_kwargs["args"]
    assert launch_kwargs["channel"] == "chrome"


def test_headless_keeps_fixed_viewport():
    """验证浏览器会话启动中的无头模式保留固定视口场景。"""
    launch_kwargs = build_launch_kwargs(make_args(headless=True))

    assert launch_kwargs["headless"] is True
    assert launch_kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert "no_viewport" not in launch_kwargs
    assert "chromium_sandbox" not in launch_kwargs


def test_bundled_browser_omits_channel():
    """验证浏览器会话启动中的内置 浏览器 省略通道场景。"""
    launch_kwargs = build_launch_kwargs(make_args(headless=False, browser_channel="bundled"))

    assert "channel" not in launch_kwargs


def test_mobile_binding_redirect_fails_immediately_with_actionable_message():
    page = SimpleNamespace(url="https://erp.lingxing.com/bindMobile")

    with pytest.raises(OrderPageAuthenticationRequired, match="手机绑定或设备验证"):
        asyncio.run(
            wait_for_order_page(
                page,
                300,
                LoginConfig(),
                auto_login=True,
            )
        )


def test_strip_modulepreload_links_preserves_other_resources():
    html = """
    <html><head>
      <link rel="modulepreload" href="/assets/one.js">
      <link href="/assets/two.js" rel='MODULEPRELOAD'>
      <link rel="preload" as="style" href="/assets/app.css">
      <link rel="stylesheet" href="/assets/app.css">
    </head></html>
    """

    filtered, removed_count = _strip_modulepreload_links(html)

    assert removed_count == 2
    assert "modulepreload" not in filtered.lower()
    assert 'rel="preload"' in filtered
    assert 'rel="stylesheet"' in filtered


def test_headless_resource_guard_filters_order_document():
    class FakeResponse:
        async def text(self):
            return '<link rel="modulepreload" href="/one.js"><main>订单管理</main>'

    class FakeRoute:
        request = SimpleNamespace(resource_type="document")

        def __init__(self):
            self.fulfilled = None

        async def fetch(self):
            return FakeResponse()

        async def fulfill(self, **kwargs):
            self.fulfilled = kwargs

        async def continue_(self):
            raise AssertionError("target order document should be fulfilled")

    class FakeContext:
        def __init__(self):
            self.pattern = None
            self.handler = None

        async def route(self, pattern, handler):
            self.pattern = pattern
            self.handler = handler

    context = FakeContext()
    asyncio.run(_install_headless_order_page_resource_guard(context, make_args(headless=True)))

    assert context.pattern == f"{ORDER_MANAGEMENT_URL}*"
    route = FakeRoute()
    asyncio.run(context.handler(route))
    assert route.fulfilled is not None
    assert "modulepreload" not in route.fulfilled["body"]
    assert "订单管理" in route.fulfilled["body"]


def test_order_page_timeout_uses_shared_load_failure(monkeypatch, tmp_path):
    class EmptyBody:
        async def inner_text(self, timeout):
            return ""

    class EmptyPage:
        url = ORDER_MANAGEMENT_URL

        def locator(self, selector):
            if selector == "body":
                return EmptyBody()
            return SimpleNamespace(count=lambda: 0)

        async def wait_for_timeout(self, _milliseconds):
            return None

    async def fake_diagnostics(*_args, **_kwargs):
        return {"diagnostic_file": str(tmp_path / "timeout.json")}

    monkeypatch.setattr(
        "lingxing_automation.browser.session.save_page_diagnostics",
        fake_diagnostics,
    )

    with pytest.raises(OrderPageLoadFailed, match="等待领星订单管理页面超时"):
        asyncio.run(
            wait_for_order_page(
                EmptyPage(),
                0,
                LoginConfig(),
                auto_login=True,
                debug_dir=tmp_path,
            )
        )
