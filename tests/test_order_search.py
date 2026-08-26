from __future__ import annotations

import asyncio
import inspect

import pytest
from playwright.async_api import async_playwright

from lingxing_automation.pages import order_search


SYSTEM_ORDER_NO = "103737209528929820"
PLATFORM_ORDER_NO = "111-1226110-9766666"


def test_order_search_actions_do_not_use_layout_or_javascript_clicks() -> None:
    source = "\n".join(
        inspect.getsource(function)
        for function in (
            order_search._order_search_root,
            order_search.select_order_search_type,
            order_search.find_order_search_input_index,
            order_search.click_order_search_button,
            order_search.fill_order_search,
        )
    )
    for forbidden in (
        "getBoundingClientRect",
        "page.mouse",
        "dispatchEvent",
        "MouseEvent",
        "InputEvent",
        "rect.top",
        "rect.left",
    ):
        assert forbidden not in source


@pytest.mark.parametrize("page_zoom", [0.8, 0.9, 1.0, 1.25])
def test_fill_order_search_uses_semantic_root_and_closes_transient_overlay(
    page_zoom: float,
) -> None:
    async def run() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1180, "height": 720})
                await page.set_content(
                    f"""
                    <style>
                      body {{ zoom: {page_zoom}; }}
                      .el-select-dropdown, .el-autocomplete-suggestion {{
                        position: fixed; z-index: 3000; background: white;
                      }}
                    </style>
                    <div id="advanced-input" style="display:none">
                      <div class="el-input-group__prepend">
                        <div class="el-select"><input class="el-input__inner" value="平台单号"></div>
                      </div>
                      <div class="search-input"><input class="el-input__inner"></div>
                      <button class="lx_combo_search">搜索</button>
                    </div>
                    <input id="date-filter" value="">
                    <div id="advanced-input">
                      <div class="el-input-group__prepend">
                        <div class="el-select">
                          <input class="el-input__inner" value="平台单号" readonly>
                        </div>
                      </div>
                      <div class="search-input">
                        <input class="el-input__inner" placeholder="搜索订单">
                      </div>
                      <button class="lx_combo_search">搜索</button>
                    </div>
                    <ul class="el-select-dropdown" style="display:none">
                      <li class="el-select-dropdown__item">平台单号</li>
                      <li class="el-select-dropdown__item">系统单号</li>
                    </ul>
                    <div class="el-autocomplete-suggestion" style="display:none">
                      搜索建议
                    </div>
                    <script>
                      window.searchClicks = 0;
                      const root = [...document.querySelectorAll('#advanced-input')]
                        .find((item) => getComputedStyle(item).display !== 'none');
                      const label = root.querySelector('.el-select input');
                      const searchInput = root.querySelector('.search-input input');
                      const dropdown = document.querySelector('.el-select-dropdown');
                      const suggestion = document.querySelector('.el-autocomplete-suggestion');
                      root.querySelector('.el-select').onclick = () => {{
                        dropdown.style.display = 'block';
                      }};
                      dropdown.querySelectorAll('li').forEach((option) => {{
                        option.onclick = () => {{
                          label.value = option.textContent.trim();
                          dropdown.style.display = 'none';
                        }};
                      }});
                      searchInput.addEventListener('input', () => {{
                        suggestion.style.display = 'block';
                      }});
                      root.querySelector('.lx_combo_search').onclick = () => {{
                        window.searchClicks += 1;
                        window.searchedValue = searchInput.value;
                      }};
                      document.addEventListener('keydown', (event) => {{
                        if (event.key === 'Escape') {{
                          dropdown.style.display = 'none';
                          suggestion.style.display = 'none';
                        }}
                      }});
                    </script>
                    """
                )

                result = await order_search.fill_order_search(
                    page,
                    SYSTEM_ORDER_NO,
                    "system",
                )

                assert result["search_validation_ok"] is True
                assert result["selected_search_type"] == "系统单号"
                assert result["search_input_value"] == SYSTEM_ORDER_NO
                assert await page.evaluate("window.searchClicks") == 1
                assert await page.evaluate("window.searchedValue") == SYSTEM_ORDER_NO
                assert not await page.locator(".el-autocomplete-suggestion").is_visible()
            finally:
                await browser.close()

    asyncio.run(run())


def test_search_click_accepts_exact_result_after_click_confirmation_timeout(
    monkeypatch,
) -> None:
    class SearchInput:
        async def input_value(self) -> str:
            return PLATFORM_ORDER_NO

    class SearchButton:
        def __init__(self) -> None:
            self.click_calls: list[dict[str, object]] = []

        @property
        def first(self):
            return self

        async def count(self) -> int:
            return 1

        async def click(self, **kwargs) -> None:
            self.click_calls.append(kwargs)
            raise TimeoutError("click confirmation timed out after dispatch")

    class Root:
        def __init__(self, search_input, button) -> None:
            self.search_input = search_input
            self.button = button

        def locator(self, selector: str):
            if selector == ".search-input > input.el-input__inner":
                return self.search_input
            if selector == ".lx_combo_search:visible":
                return self.button
            raise AssertionError(selector)

    class Page:
        async def wait_for_timeout(self, _timeout_ms: int) -> None:
            return None

    async def run() -> None:
        search_input = SearchInput()
        button = SearchButton()
        root = Root(search_input, button)

        async def order_search_root(_page):
            return root

        async def search_input_index(_search_input, fallback=None):
            return 7

        async def visible_result(_page, order_no: str, *, timeout_ms=5000):
            assert order_no == PLATFORM_ORDER_NO
            assert timeout_ms == 5000
            return True

        async def dismiss_overlays(_page):
            return None

        monkeypatch.setattr(order_search, "_order_search_root", order_search_root)
        monkeypatch.setattr(order_search, "_search_input_index", search_input_index)
        monkeypatch.setattr(
            order_search,
            "_wait_for_visible_order_search_result",
            visible_result,
        )
        monkeypatch.setattr(
            order_search,
            "dismiss_order_search_overlays",
            dismiss_overlays,
        )

        assert await order_search.click_order_search_button(
            Page(),
            7,
            PLATFORM_ORDER_NO,
        )
        assert button.click_calls == [
            {"timeout": 10_000, "no_wait_after": True}
        ]

    asyncio.run(run())


def test_search_click_does_not_retry_without_result_evidence(monkeypatch) -> None:
    class SearchInput:
        async def input_value(self) -> str:
            return PLATFORM_ORDER_NO

    class SearchButton:
        def __init__(self) -> None:
            self.click_count = 0

        @property
        def first(self):
            return self

        async def count(self) -> int:
            return 1

        async def click(self, **_kwargs) -> None:
            self.click_count += 1
            raise TimeoutError("click was not confirmed")

    class Root:
        def __init__(self, search_input, button) -> None:
            self.search_input = search_input
            self.button = button

        def locator(self, selector: str):
            if selector == ".search-input > input.el-input__inner":
                return self.search_input
            if selector == ".lx_combo_search:visible":
                return self.button
            raise AssertionError(selector)

    async def run() -> None:
        button = SearchButton()
        root = Root(SearchInput(), button)

        async def order_search_root(_page):
            return root

        async def search_input_index(_search_input, fallback=None):
            return 3

        async def no_visible_result(_page, _order_no: str, *, timeout_ms=5000):
            return False

        monkeypatch.setattr(order_search, "_order_search_root", order_search_root)
        monkeypatch.setattr(order_search, "_search_input_index", search_input_index)
        monkeypatch.setattr(
            order_search,
            "_wait_for_visible_order_search_result",
            no_visible_result,
        )

        with pytest.raises(RuntimeError, match="列表中没有出现目标订单"):
            await order_search.click_order_search_button(
                object(),
                3,
                PLATFORM_ORDER_NO,
            )
        assert button.click_count == 1

    asyncio.run(run())
