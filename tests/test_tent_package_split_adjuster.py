import asyncio

import pytest

from lingxing_automation.services.tent_package_split_adjuster import (
    _can_scroll_down,
    _clear_split_row_quantity,
    _find_split_row_for_sku,
    _matching_any_row,
    _matching_visible_row,
    _split_package_from_original,
    _set_split_item_quantity,
    _set_split_row_quantity,
    _sku_text_matches,
)
from lingxing_automation.services import tent_package_split_adjuster
from lingxing_automation.services.tent_package_split_planner import TentPackageSplitItem, TentPackageSplitPackage


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


class FakeLocator:
    def __init__(self, items):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeInput:
    def __init__(self, value="0"):
        self.value = value

    async def count(self):
        return 1

    def nth(self, index):
        if index != 0:
            raise IndexError(index)
        return self

    async def fill(self, value, timeout=None):
        self.value = value


class FakeCheckbox:
    def __init__(self, checked=False):
        self.checked = checked

    async def count(self):
        return 1

    def nth(self, index):
        if index != 0:
            raise IndexError(index)
        return self

    async def get_attribute(self, name):
        if name == "class" and self.checked:
            return "vxe-cell--checkbox is--checked"
        if name == "class":
            return "vxe-cell--checkbox"
        return None

    async def click(self, timeout=None):
        self.checked = not self.checked


class FakeRowElement:
    def __init__(self, rowid, text, *, split_value="0", checked=False):
        self.rowid = rowid
        self.text = text
        self.split_input = FakeInput(split_value)
        self.checkbox = FakeCheckbox(checked)

    async def is_visible(self):
        return True

    async def inner_text(self, timeout=None):
        return self.text

    def locator(self, selector):
        if ".vxe-cell--checkbox" in selector:
            return self.checkbox
        if "input.el-input__inner" in selector:
            return self.split_input
        return FakeLocator([])


class FakeTableElement:
    def __init__(self, rows):
        self.rows = list(rows)

    async def is_visible(self):
        return True

    def locator(self, selector):
        if selector.startswith("tr.vxe-body--row"):
            if "rowid=\"" in selector:
                rowid = selector.split('rowid="', 1)[1].split('"', 1)[0]
                return FakeLocator([row for row in self.rows if row.rowid == rowid])
            return FakeLocator(self.rows)
        return FakeLocator([])


class FakeCardElement:
    def __init__(self, text, table):
        self.text = text
        self.table = table

    async def is_visible(self):
        return True

    async def inner_text(self, timeout=None):
        return self.text

    def locator(self, selector):
        if selector == ".vxe-table":
            return FakeLocator([self.table])
        return FakeLocator([])


class FakeDialogElement:
    def __init__(self, cards):
        self.cards = list(cards)

    def locator(self, selector):
        if selector == ".splitList_warp":
            return FakeLocator(self.cards)
        if selector == ".el-card":
            return FakeLocator(self.cards)
        return FakeLocator([])


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
    assert _sku_text_matches("3x3m帐篷40mm方形铝架 10X10-FRAME-40MM-SQUARE", "10X10-FRAME-40MM-SQUARE")
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


def test_matching_visible_row_accepts_extracted_sku_value_from_product_cell():
    state = _state(
        rows=[
            {
                "rowid": "r1",
                "skuText": "3x3m帐篷40mm方形铝架 10X10-FRAME-40MM-SQUARE",
                "skuValue": "10X10-FRAME-40MM-SQUARE",
                "shipQty": "2",
                "visibleInsideWrapper": True,
            }
        ]
    )

    assert _matching_visible_row(state, "10X10-FRAME-40MM-SQUARE")["rowid"] == "r1"


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


def test_split_package_resets_original_inputs_before_selecting(monkeypatch):
    calls: list[str] = []
    counts = [1, 2]

    async def fake_count(_page):
        return counts.pop(0)

    async def fake_reset(_page):
        calls.append("reset")

    async def fake_set(_page, item):
        calls.append(f"set:{item.sku}")

    async def fake_click(_dialog, label):
        calls.append(f"click:{label}")

    monkeypatch.setattr(tent_package_split_adjuster, "_count_split_packages", fake_count)
    monkeypatch.setattr(tent_package_split_adjuster, "_reset_original_package_split_inputs", fake_reset)
    monkeypatch.setattr(tent_package_split_adjuster, "_set_split_item_quantity", fake_set)
    monkeypatch.setattr(tent_package_split_adjuster, "_click_dialog_button", fake_click)

    asyncio.run(
        _split_package_from_original(
            FakeSplitPage([]),
            object(),
            TentPackageSplitPackage(
                package_key="frame-1",
                title="支架包1",
                items=[TentPackageSplitItem(sku="10X10-FRAME-40MM-SQUARE", quantity=1)],
            ),
        )
    )

    assert calls == ["reset", "set:10X10-FRAME-40MM-SQUARE", "click:拆分成新包裹"]


def test_main_product_package_reports_post_replacement_sku_mismatch(monkeypatch):
    async def fake_count(_page):
        return 1

    async def fake_reset(_page):
        return None

    async def fake_set(_page, item):
        raise RuntimeError(f"拆分弹窗中没有找到 SKU 精确等于 {item.sku} 的可见行。")

    monkeypatch.setattr(tent_package_split_adjuster, "_count_split_packages", fake_count)
    monkeypatch.setattr(tent_package_split_adjuster, "_reset_original_package_split_inputs", fake_reset)
    monkeypatch.setattr(tent_package_split_adjuster, "_set_split_item_quantity", fake_set)

    with pytest.raises(RuntimeError, match="换货结果与拆包计划不一致"):
        asyncio.run(
            _split_package_from_original(
                FakeSplitPage([]),
                object(),
                TentPackageSplitPackage(
                    package_key="main-products",
                    title="主图商品包",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
            )
        )


def test_main_product_package_combines_duplicate_sku_before_single_split(monkeypatch):
    calls: list[tuple[str, int] | str] = []
    package_counts = iter([1, 2])

    async def fake_count(_page):
        return next(package_counts)

    async def fake_reset(_page):
        calls.append("reset")

    async def fake_set(_page, item):
        calls.append((item.sku, item.quantity))

    async def fake_click(_dialog, text):
        calls.append(text)

    monkeypatch.setattr(tent_package_split_adjuster, "_count_split_packages", fake_count)
    monkeypatch.setattr(tent_package_split_adjuster, "_reset_original_package_split_inputs", fake_reset)
    monkeypatch.setattr(tent_package_split_adjuster, "_set_split_item_quantity", fake_set)
    monkeypatch.setattr(tent_package_split_adjuster, "_click_dialog_button", fake_click)

    asyncio.run(
        _split_package_from_original(
            FakeSplitPage([]),
            object(),
            TentPackageSplitPackage(
                package_key="main-products",
                title="主图商品包",
                items=[
                    TentPackageSplitItem(sku="Instruction", quantity=1),
                    TentPackageSplitItem(sku="Instruction", quantity=1),
                ],
            ),
        )
    )

    assert calls == ["reset", ("Instruction", 2), "拆分成新包裹"]


def test_set_split_item_quantity_clears_stale_split_qty_before_excluding_row(monkeypatch):
    row = {
        "rowid": "r1",
        "skuText": "3x3m帐篷40mm方形铝架 10X10-FRAME-40MM-SQUARE",
        "skuValue": "10X10-FRAME-40MM-SQUARE",
        "shipQty": "1",
        "splitQty": "1",
    }
    exclude_history: list[set[str]] = []
    clears: list[str] = []
    fills: list[tuple[str, int]] = []

    async def fake_find(_page, sku, *, exclude_rowids=None):
        excluded = set(exclude_rowids or set())
        exclude_history.append(excluded)
        if row["rowid"] in excluded:
            raise RuntimeError("stale row was excluded before reset")
        if not _sku_text_matches(row["skuValue"], sku):
            raise RuntimeError("missing sku")
        return {"checkboxColId": "c1", "splitQtyColId": "c2"}, row

    async def fake_clear(_page, _state, stale_row, _sku):
        clears.append(stale_row["rowid"])
        stale_row["splitQty"] = "0"

    async def fake_set(_page, _state, selected_row, _sku, quantity):
        fills.append((selected_row["rowid"], quantity))
        selected_row["splitQty"] = str(quantity)

    monkeypatch.setattr(tent_package_split_adjuster, "_find_split_row_for_sku", fake_find)
    monkeypatch.setattr(tent_package_split_adjuster, "_clear_split_row_quantity", fake_clear)
    monkeypatch.setattr(tent_package_split_adjuster, "_set_split_row_quantity", fake_set)

    page = FakeSplitPage([])
    asyncio.run(
        _set_split_item_quantity(
            page,
            TentPackageSplitItem(sku="10X10-FRAME-40MM-SQUARE", quantity=1),
        )
    )

    assert exclude_history == [set(), set()]
    assert clears == ["r1"]
    assert fills == [("r1", 1)]
    assert page.waits == [100]


def test_set_and_clear_split_row_quantity_scope_to_original_package(monkeypatch):
    original_row = FakeRowElement(
        "r1",
        "3x3m帐篷40mm方形铝架 10X10-FRAME-40MM-SQUARE",
        split_value="0",
        checked=False,
    )
    split_package_row = FakeRowElement(
        "r1",
        "3x3m帐篷40mm方形铝架 10X10-FRAME-40MM-SQUARE",
        split_value="0",
        checked=False,
    )
    dialog = FakeDialogElement(
        [
            FakeCardElement("订单包裹 1", FakeTableElement([original_row])),
            FakeCardElement("订单包裹 3", FakeTableElement([split_package_row])),
        ]
    )

    async def fake_visible_dialog(_page, _title, timeout_ms=0):
        return dialog

    monkeypatch.setattr(tent_package_split_adjuster, "_visible_dialog_by_header_title", fake_visible_dialog)
    state = {"checkboxColId": "c1", "splitQtyColId": "c2"}
    row = {"rowid": "r1", "shipQty": "1", "splitQty": "0"}

    asyncio.run(
        _set_split_row_quantity(
            object(),
            state,
            row,
            "10X10-FRAME-40MM-SQUARE",
            1,
        )
    )

    assert original_row.checkbox.checked is True
    assert original_row.split_input.value == "1"
    assert split_package_row.checkbox.checked is False
    assert split_package_row.split_input.value == "0"

    asyncio.run(
        _clear_split_row_quantity(
            object(),
            state,
            {"rowid": "r1", "shipQty": "1", "splitQty": "1"},
            "10X10-FRAME-40MM-SQUARE",
        )
    )

    assert original_row.checkbox.checked is False
    assert original_row.split_input.value == "0"
    assert split_package_row.checkbox.checked is False
    assert split_package_row.split_input.value == "0"
