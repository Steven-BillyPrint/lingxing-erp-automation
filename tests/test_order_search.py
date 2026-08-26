from __future__ import annotations

import asyncio
import inspect

import pytest
from playwright.async_api import async_playwright

from lingxing_automation.pages import order_search


SYSTEM_ORDER_NO = "103737209528929820"


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
