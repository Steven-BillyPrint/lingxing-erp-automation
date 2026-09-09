from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from shipment_automation.models import ShipmentCandidate
from shipment_automation import queue_read_limiter
from shipment_automation.queue_store import ShipmentWorkflowStore


def test_queue_read_slot_serves_waiters_in_arrival_order(monkeypatch):
    slot = queue_read_limiter._ReadSlot()
    waiting = {name: threading.Event() for name in ("first", "second")}
    original_wait = slot.condition.wait
    order = []

    def observe_wait(*args, **kwargs):
        waiting[threading.current_thread().name].set()
        return original_wait(*args, **kwargs)

    def enter():
        with slot.enter():
            order.append(threading.current_thread().name)

    monkeypatch.setattr(slot.condition, "wait", observe_wait)
    threads = []
    with slot.enter():
        for name in waiting:
            thread = threading.Thread(target=enter, name=name)
            threads.append(thread)
            thread.start()
            assert waiting[name].wait(2)
        assert not order
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert order == ["first", "second"]


def test_queue_read_slot_recovers_after_wait_and_query_exceptions(monkeypatch):
    slot = queue_read_limiter._ReadSlot()

    def failed_wait(*_args, **_kwargs):
        raise RuntimeError("interrupted while queued")

    def enter():
        with slot.enter():
            return "entered"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with slot.enter():
            with monkeypatch.context() as patch:
                patch.setattr(slot.condition, "wait", failed_wait)
                future = pool.submit(enter)
                with pytest.raises(RuntimeError, match="interrupted while queued"):
                    future.result(timeout=2)
        with pytest.raises(ValueError, match="query failed"):
            with slot.enter():
                raise ValueError("query failed")
        assert pool.submit(enter).result(timeout=2) == "entered"
    assert not slot.waiters


def test_waiting_page_has_no_read_connection_and_does_not_block_writes_or_other_databases(tmp_path, monkeypatch):
    writer = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    writer.upsert_candidate(ShipmentCandidate(
        system_order_no="system", platform_order_no="platform", logistics_no="ALS",
        shipment_tag_name="synthetic",
    ))
    reader = ShipmentWorkflowStore(writer.path, read_only=True)
    reader.initialize()
    other = ShipmentWorkflowStore(tmp_path / "other.sqlite3")
    other.initialize()
    waiting, connected = threading.Event(), threading.Event()
    original_connect = reader.connect

    def connect():
        connected.set()
        return original_connect()

    monkeypatch.setattr(reader, "connect", connect)
    key = os.path.normcase(str(writer.path.resolve()))
    # The alias resolves to the same database admission slot.
    alias = tmp_path / "nested" / ".." / "queue.sqlite3"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with queue_read_limiter.queue_read_slot(alias):
            slot = queue_read_limiter._slots[key]
            original_wait = slot.condition.wait

            def observe_wait(*args, **kwargs):
                waiting.set()
                return original_wait(*args, **kwargs)

            monkeypatch.setattr(slot.condition, "wait", observe_wait)
            future = pool.submit(reader.list_queue_page)
            assert waiting.wait(2)
            assert not connected.is_set()
            with writer.connect() as connection:
                connection.execute("UPDATE shipment_jobs SET sku_text='updated while queued'")
            assert other.list_queue_page()["total"] == 0
        result = future.result(timeout=3)
    assert connected.is_set()
    assert result["items"][0]["sku_text"] == "updated while queued"
    del slot
    assert key not in queue_read_limiter._slots
