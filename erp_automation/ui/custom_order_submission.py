"""Per-attempt submission state, owned and updated by the Qt main thread."""

from __future__ import annotations

from dataclasses import dataclass

from erp_automation.contracts.controller import ControlResult, TaskSubmissionReceipt
from erp_automation.contracts.models import (
    CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY,
    SHIPMENT_SUBMISSION_ID_PAYLOAD_KEY,
    TaskArea,
    TaskRecord,
)


SUBMISSION_CHECK_SECONDS = 120.0


@dataclass
class CustomOrderSubmission:
    submission_id: str
    order_no: str
    started_at: float
    request_id: str = ""
    result: ControlResult | None = None
    task: TaskRecord | None = None
    check_required: bool = False
    acknowledged_at: float | None = None

    @property
    def awaiting_ack(self) -> bool:
        return self.task is None and (
            self.result is None
            or bool(self.result.details.get("submission_outcome_unknown"))
        )

    @property
    def awaiting_task(self) -> bool:
        return self.task is None and bool(self.result and self.result.accepted)

    @property
    def task_id(self) -> str:
        return self.task.task_id if self.task else str(
            self.result.task_id
            if self.result and self.result.accepted and self.result.task_id else ""
        )

    def apply_receipt(self, receipt: TaskSubmissionReceipt, *, now: float | None = None) -> bool:
        if (
            receipt.submission_id != self.submission_id
            or receipt.order_no != self.order_no
            or (self.request_id and receipt.request_id != self.request_id)
        ):
            return False
        # Snapshots can arrive before the original timeout callback. A task is
        # stronger evidence than that callback, or a later unknown result.
        if self.task is not None:
            return False
        if self.result is not None and not self.awaiting_ack:
            return False
        changed = self.result != receipt.result or self.request_id != receipt.request_id
        self.request_id = receipt.request_id
        self.result = receipt.result
        if not self.awaiting_ack:
            self.check_required = False
            self.acknowledged_at = self.started_at if now is None else now
        else:
            self.check_required |= bool(receipt.result.details.get("reconciliation_exhausted"))
        return changed

    def _matches_task(self, task: TaskRecord) -> bool:
        if task.area is not TaskArea.CUSTOMIZATION or task.order_no != self.order_no:
            return False
        return task.task_id == self.task_id or str(
            task.payload.get(CUSTOM_ORDER_SUBMISSION_ID_PAYLOAD_KEY) or ""
        ) == self.submission_id

    def observe_task(self, task: TaskRecord) -> bool:
        if not self._matches_task(task):
            return False
        if self.task is not None and (
            task.updated_at < self.task.updated_at
            or (self.task.status.terminal and not task.status.terminal)
        ):
            return False
        changed = task != self.task
        self.task = task
        self.check_required = False
        return changed

    def expire(self, now: float) -> bool:
        if (
            not self.check_required
            and (self.awaiting_ack or self.awaiting_task)
            and now - (
                self.acknowledged_at if self.acknowledged_at is not None else self.started_at
            ) >= SUBMISSION_CHECK_SECONDS
        ):
            self.check_required = True
            return True
        return False


@dataclass
class ShipmentSubmission(CustomOrderSubmission):
    """One shipment attempt, also disambiguating parcels of the same order."""

    logistics_no: str = ""

    def apply_receipt(self, receipt: TaskSubmissionReceipt, *, now: float | None = None) -> bool:
        if receipt.logistics_no != self.logistics_no:
            return False
        return super().apply_receipt(receipt, now=now)

    def _matches_task(self, task: TaskRecord) -> bool:
        return (
            task.area is TaskArea.SHIPMENT
            and task.order_no == self.order_no
            and str(task.payload.get("logistics_no") or "") == self.logistics_no
            and (
                task.task_id == self.task_id
                or str(task.payload.get(SHIPMENT_SUBMISSION_ID_PAYLOAD_KEY) or "")
                == self.submission_id
            )
        )
