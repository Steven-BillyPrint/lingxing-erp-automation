from __future__ import annotations

import asyncio
from dataclasses import replace

from erp_automation.contracts.internal_orders import (
    ContactSnapshot,
    InternalOrderDetail,
)
from lingxing_automation.flows import contact_sync
from lingxing_automation.models import BatchOrderItem
from lingxing_automation.services.custom_order_api import WarehouseLogisticsOutcome
from lingxing_automation.services.tent_sku_planner import (
    DestinationRegion,
    TentSkuAdjustmentPlan,
    TentSkuPlanAction,
)
from lingxing_automation.services.tent_warehouse_routing import (
    TentWarehouseRoutingDecision,
    TentWarehouseRoutingPlan,
    tent_sku_plan_to_routing_input,
)
from lingxing_automation.storage.dedupe import (
    append_folder_complete_platform_order,
    append_package_split_platform_order,
    append_sku_adjustment_platform_order,
    is_warehouse_logistics_done,
    load_order_workflow_record,
)


PLATFORM = "112-1234567-1234567"
SYSTEM = "103700000000000001"


def _sku_plan() -> TentSkuAdjustmentPlan:
    return TentSkuAdjustmentPlan(
        platform_order_no=PLATFORM,
        system_order_no=SYSTEM,
        destination=DestinationRegion(
            raw_text="United States, NY, 11725",
            country="US",
            state="NY",
            postal_code="11725",
            category="us_mainland",
        ),
        replace_main_items=[
            TentSkuPlanAction(
                action="replace_main",
                sku="10X10-FRAME-40MM-SQUARE",
                quantity=1,
                source_order_item_id="main-row",
                source_original_quantity=1,
            )
        ],
    )


def _route_plan() -> TentWarehouseRoutingPlan:
    return TentWarehouseRoutingPlan(
        platform_order_no=PLATFORM,
        postal_code="11725",
        status="ready",
        required=True,
        source_sha256="a" * 64,
        decisions=(
            TentWarehouseRoutingDecision(
                system_order_no=SYSTEM,
                status="ready",
                skus=("10X10-FRAME-40MM-SQUARE",),
                sku_classes=("frame",),
                is_main_product_package=True,
                target_warehouse_code="NJ",
                target_warehouse_name="港通 新泽西仓",
                target_channel_name="港通 新泽西仓-FedEx Ground Economy",
                channel_mode="fedex_ground_economy",
                reason="test",
            ),
        ),
    )


class FakeWarehouseOperations:
    def __init__(self, *, final_status: str = "succeeded") -> None:
        self.calls: list[bool] = []
        self.final_status = final_status

    async def set_tent_warehouse_logistics(self, *, apply: bool, **_kwargs):
        self.calls.append(apply)
        if not apply:
            return WarehouseLogisticsOutcome(
                status="preview",
                message="preview",
                plan=_route_plan(),
                details={"writes": []},
            )
        return WarehouseLogisticsOutcome(
            status=self.final_status,
            message="done" if self.final_status == "succeeded" else "ambiguous",
            plan=_route_plan(),
            details={
                "writes": [
                    {"system_order_no": SYSTEM, "status": "verified"}
                ]
            },
        )


def test_warehouse_stage_preview_never_writes_or_confirms(tmp_path):
    operations = FakeWarehouseOperations()

    result = asyncio.run(
        contact_sync.run_tent_warehouse_logistics_stage(
            object(),
            BatchOrderItem(SYSTEM, PLATFORM, "", product_type="tent"),
            SYSTEM,
            None,
            package_split_system_order_nos=[SYSTEM],
            dedupe_path=tmp_path / "state.json",
            write_dedupe=False,
            allow_page_write=False,
            read_dedupe=False,
            api_operations=operations,  # type: ignore[arg-type]
            sku_plan_override=_sku_plan(),
        )
    )

    assert result["warehouse_logistics_status"] == "write_disabled"
    assert result["warehouse_logistics_complete"] is False
    assert result["warehouse_logistics_decisions"][0]["target_warehouse_code"] == "NJ"
    assert operations.calls == [False]


def test_warehouse_stage_requires_confirmation_guard_then_persists(tmp_path):
    operations = FakeWarehouseOperations()
    events: list[str] = []

    async def confirm(_plan):
        events.append("confirm")
        return True

    async def guard(stage, _platform, _system):
        events.append(f"guard:{stage}")
        return True

    policy = type(
        "Policy",
        (),
        {
            "confirm_warehouse_logistics_plan": staticmethod(confirm),
            "runtime_write_guard": staticmethod(guard),
        },
    )()
    state = tmp_path / "state.json"

    result = asyncio.run(
        contact_sync.run_tent_warehouse_logistics_stage(
            object(),
            BatchOrderItem(SYSTEM, PLATFORM, "", product_type="tent"),
            SYSTEM,
            None,
            package_split_system_order_nos=[SYSTEM],
            dedupe_path=state,
            write_dedupe=True,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=operations,  # type: ignore[arg-type]
            interaction_policy=policy,  # type: ignore[arg-type]
            sku_plan_override=_sku_plan(),
        )
    )

    assert result["warehouse_logistics_complete"] is True
    assert events == ["confirm", "guard:warehouse_logistics"]
    assert operations.calls == [False, True]
    assert is_warehouse_logistics_done(state, PLATFORM) is True
    record = load_order_workflow_record(state, PLATFORM)
    assert record["warehouse_logistics_write_results"][0]["status"] == "verified"


def test_projected_warehouse_plan_is_shown_while_authoritative_sync_runs(tmp_path):
    events: list[str] = []
    release_authoritative = asyncio.Event()

    class ProjectedOperations:
        async def set_tent_warehouse_logistics(
            self,
            *,
            apply: bool,
            projected_packages=None,
            **_kwargs,
        ):
            if apply:
                events.append("apply")
                return WarehouseLogisticsOutcome(
                    status="succeeded",
                    message="done",
                    plan=_route_plan(),
                    details={
                        "writes": [
                            {
                                "system_order_no": SYSTEM,
                                "status": "verified",
                            }
                        ]
                    },
                )
            if projected_packages:
                events.append("projected_preview")
                return WarehouseLogisticsOutcome(
                    status="preview",
                    message="preview",
                    plan=_route_plan(),
                    details={
                        "writes": [],
                        "projection_source": "split_ack",
                    },
                )
            events.append("authoritative_started")
            await release_authoritative.wait()
            events.append("authoritative_completed")
            return WarehouseLogisticsOutcome(
                status="preview",
                message="preview",
                plan=_route_plan(),
                details={
                    "writes": [],
                    "projection_source": "order_list",
                    "projection_attempts": 2,
                    "projection_waited_seconds": 3,
                },
            )

    async def confirm(_plan):
        events.append("confirm_shown")
        await asyncio.sleep(0)
        release_authoritative.set()
        events.append("confirm_approved")
        return True

    async def guard(*_args):
        events.append("guard")
        return True

    policy = type(
        "Policy",
        (),
        {
            "confirm_warehouse_logistics_plan": staticmethod(confirm),
            "runtime_write_guard": staticmethod(guard),
        },
    )()

    result = asyncio.run(
        contact_sync.run_tent_warehouse_logistics_stage(
            object(),
            BatchOrderItem(SYSTEM, PLATFORM, "", product_type="tent"),
            SYSTEM,
            None,
            package_split_system_order_nos=[SYSTEM],
            package_split_projected_packages=[
                {
                    "system_order_no": SYSTEM,
                    "items": [
                        {
                            "sku": "10X10-FRAME-40MM-SQUARE",
                            "quantity": 1,
                            "item_id": "main-row",
                            "order_item_no": "main-row",
                        }
                    ],
                }
            ],
            dedupe_path=tmp_path / "state.json",
            write_dedupe=True,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=ProjectedOperations(),  # type: ignore[arg-type]
            interaction_policy=policy,  # type: ignore[arg-type]
            sku_plan_override=_sku_plan(),
        )
    )

    assert result["warehouse_logistics_complete"] is True
    assert result["warehouse_logistics_projection_source"] == "split_ack"
    assert result["warehouse_logistics_projection_attempts"] == 2
    assert events == [
        "projected_preview",
        "confirm_shown",
        "authoritative_started",
        "confirm_approved",
        "authoritative_completed",
        "guard",
        "apply",
    ]


def test_warehouse_stage_manual_result_is_not_replayed(tmp_path):
    operations = FakeWarehouseOperations(final_status="manual_review")

    async def confirm(_plan):
        return True

    async def guard(*_args):
        return True

    policy = type(
        "Policy",
        (),
        {
            "confirm_warehouse_logistics_plan": staticmethod(confirm),
            "runtime_write_guard": staticmethod(guard),
        },
    )()
    result = asyncio.run(
        contact_sync.run_tent_warehouse_logistics_stage(
            object(),
            BatchOrderItem(SYSTEM, PLATFORM, "", product_type="tent"),
            SYSTEM,
            None,
            package_split_system_order_nos=[SYSTEM],
            dedupe_path=tmp_path / "state.json",
            write_dedupe=True,
            allow_page_write=True,
            read_dedupe=False,
            api_operations=operations,  # type: ignore[arg-type]
            interaction_policy=policy,  # type: ignore[arg-type]
            sku_plan_override=_sku_plan(),
        )
    )

    assert result["warehouse_logistics_status"] == "warehouse_logistics_manual_review"
    assert result["warehouse_logistics_complete"] is False
    assert operations.calls == [False, True]


def test_split_order_retry_restores_persisted_plan_and_skips_detail(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    append_folder_complete_platform_order(
        state,
        PLATFORM,
        SYSTEM,
        product_type="tent",
        sku_adjustment_required=True,
    )
    append_sku_adjustment_platform_order(state, PLATFORM, SYSTEM, sku_status="api")
    append_package_split_platform_order(
        state,
        PLATFORM,
        SYSTEM,
        package_status="api",
        package_required=True,
        system_order_nos=["child-b", "child-a"],
        warehouse_plan_input=tent_sku_plan_to_routing_input(_sku_plan()),
    )
    captured: dict[str, object] = {}

    async def close(_page):
        return None

    async def fill(_page, _order, _kind):
        return {"search_validation_ok": True}

    async def wait(*_args):
        return ["child-a", "child-b"]

    async def warehouse_stage(*_args, **kwargs):
        captured["candidate_nos"] = kwargs["package_split_system_order_nos"]
        captured["plan"] = kwargs["sku_plan_override"]
        captured["runtime_system_order_no"] = kwargs["runtime_system_order_no"]
        return {
            "warehouse_logistics_complete": True,
            "warehouse_logistics_status": "warehouse_logistics_complete",
        }

    async def forbidden_detail(*_args, **_kwargs):
        raise AssertionError("拆单后的仓库阶段恢复不应重新打开任一子订单详情")

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", wait)
    monkeypatch.setattr(contact_sync, "run_tent_warehouse_logistics_stage", warehouse_stage)
    monkeypatch.setattr(contact_sync, "click_system_order", forbidden_detail)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                "child-a",
                PLATFORM,
                "tent split order",
                product_type="tent",
            ),
            object(),
            dedupe_path=state,
            create_folder=False,
            write_dedupe=False,
            ignore_payment_window=True,
            api_operations=object(),  # type: ignore[arg-type]
        )
    )

    assert result["status"] == "updated"
    assert captured["candidate_nos"] == ["child-b", "child-a"]
    assert captured["plan"].destination.postal_code == "11725"


def test_split_order_retry_refetches_missing_postal_from_original_order_only(
    monkeypatch,
    tmp_path,
):
    state = tmp_path / "state.json"
    missing_postal_plan = replace(
        _sku_plan(),
        destination=replace(
            _sku_plan().destination,
            postal_code=None,
            postal_source=None,
            postal_error="历史计划未保存邮编",
        ),
    )
    append_folder_complete_platform_order(
        state,
        PLATFORM,
        SYSTEM,
        product_type="tent",
        sku_adjustment_required=True,
    )
    append_sku_adjustment_platform_order(state, PLATFORM, SYSTEM, sku_status="api")
    append_package_split_platform_order(
        state,
        PLATFORM,
        SYSTEM,
        package_status="api",
        package_required=True,
        system_order_nos=["child-b", "child-a"],
        warehouse_plan_input=tent_sku_plan_to_routing_input(missing_postal_plan),
    )
    captured: dict[str, object] = {"opened": []}

    async def close(_page):
        return None

    async def fill(_page, _order, _kind):
        return {"search_validation_ok": True}

    async def wait(*_args):
        return ["child-a", "child-b"]

    async def click(_page, order_no):
        captured["opened"].append(order_no)

    async def detail_wait(_page, order_no):
        assert order_no == SYSTEM

    async def assert_detail(_page, order_no, platform_order_no, _label):
        assert order_no == SYSTEM
        assert platform_order_no == PLATFORM

    class InternalOperations:
        async def get_order_detail(self, order_no, platform_order_no):
            assert order_no == SYSTEM
            assert platform_order_no == PLATFORM
            return InternalOrderDetail(
                system_order_no=SYSTEM,
                platform_order_nos=(PLATFORM,),
                recipient_name="Buyer",
                address_line1="1 Main St",
                address_line2=None,
                address_line3=None,
                city="CLEVELAND",
                state_or_region="OH",
                country_code="US",
                country_name="United States of America (USA)",
                postal_code="44102",
                shipping_address_text=(
                    "收件地址 United States of America (USA)，OH，CLEVELAND 邮编 44102"
                ),
                contact=ContactSnapshot(),
                status="2",
                revision="detail-revision",
                request_id="detail-request",
            )

        async def update_contacts(self, *_args, **_kwargs):
            raise AssertionError("仓库邮编刷新不得修改联系方式")

    async def warehouse_stage(*_args, **kwargs):
        captured["candidate_nos"] = kwargs["package_split_system_order_nos"]
        captured["plan"] = kwargs["sku_plan_override"]
        captured["runtime_system_order_no"] = kwargs["runtime_system_order_no"]
        return {
            "warehouse_logistics_complete": False,
            "warehouse_logistics_status": "write_disabled",
            "warehouse_logistics_error": "等待用户重新发起测试",
        }

    monkeypatch.setattr(contact_sync, "close_order_detail_dialog", close)
    monkeypatch.setattr(contact_sync, "fill_order_search", fill)
    monkeypatch.setattr(contact_sync, "wait_for_orders_in_list", wait)
    monkeypatch.setattr(contact_sync, "click_system_order", click)
    monkeypatch.setattr(contact_sync, "wait_for_detail", detail_wait)
    monkeypatch.setattr(contact_sync, "assert_current_detail_order", assert_detail)
    monkeypatch.setattr(contact_sync, "run_tent_warehouse_logistics_stage", warehouse_stage)

    result = asyncio.run(
        contact_sync.process_batch_order_item(
            object(),
            BatchOrderItem(
                "child-a",
                PLATFORM,
                "tent split order",
                product_type="tent",
            ),
            object(),
            dedupe_path=state,
            create_folder=True,
            write_dedupe=True,
            ignore_payment_window=True,
            api_operations=object(),  # type: ignore[arg-type]
            internal_order_operations=InternalOperations(),
        )
    )

    refreshed_record = load_order_workflow_record(state, PLATFORM)
    refreshed_plan = refreshed_record["warehouse_logistics_plan_input"]
    assert captured["opened"] == []
    assert captured["candidate_nos"] == ["child-b", "child-a"]
    assert captured["plan"].destination.postal_code == "44102"
    assert captured["plan"].destination.postal_source == "lingxing_internal_detail"
    assert captured["runtime_system_order_no"] == SYSTEM
    assert refreshed_plan["destination"]["postal_code"] == "44102"
    assert refreshed_plan["destination"]["postal_source"] == "lingxing_internal_detail"
    assert refreshed_record["contact_writeback_complete"] is True
    assert refreshed_record["folder_complete"] is True
    assert refreshed_record["sku_adjustment_complete"] is True
    assert refreshed_record["package_split_complete"] is True
    assert refreshed_record["warehouse_logistics_complete"] is False
    assert result["warehouse_logistics_status"] == "write_disabled"
