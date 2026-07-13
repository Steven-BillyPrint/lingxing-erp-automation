import asyncio

import pytest

from lingxing_automation.pages.order_table_actions import (
    dismiss_outbound_success_dialog,
    ensure_dialog_warehouse,
    select_cascader_path,
)


class FakeCascaderNodes:
    def __init__(self, page, level):
        self.page = page
        self.level = level

    async def count(self):
        return len(self.page.node_counts[self.level])

    def nth(self, index):
        page = self.page
        level = self.level

        class Target:
            async def click(self):
                page.pointer_clicks.append((level, index))

        return Target()


class FakeCascaderMenu:
    def __init__(self, page, level):
        self.page = page
        self.level = level

    def locator(self, selector):
        assert selector == ".el-cascader-node"
        return FakeCascaderNodes(self.page, self.level)


class FakeCascaderMenus:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return len(self.page.node_counts)

    def nth(self, level):
        return FakeCascaderMenu(self.page, level)


class FakeCascaderPage:
    def __init__(self, level_results, node_counts=None):
        self.level_results = {level: list(results) for level, results in level_results.items()}
        self.node_counts = node_counts or {level: [None] for level in level_results}
        self.calls = []
        self.waits = []
        self.pointer_clicks = []

    async def evaluate(self, _script, payload):
        self.calls.append(payload)
        if "dialogText" in payload:
            return True
        level = payload["level"]
        return self.level_results[level].pop(0)

    def locator(self, selector):
        assert selector == ".el-cascader-menu:visible"
        return FakeCascaderMenus(self)

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


class FakeWarehouseList:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeWarehouseInput:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.input_clicks += 1


class FakeWarehouseCell:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text


class FakeWarehouseButton:
    def __init__(self, page, warehouse_name):
        self.page = page
        self.warehouse_name = warehouse_name

    async def click(self):
        self.page.selected_warehouses.append(self.warehouse_name)


class FakeWarehouseRow:
    def __init__(self, page, warehouse_name):
        self.page = page
        self.warehouse_name = warehouse_name

    def locator(self, selector):
        assert selector == "td"
        return FakeWarehouseList([FakeWarehouseCell(self.warehouse_name)])

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "选择", True)
        return FakeWarehouseList([FakeWarehouseButton(self.page, self.warehouse_name)])


class FakeWarehousePopover:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        assert selector == "tr:visible"
        names = self.page.current_warehouse_snapshot or []
        return FakeWarehouseList([FakeWarehouseRow(self.page, name) for name in names])


class FakeWarehousePopovers:
    def __init__(self, page):
        self.page = page

    def filter(self, *, has_text):
        assert has_text == "仓库"
        return self

    async def count(self):
        snapshots = self.page.warehouse_snapshots
        if len(snapshots) > 1:
            self.page.current_warehouse_snapshot = snapshots.pop(0)
        else:
            self.page.current_warehouse_snapshot = snapshots[0]
        return 0 if self.page.current_warehouse_snapshot is None else 1

    def nth(self, index):
        assert index == 0
        return FakeWarehousePopover(self.page)


class FakeWarehouseDialog:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        assert selector == 'input.el-input__inner:visible, input:visible'
        return FakeWarehouseList([FakeWarehouseInput(self.page)])


class FakeWarehouseDialogs:
    def __init__(self, page):
        self.page = page

    def filter(self, *, has_text):
        assert has_text == "设定仓库物流"
        return self

    async def count(self):
        return 1

    def nth(self, index):
        assert index == 0
        return FakeWarehouseDialog(self.page)


class FakeWarehousePage:
    def __init__(self, warehouse_snapshots, *, verified=True):
        self.warehouse_snapshots = list(warehouse_snapshots)
        self.current_warehouse_snapshot = None
        self.verified = verified
        self.evaluate_calls = 0
        self.input_clicks = 0
        self.selected_warehouses = []
        self.waits = []

    async def evaluate(self, _script, _payload):
        self.evaluate_calls += 1
        if self.evaluate_calls == 1:
            return {"ok": True, "changed": True, "current": "", "inputIndex": 0}
        return self.verified

    def locator(self, selector):
        if selector == '.el-dialog:visible, [role="dialog"]:visible':
            return FakeWarehouseDialogs(self)
        assert selector == '[role="tooltip"]:visible, .el-tooltip__popper:visible, .el-popover:visible'
        return FakeWarehousePopovers(self)

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


def test_select_cascader_path_scrolls_each_column_independently():
    page = FakeCascaderPage(
        {
            0: [
                {"clicked": False, "canContinue": True, "reset": True, "labels": []},
                {"clicked": False, "canContinue": True, "labels": ["手动"]},
                {
                    "clicked": False,
                    "readyForPointerClick": True,
                    "targetIndex": 0,
                    "canContinue": False,
                    "labels": ["手动-Alibaba logistics"],
                },
            ],
            1: [
                {"clicked": False, "canContinue": True, "reason": "menu_missing", "labels": []},
                {"clicked": False, "canContinue": True, "reset": True, "labels": []},
                {"clicked": False, "canContinue": True, "labels": ["DHL-阿里巴巴"]},
                {
                    "clicked": False,
                    "readyForPointerClick": True,
                    "targetIndex": 0,
                    "canContinue": False,
                    "labels": ["Fedex-阿里巴巴"],
                },
            ],
        }
    )

    asyncio.run(
        select_cascader_path(
            page,
            "设定仓库物流",
            "物流渠道",
            ["手动-Alibaba logistics", "Fedex-阿里巴巴"],
        )
    )

    option_calls = [call for call in page.calls if "level" in call]
    assert [call["level"] for call in option_calls] == [0, 0, 0, 1, 1, 1, 1]
    assert [call["reset"] for call in option_calls] == [
        True,
        False,
        False,
        True,
        True,
        False,
        False,
    ]
    assert option_calls[-1]["value"] == "Fedex-阿里巴巴"
    assert page.pointer_clicks == [(0, 0), (1, 0)]
    assert page.waits.count(120) == 5
    assert page.waits.count(450) == 2


def test_select_cascader_path_reports_scanned_options_after_reaching_bottom():
    page = FakeCascaderPage(
        {
            0: [
                {"clicked": False, "canContinue": True, "reset": True, "labels": []},
                {"clicked": False, "canContinue": True, "labels": ["手动"]},
                {"clicked": False, "canContinue": False, "labels": ["手动-邮政"]},
            ]
        }
    )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            select_cascader_path(
                page,
                "设定仓库物流",
                "物流渠道",
                ["手动-Alibaba logistics"],
            )
        )

    message = str(exc_info.value)
    assert "手动-Alibaba logistics" in message
    assert "已滚动到底" in message
    assert "手动" in message
    assert "手动-邮政" in message


def test_ensure_dialog_warehouse_keeps_existing_default():
    class FakePage:
        def __init__(self):
            self.calls = []

        async def evaluate(self, _script, payload):
            self.calls.append(payload)
            return {"ok": True, "changed": False, "current": "默认仓库"}

    page = FakePage()

    changed = asyncio.run(ensure_dialog_warehouse(page, "设定仓库物流"))

    assert changed is False
    assert page.calls == [{"dialogText": "设定仓库物流", "warehouseName": "默认仓库"}]


def test_ensure_dialog_warehouse_selects_and_verifies_default():
    delayed_snapshots = [None] * 35 + [["港通 新泽西仓", "默认仓库"]]
    page = FakeWarehousePage(delayed_snapshots)

    changed = asyncio.run(ensure_dialog_warehouse(page, "设定仓库物流"))

    assert changed is True
    assert page.input_clicks == 1
    assert page.selected_warehouses == ["默认仓库"]
    assert page.waits == [150] * 35 + [500]


def test_ensure_dialog_warehouse_reports_when_list_does_not_open():
    page = FakeWarehousePage([None])

    with pytest.raises(RuntimeError, match="发货仓库列表未展开"):
        asyncio.run(ensure_dialog_warehouse(page, "设定仓库物流"))

    assert page.input_clicks == 1
    assert page.selected_warehouses == []


def test_ensure_dialog_warehouse_reports_scanned_names_when_default_missing():
    page = FakeWarehousePage([["港通 新泽西仓", "港通 洛杉矶仓"]])

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(ensure_dialog_warehouse(page, "设定仓库物流"))

    message = str(exc_info.value)
    assert "没有在发货仓库列表中找到：默认仓库" in message
    assert "港通 新泽西仓" in message
    assert "港通 洛杉矶仓" in message


def test_dismiss_outbound_success_dialog_waits_for_prompt_and_close():
    class FakePage:
        def __init__(self):
            self.results = [
                {"found": False, "clicked": False},
                {"found": True, "clicked": True, "method": "acknowledge"},
                {"found": True, "clicked": True, "method": "acknowledge"},
                {"found": False, "clicked": False},
            ]
            self.waits = []

        async def evaluate(self, _script):
            return self.results.pop(0)

        async def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    page = FakePage()

    asyncio.run(dismiss_outbound_success_dialog(page))

    assert page.results == []
    assert page.waits == [300, 300, 300]
