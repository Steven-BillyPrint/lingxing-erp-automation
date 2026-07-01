import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lingxing_automation.browser.session import build_launch_kwargs


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
