import asyncio

import lingxing_automation.services.tent_sku_adjuster as adjuster
from lingxing_automation.services.tent_sku_planner import DestinationRegion, TentSkuAdjustmentPlan
from lingxing_automation.services.tent_sku_adjuster import (
    _click_add_product_button,
    _click_result_checkbox,
    _confirm_product_edit_dialog,
    _find_customer_remark_edit_button,
    _find_product_result_row_by_exact_sku,
    _find_quantity_input_in_product_row,
    _merge_instruction_customer_remark,
    _upsert_customer_remark,
    execute_tent_sku_adjustment,
)


class FakeLocator:
    def __init__(
        self,
        selector: str,
        *,
        count: int = 0,
        visible: bool = True,
        click_error: Exception | None = None,
        locators: dict[str, "FakeLocator"] | None = None,
        evaluate_result=None,
    ):
        """初始化测试替身 locator测试替身的内部状态。"""
        self.selector = selector
        self._count = count
        self._visible = visible
        self._click_error = click_error
        self.locators = locators or {}
        self.evaluate_result = evaluate_result
        self.click_count = 0
        self.fill_value = None

    @property
    def first(self):
        """模拟 Playwright 定位器取第一个元素。"""
        return self

    def nth(self, _index: int):
        """模拟 Playwright 定位器按序号取元素。"""
        return self

    def locator(self, selector: str):
        """模拟 Playwright 定位器查询。"""
        return self.locators.get(selector, FakeLocator(selector))

    async def count(self):
        """模拟 Playwright 定位器数量读取。"""
        return self._count

    async def is_visible(self):
        """模拟 Playwright 可见性判断。"""
        return self._visible

    async def click(self, *, timeout: int, force: bool = False):
        """模拟 Playwright 点击动作。"""
        self.click_count += 1
        if self._click_error:
            raise self._click_error

    async def fill(self, value: str):
        """模拟 Playwright 输入动作。"""
        self.fill_value = value

    async def evaluate(self, _script: str):
        """模拟 Playwright 页面脚本执行。"""
        return self.evaluate_result or {}

    async def hover(self, *, timeout: int):
        """模拟 Playwright 悬停动作。"""
        return None


class FakeDialog:
    def __init__(self, locators: dict[str, FakeLocator]):
        """初始化测试替身 dialog测试替身的内部状态。"""
        self.locators = locators
        self.seen_selectors: list[str] = []

    def locator(self, selector: str):
        """模拟 Playwright 定位器查询。"""
        self.seen_selectors.append(selector)
        assert ", text=" not in selector
        return self.locators.get(selector, FakeLocator(selector))


class FakeLocatorList:
    def __init__(self, items):
        """初始化测试替身 locator 列表测试替身的内部状态。"""
        self.items = items

    async def count(self):
        """模拟 Playwright 定位器数量读取。"""
        return len(self.items)

    def nth(self, index: int):
        """模拟 Playwright 定位器按序号取元素。"""
        return self.items[index]


class FakeResultDialog(FakeDialog):
    def __init__(self, rows):
        """初始化测试替身结果 dialog测试替身的内部状态。"""
        super().__init__({})
        self.rows = rows

    def locator(self, selector: str):
        """模拟 Playwright 定位器查询。"""
        if selector == "tr, .vxe-body--row, .el-table__row":
            return FakeLocatorList(self.rows)
        return super().locator(selector)


class FakePage:
    async def wait_for_timeout(self, _timeout_ms: int):
        """模拟 Playwright 等待超时接口。"""
        return None


class FakeKeyboard:
    async def press(self, _key: str):
        """模拟 Playwright 按键动作。"""
        return None


class FakeFooterButton:
    def __init__(self, text: str = "确定", class_name: str = "el-button el-button--primary"):
        """初始化编辑商品底部按钮测试替身。"""

        self.text = text
        self.class_name = class_name
        self.click_count = 0

    async def is_visible(self):
        """模拟按钮可见性判断。"""

        return True

    async def inner_text(self, *, timeout: int):
        """模拟按钮文本读取。"""

        return self.text

    async def get_attribute(self, name: str):
        """模拟按钮 class 属性读取。"""

        return self.class_name if name == "class" else None

    async def click(self, *, timeout: int, force: bool = False):
        """模拟按钮点击。"""

        self.click_count += 1


class FakeFooterButtonList:
    def __init__(self, buttons):
        """初始化编辑商品底部按钮列表测试替身。"""

        self.buttons = buttons
        self.filter_kwargs = []

    def filter(self, **kwargs):
        """模拟 Playwright locator filter。"""

        self.filter_kwargs.append(kwargs)
        return self

    async def count(self):
        """模拟按钮数量读取。"""

        return len(self.buttons)

    def nth(self, index: int):
        """模拟按序号取按钮。"""

        return self.buttons[index]


class FakeProductEditDialog:
    def __init__(self, footer_buttons):
        """初始化编辑商品弹窗测试替身。"""

        self.footer_buttons = FakeFooterButtonList(footer_buttons)
        self.seen_selectors: list[str] = []
        self.wait_calls: list[tuple[str, int]] = []

    def locator(self, selector: str):
        """模拟弹窗 locator 查询，只给 footer 区域返回按钮。"""

        self.seen_selectors.append(selector)
        if "footer" in selector and selector.endswith(" button"):
            return self.footer_buttons
        return FakeFooterButtonList([])

    async def wait_for(self, *, state: str, timeout: int):
        """模拟等待弹窗关闭。"""

        self.wait_calls.append((state, timeout))


class FakeProductEditPage:
    def __init__(self):
        """初始化编辑商品保存页面测试替身。"""

        self.evaluated = False
        self.load_states: list[tuple[str, int]] = []
        self.waits: list[int] = []

    async def evaluate(self, _script: str):
        """模拟页面脚本执行。"""

        self.evaluated = True

    async def wait_for_load_state(self, state: str, *, timeout: int):
        """模拟页面加载状态等待。"""

        self.load_states.append((state, timeout))

    async def wait_for_timeout(self, timeout_ms: int):
        """模拟页面等待。"""

        self.waits.append(timeout_ms)


class FakeRemarkButton:
    def __init__(self):
        """初始化测试替身备注按钮测试替身的内部状态。"""
        self.click_count = 0

    async def scroll_into_view_if_needed(self, *, timeout: int):
        """模拟 Playwright 滚动到可见区域。"""
        return None

    async def hover(self, *, timeout: int):
        """模拟 Playwright 悬停动作。"""
        return None

    async def click(self, *, timeout: int, force: bool = False):
        """模拟 Playwright 点击动作。"""
        self.click_count += 1


class FakeRemarkInput:
    def __init__(self, value: str):
        """初始化测试替身备注输入框测试替身的内部状态。"""
        self.value = value
        self.fill_calls = 0

    async def input_value(self, *, timeout: int):
        """模拟 Playwright 输入框取值。"""
        return self.value

    async def fill(self, value: str):
        """模拟 Playwright 输入动作。"""
        self.fill_calls += 1
        self.value = value


class FakeHandle:
    def __init__(self, element):
        """初始化测试替身 handle测试替身的内部状态。"""
        self.element = element

    def as_element(self):
        """模拟 Playwright 句柄转元素。"""
        return self.element


class FakeEvaluatePage(FakePage):
    def __init__(self, element):
        """初始化测试替身执行脚本 page测试替身的内部状态。"""
        self.element = element
        self.evaluate_script = ""
        self.evaluate_arg = None

    async def evaluate_handle(self, script: str, arg):
        """模拟 Playwright 返回句柄的脚本执行。"""
        self.evaluate_script = script
        self.evaluate_arg = arg
        return FakeHandle(self.element)


class FakeDeadlinePage(FakePage):
    def __init__(self, evaluate_result: str):
        """初始化测试替身截止日期 page测试替身的内部状态。"""
        self.evaluate_result = evaluate_result
        self.evaluate_script = ""
        self.evaluate_arg = None

    async def evaluate(self, script: str, arg):
        """模拟 Playwright 页面脚本执行。"""
        self.evaluate_script = script
        self.evaluate_arg = arg
        return self.evaluate_result


def _plan_with_customer_remark(remark: str = "7.3发说明书") -> TentSkuAdjustmentPlan:
    """构造带客户备注的帐篷 SKU 调整计划。"""
    return TentSkuAdjustmentPlan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        destination=DestinationRegion(raw_text="", country="US", state="TX", category="us_mainland"),
        replace_main_sku="Instruction",
        customer_remark=remark,
    )


def _run_remark_upsert(existing_text: str, remark: str = "7.3发说明书"):
    """运行客户备注写回测试场景并返回结果。"""
    button = FakeRemarkButton()
    input_locator = FakeRemarkInput(existing_text)
    calls = {"confirm": 0, "close": 0}

    old_find_button = adjuster._find_customer_remark_edit_button
    old_find_input = adjuster._find_customer_remark_editor_input
    old_confirm = adjuster._confirm_customer_remark_editor
    old_close = adjuster._close_customer_remark_editor

    async def fake_find_button(_page, *, system_order_no, platform_order_no):
        """模拟查找按钮行为，隔离测试中的外部依赖。"""
        assert system_order_no == "103700000000000000"
        assert platform_order_no == "111-0000000-0000000"
        return button

    async def fake_find_input(_page):
        """模拟查找输入框行为，隔离测试中的外部依赖。"""
        return object(), input_locator

    async def fake_confirm(_page, _editor):
        """模拟确认行为，隔离测试中的外部依赖。"""
        calls["confirm"] += 1

    async def fake_close(_page, _editor):
        """模拟关闭行为，隔离测试中的外部依赖。"""
        calls["close"] += 1

    adjuster._find_customer_remark_edit_button = fake_find_button
    adjuster._find_customer_remark_editor_input = fake_find_input
    adjuster._confirm_customer_remark_editor = fake_confirm
    adjuster._close_customer_remark_editor = fake_close
    try:
        action = asyncio.run(_upsert_customer_remark(FakePage(), _plan_with_customer_remark(remark)))
    finally:
        adjuster._find_customer_remark_edit_button = old_find_button
        adjuster._find_customer_remark_editor_input = old_find_input
        adjuster._confirm_customer_remark_editor = old_confirm
        adjuster._close_customer_remark_editor = old_close
    return action, input_locator, button, calls


def test_click_add_product_button_prefers_button_locator():
    """验证帐篷 SKU 页面调整中的点击添加产品按钮优先使用按钮 定位器场景。"""
    button = FakeLocator("button:has-text('添加商品')", count=1)
    text = FakeLocator("text=添加商品", count=1)
    dialog = FakeDialog(
        {
            "button:has-text('添加商品')": button,
            "text=添加商品": text,
        }
    )

    asyncio.run(_click_add_product_button(dialog))

    assert button.click_count == 1
    assert text.click_count == 0
    assert dialog.seen_selectors == ["button:has-text('添加商品')"]


def test_click_add_product_button_falls_back_to_text_locator():
    """验证帐篷 SKU 页面调整中的点击添加产品按钮 回退到 到文本 定位器场景。"""
    text = FakeLocator("text=添加商品", count=1)
    dialog = FakeDialog(
        {
            "button:has-text('添加商品')": FakeLocator("button:has-text('添加商品')", count=0),
            "text=添加商品": text,
        }
    )

    asyncio.run(_click_add_product_button(dialog))

    assert text.click_count == 1
    assert dialog.seen_selectors == ["button:has-text('添加商品')", "text=添加商品"]


def test_click_add_product_button_falls_back_when_button_click_fails():
    """验证帐篷 SKU 页面调整中的点击添加产品按钮 回退到 当按钮点击失败场景。"""
    button = FakeLocator("button:has-text('添加商品')", count=1, click_error=TimeoutError("hidden"))
    text = FakeLocator("text=添加商品", count=1)
    dialog = FakeDialog(
        {
            "button:has-text('添加商品')": button,
            "text=添加商品": text,
        }
    )

    asyncio.run(_click_add_product_button(dialog))

    assert button.click_count == 1
    assert text.click_count == 1
    assert dialog.seen_selectors == ["button:has-text('添加商品')", "text=添加商品"]


def test_click_result_checkbox_supports_vxe_checkbox_in_result_row():
    """验证帐篷 SKU 页面调整中的点击结果复选框支持vxe 表格复选框在结果行场景。"""
    checkbox = FakeLocator(".vxe-cell--checkbox", count=1)
    row = FakeLocator(
        "row",
        count=1,
        locators={
            ".vxe-cell--checkbox": checkbox,
        },
    )
    dialog = FakeDialog({})

    asyncio.run(_click_result_checkbox(dialog, row, "10x10-Canopy-Topper"))

    assert checkbox.click_count == 1


def test_find_quantity_input_scopes_to_sku_product_row_quantity_field():
    """验证帐篷 SKU 页面调整中的查找数量输入框限定范围到SKU产品行数量字段场景。"""
    quantity_input = FakeLocator(
        'td[colid="col_88"] .detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
        count=1,
    )
    readonly_stock_owner_input = FakeLocator(
        '.detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])',
        count=0,
    )
    row = FakeLocator(
        "tr.vxe-body--row",
        count=1,
        locators={
            'td[colid="col_88"] .detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])': quantity_input,
            '.detail-box-right .tiny-edit-input input.el-input__inner:not([readonly]):not([disabled])': readonly_stock_owner_input,
        },
    )

    found = asyncio.run(_find_quantity_input_in_product_row(row, "10ft-Half-Wall"))
    asyncio.run(found.fill("2"))

    assert found is quantity_input
    assert quantity_input.fill_value == "2"


def test_find_product_result_row_requires_exact_sku_not_partial_prefix():
    """验证帐篷 SKU 页面调整中的查找产品结果行要求精确SKU不部分前缀场景。"""
    stale_double_sided = FakeLocator(
        "double-sided-row",
        count=1,
        evaluate_result={
            "rowText": "3米半围双面 10ft-Half-Wall-Double-Sided",
            "cellTexts": ["3米半围双面", "10ft-Half-Wall-Double-Sided", "-", "BillyPrint"],
            "skuFromLabel": None,
            "skuCell": "10ft-Half-Wall-Double-Sided",
        },
    )
    exact_wall = FakeLocator(
        "exact-row",
        count=1,
        evaluate_result={
            "rowText": "3米半围 10ft-Half-Wall",
            "cellTexts": ["3米半围", "10ft-Half-Wall", "-", "BillyPrint"],
            "skuFromLabel": None,
            "skuCell": "10ft-Half-Wall",
        },
    )
    dialog = FakeResultDialog([stale_double_sided, exact_wall])

    found = asyncio.run(_find_product_result_row_by_exact_sku(FakePage(), dialog, "10ft-Half-Wall"))

    assert found is exact_wall


def test_find_product_result_row_rejects_partial_match_for_all_skus():
    """验证帐篷 SKU 页面调整中的查找产品结果行拒绝部分匹配用于全部 skus场景。"""
    partial_only = FakeLocator(
        "partial-row",
        count=1,
        evaluate_result={
            "rowText": "Some Product ABC-OLD",
            "cellTexts": ["Some Product", "ABC-OLD", "-", "BillyPrint"],
            "skuFromLabel": None,
            "skuCell": "ABC-OLD",
        },
    )
    dialog = FakeResultDialog([partial_only])

    try:
        asyncio.run(_find_product_result_row_by_exact_sku(FakePage(), dialog, "ABC"))
    except RuntimeError as exc:
        assert "精确等于 ABC" in str(exc)
    else:
        raise AssertionError("partial SKU match must not be accepted")


def test_read_list_shipping_deadline_uses_header_column_value():
    """验证帐篷 SKU 页面调整中的读取列表 发货截止日期 使用表头列值场景。"""
    page = FakeDeadlinePage("2026-07-04 14:59:59")

    value = asyncio.run(
        adjuster.read_list_shipping_deadline_text(
            page,
            system_order_no="103715528829012542",
            platform_order_no="114-0687646-4109850",
        )
    )

    assert value == "2026-07-04 14:59:59"
    assert page.evaluate_arg == {
        "systemOrderNo": "103715528829012542",
        "platformOrderNo": "114-0687646-4109850",
        "headerText": "发货时限",
    }
    assert "剩余发货" not in page.evaluate_script
    assert "发货时限" not in page.evaluate_script


def test_read_list_shipping_deadline_rejects_remaining_shipping_or_row_text():
    """验证帐篷 SKU 页面调整中的读取列表 发货截止日期 拒绝剩余收货或行文本场景。"""
    page = FakeDeadlinePage("4天5小时09分钟")
    old_find_row = adjuster._find_order_row

    async def fake_find_row(_page, *, system_order_no, platform_order_no):
        """模拟查找行行为，隔离测试中的外部依赖。"""
        assert system_order_no == "103716934190489909"
        assert platform_order_no == "111-8155945-9921066"
        return FakeLocator("row", locators={'td[colid="col_29"]': FakeLocator('td[colid="col_29"]', count=0)})

    adjuster._find_order_row = fake_find_row
    try:
        value = asyncio.run(
            adjuster.read_list_shipping_deadline_text(
                page,
                system_order_no="103716934190489909",
                platform_order_no="111-8155945-9921066",
            )
        )
    finally:
        adjuster._find_order_row = old_find_row

    assert value == ""


def test_merge_instruction_customer_remark_appends_replaces_and_skips_duplicate():
    """验证帐篷 SKU 页面调整中的合并说明书 客户备注 追加替换并跳过重复场景。"""
    assert _merge_instruction_customer_remark("已有备注", "7.3发说明书") == (
        "已有备注\n7.3发说明书",
        "append",
    )
    assert _merge_instruction_customer_remark("0701发说明书\n已有备注", "7.3发说明书") == (
        "7.3发说明书\n已有备注",
        "replace",
    )
    assert _merge_instruction_customer_remark("7.1发说明书\n已有备注", "7.3发说明书") == (
        "7.3发说明书\n已有备注",
        "replace",
    )
    assert _merge_instruction_customer_remark("已有备注\n7.3发说明书", "7.3发说明书") == (
        "已有备注\n7.3发说明书",
        "skip",
    )


def test_find_customer_remark_edit_button_uses_dom_header_and_order_identity():
    """验证帐篷 SKU 页面调整中的查找 客户备注 编辑按钮使用DOM表头并订单 identity场景。"""
    button = FakeRemarkButton()
    page = FakeEvaluatePage(button)

    found = asyncio.run(
        _find_customer_remark_edit_button(
            page,
            system_order_no="103700000000000000",
            platform_order_no="111-0000000-0000000",
        )
    )

    assert found is button
    assert "客服备注" in page.evaluate_script
    assert page.evaluate_arg == {
        "systemOrderNo": "103700000000000000",
        "platformOrderNo": "111-0000000-0000000",
    }


def test_upsert_customer_remark_appends_without_overwriting_existing_text():
    """验证帐篷 SKU 页面调整中的upsert 客户备注 追加不依赖 overwriting 已存在文本场景。"""
    action, input_locator, button, calls = _run_remark_upsert("已有备注")

    assert action == "append"
    assert input_locator.value == "已有备注\n7.3发说明书"
    assert input_locator.fill_calls == 1
    assert button.click_count == 1
    assert calls == {"confirm": 1, "close": 0}


def test_upsert_customer_remark_replaces_old_instruction_remark():
    """验证帐篷 SKU 页面调整中的upsert 客户备注 替换旧说明书备注场景。"""
    action, input_locator, _button, calls = _run_remark_upsert("0701发说明书\n已有备注")

    assert action == "replace"
    assert input_locator.value == "7.3发说明书\n已有备注"
    assert input_locator.fill_calls == 1
    assert calls == {"confirm": 1, "close": 0}


def test_upsert_customer_remark_skips_duplicate_instruction_remark():
    """验证帐篷 SKU 页面调整中的upsert 客户备注 跳过重复说明书备注场景。"""
    action, input_locator, _button, calls = _run_remark_upsert("已有备注\n7.3发说明书")

    assert action == "skip"
    assert input_locator.value == "已有备注\n7.3发说明书"
    assert input_locator.fill_calls == 0
    assert calls == {"confirm": 0, "close": 1}


def test_execute_tent_sku_adjustment_stops_before_sku_when_remark_write_fails():
    """验证帐篷 SKU 页面调整中的execute 帐篷 SKU调整停止之前SKU当备注写入失败场景。"""
    opened_product_editor = {"called": False}
    page = FakePage()
    page.keyboard = FakeKeyboard()

    old_find_row = adjuster._find_order_row
    old_upsert = adjuster._upsert_customer_remark
    old_open_product = adjuster._open_product_edit_dialog
    old_cancel = adjuster._cancel_visible_dialogs

    async def fake_find_row(_page, *, system_order_no, platform_order_no):
        """模拟查找行行为，隔离测试中的外部依赖。"""
        return object()

    async def fake_upsert(_page, _plan):
        """模拟upsert行为，隔离测试中的外部依赖。"""
        raise RuntimeError("客服备注写入失败")

    async def fake_open_product(_page, _row):
        """模拟打开产品行为，隔离测试中的外部依赖。"""
        opened_product_editor["called"] = True
        return object()

    async def fake_cancel(_page):
        """模拟取消行为，隔离测试中的外部依赖。"""
        return None

    adjuster._find_order_row = fake_find_row
    adjuster._upsert_customer_remark = fake_upsert
    adjuster._open_product_edit_dialog = fake_open_product
    adjuster._cancel_visible_dialogs = fake_cancel
    try:
        result = asyncio.run(execute_tent_sku_adjustment(page, _plan_with_customer_remark()))
    finally:
        adjuster._find_order_row = old_find_row
        adjuster._upsert_customer_remark = old_upsert
        adjuster._open_product_edit_dialog = old_open_product
        adjuster._cancel_visible_dialogs = old_cancel

    assert result.status == "sku_adjustment_error"
    assert "客服备注写入失败" in (result.error or "")
    assert opened_product_editor["called"] is False


def test_confirm_product_edit_dialog_clicks_footer_confirm_and_waits_for_close():
    """验证编辑商品最终保存只点击底部确定并等待保存后的关闭和稳定。"""

    page = FakeProductEditPage()
    confirm_button = FakeFooterButton()
    dialog = FakeProductEditDialog([confirm_button])

    asyncio.run(_confirm_product_edit_dialog(page, dialog))

    assert page.evaluated is True
    assert confirm_button.click_count == 1
    assert dialog.wait_calls == [("hidden", 12000)]
    assert page.load_states == [("networkidle", 5000)]
    assert page.waits == [1200]
    assert all("button:has-text" not in selector for selector in dialog.seen_selectors)
