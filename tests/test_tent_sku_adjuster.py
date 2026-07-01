import asyncio

import lingxing_automation.services.tent_sku_adjuster as adjuster
from lingxing_automation.services.tent_sku_planner import DestinationRegion, TentSkuAdjustmentPlan
from lingxing_automation.services.tent_sku_adjuster import (
    _click_add_product_button,
    _click_result_checkbox,
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
        return self

    def nth(self, _index: int):
        return self

    def locator(self, selector: str):
        return self.locators.get(selector, FakeLocator(selector))

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def click(self, *, timeout: int, force: bool = False):
        self.click_count += 1
        if self._click_error:
            raise self._click_error

    async def fill(self, value: str):
        self.fill_value = value

    async def evaluate(self, _script: str):
        return self.evaluate_result or {}

    async def hover(self, *, timeout: int):
        return None


class FakeDialog:
    def __init__(self, locators: dict[str, FakeLocator]):
        self.locators = locators
        self.seen_selectors: list[str] = []

    def locator(self, selector: str):
        self.seen_selectors.append(selector)
        assert ", text=" not in selector
        return self.locators.get(selector, FakeLocator(selector))


class FakeLocatorList:
    def __init__(self, items):
        self.items = items

    async def count(self):
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class FakeResultDialog(FakeDialog):
    def __init__(self, rows):
        super().__init__({})
        self.rows = rows

    def locator(self, selector: str):
        if selector == "tr, .vxe-body--row, .el-table__row":
            return FakeLocatorList(self.rows)
        return super().locator(selector)


class FakePage:
    async def wait_for_timeout(self, _timeout_ms: int):
        return None


class FakeKeyboard:
    async def press(self, _key: str):
        return None


class FakeRemarkButton:
    def __init__(self):
        self.click_count = 0

    async def scroll_into_view_if_needed(self, *, timeout: int):
        return None

    async def hover(self, *, timeout: int):
        return None

    async def click(self, *, timeout: int, force: bool = False):
        self.click_count += 1


class FakeRemarkInput:
    def __init__(self, value: str):
        self.value = value
        self.fill_calls = 0

    async def input_value(self, *, timeout: int):
        return self.value

    async def fill(self, value: str):
        self.fill_calls += 1
        self.value = value


class FakeHandle:
    def __init__(self, element):
        self.element = element

    def as_element(self):
        return self.element


class FakeEvaluatePage(FakePage):
    def __init__(self, element):
        self.element = element
        self.evaluate_script = ""
        self.evaluate_arg = None

    async def evaluate_handle(self, script: str, arg):
        self.evaluate_script = script
        self.evaluate_arg = arg
        return FakeHandle(self.element)


class FakeDeadlinePage(FakePage):
    def __init__(self, evaluate_result: str):
        self.evaluate_result = evaluate_result
        self.evaluate_script = ""
        self.evaluate_arg = None

    async def evaluate(self, script: str, arg):
        self.evaluate_script = script
        self.evaluate_arg = arg
        return self.evaluate_result


def _plan_with_customer_remark(remark: str = "0703发说明书") -> TentSkuAdjustmentPlan:
    return TentSkuAdjustmentPlan(
        platform_order_no="111-0000000-0000000",
        system_order_no="103700000000000000",
        destination=DestinationRegion(raw_text="", country="US", state="TX", category="us_mainland"),
        replace_main_sku="Instruction",
        customer_remark=remark,
    )


def _run_remark_upsert(existing_text: str, remark: str = "0703发说明书"):
    button = FakeRemarkButton()
    input_locator = FakeRemarkInput(existing_text)
    calls = {"confirm": 0, "close": 0}

    old_find_button = adjuster._find_customer_remark_edit_button
    old_find_input = adjuster._find_customer_remark_editor_input
    old_confirm = adjuster._confirm_customer_remark_editor
    old_close = adjuster._close_customer_remark_editor

    async def fake_find_button(_page, *, system_order_no, platform_order_no):
        assert system_order_no == "103700000000000000"
        assert platform_order_no == "111-0000000-0000000"
        return button

    async def fake_find_input(_page):
        return object(), input_locator

    async def fake_confirm(_page, _editor):
        calls["confirm"] += 1

    async def fake_close(_page, _editor):
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
    page = FakeDeadlinePage("4天5小时09分钟")
    old_find_row = adjuster._find_order_row

    async def fake_find_row(_page, *, system_order_no, platform_order_no):
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
    assert _merge_instruction_customer_remark("已有备注", "0703发说明书") == (
        "已有备注\n0703发说明书",
        "append",
    )
    assert _merge_instruction_customer_remark("0701发说明书\n已有备注", "0703发说明书") == (
        "0703发说明书\n已有备注",
        "replace",
    )
    assert _merge_instruction_customer_remark("已有备注\n0703发说明书", "0703发说明书") == (
        "已有备注\n0703发说明书",
        "skip",
    )


def test_find_customer_remark_edit_button_uses_dom_header_and_order_identity():
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
    action, input_locator, button, calls = _run_remark_upsert("已有备注")

    assert action == "append"
    assert input_locator.value == "已有备注\n0703发说明书"
    assert input_locator.fill_calls == 1
    assert button.click_count == 1
    assert calls == {"confirm": 1, "close": 0}


def test_upsert_customer_remark_replaces_old_instruction_remark():
    action, input_locator, _button, calls = _run_remark_upsert("0701发说明书\n已有备注")

    assert action == "replace"
    assert input_locator.value == "0703发说明书\n已有备注"
    assert input_locator.fill_calls == 1
    assert calls == {"confirm": 1, "close": 0}


def test_upsert_customer_remark_skips_duplicate_instruction_remark():
    action, input_locator, _button, calls = _run_remark_upsert("已有备注\n0703发说明书")

    assert action == "skip"
    assert input_locator.value == "已有备注\n0703发说明书"
    assert input_locator.fill_calls == 0
    assert calls == {"confirm": 0, "close": 1}


def test_execute_tent_sku_adjustment_stops_before_sku_when_remark_write_fails():
    opened_product_editor = {"called": False}
    page = FakePage()
    page.keyboard = FakeKeyboard()

    old_find_row = adjuster._find_order_row
    old_upsert = adjuster._upsert_customer_remark
    old_open_product = adjuster._open_product_edit_dialog
    old_cancel = adjuster._cancel_visible_dialogs

    async def fake_find_row(_page, *, system_order_no, platform_order_no):
        return object()

    async def fake_upsert(_page, _plan):
        raise RuntimeError("客服备注写入失败")

    async def fake_open_product(_page, _row):
        opened_product_editor["called"] = True
        return object()

    async def fake_cancel(_page):
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
