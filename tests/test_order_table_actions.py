import asyncio

import pytest

from lingxing_automation.pages.order_table_actions import select_cascader_path


class FakeCascaderPage:
    def __init__(self, level_results):
        self.level_results = {level: list(results) for level, results in level_results.items()}
        self.calls = []
        self.waits = []

    async def evaluate(self, _script, payload):
        self.calls.append(payload)
        if "dialogText" in payload:
            return True
        level = payload["level"]
        return self.level_results[level].pop(0)

    async def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


def test_select_cascader_path_scrolls_each_column_independently():
    page = FakeCascaderPage(
        {
            0: [
                {"clicked": False, "canContinue": True, "reset": True, "labels": []},
                {"clicked": False, "canContinue": True, "labels": ["手动"]},
                {"clicked": True, "canContinue": False, "labels": ["手动-Alibaba logistics"]},
            ],
            1: [
                {"clicked": False, "canContinue": True, "reason": "menu_missing", "labels": []},
                {"clicked": False, "canContinue": True, "reset": True, "labels": []},
                {"clicked": False, "canContinue": True, "labels": ["DHL-阿里巴巴"]},
                {"clicked": True, "canContinue": False, "labels": ["Fedex-阿里巴巴"]},
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
