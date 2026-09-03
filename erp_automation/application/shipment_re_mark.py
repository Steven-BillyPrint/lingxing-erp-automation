"""Recoverable orchestration for changed waybills on completed shipments.

The domain decision, SQLite journal, Lingxing OpenAPI adapter and Chrome DOM
adapter remain separate.  This module only coordinates their checkpoints and
never calls Amazon SP-API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from lingxing_automation.pages.marked_shipment_update import (
    MarkedShipmentUpdateEvidence,
    system_marking_contains_waybill,
    update_marked_shipment,
)
from lingxing_automation.pages.shipment_reversal import (
    withdraw_shipped_order_to_pending_review,
)
from shipment_automation.erp_mark_ship import (
    ErpMarkEmergencyStopped,
    ErpMarkManualReview,
    ErpMarkUserAbort,
    RuntimeGuardFunc,
    ensure_erp_write_allowed,
)
from shipment_automation.models import (
    ERP_CHECKPOINT_AUDITED,
    ERP_CHECKPOINT_CHANNEL_SET,
    ERP_CHECKPOINT_LOGISTICS_SAVED,
    ERP_CHECKPOINT_NONE,
    ERP_CHECKPOINT_OUTBOUNDED,
    REMARK_CANCELLED,
    REMARK_CHANNEL_CONFIRMED,
    REMARK_CHANNEL_INTENT,
    REMARK_COMPLETED,
    REMARK_DETECTED,
    REMARK_AUDIT_CONFIRMED,
    REMARK_AUDIT_INTENT,
    REMARK_MANUAL_REVIEW,
    REMARK_MARK_CONFIRMED,
    REMARK_MARK_INTENT,
    REMARK_MARK_WAITING,
    REMARK_OUTBOUND_CONFIRMED,
    REMARK_OUTBOUND_INTENT,
    REMARK_TRACKING_CONFIRMED,
    REMARK_TRACKING_INTENT,
    REMARK_WITHDRAW_CONFIRMED,
    REMARK_WITHDRAW_INTENT,
    ReadyToMarkItem,
    ReMarkCycle,
)
from shipment_automation.queue_store import ShipmentQueueStore
from shipment_automation.re_mark_domain import (
    current_lingxing_waybill_from_wms_rows,
)

from .api_erp_mark import (
    ApiErpMarkAdapter,
    ConfigurationProvider,
    GatewayFactory,
    OutboundStrategy,
)
from .lingxing_gateway import LingxingGateway


ConfirmFunc = Callable[[str], Awaitable[bool]]
ProgressFunc = Callable[[str, int], None]
CheckpointFunc = Callable[[str, dict[str, str | None]], Awaitable[None]]
ApprovalFunc = Callable[[str, str], Awaitable[None]]
BrowserMutation = Callable[..., Awaitable[Any]]
SleepFunc = Callable[[float], Awaitable[None]]


class ReMarkWorkflowStore(Protocol):
    def claim_re_mark_cycle(self, cycle_id: int, owner: str, *, lease_minutes: int = 30) -> ReMarkCycle | None: ...
    def release_re_mark_cycle(self, cycle_id: int, owner: str) -> bool: ...
    def get_re_mark_cycle(self, cycle_id: int) -> ReMarkCycle | None: ...
    def advance_re_mark_cycle(self, cycle_id: int, **values: Any) -> bool: ...
    def update_re_mark_checkpoint(self, cycle_id: int, **values: Any) -> bool: ...
    def require_re_mark_manual_review(self, cycle_id: int, reason: str, **values: Any) -> bool: ...
    def complete_re_mark_cycle(self, cycle_id: int, *, run_id: str | None = None) -> bool: ...
    def reconcile_completed_refresh_lingxing_waybill(self, **values: Any) -> dict[str, int]: ...


@dataclass(frozen=True)
class ShipmentReMarkResult:
    cycle_id: int
    system_order_no: str
    platform_order_no: str
    old_waybill_no: str
    new_waybill_no: str
    state: str
    message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "completed" if self.state == REMARK_COMPLETED else "blocked",
            "cycle_id": self.cycle_id,
            "system_order_no": self.system_order_no,
            "platform_order_no": self.platform_order_no,
            "old_waybill_no": self.old_waybill_no,
            "new_waybill_no": self.new_waybill_no,
            "re_mark_state": self.state,
            "message": self.message,
        }


def _status(row: Mapping[str, Any]) -> int | None:
    value = row.get("status")
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.replace("；", ",").split(",") if part.strip()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {str(part or "").strip() for part in value if str(part or "").strip()}
    text = str(value or "").strip()
    return {text} if text else set()


def _numeric_equal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left or "").strip()) == Decimal(str(right or "").strip())
    except InvalidOperation:
        return False


def _payload_text(payload: Mapping[str, Any], *keys: str) -> str:
    containers: list[Mapping[str, Any]] = [payload]
    for key in ("data", "order", "order_info", "orderInfo"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
    for container in containers:
        for key in keys:
            text = str(container.get(key) or "").strip()
            if text:
                return text
    return ""


class ShipmentReMarkWorkflow:
    """One-cycle state machine with read-before-write and read-after-write guards."""

    def __init__(
        self,
        gateway: LingxingGateway,
        store: ReMarkWorkflowStore,
        mark_adapter: ApiErpMarkAdapter,
        *,
        withdraw_func: BrowserMutation = withdraw_shipped_order_to_pending_review,
        update_mark_func: BrowserMutation = update_marked_shipment,
        sleeper: SleepFunc = asyncio.sleep,
        mark_visibility_delays: Sequence[float] = (0.0, 1.0, 2.0, 3.0),
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.mark_adapter = mark_adapter
        self.withdraw_func = withdraw_func
        self.update_mark_func = update_mark_func
        self.sleeper = sleeper
        self.mark_visibility_delays = tuple(max(0.0, float(value)) for value in mark_visibility_delays)

    async def execute(
        self,
        page: Any,
        cycle_id: int,
        *,
        lease_owner: str,
        confirm_func: ConfirmFunc,
        runtime_guard_func: RuntimeGuardFunc | None = None,
        progress_func: ProgressFunc | None = None,
        run_id: str | None = None,
    ) -> ShipmentReMarkResult:
        cycle = self.store.claim_re_mark_cycle(cycle_id, lease_owner)
        if cycle is None:
            raise ErpMarkUserAbort("重新标发周期不存在、已结束，或正由另一台客户端处理。")
        try:
            if cycle.state == REMARK_MANUAL_REVIEW:
                raise ErpMarkManualReview(cycle.last_error or "该重新标发周期需要人工复核。")
            if cycle.state in {REMARK_COMPLETED, REMARK_CANCELLED}:
                raise ErpMarkUserAbort("重新标发周期已经结束。")
            if cycle.state == REMARK_DETECTED:
                self._progress(progress_func, "正在通过领星 OpenAPI 复核当前运单号。", 8)
                current_waybill = await self._current_lingxing_waybill(cycle)
                reconciliation = (
                    self.store.reconcile_completed_refresh_lingxing_waybill(
                        system_order_no=cycle.system_order_no,
                        platform_order_no=cycle.platform_order_no,
                        logistics_no=cycle.logistics_no,
                        current_waybill_no=current_waybill,
                        run_id=run_id,
                    )
                )
                if int(reconciliation.get("resolved_cycle_count") or 0) == 1:
                    completed = self._required_cycle(cycle.id)
                    self._progress(
                        progress_func,
                        "领星当前运单号已是阿里最新单号，无需重复撤销标发。",
                        96,
                    )
                    return ShipmentReMarkResult(
                        cycle_id=completed.id,
                        system_order_no=completed.system_order_no,
                        platform_order_no=completed.platform_order_no,
                        old_waybill_no=completed.old_waybill_no,
                        new_waybill_no=completed.new_waybill_no,
                        state=completed.state,
                        message=(
                            "领星当前运单号已等于阿里最新单号，"
                            "已判定人工重标发完成；未执行撤销或写入。"
                        ),
                    )
                cycle = self._required_cycle(cycle.id)
            self._progress(progress_func, "正在通过领星 OpenAPI 核对原已发货包裹。", 12)
            cycle = await self._withdraw_if_needed(
                page,
                cycle,
                runtime_guard_func=runtime_guard_func,
                run_id=run_id,
            )
            self._progress(progress_func, "已撤销回待审核，正在重设渠道、运单、运费和重量。", 38)
            cycle = await self._outbound_if_needed(
                cycle,
                confirm_func=confirm_func,
                runtime_guard_func=runtime_guard_func,
                run_id=run_id,
            )
            self._progress(progress_func, "领星已重新出库，正在订单标发页按系统单号提交更新。", 82)
            cycle = await self._mark_if_needed(
                page,
                cycle,
                runtime_guard_func=runtime_guard_func,
                run_id=run_id,
            )
            if not self.store.complete_re_mark_cycle(cycle.id, run_id=run_id):
                raise ErpMarkManualReview("页面标发成功，但本地周期结案失败；禁止重复提交。")
            completed = self._required_cycle(cycle.id)
            self._progress(progress_func, "物流单号更新和重新标发已完成。", 96)
            return ShipmentReMarkResult(
                cycle_id=completed.id,
                system_order_no=completed.system_order_no,
                platform_order_no=completed.platform_order_no,
                old_waybill_no=completed.old_waybill_no,
                new_waybill_no=completed.new_waybill_no,
                state=completed.state,
                message=(
                    "已撤销回待审核、通过领星 OpenAPI 重设物流并重新出库；"
                    "订单标发提交后已读回系统标发单号为新运单号。"
                ),
            )
        except (ErpMarkEmergencyStopped, ErpMarkUserAbort):
            raise
        except ErpMarkManualReview as exc:
            self.store.require_re_mark_manual_review(cycle_id, str(exc), run_id=run_id)
            raise
        except Exception as exc:
            current = self.store.get_re_mark_cycle(cycle_id)
            if current is not None and current.state in {
                REMARK_WITHDRAW_INTENT,
                REMARK_CHANNEL_INTENT,
                REMARK_AUDIT_INTENT,
                REMARK_TRACKING_INTENT,
                REMARK_OUTBOUND_INTENT,
                REMARK_MARK_INTENT,
            }:
                reason = f"外部写入边界后的结果不明确：{type(exc).__name__}。禁止自动重试。"
                self.store.require_re_mark_manual_review(cycle_id, reason, run_id=run_id)
                raise ErpMarkManualReview(reason) from exc
            raise
        finally:
            self.store.release_re_mark_cycle(cycle_id, lease_owner)

    async def _withdraw_if_needed(
        self,
        page: Any,
        cycle: ReMarkCycle,
        *,
        runtime_guard_func: RuntimeGuardFunc | None,
        run_id: str | None,
    ) -> ReMarkCycle:
        if cycle.state not in {REMARK_DETECTED, REMARK_WITHDRAW_INTENT}:
            return cycle
        old_row = await self._old_outbound_row(cycle, required=cycle.state == REMARK_DETECTED)
        if cycle.state == REMARK_WITHDRAW_INTENT and await self._withdrawal_confirmed(cycle):
            self._advance(
                cycle,
                REMARK_WITHDRAW_CONFIRMED,
                expected=REMARK_WITHDRAW_INTENT,
                timestamp_column="withdrawn_at",
                run_id=run_id,
            )
            return self._required_cycle(cycle.id)
        if old_row is None:
            raise ErpMarkManualReview("撤销意图已记录，但无法证明原销售出库单仍为已发货或已经撤销。")
        await ensure_erp_write_allowed(runtime_guard_func)

        async def record_intent() -> None:
            await ensure_erp_write_allowed(runtime_guard_func)
            current = self._required_cycle(cycle.id)
            if current.state == REMARK_DETECTED:
                self._advance(
                    current,
                    REMARK_WITHDRAW_INTENT,
                    expected=REMARK_DETECTED,
                    run_id=run_id,
                )

        await self.withdraw_func(
            page,
            system_order_no=cycle.system_order_no,
            platform_order_no=cycle.platform_order_no,
            old_waybill_no=cycle.old_waybill_no,
            logistics_no=cycle.old_tracking_no or cycle.logistics_no,
            before_final_confirm=record_intent,
        )
        if not await self._withdrawal_confirmed(cycle):
            raise ErpMarkManualReview("页面已提交撤销，但 OpenAPI 未能证明订单已回到待审核且原出库单已失效。")
        current = self._required_cycle(cycle.id)
        self._advance(
            current,
            REMARK_WITHDRAW_CONFIRMED,
            expected=REMARK_WITHDRAW_INTENT,
            timestamp_column="withdrawn_at",
            run_id=run_id,
        )
        return self._required_cycle(cycle.id)

    async def _outbound_if_needed(
        self,
        cycle: ReMarkCycle,
        *,
        confirm_func: ConfirmFunc,
        runtime_guard_func: RuntimeGuardFunc | None,
        run_id: str | None,
    ) -> ReMarkCycle:
        if cycle.state in {
            REMARK_OUTBOUND_CONFIRMED,
            REMARK_MARK_WAITING,
            REMARK_MARK_INTENT,
            REMARK_MARK_CONFIRMED,
        }:
            await self._new_outbound_row(cycle)
            return cycle
        cycle = await self._recover_openapi_intent(cycle, run_id=run_id)
        if cycle.state == REMARK_OUTBOUND_CONFIRMED:
            return cycle
        if cycle.state not in {
            REMARK_WITHDRAW_CONFIRMED,
            REMARK_CHANNEL_CONFIRMED,
            REMARK_AUDIT_CONFIRMED,
            REMARK_TRACKING_CONFIRMED,
        }:
            raise ErpMarkManualReview(f"重新出库遇到未知周期状态：{cycle.state}")

        item = ReadyToMarkItem(
            system_order_no=cycle.system_order_no,
            platform_order_no=cycle.platform_order_no,
            logistics_no=cycle.new_tracking_no,
            carrier=cycle.new_carrier,
            service_line=cycle.new_service_line or None,
            international_tracking_no=cycle.new_waybill_no,
            actual_total=f"{cycle.new_currency} {cycle.new_freight}",
            chargeable_weight_kg=format(
                Decimal(cycle.new_fee_weight_g) / Decimal("1000"), "f"
            ),
            erp_checkpoint=cycle.checkpoint or ERP_CHECKPOINT_NONE,
        )

        async def stage_confirm(prompt: str) -> bool:
            if "改用原网页流程" in prompt:
                return False
            accepted = bool(await confirm_func(prompt))
            if accepted:
                current = self._required_cycle(cycle.id)
                if (
                    "设置仓库物流" in prompt
                    and current.state == REMARK_WITHDRAW_CONFIRMED
                ):
                    self._advance(
                        current,
                        REMARK_CHANNEL_INTENT,
                        expected=REMARK_WITHDRAW_CONFIRMED,
                        run_id=run_id,
                    )
                elif "审核发货" in prompt and current.state == REMARK_CHANNEL_CONFIRMED:
                    self._advance(
                        current,
                        REMARK_AUDIT_INTENT,
                        expected=REMARK_CHANNEL_CONFIRMED,
                        run_id=run_id,
                    )
                elif "出库发货" in prompt and current.state == REMARK_TRACKING_CONFIRMED:
                    self._advance(
                        current,
                        REMARK_OUTBOUND_INTENT,
                        expected=REMARK_TRACKING_CONFIRMED,
                        run_id=run_id,
                    )
            return accepted

        async def checkpoint(checkpoint_name: str, values: dict[str, str | None]) -> None:
            if not self.store.update_re_mark_checkpoint(
                cycle.id,
                checkpoint=checkpoint_name,
                run_id=run_id,
            ):
                raise ErpMarkManualReview("无法保存重新标发 OpenAPI 检查点。")
            if checkpoint_name == ERP_CHECKPOINT_LOGISTICS_SAVED:
                current = self._required_cycle(cycle.id)
                if current.state in {
                    REMARK_AUDIT_CONFIRMED,
                    REMARK_TRACKING_INTENT,
                }:
                    self._advance(
                        current,
                        REMARK_TRACKING_CONFIRMED,
                        expected=current.state,
                        timestamp_column="tracking_saved_at",
                        run_id=run_id,
                    )
            elif checkpoint_name == ERP_CHECKPOINT_CHANNEL_SET:
                current = self._required_cycle(cycle.id)
                if current.state == REMARK_CHANNEL_INTENT:
                    self._advance(
                        current,
                        REMARK_CHANNEL_CONFIRMED,
                        expected=REMARK_CHANNEL_INTENT,
                        run_id=run_id,
                    )
            elif checkpoint_name == ERP_CHECKPOINT_AUDITED:
                current = self._required_cycle(cycle.id)
                if current.state in {
                    REMARK_CHANNEL_CONFIRMED,
                    REMARK_AUDIT_INTENT,
                }:
                    self._advance(
                        current,
                        REMARK_AUDIT_CONFIRMED,
                        expected=current.state,
                        run_id=run_id,
                    )

        async def approval(confirmation_type: str, _payload_hash: str) -> None:
            if confirmation_type != "logistics":
                return
            current = self._required_cycle(cycle.id)
            if current.state == REMARK_AUDIT_CONFIRMED:
                self._advance(
                    current,
                    REMARK_TRACKING_INTENT,
                    expected=REMARK_AUDIT_CONFIRMED,
                    run_id=run_id,
                )

        await self.mark_adapter(
            None,
            item,
            stage_confirm,
            checkpoint_func=checkpoint,
            approval_func=approval,
            runtime_guard_func=runtime_guard_func,
            browser_page_provider=None,
        )
        outbound_row = await self._new_outbound_row(cycle)
        current = self._required_cycle(cycle.id)
        if current.checkpoint != ERP_CHECKPOINT_OUTBOUNDED:
            self.store.update_re_mark_checkpoint(
                cycle.id,
                checkpoint=ERP_CHECKPOINT_OUTBOUNDED,
                wo_number=str(outbound_row.get("wo_number") or ""),
                run_id=run_id,
            )
            current = self._required_cycle(cycle.id)
        if current.state in {
            REMARK_AUDIT_CONFIRMED,
            REMARK_TRACKING_INTENT,
        }:
            self._advance(
                current,
                REMARK_TRACKING_CONFIRMED,
                expected=current.state,
                timestamp_column="tracking_saved_at",
                run_id=run_id,
            )
            current = self._required_cycle(cycle.id)
        if current.state == REMARK_TRACKING_CONFIRMED:
            self._advance(
                current,
                REMARK_OUTBOUND_CONFIRMED,
                expected=REMARK_TRACKING_CONFIRMED,
                wo_number=str(outbound_row.get("wo_number") or ""),
                timestamp_column="reoutbounded_at",
                run_id=run_id,
            )
        elif current.state == REMARK_OUTBOUND_INTENT:
            self._advance(
                current,
                REMARK_OUTBOUND_CONFIRMED,
                expected=REMARK_OUTBOUND_INTENT,
                wo_number=str(outbound_row.get("wo_number") or ""),
                timestamp_column="reoutbounded_at",
                run_id=run_id,
            )
        return self._required_cycle(cycle.id)

    async def _recover_openapi_intent(
        self,
        cycle: ReMarkCycle,
        *,
        run_id: str | None,
    ) -> ReMarkCycle:
        """Resolve recorded write intents only from authoritative readback.

        An intent is persisted immediately before the network request.  If the
        process ends at that boundary, absence of proof is ambiguous and must
        never authorize an automatic replay.
        """

        if cycle.state == REMARK_CHANNEL_INTENT:
            raise ErpMarkManualReview(
                "设置物流渠道意图已记录，但当前 OpenAPI 读回不足以证明是否生效；"
                "禁止自动重复设置。"
            )
        if cycle.state == REMARK_AUDIT_INTENT:
            await self._audited_wms_row(cycle)
            if not self.store.update_re_mark_checkpoint(
                cycle.id,
                checkpoint=ERP_CHECKPOINT_AUDITED,
                run_id=run_id,
            ):
                raise ErpMarkManualReview("无法保存审核发货恢复检查点。")
            self._advance(
                cycle,
                REMARK_AUDIT_CONFIRMED,
                expected=REMARK_AUDIT_INTENT,
                run_id=run_id,
            )
            return self._required_cycle(cycle.id)
        if cycle.state == REMARK_TRACKING_INTENT:
            row = await self._new_tracking_row(cycle)
            checkpoint = (
                ERP_CHECKPOINT_OUTBOUNDED
                if _status(row) == 3
                else ERP_CHECKPOINT_LOGISTICS_SAVED
            )
            if not self.store.update_re_mark_checkpoint(
                cycle.id,
                checkpoint=checkpoint,
                wo_number=str(row.get("wo_number") or ""),
                run_id=run_id,
            ):
                raise ErpMarkManualReview("无法保存新物流读回恢复检查点。")
            self._advance(
                cycle,
                REMARK_TRACKING_CONFIRMED,
                expected=REMARK_TRACKING_INTENT,
                timestamp_column="tracking_saved_at",
                run_id=run_id,
            )
            current = self._required_cycle(cycle.id)
            if _status(row) == 3:
                self._advance(
                    current,
                    REMARK_OUTBOUND_CONFIRMED,
                    expected=REMARK_TRACKING_CONFIRMED,
                    wo_number=str(row.get("wo_number") or ""),
                    timestamp_column="reoutbounded_at",
                    run_id=run_id,
                )
            return self._required_cycle(cycle.id)
        if cycle.state == REMARK_OUTBOUND_INTENT:
            row = await self._new_outbound_row(cycle)
            if not self.store.update_re_mark_checkpoint(
                cycle.id,
                checkpoint=ERP_CHECKPOINT_OUTBOUNDED,
                wo_number=str(row.get("wo_number") or ""),
                run_id=run_id,
            ):
                raise ErpMarkManualReview("无法保存重新出库读回恢复检查点。")
            self._advance(
                cycle,
                REMARK_OUTBOUND_CONFIRMED,
                expected=REMARK_OUTBOUND_INTENT,
                wo_number=str(row.get("wo_number") or ""),
                timestamp_column="reoutbounded_at",
                run_id=run_id,
            )
            return self._required_cycle(cycle.id)
        return cycle

    async def _mark_if_needed(
        self,
        page: Any,
        cycle: ReMarkCycle,
        *,
        runtime_guard_func: RuntimeGuardFunc | None,
        run_id: str | None,
    ) -> ReMarkCycle:
        if cycle.state == REMARK_MARK_CONFIRMED:
            return cycle
        if cycle.state == REMARK_MARK_INTENT:
            raise ErpMarkManualReview("订单标发提交意图已记录但成功弹窗未确认，禁止自动重复标发。")
        if cycle.state == REMARK_OUTBOUND_CONFIRMED:
            self._advance(
                cycle,
                REMARK_MARK_WAITING,
                expected=REMARK_OUTBOUND_CONFIRMED,
                run_id=run_id,
            )
            cycle = self._required_cycle(cycle.id)
        if cycle.state != REMARK_MARK_WAITING:
            raise ErpMarkManualReview(f"订单标发遇到未知周期状态：{cycle.state}")

        async def record_intent() -> None:
            await ensure_erp_write_allowed(runtime_guard_func)
            current = self._required_cycle(cycle.id)
            if current.state == REMARK_MARK_WAITING:
                self._advance(
                    current,
                    REMARK_MARK_INTENT,
                    expected=REMARK_MARK_WAITING,
                    run_id=run_id,
                )

        last_error: Exception | None = None
        for delay in self.mark_visibility_delays or (0.0,):
            if delay:
                await self.sleeper(delay)
            await ensure_erp_write_allowed(runtime_guard_func)
            try:
                evidence = await self.update_mark_func(
                    page,
                    system_order_no=cycle.system_order_no,
                    new_waybill_no=cycle.new_waybill_no,
                    before_final_confirm=record_intent,
                )
                if (
                    not isinstance(evidence, MarkedShipmentUpdateEvidence)
                    or evidence.system_order_no != cycle.system_order_no
                    or not system_marking_contains_waybill(
                        evidence.after_submit_system_marking_text,
                        cycle.new_waybill_no,
                    )
                ):
                    raise RuntimeError(
                        "订单标发页没有返回与本周期一致的提交后系统标发单号证据。"
                    )
                current = self._required_cycle(cycle.id)
                self._advance(
                    current,
                    REMARK_MARK_CONFIRMED,
                    expected=REMARK_MARK_INTENT,
                    timestamp_column="platform_marked_at",
                    run_id=run_id,
                )
                return self._required_cycle(cycle.id)
            except Exception as exc:
                last_error = exc
                if self._required_cycle(cycle.id).state == REMARK_MARK_INTENT:
                    raise
        raise RuntimeError(
            f"订单标发页等待可更新记录超时：{type(last_error).__name__ if last_error else 'UnknownError'}"
        ) from last_error

    async def _order_status(self, cycle: ReMarkCycle) -> str:
        detail = await self.gateway.get_order_detail(cycle.system_order_no, browser=None)
        payload = detail.payload if isinstance(detail.payload, Mapping) else {}
        # 领星订单详情的 ``order_number`` 是系统单号，而不是平台单号。
        returned_system = _payload_text(
            payload,
            "global_order_no",
            "system_order_no",
            "order_number",
        )
        if returned_system and returned_system != cycle.system_order_no:
            raise ErpMarkManualReview("订单详情返回了不同的系统单号。")
        returned_platform = _payload_text(
            payload,
            "platform_order_no",
            "platform_order_id",
        )
        if returned_platform and returned_platform != cycle.platform_order_no:
            raise ErpMarkManualReview("订单详情返回了不同的平台单号。")
        return _payload_text(
            payload,
            "order_status_name",
            "order_status",
            "status_name",
            "status",
        )

    async def _wms_rows(self, cycle: ReMarkCycle) -> list[Mapping[str, Any]]:
        page = await self.gateway.list_wms_orders(
            filters={
                "page": 1,
                "page_size": 200,
                "order_number_arr": [cycle.system_order_no],
            },
            offset=0,
            length=200,
            browser=None,
        )
        rows: list[Mapping[str, Any]] = []
        for row in page.items:
            if str(row.get("order_number") or "").strip() != cycle.system_order_no:
                continue
            platform_numbers = _string_values(row.get("platform_order_no"))
            if platform_numbers and cycle.platform_order_no not in platform_numbers:
                raise ErpMarkManualReview("销售出库单系统单号与平台单号不一致。")
            rows.append(row)
        return rows

    async def _current_lingxing_waybill(self, cycle: ReMarkCycle) -> str:
        try:
            return current_lingxing_waybill_from_wms_rows(
                await self._wms_rows(cycle),
                system_order_no=cycle.system_order_no,
                platform_order_no=cycle.platform_order_no,
                logistics_no=cycle.logistics_no,
            )
        except ErpMarkManualReview:
            raise
        except Exception as exc:
            raise ErpMarkUserAbort(
                "领星 OpenAPI 未能返回唯一可信的当前运单号，"
                "本次未执行撤销或任何外部写入。"
            ) from exc

    async def _old_outbound_row(
        self,
        cycle: ReMarkCycle,
        *,
        required: bool,
    ) -> Mapping[str, Any] | None:
        rows = await self._wms_rows(cycle)
        matches = [
            row
            for row in rows
            if _status(row) == 3
            and str(row.get("waybill_no") or "").strip() == cycle.old_waybill_no
            and str(row.get("tracking_no") or "").strip()
            == (cycle.old_tracking_no or cycle.logistics_no)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches and not required:
            return None
        raise ErpMarkManualReview("未找到唯一且与旧运单/ALS 完全一致的已出库销售单。")

    async def _withdrawal_confirmed(self, cycle: ReMarkCycle) -> bool:
        status = await self._order_status(cycle)
        if "待审核" not in status or "已发货" in status:
            return False
        rows = await self._wms_rows(cycle)
        return not any(
            _status(row) == 3
            and str(row.get("waybill_no") or "").strip() == cycle.old_waybill_no
            and str(row.get("tracking_no") or "").strip()
            == (cycle.old_tracking_no or cycle.logistics_no)
            for row in rows
        )

    async def _new_outbound_row(self, cycle: ReMarkCycle) -> Mapping[str, Any]:
        matches = await self._matching_new_rows(cycle, statuses={3})
        if len(matches) != 1:
            raise ErpMarkManualReview("OpenAPI 未返回唯一且运单、ALS、运费、币种、重量完全一致的新已出库销售单。")
        return matches[0]

    async def _new_tracking_row(self, cycle: ReMarkCycle) -> Mapping[str, Any]:
        matches = await self._matching_new_rows(cycle, statuses={2, 3})
        if len(matches) != 1:
            raise ErpMarkManualReview(
                "新物流写入意图已记录，但 OpenAPI 未返回唯一且全部字段一致的新销售出库单；"
                "禁止自动重复写入。"
            )
        return matches[0]

    async def _matching_new_rows(
        self,
        cycle: ReMarkCycle,
        *,
        statuses: set[int],
    ) -> list[Mapping[str, Any]]:
        rows = await self._wms_rows(cycle)
        return [
            row
            for row in rows
            if _status(row) in statuses
            and str(row.get("waybill_no") or "").strip() == cycle.new_waybill_no
            and str(row.get("tracking_no") or "").strip() == cycle.new_tracking_no
            and _numeric_equal(row.get("logistics_freight"), cycle.new_freight)
            and str(row.get("logistics_freight_currency_code") or "").strip().upper()
            == cycle.new_currency.strip().upper()
            and _numeric_equal(row.get("pkg_fee_weight"), cycle.new_fee_weight_g)
            and str(row.get("pkg_fee_weight_unit") or "").strip().casefold() == "g"
        ]

    async def _audited_wms_row(self, cycle: ReMarkCycle) -> Mapping[str, Any]:
        rows = await self._wms_rows(cycle)
        matches = [
            row
            for row in rows
            if _status(row) == 1
            and not str(row.get("waybill_no") or "").strip()
            and not str(row.get("tracking_no") or "").strip()
        ]
        if len(matches) != 1:
            raise ErpMarkManualReview(
                "审核发货意图已记录，但 OpenAPI 未返回唯一的待写物流销售出库单；"
                "禁止自动重复审核。"
            )
        return matches[0]

    def _advance(
        self,
        cycle: ReMarkCycle,
        new_state: str,
        *,
        expected: str,
        run_id: str | None,
        wo_number: str | None = None,
        timestamp_column: str | None = None,
    ) -> None:
        if not self.store.advance_re_mark_cycle(
            cycle.id,
            expected_state=expected,
            new_state=new_state,
            wo_number=wo_number,
            timestamp_column=timestamp_column,
            run_id=run_id,
        ):
            raise ErpMarkManualReview(
                f"重新标发周期状态已被其它任务修改（期望 {expected}）。"
            )

    def _required_cycle(self, cycle_id: int) -> ReMarkCycle:
        cycle = self.store.get_re_mark_cycle(cycle_id)
        if cycle is None:
            raise ErpMarkManualReview("重新标发周期已不存在。")
        return cycle

    @staticmethod
    def _progress(progress_func: ProgressFunc | None, message: str, percent: int) -> None:
        if progress_func is not None:
            progress_func(message, percent)


class ManagedShipmentReMarkFunc:
    """Create and close one OpenAPI client per desktop re-mark task."""

    def __init__(
        self,
        gateway_factory: GatewayFactory,
        configuration_provider: ConfigurationProvider,
        *,
        sleeper: SleepFunc = asyncio.sleep,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.configuration_provider = configuration_provider
        self.sleeper = sleeper

    async def __call__(
        self,
        page: Any,
        store: ShipmentQueueStore,
        cycle_id: int,
        *,
        lease_owner: str,
        confirm_func: ConfirmFunc,
        runtime_guard_func: RuntimeGuardFunc | None = None,
        progress_func: ProgressFunc | None = None,
        run_id: str | None = None,
    ) -> Mapping[str, Any]:
        gateway, client = await self.gateway_factory()
        try:
            adapter = ApiErpMarkAdapter.from_configuration(
                gateway,
                self.configuration_provider(),
                sleeper=self.sleeper,
            )
            # Re-mark must recreate the pending-review/WMS sequence.  Never let
            # a global ordinary-mark preference collapse it into fast outbound.
            adapter.outbound_strategy = OutboundStrategy.STAGED
            workflow = ShipmentReMarkWorkflow(
                gateway,
                store,
                adapter,
                sleeper=self.sleeper,
            )
            result = await workflow.execute(
                page,
                cycle_id,
                lease_owner=lease_owner,
                confirm_func=confirm_func,
                runtime_guard_func=runtime_guard_func,
                progress_func=progress_func,
                run_id=run_id,
            )
            return result.to_payload()
        finally:
            try:
                await client.aclose()
            except Exception:
                pass


__all__ = [
    "ManagedShipmentReMarkFunc",
    "ShipmentReMarkResult",
    "ShipmentReMarkWorkflow",
]
