import asyncio

from lingxing_automation.pages import order_search


class FakeSearchPage:
    def __init__(self):
        """初始化测试替身搜索 page测试替身的内部状态。"""
        self.filled_order_no = None
        self.waits: list[int] = []

    def locator(self, selector: str):
        """模拟 Playwright 定位器查询。"""
        raise AssertionError(f"fill_order_search should not use strict locator: {selector}")

    async def evaluate(self, _script: str, arg=None):
        """模拟 Playwright 页面脚本执行。"""
        if isinstance(arg, dict) and "searchInputIndex" in arg:
            self.filled_order_no = arg["orderNo"]
            return True
        if isinstance(arg, int):
            return {
                "selectedLabel": "平台单号",
                "searchInputIndex": arg,
                "inputs": [
                    {
                        "index": arg,
                        "value": self.filled_order_no,
                        "around": "平台单号",
                        "placeholder": "",
                    },
                    {
                        "index": arg + 1,
                        "value": "",
                        "around": "添加商品 SKU 搜索内容",
                        "placeholder": "搜索内容",
                    },
                ],
            }
        return True

    async def wait_for_timeout(self, timeout_ms: int):
        """模拟 Playwright 等待超时接口。"""
        self.waits.append(timeout_ms)


def test_fill_order_search_uses_resolved_input_index(monkeypatch):
    """验证订单搜索中的填写订单搜索使用解析后的输入框索引场景。"""
    async def noop(*_args, **_kwargs):
        """提供订单搜索测试辅助能力：空操作。"""
        return None

    async def fake_select_order_search_type(_page, _search_kind):
        """模拟选择订单搜索类型行为，隔离测试中的外部依赖。"""
        return "平台单号"

    async def fake_find_order_search_input_index(_page):
        """模拟查找订单搜索输入框索引行为，隔离测试中的外部依赖。"""
        return 28

    monkeypatch.setattr(order_search, "close_order_detail_dialog", noop)
    monkeypatch.setattr(order_search, "close_search_overlays", noop)
    monkeypatch.setattr(order_search, "select_order_search_type", fake_select_order_search_type)
    monkeypatch.setattr(order_search, "find_order_search_input_index", fake_find_order_search_input_index)

    page = FakeSearchPage()

    result = asyncio.run(order_search.fill_order_search(page, "111-2605628-1613847", "platform"))

    assert page.filled_order_no == "111-2605628-1613847"
    assert result["search_validation_ok"] is True
    assert result["search_input_index"] == 28
