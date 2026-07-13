import argparse
import asyncio

from lingxing_automation.pages.order_list import ORDER_TABLE_PROBE_JS
from shipment_automation import lingxing_source


def test_empty_shipment_tag_returns_before_launching_browser(monkeypatch):
    async def fail_launch_context(_args):
        raise AssertionError("empty shipment tag must not launch browser")

    monkeypatch.setattr(lingxing_source, "SHIPMENT_TAG_NAME", "")
    monkeypatch.setattr(lingxing_source, "launch_context", fail_launch_context)
    args = argparse.Namespace(
        shipment_tag=None,
        queue_path="data/shipment_queue.sqlite3",
        dry_run=True,
    )

    payload = asyncio.run(lingxing_source.run_shipment_scan(args))

    assert payload["status"] == "config_missing"
    assert payload["message"] == "未配置专属发货标签。"


def test_order_table_probe_supports_independent_site_wc_platform_orders():
    assert "wc\\d+" in ORDER_TABLE_PROBE_JS


def test_complete_pending_snapshot_requires_unlimited_stable_total():
    assert lingxing_source.is_complete_pending_snapshot(
        limit=0,
        row_count=126,
        total_before=126,
        total_after=126,
    ) is True
    assert lingxing_source.is_complete_pending_snapshot(
        limit=20,
        row_count=20,
        total_before=126,
        total_after=126,
    ) is False
    assert lingxing_source.is_complete_pending_snapshot(
        limit=0,
        row_count=125,
        total_before=126,
        total_after=126,
    ) is False
    assert lingxing_source.is_complete_pending_snapshot(
        limit=0,
        row_count=126,
        total_before=126,
        total_after=127,
    ) is False
    assert lingxing_source.is_complete_pending_snapshot(
        limit=0,
        row_count=126,
        total_before=126,
        total_after=126,
        incomplete_field_count=1,
    ) is False


def _complete_row(rowid: str, platform_order_no: str = "111-1111111-1111111"):
    return {
        "rowid": rowid,
        "system_order_no": rowid,
        "platform_order_no": platform_order_no,
        "tag_text": "",
        "customer_remark": "",
        "field_presence": {
            "system": True,
            "platform": True,
            "tag": True,
            "customer_remark": True,
        },
    }


def test_merge_collected_rows_keeps_rowid_and_enriches_missing_platform():
    rows = {}
    first = _complete_row("103700000000000001", platform_order_no="")
    first["field_presence"]["platform"] = False

    assert lingxing_source.merge_collected_order_rows(rows, [first]) == (1, 0)
    assert len(rows) == 1

    second = _complete_row("103700000000000001", platform_order_no="111-2222222-3333333")
    inserted, enriched = lingxing_source.merge_collected_order_rows(rows, [second])

    assert inserted == 0
    assert enriched == 1
    merged = next(iter(rows.values()))
    assert merged["rowid"] == "103700000000000001"
    assert merged["platform_order_no"] == "111-2222222-3333333"
    assert lingxing_source.missing_required_row_fields(merged) == []


def test_recovery_scroll_positions_cover_both_ends_and_reverse():
    state = {"maxScrollTop": 5678, "clientHeight": 658}
    forward = lingxing_source.build_recovery_scroll_positions(state)
    reverse = lingxing_source.build_recovery_scroll_positions(state, reverse=True)

    assert forward[0] == 0
    assert forward[-1] == 5678
    assert reverse == list(reversed(forward))
    assert max(right - left for left, right in zip(forward, forward[1:])) <= 198


def test_collect_rows_uses_forward_and_reverse_recovery(monkeypatch):
    class FakePage:
        async def wait_for_timeout(self, _milliseconds):
            return None

    totals = iter([4, 4])
    scroll_calls = 0
    recovery_directions = []

    async def noop(*_args, **_kwargs):
        return None

    async def fake_total(_page):
        return next(totals)

    async def fake_reset(_page):
        return {"ok": True, "scrollTop": 0, "maxScrollTop": 200, "clientHeight": 200}

    async def fake_collect(_page, _page_number, _scroll_top):
        return [_complete_row("103700000000000001"), _complete_row("103700000000000002")]

    async def fake_scroll(_page):
        nonlocal scroll_calls
        scroll_calls += 1
        return {"ok": True, "changed": False, "end": True, "scrollTop": 200}

    async def fake_next(_page):
        return False

    async def fake_recover(_page, *, rows_by_key, reverse, **_kwargs):
        recovery_directions.append(reverse)
        rowid = "103700000000000004" if reverse else "103700000000000003"
        lingxing_source.merge_collected_order_rows(rows_by_key, [_complete_row(rowid)])

    monkeypatch.setattr(lingxing_source, "ensure_page_size_1000", noop)
    monkeypatch.setattr(lingxing_source, "ensure_order_table_columns_visible", noop)
    monkeypatch.setattr(lingxing_source, "wait_for_visible_batch_order_rows", noop)
    monkeypatch.setattr(lingxing_source, "read_order_table_total_count", fake_total)
    monkeypatch.setattr(lingxing_source, "reset_order_table_vertical_scroll", fake_reset)
    monkeypatch.setattr(lingxing_source, "collect_visible_batch_order_rows", fake_collect)
    monkeypatch.setattr(lingxing_source, "scroll_order_table_down", fake_scroll)
    monkeypatch.setattr(lingxing_source, "click_next_batch_page", fake_next)
    monkeypatch.setattr(lingxing_source, "_recover_current_page", fake_recover)
    debug = {}

    rows = asyncio.run(lingxing_source.collect_lingxing_shipment_rows(FakePage(), debug=debug))

    assert len(rows) == 4
    assert recovery_directions == [False, True]
    assert scroll_calls == 1
    assert debug["scan_complete"] is True


def test_collect_rows_remains_incomplete_when_recovery_cannot_fill_gap(monkeypatch):
    class FakePage:
        async def wait_for_timeout(self, _milliseconds):
            return None

    totals = iter([3, 3])

    async def noop(*_args, **_kwargs):
        return None

    async def fake_total(_page):
        return next(totals)

    async def fake_reset(_page):
        return {"ok": True, "scrollTop": 0, "maxScrollTop": 0, "clientHeight": 200}

    async def fake_collect(_page, _page_number, _scroll_top):
        return [_complete_row("103700000000000001"), _complete_row("103700000000000002")]

    async def fake_scroll(_page):
        return {"ok": True, "changed": False, "end": True, "scrollTop": 0}

    async def fake_next(_page):
        return False

    async def fake_recover(*_args, **_kwargs):
        return None

    monkeypatch.setattr(lingxing_source, "ensure_page_size_1000", noop)
    monkeypatch.setattr(lingxing_source, "ensure_order_table_columns_visible", noop)
    monkeypatch.setattr(lingxing_source, "wait_for_visible_batch_order_rows", noop)
    monkeypatch.setattr(lingxing_source, "read_order_table_total_count", fake_total)
    monkeypatch.setattr(lingxing_source, "reset_order_table_vertical_scroll", fake_reset)
    monkeypatch.setattr(lingxing_source, "collect_visible_batch_order_rows", fake_collect)
    monkeypatch.setattr(lingxing_source, "scroll_order_table_down", fake_scroll)
    monkeypatch.setattr(lingxing_source, "click_next_batch_page", fake_next)
    monkeypatch.setattr(lingxing_source, "_recover_current_page", fake_recover)
    debug = {}

    rows = asyncio.run(lingxing_source.collect_lingxing_shipment_rows(FakePage(), debug=debug))

    assert len(rows) == 2
    assert debug["scan_complete"] is False

