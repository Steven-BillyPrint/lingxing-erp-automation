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
    class FakePage:
        def __init__(self):
            self.results = [
                {"ok": True, "changed": True, "opened": True, "current": ""},
                False,
                True,
                True,
            ]
            self.waits = []

        async def evaluate(self, _script, _payload):
            return self.results.pop(0)

        async def wait_for_timeout(self, timeout):
            self.waits.append(timeout)

    page = FakePage()

    changed = asyncio.run(ensure_dialog_warehouse(page, "设定仓库物流"))

    assert changed is True
    assert page.results == []
    assert page.waits == [150, 500]


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
