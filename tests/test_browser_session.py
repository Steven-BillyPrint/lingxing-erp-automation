import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lingxing_automation.browser.session import build_launch_kwargs


def make_args(*, headless: bool, browser_channel: str = "chrome") -> argparse.Namespace:
    return argparse.Namespace(
        headless=headless,
        width=1920,
        height=1080,
        browser_channel=browser_channel,
    )


def test_headed_chrome_uses_real_window_viewport_and_sandbox():
    launch_kwargs = build_launch_kwargs(make_args(headless=False))

    assert launch_kwargs["headless"] is False
    assert launch_kwargs["no_viewport"] is True
    assert "viewport" not in launch_kwargs
    assert launch_kwargs["chromium_sandbox"] is True
    assert "--start-maximized" in launch_kwargs["args"]
    assert launch_kwargs["channel"] == "chrome"


def test_headless_keeps_fixed_viewport():
    launch_kwargs = build_launch_kwargs(make_args(headless=True))

    assert launch_kwargs["headless"] is True
    assert launch_kwargs["viewport"] == {"width": 1920, "height": 1080}
    assert "no_viewport" not in launch_kwargs
    assert "chromium_sandbox" not in launch_kwargs


def test_bundled_browser_omits_channel():
    launch_kwargs = build_launch_kwargs(make_args(headless=False, browser_channel="bundled"))

    assert "channel" not in launch_kwargs
