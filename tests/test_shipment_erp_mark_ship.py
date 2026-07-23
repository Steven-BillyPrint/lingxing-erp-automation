import asyncio
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from shipment_automation import erp_mark_ship as mark_module
from shipment_automation.erp_mark_ship import (
    ErpMarkEmergencyStopped,
    ErpMarkManualReview,
    ErpMarkUserAbort,
    channel_payload,
    clean_money_amount,
    erp_channel_path_for_carrier,
    erp_payload_hash,
    execute_erp_mark_item,
    format_chargeable_weight_g,
    logistics_form_payload,
    process_erp_mark_items_once,
)
from shipment_automation.models import (
    ERP_BLOCKED,
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_OUTBOUNDED,
    ERP_DONE,
    ERP_PENDING,
    ERP_RETRYABLE,
    ERP_WAITING,
    LOGISTICS_BLOCKED,
    LOGISTICS_READY,
    LogisticsDetail,
    ReadyToMarkItem,
    SALES_CHANNEL_INDEPENDENT_SITE,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentWorkflowStore


def _ready_item(logistics_no: str = "ALS01781406025", **overrides) -> ReadyToMarkItem:
    values = {
        "system_order_no": "103710434633847501",
        "platform_order_no": "112-1165824-9982644",
        "logistics_no": logistics_no,
        "carrier": "UPS",
        "service_line": "UPS-Saver",
        "international_tracking_no": "1Z9253126709651051",
        "actual_total": "CNY 123.45",
        "chargeable_weight_kg": "4.500",
    }
    values.update(overrides)
    return ReadyToMarkItem(**values)


def _candidate(logistics_no: str = "ALS01781406025", platform_order_no: str = "112-1165824-9982644") -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no="103710434633847501",
        platform_order_no=platform_order_no,
        logistics_no=logistics_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {logistics_no}",
        status_text="待审核发货",
        receiver_email="buyer@example.com",
    )


def _make_ready(store: ShipmentWorkflowStore, logistics_no: str = "ALS01781406025") -> None:
    store.complete_logistics_attempt(
        logistics_no,
        LogisticsDetail(
            logistics_no=logistics_no,
            status_text="运输中",
            service_line="UPS-Saver",
            carrier="UPS",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )


def test_erp_channel_path_maps_carriers():
    assert erp_channel_path_for_carrier("UPS", "无忧 UPS-Saver") == [
        "手动-Alibaba logistics",
        "UPS-全程",
    ]
    assert erp_channel_path_for_carrier("FEDEX", "普通专线") == [
        "手动",
        "Fedex-专线尾程",
    ]
    assert erp_channel_path_for_carrier("DHL", "普通专线") == [
        "手动-Alibaba logistics",
        "DHL-全程",
    ]
    assert erp_channel_path_for_carrier("Yanwen") == ["手动", "燕文"]
    assert erp_channel_path_for_carrier("SpeedX") == ["手动", "SpeedX（不得标发亚马逊）"]
    assert erp_channel_path_for_carrier("1ST") == ["手动", "一代国际物流（不得标发亚马逊）"]
    with pytest.raises(ErpMarkManualReview, match="缺少服务线路"):
        erp_channel_path_for_carrier("UPS")


def test_unknown_erp_channel_path_needs_manual_review():
    with pytest.raises(ErpMarkManualReview):
        erp_channel_path_for_carrier("YHA")


def test_money_weight_and_payload_hashes():
    item = _ready_item()
    assert clean_money_amount("CNY 123.45") == "123.45"
    assert clean_money_amount("￥1,234.50") == "1234.50"
    assert format_chargeable_weight_g("4.500") == "4500"
    assert logistics_form_payload(item)["跟踪号"] == item.logistics_no
    assert erp_payload_hash(channel_payload(item)) == erp_payload_hash(channel_payload(item))
    changed = _ready_item(actual_total="CNY 124.45")
    assert erp_payload_hash(logistics_form_payload(item)) != erp_payload_hash(logistics_form_payload(changed))


def test_process_erp_mark_dry_run_does_not_update_stage(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.list_erp_mark_candidates()[0]

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=None,
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=True,
            confirm_func=lambda _prompt: None,
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert report.results[0].erp_step == "DRY_RUN"
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["erp_checkpoint"] != ERP_CHECKPOINT_OUTBOUNDED


def test_invalid_tracking_is_blocked_before_any_erp_page_action(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    store.complete_logistics_attempt(
        "ALS01781406025",
        LogisticsDetail(
            logistics_no="ALS01781406025",
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="JYCP00000093286",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    item = store.claimed_erp_items("worker-1")[0]
    called = False

    async def mark_item(*_args):
        nonlocal called
        called = True
        return "OUTBOUNDED"

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=lambda _prompt: None,
            mark_item_func=mark_item,
            worker_id="worker-1",
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert called is False
    assert report.tracking_blocked_count == 1
    assert report.results[0].erp_step == "TRACKING_BLOCKED"
    assert row["logistics_state"] == LOGISTICS_BLOCKED
    assert row["erp_state"] == ERP_WAITING


def test_process_erp_mark_execute_checkpoints_outbound_and_creates_email_batch(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]
    calls = []

    async def fake_mark_item(page, ready_item, confirm_func):
        calls.append((page, ready_item.logistics_no))
        return ERP_CHECKPOINT_OUTBOUNDED

    async def fake_confirm(_prompt):
        return True

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
            email_preview_enabled=True,
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert calls == [(ANY, item.logistics_no)]
    assert report.done_count == 1
    assert row["erp_state"] == ERP_DONE
    assert row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED
    assert store.list_email_batches()[0].logistics_numbers == [item.logistics_no]


def test_process_erp_mark_persists_explicit_wms_outbound_selection(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]
    candidates = [
        {
            "wo_number": "WO-A",
            "order_number": item.system_order_no,
            "platform_order_no": [item.platform_order_no],
            "status": 1,
        },
        {
            "wo_number": "WO-B",
            "order_number": item.system_order_no,
            "platform_order_no": [item.platform_order_no],
            "status": 1,
        },
    ]

    async def fake_confirm(_prompt):
        return True

    async def choose(ready_item, choices):
        assert ready_item.logistics_no == item.logistics_no
        assert choices == candidates
        return "WO-B"

    fake_confirm.select_wms_row = choose

    async def fake_mark_item(_page, _item, confirm):
        assert await confirm.select_wms_row(candidates) == "WO-B"
        return ERP_CHECKPOINT_OUTBOUNDED

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert report.done_count == 1
    assert row["selected_wms_wo_number"] == "WO-B"
    assert row["selected_wms_candidates_hash"]
    assert row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED


def test_cancelled_wms_selection_retains_structured_requirement(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]
    candidates = [
        {
            "wo_number": "WO-A",
            "order_number": item.system_order_no,
            "platform_order_no": [item.platform_order_no],
        },
        {
            "wo_number": "WO-B",
            "order_number": item.system_order_no,
            "platform_order_no": [item.platform_order_no],
        },
    ]

    async def fake_confirm(_prompt):
        return True

    async def cancel_selection(_ready_item, _choices):
        return ""

    fake_confirm.select_wms_row = cancel_selection

    async def fake_mark_item(_page, _item, confirm):
        await confirm.select_wms_row(candidates)
        raise AssertionError("cancelled selection must stop before ERP writes")

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert report.skipped_count == 1
    assert row["selected_wms_wo_number"] is None
    assert row["wms_selection_required"] == 1


def test_worker_injected_mark_function_only_claims_selected_item_and_checkpoints(tmp_path):
    queue_path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(queue_path)
    first = _candidate(logistics_no="ALS-FIRST", platform_order_no="ORDER-FIRST")
    second = _candidate(logistics_no="ALS-SECOND", platform_order_no="ORDER-SECOND")
    second.system_order_no = "103710434633847502"
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    _make_ready(store, second.logistics_no)
    calls = []

    async def api_mark_item(page, ready_item, _confirm_func):
        calls.append((page, ready_item.logistics_no))
        return "API_OUTBOUNDED"

    payload = asyncio.run(
        mark_module.run_erp_mark_worker(
            SimpleNamespace(
                queue_path=str(queue_path),
                limit=1,
                dry_run=False,
                logistics_no=second.logistics_no,
                mark_item_func=api_mark_item,
            )
        )
    )

    assert calls == [(None, second.logistics_no)]
    assert payload["done_count"] == 1
    assert payload["results"][0]["erp_step"] == "API_OUTBOUNDED"
    first_row = store.get_by_logistics_no(first.logistics_no)
    second_row = store.get_by_logistics_no(second.logistics_no)
    assert first_row["lease_owner"] is None
    assert first_row["erp_state"] == ERP_PENDING
    assert second_row["erp_state"] == ERP_DONE
    assert second_row["erp_checkpoint"] == ERP_CHECKPOINT_OUTBOUNDED


def test_worker_selected_unavailable_item_does_not_claim_another_item(tmp_path):
    queue_path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(queue_path)
    first = _candidate(logistics_no="ALS-FIRST", platform_order_no="ORDER-FIRST")
    second = _candidate(logistics_no="ALS-SECOND", platform_order_no="ORDER-SECOND")
    second.system_order_no = "103710434633847502"
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    store.complete_logistics_attempt(
        second.logistics_no,
        LogisticsDetail(
            logistics_no=second.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="JYCP00000093286",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_READY,
        last_error=None,
    )
    assert [
        item.logistics_no
        for item in store.claimed_erp_items("busy-worker", logistics_no=second.logistics_no)
    ] == [second.logistics_no]

    async def api_mark_item(_page, _ready_item, _confirm_func):
        raise AssertionError("不应执行任何标发任务")

    payload = asyncio.run(
        mark_module.run_erp_mark_worker(
            SimpleNamespace(
                queue_path=str(queue_path),
                limit=1,
                dry_run=False,
                logistics_no=second.logistics_no,
                mark_item_func=api_mark_item,
            )
        )
    )

    assert "当前不可执行" in payload["message"]
    assert store.get_by_logistics_no(first.logistics_no)["lease_owner"] is None
    second_row = store.get_by_logistics_no(second.logistics_no)
    assert second_row["lease_owner"] == "busy-worker"
    assert second_row["logistics_state"] == LOGISTICS_READY


def test_worker_selected_missing_item_reports_without_claiming_queue(tmp_path):
    queue_path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentWorkflowStore(queue_path)
    store.upsert_candidate(_candidate(logistics_no="ALS-OTHER"))
    _make_ready(store, "ALS-OTHER")

    payload = asyncio.run(
        mark_module.run_erp_mark_worker(
            SimpleNamespace(
                queue_path=str(queue_path),
                limit=1,
                dry_run=True,
                logistics_no="ALS-MISSING",
            )
        )
    )

    assert "ALS-MISSING 不存在" in payload["message"]
    assert store.get_by_logistics_no("ALS-OTHER")["lease_owner"] is None


def test_user_skip_keeps_order_pending_and_continues_batch(tmp_path, monkeypatch):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    first = _candidate(logistics_no="ALS-FIRST", platform_order_no="ORDER-FIRST")
    second = _candidate(logistics_no="ALS-SECOND", platform_order_no="ORDER-SECOND")
    second.system_order_no = "103710434633847502"
    store.upsert_candidate(first)
    store.upsert_candidate(second)
    _make_ready(store, first.logistics_no)
    _make_ready(store, second.logistics_no)
    items = store.claimed_erp_items("worker-1")
    confirmations = iter([False, True])
    cleaned_pages = []

    async def fake_confirm(_prompt):
        return next(confirmations)

    async def fake_mark_item(_page, item, confirm_func):
        if not await confirm_func(f"confirm {item.platform_order_no}"):
            raise ErpMarkUserAbort(f"skip {item.platform_order_no}")
        return ERP_CHECKPOINT_OUTBOUNDED

    async def fake_cleanup(page):
        cleaned_pages.append(page)

    monkeypatch.setattr(mark_module, "_reset_erp_page_after_user_skip", fake_cleanup)
    page = object()
    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            items,
            page=page,
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    assert report.status == "completed_with_skips"
    assert report.total_count == 2
    assert report.done_count == 1
    assert report.skipped_count == 1
    assert [result.erp_step for result in report.results] == ["USER_SKIPPED", ERP_CHECKPOINT_OUTBOUNDED]
    assert store.get_by_logistics_no(first.logistics_no)["erp_state"] == ERP_PENDING
    assert store.get_by_logistics_no(second.logistics_no)["erp_state"] == ERP_DONE
    assert cleaned_pages == [page]


def test_emergency_stop_keeps_current_checkpoint_pending_and_continues_no_writes(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]

    async def emergency_mark(_page, _item, _confirm):
        raise ErpMarkEmergencyStopped("用户已紧急停止后续写入")

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=None,
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=lambda _prompt: True,
            mark_item_func=emergency_mark,
            worker_id="worker-1",
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert report.status == "cancelled"
    assert report.paused_count == 1
    assert report.results[0].erp_step == "EMERGENCY_STOPPED"
    assert row["erp_state"] == ERP_PENDING
    assert row["erp_checkpoint"] == "NONE"
    assert row["lease_owner"] is None
    assert row["erp_state"] != ERP_BLOCKED


def test_user_skip_cleanup_only_closes_and_returns_to_pending_tab(monkeypatch):
    calls = []

    class FakePage:
        async def evaluate(self, script):
            calls.append(("evaluate", script))
            return True

        async def wait_for_timeout(self, milliseconds):
            calls.append(("wait", milliseconds))

    async def fake_close(_page):
        calls.append(("close_detail",))

    async def fake_switch(_page, tab_text):
        calls.append(("switch", tab_text))

    monkeypatch.setattr(mark_module, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(mark_module, "switch_order_tab", fake_switch)

    asyncio.run(mark_module._reset_erp_page_after_user_skip(FakePage()))

    script = calls[0][1]
    assert "取消" in script
    assert "确定" not in script
    assert calls[1:] == [("wait", 500), ("close_detail",), ("switch", "待审核")]


def test_process_erp_mark_independent_site_creates_store_fulfillment_reminder(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate(platform_order_no="wc39877"))
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]
    assert item.sales_channel == SALES_CHANNEL_INDEPENDENT_SITE

    async def fake_mark_item(page, ready_item, confirm_func):
        return ERP_CHECKPOINT_OUTBOUNDED

    async def fake_confirm(_prompt):
        return True

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    assert report.done_count == 1
    assert store.list_email_batches(platform_order_no="wc39877") == []
    assert len(report.store_fulfillment_reminders) == 1
    reminder = report.store_fulfillment_reminders[0]
    assert reminder.independent_order_no == "wc39877"
    assert reminder.carrier == "UPS"
    assert reminder.international_tracking_no == "1Z9253126709651051"
    assert "店小秘" in reminder.message


def test_execute_erp_mark_records_each_checkpoint_and_dismisses_success_dialogs(monkeypatch):
    calls = []
    checkpoints = []
    approvals = []

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

    async def record(name, *values):
        calls.append((name, *values))

    async def fake_confirm(_prompt):
        return True

    async def checkpoint_func(checkpoint, values):
        checkpoints.append((checkpoint, values))

    async def approval_func(kind, payload_hash):
        approvals.append((kind, payload_hash))

    monkeypatch.setattr(mark_module, "switch_order_tab", fake_switch_order_tab)
    monkeypatch.setattr(mark_module, "search_platform_order", fake_search_platform_order)
    monkeypatch.setattr(mark_module, "wait_for_order_row", fake_wait_for_order_row)
    monkeypatch.setattr(mark_module, "select_order_row", lambda page, rowid: record("select", rowid))
    monkeypatch.setattr(mark_module, "open_row_operation_menu", lambda page, rowid: record("row_menu", rowid))
    monkeypatch.setattr(mark_module, "click_visible_menu_item", lambda page, text: record("menu_item", text))
    monkeypatch.setattr(mark_module, "wait_for_dialog", lambda page, text: record("wait_dialog", text))
    monkeypatch.setattr(
        mark_module,
        "select_cascader_path",
        lambda page, dialog, label, path: record("cascader", dialog, label, tuple(path)),
    )
    monkeypatch.setattr(
        mark_module,
        "ensure_dialog_warehouse",
        lambda page, dialog: record("warehouse", dialog),
    )
    monkeypatch.setattr(mark_module, "click_dialog_button", lambda page, dialog, text: record("dialog_button", dialog, text))
    monkeypatch.setattr(mark_module, "click_toolbar_button", lambda page, text: record("toolbar", text))
    monkeypatch.setattr(mark_module, "fill_dialog_form", lambda page, dialog, values: record("fill_form", dialog, values))
    monkeypatch.setattr(mark_module, "dismiss_result_dialog", lambda page: record("dismiss_result_dialog"))
    monkeypatch.setattr(
        mark_module,
        "dismiss_outbound_success_dialog",
        lambda page: record("dismiss_outbound_success_dialog"),
    )

    final_step = asyncio.run(
        execute_erp_mark_item(
            FakePage(),
            _ready_item(),
            fake_confirm,
            checkpoint_func=checkpoint_func,
            approval_func=approval_func,
        )
    )

    assert final_step == ERP_CHECKPOINT_OUTBOUNDED
    assert [checkpoint for checkpoint, _ in checkpoints] == [
        ERP_CHECKPOINT_CHANNEL_SET,
        ERP_CHECKPOINT_AUDITED,
        ERP_CHECKPOINT_LOGISTICS_SAVED,
        ERP_CHECKPOINT_OUTBOUNDED,
    ]
    assert [kind for kind, _ in approvals] == ["channel", "logistics"]
    assert calls.count(("dismiss_result_dialog",)) == 1
    assert calls.count(("dismiss_outbound_success_dialog",)) == 1
    assert calls.count(("warehouse", "设定仓库物流")) == 1
    cascader_call = (
        "cascader",
        "设定仓库物流",
        "物流渠道",
        tuple(
            erp_channel_path_for_carrier(
                _ready_item().carrier,
                _ready_item().service_line,
            )
        ),
    )
    assert calls.index(("warehouse", "设定仓库物流")) < calls.index(cascader_call)
    assert calls.index(cascader_call) < calls.index(("dialog_button", "设定仓库物流", "确定"))
    assert ("fill_form", "编辑运单号", logistics_form_payload(_ready_item())) in calls


def test_resume_from_audited_checkpoint_skips_channel_and_audit(monkeypatch):
    calls = []
    item = _ready_item(
        erp_checkpoint=ERP_CHECKPOINT_AUDITED,
        service_line=None,
    )

    class FakePage:
        async def wait_for_timeout(self, _milliseconds):
            return None

    async def record(name, *values):
        calls.append((name, *values))

    async def fake_wait_for_order_row(_page, **_kwargs):
        return {"rowid": item.system_order_no}

    async def fake_confirm(_prompt):
        return True

    monkeypatch.setattr(mark_module, "switch_order_tab", lambda page, text: record("switch", text))
    monkeypatch.setattr(mark_module, "search_platform_order", lambda page, text: record("search", text))
    monkeypatch.setattr(mark_module, "wait_for_order_row", fake_wait_for_order_row)
    monkeypatch.setattr(mark_module, "select_order_row", lambda page, text: record("select", text))
    monkeypatch.setattr(mark_module, "open_row_operation_menu", lambda page, text: record("row_menu", text))
    monkeypatch.setattr(mark_module, "click_visible_menu_item", lambda page, text: record("menu", text))
    monkeypatch.setattr(mark_module, "wait_for_dialog", lambda page, text: record("dialog", text))
    monkeypatch.setattr(mark_module, "fill_dialog_form", lambda page, dialog, values: record("fill", dialog))
    monkeypatch.setattr(mark_module, "click_dialog_button", lambda page, dialog, text: record("dialog_button", dialog, text))
    monkeypatch.setattr(mark_module, "click_toolbar_button", lambda page, text: record("toolbar", text))
    monkeypatch.setattr(mark_module, "dismiss_result_dialog", lambda page: record("dismiss"))
    monkeypatch.setattr(
        mark_module,
        "dismiss_outbound_success_dialog",
        lambda page: record("dismiss_outbound"),
    )

    asyncio.run(execute_erp_mark_item(FakePage(), item, fake_confirm))

    assert ("switch", "待审核") not in calls[:-1]
    assert ("toolbar", "审核") not in calls
    assert ("switch", "物流下单") in calls
    assert ("switch", "待打单") in calls


def test_erp_failure_preserves_logistics_and_checkpoint_for_retry(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]

    async def fake_mark_item(_page, _item, _confirm):
        raise RuntimeError("browser closed")

    async def fake_confirm(_prompt):
        return True

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    row = store.get_by_logistics_no(item.logistics_no)
    assert report.retryable_count == 1
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["erp_state"] == ERP_RETRYABLE


def test_erp_page_mismatch_becomes_blocked(tmp_path):
    store = ShipmentWorkflowStore(tmp_path / "shipment_queue.sqlite3")
    store.upsert_candidate(_candidate())
    _make_ready(store)
    item = store.claimed_erp_items("worker-1")[0]

    async def fake_mark_item(_page, _item, _confirm):
        raise ErpMarkManualReview("ERP 页面状态与检查点不一致")

    async def fake_confirm(_prompt):
        return True

    report = asyncio.run(
        process_erp_mark_items_once(
            store,
            [item],
            page=object(),
            queue_path=str(tmp_path / "shipment_queue.sqlite3"),
            dry_run=False,
            confirm_func=fake_confirm,
            mark_item_func=fake_mark_item,
            worker_id="worker-1",
        )
    )

    assert report.blocked_count == 1
    assert store.get_by_logistics_no(item.logistics_no)["erp_state"] == ERP_BLOCKED
