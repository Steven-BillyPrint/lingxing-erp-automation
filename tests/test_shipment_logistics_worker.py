import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from shipment_automation import logistics_worker as worker_module
from shipment_automation.alibaba_logistics import tracking_number_mismatch_reason
from shipment_automation.alibaba_session import (
    AlibabaAccountUnverifiedError,
    AlibabaAccountVerificationError,
)
from shipment_automation.logistics_worker import (
    BROWSER_CLOSED_ERROR_MESSAGE,
    BROWSER_CLOSED_RETRY_MESSAGE,
    LogisticsBrowserClosedError,
    compact_exception_message,
    fetch_logistics_detail_from_page,
    is_browser_closed_error,
    process_logistics_queue_once,
    run_logistics_worker,
)
from shipment_automation.models import (
    LOGISTICS_BLOCKED,
    LOGISTICS_CANCELLED,
    LOGISTICS_PENDING,
    LOGISTICS_READY,
    LOGISTICS_RETRYABLE,
    LOGISTICS_WAITING,
    TRACKING_REVIEW_AUTO_RECHECK,
    TRACKING_REVIEW_ORDER_ISSUE,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentQueueStore


QUERY_CONFIGURATION = {
    "alibaba.logistics_query.account": "query@example.com",
    "alibaba.logistics_query.password": "query-secret",
}


def _candidate(
    logistics_no: str = "ALS01781406025",
    *,
    system_order_no: str = "103710434633847501",
    platform_order_no: str = "112-1165824-9982644",
) -> ShipmentCandidate:
    return ShipmentCandidate(
        system_order_no=system_order_no,
        platform_order_no=platform_order_no,
        logistics_no=logistics_no,
        shipment_tag_name="自动标发",
        tag_text="自动标发",
        sku_text="10x10-Canopy 共1",
        customer_remark=f"重发邮件 {logistics_no}",
        status_text="待审核发货",
    )


def _ready_detail(logistics_no: str = "ALS01781406025") -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=logistics_no,
        status_text="运输中",
        carrier="UPS",
        international_tracking_no="1Z9253126709651051",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
        package_count=1,
    )


def _non_tail_carrier_detail(logistics_no: str = "ALS01781406025") -> LogisticsDetail:
    detail = _ready_detail(logistics_no)
    detail.carrier = "YHA"
    return detail


def _force_legacy_program_block(store: ShipmentQueueStore, logistics_no: str) -> None:
    """Simulate a BLOCKED row persisted by an older release."""

    store.initialize()
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE shipment_logistics
            SET state = ?, next_attempt_at = NULL
            WHERE job_id = (
                SELECT id FROM shipment_jobs WHERE logistics_no = ?
            )
            """,
            (LOGISTICS_BLOCKED, logistics_no),
        )


def test_logistics_worker_detects_browser_closed_errors():
    assert is_browser_closed_error("BrowserContext.new_page: Target page, context or browser has been closed")
    assert is_browser_closed_error("Page.wait_for_timeout: Target page, context or browser has been closed")
    assert compact_exception_message(
        "BrowserContext.new_page: Target page, context or browser has been closed\nBrowser logs:\n<launching> chrome"
    ) == BROWSER_CLOSED_ERROR_MESSAGE
    assert is_browser_closed_error(
        "'NoneType' object has no attribute 'new_page'"
    )


def test_ready_page_does_not_wait_forever_for_hanging_json_response(monkeypatch):
    class HangingResponse:
        request = SimpleNamespace(resource_type="xhr")
        headers = {"content-type": "application/json"}

        async def text(self):
            await asyncio.Event().wait()

    class BodyLocator:
        async def inner_text(self, *, timeout):
            assert timeout == 5000
            return """
物流订单详情
订单状态
运输中
物流订单号
ALS01829169726
服务类型
快递门到门
服务线路
无忧全球普货专线
"""

    class FakePage:
        url = "https://scm.alibaba.com/"

        def __init__(self):
            self.listeners = {}
            self.removed = []
            self.events = []

        def on(self, event, handler):
            self.listeners[event] = handler

        def remove_listener(self, event, handler):
            self.removed.append((event, handler))
            if self.listeners.get(event) is handler:
                self.listeners.pop(event)

        async def goto(self, url, *, wait_until, timeout):
            assert self.events == ["load", "state", "settle", "state"]
            self.events.append("goto")
            self.url = url
            assert wait_until == "domcontentloaded"
            assert timeout == 30000
            self.listeners["response"](HangingResponse())

        async def wait_for_load_state(self, state, *, timeout):
            assert state == "domcontentloaded"
            assert timeout == worker_module.ALIBABA_SCM_WARMUP_LOAD_TIMEOUT_MS
            self.events.append("load")

        async def evaluate(self, script):
            assert script == "() => document.readyState"
            self.events.append("state")
            return "complete"

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 1
            self.events.append("settle")

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator()

        def is_closed(self):
            return False

    async def ready_immediately(*_args, **_kwargs):
        return None

    async def no_structured_groups(_page):
        return []

    monkeypatch.setattr(worker_module, "wait_for_alibaba_logistics_detail", ready_immediately)
    monkeypatch.setattr(worker_module, "extract_logistics_field_groups", no_structured_groups)
    monkeypatch.setattr(worker_module, "READY_RESPONSE_DRAIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(worker_module, "ALIBABA_SCM_WARMUP_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(worker_module, "ALIBABA_SCM_WARMUP_STABLE_OBSERVATIONS", 2)
    page = FakePage()

    started = time.monotonic()
    detail = asyncio.run(
        asyncio.wait_for(
            fetch_logistics_detail_from_page(
                SimpleNamespace(),
                "ALS01829169726",
                page=page,
                auto_login=False,
            ),
            timeout=0.5,
        )
    )

    assert time.monotonic() - started < 0.5
    assert detail.logistics_no == "ALS01829169726"
    assert detail.status_text == "运输中"
    assert page.events == ["load", "state", "settle", "state", "goto"]
    assert page.removed


def test_account_verification_error_is_not_converted_to_order_page_error(monkeypatch):
    class FakePage:
        def on(self, _event, _handler):
            return None

        def remove_listener(self, _event, _handler):
            return None

        async def goto(self, _url, *, wait_until, timeout):
            assert wait_until == "domcontentloaded"
            assert timeout == 30000

    async def no_warmup(_page):
        return None

    async def unverified(*_args, **_kwargs):
        raise AlibabaAccountUnverifiedError("账号身份暂未显示")

    monkeypatch.setattr(worker_module, "_warm_up_alibaba_page_if_needed", no_warmup)
    monkeypatch.setattr(worker_module, "wait_for_alibaba_logistics_detail", unverified)

    with pytest.raises(AlibabaAccountUnverifiedError):
        asyncio.run(
            fetch_logistics_detail_from_page(
                SimpleNamespace(),
                "ALS01829169726",
                page=FakePage(),
            )
        )


def test_logistics_worker_reports_per_order_progress(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    store.insert_candidate(
        _candidate(
            "ALS01789020252",
            system_order_no="103710434633847502",
        )
    )
    updates = []

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            progress_callback=lambda message, percent: updates.append(
                (message, percent)
            ),
        )
    )

    assert report.scanned_page_count == 2
    assert any("（1/2）" in message for message, _percent in updates)
    assert any("（2/2）" in message for message, _percent in updates)
    assert updates[-1][1] == 92


def test_logistics_worker_stops_without_mutation_on_account_mismatch(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    updates = []

    async def mismatched_account(_logistics_no):
        raise AlibabaAccountVerificationError("物流查询账号不一致")

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=mismatched_account,
            update_queue=True,
            dry_run=False,
            progress_callback=lambda message, percent: updates.append(
                (message, percent)
            ),
        )
    )

    assert report.status == "identity_mismatch"
    assert report.message == "物流查询账号不一致"
    assert report.scanned_page_count == 0
    assert report.aborted_count == 1
    assert updates[-1] == ("物流查询账号不一致", 92)
    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_PENDING


def test_logistics_worker_stops_without_mutation_when_account_is_unverified(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    updates = []

    async def unverified_account(_logistics_no):
        raise AlibabaAccountUnverifiedError("账号身份暂未显示")

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=unverified_account,
            update_queue=True,
            dry_run=False,
            progress_callback=lambda message, percent: updates.append(
                (message, percent)
            ),
        )
    )

    assert report.status == "identity_unverified"
    assert "当前页面短暂等待并重试" in report.message
    assert report.scanned_page_count == 0
    assert report.aborted_count == 1
    assert updates[-1] == (report.message, 92)
    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_PENDING


def test_logistics_worker_reuses_existing_scm_tab():
    class FakePage:
        url = "https://scm.alibaba.com/"

        def __init__(self):
            self.front_count = 0

        def is_closed(self):
            return False

        async def bring_to_front(self):
            self.front_count += 1

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]
            self.new_page_count = 0

        async def new_page(self):
            self.new_page_count += 1
            raise AssertionError("不应新建第二个阿里物流标签页")

    page = FakePage()
    context = FakeContext(page)

    selected = asyncio.run(worker_module._acquire_logistics_page(context))

    assert selected is page
    assert page.front_count == 1
    assert context.new_page_count == 0


def test_logistics_worker_reuses_alibaba_login_redirect_for_cold_warmup():
    class FakePage:
        url = "https://login.alibaba.com/member/signin.htm"

        def __init__(self):
            self.front_count = 0

        def is_closed(self):
            return False

        async def bring_to_front(self):
            self.front_count += 1

    class FakeContext:
        def __init__(self, page):
            self.pages = [page]

        async def new_page(self):
            raise AssertionError("不应丢弃 SCM 首页重定向后的登录标签页")

    page = FakePage()
    selected = asyncio.run(worker_module._acquire_logistics_page(FakeContext(page)))

    assert selected is page
    assert page.front_count == 1


def test_logistics_worker_prefers_fresh_scm_home_over_stale_detail_page():
    class FakePage:
        def __init__(self, url):
            self.url = url
            self.front_count = 0

        def is_closed(self):
            return False

        async def bring_to_front(self):
            self.front_count += 1

    class FakeContext:
        def __init__(self, pages):
            self.pages = pages

        async def new_page(self):
            raise AssertionError("已有 SCM 首页时不应新建标签页")

    stale_detail = FakePage(
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252"
    )
    fresh_home = FakePage("https://scm.alibaba.com/")

    selected = asyncio.run(
        worker_module._acquire_logistics_page(
            FakeContext([stale_detail, fresh_home])
        )
    )

    assert selected is fresh_home
    assert fresh_home.front_count == 1
    assert stale_detail.front_count == 0


def test_logistics_worker_warmup_accepts_only_query_session_hosts():
    assert worker_module._is_alibaba_page_url("https://scm.alibaba.com/")
    assert worker_module._is_alibaba_page_url("https://login.alibaba.com/")
    assert not worker_module._is_alibaba_page_url("https://www.alibaba.com/")
    assert not worker_module._is_alibaba_page_url(
        "https://scm.alibaba.com.example.com/"
    )


def test_logistics_worker_does_not_repeat_warmup_on_detail_pages():
    class FakePage:
        url = "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252"

        async def wait_for_load_state(self, *_args, **_kwargs):
            raise AssertionError("详情页不应重复等待冷启动预热")

        async def wait_for_timeout(self, *_args, **_kwargs):
            raise AssertionError("详情页不应重复等待冷启动预热")

    asyncio.run(worker_module._warm_up_alibaba_page_if_needed(FakePage()))


def test_logistics_worker_warmup_resets_stability_after_login_redirect(
    monkeypatch,
):
    observations = iter(
        [
            ("scm_ready", "https://scm.alibaba.com/"),
            ("login_ready", "https://login.alibaba.com/member/signin.htm"),
            ("login_ready", "https://login.alibaba.com/member/signin.htm"),
            ("login_ready", "https://login.alibaba.com/member/signin.htm"),
        ]
    )

    class FakePage:
        url = "https://scm.alibaba.com/"

        def __init__(self):
            self.poll_count = 0

        async def wait_for_load_state(self, state, *, timeout):
            assert state == "domcontentloaded"
            assert timeout == worker_module.ALIBABA_SCM_WARMUP_LOAD_TIMEOUT_MS

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 1
            self.poll_count += 1

    page = FakePage()

    async def landing_state(observed_page):
        state, url = next(observations)
        observed_page.url = url
        return state

    monkeypatch.setattr(worker_module, "_alibaba_landing_page_state", landing_state)
    monkeypatch.setattr(worker_module, "ALIBABA_SCM_WARMUP_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(worker_module, "ALIBABA_SCM_WARMUP_STABLE_OBSERVATIONS", 3)

    asyncio.run(worker_module._warm_up_alibaba_page_if_needed(page))

    assert page.url == "https://login.alibaba.com/member/signin.htm"
    assert page.poll_count == 3


def test_alibaba_landing_page_state_uses_document_and_login_state(monkeypatch):
    class FakePage:
        url = "https://scm.alibaba.com/"

        def __init__(self):
            self.ready_state = "loading"

        def is_closed(self):
            return False

        async def evaluate(self, script):
            assert script == "() => document.readyState"
            return self.ready_state

    page = FakePage()

    async def not_login(_page):
        return False

    monkeypatch.setattr(worker_module, "is_alibaba_login_page", not_login)

    assert asyncio.run(worker_module._alibaba_landing_page_state(page)) == "loading"

    page.ready_state = "complete"
    assert asyncio.run(worker_module._alibaba_landing_page_state(page)) == "scm_ready"

    async def is_login(_page):
        return True

    monkeypatch.setattr(worker_module, "is_alibaba_login_page", is_login)
    assert asyncio.run(worker_module._alibaba_landing_page_state(page)) == "login_ready"


def test_logistics_worker_dry_run_does_not_update_queue(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=False))

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_PENDING
    assert row["carrier"] is None
    assert report.ready_count == 1
    assert report.ready_to_mark_items[0].logistics_no == "ALS01781406025"


def test_logistics_worker_update_queue_writes_ready_fields(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_READY
    assert row["carrier"] == "UPS"
    assert row["international_tracking_no"] == "1Z9253126709651051"
    assert report.ready_count == 1
    assert report.skipped_query_records == []


def test_logistics_worker_restarts_once_after_browser_closed(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(("first", logistics_no))
        raise LogisticsBrowserClosedError("BrowserContext.new_page: Target page, context or browser has been closed")

    async def fake_retry_fetch(logistics_no):
        calls.append(("retry", logistics_no))
        return _ready_detail(logistics_no)

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            retry_fetch_detail=fake_retry_fetch,
            update_queue=True,
            dry_run=False,
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert calls == [("first", "ALS01781406025"), ("retry", "ALS01781406025")]
    assert row["logistics_state"] == LOGISTICS_READY
    assert report.retryable_count == 0
    assert BROWSER_CLOSED_RETRY_MESSAGE in report.warnings


def test_logistics_worker_browser_closed_after_retry_stays_error(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        raise LogisticsBrowserClosedError("BrowserContext.new_page: Target page, context or browser has been closed")

    async def fake_retry_fetch(logistics_no):
        raise LogisticsBrowserClosedError(
            "Page.wait_for_timeout: Target page, context or browser has been closed\nBrowser logs:\n<launching> chrome"
        )

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            retry_fetch_detail=fake_retry_fetch,
            update_queue=True,
            dry_run=False,
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert row["logistics_state"] == LOGISTICS_RETRYABLE
    assert row["last_error"] == BROWSER_CLOSED_ERROR_MESSAGE
    assert "Browser logs:" not in row["last_error"]
    assert report.retryable_count == 1
    assert report.blocked_count == 0
    assert report.failed_count == 1
    assert report.browser_error_count == 1
    assert report.status == "failed"


def test_logistics_worker_stops_batch_after_browser_recovery_failure(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    for index in range(3):
        store.insert_candidate(
            _candidate(
                f"ALS{index:011d}",
                system_order_no=f"10371043463384750{index}",
                platform_order_no=f"112-1165824-998264{index}",
            )
        )
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(("fetch", logistics_no))
        raise LogisticsBrowserClosedError(
            "'NoneType' object has no attribute 'new_page'"
        )

    async def fake_retry_fetch(logistics_no):
        calls.append(("retry", logistics_no))
        raise LogisticsBrowserClosedError(
            "BrowserContext.new_page: Target page, context or browser has been closed"
        )

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            retry_fetch_detail=fake_retry_fetch,
            update_queue=True,
            dry_run=False,
        )
    )

    assert calls == [
        ("fetch", "ALS00000000000"),
        ("retry", "ALS00000000000"),
    ]
    assert report.status == "failed"
    assert report.scanned_page_count == 1
    assert report.failed_count == 1
    assert report.browser_error_count == 1
    assert report.aborted_count == 2
    assert store.get_by_logistics_no("ALS00000000000")["logistics_state"] == LOGISTICS_RETRYABLE
    assert store.get_by_logistics_no("ALS00000000001")["logistics_state"] == LOGISTICS_PENDING


def test_run_logistics_worker_reports_browser_restart_failure_and_releases_batch(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    logistics_numbers = []
    for index in range(3):
        logistics_no = f"ALS{index:011d}"
        logistics_numbers.append(logistics_no)
        store.insert_candidate(
            _candidate(
                logistics_no,
                system_order_no=f"10371043463384750{index}",
                platform_order_no=f"112-1165824-998264{index}",
            )
        )
    launches = 0
    fetches = []

    class FakeContext:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_launch_context(_args):
        nonlocal launches
        launches += 1
        if launches == 2:
            raise RuntimeError("Chrome restart unavailable")
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        fetches.append(logistics_no)
        return LogisticsDetail(
            logistics_no=logistics_no,
            page_error="'NoneType' object has no attribute 'new_page'",
        )

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(
        worker_module,
        "fetch_logistics_detail_from_page",
        fake_fetch_detail,
    )

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                configuration_values=QUERY_CONFIGURATION,
                env_path=".env",
                limit=20,
                process_all_batches=True,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    assert fetches == [logistics_numbers[0]]
    assert result["status"] == "failed"
    assert result["scanned_page_count"] == 1
    assert result["failed_count"] == 1
    assert result["browser_error_count"] == 1
    assert result["aborted_count"] == 2
    assert "读取失败 1 条" in result["message"]
    assert "浏览器故障 1 次" in result["message"]
    assert all(
        store.get_by_logistics_no(value)["lease_stage"] is None
        for value in logistics_numbers
    )


def test_run_logistics_worker_blocks_when_query_credentials_are_missing(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate())

    async def fail_if_launched(_args):
        raise AssertionError("物流查询账号未配置时不应启动浏览器")

    monkeypatch.setattr(worker_module, "launch_context", fail_if_launched)

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=False,
                configuration_values={
                    "alibaba.account": "order@example.com",
                    "alibaba.password": "order-secret",
                },
                limit=20,
                process_all_batches=True,
            )
        )
    )

    assert result["status"] == "configuration_missing"
    assert result["message"] == "阿里物流查询账号未配置"
    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_PENDING


def test_logistics_worker_dedupes_same_logistics_number_in_output(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))
    row = store.get_by_logistics_no("ALS01781406025")
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=fake_fetch,
            preloaded_rows=[row, row],
        )
    )

    assert calls == ["ALS01781406025"]
    assert report.scanned_page_count == 1
    assert [item.logistics_no for item in report.ready_to_mark_items] == ["ALS01781406025"]


def test_logistics_worker_non_real_carrier_not_ready_to_mark(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _non_tail_carrier_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=False))

    assert report.ready_to_mark_items == []
    assert report.waiting_count == 1
    assert report.query_results[0].logistics_state == LOGISTICS_WAITING
    assert "不是真实海外尾程承运商" in report.query_results[0].last_error


@pytest.mark.parametrize("status_text", ("订单关闭", "订单终止"))
def test_logistics_worker_closes_cancelled_alibaba_order_without_retry(
    tmp_path,
    status_text,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01920294001"))

    async def closed_detail(logistics_no):
        return LogisticsDetail(
            logistics_no=logistics_no,
            status_text=status_text,
        )

    report = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=closed_detail,
            update_queue=True,
        )
    )

    assert report.cancelled_count == 1
    assert report.waiting_count == 0
    row = store.get_by_logistics_no("ALS01920294001")
    assert row["logistics_state"] == LOGISTICS_CANCELLED
    assert row["logistics_next_attempt_at"] is None
    assert store.claim_logistics_jobs("next-worker") == []


def test_logistics_worker_reports_retryable_page_read_as_failure(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate())

    async def permission_error(logistics_no):
        return LogisticsDetail(
            logistics_no=logistics_no,
            page_error="物流详情无权限",
        )

    report = asyncio.run(
        process_logistics_queue_once(store, fetch_detail=permission_error)
    )

    assert report.status == "completed_with_skips"
    assert report.scanned_page_count == 1
    assert report.failed_count == 1
    assert report.blocked_count == 0
    assert report.retryable_count == 1
    assert "读取失败 1 条" in report.message


def test_logistics_worker_update_queue_records_non_real_carrier_reason(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01781406025"))

    async def fake_fetch(logistics_no):
        return _non_tail_carrier_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))
    row = store.get_by_logistics_no("ALS01781406025")

    assert report.ready_to_mark_items == []
    assert row["logistics_state"] == LOGISTICS_WAITING
    assert row["carrier"] == "YHA"
    assert "不是真实海外尾程承运商" in row["last_error"]


def test_logistics_worker_retries_error_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    error_candidate = _candidate("ALS01781406025")
    query_candidate = _candidate(
        "ALS01789020252",
        system_order_no="103710434633847502",
    )
    store.insert_candidate(error_candidate)
    store.insert_candidate(query_candidate)
    store.complete_logistics_attempt(
        error_candidate.logistics_no,
        LogisticsDetail(logistics_no=error_candidate.logistics_no, page_error="上一轮查询失败"),
        state=LOGISTICS_RETRYABLE,
        last_error="上一轮查询失败",
    )
    store.retry_stage(error_candidate.logistics_no, "logistics")
    calls = []

    async def fake_fetch(logistics_no):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch, update_queue=True, dry_run=False))

    assert sorted(calls) == ["ALS01781406025", "ALS01789020252"]
    assert store.get_by_logistics_no("ALS01781406025")["logistics_state"] == LOGISTICS_READY
    assert report.skipped_query_records == []


def test_logistics_worker_auto_rechecks_reviewed_mismatch_until_ready(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentQueueStore(path)
    candidate = _candidate()
    store.insert_candidate(candidate)
    mismatch = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="FedEx",
        international_tracking_no="1Z9253126709651051",
        actual_total="CNY 123.45",
        chargeable_weight_kg="4.500",
    )
    store.complete_logistics_attempt(
        candidate.logistics_no,
        mismatch,
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason(
            mismatch.carrier,
            mismatch.international_tracking_no,
        ),
    )
    store.set_tracking_mismatch_review(candidate.logistics_no, TRACKING_REVIEW_AUTO_RECHECK)
    calls = []

    async def still_mismatch(logistics_no):
        calls.append(logistics_no)
        return mismatch

    first = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=still_mismatch,
            update_queue=True,
            dry_run=False,
        )
    )

    assert calls == [candidate.logistics_no]
    assert first.blocked_count == 0
    assert first.retryable_count == 1
    row = store.get_by_logistics_no(candidate.logistics_no)
    assert row["tracking_mismatch_action"] == TRACKING_REVIEW_AUTO_RECHECK
    assert row["logistics_next_attempt_at"]
    assert store.list_pending_tracking_mismatch_reviews() == []

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE shipment_logistics SET next_attempt_at = '2000-01-01T00:00:00Z'")

    async def now_ready(logistics_no):
        return _ready_detail(logistics_no)

    second = asyncio.run(
        process_logistics_queue_once(
            store,
            fetch_detail=now_ready,
            update_queue=True,
            dry_run=False,
        )
    )

    assert second.ready_count == 1
    ready = store.get_by_logistics_no(candidate.logistics_no)
    assert ready["logistics_state"] == LOGISTICS_READY
    assert ready["tracking_mismatch_action"] is None


def test_logistics_worker_reports_skipped_manual_review_records(tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    manual_candidate = _candidate("ALS01781406025")
    query_candidate = _candidate(
        "ALS01789020252",
        system_order_no="103710434633847502",
    )
    store.insert_candidate(manual_candidate)
    store.insert_candidate(query_candidate)
    mismatch_error = tracking_number_mismatch_reason(
        "FedEx",
        "1Z9253126709651051",
    )
    store.complete_logistics_attempt(
        manual_candidate.logistics_no,
        LogisticsDetail(
            logistics_no=manual_candidate.logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="1Z9253126709651051",
            actual_total="CNY 123.45",
            chargeable_weight_kg="4.500",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=mismatch_error,
    )
    assert store.set_tracking_mismatch_review(
        manual_candidate.logistics_no,
        TRACKING_REVIEW_ORDER_ISSUE,
    )

    async def fake_fetch(logistics_no):
        return _ready_detail(logistics_no)

    report = asyncio.run(process_logistics_queue_once(store, fetch_detail=fake_fetch))

    assert [item.logistics_no for item in report.skipped_query_records] == ["ALS01781406025"]
    assert report.skipped_query_records[0].last_error == mismatch_error


def test_run_logistics_worker_queries_retryable_browser_error(monkeypatch, tmp_path):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    manual_candidate = _candidate("ALS01781406025")
    store.insert_candidate(manual_candidate)
    store.complete_logistics_attempt(
        manual_candidate.logistics_no,
        LogisticsDetail(logistics_no=manual_candidate.logistics_no, page_error=BROWSER_CLOSED_ERROR_MESSAGE),
        state=LOGISTICS_RETRYABLE,
        last_error=BROWSER_CLOSED_ERROR_MESSAGE,
    )
    store.retry_stage(manual_candidate.logistics_no, "logistics")
    calls = []

    class FakeContext:
        async def close(self):
            calls.append("context.close")

    class FakePlaywright:
        async def stop(self):
            calls.append("playwright.stop")

    async def fake_launch_context(_args):
        calls.append("launch")
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(worker_module, "fetch_logistics_detail_from_page", fake_fetch_detail)

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                configuration_values=QUERY_CONFIGURATION,
                env_path=".env",
                limit=20,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    row = store.get_by_logistics_no("ALS01781406025")
    assert "ALS01781406025" in calls
    assert row["logistics_state"] == LOGISTICS_READY
    assert result["ready_count"] == 1


def test_run_logistics_worker_processes_all_due_rows_in_safe_batches(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    logistics_numbers = []
    for index in range(45):
        logistics_no = f"ALS{index:011d}"
        logistics_numbers.append(logistics_no)
        store.insert_candidate(
            _candidate(
                logistics_no,
                system_order_no=f"1037104346338{index:05d}",
                platform_order_no=f"112-1165824-{index:07d}",
            )
        )
    calls = []

    class FakeContext:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_launch_context(_args):
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        calls.append(logistics_no)
        return _ready_detail(logistics_no)

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(
        worker_module,
        "fetch_logistics_detail_from_page",
        fake_fetch_detail,
    )

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                configuration_values=QUERY_CONFIGURATION,
                env_path=".env",
                limit=20,
                process_all_batches=True,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    assert calls == logistics_numbers
    assert result["target_count"] == 45
    assert result["scanned_page_count"] == 45
    assert result["batch_count"] == 3
    assert not store.list_logistics_check_candidates()
    assert all(
        store.get_by_logistics_no(logistics_no)["logistics_state"]
        == LOGISTICS_READY
        for logistics_no in logistics_numbers
    )


def test_new_pending_logistics_is_prioritized_before_old_retry(tmp_path):
    path = tmp_path / "shipment_queue.sqlite3"
    store = ShipmentQueueStore(path)
    old_retry = _candidate("ALS01781406025")
    new_pending = _candidate(
        "ALS01789020252",
        system_order_no="103710434633847502",
    )
    store.insert_candidate(old_retry)
    store.complete_logistics_attempt(
        old_retry.logistics_no,
        LogisticsDetail(
            logistics_no=old_retry.logistics_no,
            page_error="上一轮查询失败",
        ),
        state=LOGISTICS_RETRYABLE,
        last_error="上一轮查询失败",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE shipment_logistics
            SET next_attempt_at = '2000-01-01T00:00:00Z'
            WHERE job_id = (
                SELECT id FROM shipment_jobs WHERE logistics_no = ?
            )
            """,
            (old_retry.logistics_no,),
        )
    store.insert_candidate(new_pending)

    rows = store.list_logistics_check_candidates(limit=1)

    assert [row["logistics_no"] for row in rows] == [new_pending.logistics_no]


def test_cancelled_logistics_worker_releases_unfinished_job_leases(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    store.insert_candidate(_candidate("ALS01829169726"))
    fetch_started = asyncio.Event()

    class FakeContext:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_launch_context(_args):
        return FakePlaywright(), FakeContext()

    async def hanging_fetch(_context, _logistics_no, **_kwargs):
        fetch_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(
        worker_module,
        "fetch_logistics_detail_from_page",
        hanging_fetch,
    )

    async def scenario():
        task = asyncio.create_task(
            run_logistics_worker(
                SimpleNamespace(
                    queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                    update_queue=True,
                    from_queue=True,
                    no_auto_login=True,
                    configuration_values=QUERY_CONFIGURATION,
                    env_path=".env",
                    limit=20,
                    login_timeout_sec=300,
                    keep_browser_open=False,
                )
            )
        )
        await asyncio.wait_for(fetch_started.wait(), timeout=1)
        claimed = store.get_by_logistics_no("ALS01829169726")
        assert claimed["lease_stage"] == "logistics"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    released = store.get_by_logistics_no("ALS01829169726")
    assert released["lease_owner"] is None
    assert released["lease_stage"] is None
    assert released["lease_until"] is None


def test_run_logistics_worker_repairs_and_requeries_obvious_parser_artifact(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate("ALS01782864331")
    store.insert_candidate(candidate)
    store.complete_logistics_attempt(
        candidate.logistics_no,
        LogisticsDetail(
            logistics_no=candidate.logistics_no,
            status_text="已开船",
            carrier="FedEx",
            international_tracking_no=candidate.logistics_no,
            actual_total="CNY 631.2",
            chargeable_weight_kg="30.000",
        ),
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason("FedEx", candidate.logistics_no),
    )
    _force_legacy_program_block(store, candidate.logistics_no)

    class FakeContext:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_launch_context(_args):
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        return LogisticsDetail(
            logistics_no=logistics_no,
            status_text="运输中",
            carrier="FedEx",
            international_tracking_no="525885561600",
            actual_total="CNY 631.2",
            chargeable_weight_kg="30.000",
        )

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(worker_module, "fetch_logistics_detail_from_page", fake_fetch_detail)

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                configuration_values=QUERY_CONFIGURATION,
                env_path=".env",
                limit=20,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    assert result["parser_artifact_requeued_count"] == 1
    assert result["ready_count"] == 1
    assert store.get_by_logistics_no(candidate.logistics_no)["logistics_state"] == LOGISTICS_READY
    assert any(
        event.event_type == "LOGISTICS_PARSER_ARTIFACT_REQUEUED"
        for event in store.history(candidate.logistics_no)
    )


def test_run_logistics_worker_rechecks_tracking_pair_accepted_by_current_rules(
    monkeypatch,
    tmp_path,
):
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate("ALS01844600001")
    store.insert_candidate(candidate)
    detail = LogisticsDetail(
        logistics_no=candidate.logistics_no,
        status_text="运输中",
        carrier="YWE",
        international_tracking_no="YWNJC010158019848",
        actual_total="CNY 631.2",
        chargeable_weight_kg="30.000",
    )
    store.complete_logistics_attempt(
        candidate.logistics_no,
        detail,
        state=LOGISTICS_BLOCKED,
        last_error=tracking_number_mismatch_reason(
            detail.carrier,
            detail.international_tracking_no,
        ),
    )
    _force_legacy_program_block(store, candidate.logistics_no)

    class FakeContext:
        async def close(self):
            return None

    class FakePlaywright:
        async def stop(self):
            return None

    async def fake_launch_context(_args):
        return FakePlaywright(), FakeContext()

    async def fake_fetch_detail(_context, logistics_no, **_kwargs):
        assert logistics_no == candidate.logistics_no
        return detail

    monkeypatch.setattr(worker_module, "launch_context", fake_launch_context)
    monkeypatch.setattr(worker_module, "fetch_logistics_detail_from_page", fake_fetch_detail)

    result = asyncio.run(
        run_logistics_worker(
            SimpleNamespace(
                queue_path=str(tmp_path / "shipment_queue.sqlite3"),
                update_queue=True,
                from_queue=True,
                no_auto_login=True,
                configuration_values=QUERY_CONFIGURATION,
                env_path=".env",
                limit=20,
                login_timeout_sec=300,
                keep_browser_open=False,
            )
        )
    )

    assert result["tracking_rule_requeued_count"] == 1
    assert result["ready_count"] == 1
    assert store.get_by_logistics_no(candidate.logistics_no)["logistics_state"] == LOGISTICS_READY
    assert any(
        event.event_type == "TRACKING_RULE_MATCH_REQUEUED"
        for event in store.history(candidate.logistics_no)
    )
