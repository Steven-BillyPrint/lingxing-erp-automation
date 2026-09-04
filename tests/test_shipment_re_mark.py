from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from erp_automation.application.shipment_re_mark import ShipmentReMarkWorkflow
from lingxing_automation.pages.marked_shipment_update import (
    MarkedShipmentUpdateEvidence,
)
from shipment_automation.erp_mark_ship import ErpMarkManualReview
from shipment_automation.models import (
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_OUTBOUNDED,
    LOGISTICS_READY,
    REMARK_COMPLETED,
    REMARK_DETECTED,
    REMARK_MANUAL_REVIEW,
    LogisticsDetail,
    ShipmentCandidate,
)
from shipment_automation.queue_store import ShipmentQueueStore
from shipment_automation.re_mark_domain import (
    completed_re_mark_eligibility,
    completed_refresh_snapshot,
    current_lingxing_waybill_from_wms_rows,
)


SYSTEM_ORDER_NO = "103735075688785273"
PLATFORM_ORDER_NO = "113-1341773-1145022"
LOGISTICS_NO = "ALS01915029156"
OLD_WAYBILL_NO = "WNBAA0494424973YQ"
NEW_WAYBILL_NO = "1LSD01R0018AGMD"


def _candidate(**changes) -> ShipmentCandidate:
    values = {
        "system_order_no": SYSTEM_ORDER_NO,
        "platform_order_no": PLATFORM_ORDER_NO,
        "logistics_no": LOGISTICS_NO,
        "shipment_tag_name": "自动标发",
        "tag_text": "自动标发",
        "sku_text": "Car-Magent-24x24in-2pcs 共1",
        "product_type": "other",
        "customer_remark": f"重发邮件 {LOGISTICS_NO}",
        "status_text": "待审核发货",
        "receiver_email": "buyer@example.com",
        "sales_platform_code": "Amazon",
        "sales_platform_name": "Amazon",
        "platform_order_item_ids": ("167540768447001",),
        "logistics_provider_name": "手动",
        "logistics_type_name": "万邦速达",
    }
    values.update(changes)
    return ShipmentCandidate(**values)


def _old_detail() -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=LOGISTICS_NO,
        status_text="运输中",
        service_type="快递门到门",
        service_line="万邦速达",
        carrier="万邦速达",
        international_tracking_no=OLD_WAYBILL_NO,
        actual_total="CNY 136.03",
        chargeable_weight_kg="2",
        package_count=1,
    )


def _new_detail() -> LogisticsDetail:
    return LogisticsDetail(
        logistics_no=LOGISTICS_NO,
        status_text="运输中",
        service_type="快递门到门",
        service_line="OnTrac",
        carrier="OnTrac",
        international_tracking_no=NEW_WAYBILL_NO,
        actual_total="CNY 136.03",
        chargeable_weight_kg="2",
        package_count=1,
    )


def _completed_store(tmp_path, **candidate_changes) -> ShipmentQueueStore:
    store = ShipmentQueueStore(tmp_path / "shipment_queue.sqlite3")
    candidate = _candidate(**candidate_changes)
    store.upsert_candidate(candidate)
    assert store.complete_logistics_attempt(
        candidate.logistics_no,
        _old_detail(),
        state=LOGISTICS_READY,
        last_error=None,
    )
    assert store.mark_erp_outbounded(
        candidate.logistics_no,
        email_preview_enabled=True,
    )
    return store


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        (
            {
                "sales_platform_code": "Shopify",
                "sales_platform_name": "Shopify",
            },
            "非 Amazon",
        ),
        ({"platform_order_item_ids": ()}, "OrderItemId"),
        ({"logistics_provider_name": "万邦速达"}, "手动-xxxx"),
        ({"logistics_type_name": ""}, "手动-xxxx"),
    ],
)
def test_completed_re_mark_eligibility_applies_exact_three_gates(
    changes,
    expected_reason,
) -> None:
    values = {
        "sales_platform_code": "Amazon",
        "sales_platform_name": "Amazon",
        "platform_order_item_ids": ("167540768447001",),
        "logistics_provider_name": "手动",
        "logistics_type_name": "万邦速达",
    }
    assert completed_re_mark_eligibility(**values).eligible

    decision = completed_re_mark_eligibility(**{**values, **changes})

    assert not decision.eligible
    assert any(expected_reason in reason for reason in decision.reasons)


def test_single_order_item_id_string_is_valid_ownership_evidence() -> None:
    decision = completed_re_mark_eligibility(
        sales_platform_code="Amazon",
        platform_order_item_ids="167540768447001",
        logistics_provider_name="手动",
        logistics_type_name="OnTrac",
    )

    assert decision.eligible


def test_completed_refresh_snapshot_requires_all_reoutbound_values() -> None:
    snapshot = completed_refresh_snapshot(_new_detail())

    assert snapshot.validation_error == ""
    assert snapshot.carrier == "ONTRAC"
    assert snapshot.waybill_no == NEW_WAYBILL_NO
    assert snapshot.tracking_no == LOGISTICS_NO
    assert snapshot.freight == "136.03"
    assert snapshot.currency == "CNY"
    assert snapshot.fee_weight_g == "2000"

    invalid = completed_refresh_snapshot(
        LogisticsDetail(
            logistics_no=LOGISTICS_NO,
            carrier="OnTrac",
            international_tracking_no=NEW_WAYBILL_NO,
            actual_total="CNY 136.03",
            chargeable_weight_kg="",
        )
    )
    assert "计费重量" in invalid.validation_error


def test_current_lingxing_waybill_uses_only_unique_active_outbounded_als_row() -> None:
    rows = [
        {
            "order_number": SYSTEM_ORDER_NO,
            "platform_order_no": [PLATFORM_ORDER_NO],
            "status": 4,
            "tracking_no": LOGISTICS_NO,
            "waybill_no": OLD_WAYBILL_NO,
        },
        {
            "order_number": SYSTEM_ORDER_NO,
            "platform_order_no": [PLATFORM_ORDER_NO],
            "status": 3,
            "tracking_no": LOGISTICS_NO.lower(),
            "waybill_no": "1lsd-01r 0018agmd",
        },
    ]

    assert current_lingxing_waybill_from_wms_rows(
        rows,
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
        logistics_no=LOGISTICS_NO,
    ) == NEW_WAYBILL_NO


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "order_number": SYSTEM_ORDER_NO,
                "platform_order_no": [PLATFORM_ORDER_NO],
                "status": 2,
                "tracking_no": LOGISTICS_NO,
                "waybill_no": OLD_WAYBILL_NO,
            }
        ],
        [
            {
                "order_number": SYSTEM_ORDER_NO,
                "platform_order_no": [PLATFORM_ORDER_NO],
                "status": 3,
                "tracking_no": LOGISTICS_NO,
                "waybill_no": OLD_WAYBILL_NO,
            },
            {
                "order_number": SYSTEM_ORDER_NO,
                "platform_order_no": [PLATFORM_ORDER_NO],
                "status": 3,
                "tracking_no": LOGISTICS_NO,
                "waybill_no": NEW_WAYBILL_NO,
            },
        ],
    ],
)
def test_current_lingxing_waybill_fails_closed_for_incomplete_or_ambiguous_rows(
    rows,
) -> None:
    with pytest.raises(ValueError):
        current_lingxing_waybill_from_wms_rows(
            rows,
            system_order_no=SYSTEM_ORDER_NO,
            platform_order_no=PLATFORM_ORDER_NO,
            logistics_no=LOGISTICS_NO,
        )


def test_only_recent_automation_completed_orders_are_refresh_targets(tmp_path) -> None:
    recent = _completed_store(tmp_path)
    targets = recent.list_completed_refresh_targets(eligible_only=True)
    assert [row["system_order_no"] for row in targets] == [SYSTEM_ORDER_NO]

    old_time = (datetime.now(timezone.utc) - timedelta(days=16)).isoformat().replace(
        "+00:00",
        "Z",
    )
    with sqlite3.connect(recent.path) as connection:
        connection.execute(
            """
            UPDATE shipment_erp SET outbounded_at = ?
            WHERE job_id = (
                SELECT id FROM shipment_jobs WHERE logistics_no = ?
            )
            """,
            (old_time, LOGISTICS_NO),
        )

    assert recent.list_completed_refresh_targets(eligible_only=True) == []


def test_changed_valid_waybill_creates_one_idempotent_re_mark_cycle(tmp_path) -> None:
    store = _completed_store(tmp_path)

    first = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())
    second = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert first.old_waybill_no == OLD_WAYBILL_NO
    assert first.new_waybill_no == NEW_WAYBILL_NO
    assert first.new_tracking_no == LOGISTICS_NO
    assert first.new_freight == "136.03"
    assert first.new_fee_weight_g == "2000"
    assert len(store.list_re_mark_cycles(actionable_only=False)) == 1


def test_detected_cycle_is_available_only_to_lingxing_reconciliation(tmp_path) -> None:
    store = _completed_store(tmp_path)
    cycle = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())
    assert cycle is not None

    assert store.list_completed_refresh_targets(eligible_only=True) == []
    targets = store.list_completed_refresh_targets(
        eligible_only=True,
        include_detected_reconciliation=True,
    )

    assert [row["system_order_no"] for row in targets] == [SYSTEM_ORDER_NO]


def test_failed_live_lingxing_read_defers_stale_alibaba_comparison(tmp_path) -> None:
    store = _completed_store(tmp_path)
    assert store.list_completed_refresh_targets(eligible_only=True)

    assert store.defer_completed_refresh(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
        logistics_no=LOGISTICS_NO,
        reason="WMS evidence unavailable",
        retry_minutes=15,
    ) == 1

    assert store.list_completed_refresh_targets(eligible_only=True) == []
    row = store.get_by_logistics_no(LOGISTICS_NO)
    assert row is not None
    assert row["completed_refresh_last_error"] == "WMS evidence unavailable"


def test_live_lingxing_new_waybill_closes_detected_cycle_without_external_write(
    tmp_path,
) -> None:
    store = _completed_store(tmp_path)
    cycle = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())
    assert cycle is not None and cycle.state == REMARK_DETECTED

    result = store.reconcile_completed_refresh_lingxing_waybill(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
        logistics_no=LOGISTICS_NO,
        current_waybill_no="1lsd-01r 0018agmd",
        run_id="manual-reconcile-test",
    )

    assert result == {
        "job_count": 1,
        "waybill_changed_count": 1,
        "resolved_cycle_count": 1,
    }
    completed = store.get_re_mark_cycle(cycle.id)
    assert completed is not None and completed.state == REMARK_COMPLETED
    row = store.get_by_logistics_no(LOGISTICS_NO)
    assert row is not None
    assert row["international_tracking_no"] == NEW_WAYBILL_NO
    assert row["carrier"] == "ONTRAC"
    assert row["service_line"] == "OnTrac"
    assert row["fee_amount"] == "136.03"
    assert row["chargeable_weight_kg"] == "2"


def test_live_lingxing_old_waybill_keeps_detected_cycle_actionable(tmp_path) -> None:
    store = _completed_store(tmp_path)
    cycle = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())
    assert cycle is not None

    result = store.reconcile_completed_refresh_lingxing_waybill(
        system_order_no=SYSTEM_ORDER_NO,
        platform_order_no=PLATFORM_ORDER_NO,
        logistics_no=LOGISTICS_NO,
        current_waybill_no=OLD_WAYBILL_NO,
    )

    assert result["resolved_cycle_count"] == 0
    pending = store.get_re_mark_cycle(cycle.id)
    assert pending is not None and pending.state == REMARK_DETECTED


def test_ineligible_or_unchanged_completed_order_does_not_create_cycle(tmp_path) -> None:
    ineligible = _completed_store(tmp_path, platform_order_item_ids=())
    assert ineligible.record_completed_refresh_observation(
        LOGISTICS_NO,
        _new_detail(),
    ) is None
    assert ineligible.list_re_mark_cycles(actionable_only=False) == []

    unchanged_path = tmp_path / "unchanged"
    unchanged_path.mkdir()
    unchanged = _completed_store(unchanged_path)
    assert unchanged.record_completed_refresh_observation(
        LOGISTICS_NO,
        _old_detail(),
    ) is None
    assert unchanged.list_re_mark_cycles(actionable_only=False) == []

    cancelled_path = tmp_path / "cancelled"
    cancelled_path.mkdir()
    cancelled = _completed_store(cancelled_path)
    cancelled_detail = _new_detail()
    cancelled_detail.status_text = "已取消"
    assert cancelled.record_completed_refresh_observation(
        LOGISTICS_NO,
        cancelled_detail,
    ) is None
    assert cancelled.list_re_mark_cycles(actionable_only=False) == []


class _FakeGateway:
    def __init__(self) -> None:
        self.order_status = "已发货"
        self.old_status = 3
        self.new_row_present = False
        self.reads: list[str] = []

    async def get_order_detail(self, order_number: str, **_kwargs):
        self.reads.append(f"detail:{order_number}")
        return SimpleNamespace(
            payload={
                "order_number": SYSTEM_ORDER_NO,
                "platform_order_no": PLATFORM_ORDER_NO,
                "order_status_name": self.order_status,
            }
        )

    async def list_wms_orders(self, **_kwargs):
        self.reads.append("wms")
        rows = [
            {
                "order_number": SYSTEM_ORDER_NO,
                "platform_order_no": [PLATFORM_ORDER_NO],
                "status": self.old_status,
                "wo_number": "WO-OLD",
                "waybill_no": OLD_WAYBILL_NO,
                "tracking_no": LOGISTICS_NO,
                "logistics_freight": "136.03",
                "logistics_freight_currency_code": "CNY",
                "pkg_fee_weight": "2000",
                "pkg_fee_weight_unit": "g",
            }
        ]
        if self.new_row_present:
            rows.append(
                {
                    "order_number": SYSTEM_ORDER_NO,
                    "platform_order_no": [PLATFORM_ORDER_NO],
                    "status": 3,
                    "wo_number": "WO-NEW",
                    "waybill_no": NEW_WAYBILL_NO,
                    "tracking_no": LOGISTICS_NO,
                    "logistics_freight": "136.03",
                    "logistics_freight_currency_code": "CNY",
                    "pkg_fee_weight": "2000",
                    "pkg_fee_weight_unit": "g",
                }
            )
        return SimpleNamespace(items=tuple(rows))


class _FakeMarkAdapter:
    def __init__(self, gateway: _FakeGateway, events: list[str]) -> None:
        self.gateway = gateway
        self.events = events

    async def __call__(
        self,
        _page,
        item,
        confirm_func,
        *,
        checkpoint_func,
        approval_func,
        **_kwargs,
    ) -> None:
        self.events.append(f"api:item:{item.system_order_no}:{item.international_tracking_no}")
        assert await confirm_func("即将发送的设置仓库物流参数")
        await checkpoint_func(ERP_CHECKPOINT_CHANNEL_SET, {})
        assert await confirm_func("即将发送的审核发货参数")
        await checkpoint_func(ERP_CHECKPOINT_AUDITED, {})
        assert await confirm_func("即将发送的运单填写参数")
        await approval_func("logistics", "payload-hash")
        await checkpoint_func(ERP_CHECKPOINT_LOGISTICS_SAVED, {"wo_number": "WO-NEW"})
        assert await confirm_func("即将发送的出库发货参数")
        self.gateway.new_row_present = True
        await checkpoint_func(ERP_CHECKPOINT_OUTBOUNDED, {"wo_number": "WO-NEW"})


def _workflow_fixture(tmp_path):
    store = _completed_store(tmp_path)
    cycle = store.record_completed_refresh_observation(LOGISTICS_NO, _new_detail())
    assert cycle is not None
    gateway = _FakeGateway()
    events: list[str] = []

    async def withdraw(_page, *, before_final_confirm, **kwargs):
        events.append(f"withdraw:{kwargs['system_order_no']}")
        await before_final_confirm()
        gateway.order_status = "待审核"
        gateway.old_status = 4

    async def update(_page, *, before_final_confirm, **kwargs):
        events.append(f"mark:{kwargs['system_order_no']}")
        await before_final_confirm()
        text = f"OnTrac ： {kwargs['new_waybill_no']} 标发中"
        return MarkedShipmentUpdateEvidence(
            system_order_no=kwargs["system_order_no"],
            before_submit_row_text="可更新",
            before_submit_system_marking_text=(
                f"OnTrac ： {kwargs['new_waybill_no']} 待标发"
            ),
            after_submit_row_text="标发中",
            after_submit_system_marking_text=text,
        )

    workflow = ShipmentReMarkWorkflow(
        gateway,
        store,
        _FakeMarkAdapter(gateway, events),
        withdraw_func=withdraw,
        update_mark_func=update,
        mark_visibility_delays=(0,),
    )
    return store, cycle, gateway, events, workflow


def test_re_mark_workflow_uses_dom_withdraw_openapi_and_post_submit_dom_evidence(
    tmp_path,
) -> None:
    store, cycle, gateway, events, workflow = _workflow_fixture(tmp_path)
    email_batches_before = store.list_email_batches(platform_order_no=PLATFORM_ORDER_NO)

    async def confirm(_prompt: str) -> bool:
        return True

    result = asyncio.run(
        workflow.execute(
            object(),
            cycle.id,
            lease_owner="desktop-test",
            confirm_func=confirm,
            run_id="test-run",
        )
    )

    assert result.state == REMARK_COMPLETED
    assert events == [
        f"withdraw:{SYSTEM_ORDER_NO}",
        f"api:item:{SYSTEM_ORDER_NO}:{NEW_WAYBILL_NO}",
        f"mark:{SYSTEM_ORDER_NO}",
    ]
    assert any(read.startswith("detail:") for read in gateway.reads)
    # The initial Lingxing waybill check and old-outbound check share one
    # authoritative WMS snapshot; later reads prove withdrawal and re-outbound.
    assert gateway.reads.count("wms") == 3
    completed = store.get_re_mark_cycle(cycle.id)
    assert completed is not None and completed.state == REMARK_COMPLETED
    row = store.get_by_logistics_no(LOGISTICS_NO)
    assert row is not None
    assert row["international_tracking_no"] == NEW_WAYBILL_NO
    assert row["carrier"] == "ONTRAC"
    assert row["service_line"] == "OnTrac"
    assert row["fee_amount"] == "136.03"
    assert row["chargeable_weight_kg"] == "2"
    assert store.list_email_batches(platform_order_no=PLATFORM_ORDER_NO) == email_batches_before


def test_re_mark_workflow_stops_before_browser_write_when_lingxing_is_already_new(
    tmp_path,
) -> None:
    store, cycle, gateway, events, workflow = _workflow_fixture(tmp_path)
    gateway.old_status = 4
    gateway.new_row_present = True

    async def confirm(_prompt: str) -> bool:
        raise AssertionError("already-completed reconciliation must not ask to write")

    result = asyncio.run(
        workflow.execute(
            object(),
            cycle.id,
            lease_owner="desktop-test",
            confirm_func=confirm,
            run_id="preflight-reconcile-test",
        )
    )

    assert result.state == REMARK_COMPLETED
    assert "未执行撤销或写入" in result.message
    assert events == []


def test_mark_intent_without_post_submit_dom_evidence_requires_manual_review(
    tmp_path,
) -> None:
    store, cycle, _gateway, _events, workflow = _workflow_fixture(tmp_path)
    attempts = 0

    async def ambiguous_update(_page, *, before_final_confirm, **_kwargs):
        nonlocal attempts
        attempts += 1
        await before_final_confirm()
        raise RuntimeError("系统标发单号未更新")

    workflow.update_mark_func = ambiguous_update

    async def confirm(_prompt: str) -> bool:
        return True

    with pytest.raises(ErpMarkManualReview, match="结果不明确"):
        asyncio.run(
            workflow.execute(
                object(),
                cycle.id,
                lease_owner="desktop-test",
                confirm_func=confirm,
            )
        )

    assert attempts == 1
    blocked = store.get_re_mark_cycle(cycle.id)
    assert blocked is not None
    assert blocked.state == REMARK_MANUAL_REVIEW


def test_openapi_channel_intent_failure_is_blocked_without_automatic_replay(
    tmp_path,
) -> None:
    store, cycle, _gateway, _events, workflow = _workflow_fixture(tmp_path)
    attempts = 0

    async def ambiguous_adapter(
        _page,
        _item,
        confirm_func,
        **_kwargs,
    ):
        nonlocal attempts
        attempts += 1
        assert await confirm_func("即将发送的设置仓库物流参数")
        raise ConnectionError("response lost")

    workflow.mark_adapter = ambiguous_adapter

    async def confirm(_prompt: str) -> bool:
        return True

    with pytest.raises(ErpMarkManualReview, match="结果不明确"):
        asyncio.run(
            workflow.execute(
                object(),
                cycle.id,
                lease_owner="desktop-test",
                confirm_func=confirm,
            )
        )

    assert attempts == 1
    blocked = store.get_re_mark_cycle(cycle.id)
    assert blocked is not None
    assert blocked.state == REMARK_MANUAL_REVIEW
