from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
import erp_automation.application.api_erp_mark as api_erp_mark_module

from erp_automation.application.api_erp_mark import (
    ApiErpMarkAdapter,
    ErpLogisticsRoute,
    ManagedApiErpMarkFunc,
    OutboundStrategy,
    routes_from_configuration,
)
from erp_automation.application.capabilities import (
    Capability,
    ManualReviewRequired,
    MutationResult,
    MutationState,
)
from erp_automation.application.lingxing_gateway import (
    FAST_OUTBOUND_FAILED,
    FAST_OUTBOUND_RESULT_STATE_KEY,
    FAST_OUTBOUND_SUCCEEDED,
    PageResult,
)
from shipment_automation.erp_mark_ship import (
    ErpMarkEmergencyStopped,
    ErpMarkManualReview,
    ErpMarkUserAbort,
)
from shipment_automation.models import (
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_OUTBOUNDED,
    ReadyToMarkItem,
)


def _item(**overrides: Any) -> ReadyToMarkItem:
    values: dict[str, Any] = {
        "system_order_no": "103710434633847501",
        "platform_order_no": "112-1165824-9982644",
        "logistics_no": "ALS01781406025",
        "carrier": "UPS",
        "service_line": "UPS-Saver",
        "international_tracking_no": "1Z9253126709651051",
        "actual_total": "CNY 123.45",
        "chargeable_weight_kg": "4.500",
    }
    values.update(overrides)
    return ReadyToMarkItem(**values)


def _mutation(data: Any = None, *, state: MutationState = MutationState.SUCCEEDED) -> MutationResult:
    details = {"operation": "test", "api_code": "0"}
    if data is not None:
        details["data"] = data
    return MutationResult(
        state=state,
        source="lingxing_api",
        request_id="request-1",
        message="success",
        definitely_not_executed=state is MutationState.FAILED,
        details=details,
    )


def _wms_row(
    *,
    status: int,
    tracked: bool = False,
    suffix: str = "",
    status_name: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_number": "103710434633847501",
        "platform_order_no": ["112-1165824-9982644"],
        "wo_number": f"WO-1{suffix}",
        "status": status,
        "status_name": status_name,
        "waybill_no": "",
        "tracking_no": "",
        "logistics_freight": "0.00",
        "logistics_freight_currency_code": "",
        "pkg_fee_weight": "0.000",
        "pkg_fee_weight_unit": "g",
    }
    if tracked:
        row.update(
            {
                "waybill_no": "1Z9253126709651051",
                "tracking_no": "ALS01781406025",
                "logistics_freight": "123.450",
                "logistics_freight_currency_code": "CNY",
                "pkg_fee_weight": "4500.0",
                "pkg_fee_weight_unit": "g",
            }
        )
    return row


class FakeGateway:
    def __init__(self, *, writes_enabled: bool = True) -> None:
        self.router = SimpleNamespace(writes_enabled=writes_enabled)
        self.calls: list[tuple[str, Any]] = []
        self.wms_pages: list[object] = []
        self.fast_results: list[object] = []
        self.shipping_result = _mutation({"error_details": []})
        self.review_result = _mutation(
            {
                "success_num": 1,
                "fail_num": 0,
                "success_info": [{"global_order_no": "103710434633847501"}],
                "failure_info": [],
            }
        )
        self.tracking_result = _mutation()
        self.delivery_result = _mutation(
            {
                "success_list": [
                    {
                        "order_number": "103710434633847501",
                        "status_name": "已发货",
                    }
                ],
                "fail_list": [],
            }
        )
        self.fast_result = _mutation(True)

    async def set_shipping_channel(self, orders, **kwargs):
        self.calls.append(("set_shipping_channel", (orders, kwargs)))
        return self.shipping_result

    async def review_orders(self, orders, **kwargs):
        self.calls.append(("review_orders", (orders, kwargs)))
        return self.review_result

    async def list_wms_orders(self, **kwargs):
        self.calls.append(("list_wms_orders", kwargs))
        if not self.wms_pages:
            raise AssertionError("No WMS page queued")
        outcome = self.wms_pages.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        rows = tuple(outcome)
        return PageResult(items=rows, offset=0, length=200, total=len(rows))

    async def set_tracking_no(self, **kwargs):
        self.calls.append(("set_tracking_no", kwargs))
        return self.tracking_result

    async def deliver_orders(self, orders, **kwargs):
        self.calls.append(("deliver_orders", (orders, kwargs)))
        return self.delivery_result

    async def fast_outbound(self, packages, **kwargs):
        self.calls.append(("fast_outbound", (packages, kwargs)))
        return self.fast_result

    async def get_fast_outbound_result(self, orders, **kwargs):
        self.calls.append(("get_fast_outbound_result", (orders, kwargs)))
        if not self.fast_results:
            raise AssertionError("No fast-outbound result queued")
        outcome = self.fast_results.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return tuple(outcome)


def _adapter(gateway: FakeGateway, **kwargs: Any) -> ApiErpMarkAdapter:
    async def no_sleep(_seconds: float) -> None:
        return None

    sleeper = kwargs.pop("sleeper", no_sleep)
    return ApiErpMarkAdapter(
        gateway,  # type: ignore[arg-type]
        {"UPS": ErpLogisticsRoute(warehouse_id=50, logistics_type_id=825)},
        sleeper=sleeper,
        **kwargs,
    )


def test_staged_api_mark_uses_documented_payloads_and_readback() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [
            [],
            [_wms_row(status=1)],
            [_wms_row(status=2, tracked=True)],
            [_wms_row(status=3, tracked=True)],
        ]
        adapter = _adapter(gateway)
        prompts: list[str] = []
        approvals: list[tuple[str, str]] = []
        checkpoints: list[tuple[str, dict[str, str | None]]] = []

        async def confirm(prompt: str) -> bool:
            prompts.append(prompt)
            return True

        async def approve(kind: str, payload_hash: str) -> None:
            approvals.append((kind, payload_hash))

        async def checkpoint(
            name: str, values: dict[str, str | None]
        ) -> None:
            checkpoints.append((name, values))

        result = await adapter(
            None,
            _item(),
            confirm,
            checkpoint_func=checkpoint,
            approval_func=approve,
        )

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == [
            "list_wms_orders",
            "set_shipping_channel",
            "review_orders",
            "list_wms_orders",
            "set_tracking_no",
            "list_wms_orders",
            "deliver_orders",
            "list_wms_orders",
        ]
        assert len(prompts) == 4
        confirm_line = "请输入 y 确认，其他输入跳过当前订单："
        assert prompts == [
            (
                "即将发送的设置仓库物流参数：\n"
                "系统单号：103710434633847501\n"
                "仓库物流渠道：UPS（默认线路）\n"
                f"{confirm_line}"
            ),
            (
                "即将发送的审核发货参数：\n"
                '系统单号列表：["103710434633847501"]\n'
                f"{confirm_line}"
            ),
            (
                "即将发送的运单填写参数：\n"
                "国际物流单号：1Z9253126709651051\n"
                "销售出库单号：WO-1\n"
                "阿里物流单号：ALS01781406025\n"
                "运费：123.45\n"
                "运费币种：CNY\n"
                "计费重量：4500\n"
                "计费重量单位：g\n"
                f"{confirm_line}"
            ),
            (
                "即将发送的出库发货参数：\n"
                "系统单号列表：103710434633847501\n"
                f"{confirm_line}"
            ),
        ]
        assert approvals[0][0] == "logistics"
        assert len(approvals[0][1]) == 64
        assert checkpoints[0][0] == "CHANNEL_SET"
        assert checkpoints[0][1]["channel_path"] == "UPS（默认线路）"
        assert len(checkpoints[0][1]["channel_payload_hash"] or "") == 64
        shipping_payload = gateway.calls[1][1][0][0]
        assert shipping_payload == {
            "global_order_no": "103710434633847501",
            "logistics": {"logistics_type_id": 825, "sys_wid": 50},
        }
        wms_filters = gateway.calls[0][1]["filters"]
        assert wms_filters == {
            "page": 1,
            "page_size": 200,
            "order_number_arr": ["103710434633847501"],
        }
        tracking = gateway.calls[4][1]
        assert tracking["waybill_no"] == "1Z9253126709651051"
        assert tracking["tracking_no"] == "ALS01781406025"
        assert tracking["wo_number"] == "WO-1"
        assert tracking["logistics_freight"] == "123.45"
        assert tracking["logistics_freight_currency_code"] == "CNY"
        assert tracking["pkg_fee_weight"] == "4500"
        assert gateway.calls[6][1][0] == ["103710434633847501"]
        assert all(values[1].get("browser") is None for _, values in [gateway.calls[1], gateway.calls[2], gateway.calls[6]])

    asyncio.run(run())


def test_staged_mark_waits_for_delayed_review_tracking_and_outbound_projection() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [
            [],
            [],
            [],
            [_wms_row(status=1)],
            [_wms_row(status=1)],
            [_wms_row(status=1)],
            [_wms_row(status=2, tracked=True)],
            [_wms_row(status=2, tracked=True)],
            [_wms_row(status=3, tracked=True)],
        ]
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        adapter = _adapter(
            gateway,
            readback_delays_seconds=[0, 7, 11],
            sleeper=sleeper,
        )

        result = await adapter(None, _item(), _always_confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert sleeps == [7, 11, 7, 11, 7]
        assert [name for name, _ in gateway.calls].count("set_shipping_channel") == 1
        assert [name for name, _ in gateway.calls].count("review_orders") == 1
        assert [name for name, _ in gateway.calls].count("set_tracking_no") == 1
        assert [name for name, _ in gateway.calls].count("deliver_orders") == 1

    asyncio.run(run())


def test_emergency_stop_after_waybill_approval_prevents_tracking_request() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[], [_wms_row(status=1)]]
        adapter = _adapter(gateway)
        allowed = True

        async def guard() -> bool:
            return allowed

        async def approval(_kind: str, _payload_hash: str) -> None:
            nonlocal allowed
            allowed = False

        with pytest.raises(ErpMarkEmergencyStopped):
            await adapter(
                None,
                _item(),
                _always_confirm,
                approval_func=approval,
                runtime_guard_func=guard,
            )

        assert [name for name, _ in gateway.calls] == [
            "list_wms_orders",
            "set_shipping_channel",
            "review_orders",
            "list_wms_orders",
        ]

    asyncio.run(run())


def test_unknown_write_becomes_manual_review_and_is_not_retried() -> None:
    class UnknownGateway(FakeGateway):
        async def set_shipping_channel(self, orders, **kwargs):
            self.calls.append(("set_shipping_channel", (orders, kwargs)))
            result = _mutation(state=MutationState.UNKNOWN)
            raise ManualReviewRequired(
                Capability.SET_SHIPPING_CHANNEL,
                "unknown",
                result=result,
            )

    async def run() -> None:
        gateway = UnknownGateway()
        gateway.wms_pages = [[]]
        adapter = _adapter(gateway)

        with pytest.raises(ErpMarkManualReview, match="禁止自动重试或网页回退"):
            await adapter(None, _item(), _always_confirm)

        assert [name for name, _ in gateway.calls] == [
            "list_wms_orders",
            "set_shipping_channel",
        ]

    asyncio.run(run())


def test_definitive_api_rejection_asks_then_resumes_with_browser(monkeypatch) -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[]]
        gateway.shipping_result = _mutation(state=MutationState.FAILED)
        adapter = _adapter(gateway)
        prompts: list[str] = []
        browser_calls: list[str] = []

        async def confirm(prompt: str) -> bool:
            prompts.append(prompt)
            return True

        async def browser_fallback(
            page,
            item,
            _confirm,
            *,
            checkpoint_func,
            approval_func,
        ) -> str:
            assert page == "logged-in-page"
            assert item.erp_checkpoint == "NONE"
            assert callable(checkpoint_func)
            assert callable(approval_func)
            browser_calls.append(item.platform_order_no)
            return ERP_CHECKPOINT_OUTBOUNDED

        monkeypatch.setattr(api_erp_mark_module, "execute_erp_mark_item", browser_fallback)
        result = await adapter("logged-in-page", _item(), confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert browser_calls == ["112-1165824-9982644"]
        assert len(prompts) == 2
        assert "设置仓库物流" in prompts[0]
        assert "明确拒绝" in prompts[1]
        assert "改用原网页流程" in prompts[1]

    asyncio.run(run())


def test_browser_page_is_requested_only_after_api_rejection_is_approved(
    monkeypatch,
) -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[]]
        gateway.shipping_result = _mutation(state=MutationState.FAILED)
        adapter = _adapter(gateway)
        events: list[str] = []

        async def confirm(prompt: str) -> bool:
            events.append(
                "fallback_confirmed"
                if "改用原网页流程" in prompt
                else "api_write_confirmed"
            )
            return True

        async def browser_page_provider():
            events.append("browser_started")
            return "lazy-page"

        async def browser_fallback(
            page,
            _item,
            _confirm,
            *,
            checkpoint_func,
            approval_func,
        ) -> str:
            assert page == "lazy-page"
            assert callable(checkpoint_func)
            assert callable(approval_func)
            events.append("browser_fallback")
            return ERP_CHECKPOINT_OUTBOUNDED

        monkeypatch.setattr(
            api_erp_mark_module,
            "execute_erp_mark_item",
            browser_fallback,
        )
        result = await adapter(
            None,
            _item(),
            confirm,
            browser_page_provider=browser_page_provider,
        )

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert events == [
            "api_write_confirmed",
            "fallback_confirmed",
            "browser_started",
            "browser_fallback",
        ]

    asyncio.run(run())


def test_write_kill_switch_blocks_before_confirmation_or_api_call() -> None:
    async def run() -> None:
        gateway = FakeGateway(writes_enabled=False)
        adapter = _adapter(gateway)
        confirmations = 0

        async def confirm(_prompt: str) -> bool:
            nonlocal confirmations
            confirmations += 1
            return True

        with pytest.raises(ErpMarkManualReview, match="紧急开关未开启"):
            await adapter(None, _item(), confirm)

        assert confirmations == 0
        assert gateway.calls == []

    asyncio.run(run())


def test_missing_route_is_blocked_without_guessing_ids() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        adapter = ApiErpMarkAdapter(gateway, {})  # type: ignore[arg-type]

        with pytest.raises(ErpMarkManualReview, match="禁止按名称猜测"):
            await adapter(None, _item(), _always_confirm)

        assert gateway.calls == []

    asyncio.run(run())


def test_declined_confirmation_skips_order_before_first_write() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[]]
        adapter = _adapter(gateway)

        async def decline(_prompt: str) -> bool:
            return False

        with pytest.raises(ErpMarkUserAbort):
            await adapter(None, _item(), decline)

        assert [name for name, _ in gateway.calls] == ["list_wms_orders"]

    asyncio.run(run())


def test_fast_outbound_is_submitted_once_then_polled_until_success() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.fast_results = [
            [
                {
                    "global_order_no": "103710434633847501",
                    "error_message": "订单没提交快速出库",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
                }
            ],
            [
                {
                    "global_order_no": "103710434633847501",
                    "error_message": "正在处理",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
                }
            ],
            [
                {
                    "global_order_no": "103710434633847501",
                    "wo_number": "WO-1",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED,
                }
            ],
        ]
        sleeps: list[float] = []

        async def no_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        adapter = ApiErpMarkAdapter(
            gateway,  # type: ignore[arg-type]
            {
                "UPS": ErpLogisticsRoute(
                    warehouse_id=50,
                    logistics_type_id=825,
                    fast_logistics_type_id="6-825",
                )
            },
            outbound_strategy=OutboundStrategy.FAST_OUTBOUND,
            fast_result_attempts=2,
            readback_delays_seconds=[0, 13],
            sleeper=no_sleep,
        )
        prompts: list[str] = []

        async def confirm(prompt: str) -> bool:
            prompts.append(prompt)
            return True

        result = await adapter(None, _item(), confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == [
            "get_fast_outbound_result",
            "fast_outbound",
            "get_fast_outbound_result",
            "get_fast_outbound_result",
        ]
        package = gateway.calls[1][1][0][0]
        assert package["logistics_type_id"] == "6-825"
        assert package["wid"] == 50
        assert package["waybill_no"] == "1Z9253126709651051"
        assert prompts == [
            (
                "即将发送的快速出库参数：\n"
                "系统单号：103710434633847501\n"
                "仓库物流渠道：UPS（默认线路）\n"
                "国际物流单号：1Z9253126709651051\n"
                "阿里物流单号：ALS01781406025\n"
                "计费重量：4500\n"
                "计费重量单位：g\n"
                "运费：123.45\n"
                "运费币种：CNY\n"
                "请输入 y 确认，其他输入跳过当前订单："
            )
        ]
        assert sleeps == [13]

    asyncio.run(run())


def test_fast_outbound_inconclusive_result_never_resubmits() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        pending = {
            "global_order_no": "103710434633847501",
            "error_message": "正在处理",
            FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
        }
        not_submitted = {
            "global_order_no": "103710434633847501",
            "error_message": "订单没提交快速出库",
            FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_FAILED,
        }
        gateway.fast_results = [[not_submitted], [pending], [pending]]

        async def no_sleep(_seconds: float) -> None:
            return None

        adapter = ApiErpMarkAdapter(
            gateway,  # type: ignore[arg-type]
            {
                "UPS": ErpLogisticsRoute(
                    warehouse_id=50,
                    logistics_type_id=825,
                    fast_logistics_type_id="6-825",
                )
            },
            outbound_strategy=OutboundStrategy.FAST_OUTBOUND,
            fast_result_attempts=2,
            sleeper=no_sleep,
        )

        with pytest.raises(ErpMarkManualReview, match="禁止重复提交"):
            await adapter(None, _item(), _always_confirm)

        assert [name for name, _ in gateway.calls].count("fast_outbound") == 1

    asyncio.run(run())


def test_multiple_wms_rows_are_manual_review_not_a_guessed_write() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[_wms_row(status=1), _wms_row(status=1, suffix="-2")]]
        adapter = _adapter(gateway)

        with pytest.raises(ErpMarkManualReview, match="多个销售出库单"):
            await adapter(None, _item(), _always_confirm)

        assert [name for name, _ in gateway.calls] == ["list_wms_orders"]

    asyncio.run(run())


def test_multiple_wms_rows_use_only_explicit_selection_and_revalidate_saved_choice() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        first = _wms_row(status=1)
        second = _wms_row(status=1, suffix="-2")
        gateway.wms_pages = [[first, second]]
        adapter = _adapter(gateway)
        selections: list[list[str]] = []

        async def choose(candidates: list[dict[str, Any]]) -> str:
            selections.append([str(row["wo_number"]) for row in candidates])
            return "WO-1-2"

        adapter._wms_selection_func = choose
        rows = await adapter._read_wms_rows(_item())

        assert [row["wo_number"] for row in rows] == ["WO-1-2"]
        assert selections == [["WO-1", "WO-1-2"]]

        gateway.wms_pages = [[first]]
        with pytest.raises(ErpMarkManualReview, match="已不存在或不再唯一"):
            await adapter._read_wms_rows(
                _item(selected_wms_wo_number="WO-1-2")
            )

    asyncio.run(run())


def test_cut_off_wms_rows_are_not_offered_as_selectable_candidates() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        cut_off_1 = _wms_row(status=4, suffix="-old-1", status_name="已截单")
        cut_off_2 = _wms_row(status=4, suffix="-old-2", status_name="已截单")
        active = _wms_row(status=1, suffix="-active")
        gateway.wms_pages = [[cut_off_1, active, cut_off_2]]
        adapter = _adapter(gateway)
        selections: list[list[str]] = []

        async def choose(candidates: list[dict[str, Any]]) -> str:
            selections.append([str(row["wo_number"]) for row in candidates])
            return str(candidates[0]["wo_number"])

        adapter._wms_selection_func = choose
        rows = await adapter._read_wms_rows(_item())

        assert [row["wo_number"] for row in rows] == ["WO-1-active"]
        assert selections == []

    asyncio.run(run())


def test_only_cut_off_wms_rows_behave_as_no_current_outbound() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[
            _wms_row(status=4, suffix="-old-1", status_name="已截单"),
            _wms_row(status=4, suffix="-old-2", status_name="已截单"),
        ]]
        adapter = _adapter(gateway)
        selections = 0

        async def choose(_candidates: list[dict[str, Any]]) -> str:
            nonlocal selections
            selections += 1
            return ""

        adapter._wms_selection_func = choose
        rows = await adapter._read_wms_rows(_item())

        assert rows == []
        assert selections == 0

    asyncio.run(run())


def test_cut_off_history_does_not_hide_new_outbound_created_by_review() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        old_1 = _wms_row(status=4, suffix="-old-1", status_name="已截单")
        old_2 = _wms_row(status=4, suffix="-old-2", status_name="已截单")
        active_1 = _wms_row(status=1, suffix="-new")
        active_2 = _wms_row(status=2, tracked=True, suffix="-new")
        active_3 = _wms_row(status=3, tracked=True, suffix="-new")
        gateway.wms_pages = [
            [old_1, old_2],
            [old_1, active_1, old_2],
            [old_1, old_2, active_2],
            [old_1, active_3, old_2],
        ]
        adapter = _adapter(gateway)

        result = await adapter(None, _item(), _always_confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == [
            "list_wms_orders",
            "set_shipping_channel",
            "review_orders",
            "list_wms_orders",
            "set_tracking_no",
            "list_wms_orders",
            "deliver_orders",
            "list_wms_orders",
        ]
        tracking = gateway.calls[4][1]
        assert tracking["wo_number"] == "WO-1-new"

    asyncio.run(run())


def test_unknown_wms_status_stays_fail_closed() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[_wms_row(status=9, suffix="-unknown")]]
        adapter = _adapter(gateway)

        with pytest.raises(ErpMarkManualReview, match="无法安全识别的状态"):
            await adapter._read_wms_rows(_item())

    asyncio.run(run())


def test_staged_rerun_detects_already_outbounded_order_before_any_write() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [[_wms_row(status=3, tracked=True)]]
        adapter = _adapter(gateway)
        confirmations = 0

        async def confirm(_prompt: str) -> bool:
            nonlocal confirmations
            confirmations += 1
            return True

        result = await adapter(None, _item(), confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == ["list_wms_orders"]
        assert confirmations == 0

    asyncio.run(run())


def test_fast_rerun_detects_success_before_submitting_again() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.fast_results = [
            [
                {
                    "global_order_no": "103710434633847501",
                    "wo_number": "WO-1",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED,
                }
            ]
        ]
        adapter = ApiErpMarkAdapter(
            gateway,  # type: ignore[arg-type]
            {
                "UPS": ErpLogisticsRoute(
                    warehouse_id=50,
                    logistics_type_id=825,
                    fast_logistics_type_id="6-825",
                )
            },
            outbound_strategy=OutboundStrategy.FAST_OUTBOUND,
        )

        result = await adapter(None, _item(), _always_confirm)

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == ["get_fast_outbound_result"]

    asyncio.run(run())


def test_resume_from_logistics_checkpoint_only_verifies_then_outbounds() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        gateway.wms_pages = [
            [_wms_row(status=2, tracked=True)],
            [_wms_row(status=2, tracked=True)],
            [_wms_row(status=3, tracked=True)],
        ]
        adapter = _adapter(gateway)
        prompts: list[str] = []

        async def confirm(prompt: str) -> bool:
            prompts.append(prompt)
            return True

        result = await adapter(
            None,
            _item(erp_checkpoint=ERP_CHECKPOINT_LOGISTICS_SAVED),
            confirm,
        )

        assert result == ERP_CHECKPOINT_OUTBOUNDED
        assert [name for name, _ in gateway.calls] == [
            "list_wms_orders",
            "list_wms_orders",
            "deliver_orders",
            "list_wms_orders",
        ]
        assert len(prompts) == 1

    asyncio.run(run())


def test_routes_load_from_json_without_inference() -> None:
    routes = routes_from_configuration(
        {
            "lingxing.erp_mark.routes": """
            {
              "UPS": {
                "warehouse_id": 50,
                "logistics_type_id": 825,
                "fast_logistics_type_id": "6-825",
                "freight_currency_code": "usd"
              }
            }
            """
        }
    )

    assert routes == {
        "UPS": ErpLogisticsRoute(
            warehouse_id=50,
            logistics_type_id=825,
            fast_logistics_type_id="6-825",
            freight_currency_code="USD",
        )
    }
    with pytest.raises(ValueError, match="logistics_type_id"):
        routes_from_configuration(
            {"lingxing.erp_mark.routes": {"UPS": {"warehouse_id": 50}}}
        )


def test_wanb_route_uses_the_verified_lingxing_logistics_type_id() -> None:
    routes = routes_from_configuration(
        {
            "lingxing.erp_mark.routes": {
                "WANB": {
                    "warehouse_id": 7979,
                    "logistics_type_id": 63287,
                    "channel_name": "手动 > 万邦速达",
                }
            }
        }
    )

    route = routes["WANB"]
    assert isinstance(route, ErpLogisticsRoute)
    assert route.warehouse_id == 7979
    assert route.logistics_type_id == 63287
    assert route.channel_name == "手动 > 万邦速达"


@pytest.mark.parametrize(
    ("carrier", "logistics_type_id", "channel_name"),
    [
        ("CANADAPOST", 42492, "手动 > 加拿大邮政"),
        ("ARAMEX", 63924, "手动 > ARAMEX"),
    ],
)
def test_new_routes_use_verified_lingxing_logistics_type_ids(
    carrier,
    logistics_type_id,
    channel_name,
) -> None:
    routes = routes_from_configuration(
        {
            "lingxing.erp_mark.routes": {
                carrier: {
                    "warehouse_id": 7979,
                    "logistics_type_id": logistics_type_id,
                    "channel_name": channel_name,
                }
            }
        }
    )

    route = routes[carrier]
    assert isinstance(route, ErpLogisticsRoute)
    assert route.warehouse_id == 7979
    assert route.logistics_type_id == logistics_type_id
    assert route.channel_name == channel_name


def test_variant_routes_select_full_tail_and_dhl_always_full() -> None:
    routes = routes_from_configuration(
        {
            "lingxing.erp_mark.routes": {
                "UPS": {
                    "full": {
                        "warehouse_id": 7979,
                        "logistics_type_id": 40254,
                        "channel_name": "手动-Alibaba logistics > UPS-全程",
                    },
                    "tail": {
                        "warehouse_id": 7979,
                        "logistics_type_id": 27647,
                        "channel_name": "手动 > UPS-专线尾程",
                    },
                },
                "FEDEX": {
                    "full": {
                        "warehouse_id": 7979,
                        "logistics_type_id": 40255,
                    },
                    "tail": {
                        "warehouse_id": 7979,
                        "logistics_type_id": 27648,
                    },
                },
                "DHL": {
                    "full": {
                        "warehouse_id": 7979,
                        "logistics_type_id": 46255,
                    }
                },
            }
        }
    )
    adapter = ApiErpMarkAdapter(FakeGateway(), routes)  # type: ignore[arg-type]

    full, full_mode = adapter._route_for(_item(service_line="无忧 UPS Saver"))
    tail, tail_mode = adapter._route_for(_item(service_line="普通专线"))
    fedex_full, fedex_full_mode = adapter._route_for(
        _item(carrier="FEDEX", service_line="FedEx-IP")
    )
    fedex_tail, fedex_tail_mode = adapter._route_for(
        _item(carrier="FEDEX", service_line="普通专线")
    )
    dhl, dhl_mode = adapter._route_for(
        _item(carrier="DHL", service_line="普通专线")
    )

    assert (full.logistics_type_id, full_mode) == (40254, "full")
    assert (tail.logistics_type_id, tail_mode) == (27647, "tail")
    assert (fedex_full.logistics_type_id, fedex_full_mode) == (40255, "full")
    assert (fedex_tail.logistics_type_id, fedex_tail_mode) == (27648, "tail")
    assert (dhl.logistics_type_id, dhl_mode) == (46255, "full")
    with pytest.raises(ErpMarkManualReview, match="缺少服务线路"):
        adapter._route_for(_item(service_line=None))
    existing, existing_mode = adapter._route_for(
        _item(
            service_line=None,
            erp_checkpoint=ERP_CHECKPOINT_CHANNEL_SET,
        )
    )
    assert (existing.logistics_type_id, existing_mode) == (40254, "existing")


def test_managed_callback_creates_and_closes_client_per_asyncio_run() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    clients: list[FakeClient] = []
    gateways: list[FakeGateway] = []

    async def gateway_factory():
        gateway = FakeGateway()
        gateway.fast_results = [
            [
                {
                    "global_order_no": "103710434633847501",
                    "wo_number": "WO-1",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED,
                }
            ]
        ]
        client = FakeClient()
        gateways.append(gateway)
        clients.append(client)
        return gateway, client

    configuration = {
        "lingxing.erp_mark.outbound_strategy": "fast_outbound",
        "lingxing.erp_mark.routes": {
            "UPS": {
                "warehouse_id": 50,
                "logistics_type_id": 825,
                "fast_logistics_type_id": "6-825",
            }
        },
    }
    callback = ManagedApiErpMarkFunc(gateway_factory, lambda: configuration)

    first = asyncio.run(callback(None, _item(), _always_confirm))
    second = asyncio.run(callback(None, _item(), _always_confirm))

    assert first == second == ERP_CHECKPOINT_OUTBOUNDED
    assert len(clients) == len(gateways) == 2
    assert all(client.closed for client in clients)
    assert all(
        [name for name, _ in gateway.calls] == ["get_fast_outbound_result"]
        for gateway in gateways
    )


def test_managed_callback_closes_client_when_adapter_blocks() -> None:
    class FakeClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    client = FakeClient()

    async def gateway_factory():
        return FakeGateway(), client

    callback = ManagedApiErpMarkFunc(
        gateway_factory,
        lambda: {"lingxing.erp_mark.routes": {}},
    )

    with pytest.raises(ErpMarkManualReview, match="禁止按名称猜测"):
        asyncio.run(callback(None, _item(), _always_confirm))
    assert client.closed is True


def test_managed_callback_cleanup_error_does_not_turn_success_into_retry() -> None:
    class BadCloseClient:
        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    async def gateway_factory():
        gateway = FakeGateway()
        gateway.fast_results = [
            [
                {
                    "global_order_no": "103710434633847501",
                    "wo_number": "WO-1",
                    FAST_OUTBOUND_RESULT_STATE_KEY: FAST_OUTBOUND_SUCCEEDED,
                }
            ]
        ]
        return gateway, BadCloseClient()

    callback = ManagedApiErpMarkFunc(
        gateway_factory,
        lambda: {
            "lingxing.erp_mark.outbound_strategy": "fast_outbound",
            "lingxing.erp_mark.routes": {
                "UPS": {
                    "warehouse_id": 50,
                    "logistics_type_id": 825,
                    "fast_logistics_type_id": "6-825",
                }
            },
        },
    )

    assert (
        asyncio.run(callback(None, _item(), _always_confirm))
        == ERP_CHECKPOINT_OUTBOUNDED
    )


async def _always_confirm(_prompt: str) -> bool:
    return True
