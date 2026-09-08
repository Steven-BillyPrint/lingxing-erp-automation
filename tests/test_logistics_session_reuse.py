from __future__ import annotations

import argparse
import asyncio
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from erp_automation.contracts.models import Capability, DesktopSnapshot, TaskArea, TaskCommand, TaskRecord, TaskStatus
from erp_automation.coordination import local_browser
from erp_automation.coordination.local_browser import ALIBABA_SCM_HOME_URL, LocalChromeHost
from erp_automation.coordination.remote_controller import RemoteBackgroundTaskController
from shipment_automation import logistics_worker as worker
from shipment_automation.alibaba_session import verify_alibaba_logistics_account
from shipment_automation.models import LogisticsDetail, ShipmentCandidate
from shipment_automation.queue_store import ShipmentWorkflowStore


@pytest.mark.parametrize("existing_url, reuse", [
    ("https://scm.alibaba.com/luyou/express/detail.htm?id=123", True),
    ("https://login.alibaba.com/member/signin.htm", True),
    ("https://passport.alibaba.com/verification", True),
    ("https://login.aliexpress.com/", True),
    ("https://scm.alibaba.com.attacker.example/", False),
    ("https://erp.lingxing.com/", False),
    ("about:blank", False),
    (None, False),
])
def test_prepare_reuses_logistics_page_and_only_opens_home_if_missing(tmp_path, monkeypatch, existing_url, reuse):
    host = LocalChromeHost(24001, tmp_path / "logistics-profile")
    monkeypatch.setattr(host, "ensure_started", lambda **_kwargs: False)
    requests = []

    def get(url, **_kwargs):
        requests.append(url)
        targets = [{"type": "page", "id": "original", "url": existing_url}] if existing_url else []
        return httpx.Response(200, json=targets, request=httpx.Request("GET", url))

    def put(url, **_kwargs):
        requests.append(url)
        return httpx.Response(200, request=httpx.Request("PUT", url))

    monkeypatch.setattr(local_browser.httpx, "get", get)
    monkeypatch.setattr(local_browser.httpx, "put", put)
    host.prepare_logistics_session()
    assert requests[0].endswith("/json/list")
    assert len(requests) == 2
    if reuse:
        assert requests[1].endswith("/json/activate/original")
    else:
        assert "/json/new?https%3A%2F%2Fscm.alibaba.com%2F" in requests[1]


def test_prepare_cold_browser_opens_home_once(tmp_path, monkeypatch):
    host = LocalChromeHost(24001, tmp_path / "logistics-profile")
    starts = []
    monkeypatch.setattr(host, "ensure_started", lambda **kwargs: starts.append(kwargs) or True)
    monkeypatch.setattr(local_browser.httpx, "get", lambda *_args, **_kwargs: pytest.fail("cold start already opened SCM"))
    host.prepare_logistics_session()
    assert starts == [{"initial_url": ALIBABA_SCM_HOME_URL}]


@pytest.mark.parametrize("preserve", [True, False])
def test_worker_cleanup_always_stops_driver_and_only_closes_owned_pages(preserve):
    calls = []

    async def close_page():
        calls.append("page")

    async def close_context():
        calls.append("context")

    async def stop():
        calls.append("driver")

    state = {"page": SimpleNamespace(close=close_page, is_closed=lambda: False), "context": SimpleNamespace(close=close_context), "playwright": SimpleNamespace(stop=stop)}
    asyncio.run(worker._close_browser_state(state, preserve_session=preserve))
    assert calls == (["driver"] if preserve else ["page", "context", "driver"])
    assert all(value is None for value in state.values())


def _remote_client(host):
    client = object.__new__(RemoteBackgroundTaskController)
    client._lock = threading.RLock()
    client._closed = False
    client._authentication_required = False
    client._authentication_error = ""
    client._browser_host = None
    client.browser_endpoint = ""
    client._logistics_browser_host = host
    client.logistics_browser_endpoint = host.endpoint
    client._browser_cleanup_task_ids = set()
    client._browser_close_pending = False
    client._last_interactions = ()
    client._last_snapshot = DesktopSnapshot()
    client._revision = 0
    client.instance_id = "session-reuse-test"
    submitted = []

    def request(_method, _url, **kwargs):
        command = kwargs["json"]["args"][0]
        submitted.append(command)
        return {"revision": len(submitted), "result_type": "control_result", "result": {
            "accepted": True, "message": "submitted", "task_id": f"task-{len(submitted)}", "details": {},
        }}

    client._request = request
    return client, submitted


@pytest.mark.parametrize("account_changed", [False, True])
def test_normal_then_history_keep_same_real_cdp_page_and_session(tmp_path, monkeypatch, account_changed):
    """Exercise desktop submission, the terminal-task gap, and two driver lifetimes.

    All browser requests are fulfilled locally. No Alibaba or ERP is contacted.
    """
    store = ShipmentWorkflowStore(tmp_path / "queue.sqlite3")
    normal = ShipmentCandidate(system_order_no="SYS-1", platform_order_no="ORDER-1", logistics_no="ALS00000000001", shipment_tag_name="标发")
    history = replace(normal, system_order_no="SYS-2", platform_order_no="ORDER-2", logistics_no="ALS00000000002",
                      sales_platform_code="Amazon", platform_order_item_ids=("ITEM-2",),
                      logistics_provider_name="手动", logistics_type_name="万邦速达")

    def detail(logistics_no):
        return LogisticsDetail(logistics_no=logistics_no, carrier="UPS", international_tracking_no="1Z9253126709651051",
                               actual_total="CNY 20", chargeable_weight_kg="1", status_text="运输中")

    store.upsert_candidate(normal)
    store.upsert_candidate(history)
    store.complete_logistics_attempt(history.logistics_no, detail(history.logistics_no), state="READY", last_error=None)
    assert store.mark_erp_outbounded(history.logistics_no, email_preview_enabled=False)
    assert len(store.list_completed_refresh_targets(eligible_only=True)) == 1

    async def run():
        from playwright.async_api import async_playwright

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(tmp_path / "browser"), headless=True, args=[f"--remote-debugging-port={port}"],
            )
            try:
                async def local_response(route):
                    await route.fulfill(status=200, content_type="text/html", body="<html><body>Local session test</body></html>")

                await context.route("**/*", local_response)
                page = context.pages[0]
                await page.goto(ALIBABA_SCM_HOME_URL)
                await page.evaluate("sessionStorage.setItem('session-proof', 'original-login')")
                await context.add_cookies([{"name": "session-proof", "value": "original-cookie", "url": ALIBABA_SCM_HOME_URL}])
                calls = []

                async def fetch(_context, logistics_no, *, page, login_config, **_kwargs):
                    await page.goto(worker.logistics_detail_url(logistics_no))
                    assert await page.evaluate("sessionStorage.getItem('session-proof')") == "original-login"
                    assert any(cookie["name"] == "session-proof" and cookie["value"] == "original-cookie" for cookie in await _context.cookies())
                    calls.append(logistics_no)
                    await verify_alibaba_logistics_account(page, login_config.account)
                    return detail(logistics_no)

                monkeypatch.setattr(worker, "fetch_logistics_detail_from_page", fetch)
                host = LocalChromeHost(port, tmp_path / "browser")
                client, submitted = _remote_client(host)
                for scope, expected in [("normal", normal.logistics_no), ("completed", history.logistics_no)]:
                    account = "changed@example.com" if account_changed and scope == "completed" else "test@example.com"
                    command = TaskCommand("query", TaskArea.SHIPMENT, Capability.ALIBABA_LOGISTICS, payload={"logistics_scope": scope})
                    result = client._rpc("submit_task", command)
                    assert result.accepted
                    args = argparse.Namespace(queue_path=str(store.path), from_queue=True, update_queue=True,
                                              configuration_values={"alibaba.logistics_query.account": account, "alibaba.logistics_query.password": "test"},
                                              browser_cdp_url=host.endpoint, logistics_scope=scope, no_auto_login=True)
                    report = await worker.run_logistics_worker(args)
                    mismatch = account_changed and scope == "completed"
                    assert report["status"] == ("identity_mismatch" if mismatch else "completed")
                    assert calls[-1] == expected
                    if scope == "normal":
                        assert calls == [normal.logistics_no]
                    else:
                        assert report["completed_refresh_checked_count"] == (0 if mismatch else 1)
                        if mismatch:
                            assert report["aborted_count"] == 1
                    client._cleanup_browser_after_terminal_tasks(DesktopSnapshot(tasks=[TaskRecord(
                        result.task_id, "query", TaskArea.SHIPMENT, Capability.ALIBABA_LOGISTICS, status=TaskStatus.FAILED if mismatch else TaskStatus.SUCCEEDED,
                        payload={"_desktop_instance_id": client.instance_id},
                    )]))
                    assert not page.is_closed()
                    assert context.pages == [page]
                    assert await page.evaluate("sessionStorage.getItem('session-proof')") == "original-login"
                assert calls == [normal.logistics_no, history.logistics_no]
                assert len(submitted) == 2
                assert [item["payload"]["logistics_scope"] for item in submitted] == ["normal", "completed"]
            finally:
                await context.close()

    asyncio.run(run())


def test_desktop_exit_closes_retained_logistics_browser():
    calls = []
    host = SimpleNamespace(endpoint="http://127.0.0.1:24001", close=lambda: calls.append("browser"))
    client, _ = _remote_client(host)
    client._request = lambda *_args, **_kwargs: None
    client._client = SimpleNamespace(close=lambda: calls.append("http"))
    client._local_action_pool = SimpleNamespace(shutdown=lambda **_kwargs: calls.append("pool"))
    assert client.prepare_close().accepted
    assert calls == ["pool", "http", "browser"]
    assert client.prepare_close().accepted
    assert calls.count("browser") == 1
