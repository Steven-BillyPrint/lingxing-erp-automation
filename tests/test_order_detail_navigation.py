from __future__ import annotations

import asyncio

import pytest

from lingxing_automation.pages import order_detail_navigation


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


class _FakePage:
    def __init__(self, probes: list[dict]) -> None:
        self._probes = list(probes)
        self.evaluate_calls: list[tuple[str, str]] = []
        self.waits: list[int] = []
        self.mouse = _FakeMouse()

    async def evaluate(self, script: str, order_no: str) -> dict:
        self.evaluate_calls.append((script, order_no))
        if self._probes:
            return self._probes.pop(0)
        return {
            "found": False,
            "ready": False,
            "candidateCount": 0,
            "blocker": "",
        }

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


def test_click_system_order_waits_for_visible_pointer_and_uses_real_mouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(order_detail_navigation, "_ORDER_CLICK_STABLE_MS", 0)
    page = _FakePage(
        [
            {
                "found": True,
                "ready": False,
                "candidateCount": 4,
                "blocker": "DIV.el-loading-mask",
            },
            {
                "found": True,
                "ready": True,
                "candidateCount": 4,
                "x": 182.4,
                "y": 605.2,
                "tag": "span",
                "className": "ak-blue ak-pointer",
                "blocker": "",
            },
            {
                "found": True,
                "ready": True,
                "candidateCount": 4,
                "x": 182.4,
                "y": 605.2,
                "tag": "span",
                "className": "ak-blue ak-pointer",
                "blocker": "",
            },
        ]
    )

    asyncio.run(
        order_detail_navigation.click_system_order(
            page,
            "103727324802185912",
        )
    )

    assert page.mouse.clicks == [(182, 605)]
    assert page.waits
    script = page.evaluate_calls[0][0]
    assert "ak-pointer" in script
    assert "elementFromPoint" in script
    assert ".click()" not in script


def test_click_system_order_reports_blocking_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter([0.0, 0.0, 0.2, 13.0])
    monkeypatch.setattr(order_detail_navigation, "_monotonic", lambda: next(timestamps))
    page = _FakePage(
        [
            {
                "found": True,
                "ready": False,
                "candidateCount": 1,
                "blocker": "DIV.el-loading-mask",
            }
        ]
    )

    with pytest.raises(RuntimeError, match="加载层遮挡"):
        asyncio.run(
            order_detail_navigation.click_system_order(
                page,
                "103727324802185912",
            )
        )

    assert page.mouse.clicks == []


class _DetailTimeoutPage:
    async def wait_for_function(self, *_args, **_kwargs) -> None:
        raise RuntimeError("Page.wait_for_function: Timeout 22000ms exceeded.")


def test_wait_for_detail_replaces_raw_playwright_timeout() -> None:
    with pytest.raises(RuntimeError, match="领星订单详情.*没有完成加载") as exc_info:
        asyncio.run(
            order_detail_navigation.wait_for_detail(
                _DetailTimeoutPage(),
                "103727324802185912",
            )
        )

    assert "Page.wait_for_function" not in str(exc_info.value)
