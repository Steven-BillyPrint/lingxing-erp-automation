from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from erp_automation.application.capabilities import (
    Capability,
    ManualReviewRequired,
    MutationResult,
    MutationState,
)
from erp_automation.application.custom_order_api import LingxingCustomOrderApiOperations
from erp_automation.application.lingxing_gateway import (
    MutationVerification,
    OrderPage,
    OrderRecord,
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
)
from lingxing_automation.services.tent_sku_adjuster import TentSkuAdjustmentResult


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
    def __init__(self, *pages: OrderPage) -> None:
        self.pages = deque(pages)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.update_result: MutationResult | BaseException = _success()
        self.split_result: MutationResult | BaseException = _success()
        self.remark_result: MutationResult | BaseException = _success()
        self.phone_result: MutationResult | BaseException = _success()

    async def list_orders(self, **kwargs: Any) -> OrderPage:
        self.calls.append(("list_orders", (), kwargs))
        if not self.pages:
            raise AssertionError("unexpected list_orders call")
        return self.pages.popleft()

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
                details={**dict(result.details), "verification": verification.outcome.value},
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
        call = next(call for call in gateway.calls if call[0] == "split_order")
        assert call[1][1] == [
            [{"item_id": "fabric", "quantity": 2}],
            [{"item_id": "instruction", "quantity": 1}],
            [{"item_id": "frame", "quantity": 1}],
        ]
        assert sum(item["quantity"] for group in call[1][1] for item in group) == 4

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
        assert "禁止网页重试" in (result.error or "")

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
        )

        assert outcome.succeeded is True
        assert outcome.target_system_order_no == "103000000000000002"
        assert outcome.action == "append"
        call = next(call for call in gateway.calls if call[0] == "set_order_remark")
        assert call[1] == ("103000000000000002", "7.20发说明书\nexisting note")
        assert call[2]["append"] is False

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
