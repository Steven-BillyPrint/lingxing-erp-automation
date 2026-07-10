import asyncio

import pytest

from shipment_automation import erp_mark_ship as mark_module
from shipment_automation.erp_mark_ship import (
    ErpMarkManualReview,
    clean_money_amount,
    erp_channel_path_for_carrier,
    execute_erp_mark_item,
    format_chargeable_weight_g,
    process_erp_mark_items_once,
)
from shipment_automation.models import QUEUE_STATUS_ERP_MARKED, QUEUE_STATUS_READY_TO_MARK, ReadyToMarkItem
from shipment_automation.models import ShipmentCandidate
from shipment_automation.queue_store import ShipmentQueueStore


def _ready_item(als_no: str = "ALS01781406025") -> ReadyToMarkItem:
    return ReadyToMarkItem(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        als_no=als_no,
        logistics_order_no="ALS01781406025",
        carrier="UPS",
        international_tracking_no="1Z999",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )


def _candidate(als_no: str = "ALS01781406025") -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no="112-1165824-9982644",
        als_no=als_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {als_no}",
        status_text="待审核发货",
    )


def test_erp_channel_path_maps_carriers():
    assert erp_channel_path_for_carrier("UPS") == ["手动-Alibaba logistics", "UPS-阿里巴巴"]
    assert erp_channel_path_for_carrier("FEDEX") == ["手动-Alibaba logistics", "Fedex-阿里巴巴"]
    assert erp_channel_path_for_carrier("DHL") == ["手动-Alibaba logistics", "DHL-阿里巴巴"]
    assert erp_channel_path_for_carrier("Yanwen") == ["手动", "燕文"]
    assert erp_channel_path_for_carrier("SpeedX") == ["手动", "SpeedX（不得标发亚马逊）"]
    assert erp_channel_path_for_carrier("1ST") == ["手动", "一代国际物流（不得标发亚马逊）"]


def test_unknown_erp_channel_path_needs_manual_review():
    with pytest.raises(ErpMarkManualReview):
        erp_channel_path_for_carrier("YHA")


def test_clean_money_amount():
    assert clean_money_amount("CNY 123.45") == "123.45"
    assert clean_money_amount("￥1,234.50") == "1234.50"


def test_format_chargeable_weight_g():
    assert format_chargeable_weight_g("4.500") == "4500"
    assert format_chargeable_weight_g("0.755 KG") == "755"


def test_process_erp_mark_dry_run_does_not_update_queue(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.queue_status = QUEUE_STATUS_READY_TO_MARK
    store.insert_candidate(candidate)

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [_ready_item()],
            page=None,
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=True,
            confirm_func=lambda _prompt: None,
        )
    )

    row = store.get_by_als("ALS01781406025")
    assert report.status == "completed"
    assert report.results[0].erp_step == "DRY_RUN"
    assert row["queue_status"] == QUEUE_STATUS_READY_TO_MARK


def test_process_erp_mark_execute_updates_queue(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate()
    candidate.queue_status = QUEUE_STATUS_READY_TO_MARK
    store.insert_candidate(candidate)
    calls = []

    async def fake_mark_item(page, item, confirm_func):
        calls.append((page, item.als_no))
        return "ERP_MARKED"

    async def fake_confirm(_prompt):
        return True

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [_ready_item()],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
        )
    )

    row = store.get_by_als("ALS01781406025")
    assert len(calls) == 1
    assert calls[0][1] == "ALS01781406025"
    assert report.status == "completed"
    assert report.marked_count == 1
    assert report.results[0].queue_status == QUEUE_STATUS_ERP_MARKED
    assert row["queue_status"] == QUEUE_STATUS_ERP_MARKED
    assert row["processed_at"]


def test_execute_erp_mark_dismisses_post_action_dialogs(monkeypatch):
    calls = []

    class FakePage:
        async def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    async def fake_switch_order_tab(_page, tab_text):
        calls.append(("switch", tab_text))

    async def fake_search_platform_order(_page, platform_order_no):
        calls.append(("search", platform_order_no))

    async def fake_wait_for_order_row(_page, *, system_order_no, platform_order_no, timeout_sec):
        calls.append(("wait_row", system_order_no, platform_order_no, timeout_sec))
        return {"rowid": system_order_no}

    async def fake_select_order_row(_page, rowid):
        calls.append(("select", rowid))

    async def fake_open_row_operation_menu(_page, rowid):
        calls.append(("row_menu", rowid))

    async def fake_click_visible_menu_item(_page, item_text):
        calls.append(("menu_item", item_text))

    async def fake_wait_for_dialog(_page, dialog_text):
        calls.append(("wait_dialog", dialog_text))

    async def fake_select_cascader_path(_page, dialog_text, form_label, path):
        calls.append(("cascader", dialog_text, form_label, tuple(path)))

    async def fake_click_dialog_button(_page, dialog_text, button_text):
        calls.append(("dialog_button", dialog_text, button_text))

    async def fake_click_toolbar_button(_page, button_text):
        calls.append(("toolbar", button_text))

    async def fake_fill_dialog_form(_page, dialog_text, values_by_label):
        calls.append(("fill_form", dialog_text, values_by_label))

    async def fake_dismiss_result_dialog(_page):
        calls.append(("dismiss_result_dialog",))
        return True

    async def fake_confirm(_prompt):
        return True

    monkeypatch.setattr(mark_module, "switch_order_tab", fake_switch_order_tab)
    monkeypatch.setattr(mark_module, "search_platform_order", fake_search_platform_order)
    monkeypatch.setattr(mark_module, "wait_for_order_row", fake_wait_for_order_row)
    monkeypatch.setattr(mark_module, "select_order_row", fake_select_order_row)
    monkeypatch.setattr(mark_module, "open_row_operation_menu", fake_open_row_operation_menu)
    monkeypatch.setattr(mark_module, "click_visible_menu_item", fake_click_visible_menu_item)
    monkeypatch.setattr(mark_module, "wait_for_dialog", fake_wait_for_dialog)
    monkeypatch.setattr(mark_module, "select_cascader_path", fake_select_cascader_path)
    monkeypatch.setattr(mark_module, "click_dialog_button", fake_click_dialog_button)
    monkeypatch.setattr(mark_module, "click_toolbar_button", fake_click_toolbar_button)
    monkeypatch.setattr(mark_module, "fill_dialog_form", fake_fill_dialog_form)
    monkeypatch.setattr(mark_module, "dismiss_result_dialog", fake_dismiss_result_dialog)

    final_step = asyncio.run(execute_erp_mark_item(FakePage(), _ready_item(), fake_confirm))

    assert final_step == "ERP_MARKED"
    dismiss_indexes = [index for index, call in enumerate(calls) if call == ("dismiss_result_dialog",)]
    assert len(dismiss_indexes) == 2
    audit_index = calls.index(("dialog_button", "确认审核发货", "审核"))
    logistics_confirm_index = calls.index(("dialog_button", "编辑运单号", "确认"))
    outbound_confirm_index = calls.index(("dialog_button", "发货", "确定"))
    switch_logistics_index = calls.index(("switch", "物流下单"))
    switch_print_index = calls.index(("switch", "待打单"))
    final_switch_review_index = len(calls) - 1 - list(reversed(calls)).index(("switch", "待审核"))

    assert audit_index < dismiss_indexes[0] < switch_logistics_index
    assert logistics_confirm_index < switch_print_index
    assert outbound_confirm_index < dismiss_indexes[1] < final_switch_review_index
