from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

from erp_automation.application.capabilities import (
    Capability,
    ManualReviewRequired,
    MutationResult,
    MutationState,
)
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.application.lingxing_gateway import (
    LookupRecord,
    MutationVerification,
    OrderDetail,
    OrderPage,
    OrderRecord,
    PageResult,
    VerificationOutcome,
)
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import BatchOrderItem, FolderBuildResult, OrderFolderLine
from lingxing_automation.services.tent_package_split_planner import (
    TentPackageSplitItem,
    TentPackageSplitPackage,
    TentPackageSplitPlan,
)
from lingxing_automation.services.tent_sku_planner import (
    DestinationRegion,
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
    build_tent_sku_plan,
    parse_destination_region,
)
from lingxing_automation.services.tent_sku_adjuster import TentSkuAdjustmentResult
from lingxing_automation.services.tent_warehouse_routing import (
    TentRoutingItem,
    TentRoutingPackage,
)


def _record(global_order_no: str, platform_order_no: str, items: list[dict[str, Any]], **extra: Any) -> OrderRecord:
    payload = {
        "global_order_no": global_order_no,
        "global_latest_ship_time": "1784044800",
        "remark": "",
        "platform_info": [{"platform_order_no": platform_order_no}],
        "item_info": items,
        **extra,
    }
    return OrderRecord(global_order_no, None, payload)


def _page(*records: OrderRecord) -> OrderPage:
    return OrderPage(items=tuple(records), offset=0, length=200, total=len(records))


def _item(
    item_id: str,
    order_item_no: str,
    msku: str,
    local_sku: str,
    quantity: int,
    platform_order_no: str = "111-2222222-3333333",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "order_item_no": order_item_no,
        "msku": msku,
        "local_sku": local_sku,
        "quantity": quantity,
        "platform_order_no": platform_order_no,
    }


def _success(*, data: dict[str, Any] | None = None, request_id: str = "req-1") -> MutationResult:
    return MutationResult(
        state=MutationState.SUCCEEDED,
        source="lingxing_api",
        request_id=request_id,
        details={"data": data or {}},
    )


class FakeGateway:
    def __init__(self, *pages: OrderPage | BaseException) -> None:
        self.pages = deque(pages)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.update_result: MutationResult | BaseException = _success()
        self.split_result: MutationResult | BaseException = _success()
        self.remark_result: MutationResult | BaseException = _success()
        self.phone_result: MutationResult | BaseException = _success()
        self.shipping_result: MutationResult | BaseException = _success()
        self.warehouse_page = PageResult(
            items=(
                LookupRecord("11", "港通 洛杉矶仓", {"t_warehouse_code": "CA", "type": 3}),
                LookupRecord("22", "港通 新泽西仓", {"t_warehouse_code": "NJ", "type": 3}),
            ),
            offset=0,
            length=1000,
            total=2,
        )
        self.logistics_page = PageResult(
            items=(
                LookupRecord(
                    "101",
                    "FedEx Ground Economy",
                    {"wid": 11, "logistics_provider_name": "港通 洛杉矶仓"},
                ),
                LookupRecord(
                    "102",
                    "不限渠道比价",
                    {"wid": 11, "logistics_provider_name": "港通 洛杉矶仓"},
                ),
                LookupRecord(
                    "201",
                    "FedEx Ground Economy",
                    {"wid": 22, "logistics_provider_name": "港通 新泽西仓"},
                ),
                LookupRecord(
                    "202",
                    "不限比价渠道",
                    {"wid": 22, "logistics_provider_name": "港通 新泽西仓"},
                ),
            ),
            offset=0,
            length=1000,
            total=4,
        )

    async def list_orders(self, **kwargs: Any) -> OrderPage:
        self.calls.append(("list_orders", (), kwargs))
        if not self.pages:
            raise AssertionError("unexpected list_orders call")
        page = self.pages.popleft()
        if isinstance(page, BaseException):
            raise page
        return page

    async def update_order_items(self, *args: Any, verify=None, **kwargs: Any) -> MutationResult:
        self.calls.append(("update_order_items", args, kwargs))
        if isinstance(self.update_result, BaseException):
            raise self.update_result
        return await self._verified(self.update_result, verify)

    async def split_order(self, *args: Any, verify=None, **kwargs: Any) -> MutationResult:
        self.calls.append(("split_order", args, kwargs))
        if isinstance(self.split_result, BaseException):
            raise self.split_result
        return await self._verified(self.split_result, verify)

    async def set_order_remark(self, *args: Any, verify=None, **kwargs: Any) -> MutationResult:
        self.calls.append(("set_order_remark", args, kwargs))
        if isinstance(self.remark_result, BaseException):
            raise self.remark_result
        return await self._verified(self.remark_result, verify)

    async def update_phone(self, *args: Any, verify=None, **kwargs: Any) -> MutationResult:
        self.calls.append(("update_phone", args, kwargs))
        if isinstance(self.phone_result, BaseException):
            raise self.phone_result
        return await self._verified(self.phone_result, verify)

    async def list_warehouses(self, **kwargs: Any) -> PageResult[LookupRecord]:
        self.calls.append(("list_warehouses", (), kwargs))
        return self.warehouse_page

    async def list_logistics_types(self, **kwargs: Any) -> PageResult[LookupRecord]:
        self.calls.append(("list_logistics_types", (), kwargs))
        return self.logistics_page

    async def set_shipping_channel(self, *args: Any, **kwargs: Any) -> MutationResult:
        self.calls.append(("set_shipping_channel", args, kwargs))
        if isinstance(self.shipping_result, BaseException):
            raise self.shipping_result
        return self.shipping_result

    @staticmethod
    async def _verified(result: MutationResult, verify) -> MutationResult:
        if verify is None:
            return result
        verification: MutationVerification = await verify(result)
        if verification.outcome is VerificationOutcome.CONFIRMED_APPLIED:
            return MutationResult(
                state=MutationState.SUCCEEDED,
                source=result.source,
                request_id=result.request_id,
                message=verification.message,
                before=verification.before,
                after=verification.after,
                details={
                    **dict(result.details),
                    **dict(verification.details),
                    "verification": verification.outcome.value,
                },
            )
        raise ManualReviewRequired(
            Capability.UPDATE_ORDER_ITEMS,
            verification.message,
            result=MutationResult(
                state=MutationState.UNKNOWN,
                source=result.source,
                request_id=result.request_id,
                message=verification.message,
            ),
        )


def test_order_processing_status_reads_buyer_cancel_system_tag() -> None:
    async def run() -> None:
        platform_order_no = "114-9578255-9785802"
        system_order_no = "103722237001371149"
        gateway = FakeGateway(
            _page(
                _record(
                    system_order_no,
                    platform_order_no,
                    [
                        _item(
                            "item-1",
                            "amazon-item-1",
                            "M1",
                            "L1",
                            1,
                            platform_order_no,
                        )
                    ],
                    status=4,
                    order_tag=[
                        {
                            "tag_type": "系统处理类型",
                            "tag_no": "3-33",
                            "tag_name": "买家申请取消",
                        }
                    ],
                )
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)

        status = await operations.get_order_processing_status(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert status.buyer_cancel_requested is True
        assert status.status_text == "买家申请取消"
        assert gateway.calls[0][2]["filters"] == {
            "platform_order_nos": [platform_order_no]
        }

    asyncio.run(run())


def test_shipping_deadline_reads_only_documented_field_in_china_timezone() -> None:
    async def run() -> None:
        platform_order_no = "114-9578255-9785802"
        system_order_no = "103722237001371149"
        exact = _record(system_order_no, platform_order_no, [])
        aliases_only = _record(
            system_order_no,
            platform_order_no,
            [],
            global_latest_ship_time=None,
            globalLatestShipTime="1784044800",
            latest_ship_time="1784044800",
        )
        operations = LingxingCustomOrderApiOperations(
            FakeGateway(_page(exact), _page(aliases_only))
        )

        assert await operations.get_shipping_deadline_text(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        ) == "2026-07-15 00:00:00"
        assert await operations.get_shipping_deadline_text(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        ) is None

    asyncio.run(run())


def test_order_processing_status_reads_terminal_cancel_without_buyer_request() -> None:
    async def run() -> None:
        platform_order_no = "113-5050103-8817858"
        system_order_no = "103732277893393448"
        gateway = FakeGateway(
            _page(
                _record(
                    system_order_no,
                    platform_order_no,
                    [_item("item-1", "amazon-item-1", "M1", "L1", 1, platform_order_no)],
                    status=7,
                    order_tag=[],
                )
            )
        )

        status = await LingxingCustomOrderApiOperations(gateway).get_order_processing_status(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert status.order_cancelled is True
        assert status.buyer_cancel_requested is False
        assert status.status_text == "订单已取消"

    asyncio.run(run())


def _line(quantity: int = 3) -> OrderFolderLine:
    return OrderFolderLine(
        asin="B0CRRGTPFH",
        sku="TENT-MSKU",
        parent_asin="B0CRRGTPFH",
        product_type="tent",
        quantity=quantity,
        customization_text="3x6m",
        order_item_id="AMAZON-LINE-1",
    )


def _sku_plan(*, quantity: int = 3) -> TentSkuAdjustmentPlan:
    return TentSkuAdjustmentPlan(
        platform_order_no="111-2222222-3333333",
        system_order_no="103000000000000001",
        destination=DestinationRegion(raw_text="California, US", category="us_mainland"),
        replace_main_items=[
            TentSkuPlanAction(
                action="replace_main",
                sku="Roller-Bag-3x6m",
                quantity=quantity,
                source_scope="tent",
                source_sku="TENT-MSKU",
                source_order_item_id="AMAZON-LINE-1",
                source_original_quantity=3,
            )
        ],
        add_items=[TentSkuPlanAction(action="add_product", sku="Sandbag", quantity=2)],
    )


def test_whole_source_row_is_overwritten_without_changing_online_quantity() -> None:
    async def run() -> None:
        before = _page(
            _record(
                "103000000000000001",
                "111-2222222-3333333",
                [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
            )
        )
        after = _page(
            _record(
                "103000000000000001",
                "111-2222222-3333333",
                [
                    _item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Roller-Bag-3x6m", 3),
                    _item("erp-2", "gift-1", "", "Sandbag", 2),
                ],
            )
        )
        gateway = FakeGateway(before, after)
        operations = LingxingCustomOrderApiOperations(
            gateway, verification_attempts=1, verification_delay_seconds=0  # type: ignore[arg-type]
        )

        result = await operations.update_tent_skus(plan=_sku_plan(), order_lines=[_line()])

        assert result.status == "sku_adjustment_complete"
        call = next(call for call in gateway.calls if call[0] == "update_order_items")
        assert call[1][0] == "103000000000000001"
        assert call[1][1] == [
            {"sku": "Roller-Bag-3x6m", "type": 3, "id": "erp-1", "msku": "TENT-MSKU"},
            {
                "sku": "Sandbag",
                "quantity": 2,
                "type": 1,
                "platformOrderNo": "111-2222222-3333333",
            },
        ]
        assert "quantity" not in call[1][1][0]
        assert any("x3" in action for action in result.actions)

    asyncio.run(run())


def test_canada_planner_output_updates_only_its_bound_source_row() -> None:
    async def run() -> None:
        platform = "701-3203414-8305825"
        source_order_item_id = "canada-source-row"
        source_msku = "custom-tent-package-10x10"
        before = _page(
            _record(
                "103723035990804194",
                platform,
                [
                    _item(
                        "erp-canada-1",
                        source_order_item_id,
                        source_msku,
                        "Old-Tent-Sku",
                        1,
                        platform,
                    )
                ],
            )
        )
        after = _page(
            _record(
                "103723035990804194",
                platform,
                [
                    _item(
                        "erp-canada-1",
                        source_order_item_id,
                        source_msku,
                        "10x10-Canopy-Topper",
                        1,
                        platform,
                    )
                ],
            )
        )
        order_line = OrderFolderLine(
            asin="B0DZ2W2QWK",
            sku=source_msku,
            parent_asin="B0FTV6XDGG",
            product_type="tent",
            quantity=1,
            customization_text="",
            order_item_id=source_order_item_id,
        )
        plan = build_tent_sku_plan(
            platform_order_no=platform,
            system_order_no="103723035990804194",
            folder_components=[platform, "1个3x3m帐篷顶", "Edward Publicover"],
            destination_text="Canada, ON, TORONTO",
            asin="B0DZ2W2QWK",
            order_lines=[order_line],
        )
        gateway = FakeGateway(before, after)
        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_attempts=1,
            verification_delay_seconds=0,
        )

        result = await operations.update_tent_skus(
            plan=plan,
            order_lines=[order_line],
        )

        assert result.status == "sku_adjustment_complete"
        call = next(call for call in gateway.calls if call[0] == "update_order_items")
        assert call[1][0] == "103723035990804194"
        assert call[1][1] == [
            {
                "sku": "10x10-Canopy-Topper",
                "type": 3,
                "id": "erp-canada-1",
                "msku": source_msku,
            }
        ]
        assert "quantity" not in call[1][1][0]

    asyncio.run(run())


def test_sku_update_waits_for_delayed_list_projection() -> None:
    async def run() -> None:
        before = _record(
            "103000000000000001",
            "111-2222222-3333333",
            [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
        )
        after = _record(
            "103000000000000001",
            "111-2222222-3333333",
            [
                _item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Roller-Bag-3x6m", 3),
                _item("erp-2", "gift-1", "", "Sandbag", 2),
            ],
        )
        gateway = FakeGateway(_page(before), _page(before), _page(after))
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 5, 10],
            sleeper=sleeper,
        )

        result = await operations.update_tent_skus(
            plan=_sku_plan(),
            order_lines=[_line()],
        )

        assert result.status == "sku_adjustment_complete"
        assert sleeps == [5]

    asyncio.run(run())


def test_partial_source_row_replacement_is_rejected_before_any_api_write() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(
                _record(
                    "103000000000000001",
                    "111-2222222-3333333",
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                )
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        result = await operations.update_tent_skus(
            plan=_sku_plan(quantity=1),
            order_lines=[_line(quantity=3)],
        )

        assert result.status == "sku_adjustment_api_failed"
        assert "整行换货" in (result.error or "")
        assert not any(call[0] == "update_order_items" for call in gateway.calls)

    asyncio.run(run())


def test_unknown_sku_write_requires_manual_review_and_has_no_browser_path() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(
                _record(
                    "103000000000000001",
                    "111-2222222-3333333",
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                )
            )
        )
        unknown = MutationResult(
            state=MutationState.UNKNOWN,
            source="lingxing_api",
            request_id="ambiguous-request",
            message="result unknown",
        )
        gateway.update_result = ManualReviewRequired(
            Capability.UPDATE_ORDER_ITEMS,
            "manual review",
            result=unknown,
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        result = await operations.update_tent_skus(plan=_sku_plan(), order_lines=[_line()])

        assert result.status == "sku_adjustment_manual_review"
        assert "result unknown" in (result.error or "")
        assert result.actions[-1] == "api_manual_review:ambiguous-request"
        assert {call[0] for call in gateway.calls} == {"list_orders", "update_order_items"}

    asyncio.run(run())


def test_phone_update_uses_empty_item_list_and_unknown_is_exposed_for_review() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(
                _record(
                    "103000000000000001",
                    "111-2222222-3333333",
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                )
            )
        )
        gateway.phone_result = ManualReviewRequired(
            Capability.UPDATE_PHONE,
            "manual",
            result=MutationResult(
                state=MutationState.UNKNOWN,
                source="lingxing_api",
                request_id="phone-unknown",
                message="phone result unknown",
            ),
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        outcome = await operations.update_phone(
            platform_order_no="111-2222222-3333333",
            system_order_no="103000000000000001",
            phone="5551234567",
        )

        assert outcome.manual_review_required is True
        assert outcome.request_id == "phone-unknown"
        call = gateway.calls[-1]
        assert call[0] == "update_phone"
        assert call[1] == ("103000000000000001", "5551234567")
        assert call[2]["order_item_list"] == []

    asyncio.run(run())


def test_phone_update_requires_matching_readback_after_api_ack() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        gateway = FakeGateway(
            _page(
                _record(
                    "103000000000000001",
                    platform,
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                    address_info={"receiver_tel": "5550000000"},
                )
            ),
            _page(
                _record(
                    "103000000000000001",
                    platform,
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                    address_info={"receiver_tel": "+1 (555) 123-4567"},
                )
            ),
        )
        operations = LingxingCustomOrderApiOperations(
            gateway, verification_attempts=1, verification_delay_seconds=0  # type: ignore[arg-type]
        )

        outcome = await operations.update_phone(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            phone="15551234567",
        )

        assert outcome.succeeded is True
        assert outcome.details["verification"] == VerificationOutcome.CONFIRMED_APPLIED.value
        assert [call[0] for call in gateway.calls] == [
            "list_orders",
            "update_phone",
            "list_orders",
        ]

    asyncio.run(run())


def test_phone_update_waits_for_delayed_list_projection() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        stale = _record(
            "103000000000000001",
            platform,
            [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
            address_info={"receiver_tel": "5550000000"},
        )
        applied = _record(
            "103000000000000001",
            platform,
            [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
            address_info={"receiver_tel": "+1 (555) 123-4567"},
        )
        gateway = FakeGateway(_page(stale), _page(stale), _page(applied))
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 7, 11],
            sleeper=sleeper,
        )

        outcome = await operations.update_phone(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            phone="15551234567",
        )

        assert outcome.succeeded is True
        assert outcome.details["readback_attempt"] == 2
        assert outcome.details["readback_waited_seconds"] == 7
        assert sleeps == [7]

    asyncio.run(run())


def test_phone_readback_retries_a_transient_query_failure_without_rewriting() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        before = _record(
            "103000000000000001",
            platform,
            [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
            address_info={"receiver_tel": "5550000000"},
        )
        applied = _record(
            "103000000000000001",
            platform,
            [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
            address_info={"receiver_tel": "15551234567"},
        )
        gateway = FakeGateway(
            _page(before),
            TimeoutError("temporary readback timeout"),
            _page(applied),
        )
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 5, 10],
            sleeper=sleeper,
        )

        outcome = await operations.update_phone(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            phone="15551234567",
        )

        assert outcome.succeeded is True
        assert outcome.details["readback_attempt"] == 2
        assert outcome.details["readback_transient_error_count"] == 1
        assert outcome.details["readback_last_error_type"] == "TimeoutError"
        assert sleeps == [5]
        assert [call[0] for call in gateway.calls].count("update_phone") == 1

    asyncio.run(run())


def test_phone_update_is_noop_when_read_snapshot_already_has_target_value() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(
                _record(
                    "103000000000000001",
                    "111-2222222-3333333",
                    [_item("erp-1", "AMAZON-LINE-1", "TENT-MSKU", "Old-Tent-Sku", 3)],
                    address_info={"receiver_tel": "+1 (555) 123-4567"},
                )
            )
        )
        operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

        outcome = await operations.update_phone(
            platform_order_no="111-2222222-3333333",
            system_order_no="103000000000000001",
            phone="15551234567",
        )

        assert outcome.succeeded is True
        assert outcome.details["no_op"] is True
        assert [call[0] for call in gateway.calls] == ["list_orders"]

    asyncio.run(run())


def test_split_conserves_every_quantity_and_returns_verified_system_orders() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        before = _page(
            _record(
                "103000000000000001",
                platform,
                [
                    _item("instruction", "line-1", "", "Instruction", 1),
                    _item("frame", "line-2", "", "Tent-Frame", 1),
                    _item("fabric", "line-3", "", "Tent-Top", 2),
                ],
            )
        )
        after = _page(
            _record("103000000000000001", platform, [_item("fabric", "line-3", "", "Tent-Top", 2)]),
            _record("103000000000000002", platform, [_item("instruction", "line-1", "", "Instruction", 1)]),
            _record("103000000000000003", platform, [_item("frame", "line-2", "", "Tent-Frame", 1)]),
        )
        gateway = FakeGateway(before, after)
        gateway.split_result = _success(
            data={
                "result": [
                    {"global_order_no": "103000000000000001"},
                    {"global_order_no": "103000000000000002"},
                    {"global_order_no": "103000000000000003"},
                ]
            },
            request_id="split-request",
        )
        operations = LingxingCustomOrderApiOperations(
            gateway, verification_attempts=1, verification_delay_seconds=0  # type: ignore[arg-type]
        )
        plan = TentPackageSplitPlan(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            destination=DestinationRegion(raw_text="US", category="us_mainland"),
            status="ready",
            required=True,
            packages_to_split=[
                TentPackageSplitPackage(
                    package_key="accessory",
                    title="accessory",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
                TentPackageSplitPackage(
                    package_key="frame",
                    title="frame",
                    items=[TentPackageSplitItem(sku="Tent-Frame", quantity=1)],
                ),
            ],
        )

        result = await operations.split_tent_packages(plan=plan)

        assert result.status == "package_split_complete"
        assert result.system_order_nos == [
            "103000000000000001",
            "103000000000000002",
            "103000000000000003",
        ]
        assert result.instruction_system_order_no == "103000000000000002"
        assert result.request_id == "split-request"
        assert result.response_validation["status"] == "complete"
        assert result.response_validation["post_write_readback"] == "skipped"
        assert [
            (
                package.system_order_no,
                [(item.sku, item.quantity) for item in package.items],
            )
            for package in result.projected_routing_packages
        ] == [
            ("103000000000000001", [("Tent-Top", 2)]),
            ("103000000000000002", [("Instruction", 1)]),
            ("103000000000000003", [("Tent-Frame", 1)]),
        ]
        assert [call[0] for call in gateway.calls].count("list_orders") == 1
        call = next(call for call in gateway.calls if call[0] == "split_order")
        assert call[1][1] == [
            [{"item_id": "fabric", "quantity": 2}],
            [{"item_id": "instruction", "quantity": 1}],
            [{"item_id": "frame", "quantity": 1}],
        ]
        assert sum(item["quantity"] for group in call[1][1] for item in group) == 4

    asyncio.run(run())


def test_split_complete_ack_skips_delayed_child_order_projection() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        unsplit = _record(
            "103000000000000001",
            platform,
            [
                _item("instruction", "line-1", "", "Instruction", 1),
                _item("frame", "line-2", "", "Tent-Frame", 1),
                _item("fabric", "line-3", "", "Tent-Top", 2),
            ],
        )
        split_rows = _page(
            _record("103000000000000001", platform, [_item("fabric", "line-3", "", "Tent-Top", 2)]),
            _record("103000000000000002", platform, [_item("instruction", "line-1", "", "Instruction", 1)]),
            _record("103000000000000003", platform, [_item("frame", "line-2", "", "Tent-Frame", 1)]),
        )
        gateway = FakeGateway(_page(unsplit), _page(unsplit), split_rows)
        gateway.split_result = _success(
            data={
                "result": [
                    {"global_order_no": "103000000000000001"},
                    {"global_order_no": "103000000000000002"},
                    {"global_order_no": "103000000000000003"},
                ]
            }
        )
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 30, 60],
            sleeper=sleeper,
        )
        plan = TentPackageSplitPlan(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            destination=DestinationRegion(raw_text="US", category="us_mainland"),
            status="ready",
            required=True,
            packages_to_split=[
                TentPackageSplitPackage(
                    package_key="accessory",
                    title="accessory",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
                TentPackageSplitPackage(
                    package_key="frame",
                    title="frame",
                    items=[TentPackageSplitItem(sku="Tent-Frame", quantity=1)],
                ),
            ],
        )

        result = await operations.split_tent_packages(plan=plan)

        assert result.status == "package_split_complete"
        assert result.system_order_nos == [
            "103000000000000001",
            "103000000000000002",
            "103000000000000003",
        ]
        assert sleeps == []
        assert [call[0] for call in gateway.calls].count("list_orders") == 1
        assert len(gateway.pages) == 2

    asyncio.run(run())


def test_split_partial_ack_requires_review_without_readback_or_resubmit() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        unsplit = _record(
            "103000000000000001",
            platform,
            [
                _item("instruction", "line-1", "", "Instruction", 1),
                _item("frame", "line-2", "", "Tent-Frame", 1),
                _item("fabric", "line-3", "", "Tent-Top", 2),
            ],
        )
        split_rows = _page(
            _record("103000000000000001", platform, [_item("fabric", "line-3", "", "Tent-Top", 2)]),
            _record("103000000000000002", platform, [_item("instruction", "line-1", "", "Instruction", 1)]),
            _record("103000000000000003", platform, [_item("frame", "line-2", "", "Tent-Frame", 1)]),
        )
        gateway = FakeGateway(_page(unsplit), split_rows)
        gateway.split_result = _success(
            data={"result": [{"global_order_no": "103000000000000001"}]}
        )
        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_attempts=1,
            verification_delay_seconds=0,
        )
        plan = TentPackageSplitPlan(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            destination=DestinationRegion(raw_text="US", category="us_mainland"),
            status="ready",
            required=True,
            packages_to_split=[
                TentPackageSplitPackage(
                    package_key="accessory",
                    title="accessory",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
                TentPackageSplitPackage(
                    package_key="frame",
                    title="frame",
                    items=[TentPackageSplitItem(sku="Tent-Frame", quantity=1)],
                ),
            ],
        )

        result = await operations.split_tent_packages(plan=plan)

        assert result.status == "package_split_manual_review"
        assert result.system_order_nos == []
        assert result.fallback_eligible is False
        assert result.response_validation["reason"] == "result_count_mismatch"
        assert [call[0] for call in gateway.calls].count("split_order") == 1
        assert [call[0] for call in gateway.calls].count("list_orders") == 1
        assert len(gateway.pages) == 1

    asyncio.run(run())


def test_ambiguous_split_is_not_confirmed_by_unrelated_historical_package_rows() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        unsplit_items = [
            _item("instruction", "line-1", "", "Instruction", 1),
            _item("frame", "line-2", "", "Tent-Frame", 1),
            _item("fabric", "line-3", "", "Tent-Top", 2),
        ]
        before = _page(_record("103000000000000001", platform, unsplit_items))
        misleading_after = _page(
            _record("103000000000000001", platform, unsplit_items),
            _record("old-2", platform, [_item("old-a", "old-a", "", "Instruction", 1)]),
            _record("old-3", platform, [_item("old-b", "old-b", "", "Tent-Frame", 1)]),
            _record("old-4", platform, [_item("old-c", "old-c", "", "Tent-Top", 2)]),
        )
        gateway = FakeGateway(before, misleading_after)
        operations = LingxingCustomOrderApiOperations(
            gateway, verification_attempts=1, verification_delay_seconds=0  # type: ignore[arg-type]
        )
        plan = TentPackageSplitPlan(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            destination=DestinationRegion(raw_text="US", category="us_mainland"),
            status="ready",
            required=True,
            packages_to_split=[
                TentPackageSplitPackage(
                    package_key="accessory",
                    title="accessory",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
                TentPackageSplitPackage(
                    package_key="frame",
                    title="frame",
                    items=[TentPackageSplitItem(sku="Tent-Frame", quantity=1)],
                ),
            ],
        )

        result = await operations.split_tent_packages(plan=plan)

        assert result.status == "package_split_manual_review"
        assert "禁止自动重发或网页回退" in (result.error or "")
        assert [call[0] for call in gateway.calls].count("list_orders") == 1

    asyncio.run(run())


def test_split_ack_rejects_duplicates_and_missing_original_order_no() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        before = _page(
            _record(
                "103000000000000001",
                platform,
                [
                    _item("instruction", "line-1", "", "Instruction", 1),
                    _item("frame", "line-2", "", "Tent-Frame", 1),
                    _item("fabric", "line-3", "", "Tent-Top", 2),
                ],
            )
        )
        plan = TentPackageSplitPlan(
            platform_order_no=platform,
            system_order_no="103000000000000001",
            destination=DestinationRegion(raw_text="US", category="us_mainland"),
            status="ready",
            required=True,
            packages_to_split=[
                TentPackageSplitPackage(
                    package_key="accessory",
                    title="accessory",
                    items=[TentPackageSplitItem(sku="Instruction", quantity=1)],
                ),
                TentPackageSplitPackage(
                    package_key="frame",
                    title="frame",
                    items=[TentPackageSplitItem(sku="Tent-Frame", quantity=1)],
                ),
            ],
        )
        cases = [
            (
                [
                    {"global_order_no": "103000000000000001"},
                    {"global_order_no": "103000000000000002"},
                    {"global_order_no": "103000000000000002"},
                ],
                "duplicate_global_order_no",
            ),
            (
                [
                    {"global_order_no": "103000000000000002"},
                    {"global_order_no": "103000000000000003"},
                    {"global_order_no": "103000000000000004"},
                ],
                "original_global_order_no_missing",
            ),
        ]
        for rows, expected_reason in cases:
            gateway = FakeGateway(before)
            gateway.split_result = _success(data={"result": rows})
            operations = LingxingCustomOrderApiOperations(gateway)  # type: ignore[arg-type]

            result = await operations.split_tent_packages(plan=plan)

            assert result.status == "package_split_manual_review"
            assert result.response_validation["reason"] == expected_reason
            assert [call[0] for call in gateway.calls].count("split_order") == 1
            assert [call[0] for call in gateway.calls].count("list_orders") == 1

    asyncio.run(run())


def test_instruction_remark_targets_only_the_split_order_containing_instruction() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        before_rows = _page(
            _record(
                "103000000000000001",
                platform,
                [_item("fabric", "line-1", "", "Tent-Top", 1)],
                remark="keep this",
            ),
            _record(
                "103000000000000002",
                platform,
                [_item("instruction", "line-2", "", "Instruction", 1)],
                remark="existing note",
            ),
        )
        after_target = _page(
            _record(
                "103000000000000002",
                platform,
                [_item("instruction", "line-2", "", "Instruction", 1)],
                remark="7.20发说明书\nexisting note",
            )
        )
        gateway = FakeGateway(before_rows, after_target)
        operations = LingxingCustomOrderApiOperations(
            gateway, verification_attempts=1, verification_delay_seconds=0  # type: ignore[arg-type]
        )

        outcome = await operations.set_instruction_remark(
            platform_order_no=platform,
            candidate_system_order_nos=["103000000000000001", "103000000000000002"],
            remark="7.20发说明书",
            target_system_order_no="103000000000000002",
        )

        assert outcome.succeeded is True
        assert outcome.target_system_order_no == "103000000000000002"
        assert outcome.action == "append"
        call = next(call for call in gateway.calls if call[0] == "set_order_remark")
        assert call[1] == ("103000000000000002", "7.20发说明书\nexisting note")
        assert call[2]["append"] is False

    asyncio.run(run())


def test_instruction_remark_waits_for_delayed_list_projection() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        target_before = _record(
            "103000000000000002",
            platform,
            [_item("instruction", "line-2", "", "Instruction", 1)],
            remark="existing note",
        )
        target_after = _record(
            "103000000000000002",
            platform,
            [_item("instruction", "line-2", "", "Instruction", 1)],
            remark="7.20发说明书\nexisting note",
        )
        gateway = FakeGateway(
            _page(target_before),
            _page(target_before),
            _page(target_after),
        )
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 9, 20],
            sleeper=sleeper,
        )

        outcome = await operations.set_instruction_remark(
            platform_order_no=platform,
            candidate_system_order_nos=["103000000000000002"],
            remark="7.20发说明书",
        )

        assert outcome.succeeded is True
        assert sleeps == [9]

    asyncio.run(run())


def test_instruction_remark_waits_for_new_split_target_before_write() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        original = _record(
            "103000000000000001",
            platform,
            [_item("fabric", "line-1", "", "Tent-Top", 1)],
        )
        target_before = _record(
            "103000000000000002",
            platform,
            [_item("instruction", "line-2", "", "Instruction", 1)],
            remark="existing note",
        )
        target_after = _record(
            "103000000000000002",
            platform,
            [_item("instruction", "line-2", "", "Instruction", 1)],
            remark="7.20发说明书\nexisting note",
        )
        gateway = FakeGateway(
            _page(original),
            _page(original, target_before),
            _page(original, target_after),
        )
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 9, 20],
            sleeper=sleeper,
        )

        outcome = await operations.set_instruction_remark(
            platform_order_no=platform,
            candidate_system_order_nos=[
                "103000000000000001",
                "103000000000000002",
            ],
            remark="7.20发说明书",
            target_system_order_no="103000000000000002",
        )

        assert outcome.succeeded is True
        assert sleeps == [9]
        assert [call[0] for call in gateway.calls].count("set_order_remark") == 1

    asyncio.run(run())


def test_instruction_remark_never_writes_when_split_target_stays_hidden() -> None:
    async def run() -> None:
        platform = "111-2222222-3333333"
        original = _record(
            "103000000000000001",
            platform,
            [_item("fabric", "line-1", "", "Tent-Top", 1)],
        )
        gateway = FakeGateway(_page(original), _page(original), _page(original))
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0, 9, 20],
            sleeper=sleeper,
        )

        outcome = await operations.set_instruction_remark(
            platform_order_no=platform,
            candidate_system_order_nos=[
                "103000000000000001",
                "103000000000000002",
            ],
            remark="7.20发说明书",
            target_system_order_no="103000000000000002",
        )

        assert outcome.succeeded is False
        assert "无法唯一定位系统单号 103000000000000002" in outcome.message
        assert sleeps == [9, 20]
        assert [call[0] for call in gateway.calls].count("set_order_remark") == 0

    asyncio.run(run())


def test_custom_order_stage_uses_injected_api_for_deadline_and_sku_write(monkeypatch) -> None:
    class StageOperations:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_shipping_deadline_text(self, **_kwargs: Any) -> str:
            self.calls.append("api_deadline")
            return "2026-07-20 14:59:59"

        async def update_tent_skus(self, **_kwargs: Any) -> TentSkuAdjustmentResult:
            self.calls.append("api_sku_write")
            return TentSkuAdjustmentResult(status="sku_adjustment_complete", actions=["api"])

    operations = StageOperations()

    async def fake_close(_page: object) -> None:
        return None

    async def approve(_plan: object) -> bool:
        return True

    async def browser_write(*_args: Any, **_kwargs: Any) -> TentSkuAdjustmentResult:
        raise AssertionError("API-covered SKU write must not use the browser")

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "confirm_tent_sku_plan_in_cmd", approve)
    monkeypatch.setattr(contact_sync, "execute_tent_sku_adjustment", browser_write)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", lambda **_kwargs: _sku_plan())

    result = asyncio.run(
        contact_sync.run_tent_sku_adjustment_stage(
            object(),
            BatchOrderItem(
                system_order_no="103000000000000001",
                platform_order_no="111-2222222-3333333",
                row_text="",
            ),
            "103000000000000001",
            FolderBuildResult(status="folder_preview", folder_components=["3x6m tent"]),
            [_line()],
            shipping_address_text="California, US",
            dedupe_path=None,
            write_dedupe=False,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=operations,  # type: ignore[arg-type]
        )
    )

    assert result["sku_adjustment_complete"] is True
    assert result["sku_adjustment_write_source"] == "lingxing_api"
    assert result["shipping_deadline_text"] == "2026-07-20 14:59:59"
    assert operations.calls == ["api_deadline", "api_sku_write"]


def test_sku_stage_never_uses_browser_after_api_rejection(monkeypatch) -> None:
    class StageOperations:
        async def get_shipping_deadline_text(self, **_kwargs: Any) -> str:
            return "2026-07-20 14:59:59"

        async def update_tent_skus(self, **_kwargs: Any) -> TentSkuAdjustmentResult:
            return TentSkuAdjustmentResult(
                status="sku_adjustment_api_failed",
                error="API 在执行前拒绝",
                fallback_eligible=True,
                request_id="request-rejected",
            )

    calls: list[str] = []

    async def fake_close(_page: object) -> None:
        return None

    async def approve_plan(_plan: object) -> bool:
        return True

    async def runtime_guard(stage: str, _platform: str, _system: str) -> bool:
        calls.append(f"guard:{stage}")
        return True

    async def approve_fallback(operation: str, error: str, is_write: bool) -> bool:
        calls.append(f"fallback:{operation}:{is_write}:{error}")
        return True

    async def browser_write(*_args: Any, **_kwargs: Any) -> TentSkuAdjustmentResult:
        calls.append("browser_write")
        return TentSkuAdjustmentResult(status="sku_adjustment_complete", actions=["browser"])

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", fake_close)
    monkeypatch.setattr(contact_sync, "execute_tent_sku_adjustment", browser_write)
    monkeypatch.setattr(contact_sync, "build_tent_sku_plan", lambda **_kwargs: _sku_plan())
    policy = SimpleNamespace(
        confirm_sku_plan=approve_plan,
        runtime_write_guard=runtime_guard,
        confirm_browser_fallback=approve_fallback,
    )

    result = asyncio.run(
        contact_sync.run_tent_sku_adjustment_stage(
            object(),
            BatchOrderItem(
                system_order_no="103000000000000001",
                platform_order_no="111-2222222-3333333",
                row_text="",
            ),
            "103000000000000001",
            FolderBuildResult(status="folder_preview", folder_components=["3x6m tent"]),
            [_line()],
            shipping_address_text="California, US",
            dedupe_path=None,
            write_dedupe=False,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=StageOperations(),  # type: ignore[arg-type]
            interaction_policy=policy,  # type: ignore[arg-type]
        )
    )

    assert result["sku_adjustment_complete"] is False
    assert result["sku_adjustment_status"] == "sku_adjustment_api_failed"
    assert result["sku_adjustment_write_source"] == "lingxing_api"
    assert calls == ["guard:sku_adjustment"]


def _warehouse_sku_plan(postal_code: str = "11725") -> TentSkuAdjustmentPlan:
    return TentSkuAdjustmentPlan(
        platform_order_no="111-2222222-3333333",
        system_order_no="103000000000000001",
        destination=DestinationRegion(
            raw_text="United States of America, NY, COMMACK, 11725",
            country="US",
            state="NY",
            postal_code=postal_code,
            category="us_mainland",
        ),
        replace_main_items=[
            TentSkuPlanAction(
                action="replace_main",
                sku="10X10-FRAME-40MM-SQUARE",
                quantity=1,
                source_order_item_id="AMAZON-LINE-1",
                source_original_quantity=1,
            )
        ],
    )


def _warehouse_order(*, sys_wid: int = 0, logistics_type_id: int = 0) -> OrderRecord:
    return _record(
        "103000000000000001",
        "111-2222222-3333333",
        [
            _item(
                "erp-1",
                "AMAZON-LINE-1",
                "TENT-MSKU",
                "10X10-FRAME-40MM-SQUARE",
                1,
            )
        ],
        logistics={"sys_wid": sys_wid, "logistics_type_id": logistics_type_id},
    )


def test_warehouse_logistics_preview_outputs_plan_without_lookup_or_write() -> None:
    async def run() -> None:
        gateway = FakeGateway(_page(_warehouse_order()))
        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0],
        )

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=False,
        )

        assert outcome.status == "preview"
        assert outcome.plan is not None
        assert outcome.plan.decisions[0].target_warehouse_code == "NJ"
        assert outcome.plan.decisions[0].target_channel_name == "港通 新泽西仓-FedEx Ground Economy"
        assert [call[0] for call in gateway.calls] == ["list_orders"]

    asyncio.run(run())


def test_projected_warehouse_preview_is_immediate_and_does_not_read_order_list() -> None:
    async def run() -> None:
        gateway = FakeGateway()
        operations = LingxingCustomOrderApiOperations(gateway)

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=False,
            projected_packages=(
                TentRoutingPackage(
                    system_order_no="103000000000000001",
                    items=(
                        TentRoutingItem(
                            sku="10X10-FRAME-40MM-SQUARE",
                            quantity=1,
                            item_id="erp-1",
                            order_item_no="AMAZON-LINE-1",
                        ),
                    ),
                ),
            ),
        )

        assert outcome.status == "preview"
        assert outcome.plan is not None
        assert outcome.details["projection_source"] == "split_ack"
        assert outcome.details["projection_attempts"] == 0
        assert gateway.calls == []

    asyncio.run(run())


def test_get_order_context_uses_api_detail_for_amount_recipient_and_destination() -> None:
    async def run() -> None:
        platform_order_no = "112-2749063-2058610"
        system_order_no = "103732067724812343"
        list_record = _record(
            system_order_no,
            platform_order_no,
            [
                {
                    **_item(
                        "item-1",
                        "amazon-item-1",
                        'MAGNET-12x24',
                        'BillyPrint-Car Magnet-12"x24"-2',
                        1,
                        platform_order_no,
                    ),
                    "product_no": "B0CQLN5GNL",
                }
            ],
            status=4,
            order_tag=[],
            customer_shipping_list=["Standard"],
            logistics_info={"logistics_type_name": "UPS-全程"},
        )

        class ContextGateway(FakeGateway):
            async def get_order_detail(self, order_number: str) -> OrderDetail:
                self.calls.append(("get_order_detail", (order_number,), {}))
                return OrderDetail(
                    order_number=order_number,
                    request_id="detail-context",
                    payload={
                        "global_order_no": system_order_no,
                        "order_price_amount": "207.21",
                        "buyer_choose_express": "Expedited",
                        "platform_info": [
                            {"platform_order_no": platform_order_no}
                        ],
                        "order_item": [
                            {
                                **_item(
                                    "item-1",
                                    "amazon-item-1",
                                    'MAGNET-12x24',
                                    'BillyPrint-Car Magnet-12"x24"-2',
                                    1,
                                    platform_order_no,
                                ),
                                "product_no": "B0CQLN5GNL",
                                "currency_code": "USD",
                            }
                        ],
                        "receive_info": {
                            "receiver_name": "API Buyer",
                            # Real detail responses can put both the ISO code
                            # and display name in the same mapping, with the
                            # code appearing first in JSON order.
                            "receiver_country_code": "US",
                            "receiver_country": "US",
                            "receiver_country_name": "United States of America (USA)(美国)",
                            "state_or_region": "VA",
                            "city": "Richmond",
                            "address_line1": "123 Main Street",
                            "postal_code": "23234-5181",
                        },
                    },
                )

        operations = LingxingCustomOrderApiOperations(
            ContextGateway(_page(list_record))
        )
        context = await operations.get_order_context(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert context.item.system_order_no == system_order_no
        assert context.item.product_type == "car_magnet"
        assert context.item.sales_revenue_total == "207.21"
        assert context.item.sales_revenue_currency == "USD"
        assert context.item.sales_revenue_status == "complete"
        assert context.item.sales_revenue_source == "order_total"
        assert context.item.logistics == "UPS-全程"
        assert context.item.customer_shipping_service == "Expedited"
        assert context.recipient_name == "API Buyer"
        assert context.recipient_name_raw == "API Buyer"
        assert context.recipient_name_source == "lingxing_openapi"
        assert context.shipping_postal_code == "23234"
        assert "United States of America" in context.shipping_address_text
        assert "Richmond" in context.shipping_address_text
        destination = parse_destination_region(context.shipping_address_text)
        assert destination.country == "US"
        assert destination.state == "VA"
        assert destination.category == "us_mainland"
        assert context.request_ids == ("detail-context",)

    asyncio.run(run())


def test_get_order_context_reads_detail_shipping_service_without_detail_items() -> None:
    async def run() -> None:
        platform_order_no = "113-5083192-3170664"
        system_order_no = "103734344485759537"
        list_record = _record(
            system_order_no,
            platform_order_no,
            [
                {
                    **_item(
                        "item-1",
                        "amazon-item-1",
                        "TABLECLOTH",
                        "Tablecloth-Spandex-6ft",
                        5,
                        platform_order_no,
                    ),
                    "product_no": "B0DBGBDHL7",
                }
            ],
            customer_shipping_list=["Standard"],
            logistics_info={"logistics_type_name": "UPS-全程"},
        )

        class ContextGateway(FakeGateway):
            async def get_order_detail(self, order_number: str) -> OrderDetail:
                return OrderDetail(
                    order_number=order_number,
                    payload={
                        "global_order_no": system_order_no,
                        "buyer_choose_express": "Expedited",
                        "platform_info": [
                            {
                                "platform_order_no": "111-0000000-0000000",
                            },
                            {
                                "platform_order_no": platform_order_no,
                            },
                        ],
                        "receive_info": {
                            "receiver_name": "API Buyer",
                            "receiver_country_code": "US",
                        },
                    },
                )

        context = await LingxingCustomOrderApiOperations(
            ContextGateway(_page(list_record))
        ).get_order_context(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert context.item.logistics == "UPS-全程"
        assert context.item.customer_shipping_service == "Expedited"

    asyncio.run(run())


def test_get_order_context_marks_dash_recipient_as_missing_placeholder() -> None:
    async def run() -> None:
        platform_order_no = "112-2749063-2058610"
        system_order_no = "103732067724812343"
        list_record = _record(
            system_order_no,
            platform_order_no,
            [_item("item-1", "amazon-item-1", "M1", "L1", 1, platform_order_no)],
        )

        class ContextGateway(FakeGateway):
            async def get_order_detail(self, order_number: str) -> OrderDetail:
                return OrderDetail(
                    order_number=order_number,
                    payload={
                        "global_order_no": system_order_no,
                        "platform_info": [{"platform_order_no": platform_order_no}],
                        "order_item": [
                            {
                                **_item(
                                    "item-1",
                                    "amazon-item-1",
                                    "M1",
                                    "L1",
                                    1,
                                    platform_order_no,
                                ),
                                "product_no": "B0CRRGTPFH",
                            }
                        ],
                        "receive_info": {"receiver_name": "-"},
                    },
                )

        context = await LingxingCustomOrderApiOperations(
            ContextGateway(_page(list_record))
        ).get_order_context(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert context.recipient_name is None
        assert context.recipient_name_raw == "-"
        assert (
            context.recipient_name_source
            == "lingxing_openapi_placeholder_or_missing"
        )

    asyncio.run(run())


def test_get_order_context_merges_incomplete_detail_address_with_root_country() -> None:
    async def run() -> None:
        platform_order_no = "111-9376959-0968245"
        system_order_no = "103732377639436478"
        list_record = _record(
            system_order_no,
            platform_order_no,
            [_item("item-1", "amazon-item-1", "M1", "L1", 1, platform_order_no)],
            receiver_country_code="US",
            receiver_state="WA",
            city="Seattle",
            postal_code="98168-1303",
        )

        class ContextGateway(FakeGateway):
            async def get_order_detail(self, order_number: str) -> OrderDetail:
                return OrderDetail(
                    order_number=order_number,
                    request_id="detail-incomplete-address",
                    payload={
                        "global_order_no": system_order_no,
                        "platform_info": [{"platform_order_no": platform_order_no}],
                        "order_item": [
                            {
                                **_item("item-1", "amazon-item-1", "M1", "L1", 1, platform_order_no),
                                "product_no": "B0CRRGTPFH",
                            }
                        ],
                        "receive_info": {"receiver_name": "Seattle Buyer"},
                    },
                )

        context = await LingxingCustomOrderApiOperations(
            ContextGateway(_page(list_record))
        ).get_order_context(
            platform_order_no=platform_order_no,
            system_order_no=system_order_no,
        )

        assert context.recipient_name == "Seattle Buyer"
        assert "US" in context.shipping_address_text
        assert "Seattle" in context.shipping_address_text
        assert context.shipping_postal_code == "98168"

    asyncio.run(run())


def test_warehouse_projection_uses_short_adaptive_polling_before_success() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)

        gateway = FakeGateway(_page(), _page(_warehouse_order()))
        operations = LingxingCustomOrderApiOperations(
            gateway,
            warehouse_projection_delays_seconds=[0, 3, 3],
            sleeper=sleeper,
        )

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=False,
        )

        assert outcome.status == "preview"
        assert outcome.details["projection_source"] == "order_list"
        assert outcome.details["projection_attempts"] == 2
        assert outcome.details["projection_waited_seconds"] == 3
        assert sleeps == [3]

    asyncio.run(run())


def test_warehouse_logistics_resolves_exact_ids_writes_and_reads_back() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(_warehouse_order()),
            _page(_warehouse_order(sys_wid=22, logistics_type_id=201)),
        )
        operations = LingxingCustomOrderApiOperations(
            gateway,
            verification_delays_seconds=[0],
        )

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "succeeded"
        warehouse_call = next(call for call in gateway.calls if call[0] == "list_warehouses")
        assert warehouse_call[2]["warehouse_type"] == 3
        logistics_call = next(call for call in gateway.calls if call[0] == "list_logistics_types")
        assert logistics_call[2]["provider_type"] == 2
        shipping_call = next(call for call in gateway.calls if call[0] == "set_shipping_channel")
        assert shipping_call[1][0] == [
            {
                "global_order_no": "103000000000000001",
                "logistics": {"logistics_type_id": 201, "sys_wid": 22},
            }
        ]
        assert outcome.details["writes"][0]["status"] == "verified"

    asyncio.run(run())


def test_warehouse_logistics_duplicate_warehouse_is_manual_without_write() -> None:
    async def run() -> None:
        gateway = FakeGateway(_page(_warehouse_order()))
        duplicate = LookupRecord(
            "999",
            "港通 新泽西仓",
            {"t_warehouse_code": "NJ", "type": 3},
        )
        gateway.warehouse_page = PageResult(
            items=(*gateway.warehouse_page.items, duplicate),
            offset=0,
            length=1000,
            total=3,
        )
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "manual_review"
        assert "唯一匹配" in outcome.message
        assert not any(call[0] == "set_shipping_channel" for call in gateway.calls)

    asyncio.run(run())


def test_warehouse_logistics_is_idempotent_when_target_is_already_applied() -> None:
    async def run() -> None:
        gateway = FakeGateway(_page(_warehouse_order(sys_wid=22, logistics_type_id=201)))
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "succeeded"
        assert outcome.details["writes"][0]["status"] == "already_applied"
        assert not any(call[0] == "set_shipping_channel" for call in gateway.calls)

    asyncio.run(run())


def test_warehouse_logistics_existing_non_default_route_is_overwritten_and_verified() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(_warehouse_order(sys_wid=11, logistics_type_id=101)),
            _page(_warehouse_order(sys_wid=22, logistics_type_id=201)),
        )
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "succeeded"
        shipping_call = next(call for call in gateway.calls if call[0] == "set_shipping_channel")
        assert shipping_call[1][0] == [
            {
                "global_order_no": "103000000000000001",
                "logistics": {"logistics_type_id": 201, "sys_wid": 22},
            }
        ]
        assert outcome.details["writes"][0]["overwriting_existing_route"] is True
        assert outcome.details["writes"][0]["status"] == "verified"

    asyncio.run(run())


def test_warehouse_logistics_duplicate_channel_is_manual_without_write() -> None:
    async def run() -> None:
        gateway = FakeGateway(_page(_warehouse_order()))
        duplicate = LookupRecord(
            "999",
            "FedEx Ground Economy",
            {"wid": 22, "logistics_provider_name": "港通 新泽西仓"},
        )
        gateway.logistics_page = PageResult(
            items=(*gateway.logistics_page.items, duplicate),
            offset=0,
            length=1000,
            total=5,
        )
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "manual_review"
        assert "唯一匹配" in outcome.message
        assert not any(call[0] == "set_shipping_channel" for call in gateway.calls)

    asyncio.run(run())


def test_warehouse_logistics_ambiguous_write_is_not_replayed() -> None:
    async def run() -> None:
        gateway = FakeGateway(_page(_warehouse_order()))
        gateway.shipping_result = MutationResult(
            state=MutationState.UNKNOWN,
            source="lingxing_api",
            request_id="ambiguous-1",
            message="transport timeout",
        )
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "manual_review"
        assert len([call for call in gateway.calls if call[0] == "set_shipping_channel"]) == 1
        assert len([call for call in gateway.calls if call[0] == "list_orders"]) == 1

    asyncio.run(run())


def test_warehouse_logistics_unconfirmed_readback_is_manual_without_replay() -> None:
    async def run() -> None:
        gateway = FakeGateway(
            _page(_warehouse_order()),
            _page(_warehouse_order()),
        )
        operations = LingxingCustomOrderApiOperations(gateway, verification_delays_seconds=[0])

        outcome = await operations.set_tent_warehouse_logistics(
            plan=_warehouse_sku_plan(),
            candidate_system_order_nos=["103000000000000001"],
            apply=True,
        )

        assert outcome.status == "manual_review"
        assert "读回确认" in outcome.message
        assert len([call for call in gateway.calls if call[0] == "set_shipping_channel"]) == 1

    asyncio.run(run())
