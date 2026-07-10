import argparse
import asyncio

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

