import asyncio

from lingxing_automation.services.tent_package_split_adjuster import (
    _can_scroll_down,
    _find_split_row_for_sku,
    _matching_any_row,
    _matching_visible_row,
    _set_split_item_quantity,
    _sku_text_matches,
)
from lingxing_automation.services import tent_package_split_adjuster
from lingxing_automation.services.tent_package_split_planner import TentPackageSplitItem


class FakeSplitPage:
    def __init__(self, states):
        """初始化拆包弹窗滚动测试替身页面。"""

        self.states = list(states)
        self.state_index = 0
        self.evaluate_args = []
        self.waits = []

    async def evaluate(self, _script, *args):
        """模拟 Playwright evaluate，并区分读取状态和滚动操作。"""

        if args:
            self.evaluate_args.append(args[0])
            return None
        state = self.states[min(self.state_index, len(self.states) - 1)]
        self.state_index += 1
        return state

    async def wait_for_timeout(self, timeout_ms: int):
        """模拟 Playwright 等待接口。"""

        self.waits.append(timeout_ms)


def _state(*, rows, scroll_top=0, scroll_height=336, client_height=260):
    """构造拆包表格状态。"""

    return {
        "checkboxColId": "col_534",
        "skuColId": "col_535",
        "shipQtyColId": "col_536",
        "splitQtyColId": "col_537",
        "scrollTop": scroll_top,
        "scrollHeight": scroll_height,
        "clientHeight": client_height,
        "rows": rows,
    }


def _row(rowid, sku_text, *, visible):
    """构造拆包表格商品行状态。"""

    return {
        "rowid": rowid,
        "skuText": sku_text,
        "shipQty": "1",
        "visibleInsideWrapper": visible,
    }


def test_sku_text_match_requires_exact_sku_token():
    """验证 SKU 匹配不会把相近 SKU 误判为目标商品。"""

    assert _sku_text_matches("平台单号 114 品名 TENT-ROLLER-BAG-10X10-50MM", "TENT-ROLLER-BAG-10X10-50MM")
    assert not _sku_text_matches("TENT-ROLLER-BAG-10X10-50MM-EXTRA", "TENT-ROLLER-BAG-10X10-50MM")
    assert not _sku_text_matches("10ft-Half-Wall-Double-Sided", "10ft-Half-Wall")


def test_matching_visible_row_ignores_dom_row_outside_wrapper():
    """验证 DOM 中存在但已超出滚动容器可视范围的行不会被当成可点击行。"""

    state = _state(
        rows=[
            _row("r1", "10x10-Canopy-Topper", visible=False),
            _row("r2", "SANDBAGS-4PCS", visible=True),
        ]
    )

    assert _matching_visible_row(state, "10x10-Canopy-Topper") is None
    assert _matching_any_row(state, "10x10-Canopy-Topper")["rowid"] == "r1"
    assert _matching_visible_row(state, "SANDBAGS-4PCS")["rowid"] == "r2"


def test_can_scroll_down_uses_wrapper_dimensions():
    """验证滚动判断使用 wrapper 的 clientHeight 和 scrollHeight。"""

    assert _can_scroll_down(_state(rows=[], scroll_top=0, scroll_height=336, client_height=260)) is True
    assert _can_scroll_down(_state(rows=[], scroll_top=76, scroll_height=336, client_height=260)) is False


def test_find_split_row_scrolls_dom_row_into_view_and_requeries():
    """验证目标行在 DOM 中但不可见时会滚动到可视范围后重新读取表格。"""

    page = FakeSplitPage(
        [
            _state(rows=[_row("r5", "SANDBAGS-4PCS", visible=False)]),
            _state(rows=[_row("r5", "SANDBAGS-4PCS", visible=True)]),
        ]
    )

    state, row = asyncio.run(_find_split_row_for_sku(page, "SANDBAGS-4PCS"))

    assert row["rowid"] == "r5"
    assert state["rows"][0]["visibleInsideWrapper"] is True
    assert page.evaluate_args == ["r5"]
    assert page.waits == [200]


def test_find_split_row_scrolls_wrapper_when_target_not_in_current_dom_state():
    """验证当前可读 DOM 中没有目标 SKU 时会滚动 wrapper 后继续查找。"""

    page = FakeSplitPage(
        [
            _state(rows=[_row("r1", "10x10-Canopy-Topper", visible=True)], scroll_top=0),
            _state(rows=[_row("r7", "Instruction", visible=True)], scroll_top=76),
        ]
    )

    _state_after_scroll, row = asyncio.run(_find_split_row_for_sku(page, "Instruction"))

    assert row["rowid"] == "r7"
    assert page.evaluate_args == [212]
    assert page.waits == [200]


def test_find_split_row_error_reports_current_dialog_skus():
    """验证拆分弹窗缺少目标 SKU 时错误会包含当前弹窗实际 SKU 摘要。"""

    page = FakeSplitPage(
        [
            _state(
                rows=[_row("r1", "平台单号 114 主商品 canopytents", visible=True)],
                scroll_top=0,
                scroll_height=260,
                client_height=260,
            )
        ]
    )

    try:
        asyncio.run(_find_split_row_for_sku(page, "Instruction"))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing SKU must raise a diagnostic error")

    assert "拆分弹窗中没有找到 SKU 精确等于 Instruction 的可见行" in message
    assert "当前订单包裹 1 SKU" in message
    assert "canopytents" in message


def test_set_split_item_quantity_spreads_same_sku_across_rows(monkeypatch):
    rows = [
        {"rowid": "r1", "skuText": "TENT-ROLLER-BAG-10X10-50MM", "shipQty": "1", "splitQty": ""},
        {"rowid": "r2", "skuText": "TENT-ROLLER-BAG-10X10-50MM", "shipQty": "1", "splitQty": ""},
    ]
    fills: list[tuple[str, int]] = []

    async def fake_find(_page, sku, *, exclude_rowids=None):
        excluded = exclude_rowids or set()
        for row in rows:
            if row["rowid"] not in excluded and _sku_text_matches(row["skuText"], sku):
                return {"checkboxColId": "c1", "splitQtyColId": "c2"}, row
        raise RuntimeError("missing sku")

    async def fake_set(_page, _state, row, _sku, quantity):
        fills.append((row["rowid"], quantity))
        row["splitQty"] = str(quantity)

    monkeypatch.setattr(tent_package_split_adjuster, "_find_split_row_for_sku", fake_find)
    monkeypatch.setattr(tent_package_split_adjuster, "_set_split_row_quantity", fake_set)

    asyncio.run(
        _set_split_item_quantity(
            object(),
            TentPackageSplitItem(sku="TENT-ROLLER-BAG-10X10-50MM", quantity=2),
        )
    )

    assert fills == [("r1", 1), ("r2", 1)]
